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
MAX_GLOBS_PER_KIND = 32
MAX_TOTAL_GLOBS = 256
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
class ConfiguredPathsConfig:
    """Canonical configuration for :mod:`configured_paths` version 1."""

    path_predicates: tuple[PathPredicateConfig, ...]

    def __post_init__(self) -> None:
        if isinstance(self.path_predicates, str) or not isinstance(
            self.path_predicates, tuple | list
        ):
            raise ModelError("pack_config.path_predicates must be an array")
        if not self.path_predicates:
            raise ModelError("pack_config.path_predicates must contain at least one predicate")
        if len(self.path_predicates) > MAX_PREDICATES:
            raise ModelError(
                f"pack_config.path_predicates supports at most {MAX_PREDICATES} predicates"
            )
        if not all(isinstance(item, PathPredicateConfig) for item in self.path_predicates):
            raise ModelError("pack_config.path_predicates contains an invalid predicate")
        predicates = cast(tuple[PathPredicateConfig, ...], tuple(self.path_predicates))
        names = [item.predicate for item in predicates]
        if len(names) != len(set(names)):
            raise ModelError("pack_config.path_predicates cannot contain duplicate predicates")
        total_globs = sum(len(item.include_paths) + len(item.exclude_paths) for item in predicates)
        if total_globs > MAX_TOTAL_GLOBS:
            raise ModelError(
                f"pack_config supports at most {MAX_TOTAL_GLOBS} include and exclude globs"
            )
        object.__setattr__(
            self,
            "path_predicates",
            tuple(sorted(predicates, key=lambda item: item.predicate)),
        )

    @property
    def predicates(self) -> tuple[str, ...]:
        return tuple(item.predicate for item in self.path_predicates)

    @property
    def hash(self) -> str:
        return content_hash(self.to_dict())

    @property
    def total_globs(self) -> int:
        return sum(
            len(item.include_paths) + len(item.exclude_paths) for item in self.path_predicates
        )

    def to_dict(self) -> JsonObject:
        return {
            "path_predicates": [item.to_dict() for item in self.path_predicates],
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> ConfiguredPathsConfig:
        unknown = set(value).difference({"path_predicates"})
        if unknown:
            raise ModelError("unknown pack_config fields: " + ", ".join(sorted(unknown)))
        if "path_predicates" not in value:
            raise ModelError("pack_config is missing required field: path_predicates")
        raw = value.get("path_predicates")
        if not isinstance(raw, list):
            raise ModelError("pack_config.path_predicates must be an array")
        predicates: list[PathPredicateConfig] = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict) or not all(isinstance(key, str) for key in item):
                raise ModelError(f"pack_config.path_predicates[{index}] must be an object")
            predicates.append(PathPredicateConfig.from_dict(item))
        return cls(tuple(predicates))


@dataclass(frozen=True, slots=True)
class _CompiledGlob:
    components: tuple[str | None, ...]
    component_weight: int

    def matches(self, path_components: tuple[str, ...]) -> bool:
        previous = [False] * (len(path_components) + 1)
        previous[0] = True
        for pattern_component in self.components:
            current = [False] * (len(path_components) + 1)
            if pattern_component is None:
                current[0] = previous[0]
                for path_index in range(1, len(path_components) + 1):
                    current[path_index] = previous[path_index] or current[path_index - 1]
            else:
                for path_index in range(1, len(path_components) + 1):
                    current[path_index] = previous[path_index - 1] and _component_matches(
                        pattern_component,
                        path_components[path_index - 1],
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
    components = tuple(None if component == "**" else component for component in pattern.split("/"))
    return _CompiledGlob(
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
    path_outer_weight = 0
    path_character_weight = 0
    for change in visible:
        component_count = change.path.count("/") + 1
        if (
            len(change.path) > MAX_EVIDENCE_PATH_LENGTH
            or component_count > MAX_EVIDENCE_PATH_COMPONENTS
        ):
            raise ValueError(
                "configured path extraction encountered a path beyond the safe matcher limits"
            )
        path_outer_weight += component_count + 1
        path_character_weight += len(change.path) + 1
    comparisons = len(visible) * config.total_globs
    if comparisons > MAX_MATCH_COMPARISONS:
        raise ValueError(
            f"configured path extraction requires {comparisons} potential glob comparisons; "
            f"the safe limit is {MAX_MATCH_COMPARISONS}"
        )
    compiled = configured_predicates(config)
    globs = tuple(glob for predicate in compiled for glob in predicate.globs)
    work_units = path_outer_weight * sum(
        len(glob.components) + 1 for glob in globs
    ) + path_character_weight * (1 + sum(glob.component_weight for glob in globs))
    if work_units > MAX_MATCH_WORK_UNITS:
        raise ValueError(
            f"configured path extraction requires {work_units} estimated matcher work units; "
            f"the safe limit is {MAX_MATCH_WORK_UNITS}"
        )
    reasons: dict[str, set[str]] = {}
    counts = {item.predicate: 0 for item in compiled}
    unmatched = 0
    overlapping = 0
    manifest = hashlib.sha256()
    for change in visible:
        path_components = tuple(change.path.split("/"))
        matched = tuple(item.predicate for item in compiled if item.matches(path_components))
        if not matched:
            unmatched += 1
        if len(matched) > 1:
            overlapping += 1
        for predicate in matched:
            _record_bounded_reason(reasons, predicate, change.path)
            counts[predicate] += 1
        manifest.update(
            json.dumps(
                [change.path, list(matched)],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        manifest.update(b"\n")
    result = finalize_extraction(evidence, reasons, extractor=EXTRACTOR, options=options)
    metadata = dict(result.metadata)
    metadata.update(
        {
            "configured_paths_config_hash": config.hash,
            "configured_path_match_counts": cast(JsonValue, counts),
            "configured_unmatched_files": unmatched,
            "configured_overlapping_files": overlapping,
            "configured_match_manifest_hash": manifest.hexdigest(),
        }
    )
    return PackExtraction(result.facts, result.provenance, metadata)


def ignores_content(_path: str) -> bool:
    return False
