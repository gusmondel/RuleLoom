from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import ruleloom.repository_assertions as repository_assertions_module
from ruleloom.models import LabelEvidence, LabelValue, ModelError, Observation, RuleLiteral
from ruleloom.repository_assertions import (
    REPOSITORY_ASSERTION_EVALUATION_MODE,
    RepositoryAssertion,
    RepositoryAssertionDeclaration,
    RepositoryAssertionManifest,
    RepositoryAssertionSourceRef,
    audit_repository_assertions,
    declare_repository_assertions,
    verify_repository_assertion_sources,
)

PROTOCOL_HASH = "b" * 64
TARGET = "validation_rework_required"


def _assertion(
    *,
    sources: tuple[RepositoryAssertionSourceRef, ...] = (
        RepositoryAssertionSourceRef("ENGINEERING.md"),
    ),
) -> RepositoryAssertion:
    return RepositoryAssertion(
        assertion_id="contracts_expect_tests",
        revision=1,
        summary="Contract contact expects test contact in the same change.",
        category="test_structure",
        antecedent=(RuleLiteral("touches_contracts"),),
        expectation=(RuleLiteral("touches_tests"),),
        sources=sources,
    )


def _observation(index: int, facts: set[str]) -> Observation:
    return Observation(
        id=f"assertion-observation-{index}",
        observed_at=datetime(2026, 1, index, tzinfo=UTC).isoformat(),
        protocol_hash=PROTOCOL_HASH,
        facts=frozenset(facts),
        labels={TARGET: LabelValue.UNKNOWN},
        source={"repository": "repository.example"},
        metadata={"topological_index": index},
    )


def test_manifest_is_strict_explicit_and_canonical() -> None:
    raw = {
        "schema_version": 1,
        "assertions": [
            {
                "assertion_id": "contracts_expect_tests",
                "revision": 1,
                "summary": "Contract contact expects test contact in the same change.",
                "category": "test_structure",
                "semantics": "antecedent_implies_expectation",
                "antecedent": [
                    {"predicate": "touches_contracts", "negated": False},
                    {"predicate": "touches_generated", "negated": True},
                ],
                "expectation": [{"predicate": "touches_tests", "negated": False}],
                "sources": [
                    {"path": "ENGINEERING.md", "start_line": 3, "end_line": 4},
                    {"path": "AGENTS.md"},
                ],
            }
        ],
    }

    manifest = RepositoryAssertionManifest.from_dict(raw)

    assertion = manifest.assertions[0]
    assert [item.name for item in assertion.antecedent] == [
        "touches_contracts",
        "not_touches_generated",
    ]
    assert [item.path for item in assertion.sources] == ["AGENTS.md", "ENGINEERING.md"]
    assert manifest.to_dict() == RepositoryAssertionManifest.from_dict(manifest.to_dict()).to_dict()
    with pytest.raises(ModelError, match="unknown repository assertion fields"):
        RepositoryAssertionManifest.from_dict(
            {
                **raw,
                "assertions": [{**raw["assertions"][0], "prose_prompt": "interpret this"}],
            }
        )
    with pytest.raises(ModelError, match="antecedent_implies_expectation"):
        RepositoryAssertionManifest.from_dict(
            {
                **raw,
                "assertions": [{**raw["assertions"][0], "semantics": "causes"}],
            }
        )


def test_assertion_requires_a_source_and_canonicalizes_mixed_spans() -> None:
    with pytest.raises(ModelError, match="requires at least one hashed source"):
        RepositoryAssertion(
            assertion_id="missing_source",
            revision=1,
            summary="This declaration is intentionally incomplete.",
            antecedent=(RuleLiteral("touches_contracts"),),
            expectation=(RuleLiteral("touches_tests"),),
        )

    assertion = _assertion(
        sources=(
            RepositoryAssertionSourceRef("ENGINEERING.md", 4, 8),
            RepositoryAssertionSourceRef("ENGINEERING.md"),
            RepositoryAssertionSourceRef("ENGINEERING.md", 1, 2),
        )
    )

    assert assertion.sources == (
        RepositoryAssertionSourceRef("ENGINEERING.md"),
        RepositoryAssertionSourceRef("ENGINEERING.md", 1, 2),
        RepositoryAssertionSourceRef("ENGINEERING.md", 4, 8),
    )


@pytest.mark.parametrize(
    "path",
    (
        "../ENGINEERING.md",
        "/tmp/ENGINEERING.md",
        ".git/config",
        ".GIT/config",
        ".ruleloom/assertions.json",
        ".RuleLoom/assertions.json",
        ".agents/skills/ruleloom/SKILL.md",
        ".CLAUDE/skills/ruleloom/SKILL.md",
    ),
)
def test_source_paths_reject_traversal_git_and_generated_output(path: str) -> None:
    with pytest.raises(ModelError, match="normalized repository-relative"):
        RepositoryAssertionSourceRef(path)


def test_declaration_hashes_document_and_exact_source_span(tmp_path: Path) -> None:
    source = tmp_path / "ENGINEERING.md"
    source.write_text("heading\nassertion line\ncontext\n", encoding="utf-8")
    manifest = RepositoryAssertionManifest(
        (_assertion(sources=(RepositoryAssertionSourceRef("ENGINEERING.md", 2, 2),)),)
    )

    declaration = declare_repository_assertions(
        tmp_path,
        manifest,
        repository_id="repository.example",
        protocol_hash=PROTOCOL_HASH,
        predicate_vocabulary=("touches_tests", "touches_contracts"),
        declared_at=datetime(2026, 1, 10, tzinfo=UTC),
    )

    snapshot = declaration.sources[0]
    assert snapshot.document_sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert snapshot.excerpt_sha256 == hashlib.sha256(b"assertion line").hexdigest()
    assert declaration.predicate_vocabulary == ("touches_contracts", "touches_tests")
    assert declaration.id == declaration.expected_id
    assert len(declaration.manifest_hash) == 64
    assert RepositoryAssertionDeclaration.from_dict(declaration.to_dict()) == declaration

    tampered = declaration.to_dict()
    tampered["manifest_hash"] = "0" * 64
    with pytest.raises(ModelError, match="manifest_hash does not match"):
        RepositoryAssertionDeclaration.from_dict(tampered)

    source.write_text("heading\nchanged assertion\ncontext\n", encoding="utf-8")
    statuses = verify_repository_assertion_sources(tmp_path, declaration)
    assert statuses[0].status == "changed"


def test_declaration_reuses_documents_and_enforces_total_source_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "ENGINEERING.md").write_text("first\nsecond\n", encoding="utf-8")
    manifest = RepositoryAssertionManifest(
        (
            _assertion(
                sources=(
                    RepositoryAssertionSourceRef("ENGINEERING.md"),
                    RepositoryAssertionSourceRef("ENGINEERING.md", 2, 2),
                )
            ),
        )
    )
    original = repository_assertions_module._load_source_document
    loaded: list[str] = []

    def counting_loader(
        root: Path,
        path_text: str,
    ) -> repository_assertions_module._RepositoryAssertionSourceDocument:
        loaded.append(path_text)
        return original(root, path_text)

    monkeypatch.setattr(repository_assertions_module, "_load_source_document", counting_loader)
    declare_repository_assertions(
        tmp_path,
        manifest,
        repository_id="repository.example",
        protocol_hash=PROTOCOL_HASH,
        predicate_vocabulary=("touches_contracts", "touches_tests"),
        declared_at=datetime(2026, 1, 10, tzinfo=UTC),
    )
    assert loaded == ["ENGINEERING.md"]

    (tmp_path / "A.md").write_text("abc", encoding="utf-8")
    (tmp_path / "B.md").write_text("def", encoding="utf-8")
    monkeypatch.setattr(repository_assertions_module, "_MAX_TOTAL_SOURCE_BYTES", 5)
    with pytest.raises(ModelError, match="total source byte budget"):
        declare_repository_assertions(
            tmp_path,
            RepositoryAssertionManifest(
                (
                    _assertion(
                        sources=(
                            RepositoryAssertionSourceRef("A.md"),
                            RepositoryAssertionSourceRef("B.md"),
                        )
                    ),
                )
            ),
            repository_id="repository.example",
            protocol_hash=PROTOCOL_HASH,
            predicate_vocabulary=("touches_contracts", "touches_tests"),
            declared_at=datetime(2026, 1, 10, tzinfo=UTC),
        )


def test_declaration_rejects_predicates_outside_frozen_vocabulary(tmp_path: Path) -> None:
    (tmp_path / "ENGINEERING.md").write_text("expectation\n", encoding="utf-8")
    with pytest.raises(ModelError, match="outside the frozen vocabulary"):
        declare_repository_assertions(
            tmp_path,
            RepositoryAssertionManifest((_assertion(),)),
            repository_id="repository.example",
            protocol_hash=PROTOCOL_HASH,
            predicate_vocabulary=("touches_contracts",),
            declared_at=datetime(2026, 1, 10, tzinfo=UTC),
        )


def test_audit_reports_structural_adherence_and_is_outcome_blind(tmp_path: Path) -> None:
    (tmp_path / "ENGINEERING.md").write_text("expectation\n", encoding="utf-8")
    declaration = declare_repository_assertions(
        tmp_path,
        RepositoryAssertionManifest(
            (_assertion(sources=(RepositoryAssertionSourceRef("ENGINEERING.md"),)),)
        ),
        repository_id="repository.example",
        protocol_hash=PROTOCOL_HASH,
        predicate_vocabulary=("touches_contracts", "touches_tests"),
        declared_at=datetime(2026, 1, 10, tzinfo=UTC),
    )
    observations = [
        _observation(1, {"touches_contracts", "touches_tests"}),
        _observation(2, {"touches_contracts"}),
        _observation(3, {"touches_tests"}),
    ]
    labelled = [
        replace(
            item,
            labels={TARGET: LabelValue.POSITIVE},
            label_evidence={
                TARGET: LabelEvidence(
                    kind="synthetic",
                    available_at=(
                        datetime.fromisoformat(item.observed_at) + timedelta(days=1)
                    ).isoformat(),
                    source="must-not-be-consumed",
                )
            },
        )
        for item in observations
    ]

    unknown_audit = audit_repository_assertions(tmp_path, declaration, observations)
    labelled_audit = audit_repository_assertions(tmp_path, declaration, labelled)

    assert unknown_audit.to_dict() == labelled_audit.to_dict()
    assert unknown_audit.outcome_blind is True
    assert unknown_audit.evaluation_mode == REPOSITORY_ASSERTION_EVALUATION_MODE
    assert unknown_audit.ordering == "first_parent_topology"
    row = unknown_audit.rows[0]
    assert row.eligible_observations == 2
    assert row.expectation_met == 1
    assert row.expectation_absent == 1
    assert row.adherence_rate == 0.5
    assert row.absent_example_ids == ("assertion-observation-2",)
    assert len(unknown_audit.manifest_hash) == 64
    assert any("does not establish causality" in item for item in unknown_audit.limitations)
    rendered = unknown_audit.render_text()
    assert rendered.startswith("RuleLoom repository assertion audit\n")
    assert "contracts_expect_tests: 50.0%; 2 eligible, 1 exceptions" in rendered
    assert f"Manifest: {unknown_audit.manifest_hash}" in rendered


def test_audit_rejects_duplicate_or_mismatched_observations(tmp_path: Path) -> None:
    (tmp_path / "ENGINEERING.md").write_text("expectation\n", encoding="utf-8")
    declaration = declare_repository_assertions(
        tmp_path,
        RepositoryAssertionManifest((_assertion(),)),
        repository_id="repository.example",
        protocol_hash=PROTOCOL_HASH,
        predicate_vocabulary=("touches_contracts", "touches_tests"),
        declared_at=datetime(2026, 1, 10, tzinfo=UTC),
    )
    observation = _observation(1, {"touches_contracts"})

    with pytest.raises(ModelError, match="ids must be unique"):
        audit_repository_assertions(tmp_path, declaration, [observation, observation])
    with pytest.raises(ModelError, match="does not match"):
        audit_repository_assertions(
            tmp_path,
            declaration,
            [replace(observation, protocol_hash="c" * 64)],
        )
