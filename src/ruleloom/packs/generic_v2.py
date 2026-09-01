"""Language-neutral, ordinal change-shape predicates for config schema v4."""

from __future__ import annotations

import math

from ruleloom.history_features import HISTORY_FEATURE_PREDICATES
from ruleloom.packs.base import (
    DiffEvidence,
    PackExtraction,
    PackOptions,
    finalize_extraction,
    is_internal_path,
)

NAME = "generic_changes"
VERSION = 2
EXTRACTOR = "ruleloom.generic_changes.git.v2"

PREDICATES = (
    "change_diffusion_high",
    "change_diffusion_low",
    "churn_band_extreme",
    "churn_band_large",
    "churn_band_small",
    "churn_band_tiny",
    "file_count_band_few",
    "file_count_band_many",
    "file_count_band_single",
    "file_count_band_wide",
    *HISTORY_FEATURE_PREDICATES,
)


def ignores_content(_path: str) -> bool:
    return False


def _normalized_entropy(churn: list[int]) -> float:
    total = sum(churn)
    nonzero = [value for value in churn if value > 0]
    if total <= 0 or len(nonzero) <= 1:
        return 0.0
    entropy = -sum((value / total) * math.log2(value / total) for value in nonzero)
    return entropy / math.log2(len(nonzero))


def extract_generic_change_facts_v2(
    evidence: DiffEvidence,
    options: PackOptions,
) -> PackExtraction:
    """Add ordinal size and diffusion bands without inspecting a programming language."""

    visible = tuple(change for change in evidence.changes if not is_internal_path(change.path))
    aggregate = evidence.aggregate_additions is not None
    additions = (
        evidence.aggregate_additions if aggregate else sum(change.additions for change in visible)
    )
    deletions = (
        evidence.aggregate_deletions if aggregate else sum(change.deletions for change in visible)
    )
    assert additions is not None and deletions is not None
    churn = additions + deletions
    file_count = len(visible)
    reasons: dict[str, set[str]] = {}

    def record(predicate: str, reason: str) -> None:
        reasons.setdefault(predicate, set()).add(reason)

    tiny_boundary = max(1, options.large_change_churn // 4)
    extreme_boundary = min(10_000_000, options.large_change_churn * 4)
    if churn < tiny_boundary:
        record("churn_band_tiny", f"churn:{churn}<{tiny_boundary}")
    elif churn < options.large_change_churn:
        record(
            "churn_band_small",
            f"churn:{tiny_boundary}<={churn}<{options.large_change_churn}",
        )
    elif churn < extreme_boundary:
        record(
            "churn_band_large",
            f"churn:{options.large_change_churn}<={churn}<{extreme_boundary}",
        )
    else:
        record("churn_band_extreme", f"churn:{churn}>={extreme_boundary}")

    wide_boundary = min(100_000, options.multi_file_count * 4)
    if file_count <= 1:
        record("file_count_band_single", f"files:{file_count}<=1")
    elif file_count < options.multi_file_count:
        record(
            "file_count_band_few",
            f"files:2<={file_count}<{options.multi_file_count}",
        )
    elif file_count < wide_boundary:
        record(
            "file_count_band_many",
            f"files:{options.multi_file_count}<={file_count}<{wide_boundary}",
        )
    else:
        record("file_count_band_wide", f"files:{file_count}>={wide_boundary}")

    if not aggregate and file_count > 1:
        diffusion = _normalized_entropy([change.churn for change in visible])
        if diffusion >= 0.5:
            record("change_diffusion_high", f"normalized_entropy:{diffusion:.6f}>=0.5")
        else:
            record("change_diffusion_low", f"normalized_entropy:{diffusion:.6f}<0.5")

    return finalize_extraction(evidence, reasons, extractor=EXTRACTOR, options=options)
