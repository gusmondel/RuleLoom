from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from ruleloom.config import EvaluationConfig, RuleLoomConfig, SignalProbeConfig
from ruleloom.models import FactEvidence, LabelEvidence, LabelValue, ModelError, Observation
from ruleloom.signal_probe import (
    SignalProbeReport,
    _average_precision,
    _fit_boolean_tree,
    _weighted_probability,
    run_signal_probe,
    wilson_interval,
)

TARGET = "post_merge_defect"


def _config() -> RuleLoomConfig:
    return RuleLoomConfig(
        schema_version=4,
        project="SignalTests",
        target=TARGET,
        pack="generic_changes",
        pack_version=1,
        evaluation=EvaluationConfig(test_start_at="2026-03-15T00:00:00Z"),
        signal_probe=SignalProbeConfig(
            enabled=True,
            folds=4,
            min_train_examples=20,
            min_validation_examples=5,
            min_mcc=0.25,
            min_lift_lower_bound=3.0,
        ),
    )


def _observation(
    config: RuleLoomConfig,
    index: int,
    *,
    positive: bool,
    facts: set[str],
    day_offset: int | None = None,
) -> Observation:
    observed = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(
        days=index if day_offset is None else day_offset
    )
    available = observed + timedelta(hours=12)
    label = LabelValue.POSITIVE if positive else LabelValue.NEGATIVE
    return Observation(
        id=f"change.{index}",
        observed_at=observed.isoformat(),
        protocol_hash=config.evidence_protocol_hash,
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


def test_signal_probe_passes_strong_train_only_signal() -> None:
    config = _config()
    observations = [
        _observation(
            config,
            index,
            positive=index % 4 == 0,
            facts={"touches_test"} if index % 4 == 0 else set(),
        )
        for index in range(60)
    ]

    report = run_signal_probe(observations, config, as_of=datetime(2026, 4, 1, tzinfo=UTC))

    assert report.status == "pass"
    assert len(report.models) == 2
    assert all(model.folds == 4 for model in report.models)
    assert max(model.metrics.matthews_correlation for model in report.models) == 1.0
    assert report.to_dict()["methodology"]["holdout_consulted"] is False


def test_signal_probe_fails_when_vocabulary_has_no_signal() -> None:
    config = _config()
    observations = [
        _observation(
            config,
            index,
            positive=index % 4 == 0,
            facts={"touches_docs"},
        )
        for index in range(60)
    ]

    report = run_signal_probe(observations, config, as_of=datetime(2026, 4, 1, tzinfo=UTC))

    assert report.status == "fail"
    assert all(not model.gate_passed for model in report.models)


def test_signal_probe_does_not_consult_post_boundary_labels_or_facts() -> None:
    config = _config()
    training = [
        _observation(
            config,
            index,
            positive=index % 4 == 0,
            facts={"touches_test"} if index % 4 == 0 else set(),
        )
        for index in range(60)
    ]
    holdout = _observation(
        config,
        100,
        day_offset=100,
        positive=True,
        facts={"large_change"},
    )
    changed_holdout = replace(
        holdout,
        facts=frozenset({"touches_ci"}),
        fact_evidence={
            "touches_ci": FactEvidence(
                kind="deterministic",
                extractor="ruleloom.generic_changes.git.v1",
                evidence=("synthetic:touches_ci",),
            )
        },
        labels={TARGET: LabelValue.NEGATIVE},
    )
    instant = datetime(2026, 5, 1, tzinfo=UTC)

    first = run_signal_probe([*training, holdout], config, as_of=instant)
    second = run_signal_probe([*training, changed_holdout], config, as_of=instant)

    assert first.identity_payload() == second.identity_payload()
    assert first.id == second.id


def test_signal_probe_identity_includes_the_evaluation_cutoff() -> None:
    config = _config()
    observations = [
        _observation(
            config,
            index,
            positive=index % 4 == 0,
            facts={"touches_test"} if index % 4 == 0 else set(),
        )
        for index in range(60)
    ]

    first = run_signal_probe(observations, config, as_of=datetime(2026, 4, 1, tzinfo=UTC))
    second = run_signal_probe(observations, config, as_of=datetime(2026, 4, 2, tzinfo=UTC))

    assert first.dataset_hash == second.dataset_hash
    assert first.id != second.id


def test_wilson_interval_is_bounded_for_sparse_alerts() -> None:
    lower, upper = wilson_interval(1, 2, 0.95)

    assert 0 < lower < 0.5 < upper < 1


def test_signal_probe_rejects_invalid_inputs_and_one_class_tree() -> None:
    config = _config()
    negative = _observation(config, 0, positive=False, facts=set())

    with pytest.raises(ModelError, match="successes"):
        wilson_interval(2, 1, 0.95)
    with pytest.raises(ModelError, match="confidence"):
        wilson_interval(1, 2, 0.5)
    with pytest.raises(ModelError, match="both mature classes"):
        _fit_boolean_tree([negative], TARGET, max_depth=2, max_predicates=10)
    with pytest.raises(ModelError, match="unsupported signal probe status"):
        SignalProbeReport(
            id="probe-invalid",
            created_at="2026-01-01T00:00:00Z",
            status="invalid",
            version="test",
            dataset_hash="a" * 64,
            config_hash="b" * 64,
            holdout_start_at="2026-03-15T00:00:00Z",
            training_observations=0,
            models=(),
            warnings=(),
        )
    disabled = replace(config, signal_probe=SignalProbeConfig(enabled=False))
    with pytest.raises(ModelError, match="disabled"):
        run_signal_probe([], disabled)
    with pytest.raises(ModelError, match="timezone"):
        run_signal_probe([], config, as_of=datetime(2026, 4, 1))

    assert wilson_interval(0, 0, 0.95) == (0.0, 1.0)
    assert _average_precision([False], [0.0]) == 0.0
    assert _weighted_probability([False], [0.0], [0]) == 0.0


def test_signal_probe_is_inconclusive_when_labels_are_insufficient_or_late() -> None:
    config = _config()
    empty = run_signal_probe([], config, as_of=datetime(2026, 4, 1, tzinfo=UTC))
    assert empty.status == "inconclusive"
    assert any("insufficient" in warning for warning in empty.warnings)

    observations = [
        replace(
            _observation(
                config,
                index,
                positive=index % 4 == 0,
                facts={"touches_test"} if index % 4 == 0 else set(),
            ),
            label_evidence={
                TARGET: LabelEvidence(
                    kind="synthetic",
                    available_at="2026-12-01T00:00:00Z",
                    source="tests",
                )
            },
        )
        for index in range(60)
    ]
    late = run_signal_probe(observations, config, as_of=datetime(2027, 1, 1, tzinfo=UTC))

    assert late.status == "inconclusive"
    assert any("skipped fold" in warning for warning in late.warnings)


def test_signal_probe_enforces_the_minimum_alert_rate() -> None:
    config = _config()
    config = replace(
        config,
        signal_probe=replace(config.signal_probe, min_alert_rate=1.0),
    )
    observations = [
        _observation(
            config,
            index,
            positive=index % 4 == 0,
            facts={"touches_test"} if index % 4 == 0 else set(),
        )
        for index in range(60)
    ]

    report = run_signal_probe(observations, config, as_of=datetime(2026, 4, 1, tzinfo=UTC))

    assert any("alert_rate_below_minimum" in model.gate_reasons for model in report.models)
