from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from ruleloom.config import (
    EvaluationConfig,
    LearnerConfig,
    ProtocolConfig,
    RuleLoomConfig,
    SignalProbeConfig,
    default_config,
)
from ruleloom.learners.horn import (
    HORN_ENGINE_VERSION,
    HornBudget,
    HornSettings,
    apply_predicate_order,
    learn_horn_diagnostics,
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
)
from ruleloom.signal_probe import tree_seed_bodies

TARGET = "needs_extra_validation"
PROTOCOL_HASH = RuleLoomConfig(
    schema_version=1,
    project="Tests",
    pack="flutter_testing",
    pack_version=1,
).evidence_protocol_hash


def _observation(index: int, facts: set[str], label: LabelValue) -> Observation:
    observed = datetime(2026, 1, 1, 9, tzinfo=UTC) + timedelta(days=index)
    available = observed + timedelta(hours=1)
    return Observation(
        id=f"example-{index:03d}",
        observed_at=observed.isoformat(),
        protocol_hash=PROTOCOL_HASH,
        facts=frozenset(facts),
        labels={TARGET: label},
        label_evidence={
            TARGET: LabelEvidence(
                kind="synthetic",
                available_at=available.isoformat(),
                source="tests",
            )
        },
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


def _conjunction_cohort() -> list[Observation]:
    """Positives satisfy ``a and not b``; no single literal reaches 0.7 precision.

    The marginal rate gap ranks ``b`` (0.75) above ``a`` (0.5), so an exhaustive
    search restricted to the single best-ranked predicate can only try ``not b``
    (precision 20/30) and abstains.
    """
    examples: list[Observation] = []
    index = 0
    for _ in range(20):
        examples.append(_observation(index, {"a"}, LabelValue.POSITIVE))
        index += 1
    for _ in range(20):
        examples.append(_observation(index, {"a", "b"}, LabelValue.NEGATIVE))
        index += 1
    for _ in range(10):
        examples.append(_observation(index, set(), LabelValue.NEGATIVE))
        index += 1
    for _ in range(10):
        examples.append(_observation(index, {"b"}, LabelValue.NEGATIVE))
        index += 1
    # Interleave labels chronologically so both halves contain both classes.
    examples.sort(key=lambda item: (item.id[-1], item.id))
    return [
        Observation.from_dict(
            {
                **item.to_dict(),
                "id": f"example-{position:03d}",
                "observed_at": (
                    datetime(2026, 1, 1, 9, tzinfo=UTC) + timedelta(days=position)
                ).isoformat(),
                "label_evidence": {
                    TARGET: {
                        "kind": "synthetic",
                        "available_at": (
                            datetime(2026, 1, 1, 10, tzinfo=UTC) + timedelta(days=position)
                        ).isoformat(),
                        "source": "tests",
                        "reason": "",
                    }
                },
            }
        )
        for position, item in enumerate(examples)
    ]


def test_engine_version_advertises_search_controls() -> None:
    assert HORN_ENGINE_VERSION == "ruleloom-horn/0.6"


def test_beam_search_finds_the_conjunction_the_marginal_prefix_hides() -> None:
    examples = _conjunction_cohort()
    exhaustive = learn_horn_diagnostics(
        examples,
        TARGET,
        HornSettings(max_body=2, max_rules=1, min_precision=0.7, max_predicates=1),
    )
    beam = learn_horn_diagnostics(
        examples,
        TARGET,
        HornSettings(
            max_body=2,
            max_rules=1,
            min_precision=0.7,
            max_predicates=64,
            search_strategy="beam",
            beam_width=1,
        ),
    )

    assert exhaustive.rules.clauses == ()
    assert exhaustive.search["strategy"] == "exhaustive"
    assert [clause.signature for clause in beam.rules.clauses] == [f"{TARGET}:-not_b,a"]
    assert beam.search["strategy"] == "beam"
    assert beam.search["beam_width"] == 1
    assert beam.search["eligible_predicates"] == 2
    assert beam.rules.predicts(frozenset({"a"}))
    assert not beam.rules.predicts(frozenset({"a", "b"}))


def test_beam_search_agrees_with_exhaustive_search_while_examining_fewer_bodies() -> None:
    examples = _conjunction_cohort()
    settings = HornSettings(max_body=2, max_rules=1, min_precision=0.7, max_predicates=64)
    exhaustive = learn_horn_diagnostics(examples, TARGET, settings)
    beam = learn_horn_diagnostics(
        examples,
        TARGET,
        HornSettings(
            max_body=2,
            max_rules=1,
            min_precision=0.7,
            max_predicates=64,
            search_strategy="beam",
            beam_width=1,
        ),
    )

    assert exhaustive.rules.signatures == beam.rules.signatures
    assert exhaustive.hypotheses_examined == 4 + 4
    assert beam.hypotheses_examined < exhaustive.hypotheses_examined


def test_wilson_lower_precision_rejects_a_two_example_clause() -> None:
    examples = [
        _observation(1, {"risk"}, LabelValue.POSITIVE),
        _observation(2, {"risk"}, LabelValue.POSITIVE),
        *(_observation(index, set(), LabelValue.NEGATIVE) for index in range(3, 11)),
    ]
    point = learn_horn_diagnostics(
        examples,
        TARGET,
        HornSettings(max_body=1, max_rules=1, min_precision=0.7, min_support=2),
    )
    lower = learn_horn_diagnostics(
        examples,
        TARGET,
        HornSettings(
            max_body=1,
            max_rules=1,
            min_precision=0.7,
            min_support=2,
            precision_estimate="wilson_lower",
        ),
    )

    assert [clause.signature for clause in point.rules.clauses] == [f"{TARGET}:-risk"]
    assert lower.rules.clauses == ()
    rejected = next(item for item in lower.near_misses if item.clause.signature.endswith("risk"))
    assert "precision_lower_bound_below_minimum" in rejected.rejection_reasons
    assert lower.search["precision_estimate"] == "wilson_lower"


def test_temporal_consistency_gate_rejects_support_confined_to_one_half() -> None:
    examples: list[Observation] = []
    for index in range(20):
        if index < 10:
            examples.append(_observation(index, {"x"}, LabelValue.POSITIVE))
        else:
            examples.append(_observation(index, set(), LabelValue.NEGATIVE))
    for index in range(20, 40):
        if index < 22:
            examples.append(_observation(index, set(), LabelValue.POSITIVE))
        elif index < 25:
            examples.append(_observation(index, {"x"}, LabelValue.NEGATIVE))
        else:
            examples.append(_observation(index, set(), LabelValue.NEGATIVE))
    base = HornSettings(max_body=1, max_rules=1, min_precision=0.7, min_support=2)

    unguarded = learn_horn_diagnostics(examples, TARGET, base)
    guarded = learn_horn_diagnostics(
        examples,
        TARGET,
        HornSettings(
            max_body=1,
            max_rules=1,
            min_precision=0.7,
            min_support=2,
            require_temporal_consistency=True,
        ),
    )

    assert [clause.signature for clause in unguarded.rules.clauses] == [f"{TARGET}:-x"]
    assert guarded.rules.clauses == ()
    rejected = next(item for item in guarded.near_misses if item.clause.signature.endswith("x"))
    assert "unstable_across_train_halves" in rejected.rejection_reasons
    assert guarded.search["temporal_consistency_gate"] is True


def _pruning_cohort() -> list[Observation]:
    examples: list[Observation] = []
    index = 0
    # Grow window (first 40): positives carry a and b; b is incidental.
    for _ in range(10):
        examples.append(_observation(index, {"a", "b"}, LabelValue.POSITIVE))
        index += 1
    for _ in range(5):
        examples.append(_observation(index, {"a"}, LabelValue.NEGATIVE))
        index += 1
    for _ in range(4):
        examples.append(_observation(index, {"b"}, LabelValue.NEGATIVE))
        index += 1
    for _ in range(21):
        examples.append(_observation(index, set(), LabelValue.NEGATIVE))
        index += 1
    # Prune window (last 10): positives carry a without b.
    for _ in range(3):
        examples.append(_observation(index, {"a"}, LabelValue.POSITIVE))
        index += 1
    for _ in range(7):
        examples.append(_observation(index, set(), LabelValue.NEGATIVE))
        index += 1
    return examples


def test_chronological_pruning_deletes_incidental_literals_and_regates_on_full_train() -> None:
    examples = _pruning_cohort()
    unpruned = learn_horn_diagnostics(
        examples,
        TARGET,
        HornSettings(max_body=2, max_rules=1, min_precision=0.7, min_support=2),
    )
    pruned = learn_horn_diagnostics(
        examples,
        TARGET,
        HornSettings(
            max_body=2,
            max_rules=1,
            min_precision=0.7,
            min_support=2,
            prune_fraction=0.2,
        ),
    )

    assert [clause.signature for clause in unpruned.rules.clauses] == [f"{TARGET}:-a,b"]
    assert unpruned.pruning["status"] == "disabled"
    assert [clause.signature for clause in pruned.rules.clauses] == [f"{TARGET}:-a"]
    assert pruned.pruning == {
        "status": "applied",
        "prune_fraction": 0.2,
        "grow_examples": 40,
        "prune_examples": 10,
        "literals_removed": 1,
        "clauses_dropped": 0,
        "candidates_rejected_on_complete_window": 0,
    }


def test_pruning_is_skipped_when_the_prune_window_lacks_a_class() -> None:
    examples = [
        *(_observation(index, {"a"}, LabelValue.POSITIVE) for index in range(10)),
        *(_observation(index, set(), LabelValue.NEGATIVE) for index in range(10, 20)),
    ]

    result = learn_horn_diagnostics(
        examples,
        TARGET,
        HornSettings(max_body=1, max_rules=1, min_precision=0.7, prune_fraction=0.2),
    )

    assert [clause.signature for clause in result.rules.clauses] == [f"{TARGET}:-a"]
    assert result.pruning["status"] == "skipped_insufficient_classes"
    assert result.pruning["prune_examples"] == 0


def test_permutation_null_calibrates_strong_signal_against_shuffled_labels() -> None:
    examples = [
        _observation(index, {"risk"} if index % 2 else {"safe"}, LabelValue.POSITIVE)
        if index % 2
        else _observation(index, {"safe"}, LabelValue.NEGATIVE)
        for index in range(60)
    ]

    result = learn_horn_diagnostics(
        examples,
        TARGET,
        HornSettings(max_body=1, max_rules=1, min_precision=0.7, permutation_runs=50, seed=3),
    )

    null = result.permutation_null
    assert null is not None
    assert null["statistic"] == "precision_point"
    assert null["requested_runs"] == 50
    assert null["completed_runs"] == 50
    assert null["budget_exhausted"] is False
    assert null["observed_best"] == 1.0
    assert null["empirical_p_value"] <= 0.1
    assert null["null_best_maximum"] is not None
    assert null["null_best_maximum"] < 1.0
    assert "not a formal hypothesis test" in null["interpretation"]


def test_permutation_null_reports_high_p_value_for_label_independent_facts() -> None:
    examples = [
        _observation(
            index,
            {"noise"} if index % 3 == 0 else set(),
            LabelValue.POSITIVE if index % 2 else LabelValue.NEGATIVE,
        )
        for index in range(60)
    ]

    result = learn_horn_diagnostics(
        examples,
        TARGET,
        HornSettings(
            max_body=1,
            max_rules=1,
            min_precision=0.7,
            gate_mode="relative_lift",
            permutation_runs=40,
            seed=11,
        ),
    )

    assert result.rules.clauses == ()
    null = result.permutation_null
    assert null is not None
    assert null["statistic"] == "conservative_lift_lower"
    assert null["completed_runs"] == 40
    assert null["empirical_p_value"] >= 0.2


def test_permutation_null_stops_before_exhausting_the_shared_budget() -> None:
    examples = [
        _observation(index, {"risk"} if index % 2 else set(), LabelValue.POSITIVE)
        if index % 2
        else _observation(index, set(), LabelValue.NEGATIVE)
        for index in range(20)
    ]
    settings = HornSettings(max_body=1, max_rules=1, min_precision=0.7, permutation_runs=100)

    result = learn_horn_diagnostics(examples, TARGET, settings, budget=HornBudget(12))

    null = result.permutation_null
    assert null is not None
    assert null["budget_exhausted"] is True
    assert null["completed_runs"] < 100
    assert 0 < null["empirical_p_value"] <= 1


def test_seed_bodies_are_evaluated_beyond_the_ranked_prefix_and_validated() -> None:
    examples = _conjunction_cohort()
    seeds = (
        (RuleLiteral("a"), RuleLiteral("b", negated=True)),
        (RuleLiteral("b", negated=True), RuleLiteral("a")),
        (RuleLiteral("unknown"),),
        (RuleLiteral("a"), RuleLiteral("b"), RuleLiteral("a", negated=True)),
    )

    result = learn_horn_diagnostics(
        examples,
        TARGET,
        HornSettings(max_body=2, max_rules=1, min_precision=0.7, max_predicates=1),
        seed_bodies=seeds,
    )

    assert [clause.signature for clause in result.rules.clauses] == [f"{TARGET}:-not_b,a"]
    assert result.search["seed_bodies_evaluated"] == 1


def test_predicate_order_reorders_without_adding_predicates() -> None:
    examples = _conjunction_cohort()
    assert apply_predicate_order(("b", "a"), ("a", "zeta", "b")) == ("a", "b")
    assert apply_predicate_order(("b", "a"), None) == ("b", "a")

    result = learn_horn_diagnostics(
        examples,
        TARGET,
        HornSettings(max_body=1, max_rules=1, min_precision=0.7, max_predicates=1),
        predicate_order=("a", "b"),
    )

    assert result.search["searched_predicates"] == ["a"]
    assert result.rules.clauses == ()


@pytest.mark.parametrize(
    "overrides",
    [
        {"search_strategy": "random"},
        {"precision_estimate": "laplace"},
        {"beam_width": 0},
        {"prune_fraction": 0.6},
        {"permutation_runs": -1},
        {"seed": -1},
    ],
)
def test_horn_settings_reject_invalid_search_controls(overrides: dict[str, object]) -> None:
    with pytest.raises(ModelError):
        HornSettings(**overrides)  # type: ignore[arg-type]


def test_tree_seed_bodies_follow_positive_leaves_and_abstain_without_both_classes() -> None:
    examples = [
        _observation(index, {"risk"} if index % 2 else {"safe"}, LabelValue.POSITIVE)
        if index % 2
        else _observation(index, {"safe"}, LabelValue.NEGATIVE)
        for index in range(20)
    ]

    bodies = tree_seed_bodies(examples, TARGET, max_depth=2)

    assert bodies
    assert all(literal.predicate in {"risk", "safe"} for body in bodies for literal in body)
    assert any(RuleLiteral("risk") in body for body in bodies)
    assert tree_seed_bodies(examples[1::2], TARGET) == ()


def test_schema_v5_defaults_enable_search_controls_and_preserve_v4_serialization() -> None:
    v5 = default_config("Example", schema_version=5, test_start_at="2026-09-01T00:00:00Z")
    v4 = default_config("Example", schema_version=4, test_start_at="2026-09-01T00:00:00Z")

    assert v5.learner.search_strategy == "beam"
    assert v5.learner.beam_width == 20
    assert v5.learner.max_predicates == 64
    assert v5.learner.predicate_ranking == "logistic_weight"
    assert v5.learner.precision_estimate == "wilson_lower"
    assert v5.learner.require_temporal_consistency is True
    assert v5.learner.prune_fraction == 0.2
    assert v5.learner.permutation_runs == 100
    assert v5.learner.tree_seeds is True
    assert v5.learner.gate_mode == "relative_lift"
    assert not v5.learner.search_controls_are_legacy
    assert v4.learner.search_controls_are_legacy
    assert "search_strategy" not in v4.to_dict()["learner"]
    assert RuleLoomConfig.from_dict(v5.to_dict()) == v5
    assert RuleLoomConfig.from_dict(v4.to_dict()) == v4
    assert v5.hash != v4.hash


def test_legacy_schemas_reject_search_controls_and_bounds_follow_the_strategy() -> None:
    with pytest.raises(ModelError, match="schema_version 5"):
        RuleLoomConfig(
            schema_version=4,
            project="Example",
            pack="generic_changes",
            pack_version=1,
            learner=LearnerConfig(gate_mode="relative_lift", search_strategy="beam"),
        )
    with pytest.raises(ModelError, match="max_predicates"):
        LearnerConfig(max_predicates=64)
    assert LearnerConfig(search_strategy="beam", max_predicates=64).hypothesis_count() == (
        2 * 64 * (1 + 20 * 2)
    )
    with pytest.raises(ModelError, match="Horn engine only"):
        LearnerConfig(
            engine="popper",
            max_rules=1,
            bootstrap_runs=0,
            popper_dir="/opt/popper",
            search_strategy="beam",
        )
    with pytest.raises(ModelError, match="search budget"):
        LearnerConfig(search_strategy="beam", max_predicates=256, beam_width=256, max_body=4)


def _validate_config(payload: dict[str, object]) -> None:
    import json
    from importlib.resources import files

    resource = files("ruleloom").joinpath("schemas", "config.schema.json")
    schema = json.loads(resource.read_text(encoding="utf-8"))
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(payload)


def test_config_json_schema_enforces_v5_search_controls() -> None:
    v5 = default_config("Example", schema_version=5, test_start_at="2026-09-01T00:00:00Z")
    payload = v5.to_dict()
    _validate_config(payload)

    missing = dict(payload)
    missing["learner"] = {
        key: value for key, value in dict(payload["learner"]).items() if key != "tree_seeds"
    }
    with pytest.raises(ValidationError):
        _validate_config(missing)

    legacy = default_config("Example", schema_version=4, test_start_at="2026-09-01T00:00:00Z")
    legacy_payload = legacy.to_dict()
    legacy_payload["learner"] = {**dict(legacy_payload["learner"]), "search_strategy": "beam"}
    with pytest.raises(ValidationError):
        _validate_config(legacy_payload)

    wide_exhaustive = v5.to_dict()
    wide_exhaustive["learner"] = {
        **dict(wide_exhaustive["learner"]),
        "search_strategy": "exhaustive",
        "max_predicates": 64,
    }
    with pytest.raises(ValidationError):
        _validate_config(wide_exhaustive)


def _v5_config() -> RuleLoomConfig:
    return RuleLoomConfig(
        schema_version=5,
        project="SearchControls",
        target=TARGET,
        pack="generic_changes",
        pack_version=1,
        protocol=ProtocolConfig(repository_id="repository.unspecified"),
        learner=LearnerConfig(
            max_body=2,
            max_rules=2,
            min_precision=0.7,
            min_support=2,
            bootstrap_runs=2,
            gate_mode="absolute_precision",
            search_strategy="beam",
            beam_width=4,
            max_predicates=64,
            predicate_ranking="logistic_weight",
            precision_estimate="wilson_lower",
            require_temporal_consistency=True,
            prune_fraction=0.2,
            permutation_runs=10,
            tree_seeds=True,
        ),
        evaluation=EvaluationConfig(
            test_fraction=0.25,
            min_train_examples=8,
            min_test_examples=4,
            test_start_at="2026-02-15T00:00:00Z",
        ),
        signal_probe=SignalProbeConfig(enabled=False),
    )


def _generic_observation(
    config: RuleLoomConfig, index: int, facts: set[str], positive: bool
) -> Observation:
    observed = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index)
    available = observed + timedelta(hours=2)
    return Observation(
        id=f"change.{index:03d}",
        observed_at=observed.isoformat(),
        protocol_hash=config.evidence_protocol_hash,
        facts=frozenset(facts),
        labels={TARGET: LabelValue.POSITIVE if positive else LabelValue.NEGATIVE},
        label_evidence={
            TARGET: LabelEvidence(
                kind="synthetic",
                available_at=available.isoformat(),
                source="tests",
            )
        },
        fact_evidence={
            fact: FactEvidence(
                kind="deterministic",
                extractor="ruleloom.generic_changes.git.v1",
                evidence=(f"synthetic:{fact}",),
            )
            for fact in facts
        },
        source={
            "kind": "git_commit",
            "repository": "repository.unspecified",
            "pack": "generic_changes",
            "pack_version": 1,
            "extractor": "ruleloom.generic_changes.git.v1",
        },
        metadata={"topological_index": index + 1},
    )


def test_learn_candidate_records_search_controls_seeds_and_permutation_null() -> None:
    config = _v5_config()
    observations = []
    for index in range(60):
        positive = index % 3 == 0
        facts = {"touches_ci"} if positive else set()
        if index % 5 == 0:
            facts.add("touches_docs")
        observations.append(_generic_observation(config, index, facts, positive))

    candidate = learn_candidate(observations, config, as_of=datetime(2026, 4, 1, tzinfo=UTC))

    assert candidate.engine_version == HORN_ENGINE_VERSION
    assert [clause.signature for clause in candidate.rules.clauses] == [f"{TARGET}:-touches_ci"]
    diagnostics = candidate.metadata["horn_diagnostics"]
    assert diagnostics["search"]["strategy"] == "beam"
    assert diagnostics["search"]["beam_width"] == 4
    assert diagnostics["search"]["precision_estimate"] == "wilson_lower"
    assert diagnostics["search"]["temporal_consistency_gate"] is True
    assert diagnostics["pruning"]["status"] in {"applied", "skipped_insufficient_classes"}
    assert diagnostics["permutation_null"]["completed_runs"] == 10
    selection = candidate.metadata["predicate_selection"]
    assert selection["search_strategy"] == "beam"
    assert selection["predicate_ranking"] == "logistic_weight"
    assert selection["search_predicates"][0] == "touches_ci"
    assert selection["tree_seed_bodies"]
    assert candidate.stability == 1.0
    assert HornClause(TARGET, (RuleLiteral("touches_ci"),)) in candidate.rules.clauses
