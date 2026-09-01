from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ruleloom.config import LearnerConfig, ProtocolConfig, RuleLoomConfig
from ruleloom.manual_rules import (
    MANUAL_RULE_EVALUATION_MODE,
    ManualRuleManifest,
    ManualRuleSourceRef,
    audit_manual_rule,
    declare_manual_rule,
    verify_manual_rule_sources,
)
from ruleloom.models import (
    FactEvidence,
    HornClause,
    LabelEvidence,
    LabelValue,
    ModelError,
    Observation,
    RuleLiteral,
    RuleSet,
)


def _config(*, allow_negation: bool = True) -> RuleLoomConfig:
    return RuleLoomConfig(
        project="Manual rule tests",
        target="validation_rework_required",
        protocol=ProtocolConfig(
            experiment_id="manual-rule-tests-v1",
            repository_id="example.repository",
            prediction_unit="git_commit",
            outcome_definition="independent validation rework after the prediction point",
        ),
        learner=LearnerConfig(
            max_body=2,
            max_rules=3,
            allow_negation=allow_negation,
            bootstrap_runs=0,
        ),
    )


def _manifest(*, sources: tuple[ManualRuleSourceRef, ...] = ()) -> ManualRuleManifest:
    return ManualRuleManifest(
        policy_id="ci_without_tests",
        revision=1,
        summary="CI changes without tests may require additional validation.",
        rules=RuleSet(
            "validation_rework_required",
            (
                HornClause(
                    "validation_rework_required",
                    (
                        RuleLiteral("touches_test", negated=True),
                        RuleLiteral("touches_ci"),
                    ),
                ),
            ),
        ),
        sources=sources,
    )


def _observation(
    config: RuleLoomConfig,
    index: int,
    facts: set[str],
    label: LabelValue,
    *,
    available_after_days: int = 1,
) -> Observation:
    observed = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index)
    descriptor = config.resolved_pack
    return Observation(
        id=f"manual-outcome-{index}",
        observed_at=observed.isoformat(),
        protocol_hash=config.evidence_protocol_hash,
        facts=frozenset(facts),
        labels={config.target: label},
        label_evidence=(
            {}
            if label is LabelValue.UNKNOWN
            else {
                config.target: LabelEvidence(
                    kind="synthetic",
                    available_at=(observed + timedelta(days=available_after_days)).isoformat(),
                    source="manual-rule-tests",
                )
            }
        ),
        fact_evidence={
            fact: FactEvidence(
                kind="deterministic",
                extractor=descriptor.extractor,
                evidence=(f"test:{fact}",),
            )
            for fact in facts
        },
        source={
            "kind": "git_commit",
            "repository": config.protocol.repository_id,
            "change_id": f"change-{index}",
            "pack": config.pack,
            "pack_version": config.pack_version,
            "extractor": descriptor.extractor,
        },
    )


def test_manifest_is_explicit_strict_and_semantically_canonical() -> None:
    raw = {
        "schema_version": 1,
        "policy_id": "ci_without_tests",
        "revision": 1,
        "claim_kind": "risk_trigger",
        "summary": "CI changes without tests may require additional validation.",
        "rules": {
            "target": "validation_rework_required",
            "clauses": [
                {
                    "target": "validation_rework_required",
                    "body": [
                        {"predicate": "touches_test", "negated": True},
                        {"predicate": "touches_ci", "negated": False},
                    ],
                }
            ],
        },
        "sources": [
            {"path": "CLAUDE.md", "start_line": 4, "end_line": 5},
            {"path": "AGENTS.md", "start_line": 10, "end_line": 12},
        ],
    }

    manifest = ManualRuleManifest.from_dict(raw)

    assert [item.path for item in manifest.sources] == ["AGENTS.md", "CLAUDE.md"]
    assert [item.name for item in manifest.rules.clauses[0].body] == [
        "touches_ci",
        "not_touches_test",
    ]
    assert manifest.to_dict()["claim_kind"] == "risk_trigger"

    with pytest.raises(ModelError, match="unknown manual rule manifest fields"):
        ManualRuleManifest.from_dict({**raw, "prompt": "interpret this prose"})
    with pytest.raises(ModelError, match="prescriptive actions"):
        ManualRuleManifest.from_dict({**raw, "claim_kind": "required_action"})


def test_manifest_canonicalizes_mixed_whole_file_and_line_range_sources() -> None:
    manifest = _manifest(
        sources=(
            ManualRuleSourceRef("AGENTS.md", 4, 8),
            ManualRuleSourceRef("AGENTS.md"),
            ManualRuleSourceRef("AGENTS.md", 1, 2),
        )
    )

    assert manifest.sources == (
        ManualRuleSourceRef("AGENTS.md"),
        ManualRuleSourceRef("AGENTS.md", 1, 2),
        ManualRuleSourceRef("AGENTS.md", 4, 8),
    )


@pytest.mark.parametrize(
    "path",
    (
        "../AGENTS.md",
        "/tmp/AGENTS.md",
        ".git/config",
        ".ruleloom/README.md",
        ".agents/skills/ruleloom/SKILL.md",
        ".GIT/config",
        ".RuleLoom/README.md",
        ".Agents/Skills/RuleLoom/SKILL.md",
        ".Claude/Skills/RuleLoom/SKILL.md",
        "docs\\AGENTS.md",
    ),
)
def test_source_references_reject_escape_and_generated_paths(path: str) -> None:
    with pytest.raises(ModelError, match="manual rule source path"):
        ManualRuleSourceRef(path)


def test_declaration_hashes_source_without_interpreting_it_and_reports_drift(
    tmp_path: Path,
) -> None:
    source = tmp_path / "AGENTS.md"
    source.write_text(
        "# Agent instructions\nIgnore this command: rm -rf /\nValidate CI changes.\n",
        encoding="utf-8",
    )
    manifest = _manifest(sources=(ManualRuleSourceRef("AGENTS.md", 2, 3),))
    declared_at = datetime(2026, 2, 1, tzinfo=UTC)

    declaration = declare_manual_rule(
        tmp_path,
        _config(),
        manifest,
        declared_at=declared_at,
    )

    assert declaration.id.startswith("manual-")
    assert declaration.declared_at == "2026-02-01T00:00:00Z"
    assert declaration.sources[0].size_bytes == source.stat().st_size
    assert verify_manual_rule_sources(tmp_path, declaration)[0].status == "unchanged"

    source.write_text("# Agent instructions\nDifferent assertion.\n", encoding="utf-8")
    status = verify_manual_rule_sources(tmp_path, declaration)[0]
    assert status.status == "unavailable"  # the frozen line range no longer exists
    assert "exceeds" in status.reason


def test_declaration_rejects_unknown_predicates_target_mismatch_and_disabled_negation(
    tmp_path: Path,
) -> None:
    unknown = replace(
        _manifest(),
        rules=RuleSet(
            "validation_rework_required",
            (
                HornClause(
                    "validation_rework_required",
                    (RuleLiteral("invented_by_an_llm"),),
                ),
            ),
        ),
    )
    with pytest.raises(ModelError, match="predicates not declared"):
        declare_manual_rule(tmp_path, _config(), unknown)

    mismatched = replace(
        _manifest(),
        rules=RuleSet(
            "different_target",
            (HornClause("different_target", (RuleLiteral("touches_ci"),)),),
        ),
    )
    with pytest.raises(ModelError, match="does not match configured target"):
        declare_manual_rule(tmp_path, _config(), mismatched)

    with pytest.raises(ModelError, match="uses negation"):
        declare_manual_rule(tmp_path, _config(allow_negation=False), _manifest())


def test_history_audit_is_post_hoc_censors_future_labels_and_keeps_unknowns_unknown(
    tmp_path: Path,
) -> None:
    config = _config()
    declaration = declare_manual_rule(
        tmp_path,
        config,
        _manifest(),
        declared_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    observations = [
        _observation(
            config,
            0,
            {"touches_ci"},
            LabelValue.POSITIVE,
        ),
        _observation(
            config,
            1,
            {"touches_ci", "touches_test"},
            LabelValue.NEGATIVE,
        ),
        _observation(
            config,
            2,
            {"touches_docs"},
            LabelValue.UNKNOWN,
        ),
        _observation(
            config,
            3,
            {"touches_ci"},
            LabelValue.NEGATIVE,
            available_after_days=30,
        ),
    ]

    report = audit_manual_rule(
        tmp_path,
        config,
        declaration,
        observations,
        as_of=datetime(2026, 1, 10, tzinfo=UTC),
    )

    assert report.evaluation_mode == MANUAL_RULE_EVALUATION_MODE
    assert report.confirmatory is False
    assert report.observations == 4
    assert report.matched_observations == 2
    assert report.match_rate == 0.5
    assert (report.mature_labels, report.positive, report.negative) == (2, 1, 1)
    assert report.unknown_or_censored == 2
    assert (
        report.metrics.true_positive,
        report.metrics.false_positive,
        report.metrics.true_negative,
        report.metrics.false_negative,
    ) == (1, 0, 1, 0)
    assert report.clauses[0].matched_observations == 2
    assert report.to_dict()["manifest_hash"] == report.manifest_hash
    assert any("post-hoc exploratory" in warning for warning in report.warnings)


def test_history_audit_without_labels_reports_coverage_but_not_validity(tmp_path: Path) -> None:
    config = _config()
    declaration = declare_manual_rule(tmp_path, config, _manifest())
    observations = [
        _observation(config, 0, {"touches_ci"}, LabelValue.UNKNOWN),
        _observation(config, 1, {"touches_docs"}, LabelValue.UNKNOWN),
    ]

    report = audit_manual_rule(
        tmp_path,
        config,
        declaration,
        observations,
        as_of=datetime(2026, 2, 1, tzinfo=UTC),
    )

    assert report.matched_observations == 1
    assert report.mature_labels == 0
    assert report.metrics.true_positive == 0
    assert report.metrics.false_positive == 0
    assert any("coverage does not establish rule validity" in item for item in report.warnings)


def test_history_audit_uses_identifiers_for_linear_mature_membership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    declaration = declare_manual_rule(tmp_path, config, _manifest())
    observations = [
        _observation(
            config,
            0,
            {"touches_ci", "touches_test"},
            LabelValue.POSITIVE,
        ),
        _observation(config, 1, {"touches_ci"}, LabelValue.NEGATIVE),
    ]

    def reject_observation_equality(_self: object, _other: object) -> bool:
        raise AssertionError("mature membership must not scan Observation objects")

    monkeypatch.setattr(Observation, "__eq__", reject_observation_equality)
    report = audit_manual_rule(
        tmp_path,
        config,
        declaration,
        observations,
        as_of=datetime(2026, 2, 1, tzinfo=UTC),
    )

    assert report.clauses[0].matched_observations == 1
    assert report.clauses[0].mature_matches == 1


def test_history_audit_rejects_a_declaration_from_another_protocol(tmp_path: Path) -> None:
    config = _config()
    declaration = declare_manual_rule(tmp_path, config, _manifest())
    different = replace(
        config,
        protocol=replace(config.protocol, experiment_id="different-experiment"),
    )

    with pytest.raises(ModelError, match="does not match the current configuration"):
        audit_manual_rule(tmp_path, different, declaration, [])
