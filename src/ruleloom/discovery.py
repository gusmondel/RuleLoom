"""Outcome-blind proposal of instantiated repository concepts and assertion drafts.

The proposer reads Git structure only: changed paths per commit, path co-change,
and the ``CODEOWNERS`` document at the audited revision. It never reads labels,
outcomes, review prose, or file contents. Its output is a *draft*: a
``generic_changes@3`` ``pack_config`` plus an assertion manifest that a human
reviews before freezing a new experiment. When a project is initialized, the
draft is bounded to commits before the frozen holdout boundary so the future
confirmation window stays untouched.

Why this exists: coarse Booleans such as ``missing_usual_cochange_partner`` or
``crosses_codeowners_boundary`` collapse whole families of repository facts into
one bit. A learner cannot exceed the base rate of an equivalence class it cannot
split. Instantiating the strongest hotspots, owner areas, and co-change pairs as
declared predicates gives the same bounded Horn search enough resolution while
keeping every predicate reviewable, deterministic, and frozen before labels.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import cast

from ruleloom.first_hour import (
    FirstHourAuditError,
    RepositoryAuditLimits,
    collect_commit_diffs,
)
from ruleloom.gitfacts import GitFactsError, _run_git_capped
from ruleloom.history_features import (
    _normalize_codeowners_pattern,
    _read_codeowners_batch,
    parse_codeowners_rules,
)
from ruleloom.models import (
    JsonObject,
    JsonValue,
    ModelError,
    RuleLiteral,
    content_hash,
    parse_timestamp,
)
from ruleloom.packs import latest_pack_version
from ruleloom.packs.configured_paths import (
    MAX_PREDICATE_LENGTH,
    MAX_PREDICATES,
    MISSING_PARTNER_PREFIX,
    ConfiguredPathsConfig,
    MatcherBudgetError,
    PartnerPredicateConfig,
    PathPredicateConfig,
    _validate_glob,
    configured_matches,
)
from ruleloom.repository_assertions import (
    RepositoryAssertion,
    RepositoryAssertionManifest,
    RepositoryAssertionSourceRef,
    _source_path,
)

_MAX_SOURCE_BYTES = 1024 * 1024

DISCOVERY_ENGINE_VERSION = "ruleloom-discovery/0.1"
HOTSPOT_PREFIX = "touches_hotspot_"
DIRECTORY_PREFIX = "touches_dir_"
OWNER_AREA_PREFIX = "touches_owner_area_"
PAIR_ENDPOINT_PREFIX = "touches_path_"
_MAX_DIRECTORY_DEPTH = 3
_SLUG_CHARS = re.compile(r"[^a-z0-9]+")
_MAX_SLUG_CHARS = 24
_MAX_OWNER_GLOBS = 32
_MAX_COMMITS = 10_000
_MAX_PROPOSALS = 64


@dataclass(frozen=True, slots=True)
class DiscoveryLimits:
    """Explicit, bounded selection thresholds for one proposal run."""

    max_commits: int = 2_000
    max_hotspots: int = 6
    max_directories: int = 8
    max_owner_areas: int = 6
    max_pairs: int = 12
    min_hotspot_changes: int = 3
    min_pair_support: int = 5
    min_pair_confidence: float = 0.7
    max_cochange_paths_per_commit: int = 200
    max_pairs_per_source: int = 2
    min_pair_violations: int = 2
    max_owner_area_coverage: float = 0.95
    min_directory_coverage: float = 0.02

    def __post_init__(self) -> None:
        for name, value, maximum in (
            ("max_commits", self.max_commits, _MAX_COMMITS),
            ("max_hotspots", self.max_hotspots, _MAX_PROPOSALS),
            ("max_directories", self.max_directories, _MAX_PROPOSALS),
            ("max_owner_areas", self.max_owner_areas, _MAX_PROPOSALS),
            ("max_pairs", self.max_pairs, _MAX_PROPOSALS),
            ("min_hotspot_changes", self.min_hotspot_changes, _MAX_COMMITS),
            ("min_pair_support", self.min_pair_support, _MAX_COMMITS),
            ("max_cochange_paths_per_commit", self.max_cochange_paths_per_commit, 500),
            ("max_pairs_per_source", self.max_pairs_per_source, _MAX_PROPOSALS),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ModelError(f"{name} must be an integer between 1 and {maximum}")
        if (
            isinstance(self.min_pair_confidence, bool)
            or not isinstance(self.min_pair_confidence, int | float)
            or not 0 < self.min_pair_confidence <= 1
        ):
            raise ModelError("min_pair_confidence must be between 0 (exclusive) and 1")
        if (
            isinstance(self.min_pair_violations, bool)
            or not isinstance(self.min_pair_violations, int)
            or not 0 <= self.min_pair_violations <= _MAX_COMMITS
        ):
            raise ModelError(f"min_pair_violations must be between 0 and {_MAX_COMMITS}")
        if (
            isinstance(self.max_owner_area_coverage, bool)
            or not isinstance(self.max_owner_area_coverage, int | float)
            or not 0 < self.max_owner_area_coverage <= 1
        ):
            raise ModelError("max_owner_area_coverage must be between 0 (exclusive) and 1")
        if (
            isinstance(self.min_directory_coverage, bool)
            or not isinstance(self.min_directory_coverage, int | float)
            or not 0 < self.min_directory_coverage <= 1
        ):
            raise ModelError("min_directory_coverage must be between 0 (exclusive) and 1")
        if self.max_hotspots + self.max_directories + self.max_owner_areas > MAX_PREDICATES:
            raise ModelError(
                "max_hotspots plus max_directories plus max_owner_areas cannot exceed "
                f"{MAX_PREDICATES} path predicates"
            )

    def to_dict(self) -> JsonObject:
        return {
            "max_commits": self.max_commits,
            "max_hotspots": self.max_hotspots,
            "max_directories": self.max_directories,
            "max_owner_areas": self.max_owner_areas,
            "max_pairs": self.max_pairs,
            "min_hotspot_changes": self.min_hotspot_changes,
            "min_pair_support": self.min_pair_support,
            "min_pair_confidence": self.min_pair_confidence,
            "max_cochange_paths_per_commit": self.max_cochange_paths_per_commit,
            "max_pairs_per_source": self.max_pairs_per_source,
            "min_pair_violations": self.min_pair_violations,
            "max_owner_area_coverage": self.max_owner_area_coverage,
            "min_directory_coverage": self.min_directory_coverage,
        }


@dataclass(frozen=True, slots=True)
class DiscoveryProposal:
    """A reviewable, outcome-blind vocabulary and assertion draft."""

    repository_id: str
    ref: str
    resolved_ref: str
    until: str | None
    commit_count: int
    excluded_after_until: int
    limits: DiscoveryLimits
    pack_config: ConfiguredPathsConfig
    assertion_manifest: RepositoryAssertionManifest | None
    hotspots: tuple[JsonValue, ...]
    owner_areas: tuple[JsonValue, ...]
    pairs: tuple[JsonValue, ...]
    warnings: tuple[str, ...]
    directories: tuple[JsonValue, ...] = ()
    evidence_path: str | None = None
    evidence_document: str | None = None
    paths_only: bool = False
    engine_version: str = DISCOVERY_ENGINE_VERSION

    @property
    def limitations(self) -> tuple[str, ...]:
        return (
            "Proposals describe Git structure only; they are not predictive or causal claims.",
            "Every predicate must be reviewed by a human and frozen before outcomes are opened.",
            "Co-change confidence is a historical rate, not a dependency or a rule.",
            "Owner areas hash owner sets and store globs only; identities are not persisted.",
        )

    def payload(self) -> JsonObject:
        return {
            "engine_version": self.engine_version,
            "outcome_blind": True,
            "draft": True,
            "repository_id": self.repository_id,
            "ref": self.ref,
            "resolved_ref": self.resolved_ref,
            "until": self.until,
            "commit_count": self.commit_count,
            "excluded_after_until": self.excluded_after_until,
            "limits": self.limits.to_dict(),
            "paths_only": self.paths_only,
            "pack": {"name": "generic_changes", "version": latest_pack_version("generic_changes")},
            "pack_config": self.pack_config.to_dict(),
            "assertion_manifest": (
                None if self.assertion_manifest is None else self.assertion_manifest.to_dict()
            ),
            "hotspots": list(self.hotspots),
            "directories": list(self.directories),
            "owner_areas": list(self.owner_areas),
            "pairs": list(self.pairs),
            "evidence_path": self.evidence_path,
            "evidence_document_sha256": (
                None
                if self.evidence_document is None
                else hashlib.sha256(self.evidence_document.encode("utf-8")).hexdigest()
            ),
            "warnings": list(self.warnings),
            "limitations": list(self.limitations),
        }

    @property
    def manifest_hash(self) -> str:
        return content_hash(self.payload())

    def to_dict(self) -> JsonObject:
        return {**self.payload(), "manifest_hash": self.manifest_hash}

    def render_text(self) -> str:
        lines = [
            "RuleLoom vocabulary proposal (outcome-blind draft)",
            "",
            f"Repository: {self.repository_id}",
            f"Resolved ref: {self.resolved_ref}",
            (
                f"Commits scanned: {self.commit_count}"
                + (
                    f" (before {self.until}; {self.excluded_after_until} later commits excluded)"
                    if self.until is not None
                    else " (no holdout boundary; bound the scan with --until before freezing)"
                )
            ),
            "",
            f"Hotspot predicates ({len(self.hotspots)})",
        ]
        for row_value in self.hotspots:
            row = cast(JsonObject, row_value)
            lines.append(f"- {row['predicate']}: {row['path']} ({row['change_count']} changes)")
        if not self.hotspots:
            lines.append("- None met the change-count floor.")
        lines.extend(("", f"Directory predicates ({len(self.directories)})"))
        for row_value in self.directories:
            row = cast(JsonObject, row_value)
            lines.append(
                f"- {row['predicate']}: {row['directory']}/ touched by {row['commit_count']} "
                f"commits ({cast(float, row['coverage']):.0%})"
            )
        if not self.directories:
            lines.append("- No directory sat between the coverage floors.")
        lines.extend(("", f"Owner-area predicates ({len(self.owner_areas)})"))
        for row_value in self.owner_areas:
            row = cast(JsonObject, row_value)
            lines.append(
                f"- {row['predicate']}: {row['glob_count']} globs, "
                f"{row['commit_count']} commits touched"
            )
        if not self.owner_areas:
            lines.append("- No supported CODEOWNERS rules at this revision.")
        lines.extend(("", f"Missing-partner predicates ({len(self.pairs)})"))
        for row_value in self.pairs:
            row = cast(JsonObject, row_value)
            predicate = row.get("predicate") or "no predicate (never violated)"
            lines.append(
                f"- {predicate}: {row['path']} changed without {row['partner']} "
                f"(support {row['support']}, confidence {row['confidence']:.2f}, "
                f"violations {row['violations']})"
            )
        if not self.pairs:
            lines.append("- No pair met the support and confidence floors.")
        assertion_count = (
            0 if self.assertion_manifest is None else len(self.assertion_manifest.assertions)
        )
        lines.extend(
            (
                "",
                f"Assertion drafts: {assertion_count}",
                "",
                "Warnings",
                *(f"- {item}" for item in self.warnings),
                *(("- None.",) if not self.warnings else ()),
                "",
                "Limits of interpretation",
                *(f"- {item}" for item in self.limitations),
                "",
                f"Manifest: {self.manifest_hash}",
            )
        )
        return "\n".join(lines) + "\n"


def _slug(path: str) -> str:
    name = path.rsplit("/", 1)[-1].lower()
    slug = _SLUG_CHARS.sub("_", name).strip("_")
    if not slug:
        slug = "path"
    return slug[:_MAX_SLUG_CHARS].rstrip("_") or "path"


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def _predicate_name(prefix: str, slug: str, digest: str) -> str:
    name = f"{prefix}{slug}_{digest[:6]}"
    if len(name) > MAX_PREDICATE_LENGTH:
        overflow = len(name) - MAX_PREDICATE_LENGTH
        trimmed = slug[: max(1, len(slug) - overflow)].rstrip("_") or "p"
        name = f"{prefix}{trimmed}_{digest[:6]}"
    return name


def _literal_glob(path: str) -> str | None:
    try:
        return _validate_glob(path, "proposed path")
    except ModelError:
        return None


def _blob_sizes(root: Path, revision: str, paths: tuple[str, ...]) -> dict[str, int]:
    """Return blob sizes at ``revision`` for the requested paths, or nothing on failure."""
    if not paths:
        return {}
    try:
        stdout, _stderr, returncode = _run_git_capped(
            root,
            ("ls-tree", "-l", "-z", revision, "--", *paths),
            allow_lazy_fetch=False,
        )
    except GitFactsError:
        return {}
    if returncode != 0:
        return {}
    sizes: dict[str, int] = {}
    for record in stdout.split(b"\x00"):
        if not record:
            continue
        header, _tab, path = record.partition(b"\t")
        fields = header.split()
        if len(fields) != 4 or fields[1] != b"blob":
            continue
        try:
            sizes[path.decode("utf-8")] = int(fields[3])
        except (UnicodeDecodeError, ValueError):
            continue
    return sizes


def _evidence_document(
    *,
    repository_id: str,
    resolved_ref: str,
    until: str | None,
    rows: list[tuple[str, str, str, int, int, float]],
) -> tuple[str, dict[str, int]]:
    """Render the reviewable co-change evidence document and the line of each pair."""
    lines = [
        "# Co-change evidence proposed by RuleLoom",
        "",
        f"Generated by `ruleloom predicates propose` from Git structure only at {resolved_ref}",
        (f"for commits before {until}." if until is not None else "without a holdout boundary."),
        f"Repository identity: {repository_id}.",
        "",
        "Each line records one historical co-change rate. It is not a dependency, a rule,",
        "or a causal claim. A human must confirm that the convention is real before the",
        "matching assertion is declared; delete any line that is not.",
        "",
    ]
    positions: dict[str, int] = {}
    for assertion_id, source, target, support, total, confidence in rows:
        positions[assertion_id] = len(lines) + 1
        lines.append(
            f"- {assertion_id}: {source} changed together with {target} in {support} of "
            f"{total} changes ({confidence:.0%})."
        )
    lines.append("")
    return "\n".join(lines), positions


def propose_vocabulary(
    root: Path,
    *,
    ref: str = "HEAD",
    until: str | None = None,
    limits: DiscoveryLimits | None = None,
    evidence_path: str | None = None,
    paths_only: bool = False,
) -> DiscoveryProposal:
    """Propose instantiated predicates and assertion drafts from Git structure only.

    ``evidence_path`` names a repository-relative Markdown document that the
    caller will add to the repository; drafted assertions then cite the line of
    that document describing their pair. Without it, each draft cites its
    antecedent path and pairs whose antecedent blob exceeds the assertion source
    limit are left without a draft.
    """

    selected = limits or DiscoveryLimits()
    if evidence_path is not None:
        _source_path(evidence_path)
    boundary = parse_timestamp(until) if until is not None else None
    try:
        diffs, topology, repository_id, history_warnings = collect_commit_diffs(
            root,
            ref=ref,
            limits=RepositoryAuditLimits(
                max_commits=selected.max_commits,
                max_cochange_paths_per_commit=selected.max_cochange_paths_per_commit,
            ),
            paths_only=paths_only,
        )
    except FirstHourAuditError as exc:
        raise ModelError(str(exc)) from exc
    warnings = list(history_warnings)
    if paths_only:
        warnings.append(
            "paths-only scan: changed paths were read from trees without blobs, so churn "
            "is unavailable; hotspots, owner areas, and pairs use path counts only"
        )
    if boundary is not None:
        eligible = tuple(item for item in diffs if parse_timestamp(item.committed_at) < boundary)
    else:
        eligible = tuple(diffs)
        warnings.append(
            "no holdout boundary supplied; proposals used every scanned commit, so freeze "
            "the vocabulary before any outcome is opened"
        )
    excluded_after_until = len(diffs) - len(eligible)
    resolved_ref = str(topology.get("resolved_ref"))

    touches: Counter[str] = Counter()
    pairs: Counter[tuple[str, str]] = Counter()
    commit_paths: list[tuple[str, ...]] = []
    skipped_large = 0
    for diff in eligible:
        paths = tuple(sorted({item.path for item in diff.changes}))
        commit_paths.append(paths)
        if len(paths) > selected.max_cochange_paths_per_commit:
            skipped_large += 1
            continue
        touches.update(paths)
        pairs.update(combinations(paths, 2))
    if skipped_large:
        warnings.append(
            f"co-change excluded {skipped_large} commit(s) above the per-commit path budget"
        )

    path_predicates: dict[str, PathPredicateConfig] = {}
    predicate_by_path: dict[str, str] = {}
    hotspot_rows: list[JsonValue] = []
    for path, count in sorted(touches.items(), key=lambda item: (-item[1], item[0])):
        if len(hotspot_rows) >= selected.max_hotspots or count < selected.min_hotspot_changes:
            break
        glob = _literal_glob(path)
        if glob is None:
            continue
        predicate = _predicate_name(HOTSPOT_PREFIX, _slug(path), _digest("hotspot", path))
        if predicate in path_predicates:
            continue
        path_predicates[predicate] = PathPredicateConfig(predicate=predicate, include_paths=(glob,))
        predicate_by_path[path] = predicate
        hotspot_rows.append({"predicate": predicate, "path": path, "change_count": count})

    directory_rows: list[JsonValue] = []
    directory_touches: Counter[str] = Counter()
    for paths in commit_paths:
        prefixes: set[str] = set()
        for path in paths:
            parts = path.split("/")
            for depth in range(1, min(_MAX_DIRECTORY_DEPTH, len(parts) - 1) + 1):
                prefixes.add("/".join(parts[:depth]))
        directory_touches.update(prefixes)
    selected_directories: list[tuple[str, float]] = []
    for directory, count in sorted(directory_touches.items(), key=lambda item: (-item[1], item[0])):
        if len(selected_directories) >= selected.max_directories:
            break
        coverage = count / len(commit_paths) if commit_paths else 0.0
        if coverage < selected.min_directory_coverage:
            break
        if coverage >= selected.max_owner_area_coverage:
            continue
        near_duplicate = any(
            (
                other == directory
                or other.startswith(directory + "/")
                or directory.startswith(other + "/")
            )
            and abs(other_coverage - coverage) < 0.1 * max(coverage, other_coverage)
            for other, other_coverage in selected_directories
        )
        if near_duplicate:
            continue
        glob = _literal_glob(directory + "/**")
        if glob is None:
            continue
        predicate = _predicate_name(
            DIRECTORY_PREFIX, _slug(directory), _digest("directory", directory)
        )
        if predicate in path_predicates:
            continue
        path_predicates[predicate] = PathPredicateConfig(predicate=predicate, include_paths=(glob,))
        selected_directories.append((directory, coverage))
        directory_rows.append(
            {
                "predicate": predicate,
                "directory": directory,
                "commit_count": count,
                "coverage": coverage,
            }
        )

    owner_rows: list[JsonValue] = []
    snapshots = _read_codeowners_batch(root, {resolved_ref})
    content, location_or_reason = snapshots.get(resolved_ref, (None, "base_commit_unavailable"))
    if content is None:
        warnings.append(f"CODEOWNERS unavailable at {resolved_ref}: {location_or_reason}")
    else:
        try:
            rules, unsupported = parse_codeowners_rules(content)
        except ValueError as exc:
            rules, unsupported = [], 0
            warnings.append(f"CODEOWNERS skipped: {exc}")
        if unsupported:
            warnings.append(f"CODEOWNERS contained {unsupported} unsupported rule(s)")
        globs_by_owner: dict[tuple[str, ...], list[str]] = {}
        # Re-read raw patterns so the frozen config stores declared globs, never handles.
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            fields = stripped.split()
            if len(fields) < 2:
                continue
            raw_owners = fields[1:]
            if "#" in raw_owners:
                raw_owners = raw_owners[: raw_owners.index("#")]
            if not raw_owners:
                continue
            owner_key = tuple(sorted(set(raw_owners)))
            normalized = _normalize_codeowners_pattern(fields[0])
            if normalized is None or _literal_glob(normalized) is None:
                continue
            globs_by_owner.setdefault(owner_key, []).append(normalized)
        del rules
        area_candidates: list[tuple[int, str, tuple[str, ...]]] = []
        budget_skipped: set[int] = set()
        for owner_key, raw_globs in globs_by_owner.items():
            globs = tuple(sorted(dict.fromkeys(raw_globs)))[:_MAX_OWNER_GLOBS]
            digest = _digest("owner-area", repository_id, *owner_key)
            predicate = f"{OWNER_AREA_PREFIX}{digest[:10]}"
            compiled = PathPredicateConfig(predicate=predicate, include_paths=globs)
            area_config = ConfiguredPathsConfig(path_predicates=(compiled,))
            commit_count = 0
            for index, paths in enumerate(commit_paths):
                try:
                    matched = configured_matches(paths, area_config).matched
                except MatcherBudgetError:
                    budget_skipped.add(index)
                    continue
                if any(matched):
                    commit_count += 1
            area_candidates.append((commit_count, predicate, globs))
        if budget_skipped:
            warnings.append(
                f"{len(budget_skipped)} commit(s) exceeded the path matcher budget and were "
                "not counted for owner areas (very large manifests, e.g. vendored imports)"
            )
        area_candidates.sort(key=lambda item: (-item[0], item[1]))
        selected_areas = 0
        for commit_count, predicate, globs in area_candidates:
            if selected_areas >= selected.max_owner_areas:
                break
            if commit_count == 0:
                continue
            coverage = commit_count / len(commit_paths) if commit_paths else 0.0
            if coverage >= selected.max_owner_area_coverage:
                warnings.append(
                    f"{predicate}: owner area covers {coverage:.0%} of scanned commits and was "
                    "skipped as uninformative (likely a catch-all CODEOWNERS rule)"
                )
                continue
            selected_areas += 1
            path_predicates[predicate] = PathPredicateConfig(
                predicate=predicate, include_paths=globs
            )
            owner_rows.append(
                {
                    "predicate": predicate,
                    "glob_count": len(globs),
                    "commit_count": commit_count,
                    "coverage": coverage,
                }
            )

    pair_rows: list[JsonValue] = []
    partner_predicates: dict[str, PartnerPredicateConfig] = {}
    directional: list[tuple[str, str, int, int, float]] = []
    for (left, right), count in pairs.items():
        if count < selected.min_pair_support:
            continue
        for source, target in ((left, right), (right, left)):
            total = touches[source]
            confidence = count / total if total else 0.0
            if confidence >= selected.min_pair_confidence:
                directional.append((source, target, count, total, confidence))
    # Predicates need pairs that were actually violated; assertions prefer the
    # strictest contracts. Each family is selected separately under the same caps.
    predicate_candidates = sorted(
        (item for item in directional if item[3] - item[2] >= selected.min_pair_violations),
        key=lambda item: (-item[2], -item[4], item[0], item[1]),
    )
    assertion_candidates = sorted(
        directional, key=lambda item: (-item[4], -item[2], item[0], item[1])
    )
    rows_by_pair: dict[tuple[str, str], JsonObject] = {}

    def row_for(source: str, target: str, count: int, total: int, confidence: float) -> JsonObject:
        row = rows_by_pair.get((source, target))
        if row is None:
            row = {
                "predicate": None,
                "path": source,
                "partner": target,
                "support": count,
                "total": total,
                "violations": total - count,
                "confidence": confidence,
                "assertion_id": None,
            }
            rows_by_pair[(source, target)] = row
            pair_rows.append(row)
        return row

    predicate_sources: Counter[str] = Counter()
    for source, target, count, total, confidence in predicate_candidates:
        if len(partner_predicates) >= selected.max_pairs:
            break
        if predicate_sources[source] >= selected.max_pairs_per_source:
            continue
        source_glob = _literal_glob(source)
        target_glob = _literal_glob(target)
        if source_glob is None or target_glob is None:
            continue
        digest = _digest("pair", source, target)
        partner_predicate = _predicate_name(MISSING_PARTNER_PREFIX, _slug(source), digest)
        if partner_predicate in partner_predicates:
            continue
        partner_predicates[partner_predicate] = PartnerPredicateConfig(
            predicate=partner_predicate, path=source_glob, partner=target_glob
        )
        predicate_sources[source] += 1
        row_for(source, target, count, total, confidence)["predicate"] = partner_predicate

    assertions: list[RepositoryAssertion] = []
    evidence_rows: list[tuple[str, str, str, int, int, float]] = []
    pending_sources: list[tuple[int, str]] = []
    assertion_sources: Counter[str] = Counter()
    for source, target, count, total, confidence in assertion_candidates:
        if len(assertions) >= selected.max_pairs:
            break
        if assertion_sources[source] >= selected.max_pairs_per_source:
            continue
        source_glob = _literal_glob(source)
        target_glob = _literal_glob(target)
        if source_glob is None or target_glob is None:
            continue
        digest = _digest("pair", source, target)
        endpoints: list[str] = []
        for path, glob in ((source, source_glob), (target, target_glob)):
            existing = predicate_by_path.get(path)
            if existing is None and len(path_predicates) < MAX_PREDICATES:
                existing = _predicate_name(PAIR_ENDPOINT_PREFIX, _slug(path), _digest("path", path))
                if existing not in path_predicates:
                    path_predicates[existing] = PathPredicateConfig(
                        predicate=existing, include_paths=(glob,)
                    )
                predicate_by_path[path] = existing
            if existing is not None:
                endpoints.append(existing)
        row = row_for(source, target, count, total, confidence)
        if len(endpoints) != 2:
            warnings.append(
                f"{source} -> {target}: endpoint path predicates did not fit the "
                f"{MAX_PREDICATES}-predicate cap, so no assertion draft was emitted"
            )
            continue
        assertion_sources[source] += 1
        assertion_id = f"cochange_{_slug(source)}_{digest[:8]}"
        assertions.append(
            RepositoryAssertion(
                assertion_id=assertion_id,
                revision=1,
                summary=(
                    f"Changes to {source} co-changed with {target} in {confidence:.0%} of "
                    f"{total} historical changes (support {count}); review whether the "
                    "partner must be updated too."
                ),
                antecedent=(RuleLiteral(endpoints[0]),),
                expectation=(RuleLiteral(endpoints[1]),),
                sources=(RepositoryAssertionSourceRef(path=source, start_line=1, end_line=1),),
            )
        )
        evidence_rows.append((assertion_id, source, target, count, total, confidence))
        pending_sources.append((len(assertions) - 1, source))
        row["assertion_id"] = assertion_id
    pair_rows.sort(
        key=lambda item: (
            -cast(int, cast(JsonObject, item)["support"]),
            str(cast(JsonObject, item)["path"]),
            str(cast(JsonObject, item)["partner"]),
        )
    )

    evidence_document: str | None = None
    if assertions and evidence_path is not None:
        evidence_document, positions = _evidence_document(
            repository_id=repository_id,
            resolved_ref=resolved_ref,
            until=None if boundary is None else until,
            rows=evidence_rows,
        )
        assertions = [
            RepositoryAssertion(
                assertion_id=item.assertion_id,
                revision=item.revision,
                summary=item.summary,
                antecedent=item.antecedent,
                expectation=item.expectation,
                sources=(
                    RepositoryAssertionSourceRef(
                        path=evidence_path,
                        start_line=positions[item.assertion_id],
                        end_line=positions[item.assertion_id],
                    ),
                ),
            )
            for item in assertions
        ]
    elif assertions:
        sizes = _blob_sizes(
            root, resolved_ref, tuple(dict.fromkeys(source for _index, source in pending_sources))
        )
        oversized = {
            index for index, source in pending_sources if sizes.get(source, 0) > _MAX_SOURCE_BYTES
        }
        if oversized:
            for index in sorted(oversized):
                dropped = assertions[index]
                warnings.append(
                    f"{dropped.assertion_id}: antecedent blob exceeds the {_MAX_SOURCE_BYTES}-"
                    "byte assertion source limit; pass --evidence-path to cite a reviewable "
                    "evidence document instead"
                )
            kept_ids = {
                item.assertion_id for index, item in enumerate(assertions) if index not in oversized
            }
            assertions = [item for item in assertions if item.assertion_id in kept_ids]
            for row_value in pair_rows:
                row = cast(JsonObject, row_value)
                if row.get("assertion_id") not in kept_ids:
                    row["assertion_id"] = None

    pack_config = ConfiguredPathsConfig(
        path_predicates=tuple(path_predicates.values()),
        partner_predicates=tuple(partner_predicates.values()),
    )
    manifest = RepositoryAssertionManifest(assertions=tuple(assertions)) if assertions else None
    return DiscoveryProposal(
        repository_id=repository_id,
        ref=ref,
        resolved_ref=resolved_ref,
        until=None if boundary is None else until,
        commit_count=len(eligible),
        excluded_after_until=excluded_after_until,
        limits=selected,
        pack_config=pack_config,
        assertion_manifest=manifest,
        hotspots=tuple(hotspot_rows),
        directories=tuple(directory_rows),
        owner_areas=tuple(owner_rows),
        pairs=tuple(pair_rows),
        warnings=tuple(dict.fromkeys(warnings)),
        evidence_path=evidence_path if evidence_document is not None else None,
        evidence_document=evidence_document,
        paths_only=paths_only,
    )


__all__ = [
    "DISCOVERY_ENGINE_VERSION",
    "DiscoveryLimits",
    "DiscoveryProposal",
    "propose_vocabulary",
]
