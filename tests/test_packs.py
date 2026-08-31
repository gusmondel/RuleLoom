from __future__ import annotations

import pytest

from ruleloom.config import EvidenceConfig, RuleLoomConfig
from ruleloom.models import FactEvidence, ModelError
from ruleloom.packs import DiffEvidence, EvidencePack, FileChange, PackExtraction, get_pack


def test_registry_resolves_exact_pack_versions() -> None:
    assert get_pack("flutter_testing", 1).extractor == "ruleloom.flutter_testing.git.v1"
    assert get_pack("flutter_testing", 2).extractor == "ruleloom.flutter_testing.git.v2"
    assert get_pack("generic_changes", 1).content_path("service.py") is False
    with pytest.raises(ModelError, match="available packs"):
        get_pack("python_testing", 1)
    for invalid in (True, 1.0, 0):
        with pytest.raises(ModelError, match="integer >= 1"):
            get_pack("generic_changes", invalid)  # type: ignore[arg-type]


def test_pack_contract_requires_declared_facts_with_provenance() -> None:
    broken = EvidencePack(
        name="broken",
        version=1,
        extractor="tests.broken.v1",
        description="broken test pack",
        predicates=("risk",),
        content_path=lambda _path: False,
        extract=lambda _evidence, _options: PackExtraction(
            facts=frozenset({"risk"}),
            provenance={},
            metadata={},
        ),
    )

    with pytest.raises(ValueError, match="matching provenance"):
        broken.run(
            DiffEvidence(changes=()),
            EvidenceConfig().pack_options,
        )

    wrong_provenance = EvidencePack(
        name="wrong_provenance",
        version=1,
        extractor="tests.expected.v1",
        description="wrong provenance test pack",
        predicates=("risk",),
        content_path=lambda _path: False,
        extract=lambda _evidence, _options: PackExtraction(
            facts=frozenset({"risk"}),
            provenance={
                "risk": FactEvidence(
                    kind="human",
                    extractor="tests.untrusted.v1",
                    evidence=("synthetic",),
                )
            },
            metadata={},
        ),
    )
    with pytest.raises(ValueError, match="deterministic provenance"):
        wrong_provenance.run(DiffEvidence(changes=()), EvidenceConfig().pack_options)


def test_flutter_semantic_change_isolated_by_version_and_protocol_hash() -> None:
    evidence = DiffEvidence(
        changes=(FileChange("lib/notifier.dart", additions=1, deletions=0),),
        content_patch="+state = const AsyncLoading();",
    )
    options = EvidenceConfig().pack_options
    v1 = get_pack("flutter_testing", 1).run(evidence, options)
    v2 = get_pack("flutter_testing", 2).run(evidence, options)
    v2_config = RuleLoomConfig(
        schema_version=2,
        project="ExampleProject",
        pack="flutter_testing",
        pack_version=2,
    )
    v1_profile = RuleLoomConfig(project="ExampleProject")

    assert "mutates_state" not in v1.facts
    assert "mutates_state" in v2.facts
    assert v2.provenance["mutates_state"] == FactEvidence(
        kind="deterministic",
        extractor="ruleloom.flutter_testing.git.v2",
        evidence=("diff-pattern:provider state assignment",),
    )
    assert v1_profile.evidence_protocol_hash != v2_config.evidence_protocol_hash

    with pytest.raises(ModelError, match="frozen for schema-v1"):
        RuleLoomConfig(
            schema_version=2,
            project="ExampleProject",
            pack="flutter_testing",
            pack_version=1,
        )
