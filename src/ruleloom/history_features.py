"""Point-in-time, language-neutral predicates derived from prior observations only."""

from __future__ import annotations

import re
from collections import Counter, deque
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from itertools import combinations
from pathlib import Path
from typing import cast

from ruleloom.models import FactEvidence, JsonObject, Observation, parse_timestamp
from ruleloom.packs.configured_paths import _compile_glob, _CompiledGlob

HISTORY_FEATURE_VERSION = "ruleloom-history-features/1"
HISTORY_FEATURE_VERSION_V3 = "ruleloom-history-features/2"
HISTORY_FEATURE_PREDICATES = (
    "crosses_codeowners_boundary",
    "missing_usual_cochange_partner",
    "touches_dormant_area",
    "touches_recent_change_hotspot",
)
OWNER_AREA_PREDICATES = ("owner_areas_at_least_2", "owner_areas_at_least_3")
GENERATED_ARTIFACT_PREDICATE = "touches_generated_artifact"
HISTORY_FEATURE_PREDICATES_V3 = (
    *HISTORY_FEATURE_PREDICATES,
    *OWNER_AREA_PREDICATES,
    GENERATED_ARTIFACT_PREDICATE,
)

_HOTSPOT_WINDOW = timedelta(days=90)
_HOTSPOT_MIN_TOUCHES = 3
_DORMANT_WINDOW = timedelta(days=365)
_COCHANGE_MIN_SUPPORT = 5
_COCHANGE_MIN_CONFIDENCE = 0.7
_MAX_PATHS_PER_OBSERVATION = 50
_MAX_PAIR_UPDATES = 5_000_000
_MAX_EVIDENCE_REASONS = 12
_MAX_CODEOWNERS_BYTES = 1024 * 1024
_MAX_CODEOWNERS_RULES = 10_000
_MAX_CODEOWNERS_MATCH_WORK = 1_000_000
_CODEOWNERS_BATCH_SIZE = 2_048
_CODEOWNERS_CONTENT_BATCH_SIZE = 32
_CODEOWNERS_LOCATIONS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
_GITATTRIBUTES_LOCATIONS = (".gitattributes",)
_MAX_GITATTRIBUTES_RULES = 10_000
_OBJECT_ID_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


@dataclass(slots=True)
class _HistoryState:
    recent: deque[tuple[datetime, tuple[str, ...]]]
    recent_touches: Counter[str]
    total_touches: Counter[str]
    partners: dict[str, Counter[str]]
    last_seen: dict[str, datetime]
    observations: int = 0
    pair_updates: int = 0
    pair_budget_exhausted: bool = False
    latest_instant: datetime | None = None
    time_windows_valid: bool = True


def _new_state() -> _HistoryState:
    return _HistoryState(
        deque(),
        Counter(),
        Counter(),
        {},
        {},
    )


def _paths(item: Observation) -> tuple[str, ...] | None:
    truncated = item.metadata.get("metadata_files_truncated")
    files_changed = item.metadata.get("files_changed")
    raw = item.metadata.get("changed_files")
    if (
        truncated != 0
        or isinstance(files_changed, bool)
        or not isinstance(files_changed, int)
        or not isinstance(raw, list)
        or not all(isinstance(path, str) and path for path in raw)
        or len(raw) != files_changed
    ):
        return None
    return tuple(sorted(set(cast(list[str], raw))))


def _cohort(item: Observation) -> str:
    kind = item.source.get("kind")
    if kind in {"git_commit", "git_range", "git_worktree"}:
        return "git"
    return "historical_change"


def _chunks(values: tuple[str, ...], size: int) -> Iterator[tuple[str, ...]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _codeowners_blob_index(
    root: Path,
    bases: tuple[str, ...],
    locations: tuple[str, ...] = _CODEOWNERS_LOCATIONS,
) -> tuple[dict[tuple[str, str], str], dict[tuple[str, str], str]]:
    from ruleloom.gitfacts import GitFactsError, _run_git_capped

    objects: dict[tuple[str, str], str] = {}
    failures: dict[tuple[str, str], str] = {}
    bases_per_batch = max(1, _CODEOWNERS_BATCH_SIZE // len(locations))
    for base_batch in _chunks(bases, bases_per_batch):
        batch = tuple(f"{base}:{location}" for base in base_batch for location in locations)
        try:
            stdout, _stderr, returncode = _run_git_capped(
                root,
                ("cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"),
                input_bytes=("\n".join(batch) + "\n").encode(),
                allow_lazy_fetch=False,
            )
        except GitFactsError:
            return {}, {
                (base, location): "git_codeowners_read_failed"
                for base in bases
                for location in locations
            }
        if returncode != 0:
            return {}, {
                (base, location): "git_codeowners_read_failed"
                for base in bases
                for location in locations
            }
        try:
            lines = stdout.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            return {}, {
                (base, location): "git_codeowners_read_failed"
                for base in bases
                for location in locations
            }
        if len(lines) != len(batch):
            return {}, {
                (base, location): "git_codeowners_read_failed"
                for base in bases
                for location in locations
            }
        for expression, line in zip(batch, lines, strict=True):
            base, location = expression.split(":", 1)
            fields = line.split()
            key = (base, location)
            if len(fields) == 2 and fields[1] == "missing":
                failures[key] = "codeowners_not_found"
            elif len(fields) != 3 or fields[1] != "blob":
                failures[key] = "codeowners_is_not_a_blob"
            else:
                try:
                    size = int(fields[2])
                except ValueError:
                    failures[key] = "git_codeowners_read_failed"
                    continue
                if size > _MAX_CODEOWNERS_BYTES:
                    failures[key] = "codeowners_exceeds_byte_limit"
                else:
                    objects[key] = fields[0]
    return objects, failures


def _codeowners_blob_contents(root: Path, object_ids: tuple[str, ...]) -> dict[str, bytes]:
    from ruleloom.gitfacts import GitFactsError, _run_git_capped

    contents: dict[str, bytes] = {}
    for batch in _chunks(object_ids, _CODEOWNERS_CONTENT_BATCH_SIZE):
        try:
            stdout, _stderr, returncode = _run_git_capped(
                root,
                ("cat-file", "--batch"),
                input_bytes=("\n".join(batch) + "\n").encode(),
                allow_lazy_fetch=False,
            )
        except GitFactsError:
            continue
        if returncode != 0:
            continue
        offset = 0
        for requested in batch:
            end = stdout.find(b"\n", offset)
            if end < 0:
                break
            try:
                header = stdout[offset:end].decode("ascii").split()
            except UnicodeDecodeError:
                break
            if len(header) != 3 or header[0] != requested or header[1] != "blob":
                break
            try:
                size = int(header[2])
            except ValueError:
                break
            start = end + 1
            finish = start + size
            if size > _MAX_CODEOWNERS_BYTES or finish >= len(stdout) or stdout[finish] != 10:
                break
            contents[requested] = stdout[start:finish]
            offset = finish + 1
    return contents


def _read_codeowners_batch(
    root: Path | None,
    bases: set[str],
    locations: tuple[str, ...] = _CODEOWNERS_LOCATIONS,
) -> dict[str, tuple[str | None, str]]:
    """Read one bounded snapshot document per base from the first matching location."""
    valid = tuple(sorted(base for base in bases if _OBJECT_ID_RE.fullmatch(base)))
    results: dict[str, tuple[str | None, str]] = {
        base: (None, "base_commit_unavailable") for base in bases.difference(valid)
    }
    if root is None or not valid:
        return results
    objects, failures = _codeowners_blob_index(root, valid, locations)
    selected: dict[str, tuple[str, str] | tuple[None, str]] = {}
    for base in valid:
        for location in locations:
            key = (base, location)
            if key in objects:
                selected[base] = (objects[key], location)
                break
            failure = failures.get(key, "git_codeowners_read_failed")
            if failure != "codeowners_not_found":
                selected[base] = (None, failure)
                break
        else:
            selected[base] = (None, "codeowners_not_found")
    object_ids = tuple(
        sorted({object_id for object_id, _location in selected.values() if object_id is not None})
    )
    contents = _codeowners_blob_contents(root, object_ids)
    for base, (object_id, location_or_reason) in selected.items():
        if object_id is None:
            results[base] = (None, location_or_reason)
            continue
        payload = contents.get(object_id)
        if payload is None:
            results[base] = (None, "git_codeowners_read_failed")
            continue
        try:
            results[base] = (payload.decode("utf-8"), location_or_reason)
        except UnicodeDecodeError:
            results[base] = (None, "codeowners_is_not_utf8")
    return results


def _normalize_codeowners_pattern(pattern: str) -> str | None:
    if (
        not pattern
        or pattern.startswith("!")
        or any(character in pattern for character in "\\[]{}")
    ):
        return None
    normalized = pattern.removeprefix("/")
    if normalized.endswith("/"):
        normalized += "**"
    if "/" not in normalized:
        normalized = f"**/{normalized}"
    return normalized


def parse_codeowners_rules(content: str) -> tuple[list[tuple[_CompiledGlob, tuple[str, ...]]], int]:
    """Compile supported CODEOWNERS rules; owner handles are returned transiently only."""
    rules: list[tuple[_CompiledGlob, tuple[str, ...]]] = []
    unsupported = 0
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        if len(fields) < 2:
            unsupported += 1
            continue
        normalized = _normalize_codeowners_pattern(fields[0])
        if normalized is None:
            unsupported += 1
            continue
        try:
            matcher = _compile_glob(normalized)
        except ValueError:
            unsupported += 1
            continue
        raw_owners = fields[1:]
        if "#" in raw_owners:
            raw_owners = raw_owners[: raw_owners.index("#")]
        if not raw_owners:
            unsupported += 1
            continue
        rules.append((matcher, tuple(sorted(set(raw_owners)))))
        if len(rules) > _MAX_CODEOWNERS_RULES:
            raise ValueError("codeowners_rule_limit_exceeded")
    return rules, unsupported


def parse_generated_attribute_globs(content: str) -> tuple[list[_CompiledGlob], int]:
    """Compile ``linguist-generated`` patterns from a ``.gitattributes`` document."""
    globs: list[_CompiledGlob] = []
    unsupported = 0
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        if len(fields) < 2:
            continue
        attributes = fields[1:]
        marked = any(
            attribute in {"linguist-generated", "linguist-generated=true"}
            for attribute in attributes
        )
        if (
            not marked
            or "-linguist-generated" in attributes
            or "linguist-generated=false" in attributes
        ):
            continue
        normalized = _normalize_codeowners_pattern(fields[0])
        if normalized is None:
            unsupported += 1
            continue
        try:
            globs.append(_compile_glob(normalized))
        except ValueError:
            unsupported += 1
            continue
        if len(globs) > _MAX_GITATTRIBUTES_RULES:
            raise ValueError("gitattributes_rule_limit_exceeded")
    return globs, unsupported


def _generated_attribute_paths(
    item: Observation,
    paths: tuple[str, ...],
    snapshots: dict[str, tuple[str | None, str]],
) -> tuple[tuple[str, ...], JsonObject]:
    raw_base = item.source.get("base")
    if not isinstance(raw_base, str):
        return (), {"status": "abstained", "reason": "base_commit_unavailable"}
    content, location_or_reason = snapshots.get(raw_base, (None, "base_commit_unavailable"))
    if content is None:
        return (), {"status": "abstained", "reason": location_or_reason}
    try:
        globs, unsupported = parse_generated_attribute_globs(content)
    except ValueError as exc:
        return (), {"status": "abstained", "reason": str(exc)}
    match_work = len(globs) * len(paths)
    if match_work > _MAX_CODEOWNERS_MATCH_WORK:
        return (), {
            "status": "abstained",
            "reason": "gitattributes_match_work_limit_exceeded",
            "limit": _MAX_CODEOWNERS_MATCH_WORK,
        }
    matched = tuple(
        path for path in paths if any(glob.matches(tuple(path.split("/"))) for glob in globs)
    )
    return matched, {
        "status": "available",
        "location": location_or_reason,
        "generated_rules": len(globs),
        "unsupported_rules": unsupported,
        "matched_paths": len(matched),
    }


def _codeowners_boundary(
    item: Observation,
    paths: tuple[str, ...],
    snapshots: dict[str, tuple[str | None, str]],
) -> tuple[int, JsonObject]:
    """Return the number of distinct owner sets touched plus an abstention-aware context."""
    raw_base = item.source.get("base")
    if not isinstance(raw_base, str):
        return 0, {"status": "abstained", "reason": "base_commit_unavailable"}
    content, location_or_reason = snapshots.get(raw_base, (None, "base_commit_unavailable"))
    if content is None:
        return 0, {"status": "abstained", "reason": location_or_reason}
    try:
        rules, unsupported = parse_codeowners_rules(content)
    except ValueError:
        return 0, {
            "status": "abstained",
            "reason": "codeowners_rule_limit_exceeded",
            "limit": _MAX_CODEOWNERS_RULES,
        }
    match_work = len(rules) * len(paths)
    if match_work > _MAX_CODEOWNERS_MATCH_WORK:
        return 0, {
            "status": "abstained",
            "reason": "codeowners_match_work_limit_exceeded",
            "rules": len(rules),
            "paths": len(paths),
            "estimated_match_work": match_work,
            "limit": _MAX_CODEOWNERS_MATCH_WORK,
        }
    owners: set[tuple[str, ...]] = set()
    matched_paths = 0
    for path in paths:
        selected: tuple[str, ...] | None = None
        components = tuple(path.split("/"))
        for matcher, owner_set in rules:
            if matcher.matches(components):
                selected = owner_set
        if selected:
            owners.add(selected)
            matched_paths += 1
    return len(owners), {
        "status": "available",
        "location": location_or_reason,
        "rules": len(rules),
        "unsupported_rules": unsupported,
        "matched_paths": matched_paths,
        "owner_boundaries": len(owners),
    }


def _sort_key(item: Observation) -> tuple[object, ...]:
    position = item.metadata.get("topological_index")
    if _cohort(item) == "git" and isinstance(position, int) and not isinstance(position, bool):
        return 0, position, item.id
    return 1, parse_timestamp(item.observed_at), item.id


def _expire_recent(state: _HistoryState, instant: datetime) -> None:
    while state.recent and instant - state.recent[0][0] > _HOTSPOT_WINDOW:
        _, paths = state.recent.popleft()
        for path in paths:
            state.recent_touches[path] -= 1
            if state.recent_touches[path] <= 0:
                del state.recent_touches[path]


def _derive(
    item: Observation,
    paths: tuple[str, ...],
    state: _HistoryState,
) -> tuple[set[str], dict[str, set[str]], JsonObject]:
    instant = parse_timestamp(item.observed_at)
    if state.latest_instant is not None and instant < state.latest_instant:
        state.time_windows_valid = False
    if state.time_windows_valid:
        _expire_recent(state, instant)
    reasons: dict[str, set[str]] = {}

    def record(predicate: str, reason: str) -> None:
        reasons.setdefault(predicate, set()).add(reason)

    for path in paths:
        if state.time_windows_valid:
            touches = state.recent_touches[path]
            if touches >= _HOTSPOT_MIN_TOUCHES:
                record(
                    "touches_recent_change_hotspot",
                    f"path:{path};prior_90d_touches:{touches}",
                )
            previous = state.last_seen.get(path)
            if previous is not None:
                age = instant - previous
                if age > _DORMANT_WINDOW:
                    record("touches_dormant_area", f"path:{path};dormant_days:{age.days}")
        path_touches = state.total_touches[path]
        if (
            path_touches
            and not state.pair_budget_exhausted
            and len(paths) <= _MAX_PATHS_PER_OBSERVATION
        ):
            for partner, support in state.partners.get(path, Counter()).items():
                confidence = support / path_touches
                if (
                    support >= _COCHANGE_MIN_SUPPORT
                    and confidence >= _COCHANGE_MIN_CONFIDENCE
                    and partner not in paths
                ):
                    record(
                        "missing_usual_cochange_partner",
                        f"path:{path};missing:{partner};support:{support};confidence:{confidence:.3f}",
                    )

    context: JsonObject = {
        "version": HISTORY_FEATURE_VERSION,
        "cohort": _cohort(item),
        "eligible_prior_observations": state.observations,
        "current_paths": len(paths),
        "hotspot_window_days": _HOTSPOT_WINDOW.days,
        "hotspot_min_touches": _HOTSPOT_MIN_TOUCHES,
        "dormant_days": _DORMANT_WINDOW.days,
        "cochange_min_support": _COCHANGE_MIN_SUPPORT,
        "cochange_min_confidence": _COCHANGE_MIN_CONFIDENCE,
        "max_paths_per_observation": _MAX_PATHS_PER_OBSERVATION,
        "pair_updates": state.pair_updates,
        "pair_budget_exhausted": state.pair_budget_exhausted,
        "cochange_feature_status": (
            "abstained_pair_budget_exhausted"
            if state.pair_budget_exhausted
            else "abstained_current_path_limit"
            if len(paths) > _MAX_PATHS_PER_OBSERVATION
            else "available"
        ),
        "time_features_status": (
            "available" if state.time_windows_valid else "abstained_non_monotonic_timestamps"
        ),
        "left_censored": True,
        "outcome_blind": True,
    }
    return set(reasons), reasons, context


def _update(
    state: _HistoryState,
    instant: datetime,
    paths: tuple[str, ...],
) -> None:
    state.observations += 1
    if state.latest_instant is not None and instant < state.latest_instant:
        state.time_windows_valid = False
    if state.time_windows_valid:
        state.recent.append((instant, paths))
        state.recent_touches.update(paths)
        state.latest_instant = instant
    state.total_touches.update(paths)
    if state.time_windows_valid:
        for path in paths:
            state.last_seen[path] = instant
    updates = len(paths) * (len(paths) - 1) // 2
    if len(paths) > _MAX_PATHS_PER_OBSERVATION or state.pair_updates + updates > _MAX_PAIR_UPDATES:
        if state.pair_updates + updates > _MAX_PAIR_UPDATES:
            state.pair_budget_exhausted = True
        return
    for left, right in combinations(paths, 2):
        state.partners.setdefault(left, Counter())[right] += 1
        state.partners.setdefault(right, Counter())[left] += 1
    state.pair_updates += updates


def _merge_fact_evidence(
    existing: FactEvidence | None,
    extractor: str,
    reasons: set[str],
) -> FactEvidence:
    """Append enrichment reasons to a fact the pack extractor may already have emitted."""
    prior = () if existing is None else existing.evidence
    combined = tuple(dict.fromkeys((*prior, *sorted(reasons))))[:_MAX_EVIDENCE_REASONS]
    return FactEvidence(kind="deterministic", extractor=extractor, evidence=combined)


def enrich_history_features(
    existing: list[Observation],
    collected: list[Observation],
    *,
    extractor: str,
    root: Path | None = None,
    pack_version: int = 2,
) -> list[Observation]:
    """Enrich collected generic-v2/v3 observations using strictly earlier snapshots.

    ``pack_version`` 3 additionally derives owner-area counts and
    ``linguist-generated`` artifact facts from the base snapshot.
    """

    collected_ids = {item.id for item in collected}
    bases = {base for item in collected if isinstance((base := item.source.get("base")), str)}
    codeowners_snapshots = _read_codeowners_batch(root, bases)
    attribute_snapshots = (
        _read_codeowners_batch(root, bases, _GITATTRIBUTES_LOCATIONS) if pack_version >= 3 else {}
    )
    records = [item for item in existing if item.id not in collected_ids]
    records.extend(collected)
    states: dict[tuple[str, str], _HistoryState] = {}
    enriched: dict[str, Observation] = {}
    for item in sorted(records, key=_sort_key):
        repository = item.source.get("repository")
        key = (str(repository), _cohort(item))
        state = states.setdefault(key, _new_state())
        paths = _paths(item)
        if item.id in collected_ids:
            metadata = dict(item.metadata)
            if paths is None:
                metadata["historical_context"] = {
                    "version": HISTORY_FEATURE_VERSION,
                    "status": "abstained",
                    "reason": "exact_changed_path_manifest_unavailable",
                    "outcome_blind": True,
                }
                enriched[item.id] = replace(item, metadata=metadata)
            else:
                facts, reasons, context = _derive(item, paths, state)
                owner_boundaries, codeowners = _codeowners_boundary(
                    item, paths, codeowners_snapshots
                )
                context["codeowners"] = codeowners
                if owner_boundaries >= 2:
                    facts.add("crosses_codeowners_boundary")
                    reasons["crosses_codeowners_boundary"] = {
                        "codeowners:multiple_owner_boundaries"
                    }
                if pack_version >= 3:
                    context["version"] = HISTORY_FEATURE_VERSION_V3
                    if owner_boundaries >= 2:
                        facts.add("owner_areas_at_least_2")
                        reasons["owner_areas_at_least_2"] = {
                            f"codeowners:owner_boundaries:{owner_boundaries}>=2"
                        }
                    if owner_boundaries >= 3:
                        facts.add("owner_areas_at_least_3")
                        reasons["owner_areas_at_least_3"] = {
                            f"codeowners:owner_boundaries:{owner_boundaries}>=3"
                        }
                    generated_paths, attributes = _generated_attribute_paths(
                        item, paths, attribute_snapshots
                    )
                    context["gitattributes"] = attributes
                    if generated_paths:
                        facts.add(GENERATED_ARTIFACT_PREDICATE)
                        reasons[GENERATED_ARTIFACT_PREDICATE] = {
                            f"path:{path};gitattributes:linguist-generated"
                            for path in generated_paths[:_MAX_EVIDENCE_REASONS]
                        }
                evidence = dict(item.fact_evidence)
                evidence.update(
                    {
                        predicate: _merge_fact_evidence(evidence.get(predicate), extractor, values)
                        for predicate, values in reasons.items()
                    }
                )
                metadata["historical_context"] = context
                enriched[item.id] = replace(
                    item,
                    facts=frozenset({*item.facts, *facts}),
                    fact_evidence=evidence,
                    metadata=metadata,
                )
        if paths is not None:
            _update(state, parse_timestamp(item.observed_at), paths)
    return [enriched[item.id] for item in collected]
