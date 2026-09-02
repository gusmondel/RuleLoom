"""Language-neutral change facts plus instantiated repository concepts (schema v5).

``generic_changes@3`` keeps every ``generic_changes@2`` fact and adds:

- cumulative ordinal literals (``churn_at_least_*`` and ``files_at_least_*``)
  so a clause can express a threshold instead of one exclusive band;
- ``touches_generated_artifact`` from bounded, documented path conventions and
  from ``linguist-generated`` attributes at the base snapshot;
- ``owner_areas_at_least_2`` / ``owner_areas_at_least_3`` from the base-snapshot
  ``CODEOWNERS`` (identities are counted transiently, never persisted);
- optional *instantiated* predicates frozen in ``pack_config``: exact
  ``touches_*`` path concepts (hotspots, owner areas, pair endpoints) and
  ``missing_partner_*`` co-change omissions (``path`` changed, ``partner`` not).

Instantiated predicates are proposed outcome-blind by ``ruleloom predicates
propose`` and reviewed by a human before the vocabulary is frozen. They are
ordinary declared predicates: the learner cannot invent them at search time.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from ruleloom.history_features import HISTORY_FEATURE_PREDICATES_V3
from ruleloom.models import JsonObject
from ruleloom.packs.base import (
    DiffEvidence,
    FileChange,
    PackExtraction,
    PackOptions,
    finalize_extraction,
    is_internal_path,
)
from ruleloom.packs.configured_paths import (
    ConfiguredMatchResult,
    ConfiguredPathsConfig,
    configured_matches,
)
from ruleloom.packs.generic_v2 import PREDICATES as GENERIC_V2_PREDICATES
from ruleloom.packs.generic_v2 import generic_change_shape_reasons

NAME = "generic_changes"
VERSION = 3
EXTRACTOR = "ruleloom.generic_changes.git.v3"

ORDINAL_PREDICATES = (
    "churn_at_least_extreme",
    "churn_at_least_large",
    "churn_at_least_small",
    "files_at_least_few",
    "files_at_least_many",
    "files_at_least_wide",
)
GENERATED_ARTIFACT_PREDICATE = "touches_generated_artifact"
PREDICATES = tuple(
    sorted(
        {
            *GENERIC_V2_PREDICATES,
            *HISTORY_FEATURE_PREDICATES_V3,
            *ORDINAL_PREDICATES,
            GENERATED_ARTIFACT_PREDICATE,
        }
    )
)

# Bounded, documented path conventions for generated artifacts. They are
# heuristics about names, not proof that a file is generated; the base-snapshot
# ``linguist-generated`` attribute is the repository-declared signal.
_GENERATED_DIRECTORY_PARTS = frozenset({"__generated__", "__snapshots__", "generated"})
_GENERATED_NAME_PREFIXES = ("zz_generated",)
_GENERATED_NAME_INFIXES = (".generated.", "_generated.", ".gen.", "_gen.")
_GENERATED_NAME_SUFFIXES = (
    ".pb.go",
    ".pb.cc",
    ".pb.h",
    "_pb2.py",
    "_pb2_grpc.py",
    ".g.dart",
    ".freezed.dart",
    ".g.cs",
    ".designer.cs",
    ".min.js",
    ".min.css",
    ".bundle.js",
    ".snap",
)


def generated_path_marker(path: str) -> str | None:
    """Return the documented naming convention that marks ``path`` as generated."""
    lowered = path.lower()
    pure = PurePosixPath(lowered)
    parts = pure.parts[:-1]
    for part in parts:
        if part in _GENERATED_DIRECTORY_PARTS:
            return f"directory:{part}"
    name = pure.name
    for prefix in _GENERATED_NAME_PREFIXES:
        if name.startswith(prefix):
            return f"prefix:{prefix}"
    for suffix in _GENERATED_NAME_SUFFIXES:
        if name.endswith(suffix):
            return f"suffix:{suffix}"
    for infix in _GENERATED_NAME_INFIXES:
        if infix in name:
            return f"infix:{infix}"
    return None


def ignores_content(_path: str) -> bool:
    return False


def generic_v3_reasons(
    evidence: DiffEvidence,
    options: PackOptions,
    config: ConfiguredPathsConfig | None = None,
) -> tuple[
    dict[str, set[str]],
    ConfiguredMatchResult,
    ConfiguredPathsConfig,
    tuple[FileChange, ...],
]:
    """Return the exact v3 reasons plus the configured matches and visible changes."""

    effective_config = config if config is not None else ConfiguredPathsConfig()
    reasons, shape = generic_change_shape_reasons(evidence, options)

    def record(predicate: str, reason: str) -> None:
        reasons.setdefault(predicate, set()).add(reason)

    if shape.churn >= shape.tiny_boundary:
        record("churn_at_least_small", f"churn:{shape.churn}>={shape.tiny_boundary}")
    if shape.churn >= shape.large_boundary:
        record("churn_at_least_large", f"churn:{shape.churn}>={shape.large_boundary}")
    if shape.churn >= shape.extreme_boundary:
        record("churn_at_least_extreme", f"churn:{shape.churn}>={shape.extreme_boundary}")
    if shape.file_count >= 2:
        record("files_at_least_few", f"files:{shape.file_count}>=2")
    if shape.file_count >= shape.many_boundary:
        record("files_at_least_many", f"files:{shape.file_count}>={shape.many_boundary}")
    if shape.file_count >= shape.wide_boundary:
        record("files_at_least_wide", f"files:{shape.file_count}>={shape.wide_boundary}")

    visible = tuple(
        sorted(
            (change for change in evidence.changes if not is_internal_path(change.path)),
            key=lambda item: item.path,
        )
    )
    for change in visible:
        marker = generated_path_marker(change.path)
        if marker is not None:
            record(GENERATED_ARTIFACT_PREDICATE, f"path:{change.path};{marker}")

    matches = configured_matches(tuple(change.path for change in visible), effective_config)
    for path, matched in zip(visible, matches.matched, strict=True):
        for predicate in matched:
            record(predicate, f"path:{path.path}")
    for predicate, reason in matches.partner_evidence.items():
        record(predicate, reason)
    return reasons, matches, effective_config, visible


def configured_metadata(
    matches: ConfiguredMatchResult, effective_config: ConfiguredPathsConfig
) -> JsonObject:
    """Metadata every configurable generic pack version records identically."""
    return {
        "configured_paths_config_hash": effective_config.hash,
        "configured_path_match_counts": dict(matches.counts),
        "configured_unmatched_files": matches.unmatched,
        "configured_overlapping_files": matches.overlapping,
        "configured_match_manifest_hash": matches.manifest_hash,
        "configured_partner_status": dict(matches.partner_status),
    }


def extract_generic_change_facts_v3(
    evidence: DiffEvidence,
    options: PackOptions,
    config: ConfiguredPathsConfig | None = None,
) -> PackExtraction:
    """Extract v2 shape facts, cumulative ordinals, generated hints, and configured concepts."""

    reasons, matches, effective_config, _visible = generic_v3_reasons(evidence, options, config)
    result = finalize_extraction(evidence, reasons, extractor=EXTRACTOR, options=options)
    metadata = dict(result.metadata)
    metadata.update(configured_metadata(matches, effective_config))
    return PackExtraction(result.facts, result.provenance, metadata)
