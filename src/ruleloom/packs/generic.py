"""Language-neutral repository-change evidence pack."""

from __future__ import annotations

from ruleloom.packs.base import DiffEvidence, PackExtraction, PackOptions, finalize_extraction

NAME = "generic_changes"
EXTRACTOR = "ruleloom.generic_changes.git.v1"


def ignores_content(_path: str) -> bool:
    return False


def extract_generic_change_facts(
    evidence: DiffEvidence,
    options: PackOptions,
) -> PackExtraction:
    """Extract only language-neutral path and change-shape predicates."""

    return finalize_extraction(evidence, {}, extractor=EXTRACTOR, options=options)
