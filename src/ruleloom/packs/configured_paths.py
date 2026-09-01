"""Declarative, language-neutral path predicates for heterogeneous repositories."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from typing import cast

from ruleloom.models import JsonObject, JsonValue, ModelError, content_hash, validate_predicate
from ruleloom.packs.base import (
    EVIDENCE_LIMIT,
    DiffEvidence,
    PackExtraction,
    PackOptions,
    finalize_extraction,
    is_internal_path,
)

NAME = "configured_paths"
VERSION = 1
EXTRACTOR = "ruleloom.configured_paths.git.v1"

MAX_PREDICATES = 32
MAX_PARTNER_PREDICATES = 32
MAX_GLOBS_PER_KIND = 32
MAX_TOTAL_GLOBS = 256
MISSING_PARTNER_PREFIX = "missing_partner_"
MAX_GLOB_LENGTH = 256
MAX_MATCH_COMPARISONS = 5_000_000
MAX_MATCH_WORK_UNITS = 200_000_000
MAX_EVIDENCE_PATH_LENGTH = 4096
MAX_EVIDENCE_PATH_COMPONENTS = 256
MAX_PREDICATE_LENGTH = 64


def _validate_glob(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ModelError(f"{field_name} must be a non-empty string")
    if (
        len(value) > MAX_GLOB_LENGTH
        or value.startswith(("/", ":"))
        or value.endswith("/")
        or "//" in value
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or any(0xD800 <= ord(character) <= 0xDFFF for character in value)
    ):
        raise ModelError(
            f"{field_name} must be a portable root-anchored glob without empty segments, "
            "backslashes, Git pathspec magic, or control characters"
        )
    parts = value.split("/")
    if any(part in {".", ".."} for part in parts):
        raise ModelError(f"{field_name} cannot contain '.' or '..' path segments")
    if any("**" in part and part != "**" for part in parts):
        raise ModelError(f"{field_name} may use '**' only as a complete path segment")
    if any("[" in part or "]" in part or "{" in part or "}" in part for part in parts):
        raise ModelError(
            f"{field_name} supports literals, '*', '?', and complete '**' segments only"
        )
    return value


def _string_array(value: object, field_name: str, *, required: bool) -> tuple[str, ...]:
    if not isinstance(value, list | tuple) or isinstance(value, str):
        raise ModelError(f"{field_name} must be an array of globs")
    if required and not value:
        raise ModelError(f"{field_name} must contain at least one glob")
    if len(value) > MAX_GLOBS_PER_KIND:
        raise ModelError(f"{field_name} supports at most {MAX_GLOBS_PER_KIND} globs")
    validated = tuple(_validate_glob(item, f"{field_name} item") for item in value)
    if len(validated) != len(set(validated)):
        raise ModelError(f"{field_name} cannot contain duplicate globs")
    return tuple(sorted(validated))


@dataclass(frozen=True, slots=True)
class PathPredicateConfig:
    """One Boolean predicate derived from repository-relative file paths."""

    predicate: str
    include_paths: tuple[str, ...]
    exclude_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_predicate(self.predicate, field_name="path predicate")
        if len(self.predicate) > MAX_PREDICATE_LENGTH:
            raise ModelError(
                f"configured path predicates must contain at most {MAX_PREDICATE_LENGTH} characters"
            )
        if self.predicate.startswith("not_"):
            raise ModelError("configured path predicates cannot start with reserved prefix 'not_'")
        if self.predicate == "touches_" or not self.predicate.startswith("touches_"):
            raise ModelError(
                "configured_paths@1 predicates must start with 'touches_' because the pack "
                "asserts path contact only"
            )
        object.__setattr__(
            self,
            "include_paths",
            _string_array(
                self.include_paths,
                f"pack_config path predicate {self.predicate!r} include_paths",
                required=True,
            ),
        )
        object.__setattr__(
            self,
            "exclude_paths",
            _string_array(
                self.exclude_paths,
                f"pack_config path predicate {self.predicate!r} exclude_paths",
                required=False,
            ),
        )

    def to_dict(self) -> JsonObject:
        return {
            "predicate": self.predicate,
            "include_paths": list(self.include_paths),
            "exclude_paths": list(self.exclude_paths),
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> PathPredicateConfig:
        unknown = set(value).difference({"predicate", "include_paths", "exclude_paths"})
        if unknown:
            raise ModelError(
                "unknown pack_config path predicate fields: " + ", ".join(sorted(unknown))
            )
        missing = {"predicate", "include_paths", "exclude_paths"}.difference(value)
        if missing:
            raise ModelError(
                "pack_config path predicate is missing required fields: "
                + ", ".join(sorted(missing))
            )
        predicate = value.get("predicate")
        if not isinstance(predicate, str):
            raise ModelError("pack_config path predicate name must be a string")
        return cls(
            predicate=predicate,
            include_paths=_string_array(
                value.get("include_paths"),
                f"pack_config path predicate {predicate!r} include_paths",
                required=True,
            ),
            exclude_paths=_string_array(
                value.get("exclude_paths"),
                f"pack_config path predicate {predicate!r} exclude_paths",
                required=False,
            ),
        )


@dataclass(frozen=True, slots=True)
class PartnerPredicateConfig:
    """A co-change omission: ``path`` changed while no ``partner`` path changed.

    The predicate is true only for the *violation*. It instantiates the
    relational pattern ``touches(C, P), usual_partner(P, Q), not touches(C, Q)``
    for one frozen, human-reviewed pair of globs.
    """

    predicate: str
    path: str
    partner: str

    def __post_init__(self) -> None:
        validate_predicate(self.predicate, field_name="partner predicate")
        if len(self.predicate) > MAX_PREDICATE_LENGTH:
            raise ModelError(
                f"partner predicates must contain at most {MAX_PREDICATE_LENGTH} characters"
            )
        if (
            not self.predicate.startswith(MISSING_PARTNER_PREFIX)
            or self.predicate == MISSING_PARTNER_PREFIX
        ):
            raise ModelError(
                f"partner predicates must start with {MISSING_PARTNER_PREFIX!r} because they "
                "assert a missing co-change only"
            )
        object.__setattr__(
            self, "path", _validate_glob(self.path, f"partner predicate {self.predicate!r} path")
        )
        object.__setattr__(
            self,
            "partner",
            _validate_glob(self.partner, f"partner predicate {self.predicate!r} partner"),
        )
        if self.path == self.partner:
            raise ModelError(f"partner predicate {self.predicate!r} path and partner must differ")

    def to_dict(self) -> JsonObject:
        return {"predicate": self.predicate, "path": self.path, "partner": self.partner}

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> PartnerPredicateConfig:
        unknown = set(value).difference({"predicate", "path", "partner"})
        if unknown:
            raise ModelError(
                "unknown pack_config partner predicate fields: " + ", ".join(sorted(unknown))
            )
        missing = {"predicate", "path", "partner"}.difference(value)
        if missing:
            raise ModelError(
                "pack_config partner predicate is missing required fields: "
                + ", ".join(sorted(missing))
            )
        predicate = value.get("predicate")
        path = value.get("path")
        partner = value.get("partner")
        if (
            not isinstance(predicate, str)
            or not isinstance(path, str)
            or not isinstance(partner, str)
        ):
            raise ModelError("pack_config partner predicate fields must be strings")
        return cls(predicate=predicate, path=path, partner=partner)


@dataclass(frozen=True, slots=True)
class ConfiguredPathsConfig:
    """Canonical instantiated-predicate configuration shared by configurable packs.

    ``configured_paths@1`` accepts ``path_predicates`` only and requires at least
    one; ``generic_changes@3`` accepts both families and an empty configuration.
    Empty families are omitted from the canonical form so existing
    ``configured_paths@1`` hashes are unchanged.
    """

    path_predicates: tuple[PathPredicateConfig, ...] = ()
    partner_predicates: tuple[PartnerPredicateConfig, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.path_predicates, str) or not isinstance(
            self.path_predicates, tuple | list
        ):
            raise ModelError("pack_config.path_predicates must be an array")
        if isinstance(self.partner_predicates, str) or not isinstance(
            self.partner_predicates, tuple | list
        ):
            raise ModelError("pack_config.partner_predicates must be an array")
        if len(self.path_predicates) > MAX_PREDICATES:
            raise ModelError(
                f"pack_config.path_predicates supports at most {MAX_PREDICATES} predicates"
            )
        if len(self.partner_predicates) > MAX_PARTNER_PREDICATES:
            raise ModelError(
                "pack_config.partner_predicates supports at most "
                f"{MAX_PARTNER_PREDICATES} predicates"
            )
        if not all(isinstance(item, PathPredicateConfig) for item in self.path_predicates):
            raise ModelError("pack_config.path_predicates contains an invalid predicate")
        if not all(isinstance(item, PartnerPredicateConfig) for item in self.partner_predicates):
            raise ModelError("pack_config.partner_predicates contains an invalid predicate")
        predicates = cast(tuple[PathPredicateConfig, ...], tuple(self.path_predicates))
        partners = cast(tuple[PartnerPredicateConfig, ...], tuple(self.partner_predicates))
        names = [item.predicate for item in predicates] + [item.predicate for item in partners]
        if len(names) != len(set(names)):
            raise ModelError("pack_config cannot contain duplicate predicates")
        total_globs = sum(
            len(item.include_paths) + len(item.exclude_paths) for item in predicates
        ) + 2 * len(partners)
        if total_globs > MAX_TOTAL_GLOBS:
            raise ModelError(
                f"pack_config supports at most {MAX_TOTAL_GLOBS} include, exclude, and "
                "partner globs"
            )
        object.__setattr__(
            self,
            "path_predicates",
            tuple(sorted(predicates, key=lambda item: item.predicate)),
        )
        object.__setattr__(
            self,
            "partner_predicates",
            tuple(sorted(partners, key=lambda item: item.predicate)),
        )

    @property
    def predicates(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                [item.predicate for item in self.path_predicates]
                + [item.predicate for item in self.partner_predicates]
            )
        )

    @property
    def path_predicate_names(self) -> tuple[str, ...]:
        return tuple(item.predicate for item in self.path_predicates)

    @property
    def is_empty(self) -> bool:
        return not self.path_predicates and not self.partner_predicates

    @property
    def hash(self) -> str:
        return content_hash(self.to_dict())

    @property
    def total_globs(self) -> int:
        return sum(
            len(item.include_paths) + len(item.exclude_paths) for item in self.path_predicates
        ) + 2 * len(self.partner_predicates)

    def to_dict(self) -> JsonObject:
        value: JsonObject = {}
        if self.path_predicates:
            value["path_predicates"] = [item.to_dict() for item in self.path_predicates]
        if self.partner_predicates:
            value["partner_predicates"] = [item.to_dict() for item in self.partner_predicates]
        return value

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> ConfiguredPathsConfig:
        unknown = set(value).difference({"path_predicates", "partner_predicates"})
        if unknown:
            raise ModelError("unknown pack_config fields: " + ", ".join(sorted(unknown)))
        raw = value.get("path_predicates", [])
        if not isinstance(raw, list):
            raise ModelError("pack_config.path_predicates must be an array")
        predicates: list[PathPredicateConfig] = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict) or not all(isinstance(key, str) for key in item):
                raise ModelError(f"pack_config.path_predicates[{index}] must be an object")
            predicates.append(PathPredicateConfig.from_dict(item))
        raw_partners = value.get("partner_predicates", [])
        if not isinstance(raw_partners, list):
            raise ModelError("pack_config.partner_predicates must be an array")
        partners: list[PartnerPredicateConfig] = []
        for index, item in enumerate(raw_partners):
            if not isinstance(item, dict) or not all(isinstance(key, str) for key in item):
                raise ModelError(f"pack_config.partner_predicates[{index}] must be an object")
            partners.append(PartnerPredicateConfig.from_dict(item))
        return cls(tuple(predicates), tuple(partners))


@dataclass(frozen=True, slots=True)
class _CompiledGlob:
    literal_prefix: tuple[str, ...]
    components: tuple[str | None, ...]
    component_weight: int

    def _prefix_work(self, path_components: tuple[str, ...]) -> tuple[int, bool]:
        if len(path_components) < len(self.literal_prefix):
            return 2, False
        work_units = 1
        for pattern_component, path_component in zip(
            self.literal_prefix,
            path_components,
            strict=False,
        ):
            work_units += len(pattern_component) + len(path_component) + 2
            if pattern_component != path_component:
                return work_units, False
        return work_units, True

    def estimated_work(self, path_components: tuple[str, ...]) -> int:
        prefix_work, prefix_matches = self._prefix_work(path_components)
        if not prefix_matches:
            return prefix_work
        remaining = path_components[len(self.literal_prefix) :]
        path_outer_weight = len(remaining) + 1
        path_character_weight = 1 + sum(len(component) + 1 for component in remaining)
        return (
            prefix_work
            + path_outer_weight * (len(self.components) + 1)
            + path_character_weight * (1 + self.component_weight)
        )

    def matches(self, path_components: tuple[str, ...]) -> bool:
        _, prefix_matches = self._prefix_work(path_components)
        if not prefix_matches:
            return False
        prefix_length = len(self.literal_prefix)
        remaining_length = len(path_components) - prefix_length
        previous = [False] * (remaining_length + 1)
        previous[0] = True
        for pattern_component in self.components:
            current = [False] * (remaining_length + 1)
            if pattern_component is None:
                current[0] = previous[0]
                for path_index in range(1, remaining_length + 1):
                    current[path_index] = previous[path_index] or current[path_index - 1]
            else:
                for path_index in range(1, remaining_length + 1):
                    current[path_index] = previous[path_index - 1] and _component_matches(
                        pattern_component,
                        path_components[prefix_length + path_index - 1],
                    )
            previous = current
        return previous[-1]


def _component_matches(pattern: str, value: str) -> bool:
    pattern_index = 0
    value_index = 0
    latest_star = -1
    retry_value_index = -1
    while value_index < len(value):
        if pattern_index < len(pattern) and pattern[pattern_index] == "*":
            latest_star = pattern_index
            retry_value_index = value_index
            pattern_index += 1
        elif pattern_index < len(pattern) and pattern[pattern_index] in {
            "?",
            value[value_index],
        }:
            pattern_index += 1
            value_index += 1
        elif latest_star >= 0:
            retry_value_index += 1
            value_index = retry_value_index
            pattern_index = latest_star + 1
        else:
            return False
    while pattern_index < len(pattern) and pattern[pattern_index] == "*":
        pattern_index += 1
    return pattern_index == len(pattern)


def _compile_glob(pattern: str) -> _CompiledGlob:
    raw_components = pattern.split("/")
    prefix_length = 0
    for component in raw_components:
        if component == "**" or "*" in component or "?" in component:
            break
        prefix_length += 1
    literal_prefix = tuple(raw_components[:prefix_length])
    components = tuple(
        None if component == "**" else component for component in raw_components[prefix_length:]
    )
    return _CompiledGlob(
        literal_prefix,
        components,
        sum(len(component) + 1 for component in components if component is not None),
    )


@dataclass(frozen=True, slots=True)
class _CompiledPredicate:
    predicate: str
    includes: tuple[_CompiledGlob, ...]
    excludes: tuple[_CompiledGlob, ...]

    def matches(self, path_components: tuple[str, ...]) -> bool:
        return any(item.matches(path_components) for item in self.includes) and not any(
            item.matches(path_components) for item in self.excludes
        )

    @property
    def globs(self) -> tuple[_CompiledGlob, ...]:
        return (*self.includes, *self.excludes)


@lru_cache(maxsize=128)
def configured_predicates(config: ConfiguredPathsConfig) -> tuple[_CompiledPredicate, ...]:
    return tuple(
        _CompiledPredicate(
            predicate=item.predicate,
            includes=tuple(_compile_glob(pattern) for pattern in item.include_paths),
            excludes=tuple(_compile_glob(pattern) for pattern in item.exclude_paths),
        )
        for item in config.path_predicates
    )


def _record_bounded_reason(reasons: dict[str, set[str]], predicate: str, path: str) -> None:
    evidence_reasons = reasons.setdefault(predicate, set())
    if len(evidence_reasons) < EVIDENCE_LIMIT:
        evidence_reasons.add(f"path:{path}")


@dataclass(frozen=True, slots=True)
class _CompiledPartner:
    predicate: str
    path: _CompiledGlob
    partner: _CompiledGlob
    partner_glob: str


@lru_cache(maxsize=128)
def configured_partners(config: ConfiguredPathsConfig) -> tuple[_CompiledPartner, ...]:
    return tuple(
        _CompiledPartner(
            predicate=item.predicate,
            path=_compile_glob(item.path),
            partner=_compile_glob(item.partner),
            partner_glob=item.partner,
        )
        for item in config.partner_predicates
    )


@dataclass(frozen=True, slots=True)
class ConfiguredMatchResult:
    """Deterministic matches of one visible path manifest against a frozen config."""

    matched: tuple[tuple[str, ...], ...]
    counts: dict[str, int]
    unmatched: int
    overlapping: int
    manifest_hash: str
    partner_status: dict[str, str]
    partner_evidence: dict[str, str]


def configured_matches(
    paths: tuple[str, ...], config: ConfiguredPathsConfig
) -> ConfiguredMatchResult:
    """Match sorted visible paths against path and partner predicates under bounded work."""

    path_components: list[tuple[str, ...]] = []
    for path in paths:
        component_count = path.count("/") + 1
        if len(path) > MAX_EVIDENCE_PATH_LENGTH or component_count > MAX_EVIDENCE_PATH_COMPONENTS:
            raise ValueError(
                "configured path extraction encountered a path beyond the safe matcher limits"
            )
        path_components.append(tuple(path.split("/")))
    comparisons = len(paths) * config.total_globs
    if comparisons > MAX_MATCH_COMPARISONS:
        raise ValueError(
            f"configured path extraction requires {comparisons} potential glob comparisons; "
            f"the safe limit is {MAX_MATCH_COMPARISONS}"
        )
    compiled = configured_predicates(config)
    partners = configured_partners(config)
    globs = (
        *(glob for predicate in compiled for glob in predicate.globs),
        *(glob for partner in partners for glob in (partner.path, partner.partner)),
    )
    work_units = 0
    for components in path_components:
        for glob in globs:
            work_units += glob.estimated_work(components)
            if work_units > MAX_MATCH_WORK_UNITS:
                raise ValueError(
                    "configured path extraction exceeds the safe limit of "
                    f"{MAX_MATCH_WORK_UNITS} estimated matcher work units"
                )
    counts = {item.predicate: 0 for item in compiled}
    matched_rows: list[tuple[str, ...]] = []
    unmatched = 0
    overlapping = 0
    manifest = hashlib.sha256()
    for path, components in zip(paths, path_components, strict=True):
        matched = tuple(item.predicate for item in compiled if item.matches(components))
        if not matched:
            unmatched += 1
        if len(matched) > 1:
            overlapping += 1
        for predicate in matched:
            counts[predicate] += 1
        matched_rows.append(matched)
        manifest.update(
            json.dumps(
                [path, list(matched)],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        manifest.update(b"\n")
    partner_status: dict[str, str] = {}
    partner_evidence: dict[str, str] = {}
    for partner in partners:
        path_hits = [
            path
            for path, components in zip(paths, path_components, strict=True)
            if partner.path.matches(components)
        ]
        if not path_hits:
            partner_status[partner.predicate] = "inactive"
            continue
        partner_hit = any(partner.partner.matches(components) for components in path_components)
        if partner_hit:
            partner_status[partner.predicate] = "satisfied"
        else:
            partner_status[partner.predicate] = "violated"
            partner_evidence[partner.predicate] = (
                f"path:{path_hits[0]};missing:{partner.partner_glob}"
            )
        manifest.update(
            json.dumps(
                [partner.predicate, partner_status[partner.predicate]],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        manifest.update(b"\n")
    return ConfiguredMatchResult(
        matched=tuple(matched_rows),
        counts=counts,
        unmatched=unmatched,
        overlapping=overlapping,
        manifest_hash=manifest.hexdigest(),
        partner_status=partner_status,
        partner_evidence=partner_evidence,
    )


def extract_configured_path_facts(
    evidence: DiffEvidence,
    options: PackOptions,
    config: ConfiguredPathsConfig,
) -> PackExtraction:
    """Project configured paths into Boolean facts without inspecting file contents."""

    visible = tuple(
        sorted(
            (change for change in evidence.changes if not is_internal_path(change.path)),
            key=lambda item: item.path,
        )
    )
    matches = configured_matches(tuple(change.path for change in visible), config)
    reasons: dict[str, set[str]] = {}
    for change, matched in zip(visible, matches.matched, strict=True):
        for predicate in matched:
            _record_bounded_reason(reasons, predicate, change.path)
    result = finalize_extraction(evidence, reasons, extractor=EXTRACTOR, options=options)
    metadata = dict(result.metadata)
    metadata.update(
        {
            "configured_paths_config_hash": config.hash,
            "configured_path_match_counts": cast(JsonValue, matches.counts),
            "configured_unmatched_files": matches.unmatched,
            "configured_overlapping_files": matches.overlapping,
            "configured_match_manifest_hash": matches.manifest_hash,
        }
    )
    return PackExtraction(result.facts, result.provenance, metadata)


def ignores_content(_path: str) -> bool:
    return False
