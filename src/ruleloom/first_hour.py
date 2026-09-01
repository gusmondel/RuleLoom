"""Read-only, zero-configuration structural audit for a Git checkout.

The primary API needs only a repository root. It never creates RuleLoom state,
never reads labels, and never infers programming languages or test conventions.
Optional pre-existing observations and explicit repository assertions enrich the
same deterministic report when a configured vocabulary is available.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import cast

from ruleloom.gitfacts import GitFactsError, _run_git_capped, repository_identity
from ruleloom.history.git import GitHistoryError, collect_git_history
from ruleloom.history.models import ChangeUnit
from ruleloom.models import JsonObject, JsonValue, ModelError, Observation, content_hash
from ruleloom.packs.base import is_internal_path
from ruleloom.predicate_audit import audit_predicates
from ruleloom.repository_assertions import (
    RepositoryAssertionDeclaration,
    audit_repository_assertions,
)

FIRST_HOUR_REPORT_SCHEMA_VERSION = 1
FIRST_HOUR_REPORT_ENGINE_VERSION = "ruleloom-first-hour/0.1"

_MAX_COMMITS = 10_000
_MAX_OUTPUT_ROWS = 1_000
_MAX_COHANGE_PATHS_PER_COMMIT = 500
_MAX_PAIR_UPDATES = 5_000_000
_MAX_TOTAL_PATH_ENTRIES = 2_000_000
_MAX_DIFF_BATCH_SIZE = 512


class FirstHourAuditError(RuntimeError):
    """Raised when a read-only repository audit cannot complete safely."""


@dataclass(frozen=True, slots=True)
class RepositoryAuditLimits:
    """Explicit work and output budgets for a zero-configuration audit."""

    max_commits: int = 500
    max_hotspots: int = 25
    max_cochanges: int = 50
    min_cochange_count: int = 2
    max_cochange_paths_per_commit: int = 200
    max_pair_updates: int = 2_000_000
    max_total_path_entries: int = 1_000_000
    diff_batch_size: int = 128

    def __post_init__(self) -> None:
        _bounded_integer(self.max_commits, "max_commits", maximum=_MAX_COMMITS)
        _bounded_integer(self.max_hotspots, "max_hotspots", maximum=_MAX_OUTPUT_ROWS)
        _bounded_integer(self.max_cochanges, "max_cochanges", maximum=_MAX_OUTPUT_ROWS)
        _bounded_integer(
            self.min_cochange_count,
            "min_cochange_count",
            maximum=_MAX_COMMITS,
        )
        _bounded_integer(
            self.max_cochange_paths_per_commit,
            "max_cochange_paths_per_commit",
            maximum=_MAX_COHANGE_PATHS_PER_COMMIT,
        )
        _bounded_integer(
            self.max_pair_updates,
            "max_pair_updates",
            maximum=_MAX_PAIR_UPDATES,
        )
        _bounded_integer(
            self.max_total_path_entries,
            "max_total_path_entries",
            maximum=_MAX_TOTAL_PATH_ENTRIES,
        )
        _bounded_integer(
            self.diff_batch_size,
            "diff_batch_size",
            maximum=_MAX_DIFF_BATCH_SIZE,
        )

    def to_dict(self) -> JsonObject:
        return {
            "max_commits": self.max_commits,
            "max_hotspots": self.max_hotspots,
            "max_cochanges": self.max_cochanges,
            "min_cochange_count": self.min_cochange_count,
            "max_cochange_paths_per_commit": self.max_cochange_paths_per_commit,
            "max_pair_updates": self.max_pair_updates,
            "max_total_path_entries": self.max_total_path_entries,
            "diff_batch_size": self.diff_batch_size,
        }


def _bounded_integer(value: int, name: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ModelError(f"{name} must be an integer between 1 and {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class _PathChange:
    path: str
    additions: int
    deletions: int
    binary: bool

    @property
    def churn(self) -> int:
        return self.additions + self.deletions


@dataclass(frozen=True, slots=True)
class _CommitDiff:
    commit: str
    base: str
    committed_at: str
    additions: int
    deletions: int
    changes: tuple[_PathChange, ...]
    excluded_internal_files: int
    binary_files: int

    @property
    def churn(self) -> int:
        return self.additions + self.deletions

    @property
    def files_changed(self) -> int:
        return len(self.changes)

    def manifest_record(self) -> JsonObject:
        return {
            "commit": self.commit,
            "base": self.base,
            "committed_at": self.committed_at,
            "additions": self.additions,
            "deletions": self.deletions,
            "binary_files": self.binary_files,
            "excluded_internal_files": self.excluded_internal_files,
            "changes": [
                {
                    "path": item.path,
                    "additions": item.additions,
                    "deletions": item.deletions,
                    "binary": item.binary,
                }
                for item in self.changes
            ],
        }


def _parse_numstat_record(raw_record: bytes, *, commit: str) -> _PathChange:
    try:
        text = raw_record.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FirstHourAuditError(
            f"Git returned a non-UTF-8 path while auditing commit {commit}; "
            "lossy decoding is refused"
        ) from exc
    fields = text.split("\t", 2)
    if len(fields) != 3 or not fields[2]:
        raise FirstHourAuditError(f"Git returned malformed numstat evidence for commit {commit}")
    raw_additions, raw_deletions, path = fields
    binary = raw_additions == "-" and raw_deletions == "-"
    if binary:
        additions = deletions = 0
    else:
        try:
            additions = int(raw_additions)
            deletions = int(raw_deletions)
        except ValueError as exc:
            raise FirstHourAuditError(
                f"Git returned invalid numstat counts for commit {commit}"
            ) from exc
        if additions < 0 or deletions < 0:
            raise FirstHourAuditError(f"Git returned negative numstat counts for commit {commit}")
    return _PathChange(path, additions, deletions, binary)


def _nul_records(payload: bytes) -> Iterator[bytes]:
    """Yield NUL-delimited records without allocating a second full token list."""
    start = 0
    while start < len(payload):
        end = payload.find(b"\x00", start)
        if end < 0:
            end = len(payload)
        if end > start:
            yield payload[start:end]
        start = end + 1


def _validate_batch_path_budget(max_path_entries: int | None) -> None:
    if max_path_entries is not None and (
        isinstance(max_path_entries, bool)
        or not isinstance(max_path_entries, int)
        or max_path_entries < 0
    ):
        raise FirstHourAuditError("max_path_entries must be an integer >= 0")


def _collect_numstat_batch(
    root: Path,
    units: tuple[ChangeUnit, ...],
    *,
    max_path_entries: int | None = None,
) -> tuple[tuple[_PathChange, ...], ...]:
    _validate_batch_path_budget(max_path_entries)
    # ``diff-tree --stdin`` consumes ``<commit> [<parent>...]`` records, not
    # ordinary left/right diff arguments.  Head must therefore precede base to
    # preserve additions/deletions in the base -> prediction direction.
    payload = "".join(f"{unit.prediction_sha} {unit.base_sha}\n" for unit in units).encode()
    try:
        stdout, stderr, returncode = _run_git_capped(
            root,
            (
                "diff-tree",
                "--stdin",
                "--always",
                "-r",
                "--numstat",
                "-z",
                "--no-renames",
            ),
            input_bytes=payload,
        )
    except GitFactsError as exc:
        raise FirstHourAuditError(str(exc)) from exc
    if returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip() or "unknown Git error"
        raise FirstHourAuditError(f"git diff-tree --stdin failed: {detail}")
    results: list[list[_PathChange]] = []
    current_index = -1
    parsed_path_entries = 0
    for token in _nul_records(stdout):
        if b"\t" not in token:
            current_index += 1
            if current_index >= len(units):
                raise FirstHourAuditError("Git returned an unexpected extra diff boundary")
            expected = units[current_index].prediction_sha.encode("ascii")
            if token != expected:
                raise FirstHourAuditError(
                    "Git diff-tree boundary does not match the requested range order"
                )
            results.append([])
            continue
        if current_index < 0:
            raise FirstHourAuditError("Git returned numstat evidence before its range boundary")
        if max_path_entries is not None and parsed_path_entries >= max_path_entries:
            raise FirstHourAuditError(
                "repository audit exceeds max_total_path_entries while parsing a Git diff batch"
            )
        change = _parse_numstat_record(token, commit=units[current_index].prediction_sha)
        parsed_path_entries += 1
        results[current_index].append(change)
    if len(results) != len(units):
        raise FirstHourAuditError("Git returned an incomplete diff batch")
    return tuple(tuple(item) for item in results)


def _collect_numstat_single(
    root: Path,
    unit: ChangeUnit,
    *,
    max_path_entries: int | None = None,
) -> tuple[_PathChange, ...]:
    """Collect one range that ``diff-tree --stdin`` cannot represent.

    Git's canonical empty-tree object is a tree rather than a commit.  A root
    commit therefore has no ``diff-tree --stdin`` boundary, even though
    ``git diff <empty-tree> <root-commit>`` is well-defined.  Keeping this one
    exceptional process preserves the existing root-commit evidence while all
    ordinary commit ranges use the batch transport.
    """
    _validate_batch_path_budget(max_path_entries)
    try:
        stdout, stderr, returncode = _run_git_capped(
            root,
            (
                "diff",
                "--numstat",
                "-z",
                "--no-renames",
                unit.base_sha,
                unit.prediction_sha,
                "--",
            ),
        )
    except GitFactsError as exc:
        raise FirstHourAuditError(str(exc)) from exc
    if returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip() or "unknown Git error"
        raise FirstHourAuditError(f"git diff failed: {detail}")
    changes: list[_PathChange] = []
    for parsed_path_entries, record in enumerate(_nul_records(stdout)):
        if max_path_entries is not None and parsed_path_entries >= max_path_entries:
            raise FirstHourAuditError(
                "repository audit exceeds max_total_path_entries while parsing a root diff"
            )
        change = _parse_numstat_record(record, commit=unit.prediction_sha)
        changes.append(change)
    return tuple(changes)


def _collect_diffs(
    root: Path,
    *,
    ref: str,
    limits: RepositoryAuditLimits,
) -> tuple[
    tuple[_CommitDiff, ...],
    JsonObject,
    str,
    tuple[str, ...],
]:
    try:
        history = collect_git_history(root, ref=ref, max_commits=limits.max_commits)
        repository_id = repository_identity(root)
    except GitHistoryError as exc:
        raise FirstHourAuditError(str(exc)) from exc
    except RuntimeError as exc:
        raise FirstHourAuditError(str(exc)) from exc

    units = tuple(history.units)
    root_predictions = frozenset(
        event.source_ref
        for event in history.events
        if isinstance(event.data.get("parents"), list) and not event.data["parents"]
    )
    ordinary_units = tuple(unit for unit in units if unit.prediction_sha not in root_predictions)
    diffs_by_prediction: dict[str, _CommitDiff] = {}
    parsed_path_entries = 0

    def retain_batch(
        batch_units: tuple[ChangeUnit, ...],
        batch_changes: tuple[tuple[_PathChange, ...], ...],
    ) -> None:
        """Retain one complete batch only when it fits the global path budget."""
        nonlocal parsed_path_entries
        prepared: list[_CommitDiff] = []
        batch_path_entries = 0
        for unit, all_changes in zip(batch_units, batch_changes, strict=True):
            visible = tuple(item for item in all_changes if not is_internal_path(item.path))
            batch_path_entries += len(all_changes)
            prepared.append(
                _CommitDiff(
                    commit=unit.prediction_sha,
                    base=unit.base_sha,
                    committed_at=unit.prediction_at,
                    additions=sum(item.additions for item in visible),
                    deletions=sum(item.deletions for item in visible),
                    changes=visible,
                    excluded_internal_files=len(all_changes) - len(visible),
                    binary_files=sum(item.binary for item in visible),
                )
            )
        if parsed_path_entries + batch_path_entries > limits.max_total_path_entries:
            raise FirstHourAuditError(
                "repository audit exceeds max_total_path_entries; lower max_commits or "
                "raise the explicit audit budget"
            )
        parsed_path_entries += batch_path_entries
        diffs_by_prediction.update((item.commit, item) for item in prepared)

    for offset in range(0, len(ordinary_units), limits.diff_batch_size):
        batch_units = ordinary_units[offset : offset + limits.diff_batch_size]
        batch_changes = _collect_numstat_batch(
            root,
            batch_units,
            max_path_entries=limits.max_total_path_entries - parsed_path_entries,
        )
        retain_batch(batch_units, batch_changes)
        del batch_changes
    for unit in units:
        if unit.prediction_sha in root_predictions:
            retain_batch(
                (unit,),
                (
                    _collect_numstat_single(
                        root,
                        unit,
                        max_path_entries=(limits.max_total_path_entries - parsed_path_entries),
                    ),
                ),
            )

    diffs = tuple(diffs_by_prediction[unit.prediction_sha] for unit in units)
    topology: JsonObject = {
        "commit_count": len(diffs),
        "root_commits": 0,
        "merge_commits": sum(unit.kind == "git_merge" for unit in history.units),
        "resolved_ref": history.resolved_ref,
        "first_commit_at": diffs[0].committed_at if diffs else None,
        "last_commit_at": diffs[-1].committed_at if diffs else None,
        "shallow": history.shallow,
        "truncated": history.truncated,
        "selection": "reachable_commits_date_order",
        "git_history_manifest_hash": history.manifest_hash,
    }
    # ChangeUnit does not expose a root marker, but its first commit compares
    # against Git's empty-tree object rather than a parent. Recompute roots from
    # the normalized Git event parent lists without inspecting commit prose.
    topology["root_commits"] = sum(
        isinstance(event.data.get("parents"), list) and not event.data["parents"]
        for event in history.events
    )
    warnings = list(history.warnings)
    return diffs, topology, repository_id, tuple(warnings)


def collect_commit_diffs(
    root: Path,
    *,
    ref: str = "HEAD",
    limits: RepositoryAuditLimits | None = None,
) -> tuple[tuple[_CommitDiff, ...], JsonObject, str, tuple[str, ...]]:
    """Public, read-only access to bounded per-commit path churn for other auditors.

    Returns the commit diffs (oldest first), the topology summary, the repository
    identity, and collection warnings. It never reads outcomes or file contents.
    """

    return _collect_diffs(root, ref=ref, limits=limits or RepositoryAuditLimits())


def _nearest_rank(values: list[int], probability: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    if probability <= 0:
        return ordered[0]
    rank = math.ceil(probability * len(ordered))
    return ordered[rank - 1]


def _distribution(values: list[int]) -> JsonObject:
    return {
        "count": len(values),
        "minimum": _nearest_rank(values, 0),
        "p25": _nearest_rank(values, 0.25),
        "median": _nearest_rank(values, 0.50),
        "p75": _nearest_rank(values, 0.75),
        "p90": _nearest_rank(values, 0.90),
        "p95": _nearest_rank(values, 0.95),
        "maximum": _nearest_rank(values, 1),
        "method": "nearest_rank",
    }


def _volume(diffs: tuple[_CommitDiff, ...]) -> JsonObject:
    return {
        "total_additions": sum(item.additions for item in diffs),
        "total_deletions": sum(item.deletions for item in diffs),
        "total_churn": sum(item.churn for item in diffs),
        "total_file_entries": sum(item.files_changed for item in diffs),
        "binary_file_entries": sum(item.binary_files for item in diffs),
        "excluded_internal_file_entries": sum(item.excluded_internal_files for item in diffs),
        "churn_per_commit": _distribution([item.churn for item in diffs]),
        "files_per_commit": _distribution([item.files_changed for item in diffs]),
    }


def _hotspots(diffs: tuple[_CommitDiff, ...], limit: int) -> list[JsonValue]:
    touches: Counter[str] = Counter()
    additions: Counter[str] = Counter()
    deletions: Counter[str] = Counter()
    binaries: Counter[str] = Counter()
    for diff in diffs:
        for change in diff.changes:
            touches[change.path] += 1
            additions[change.path] += change.additions
            deletions[change.path] += change.deletions
            binaries[change.path] += change.binary
    ordered = sorted(
        touches,
        key=lambda path: (
            -touches[path],
            -(additions[path] + deletions[path]),
            path,
        ),
    )
    return [
        {
            "path": path,
            "change_count": touches[path],
            "additions": additions[path],
            "deletions": deletions[path],
            "churn": additions[path] + deletions[path],
            "binary_change_count": binaries[path],
        }
        for path in ordered[:limit]
    ]


def _cochanges(
    diffs: tuple[_CommitDiff, ...],
    limits: RepositoryAuditLimits,
) -> tuple[list[JsonValue], JsonObject, tuple[str, ...]]:
    touches: Counter[str] = Counter()
    pairs: Counter[tuple[str, str]] = Counter()
    eligible_commits = 0
    skipped_large = 0
    skipped_budget = 0
    pair_updates = 0
    for diff in diffs:
        paths = tuple(sorted({item.path for item in diff.changes}))
        updates = len(paths) * (len(paths) - 1) // 2
        if len(paths) > limits.max_cochange_paths_per_commit:
            skipped_large += 1
            continue
        if pair_updates + updates > limits.max_pair_updates:
            skipped_budget += 1
            continue
        touches.update(paths)
        pairs.update(combinations(paths, 2))
        pair_updates += updates
        eligible_commits += 1
    selected = [
        (pair, count) for pair, count in pairs.items() if count >= limits.min_cochange_count
    ]
    selected.sort(key=lambda item: (-item[1], item[0][0], item[0][1]))
    rows: list[JsonValue] = []
    for (left, right), count in selected[: limits.max_cochanges]:
        union = touches[left] + touches[right] - count
        rows.append(
            {
                "left_path": left,
                "right_path": right,
                "cochange_count": count,
                "left_change_count": touches[left],
                "right_change_count": touches[right],
                "jaccard": count / union if union else None,
                "left_given_right": count / touches[right] if touches[right] else None,
                "right_given_left": count / touches[left] if touches[left] else None,
            }
        )
    warnings: list[str] = []
    if skipped_large:
        warnings.append(
            f"co-change excluded {skipped_large} commit(s) above the per-commit path budget"
        )
    if skipped_budget:
        warnings.append(
            f"co-change excluded {skipped_budget} commit(s) after the pair-update budget"
        )
    coverage: JsonObject = {
        "eligible_commits": eligible_commits,
        "skipped_large_commits": skipped_large,
        "skipped_budget_commits": skipped_budget,
        "pair_updates": pair_updates,
        "minimum_pair_count": limits.min_cochange_count,
    }
    return rows, coverage, tuple(warnings)


def _observation_enrichment(
    observations: tuple[Observation, ...],
    predicate_vocabulary: tuple[str, ...],
    configured_predicates: tuple[str, ...],
) -> tuple[JsonObject, JsonObject]:
    if not observations:
        return (
            {
                "status": "not_supplied",
                "reason": "No materialized observations were supplied.",
            },
            {
                "status": "not_supplied",
                "reason": "Predicate quality requires materialized deterministic facts.",
            },
        )
    identifiers = [item.id for item in observations]
    if len(identifiers) != len(set(identifiers)):
        raise ModelError("first-hour observation ids must be unique")
    vocabulary = tuple(sorted(set(predicate_vocabulary)))
    if not vocabulary:
        vocabulary = tuple(sorted({fact for item in observations for fact in item.facts}))
    audit = audit_predicates(
        observations,
        vocabulary,
        configured_predicates=configured_predicates,
    )
    protocols = sorted({item.protocol_hash for item in observations})
    repositories = sorted(
        {
            repository
            for item in observations
            if isinstance((repository := item.source.get("repository")), str)
        }
    )
    enrichment: JsonObject = {
        "status": "available",
        "observation_count": len(observations),
        "protocol_hashes": cast(JsonValue, protocols),
        "repository_bindings": cast(JsonValue, repositories),
        "configured_predicates": cast(JsonValue, list(sorted(set(configured_predicates)))),
    }
    return enrichment, {"status": "available", "audit": audit}


def _assertion_enrichment(
    root: Path,
    declaration: RepositoryAssertionDeclaration | None,
    observations: tuple[Observation, ...],
) -> tuple[JsonObject, JsonObject]:
    if declaration is None:
        return (
            {
                "status": "not_configured",
                "reason": (
                    "Test structure is never inferred from names; explicit test_structure "
                    "assertions are required."
                ),
            },
            {"status": "not_configured"},
        )
    if not observations:
        raise ModelError(
            "repository assertion enrichment requires materialized observations from its "
            "frozen predicate vocabulary"
        )
    audit = audit_repository_assertions(root, declaration, list(observations))
    test_rows = [item for item in audit.rows if item.category == "test_structure"]
    if not test_rows:
        test_section: JsonObject = {
            "status": "not_configured",
            "reason": "The declaration contains no explicit test_structure assertion.",
        }
    else:
        test_section = {
            "status": "available",
            "assertion_count": len(test_rows),
            "eligible_assertion_observation_pairs": sum(
                item.eligible_observations for item in test_rows
            ),
            "expectation_absent_pairs": sum(item.expectation_absent for item in test_rows),
            "rows": [item.to_dict() for item in test_rows],
        }
    return test_section, {"status": "available", "audit": audit.to_dict()}


@dataclass(frozen=True, slots=True)
class FirstHourReport:
    """Deterministic report generated entirely from read-only local evidence."""

    repository_id: str
    ref: str
    limits: RepositoryAuditLimits
    topology: JsonObject
    volume: JsonObject
    hotspots: tuple[JsonValue, ...]
    cochanges: tuple[JsonValue, ...]
    coverage: JsonObject
    predicate_quality: JsonObject
    structural_test_expectations: JsonObject
    assertion_adherence: JsonObject
    evidence_manifest_hash: str
    warnings: tuple[str, ...]
    limitations: tuple[str, ...]
    schema_version: int = FIRST_HOUR_REPORT_SCHEMA_VERSION
    engine_version: str = FIRST_HOUR_REPORT_ENGINE_VERSION
    outcome_blind: bool = True
    read_only: bool = True

    def __post_init__(self) -> None:
        if self.schema_version != FIRST_HOUR_REPORT_SCHEMA_VERSION:
            raise ModelError("unsupported first-hour report schema version")
        if self.engine_version != FIRST_HOUR_REPORT_ENGINE_VERSION:
            raise ModelError("unsupported first-hour report engine version")
        if not self.outcome_blind or not self.read_only:
            raise ModelError("first-hour reports must remain outcome-blind and read-only")
        if not self.repository_id or not self.ref:
            raise ModelError("first-hour report repository_id and ref cannot be empty")
        if len(self.evidence_manifest_hash) != 64 or any(
            item not in "0123456789abcdef" for item in self.evidence_manifest_hash
        ):
            raise ModelError("first-hour report evidence_manifest_hash must be SHA-256")
        if not self.limitations:
            raise ModelError("first-hour report limitations cannot be empty")

    def payload(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "engine_version": self.engine_version,
            "outcome_blind": self.outcome_blind,
            "read_only": self.read_only,
            "repository_id": self.repository_id,
            "ref": self.ref,
            "limits": self.limits.to_dict(),
            "topology": self.topology,
            "volume": self.volume,
            "hotspots": list(self.hotspots),
            "cochanges": list(self.cochanges),
            "coverage": self.coverage,
            "predicate_quality": self.predicate_quality,
            "structural_test_expectations": self.structural_test_expectations,
            "assertion_adherence": self.assertion_adherence,
            "evidence_manifest_hash": self.evidence_manifest_hash,
            "warnings": list(self.warnings),
            "limitations": list(self.limitations),
        }

    @property
    def manifest_hash(self) -> str:
        return content_hash(self.payload())

    def to_dict(self) -> JsonObject:
        return {**self.payload(), "manifest_hash": self.manifest_hash}

    def render_text(self) -> str:
        """Render a compact deterministic view suitable for terminals and CI logs."""

        churn = cast(JsonObject, self.volume["churn_per_commit"])
        files = cast(JsonObject, self.volume["files_per_commit"])
        cochange_coverage = cast(JsonObject, self.coverage["cochange"])
        lines = [
            "RuleLoom repository structure audit",
            "",
            f"Repository: {self.repository_id}",
            f"Resolved ref: {self.topology.get('resolved_ref')}",
            (
                f"History: {self.topology.get('commit_count')} commits; "
                f"{self.topology.get('merge_commits')} merges; "
                f"shallow={str(self.topology.get('shallow')).lower()}; "
                f"truncated={str(self.topology.get('truncated')).lower()}"
            ),
            (
                f"Churn per commit: median={churn.get('median')}, "
                f"p90={churn.get('p90')}, maximum={churn.get('maximum')}"
            ),
            (
                f"Files per commit: median={files.get('median')}, "
                f"p90={files.get('p90')}, maximum={files.get('maximum')}"
            ),
            "",
            "Most frequently changed paths",
        ]
        if self.hotspots:
            for row_value in self.hotspots:
                row = cast(JsonObject, row_value)
                lines.append(
                    f"- {json.dumps(row.get('path'), ensure_ascii=True)}: "
                    f"{row.get('change_count')} changes, "
                    f"churn {row.get('churn')}"
                )
        else:
            lines.append("- No changed paths in the selected history.")
        lines.extend(("", "Most frequent path co-changes"))
        if self.cochanges:
            for row_value in self.cochanges:
                row = cast(JsonObject, row_value)
                lines.append(
                    f"- {json.dumps(row.get('left_path'), ensure_ascii=True)} + "
                    f"{json.dumps(row.get('right_path'), ensure_ascii=True)}: "
                    f"{row.get('cochange_count')} commits"
                )
        else:
            lines.append("- No pairs met the configured support floor.")
        lines.extend(
            (
                "",
                "Co-change coverage",
                (
                    f"- Eligible commits: {cochange_coverage.get('eligible_commits')}; "
                    f"skipped above path budget: "
                    f"{cochange_coverage.get('skipped_large_commits')}; "
                    f"skipped after pair budget: "
                    f"{cochange_coverage.get('skipped_budget_commits')}"
                ),
                (
                    f"- Pair updates: {cochange_coverage.get('pair_updates')}; "
                    f"minimum support: {cochange_coverage.get('minimum_pair_count')}"
                ),
                "",
                "Warnings",
                *(f"- {item}" for item in self.warnings),
                *(("- None.",) if not self.warnings else ()),
                "",
                f"Predicate quality: {self.predicate_quality.get('status')}",
                (
                    "Explicit test-structure assertions: "
                    f"{self.structural_test_expectations.get('status')}"
                ),
                "",
                "Limits of interpretation",
                *(f"- {item}" for item in self.limitations),
                "",
                f"Manifest: {self.manifest_hash}",
            )
        )
        return "\n".join(lines) + "\n"


def build_first_hour_report(
    root: Path,
    *,
    ref: str = "HEAD",
    limits: RepositoryAuditLimits | None = None,
    observations: tuple[Observation, ...] | list[Observation] = (),
    predicate_vocabulary: tuple[str, ...] = (),
    configured_predicates: tuple[str, ...] = (),
    assertion_declaration: RepositoryAssertionDeclaration | None = None,
) -> FirstHourReport:
    """Audit a Git checkout without initialization, configuration, labels, or writes."""

    selected_limits = limits or RepositoryAuditLimits()
    diffs, topology, repository_id, history_warnings = _collect_diffs(
        root,
        ref=ref,
        limits=selected_limits,
    )

    cochanges, cochange_coverage, cochange_warnings = _cochanges(diffs, selected_limits)
    observation_values = tuple(observations)
    observation_coverage, predicate_quality = _observation_enrichment(
        observation_values,
        predicate_vocabulary,
        configured_predicates,
    )
    structural_tests, assertion_adherence = _assertion_enrichment(
        root,
        assertion_declaration,
        observation_values,
    )
    evidence_manifest: JsonObject = {
        "schema_version": FIRST_HOUR_REPORT_SCHEMA_VERSION,
        "repository_id": repository_id,
        "resolved_ref": topology.get("resolved_ref"),
        # Batch size is a transport optimization. It is intentionally omitted
        # so identical Git evidence has the same hash regardless of how many
        # ranges were sent to each subprocess.
        "limits": {
            key: value
            for key, value in selected_limits.to_dict().items()
            if key != "diff_batch_size"
        },
        "commit_diffs": [item.manifest_record() for item in diffs],
        "observation_enrichment": observation_coverage,
        "predicate_quality_manifest_hash": (
            cast(JsonObject, predicate_quality.get("audit", {})).get("observation_manifest_hash")
            if predicate_quality.get("status") == "available"
            else None
        ),
        "assertion_audit_manifest_hash": (
            cast(JsonObject, assertion_adherence.get("audit", {})).get("manifest_hash")
            if assertion_adherence.get("status") == "available"
            else None
        ),
    }
    warnings = (*history_warnings, *cochange_warnings)
    coverage: JsonObject = {
        "diff_commits": len(diffs),
        "path_entries": sum(item.files_changed for item in diffs),
        "numstat_complete_for_selected_commits": True,
        "cochange": cochange_coverage,
        "observation_enrichment": observation_coverage,
    }
    return FirstHourReport(
        repository_id=repository_id,
        ref=ref,
        limits=selected_limits,
        topology=topology,
        volume=_volume(diffs),
        hotspots=tuple(_hotspots(diffs, selected_limits.max_hotspots)),
        cochanges=tuple(cochanges),
        coverage=coverage,
        predicate_quality=predicate_quality,
        structural_test_expectations=structural_tests,
        assertion_adherence=assertion_adherence,
        evidence_manifest_hash=content_hash(evidence_manifest),
        warnings=tuple(warnings),
        limitations=(
            "The report describes local Git structure and co-change; it does not estimate risk.",
            "Co-change and historical adherence do not establish causality.",
            "Binary-file churn is unavailable from Git numstat and is reported separately.",
            "Test structure is reported only from explicit configured predicates and assertions.",
            "Git history alone does not provide independent CI, review, or incident outcomes.",
        ),
    )


def audit_repository(
    root: Path,
    *,
    ref: str = "HEAD",
    limits: RepositoryAuditLimits | None = None,
    observations: tuple[Observation, ...] | list[Observation] = (),
    predicate_vocabulary: tuple[str, ...] = (),
    configured_predicates: tuple[str, ...] = (),
    assertion_declaration: RepositoryAssertionDeclaration | None = None,
) -> FirstHourReport:
    """Public alias matching the zero-configuration ``audit`` command boundary."""

    return build_first_hour_report(
        root,
        ref=ref,
        limits=limits,
        observations=observations,
        predicate_vocabulary=predicate_vocabulary,
        configured_predicates=configured_predicates,
        assertion_declaration=assertion_declaration,
    )


__all__ = [
    "FIRST_HOUR_REPORT_ENGINE_VERSION",
    "FIRST_HOUR_REPORT_SCHEMA_VERSION",
    "FirstHourAuditError",
    "FirstHourReport",
    "RepositoryAuditLimits",
    "audit_repository",
    "build_first_hour_report",
    "collect_commit_diffs",
]
