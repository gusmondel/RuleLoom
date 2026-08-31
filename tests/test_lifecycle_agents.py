from __future__ import annotations

import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import ruleloom.lifecycle as lifecycle_module
from ruleloom.agents import GENERATED_MARKER, sync_agents
from ruleloom.config import (
    EvaluationConfig,
    LearnerConfig,
    PromotionConfig,
    ProtocolConfig,
    RuleLoomConfig,
)
from ruleloom.lifecycle import (
    ShadowEvidence,
    deprecate_candidate,
    learn_candidate,
    make_prediction,
    match_rules,
    observations_hash,
    promote_candidate,
    promotion_decision,
    readiness,
    trust_reviewed_artifact,
)
from ruleloom.models import (
    Candidate,
    FactEvidence,
    HornClause,
    LabelEvidence,
    LabelValue,
    Metrics,
    ModelError,
    Observation,
    RuleLiteral,
    RuleSet,
)
from ruleloom.packs import ConfiguredPathsConfig, PathPredicateConfig, get_pack
from ruleloom.project import initialize_project, validate_observations, validate_project
from ruleloom.storage import (
    append_prediction,
    candidate_path,
    dataset_path,
    deprecated_path,
    load_approved,
    load_candidate,
    load_shadow,
    predictions_path,
    read_json,
    record_transition_attestation,
    save_candidate,
    save_observations,
    shadow_path,
    trusted_state_path,
    write_json,
)

TARGET = "needs_extra_validation"


def _git_init(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=RuleLoom Tests",
            "-c",
            "user.email=tests@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "initial",
        ],
        check=True,
        capture_output=True,
    )


def _observation(
    index: int,
    label: LabelValue,
    facts: set[str] | None = None,
    *,
    evidence_facts: set[str] | None = None,
    kind: str = "git_commit",
    repository_id: str = "repository.unspecified",
    protocol_hash: str | None = None,
    config: RuleLoomConfig | None = None,
) -> Observation:
    observed = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index)
    available = observed + timedelta(hours=1)
    facts = facts or set()
    effective_config = config or RuleLoomConfig(
        project="Tests",
        protocol=ProtocolConfig(repository_id=repository_id),
    )
    descriptor = effective_config.resolved_pack
    provenance_facts = facts if evidence_facts is None else evidence_facts
    source = {
        "kind": kind,
        "repository": effective_config.protocol.repository_id,
        "change_id": f"change-{index}",
        "pack": effective_config.pack,
        "extractor": descriptor.extractor,
    }
    if effective_config.schema_version >= 2:
        source["pack_version"] = effective_config.pack_version
    if descriptor.configuration_hash is not None:
        source["pack_config_hash"] = descriptor.configuration_hash
    metadata = {}
    if descriptor.configuration_hash is not None:
        metadata["configured_paths_config_hash"] = descriptor.configuration_hash
    return Observation(
        id=f"outcome-{index}",
        observed_at=observed.isoformat(),
        protocol_hash=protocol_hash or effective_config.evidence_protocol_hash,
        facts=frozenset(facts),
        labels={TARGET: label},
        label_evidence=(
            {}
            if label is LabelValue.UNKNOWN
            else {
                TARGET: LabelEvidence(
                    kind="synthetic",
                    available_at=available.isoformat(),
                    source="tests",
                )
            }
        ),
        fact_evidence={
            fact: FactEvidence(
                kind="deterministic",
                extractor=descriptor.extractor,
                evidence=(f"synthetic:{fact}",),
            )
            for fact in provenance_facts
        },
        source=source,
        metadata=metadata,
    )


def _candidate(
    *,
    candidate_id: str = "cand-lifecycle",
    positive: int = 50,
    status: str = "candidate",
    test_metrics: Metrics | None = None,
    baseline_metrics: Metrics | None = None,
    stability: float = 0.8,
    with_rules: bool = True,
    config: RuleLoomConfig | None = None,
    dataset_hash: str | None = None,
) -> Candidate:
    effective_config = config or _promotion_config()
    clauses = (HornClause(TARGET, (RuleLiteral("large_change"),)),) if with_rules else ()
    test = test_metrics or Metrics.from_counts(8, 2, 8, 2)
    baseline = baseline_metrics or Metrics.from_counts(0, 0, 10, 10)
    descriptor = effective_config.resolved_pack
    metadata = {
        "pack": effective_config.pack,
        "pack_version": effective_config.pack_version,
        "repository_id": effective_config.protocol.repository_id,
        "evidence_protocol_hash": effective_config.evidence_protocol_hash,
        "historical_observation_unit": "git_commit",
        "extractors": [descriptor.extractor],
        "readiness": {"positive": positive},
        "rule_evaluation": [
            {
                "signature": f"{TARGET}:-large_change",
                "train": test.to_dict(),
                "test": test.to_dict(),
            }
        ]
        if with_rules
        else [],
    }
    if descriptor.configuration_hash is not None:
        metadata["pack_config_hash"] = descriptor.configuration_hash
    return Candidate(
        id=candidate_id,
        created_at="2026-08-31T12:00:00Z",
        engine="horn",
        engine_version="test",
        dataset_hash=dataset_hash or observations_hash([]),
        config_hash=effective_config.hash,
        rules=RuleSet(TARGET, clauses),
        metrics={"train": test, "test": test},
        baselines={
            "never_alert": baseline,
            "always_alert": baseline,
            "train_majority": baseline,
            "best_single_literal": baseline,
        },
        stability=stability,
        train_ids=tuple(f"train-{index}" for index in range(1, 9)),
        test_ids=("test-1", "test-2", "test-3", "test-4"),
        metadata=metadata,
        review=(
            {"reviewer": "Test Reviewer", "note": "approved for test"}
            if status == "approved"
            else {}
        ),
        status=status,
    ).with_identity()


def _passing_shadow_evidence(candidate: Candidate) -> ShadowEvidence:
    metrics = Metrics.from_counts(20, 0, 20, 0)
    return ShadowEvidence(
        predictions=40,
        unique_observations=40,
        mature_outcomes=40,
        elapsed_days=14,
        metrics=metrics,
        rule_metrics={signature: metrics for signature in candidate.rules.signatures},
        manifest_hash="a" * 64,
    )


def _promotion_config() -> RuleLoomConfig:
    return RuleLoomConfig(
        project="ExampleProject",
        promotion=PromotionConfig(
            min_test_precision=0.75,
            min_test_recall=0.5,
            min_stability=0.4,
            require_test_set=True,
            min_positive_for_shadow=20,
            min_positive_for_approval=50,
            require_baseline_improvement=True,
        ),
    )


def test_readiness_reports_evidence_coverage_stages_and_censoring() -> None:
    observations = [
        _observation(
            0,
            LabelValue.POSITIVE,
            {"large_change", "uses_async"},
            evidence_facts={"large_change"},
        ),
        _observation(1, LabelValue.NEGATIVE, {"touches_test"}, evidence_facts={"touches_test"}),
        _observation(2, LabelValue.UNKNOWN),
    ]

    result = readiness(observations, TARGET)

    assert result.observations == 3
    assert (result.positive, result.negative, result.unknown) == (1, 1, 1)
    assert result.fact_evidence_coverage == pytest.approx(2 / 3)
    assert result.label_evidence_coverage == 1.0
    assert result.distinct_predicates == 3
    assert result.stage == "collection"
    assert any("censored" in warning for warning in result.warnings)

    shadow = readiness([_observation(index, LabelValue.POSITIVE) for index in range(20)], TARGET)
    preliminary = readiness(
        [_observation(index, LabelValue.POSITIVE) for index in range(50)], TARGET
    )
    assert shadow.stage == "shadow"
    assert preliminary.stage == "preliminary_evaluation"


def test_project_validation_rejects_labels_available_before_observation() -> None:
    item = _observation(0, LabelValue.POSITIVE, {"large_change"})
    invalid_evidence = LabelEvidence(
        kind="synthetic",
        available_at="2025-12-31T23:00:00+00:00",
        source="tests",
    )

    with pytest.raises(ModelError, match="cannot predate observation time"):
        validate_observations(
            [replace(item, label_evidence={TARGET: invalid_evidence})],
            RuleLoomConfig(project="ExampleProject"),
        )


def test_project_validation_rejects_source_and_checkout_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "example_project"
    _git_init(root)
    initialized = initialize_project(root, "ExampleProject")
    valid = _observation(
        0,
        LabelValue.UNKNOWN,
        config=initialized.config,
    )
    for source, message in (
        ({}, "source.kind"),
        ({**valid.source, "repository": "repository.other"}, "different repository"),
        ({**valid.source, "pack": "other"}, "different fact pack"),
        ({**valid.source, "extractor": ""}, "extractor provenance"),
    ):
        with pytest.raises(ModelError, match=message):
            validate_observations(
                [replace(valid, source=source)],
                initialized.config,
            )
    wrong_config = replace(
        initialized.config,
        protocol=replace(
            initialized.config.protocol,
            repository_id="repository.other",
        ),
    )
    with pytest.raises(ModelError, match="does not match this checkout"):
        validate_project(root, wrong_config)


def test_validation_and_learning_reject_tampered_pack_facts() -> None:
    config = RuleLoomConfig(project="ExampleProject")
    valid = _observation(0, LabelValue.POSITIVE, {"large_change"}, config=config)
    undeclared_evidence = FactEvidence(
        kind="deterministic",
        extractor="ruleloom.flutter_testing.git.v1",
        evidence=("synthetic:injected_predicate",),
    )
    cases = (
        (
            replace(
                valid,
                facts=frozenset({"large_change", "injected_predicate"}),
                fact_evidence={
                    **valid.fact_evidence,
                    "injected_predicate": undeclared_evidence,
                },
            ),
            "not declared",
        ),
        (replace(valid, fact_evidence={}), "facts and fact_evidence differ"),
        (
            replace(
                valid,
                fact_evidence={
                    "large_change": replace(
                        valid.fact_evidence["large_change"],
                        extractor="untrusted.extractor.v1",
                    )
                },
            ),
            "deterministic provenance",
        ),
    )

    for tampered, message in cases:
        with pytest.raises(ModelError, match=message):
            validate_observations([tampered], config)
        with pytest.raises(ModelError, match=message):
            learn_candidate([tampered], config)


@pytest.mark.parametrize("invalid_version", [True, 1.0, None])
def test_v2_consumers_reject_non_integer_or_missing_pack_version(
    invalid_version: object,
) -> None:
    config = RuleLoomConfig(
        project="ExampleProject",
        schema_version=2,
        pack="generic_changes",
        pack_version=1,
    )
    historical = _observation(
        0,
        LabelValue.POSITIVE,
        {"large_change"},
        config=config,
    )
    prospective = _observation(
        1,
        LabelValue.UNKNOWN,
        {"large_change"},
        kind="git_worktree",
        config=config,
    )
    if invalid_version is None:
        historical_source = {
            key: value for key, value in historical.source.items() if key != "pack_version"
        }
        prospective_source = {
            key: value for key, value in prospective.source.items() if key != "pack_version"
        }
    else:
        historical_source = {**historical.source, "pack_version": invalid_version}
        prospective_source = {**prospective.source, "pack_version": invalid_version}

    with pytest.raises(ModelError, match="fact pack version"):
        validate_observations([replace(historical, source=historical_source)], config)
    with pytest.raises(ModelError, match="fact pack version"):
        learn_candidate([replace(historical, source=historical_source)], config)
    with pytest.raises(ModelError, match="fact pack version"):
        make_prediction(
            replace(prospective, source=prospective_source),
            [_candidate(status="approved", config=config)],
            config,
        )


@pytest.mark.parametrize("invalid_version", [2, True, 1.0])
def test_v1_consumers_reject_conflicting_present_pack_version(
    invalid_version: object,
) -> None:
    config = RuleLoomConfig(project="ExampleProject")
    historical = _observation(0, LabelValue.POSITIVE, {"large_change"}, config=config)
    prospective = _observation(
        1,
        LabelValue.UNKNOWN,
        {"large_change"},
        kind="git_worktree",
        config=config,
    )
    historical_source = {**historical.source, "pack_version": invalid_version}
    prospective_source = {**prospective.source, "pack_version": invalid_version}

    with pytest.raises(ModelError, match="fact pack version"):
        validate_observations([replace(historical, source=historical_source)], config)
    with pytest.raises(ModelError, match="fact pack version"):
        learn_candidate([replace(historical, source=historical_source)], config)
    with pytest.raises(ModelError, match="fact pack version"):
        make_prediction(
            replace(prospective, source=prospective_source),
            [_candidate(status="approved", config=config)],
            config,
        )


def test_prediction_rejects_candidate_outside_v2_pack_contract() -> None:
    config = RuleLoomConfig(
        project="ExampleProject",
        schema_version=2,
        pack="generic_changes",
        pack_version=1,
    )
    observation = _observation(
        1,
        LabelValue.UNKNOWN,
        {"large_change"},
        kind="git_worktree",
        config=config,
    )
    baseline = _candidate(status="approved", config=config)
    invalid = (
        (
            replace(
                baseline,
                metadata={**baseline.metadata, "pack_version": True},
            ).with_identity(),
            "pack-version provenance",
        ),
        (
            replace(
                baseline,
                metadata={
                    key: value
                    for key, value in baseline.metadata.items()
                    if key != "evidence_protocol_hash"
                },
            ).with_identity(),
            "evidence-protocol provenance",
        ),
        (
            replace(
                baseline,
                rules=RuleSet(
                    TARGET,
                    (
                        HornClause(
                            TARGET,
                            (RuleLiteral("undeclared_predicate", negated=True),),
                        ),
                    ),
                ),
            ).with_identity(),
            "predicates not declared",
        ),
    )

    for candidate, message in invalid:
        with pytest.raises(ModelError, match=message):
            make_prediction(observation, [candidate], config)


def test_approval_gates_require_data_quality_performance_and_stability() -> None:
    config = _promotion_config()
    passing = _candidate()
    evidence = _passing_shadow_evidence(passing)

    assert promotion_decision(
        passing,
        config,
        "approved",
        prospective_shadow=evidence,
    ).allowed

    too_few_candidate = _candidate(positive=49)
    too_few = promotion_decision(
        too_few_candidate,
        config,
        "approved",
        prospective_shadow=_passing_shadow_evidence(too_few_candidate),
    )
    assert not too_few.allowed
    assert any("positive outcomes 49" in reason for reason in too_few.unmet)

    unstable_candidate = _candidate(stability=0.2)
    unstable = promotion_decision(
        unstable_candidate,
        config,
        "approved",
        prospective_shadow=_passing_shadow_evidence(unstable_candidate),
    )
    assert any("stability" in reason for reason in unstable.unmet)

    weak_candidate = _candidate(test_metrics=Metrics.from_counts(1, 9, 9, 9))
    weak = promotion_decision(
        weak_candidate,
        config,
        "approved",
        prospective_shadow=_passing_shadow_evidence(weak_candidate),
    )
    assert any("test precision" in reason for reason in weak.unmet)
    assert any("test recall" in reason for reason in weak.unmet)

    no_improvement_metrics = Metrics.from_counts(8, 2, 8, 2)
    no_improvement_candidate = _candidate(baseline_metrics=no_improvement_metrics)
    no_improvement = promotion_decision(
        no_improvement_candidate,
        config,
        "approved",
        prospective_shadow=_passing_shadow_evidence(no_improvement_candidate),
    )
    assert any("does not exceed best baseline" in reason for reason in no_improvement.unmet)

    empty_candidate = _candidate(with_rules=False)
    empty = promotion_decision(
        empty_candidate,
        config,
        "approved",
        prospective_shadow=_passing_shadow_evidence(empty_candidate),
    )
    assert any("no learned rules" in reason for reason in empty.unmet)


def test_shadow_gate_uses_minimum_positive_outcomes_and_rules() -> None:
    config = _promotion_config()

    assert promotion_decision(_candidate(positive=20), config, "shadow").allowed
    decision = promotion_decision(_candidate(positive=19), config, "shadow")
    assert not decision.allowed
    assert decision.unmet == ("positive outcomes 19 < required 20",)
    with pytest.raises(ModelError, match="shadow or approved"):
        promotion_decision(_candidate(), config, "production")


def test_initialize_creates_portable_agent_skills_and_refuses_reinitialization(
    tmp_path: Path,
) -> None:
    root = tmp_path / "example_project"
    _git_init(root)

    result = initialize_project(root, "ExampleProject", agents=("codex", "claude"))

    assert result.config.project == "ExampleProject"
    assert (result.config.schema_version, result.config.pack, result.config.pack_version) == (
        2,
        "generic_changes",
        1,
    )
    assert (root / ".ruleloom/config.json").is_file()
    assert (root / ".ruleloom/observations.jsonl").read_text(encoding="utf-8") == ""
    assert (root / ".ruleloom/predictions.jsonl").read_text(encoding="utf-8") == ""
    assert len(result.agent_files) == 4
    for path in (
        root / ".agents/skills/ruleloom/SKILL.md",
        root / ".claude/skills/ruleloom/SKILL.md",
    ):
        content = path.read_text(encoding="utf-8")
        assert GENERATED_MARKER in content
        assert "ruleloom assess --base HEAD --change-id <stable-change-id>" in content
        assert "Approved-policy assessments may show facts" in content
        assert "rule matches normally" in content
        assert "do not pass `--blind` or `--include-shadow`" in content
        assert "Shadow policies are never synchronized into this skill" in content
        assert "Never promote" in content

    approved_rules = root / ".agents/skills/ruleloom/references/approved-rules.md"
    assert "RuleLoom must abstain" in approved_rules.read_text(encoding="utf-8")
    with pytest.raises(ModelError, match="refusing to overwrite"):
        initialize_project(root, "ExampleProject", agents=("codex", "claude"))


def test_initialize_does_not_replace_an_explicit_empty_project_name(tmp_path: Path) -> None:
    root = tmp_path / "named_repository"
    _git_init(root)

    with pytest.raises(ModelError, match="project cannot be empty"):
        initialize_project(root, "")


def test_initialize_refuses_managed_directory_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "example_project"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / ".ruleloom").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ModelError, match="symlink"):
        initialize_project(root, "ExampleProject")

    assert list(outside.iterdir()) == []


def test_initialize_only_resolves_selected_agent_destinations(tmp_path: Path) -> None:
    outside = tmp_path / "shared-claude-skills"
    outside.mkdir()

    def repository(name: str) -> Path:
        root = tmp_path / name
        _git_init(root)
        (root / ".claude").mkdir()
        (root / ".claude/skills").symlink_to(outside, target_is_directory=True)
        return root

    no_agents = repository("no-agents")
    codex_only = repository("codex-only")
    claude_selected = repository("claude-selected")

    initialize_project(no_agents, "NoAgents", agents=())
    initialize_project(codex_only, "CodexOnly", agents=("codex",))
    with pytest.raises(ModelError, match="symlink"):
        initialize_project(claude_selected, "ClaudeSelected", agents=("claude",))

    assert (no_agents / ".ruleloom/config.json").is_file()
    assert (codex_only / ".agents/skills/ruleloom/SKILL.md").is_file()
    assert list(outside.iterdir()) == []


def test_agent_sync_check_mode_and_unmanaged_overwrite_guard(tmp_path: Path) -> None:
    root = tmp_path / "example_project"
    _git_init(root)
    initialized = initialize_project(root, "ExampleProject", agents=("codex",))
    skill = root / ".agents/skills/ruleloom/SKILL.md"
    original = skill.read_text(encoding="utf-8")
    drifted = original + "\n<!-- local drift -->\n"
    skill.write_text(drifted, encoding="utf-8")

    results = sync_agents(root, initialized.config, agents=("codex",), check=True)

    assert any(result.changed for result in results)
    assert skill.read_text(encoding="utf-8") == drifted

    skill.write_text("# Team-owned skill\n", encoding="utf-8")
    with pytest.raises(ModelError, match="unmanaged agent file"):
        sync_agents(root, initialized.config, agents=("codex",))


def test_agent_sync_never_materializes_shadow_policies(tmp_path: Path) -> None:
    root = tmp_path / "example_project"
    _git_init(root)
    initialized = initialize_project(root, "ExampleProject", agents=("codex",))
    shadow = _candidate(status="shadow", config=initialized.config)
    save_candidate(shadow_path(root, initialized.config, shadow.id), shadow)

    sync_agents(root, initialized.config, agents=("codex",))

    skill = (root / ".agents/skills/ruleloom/SKILL.md").read_text(encoding="utf-8")
    rule_cards = (root / ".agents/skills/ruleloom/references/approved-rules.md").read_text(
        encoding="utf-8"
    )
    assert "Shadow policies are never synchronized into this skill" in skill
    assert shadow.id not in skill
    assert shadow.id not in rule_cards
    assert "No rule is approved" in rule_cards


def test_agent_sync_refuses_nested_reference_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "example_project"
    outside = tmp_path / "outside"
    _git_init(root)
    initialized = initialize_project(root, "ExampleProject")
    skill_root = root / ".agents/skills/ruleloom"
    skill_root.mkdir(parents=True)
    outside.mkdir()
    (skill_root / "references").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ModelError, match="symlink"):
        sync_agents(root, initialized.config, agents=("codex",))

    assert list(outside.iterdir()) == []


def test_promote_candidate_records_human_review_and_syncs_approved_rules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "example_project"
    _git_init(root)
    initialized = initialize_project(root, "ExampleProject")
    config = replace(
        initialized.config,
        learner=LearnerConfig(
            max_body=1,
            max_rules=1,
            min_precision=1,
            min_support=1,
            bootstrap_runs=3,
            max_predicates=4,
        ),
        evaluation=EvaluationConfig(
            test_fraction=0.25,
            min_train_examples=4,
            min_test_examples=2,
            seed=7,
        ),
        promotion=PromotionConfig(
            min_positive_for_shadow=1,
            min_positive_for_approval=1,
            min_test_precision=0,
            min_test_recall=0,
            min_stability=0,
            require_baseline_improvement=False,
            min_shadow_predictions_for_approval=2,
            min_shadow_mature_outcomes_for_approval=2,
            min_shadow_days_for_approval=0,
            min_shadow_precision=0,
            min_shadow_recall=0,
            min_shadow_mcc=-1,
            min_shadow_positive_outcomes_for_approval=1,
            min_shadow_negative_outcomes_for_approval=1,
            min_shadow_matches_per_rule_for_approval=1,
        ),
    )
    history = [
        _observation(
            index,
            LabelValue.POSITIVE if index % 2 == 0 else LabelValue.NEGATIVE,
            {"large_change"} if index % 2 == 0 else {"touches_test"},
            config=config,
        )
        for index in range(8)
    ]
    transition_time = datetime(2026, 8, 1, 12, tzinfo=UTC)
    clock = {"now": transition_time}

    def fake_as_of(value: datetime | None = None) -> datetime:
        return value or clock["now"]

    monkeypatch.setattr(lifecycle_module, "_as_of", fake_as_of)
    save_observations(dataset_path(root, config), history)
    candidate = learn_candidate(history, config, as_of=transition_time)
    save_candidate(candidate_path(root, config, candidate.id), candidate)

    shadowed, shadow_decision, _ = promote_candidate(
        root,
        config,
        candidate.id,
        destination="shadow",
        reviewer="Test Reviewer",
        note="Reviewed for shadow evaluation",
    )
    assert shadow_decision.allowed
    assert shadowed.status == "shadow"

    prediction_time = transition_time + timedelta(hours=1)
    positive_snapshot = replace(
        _observation(
            100,
            LabelValue.UNKNOWN,
            {"large_change"},
            kind="git_worktree",
            config=config,
        ),
        observed_at=prediction_time.isoformat(),
    )
    negative_snapshot = replace(
        _observation(
            101,
            LabelValue.UNKNOWN,
            {"touches_test"},
            kind="git_worktree",
            config=config,
        ),
        observed_at=prediction_time.isoformat(),
    )
    iteration_time = prediction_time + timedelta(days=8)
    positive_iteration = replace(
        _observation(
            102,
            LabelValue.UNKNOWN,
            {"large_change"},
            kind="git_worktree",
            config=config,
        ),
        observed_at=iteration_time.isoformat(),
        source={**positive_snapshot.source},
    )
    for snapshot, predicted_at in (
        (positive_snapshot, prediction_time),
        (negative_snapshot, prediction_time),
        (positive_iteration, iteration_time),
    ):
        prediction = make_prediction(
            snapshot,
            [shadowed],
            config,
            predicted_at=predicted_at,
        )
        append_prediction(
            predictions_path(root, config),
            prediction,
            root=root,
            recorded_at=predicted_at,
        )
    outcome_time = iteration_time + timedelta(hours=1)
    positive_outcome = positive_iteration.with_label(
        TARGET,
        LabelValue.POSITIVE,
        LabelEvidence("synthetic", outcome_time.isoformat(), "tests"),
    )
    negative_outcome = negative_snapshot.with_label(
        TARGET,
        LabelValue.NEGATIVE,
        LabelEvidence("synthetic", outcome_time.isoformat(), "tests"),
    )
    save_observations(
        dataset_path(root, config),
        [*history, positive_snapshot, negative_outcome, positive_outcome],
    )
    clock["now"] = outcome_time + timedelta(hours=1)

    promoted, decision, path = promote_candidate(
        root,
        config,
        candidate.id,
        destination="approved",
        reviewer="Test Reviewer",
        note="Reviewed against repository examples",
    )

    assert decision.allowed
    assert promoted.status == "approved"
    assert promoted.review["reviewer"] == "Test Reviewer"
    assert promoted.review["override"] is False
    assert promoted.review["shadow_evidence"]["predictions"] == 3
    assert promoted.review["shadow_evidence"]["unique_observations"] == 2
    assert promoted.review["shadow_evidence"]["elapsed_days"] == 0.0
    assert load_candidate(path) == promoted

    sync_agents(root, config)
    rule_cards_path = root / ".agents/skills/ruleloom/references/approved-rules.md"
    rule_cards = rule_cards_path.read_text(encoding="utf-8")
    assert candidate.id in rule_cards
    assert candidate.rules.clauses[0].to_prolog() in rule_cards

    deprecated, tombstone = deprecate_candidate(
        root,
        config,
        candidate.id,
        reviewer="Test Reviewer",
        note="Superseded after pilot review",
    )
    assert deprecated.status == "deprecated"
    assert load_candidate(tombstone) == deprecated
    deprecation_attestation = read_json(
        trusted_state_path(root)
        / "transition-records"
        / config.hash
        / "deprecated"
        / f"{candidate.id}.json"
    )
    assert deprecation_attestation["trusted_by"] == "Test Reviewer"
    assert deprecation_attestation["note"] == "Superseded after pilot review"
    assert deprecation_attestation["trusted_at"] == deprecated.review["deprecation"]["reviewed_at"]
    assert load_approved(root, config) == []
    sync_agents(root, config, agents=("codex",))
    assert candidate.id not in rule_cards_path.read_text(encoding="utf-8")
    with pytest.raises(ModelError, match="already deprecated"):
        deprecate_candidate(
            root,
            config,
            candidate.id,
            reviewer="Test Reviewer",
            note="duplicate",
        )


def test_failed_promotion_requires_explicit_override_with_note(tmp_path: Path) -> None:
    root = tmp_path / "example_project"
    _git_init(root)
    initialized = initialize_project(root, "ExampleProject")
    config = replace(
        initialized.config,
        learner=LearnerConfig(
            max_body=1,
            max_rules=1,
            min_precision=1,
            min_support=1,
            bootstrap_runs=2,
            max_predicates=4,
        ),
        evaluation=EvaluationConfig(
            test_fraction=0.25,
            min_train_examples=4,
            min_test_examples=2,
            seed=7,
        ),
    )
    observations = [
        _observation(
            index,
            LabelValue.POSITIVE if index % 2 == 0 else LabelValue.NEGATIVE,
            {"large_change"} if index % 2 == 0 else {"touches_test"},
            config=config,
        )
        for index in range(8)
    ]
    save_observations(dataset_path(root, config), observations)
    candidate = learn_candidate(observations, config)
    save_candidate(candidate_path(root, config, candidate.id), candidate)

    with pytest.raises(ModelError, match="promotion gates failed"):
        promote_candidate(
            root,
            config,
            candidate.id,
            destination="shadow",
            reviewer="Test Reviewer",
            note="not enough data",
        )
    with pytest.raises(ModelError, match="override requires"):
        promote_candidate(
            root,
            config,
            candidate.id,
            destination="shadow",
            reviewer="Test Reviewer",
            note="",
            override=True,
        )

    shadowed, decision, _ = promote_candidate(
        root,
        config,
        candidate.id,
        destination="shadow",
        reviewer="Test Reviewer",
        note="Time-boxed experiment; do not enforce",
        override=True,
    )
    assert not decision.allowed
    assert shadowed.review["override"] is True
    assert shadowed.review["unmet_gates"]
    with pytest.raises(ModelError, match="non-overridable"):
        promote_candidate(
            root,
            config,
            candidate.id,
            destination="approved",
            reviewer="Test Reviewer",
            note="cannot bypass prospective evidence",
            override=True,
        )


def test_predictions_are_selective_and_include_matching_rule_identity() -> None:
    config = _promotion_config()
    candidate = _candidate(status="approved", config=config)
    matching = _observation(100, LabelValue.UNKNOWN, {"large_change"}, kind="git_worktree")
    unrelated = _observation(101, LabelValue.UNKNOWN, {"touches_test"}, kind="git_worktree")

    matches = match_rules(matching.facts, [candidate])
    prediction = make_prediction(matching, [candidate], config)
    abstention = make_prediction(unrelated, [candidate], config)

    assert len(matches) == 1
    assert matches[0].candidate_id == candidate.id
    assert matches[0].clause.signature == f"{TARGET}:-large_change"
    assert not prediction.abstained
    assert prediction.matches[0]["candidate_id"] == candidate.id
    assert prediction.matches[0]["status"] == "approved"
    assert abstention.abstained
    assert abstention.matches == ()
    assert prediction.policy_set_hash == abstention.policy_set_hash


def test_learning_fails_closed_on_unknown_units_repositories_and_excessive_work() -> None:
    config = replace(
        _promotion_config(),
        learner=LearnerConfig(
            max_body=1,
            max_rules=1,
            min_support=1,
            bootstrap_runs=1,
            max_predicates=4,
        ),
        evaluation=EvaluationConfig(
            test_fraction=0.25,
            min_train_examples=4,
            min_test_examples=2,
        ),
    )
    observations = [
        _observation(
            index,
            LabelValue.POSITIVE if index % 2 == 0 else LabelValue.NEGATIVE,
            {"large_change"} if index % 2 == 0 else {"touches_test"},
        )
        for index in range(8)
    ]
    with pytest.raises(ModelError, match="supported observation unit"):
        learn_candidate(
            [replace(observations[0], source={}), *observations[1:]],
            config,
        )
    with pytest.raises(ModelError, match="git_commit units only"):
        learn_candidate(
            [
                replace(
                    item,
                    source={**item.source, "kind": "git_range"},
                )
                for item in observations
            ],
            config,
        )
    with pytest.raises(ModelError, match="configured repository"):
        learn_candidate(
            [
                replace(
                    item,
                    source={**item.source, "repository": "repository.other"},
                )
                for item in observations
            ],
            config,
        )

    large = [
        _observation(
            index,
            LabelValue.POSITIVE if index % 2 == 0 else LabelValue.NEGATIVE,
            set(get_pack("flutter_testing", 1).predicates[:12]),
        )
        for index in range(400)
    ]
    with pytest.raises(ModelError, match="safe budget"):
        learn_candidate(
            large,
            RuleLoomConfig(project="ExampleProject"),
            as_of=datetime(2028, 1, 1, tzinfo=UTC),
        )


def test_evidence_cannot_be_reinterpreted_under_a_changed_protocol() -> None:
    config = RuleLoomConfig(project="ExampleProject")
    observation = _observation(1, LabelValue.UNKNOWN)
    changed = replace(
        config,
        protocol=replace(
            config.protocol,
            experiment_id="ruleloom-pilot-v2",
            outcome_definition="A different outcome contract.",
        ),
    )

    assert observation.protocol_hash == config.evidence_protocol_hash
    with pytest.raises(ModelError, match="different evidence protocol"):
        validate_observations([observation], changed)
    with pytest.raises(ModelError, match="different evidence protocol"):
        learn_candidate([observation], changed)


def test_learning_reports_zero_mature_labels_before_unit_diagnostics() -> None:
    config = RuleLoomConfig(project="ExampleProject")
    observation = _observation(1, LabelValue.UNKNOWN, config=config)

    with pytest.raises(ModelError, match="no mature labels are available for learning"):
        learn_candidate([observation], config)


def test_configured_paths_contract_survives_learning_candidate_and_prediction() -> None:
    config = RuleLoomConfig(
        schema_version=3,
        project="ConfiguredLifecycle",
        pack="configured_paths",
        pack_version=1,
        pack_config=ConfiguredPathsConfig(
            (PathPredicateConfig("touches_shared_contract", ("packages/shared/**",)),)
        ),
        learner=LearnerConfig(
            max_body=1,
            max_rules=1,
            min_precision=1,
            min_support=2,
            bootstrap_runs=0,
            max_predicates=4,
        ),
        evaluation=EvaluationConfig(
            test_fraction=0.25,
            min_train_examples=6,
            min_test_examples=2,
        ),
    )
    observations = [
        _observation(
            index,
            LabelValue.POSITIVE if index % 2 == 0 else LabelValue.NEGATIVE,
            {"touches_shared_contract"} if index % 2 == 0 else set(),
            config=config,
        )
        for index in range(12)
    ]

    candidate = learn_candidate(
        observations,
        config,
        as_of=datetime(2027, 1, 1, tzinfo=UTC),
    )

    assert candidate.metadata["pack_config_hash"] == config.pack_config_hash
    assert candidate.rules.clauses
    reviewed = replace(
        candidate,
        status="approved",
        review={
            "reviewer": "Test Reviewer",
            "reviewed_at": "2027-01-01T01:00:00Z",
            "note": "configured pack reviewed",
            "override": False,
            "unmet_gates": [],
        },
    ).with_identity()
    prospective = _observation(
        100,
        LabelValue.UNKNOWN,
        {"touches_shared_contract"},
        kind="git_worktree",
        config=config,
    )
    prediction = make_prediction(
        prospective,
        [reviewed],
        config,
        predicted_at=datetime(2027, 1, 1, tzinfo=UTC),
    )

    assert prediction.abstained is False
    assert prediction.matches

    missing_configuration = replace(
        reviewed,
        metadata={
            key: value for key, value in reviewed.metadata.items() if key != "pack_config_hash"
        },
    ).with_identity()
    with pytest.raises(ModelError, match="pack-configuration provenance"):
        make_prediction(prospective, [missing_configuration], config)


def test_configured_paths_rejects_tampered_extraction_metadata() -> None:
    config = RuleLoomConfig(
        schema_version=3,
        project="ConfiguredMetadata",
        pack="configured_paths",
        pack_version=1,
        pack_config=ConfiguredPathsConfig(
            (PathPredicateConfig("touches_shared_contract", ("packages/shared/**",)),)
        ),
    )
    observation = _observation(
        1,
        LabelValue.POSITIVE,
        {"touches_shared_contract"},
        config=config,
    )

    for metadata in ({}, {"configured_paths_config_hash": "0" * 64}):
        tampered = replace(observation, metadata=metadata)
        with pytest.raises(ModelError, match="inconsistent configured-path metadata"):
            validate_observations([tampered], config)
        with pytest.raises(ModelError, match="inconsistent configured-path metadata"):
            learn_candidate([tampered], config, as_of=datetime(2027, 1, 1, tzinfo=UTC))


@pytest.mark.parametrize(
    ("tamper", "message"),
    [
        ("pack_version", "fact pack version"),
        ("pack_config", "different pack configuration"),
        ("metadata", "inconsistent configured-path metadata"),
    ],
)
def test_shadow_outcomes_must_preserve_configured_pack_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
    message: str,
) -> None:
    config = replace(
        _promotion_config(),
        schema_version=3,
        pack="configured_paths",
        pack_version=1,
        pack_config=ConfiguredPathsConfig(
            (PathPredicateConfig("touches_shared_contract", ("packages/shared/**",)),)
        ),
    )
    shadow = replace(
        _candidate(status="shadow", config=config),
        review={
            "reviewer": "Test Reviewer",
            "reviewed_at": "2026-08-31T13:00:00Z",
            "note": "shadow test",
            "override": False,
            "unmet_gates": [],
        },
    ).with_identity()
    snapshot = _observation(
        100,
        LabelValue.UNKNOWN,
        {"large_change"},
        kind="git_worktree",
        config=config,
    )
    predicted_at = datetime(2026, 9, 1, tzinfo=UTC)
    prediction = make_prediction(snapshot, [shadow], config, predicted_at=predicted_at)
    outcome = snapshot.with_label(
        TARGET,
        LabelValue.POSITIVE,
        LabelEvidence(
            "synthetic",
            "2026-09-02T00:00:00Z",
            "tests",
        ),
    )
    if tamper == "pack_version":
        outcome = replace(outcome, source={**outcome.source, "pack_version": True})
    elif tamper == "pack_config":
        outcome = replace(outcome, source={**outcome.source, "pack_config_hash": "0" * 64})
    else:
        outcome = replace(
            outcome,
            metadata={"configured_paths_config_hash": "0" * 64},
        )
    monkeypatch.setattr(lifecycle_module, "load_trusted_predictions", lambda *_args: [prediction])
    monkeypatch.setattr(
        lifecycle_module,
        "load_observations",
        lambda *_args: [snapshot, outcome],
    )

    with pytest.raises(ModelError, match=message):
        lifecycle_module.shadow_evidence(
            tmp_path,
            config,
            shadow,
            as_of=datetime(2026, 9, 3, tzinfo=UTC),
        )


def test_learning_rejects_unknown_observation_from_another_repository() -> None:
    config = RuleLoomConfig(project="ExampleProject")
    observation = _observation(1, LabelValue.UNKNOWN, config=config)
    foreign = replace(
        observation,
        source={**observation.source, "repository": "repository.other"},
    )

    with pytest.raises(ModelError, match="different configured repository"):
        learn_candidate([foreign], config)


def test_prediction_builder_enforces_protocol_and_candidate_compatibility() -> None:
    config = _promotion_config()
    candidate = _candidate(status="approved", config=config)
    observation = _observation(100, LabelValue.UNKNOWN, {"large_change"}, kind="git_worktree")
    with pytest.raises(ModelError, match="duplicate candidate ids"):
        make_prediction(observation, [candidate, candidate], config)
    with pytest.raises(ModelError, match=r"stable source\.change_id"):
        make_prediction(
            replace(
                observation,
                source={
                    key: value for key, value in observation.source.items() if key != "change_id"
                },
            ),
            [candidate],
            config,
        )
    with pytest.raises(ModelError, match="prospective unit"):
        make_prediction(
            replace(observation, source={**observation.source, "kind": "git_commit"}),
            [candidate],
            config,
        )
    with pytest.raises(ModelError, match="configured repository"):
        make_prediction(
            replace(
                observation,
                source={**observation.source, "repository": "repository.other"},
            ),
            [candidate],
            config,
        )
    with pytest.raises(ModelError, match="configured pack"):
        make_prediction(
            replace(observation, source={**observation.source, "pack": "other"}),
            [candidate],
            config,
        )
    with pytest.raises(ModelError, match="configured prediction target"):
        make_prediction(
            replace(observation, labels={"other_target": LabelValue.UNKNOWN}),
            [candidate],
            config,
        )
    with pytest.raises(ModelError, match="extractor provenance"):
        make_prediction(
            replace(observation, source={**observation.source, "extractor": ""}),
            [candidate],
            config,
        )

    other_target = replace(
        candidate,
        rules=RuleSet(
            "other_target",
            (HornClause("other_target", (RuleLiteral("large_change"),)),),
        ),
    ).with_identity()
    with pytest.raises(ModelError, match="multiple prediction targets"):
        make_prediction(observation, [candidate, other_target], config)
    with pytest.raises(ModelError, match="does not match configured prediction target"):
        make_prediction(observation, [other_target], config)
    with pytest.raises(ModelError, match="not an active reviewed policy"):
        make_prediction(
            observation, [replace(candidate, status="candidate").with_identity()], config
        )

    different_config_candidate = _candidate(
        status="approved",
        config=replace(config, project="Other"),
    )
    with pytest.raises(ModelError, match="current configuration"):
        make_prediction(observation, [different_config_candidate], config)
    wrong_repository = replace(
        candidate,
        metadata={**candidate.metadata, "repository_id": "repository.other"},
    ).with_identity()
    with pytest.raises(ModelError, match="different repository"):
        make_prediction(observation, [wrong_repository], config)
    wrong_pack = replace(
        candidate,
        metadata={**candidate.metadata, "pack": "other"},
    ).with_identity()
    with pytest.raises(ModelError, match="incompatible"):
        make_prediction(observation, [wrong_pack], config)
    bad_extractors = replace(
        candidate,
        metadata={**candidate.metadata, "extractors": "bad"},
    ).with_identity()
    with pytest.raises(ModelError, match="invalid extractor provenance"):
        make_prediction(observation, [bad_extractors], config)
    incompatible_extractor = replace(
        candidate,
        metadata={**candidate.metadata, "extractors": ["other"]},
    ).with_identity()
    with pytest.raises(ModelError, match="extractor provenance is incompatible"):
        make_prediction(observation, [incompatible_extractor], config)


def test_approval_uses_wilson_lower_bounds_not_small_sample_point_estimates() -> None:
    config = RuleLoomConfig(
        project="ExampleProject",
        promotion=PromotionConfig(
            min_test_precision=0,
            min_test_recall=0,
            min_stability=0,
            min_positive_for_shadow=1,
            min_positive_for_approval=1,
            require_baseline_improvement=False,
            min_shadow_predictions_for_approval=10,
            min_shadow_mature_outcomes_for_approval=10,
            min_shadow_days_for_approval=0,
            min_shadow_precision=0.7,
            min_shadow_recall=0.5,
            min_shadow_mcc=-1,
            min_shadow_positive_outcomes_for_approval=5,
            min_shadow_negative_outcomes_for_approval=5,
            min_shadow_matches_per_rule_for_approval=5,
        ),
    )
    candidate = _candidate(config=config)
    perfect_but_small = Metrics.from_counts(5, 0, 5, 0)
    evidence = ShadowEvidence(
        predictions=10,
        unique_observations=10,
        mature_outcomes=10,
        elapsed_days=1,
        metrics=perfect_but_small,
        rule_metrics={signature: perfect_but_small for signature in candidate.rules.signatures},
        manifest_hash="a" * 64,
    )

    decision = promotion_decision(
        candidate,
        config,
        "approved",
        prospective_shadow=evidence,
    )

    assert not decision.allowed
    assert any("Wilson lower bound" in reason for reason in decision.blocking)


def test_explicit_clone_trust_binds_a_reviewed_artifact_locally(tmp_path: Path) -> None:
    root = tmp_path / "clone"
    _git_init(root)
    initialized = initialize_project(root, "Clone")
    config = replace(
        _promotion_config(),
        protocol=initialized.config.protocol,
    )
    source = _candidate(config=config)
    reviewed = replace(
        source,
        status="shadow",
        review={
            "reviewer": "upstream-reviewer",
            "reviewed_at": "2026-08-31T12:00:00Z",
            "note": "reviewed upstream",
            "override": False,
            "unmet_gates": [],
        },
    )
    save_candidate(shadow_path(root, config, reviewed.id), reviewed)

    with pytest.raises(ModelError, match="non-empty reviewer and note"):
        trust_reviewed_artifact(
            root,
            config,
            reviewed.id,
            status="shadow",
            reviewer="",
            note="",
        )
    trusted, attestation = trust_reviewed_artifact(
        root,
        config,
        reviewed.id,
        status="shadow",
        reviewer="clone-reviewer",
        note="inspected in this clone",
    )

    assert trusted == reviewed
    assert attestation.is_file()

    empty_root = tmp_path / "empty"
    _git_init(empty_root)
    initialized = initialize_project(empty_root, "Empty")
    with pytest.raises(ModelError, match="no active reviewed candidate"):
        deprecate_candidate(
            empty_root,
            initialized.config,
            "cand-0123456789abcdef",
            reviewer="reviewer",
            note="nothing to deprecate",
        )


def test_clone_trust_rejects_policy_outside_the_pack_contract(tmp_path: Path) -> None:
    root = tmp_path / "pack-contract"
    _git_init(root)
    config = initialize_project(root, "PackContract").config
    review = {
        "reviewer": "upstream-reviewer",
        "reviewed_at": "2026-08-31T12:00:00Z",
        "note": "reviewed upstream",
        "override": False,
        "unmet_gates": [],
    }
    baseline = replace(_candidate(config=config), status="shadow", review=review)
    invalid = (
        (
            replace(
                baseline,
                metadata={**baseline.metadata, "extractors": ["untrusted.extractor.v1"]},
            ).with_identity(),
            "extractor provenance",
        ),
        (
            replace(
                baseline,
                metadata={**baseline.metadata, "pack_version": True},
            ).with_identity(),
            "pack-version provenance",
        ),
        (
            replace(
                baseline,
                rules=RuleSet(
                    TARGET,
                    (HornClause(TARGET, (RuleLiteral("undeclared_predicate"),)),),
                ),
            ).with_identity(),
            "predicates not declared",
        ),
    )

    for candidate, message in invalid:
        save_candidate(shadow_path(root, config, candidate.id), candidate)
        with pytest.raises(ModelError, match=message):
            trust_reviewed_artifact(
                root,
                config,
                candidate.id,
                status="shadow",
                reviewer="clone-reviewer",
                note="inspected in this clone",
            )


def test_transition_trust_is_namespaced_by_configuration(tmp_path: Path) -> None:
    root = tmp_path / "clone"
    _git_init(root)
    initialized = initialize_project(root, "Clone")
    first_config = replace(_promotion_config(), protocol=initialized.config.protocol)
    second_config = replace(
        first_config,
        shadow_dir=".ruleloom/shadow-v2",
        protocol=replace(
            first_config.protocol,
            experiment_id="ruleloom-pilot-v2",
            outcome_definition="A new experiment outcome.",
        ),
    )
    review = {
        "reviewer": "Test Reviewer",
        "reviewed_at": "2026-08-31T12:00:00Z",
        "note": "reviewed",
        "override": False,
        "unmet_gates": [],
    }
    first = replace(_candidate(status="shadow", config=first_config), review=review)
    second = replace(_candidate(status="shadow", config=second_config), review=review)
    save_candidate(shadow_path(root, first_config, first.id), first)
    save_candidate(shadow_path(root, second_config, second.id), second)
    record_transition_attestation(root, first)
    record_transition_attestation(root, second)

    assert load_shadow(root, first_config) == [first]
    assert load_shadow(root, second_config) == [second]
    assert first_config.hash != second_config.hash


def test_deprecated_trust_rejects_unsafe_config_hash_namespace(tmp_path: Path) -> None:
    root = tmp_path / "clone"
    _git_init(root)
    initialized = initialize_project(root, "Clone")
    config = replace(_promotion_config(), protocol=initialized.config.protocol)
    source = _candidate(status="shadow", config=config)
    unsafe = replace(
        source,
        id="cand-unsafe",
        status="deprecated",
        review={
            **source.review,
            "deprecation": {
                "reviewer": "Test Reviewer",
                "reviewed_at": "2026-08-31T13:00:00Z",
                "note": "retired",
            },
        },
    ).to_dict()
    unsafe["config_hash"] = "../../../../../../outside"
    write_json(deprecated_path(root, config, "cand-unsafe"), unsafe)
    outside = tmp_path / "outside"

    with pytest.raises(ModelError, match="config_hash must be a lowercase SHA-256 digest"):
        trust_reviewed_artifact(
            root,
            config,
            "cand-unsafe",
            status="deprecated",
            reviewer="Test Reviewer",
            note="reviewed locally",
        )

    assert not outside.exists()
