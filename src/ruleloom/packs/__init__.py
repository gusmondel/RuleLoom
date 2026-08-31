"""Built-in evidence-pack registry.

The learner and lifecycle depend only on persisted Boolean facts. Adding a
language pack therefore requires implementing this contract and registering it
here; it does not require changing ILP, evaluation, promotion, or agent code.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

from ruleloom.models import FactEvidence, ModelError
from ruleloom.packs.base import DiffEvidence, EvidencePack, FileChange, PackExtraction, PackOptions
from ruleloom.packs.flutter_testing import (
    EXTRACTOR as FLUTTER_EXTRACTOR,
)
from ruleloom.packs.flutter_testing import (
    NAME as FLUTTER_NAME,
)
from ruleloom.packs.flutter_testing import (
    extract_flutter_testing_facts,
    wants_dart_content,
)
from ruleloom.packs.flutter_testing_v1 import (
    EXTRACTOR as FLUTTER_V1_EXTRACTOR,
)
from ruleloom.packs.flutter_testing_v1 import (
    extract_flutter_testing_v1_facts,
)
from ruleloom.packs.generic import (
    EXTRACTOR as GENERIC_EXTRACTOR,
)
from ruleloom.packs.generic import (
    NAME as GENERIC_NAME,
)
from ruleloom.packs.generic import (
    extract_generic_change_facts,
    ignores_content,
)

_COMMON_PREDICATES = (
    "large_change",
    "multi_file_change",
    "touches_ci",
    "touches_dependencies",
    "touches_docs",
    "touches_test",
)
_FLUTTER_PREDICATES = (
    "adds_widget_test",
    "auth",
    "backend_contract",
    "changes_dart",
    "large_change",
    "multi_file_change",
    "mutates_state",
    "navigation",
    "payment",
    "touches_test",
    "touches_widget",
    "user_input",
    "uses_async",
)

_PACKS = {
    (GENERIC_NAME, 1): EvidencePack(
        name=GENERIC_NAME,
        version=1,
        extractor=GENERIC_EXTRACTOR,
        description="Language-neutral change shape, tests, docs, CI, and dependency facts.",
        predicates=_COMMON_PREDICATES,
        content_path=ignores_content,
        extract=extract_generic_change_facts,
    ),
    (FLUTTER_NAME, 1): EvidencePack(
        name=FLUTTER_NAME,
        version=1,
        extractor=FLUTTER_V1_EXTRACTOR,
        description="Frozen schema-v1 Flutter extractor for reproducibility only.",
        predicates=_FLUTTER_PREDICATES,
        content_path=wants_dart_content,
        extract=extract_flutter_testing_v1_facts,
    ),
    (FLUTTER_NAME, 2): EvidencePack(
        name=FLUTTER_NAME,
        version=2,
        extractor=FLUTTER_EXTRACTOR,
        description="Generic change facts plus deterministic Dart and Flutter testing signals.",
        predicates=tuple(sorted(set(_FLUTTER_PREDICATES).union(_COMMON_PREDICATES))),
        content_path=wants_dart_content,
        extract=extract_flutter_testing_facts,
    ),
}

if len(_PACKS) != len({(item.name, item.version) for item in _PACKS.values()}):
    raise RuntimeError("duplicate evidence-pack name/version registration")


def available_packs() -> tuple[EvidencePack, ...]:
    return tuple(_PACKS[key] for key in sorted(_PACKS))


def latest_pack_version(name: str) -> int:
    versions = [version for pack_name, version in _PACKS if pack_name == name]
    if not versions:
        available = ", ".join(sorted({pack_name for pack_name, _ in _PACKS}))
        raise ModelError(f"unsupported evidence pack {name!r}; available packs: {available}")
    return max(versions)


def get_pack(name: str, version: int | None = None) -> EvidencePack:
    if version is not None and (
        isinstance(version, bool) or not isinstance(version, int) or version < 1
    ):
        raise ModelError("evidence pack version must be an integer >= 1")
    selected_version = latest_pack_version(name) if version is None else version
    try:
        return _PACKS[(name, selected_version)]
    except KeyError as exc:
        available = ", ".join(
            f"{pack_name}@{pack_version}" for pack_name, pack_version in sorted(_PACKS)
        )
        raise ModelError(
            f"unsupported evidence pack {name!r} version {selected_version}; "
            f"available packs: {available}"
        ) from exc


def matches_pack_version(value: object, expected: int) -> bool:
    """Return whether persisted provenance carries the exact integer pack version."""

    return not isinstance(value, bool) and isinstance(value, int) and value == expected


def validate_policy_pack_contract(
    pack: EvidencePack,
    metadata: Mapping[str, object],
    predicates: Collection[str],
    *,
    schema_version: int,
    evidence_protocol_hash: str,
    subject: str,
) -> None:
    """Fail closed when an active policy is not bound to its evidence-pack contract."""

    if metadata.get("pack") != pack.name:
        raise ModelError(f"{subject} fact pack is incompatible with {pack.name!r}")
    persisted_version = metadata.get("pack_version")
    if schema_version >= 2 and not matches_pack_version(persisted_version, pack.version):
        raise ModelError(f"{subject} lacks valid pack-version provenance")
    if (
        schema_version == 1
        and persisted_version is not None
        and not matches_pack_version(persisted_version, pack.version)
    ):
        raise ModelError(f"{subject} has conflicting pack-version provenance")
    extractors = metadata.get("extractors")
    if not isinstance(extractors, list) or not all(isinstance(item, str) for item in extractors):
        raise ModelError(f"{subject} has invalid extractor provenance")
    if extractors != [pack.extractor]:
        raise ModelError(f"{subject} extractor provenance is incompatible with {pack.extractor!r}")
    persisted_protocol = metadata.get("evidence_protocol_hash")
    if schema_version >= 2 and persisted_protocol != evidence_protocol_hash:
        raise ModelError(f"{subject} lacks matching evidence-protocol provenance")
    if (
        schema_version == 1
        and persisted_protocol is not None
        and persisted_protocol != evidence_protocol_hash
    ):
        raise ModelError(f"{subject} has conflicting evidence-protocol provenance")
    undeclared = set(predicates).difference(pack.predicates)
    if undeclared:
        raise ModelError(
            f"{subject} uses predicates not declared by {pack.name}@{pack.version}: "
            + ", ".join(sorted(undeclared))
        )


def validate_persisted_extraction(
    pack: EvidencePack,
    facts: Collection[str],
    provenance: Mapping[str, FactEvidence],
    *,
    subject: str,
) -> None:
    """Fail closed when persisted facts do not match a built-in pack contract."""

    fact_set = set(facts)
    undeclared = fact_set.difference(pack.predicates)
    if undeclared:
        raise ModelError(
            f"{subject} contains predicates not declared by {pack.name}@{pack.version}: "
            + ", ".join(sorted(undeclared))
        )
    provenance_set = set(provenance)
    if fact_set != provenance_set:
        missing = sorted(fact_set.difference(provenance_set))
        extra = sorted(provenance_set.difference(fact_set))
        raise ModelError(
            f"{subject} facts and fact_evidence differ; missing={missing}, extra={extra}"
        )
    for fact, item in provenance.items():
        if item.kind != "deterministic" or item.extractor != pack.extractor:
            raise ModelError(
                f"{subject} fact {fact!r} must have deterministic provenance from "
                f"{pack.extractor!r}"
            )


__all__ = [
    "DiffEvidence",
    "EvidencePack",
    "FileChange",
    "PackExtraction",
    "PackOptions",
    "available_packs",
    "get_pack",
    "latest_pack_version",
    "matches_pack_version",
    "validate_persisted_extraction",
    "validate_policy_pack_contract",
]
