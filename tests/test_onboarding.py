from __future__ import annotations

import pytest

from ruleloom.lifecycle import Readiness
from ruleloom.models import ModelError
from ruleloom.onboarding import diagnose_onboarding


def _readiness(
    *,
    observations: int = 100,
    positive: int = 0,
    negative: int = 0,
    unknown: int | None = None,
    predicates: int = 6,
    stage: str = "collection",
) -> Readiness:
    unresolved = observations - positive - negative if unknown is None else unknown
    return Readiness(
        observations=observations,
        labeled=positive + negative,
        positive=positive,
        negative=negative,
        unknown=unresolved,
        fact_evidence_coverage=1.0 if predicates else 0.0,
        label_evidence_coverage=1.0 if positive + negative else 0.0,
        distinct_predicates=predicates,
        stage=stage,
        warnings=(),
    )


def _audit() -> dict[str, object]:
    return {
        "predicates": [
            {"predicate": "never", "flags": ["never_true"]},
            {"predicate": "rare", "flags": ["rare", "prevalence_drift"]},
            {"predicate": "stable", "flags": []},
        ],
        "relations": [
            {
                "left": "never",
                "right": "stable",
                "equivalent": True,
                "high_overlap": True,
            }
        ],
        "configured_coverage": {"coverage": 0.75},
    }


def test_diagnosis_identifies_outcome_bottleneck_and_exact_gate_gaps() -> None:
    diagnosis = diagnose_onboarding(
        _readiness(observations=1_566),
        history_status={
            "events": 1_573,
            "change_units": 1_573,
            "confirmatory_units": 0,
        },
        predicate_audit=_audit(),
    )

    assert diagnosis.stage == "collect_outcomes"
    assert "outcome evidence is the bottleneck" in diagnosis.headline
    assert diagnosis.gate_gaps == {
        "positive_for_shadow": 20,
        "positive_for_approval": 50,
        "positive_class": 1,
        "negative_class": 1,
    }
    assert [action.code for action in diagnosis.actions] == [
        "review_vocabulary_redundancy",
        "import_outcome_evidence",
        "rematerialize_outcomes",
        "review_vocabulary_distribution",
    ]
    assert "1566 observations; 0 mature outcomes" in diagnosis.render_text()
    rendered = diagnosis.render_text()
    assert "history import --events /absolute/path/to/outcome-events.jsonl" in rendered
    assert "GitHub archive is exploratory and does not supply strong outcomes" in rendered


def test_diagnosis_bootstraps_then_materializes_without_claiming_labels() -> None:
    empty = diagnose_onboarding(
        _readiness(observations=0, predicates=0),
        history_status={"events": 0, "change_units": 0, "confirmatory_units": 0},
    )
    assert empty.stage == "bootstrap"
    assert [action.code for action in empty.actions] == [
        "bootstrap_git_history",
        "materialize_history",
        "audit_predicates",
        "import_outcome_evidence",
    ]

    collected = diagnose_onboarding(
        _readiness(observations=0, predicates=0),
        history_status={"events": 8, "change_units": 8, "confirmatory_units": 0},
        predicate_audit={"predicates": [], "relations": []},
    )
    assert [action.code for action in collected.actions][:2] == [
        "materialize_history",
        "import_outcome_evidence",
    ]
    assert not any(
        "negative" in item.lower() and "inferred" in item.lower() for item in collected.evidence
    )


@pytest.mark.parametrize(
    ("positive", "negative", "expected_stage", "missing"),
    [
        (4, 0, "balance_outcomes", "negative"),
        (0, 4, "balance_outcomes", "positive"),
    ],
)
def test_diagnosis_preserves_unknown_when_one_class_is_missing(
    positive: int,
    negative: int,
    expected_stage: str,
    missing: str,
) -> None:
    diagnosis = diagnose_onboarding(
        _readiness(positive=positive, negative=negative),
        history_status={"events": 10, "change_units": 10, "confirmatory_units": 10},
        predicate_audit={"predicates": [], "relations": []},
    )

    assert diagnosis.stage == expected_stage
    action = next(item for item in diagnosis.actions if item.code == "collect_missing_class")
    assert missing in action.title.lower()
    assert "do not manufacture" in action.detail
    assert not any(item.code == "try_exploratory_learning" for item in diagnosis.actions)


def test_diagnosis_allows_only_an_exploratory_learning_attempt_with_both_classes() -> None:
    diagnosis = diagnose_onboarding(
        _readiness(positive=6, negative=6, stage="collection"),
        history_status={"events": 20, "change_units": 12, "confirmatory_units": 12},
        predicate_audit={"predicates": [], "relations": []},
    )

    assert diagnosis.stage == "exploratory_learning"
    action = next(item for item in diagnosis.actions if item.code == "try_exploratory_learning")
    assert action.command == "ruleloom learn --json"
    assert "still enforce" in action.detail
    assert diagnosis.gate_gaps["positive_for_shadow"] == 14


def test_diagnosis_does_not_guess_when_optional_reports_are_missing() -> None:
    diagnosis = diagnose_onboarding(_readiness())

    assert [action.code for action in diagnosis.actions] == [
        "inspect_history",
        "audit_predicates",
        "import_outcome_evidence",
    ]
    assert not any("confirmatory" in item for item in diagnosis.evidence)


def test_diagnosis_serialization_is_stable_and_thresholds_are_validated() -> None:
    diagnosis = diagnose_onboarding(
        _readiness(positive=20, negative=10, stage="shadow"),
        history_status={"events": 40, "change_units": 30, "confirmatory_units": 30},
        predicate_audit={"predicates": [], "relations": []},
    )
    payload = diagnosis.to_dict()

    assert payload["stage"] == "shadow"
    assert payload["gate_gaps"] == {
        "negative_class": 0,
        "positive_class": 0,
        "positive_for_approval": 30,
        "positive_for_shadow": 0,
    }
    assert diagnosis.render_text().endswith("\n")

    with pytest.raises(ModelError, match="integer >= 1"):
        diagnose_onboarding(_readiness(), min_positive_for_shadow=0)
    with pytest.raises(ModelError, match="must be >="):
        diagnose_onboarding(
            _readiness(),
            min_positive_for_shadow=20,
            min_positive_for_approval=10,
        )
