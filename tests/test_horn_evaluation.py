from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from ruleloom.config import EvaluationConfig, LearnerConfig, RuleLoomConfig
from ruleloom.evaluation import (
    best_literal_baseline,
    bootstrap_stability,
    evaluate,
    fit_boolean_logistic_baseline,
    majority_baseline,
    temporal_split,
)
from ruleloom.learners.horn import (
    HORN_ENGINE_VERSION,
    HornBudget,
    HornSettings,
    learn_horn,
    learn_horn_diagnostics,
    rank_predicates,
    select_train_predicates,
)
from ruleloom.lifecycle import learn_candidate
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

TARGET = "needs_extra_validation"
LEGACY_CONFIG = RuleLoomConfig(
    schema_version=1,
    project="Tests",
    pack="flutter_testing",
    pack_version=1,
)
PROTOCOL_HASH = LEGACY_CONFIG.evidence_protocol_hash


def _observation(
    item_id: str,
    facts: set[str],
    label: LabelValue,
    *,
    observed_at: str,
    available_at: str | None = None,
) -> Observation:
    default_available = (
        datetime.fromisoformat(observed_at.replace("Z", "+00:00")) + timedelta(minutes=1)
    ).isoformat()
    evidence = (
        {}
        if label is LabelValue.UNKNOWN
        else {
            TARGET: LabelEvidence(
                kind="synthetic",
                available_at=available_at or default_available,
                source="tests",
            )
        }
    )
    return Observation(
        id=item_id,
        observed_at=observed_at,
        protocol_hash=PROTOCOL_HASH,
        facts=frozenset(facts),
        labels={TARGET: label},
        label_evidence=evidence,
        fact_evidence={
            fact: FactEvidence(
                kind="deterministic",
                extractor="ruleloom.flutter_testing.git.v1",
                evidence=(f"synthetic:{fact}",),
            )
            for fact in facts
        },
        source={
            "kind": "git_commit",
            "repository": "repository.unspecified",
            "pack": "flutter_testing",
            "extractor": "ruleloom.flutter_testing.git.v1",
        },
    )


def _dated_observation(
    index: int,
    facts: set[str],
    label: LabelValue,
    *,
    available_day: int | None = None,
) -> Observation:
    observed = datetime(2026, 1, index, 9, tzinfo=UTC)
    available = datetime(2026, 1, available_day or index, 10, tzinfo=UTC)
    return _observation(
        f"example-{index}",
        facts,
        label,
        observed_at=observed.isoformat(),
        available_at=available.isoformat(),
    )


def test_horn_learner_finds_a_precise_conjunction_with_negation() -> None:
    examples = [
        _dated_observation(1, {"uses_async"}, LabelValue.POSITIVE),
        _dated_observation(2, {"uses_async"}, LabelValue.POSITIVE),
        _dated_observation(
            3,
            {"uses_async", "adds_widget_test"},
            LabelValue.NEGATIVE,
        ),
        _dated_observation(4, set(), LabelValue.NEGATIVE),
        _observation(
            "unknown",
            {"uses_async"},
            LabelValue.UNKNOWN,
            observed_at="2026-01-05T09:00:00+00:00",
        ),
    ]

    rules = learn_horn(
        examples,
        TARGET,
        HornSettings(
            max_body=2,
            max_rules=1,
            allow_negation=True,
            min_precision=1.0,
            min_support=2,
        ),
    )

    assert len(rules.clauses) == 1
    assert {literal.name for literal in rules.clauses[0].body} == {
        "uses_async",
        "not_adds_widget_test",
    }
    assert rules.predicts(frozenset({"uses_async"}))
    assert not rules.predicts(frozenset({"uses_async", "adds_widget_test"}))


def test_relative_lift_gate_accepts_precise_niche_guardrail() -> None:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    examples = [
        _observation(
            f"lift-{index}",
            {"uses_async"} if index < 10 else set(),
            LabelValue.POSITIVE if index < 10 else LabelValue.NEGATIVE,
            observed_at=(start + timedelta(days=index)).isoformat(),
        )
        for index in range(100)
    ]

    result = learn_horn_diagnostics(
        examples,
        TARGET,
        HornSettings(
            max_body=1,
            max_rules=1,
            allow_negation=False,
            min_support=2,
            max_predicates=1,
            gate_mode="relative_lift",
            min_lift_lower_bound=3.0,
            min_alert_rate=0.01,
        ),
    )

    assert len(result.rules.clauses) == 1
    assert result.rules.clauses[0].signature == "needs_extra_validation:-uses_async"


def test_near_misses_report_train_only_rejection_and_search_size() -> None:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    examples = [
        _observation(
            f"near-{index}",
            {"uses_async"} if index < 20 else set(),
            LabelValue.POSITIVE if index < 10 else LabelValue.NEGATIVE,
            observed_at=(start + timedelta(days=index)).isoformat(),
        )
        for index in range(100)
    ]

    result = learn_horn_diagnostics(
        examples,
        TARGET,
        HornSettings(
            max_body=1,
            max_rules=1,
            allow_negation=False,
            min_support=2,
            max_predicates=1,
            gate_mode="relative_lift",
            min_lift_lower_bound=3.0,
            near_miss_limit=3,
        ),
    )

    assert not result.rules.clauses
    assert result.hypotheses_examined == 1
    assert len(result.near_misses) == 1
    diagnostic = result.near_misses[0].to_dict()
    assert diagnostic["selection_scope"] == "train_only_exploratory"
    assert diagnostic["post_selection_inference"] is False
    assert "conservative_lift_below_minimum" in diagnostic["rejection_reasons"]


def test_predicate_ranking_resists_negative_class_imbalance() -> None:
    examples = [
        *(
            _dated_observation(index, {"always", "risk"}, LabelValue.POSITIVE)
            for index in range(1, 3)
        ),
        *(_dated_observation(index, {"always"}, LabelValue.NEGATIVE) for index in range(3, 11)),
    ]

    assert rank_predicates(examples, TARGET) == ["risk"]

    rules = learn_horn(
        examples,
        TARGET,
        HornSettings(
            max_body=1,
            max_rules=1,
            allow_negation=False,
            min_precision=1.0,
            min_support=2,
            max_predicates=1,
        ),
    )

    assert rules.clauses == (HornClause(TARGET, (RuleLiteral("risk"),)),)


def test_predicate_ranking_keeps_discriminative_negation_under_imbalance() -> None:
    examples = [
        *(_dated_observation(index, {"always"}, LabelValue.POSITIVE) for index in range(1, 9)),
        *(
            _dated_observation(index, {"always", "safe"}, LabelValue.NEGATIVE)
            for index in range(9, 11)
        ),
    ]

    assert rank_predicates(examples, TARGET) == ["safe"]

    rules = learn_horn(
        examples,
        TARGET,
        HornSettings(
            max_body=1,
            max_rules=1,
            allow_negation=True,
            min_precision=1.0,
            min_support=8,
            max_predicates=1,
        ),
    )

    assert rules.clauses == (HornClause(TARGET, (RuleLiteral("safe", negated=True),)),)


def test_predicate_ranking_without_negation_prefers_positive_direction() -> None:
    examples = [
        _dated_observation(1, {"z_risk"}, LabelValue.POSITIVE),
        _dated_observation(2, {"z_risk"}, LabelValue.POSITIVE),
        _dated_observation(3, {"a_safe"}, LabelValue.NEGATIVE),
        _dated_observation(4, {"a_safe"}, LabelValue.NEGATIVE),
    ]

    assert rank_predicates(examples, TARGET) == ["a_safe", "z_risk"]
    assert rank_predicates(examples, TARGET, allow_negation=False) == [
        "z_risk",
        "a_safe",
    ]
    assert learn_horn(
        examples,
        TARGET,
        HornSettings(
            max_body=1,
            max_rules=1,
            allow_negation=False,
            min_precision=1.0,
            min_support=2,
            max_predicates=1,
        ),
    ).clauses == (HornClause(TARGET, (RuleLiteral("z_risk"),)),)


def test_predicate_ranking_abstains_when_either_class_is_absent() -> None:
    examples = [
        _dated_observation(1, {"always", "sometimes"}, LabelValue.POSITIVE),
        _dated_observation(2, {"always"}, LabelValue.POSITIVE),
    ]

    assert rank_predicates(examples, TARGET) == []
    assert (
        learn_horn(
            examples,
            TARGET,
            HornSettings(min_support=1, max_predicates=1),
        ).clauses
        == ()
    )


def test_predicate_ranking_drops_constants_and_breaks_rate_ties_lexically() -> None:
    examples = [
        _dated_observation(1, {"always", "alpha"}, LabelValue.POSITIVE),
        _dated_observation(2, {"always", "zeta"}, LabelValue.POSITIVE),
        _dated_observation(3, {"always", "alpha"}, LabelValue.NEGATIVE),
        _dated_observation(4, {"always", "zeta"}, LabelValue.NEGATIVE),
    ]

    assert rank_predicates(examples, TARGET) == ["alpha", "zeta"]


def test_predicate_selection_collapses_duplicate_training_columns_lexically() -> None:
    examples = [
        _dated_observation(1, {"always", "alpha", "alpha_alias"}, LabelValue.POSITIVE),
        _dated_observation(2, {"always", "alpha", "alpha_alias"}, LabelValue.POSITIVE),
        _dated_observation(3, {"always"}, LabelValue.NEGATIVE),
        _dated_observation(4, {"always"}, LabelValue.NEGATIVE),
        _observation(
            "unknown-future",
            {"always", "alpha_alias"},
            LabelValue.UNKNOWN,
            observed_at="2026-01-05T09:00:00+00:00",
        ),
    ]

    selection = select_train_predicates(examples, TARGET)

    assert selection.constant_predicates == ("always",)
    assert selection.duplicate_groups == (("alpha", "alpha_alias"),)
    assert selection.duplicate_predicates == ("alpha_alias",)
    assert selection.ranked_predicates == ("alpha",)
    assert rank_predicates(examples, TARGET) == ["alpha"]
    assert learn_horn(
        examples,
        TARGET,
        HornSettings(
            max_body=1,
            max_rules=1,
            allow_negation=False,
            min_precision=1.0,
            min_support=2,
        ),
    ).clauses == (HornClause(TARGET, (RuleLiteral("alpha"),)),)


def test_horn_learner_abstains_when_support_or_precision_is_insufficient() -> None:
    examples = [
        _dated_observation(1, {"risk"}, LabelValue.POSITIVE),
        _dated_observation(2, {"risk"}, LabelValue.NEGATIVE),
    ]

    rules = learn_horn(
        examples,
        TARGET,
        HornSettings(min_support=2, min_precision=1.0),
    )

    assert rules.clauses == ()


def test_horn_hard_budget_covers_multi_rule_search() -> None:
    examples = [
        _dated_observation(
            index,
            {"risk", f"feature_{index % 3}"},
            LabelValue.POSITIVE if index % 2 else LabelValue.NEGATIVE,
        )
        for index in range(1, 9)
    ]

    with pytest.raises(ModelError, match="hard bitset-work budget"):
        learn_horn(
            examples,
            TARGET,
            HornSettings(max_body=2, max_rules=3, min_support=1, max_predicates=4),
            budget=HornBudget(10),
        )


def test_horn_rejects_target_leakage_and_reserved_fact_namespace() -> None:
    leaking = [
        _dated_observation(1, {TARGET}, LabelValue.POSITIVE),
        _dated_observation(2, {"safe"}, LabelValue.NEGATIVE),
    ]
    reserved = [
        _dated_observation(1, {"not_safe"}, LabelValue.POSITIVE),
        _dated_observation(2, {"safe"}, LabelValue.NEGATIVE),
    ]

    with pytest.raises(ModelError, match="leak"):
        learn_horn(leaking, TARGET)
    with pytest.raises(ModelError, match="reserved"):
        learn_horn(reserved, TARGET)


def test_evaluation_counts_and_baselines_are_fitted_only_on_train() -> None:
    train = [
        _dated_observation(1, {"risk"}, LabelValue.POSITIVE),
        _dated_observation(2, {"risk"}, LabelValue.POSITIVE),
        _dated_observation(3, {"safe"}, LabelValue.NEGATIVE),
        _dated_observation(4, {"safe"}, LabelValue.NEGATIVE),
    ]
    test = [
        _dated_observation(5, {"risk"}, LabelValue.POSITIVE),
        _dated_observation(6, {"safe"}, LabelValue.NEGATIVE),
    ]

    metrics = evaluate(test, TARGET, lambda facts: "risk" in facts)
    majority_value, train_majority_metrics = majority_baseline(train, TARGET)
    literal, literal_metrics = best_literal_baseline(train, test, TARGET)

    assert metrics.true_positive == 1
    assert metrics.true_negative == 1
    assert metrics.matthews_correlation == 1.0
    assert majority_value is False
    assert train_majority_metrics.accuracy == 0.5
    assert literal == "risk"
    assert literal_metrics.matthews_correlation == 1.0


def test_temporal_split_uses_instant_order_across_timezone_offsets() -> None:
    earlier_instant = _observation(
        "earlier-instant",
        {"risk"},
        LabelValue.POSITIVE,
        observed_at="2026-01-01T01:00:00+02:00",
        available_at="2026-01-01T01:05:00+02:00",
    )
    later_instant = _observation(
        "later-instant",
        set(),
        LabelValue.NEGATIVE,
        observed_at="2025-12-31T23:30:00+00:00",
        available_at="2025-12-31T23:35:00+00:00",
    )

    split = temporal_split(
        [later_instant, earlier_instant],
        TARGET,
        test_fraction=0.5,
        min_train=1,
        min_test=1,
    )

    assert [item.id for item in split.train] == ["earlier-instant"]
    assert [item.id for item in split.test] == ["later-instant"]


def test_boolean_logistic_baseline_is_deterministic_and_training_only() -> None:
    examples = [
        _dated_observation(
            index,
            {"risk"} if index % 2 else {"safe"},
            LabelValue.POSITIVE if index % 2 else LabelValue.NEGATIVE,
        )
        for index in range(1, 9)
    ]

    first = fit_boolean_logistic_baseline(examples, TARGET, iterations=100)
    second = fit_boolean_logistic_baseline(list(reversed(examples)), TARGET, iterations=100)

    assert first == second
    assert first.predicts(frozenset({"risk"}))
    assert not first.predicts(frozenset({"safe"}))
    assert evaluate(examples, TARGET, first.predicts).matthews_correlation == 1.0


def test_temporal_split_honors_a_fixed_preregistered_boundary() -> None:
    observations = [
        _observation(
            f"item-{day}",
            {"risk"} if day % 2 else set(),
            LabelValue.POSITIVE if day % 2 else LabelValue.NEGATIVE,
            observed_at=f"2025-01-{day:02d}T00:00:00Z",
            available_at=f"2025-01-{day:02d}T01:00:00Z",
        )
        for day in range(1, 7)
    ]

    split = temporal_split(
        observations,
        TARGET,
        test_fraction=0.8,
        min_train=2,
        min_test=2,
        test_start_at="2025-01-05T00:00:00Z",
    )

    assert [item.id for item in split.train] == [f"item-{day}" for day in range(1, 5)]
    assert [item.id for item in split.test] == ["item-5", "item-6"]


def test_temporal_split_prefers_git_topology_over_backdated_commit_time() -> None:
    first = replace(
        _observation(
            "first-topological",
            {"risk"},
            LabelValue.POSITIVE,
            observed_at="2026-01-02T10:00:00Z",
        ),
        source={"repository": "example_project"},
        metadata={"topological_index": 1},
    )
    second = replace(
        _observation(
            "second-topological",
            set(),
            LabelValue.NEGATIVE,
            observed_at="2026-01-01T10:00:00Z",
        ),
        source={"repository": "example_project"},
        metadata={"topological_index": 2},
    )

    split = temporal_split(
        [second, first],
        TARGET,
        test_fraction=0.5,
        min_train=1,
        min_test=1,
    )

    assert [item.id for item in split.train] == ["first-topological"]
    assert [item.id for item in split.test] == ["second-topological"]
    assert any("non-monotonic" in warning for warning in split.warnings)


def test_candidate_excludes_labels_unavailable_at_holdout_start() -> None:
    observations = [
        _dated_observation(1, {"large_change"}, LabelValue.POSITIVE),
        _dated_observation(2, {"touches_test"}, LabelValue.NEGATIVE),
        _dated_observation(
            3,
            {"uses_async"},
            LabelValue.POSITIVE,
            available_day=7,
        ),
        _dated_observation(4, {"touches_test"}, LabelValue.NEGATIVE),
        _dated_observation(5, {"large_change"}, LabelValue.POSITIVE),
        _dated_observation(6, {"touches_test"}, LabelValue.NEGATIVE),
    ]
    config = RuleLoomConfig(
        schema_version=1,
        project="ExampleProject",
        pack="flutter_testing",
        pack_version=1,
        learner=LearnerConfig(
            max_body=1,
            max_rules=1,
            min_precision=1.0,
            min_support=1,
            bootstrap_runs=3,
        ),
        evaluation=EvaluationConfig(
            test_fraction=1 / 3,
            min_train_examples=3,
            min_test_examples=2,
            seed=7,
        ),
    )

    candidate = learn_candidate(observations, config)

    assert candidate.engine_version == HORN_ENGINE_VERSION
    assert candidate.train_ids == ("example-1", "example-2", "example-4")
    assert candidate.test_ids == ("example-5", "example-6")
    assert any("temporal leakage" in warning for warning in candidate.warnings)
    assert candidate.metrics["test"].matthews_correlation == 1.0
    assert set(candidate.baselines) == {
        "never_alert",
        "always_alert",
        "train_majority",
        "best_single_literal",
        "size_only",
        "logistic_regression_boolean_facts",
    }
    assert candidate.metadata["baseline_models"]["size_only"]["training_selected"] is False


def test_candidate_deduplicates_only_on_temporally_eligible_train() -> None:
    observations = [
        _dated_observation(1, {"large_change", "multi_file_change"}, LabelValue.POSITIVE),
        _dated_observation(2, {"large_change", "multi_file_change"}, LabelValue.POSITIVE),
        _dated_observation(3, set(), LabelValue.NEGATIVE),
        _dated_observation(4, set(), LabelValue.NEGATIVE),
        _dated_observation(5, {"large_change", "multi_file_change"}, LabelValue.POSITIVE),
        _dated_observation(6, set(), LabelValue.NEGATIVE),
        # The future holdout distinguishes the aliases. It must not influence
        # preprocessing or representative choice.
        _dated_observation(7, {"multi_file_change"}, LabelValue.POSITIVE),
        _dated_observation(8, set(), LabelValue.NEGATIVE),
    ]
    config = RuleLoomConfig(
        schema_version=1,
        project="ExampleProject",
        pack="flutter_testing",
        pack_version=1,
        learner=LearnerConfig(
            max_body=1,
            max_rules=1,
            min_precision=1.0,
            min_support=2,
            bootstrap_runs=0,
        ),
        evaluation=EvaluationConfig(
            test_fraction=0.25,
            min_train_examples=6,
            min_test_examples=2,
        ),
    )

    candidate = learn_candidate(observations, config)

    assert candidate.rules.clauses == (HornClause(TARGET, (RuleLiteral("large_change"),)),)
    assert candidate.metadata["predicate_selection"] == {
        "scope": "temporally_eligible_train",
        "holdout_consulted": False,
        "labelled_observations": 6,
        "positive_observations": 3,
        "negative_observations": 3,
        "observed_predicate_count": 2,
        "eligible_representative_count": 1,
        "search_predicates": ["large_change"],
        "constant_predicates": [],
        "duplicate_groups": [
            {
                "representative": "large_change",
                "aliases": ["multi_file_change"],
            }
        ],
    }
    assert any("without consulting holdout" in warning for warning in candidate.warnings)
    assert candidate.metrics["test"].false_negative == 1


def test_candidate_without_negation_uses_signed_training_ranking() -> None:
    observations = [
        _dated_observation(1, {"uses_async"}, LabelValue.POSITIVE),
        _dated_observation(2, {"uses_async"}, LabelValue.POSITIVE),
        _dated_observation(3, {"adds_widget_test"}, LabelValue.NEGATIVE),
        _dated_observation(4, {"adds_widget_test"}, LabelValue.NEGATIVE),
        _dated_observation(5, {"uses_async"}, LabelValue.POSITIVE),
        _dated_observation(6, {"adds_widget_test"}, LabelValue.NEGATIVE),
        _dated_observation(7, {"uses_async"}, LabelValue.POSITIVE),
        _dated_observation(8, {"adds_widget_test"}, LabelValue.NEGATIVE),
    ]
    config = RuleLoomConfig(
        schema_version=1,
        project="ExampleProject",
        pack="flutter_testing",
        pack_version=1,
        learner=LearnerConfig(
            max_body=1,
            max_rules=1,
            allow_negation=False,
            min_precision=1.0,
            min_support=2,
            max_predicates=1,
            bootstrap_runs=0,
        ),
        evaluation=EvaluationConfig(
            test_fraction=0.25,
            min_train_examples=6,
            min_test_examples=2,
        ),
    )

    candidate = learn_candidate(observations, config)

    assert candidate.rules.clauses == (HornClause(TARGET, (RuleLiteral("uses_async"),)),)
    predicate_selection = candidate.metadata["predicate_selection"]
    assert isinstance(predicate_selection, dict)
    assert predicate_selection["search_predicates"] == ["uses_async"]
    assert candidate.metrics["test"].matthews_correlation == 1.0


def test_bootstrap_stability_is_reproducible_and_handles_no_runs() -> None:
    examples = [
        _dated_observation(1, {"risk"}, LabelValue.POSITIVE),
        _dated_observation(2, set(), LabelValue.NEGATIVE),
    ]
    reference = RuleSet(
        TARGET,
        (HornClause(TARGET, (RuleLiteral("risk"),)),),
    )

    def stable_learner(
        _sample: list[Observation] | tuple[Observation, ...], _target: str
    ) -> RuleSet:
        return reference

    first = bootstrap_stability(
        examples,
        TARGET,
        reference,
        stable_learner,
        runs=10,
        seed=42,
    )
    second = bootstrap_stability(
        examples,
        TARGET,
        reference,
        stable_learner,
        runs=10,
        seed=42,
    )

    assert first == second == 1.0
    assert (
        bootstrap_stability(
            examples,
            TARGET,
            reference,
            stable_learner,
            runs=0,
            seed=42,
        )
        == 0.0
    )


def test_temporal_split_warns_but_preserves_a_holdout_when_data_is_small() -> None:
    observations = [
        _dated_observation(1, {"risk"}, LabelValue.POSITIVE),
        _dated_observation(2, set(), LabelValue.NEGATIVE),
        _observation(
            "unknown",
            set(),
            LabelValue.UNKNOWN,
            observed_at=(datetime(2026, 1, 3, tzinfo=UTC) + timedelta(hours=1)).isoformat(),
        ),
    ]

    split = temporal_split(
        observations,
        TARGET,
        test_fraction=0.25,
        min_train=8,
        min_test=4,
    )

    assert len(split.train) == 1
    assert len(split.test) == 1
    assert "only 2 mature labels" in split.warnings[0]
