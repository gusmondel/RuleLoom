"""Language-neutral change facts plus experience, ownership, history and timing (schema v5).

``generic_changes@4`` keeps every ``generic_changes@3`` fact and adds the
just-in-time families that Kamei et al. (2013) found informative beyond size
and diffusion, all computable from Git alone at prediction time:

- ``change_entropy_high``: the change's churn is spread evenly across at least
  three files (normalized entropy at least 0.75; Hassan 2009);
- ``author_low_experience`` / ``author_new_to_area`` /
  ``touched_files_many_authors``: author experience and file ownership from the
  strictly earlier observation stream, using privacy-preserving author hashes
  (Mockus and Weiss 2000; Bird et al. 2011);
- ``touches_recently_reworked_file``: a touched path was reworked within the
  prior 90 days according to persisted ``rework`` events (Kim et al. 2007);
- ``authored_off_hours``: weekend or late-night authoring judged from the
  timestamp's own offset (Eyolfson, Tan and Lam 2011).

The enrichment-side facts abstain during a 90-day warm-up, without an author
hash, or without a rework scan; absence means "not observed", never "false".
"""

from __future__ import annotations

from ruleloom.history_features import HISTORY_FEATURE_PREDICATES_V4
from ruleloom.packs.base import DiffEvidence, PackExtraction, PackOptions, finalize_extraction
from ruleloom.packs.configured_paths import ConfiguredPathsConfig
from ruleloom.packs.generic_v2 import _normalized_entropy
from ruleloom.packs.generic_v3 import PREDICATES as GENERIC_V3_PREDICATES
from ruleloom.packs.generic_v3 import configured_metadata, generic_v3_reasons

NAME = "generic_changes"
VERSION = 4
EXTRACTOR = "ruleloom.generic_changes.git.v4"

ENTROPY_PREDICATE = "change_entropy_high"
ENTROPY_MIN_FILES = 3
ENTROPY_THRESHOLD = 0.75
PREDICATES = tuple(
    sorted({*GENERIC_V3_PREDICATES, *HISTORY_FEATURE_PREDICATES_V4, ENTROPY_PREDICATE})
)


def ignores_content(_path: str) -> bool:
    return False


def extract_generic_change_facts_v4(
    evidence: DiffEvidence,
    options: PackOptions,
    config: ConfiguredPathsConfig | None = None,
) -> PackExtraction:
    """Extract every v3 fact plus the high-entropy diffusion ordinal."""

    reasons, matches, effective_config, visible = generic_v3_reasons(evidence, options, config)
    churn = [change.churn for change in visible if change.churn > 0]
    if evidence.aggregate_additions is None and len(churn) >= ENTROPY_MIN_FILES:
        entropy = _normalized_entropy(churn)
        if entropy >= ENTROPY_THRESHOLD:
            reasons.setdefault(ENTROPY_PREDICATE, set()).add(
                f"normalized_entropy:{entropy:.6f}>={ENTROPY_THRESHOLD};files:{len(churn)}"
            )
    result = finalize_extraction(evidence, reasons, extractor=EXTRACTOR, options=options)
    metadata = dict(result.metadata)
    metadata.update(configured_metadata(matches, effective_config))
    return PackExtraction(result.facts, result.provenance, metadata)
