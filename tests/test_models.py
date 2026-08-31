from __future__ import annotations

from dataclasses import replace

import pytest

from ruleloom.models import (
    Candidate,
    FactEvidence,
    HornClause,
    LabelEvidence,
    LabelValue,
    Metrics,
    ModelError,
    Observation,
    Prediction,
    RuleLiteral,
    RuleSet,
    canonical_json,
    content_hash,
    strict_json_loads,
    validate_json_value,
    validate_prediction_cohort,
)

TARGET = "needs_extra_validation"
OBSERVED_AT = "2026-08-31T12:00:00+00:00"
AVAILABLE_AT = "2026-08-31T13:00:00+00:00"
EXPERIMENT_ID = "ruleloom-pilot-v1"
REPOSITORY_ID = "repository.example"
PACK = "flutter_testing"
EXTRACTOR = "ruleloom.gitfacts/flutter_testing@1"
CONFIG_HASH = "c" * 64
EVIDENCE_PROTOCOL_HASH = "e" * 64
OUTCOME_DEFINITION = "Whether the change needs extra validation after prospective review."
CHANGE_ID = "range.abc123"


def _protocol() -> dict[str, object]:
    return {
        "experiment_id": EXPERIMENT_ID,
        "repository_id": REPOSITORY_ID,
        "observation_unit": "git_range",
        "outcome_definition": OUTCOME_DEFINITION,
        "target": TARGET,
        "pack": PACK,
        "extractor": EXTRACTOR,
        "config_hash": CONFIG_HASH,
        "evidence_protocol_hash": EVIDENCE_PROTOCOL_HASH,
    }


def _label_evidence() -> LabelEvidence:
    return LabelEvidence(
        kind="synthetic",
        available_at=AVAILABLE_AT,
        source="tests",
        reason="known synthetic outcome",
        confidence=0.9,
    )


def _observation() -> Observation:
    return Observation(
        id="commit.abc123",
        observed_at=OBSERVED_AT,
        protocol_hash=EVIDENCE_PROTOCOL_HASH,
        facts=frozenset({"uses_async", "changes_dart"}),
        labels={TARGET: LabelValue.POSITIVE},
        label_evidence={TARGET: _label_evidence()},
        fact_evidence={
            "uses_async": FactEvidence(
                kind="deterministic",
                extractor="flutter-testing/1",
                evidence=("lib/item.dart:+await save()",),
                confidence=1.0,
            ),
            "changes_dart": FactEvidence(
                kind="deterministic",
                extractor="flutter-testing/1",
                evidence=("lib/item.dart",),
            ),
        },
        source={
            "kind": "git_range",
            "repository": REPOSITORY_ID,
            "pack": PACK,
            "extractor": EXTRACTOR,
            "change_id": CHANGE_ID,
            "head": "abc123",
        },
        metadata={"files_changed": 1, "tags": ["synthetic"]},
    )


def _candidate() -> Candidate:
    rules = RuleSet(
        target=TARGET,
        clauses=(
            HornClause(
                target=TARGET,
                body=(
                    RuleLiteral("uses_async"),
                    RuleLiteral("adds_widget_test", negated=True),
                ),
            ),
        ),
    )
    return Candidate(
        id="cand-model-roundtrip",
        created_at="2026-08-31T14:00:00Z",
        engine="horn",
        engine_version="ruleloom-horn/test",
        dataset_hash="d" * 64,
        config_hash="c" * 64,
        rules=rules,
        metrics={"test": Metrics.from_counts(8, 2, 9, 1)},
        baselines={
            "never_alert": Metrics.from_counts(0, 0, 11, 9),
            "always_alert": Metrics.from_counts(9, 11, 0, 0),
            "train_majority": Metrics.from_counts(9, 11, 0, 0),
            "best_single_literal": Metrics.from_counts(8, 2, 9, 1),
        },
        stability=0.75,
        train_ids=("train-1", "train-2"),
        test_ids=("test-1",),
        warnings=("synthetic candidate",),
        metadata={"readiness": {"positive": 50}},
        review={"reviewer": "Test Reviewer"},
        status="approved",
    ).with_identity()


def _prediction_snapshot() -> Observation:
    return replace(
        _observation(),
        labels={TARGET: LabelValue.UNKNOWN},
        label_evidence={},
    )


def _policies(candidate: Candidate) -> tuple[dict[str, object], ...]:
    return (
        {
            "candidate_id": candidate.id,
            "status": candidate.status,
            "target": TARGET,
            "manifest_hash": content_hash(candidate.to_dict()),
            "rule_signatures": sorted(candidate.rules.signatures),
        },
    )


def _prediction(candidate: Candidate | None = None) -> Prediction:
    candidate = candidate or _candidate()
    policies = _policies(candidate)
    protocol = _protocol()
    protocol_hash = content_hash(protocol)
    return Prediction(
        id="prediction.pending",
        predicted_at="2026-08-31T14:30:00Z",
        observation=_prediction_snapshot(),
        target=TARGET,
        unit_id=CHANGE_ID,
        protocol_hash=protocol_hash,
        protocol=protocol,
        policy_set_hash=content_hash(
            {
                "protocol_hash": protocol_hash,
                "target": TARGET,
                "policies": list(policies),
            }
        ),
        policies=policies,
        matches=(
            {
                "candidate_id": candidate.id,
                "status": candidate.status,
                "rule": candidate.rules.clauses[0].to_dict(),
                "prolog": candidate.rules.clauses[0].to_prolog(),
            },
        ),
        abstained=False,
    ).with_identity()


def _with_protocol(prediction: Prediction, **changes: object) -> Prediction:
    protocol = dict(prediction.protocol)
    protocol.update(changes)
    protocol_hash = content_hash(protocol)
    return replace(
        prediction,
        protocol=protocol,
        protocol_hash=protocol_hash,
        policy_set_hash=content_hash(
            {
                "protocol_hash": protocol_hash,
                "target": prediction.target,
                "policies": list(prediction.policies),
            }
        ),
    )


def test_observation_round_trip_preserves_fact_and_label_provenance() -> None:
    observation = _observation()

    encoded = observation.to_dict()
    decoded = Observation.from_dict(encoded)

    assert decoded == observation
    assert encoded["facts"] == ["changes_dart", "uses_async"]
    assert decoded.label_evidence[TARGET].available_at == AVAILABLE_AT
    assert decoded.fact_evidence["uses_async"].evidence == ("lib/item.dart:+await save()",)
    assert decoded.fact_evidence["uses_async"].confidence == 1.0
    assert decoded.source == {
        "kind": "git_range",
        "repository": REPOSITORY_ID,
        "pack": PACK,
        "extractor": EXTRACTOR,
        "change_id": CHANGE_ID,
        "head": "abc123",
    }


def test_mature_labels_require_provenance() -> None:
    with pytest.raises(ModelError, match="mature labels require label_evidence"):
        Observation(
            id="missing-evidence",
            observed_at=OBSERVED_AT,
            protocol_hash=EVIDENCE_PROTOCOL_HASH,
            facts=frozenset(),
            labels={TARGET: LabelValue.NEGATIVE},
        )

    unknown = Observation(
        id="not-mature",
        observed_at=OBSERVED_AT,
        protocol_hash=EVIDENCE_PROTOCOL_HASH,
        facts=frozenset(),
        labels={TARGET: LabelValue.UNKNOWN},
    )
    with pytest.raises(ModelError, match="mature labels require label_evidence"):
        unknown.with_label(TARGET, LabelValue.POSITIVE)

    mature = unknown.with_label(TARGET, LabelValue.POSITIVE, _label_evidence())
    assert mature.labels[TARGET] is LabelValue.POSITIVE
    assert mature.label_evidence[TARGET].source == "tests"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"fact_evidence": {"unknown_fact": FactEvidence("human", "reviewer")}}, "absent facts"),
        (
            {
                "label_evidence": {
                    TARGET: _label_evidence(),
                    "unknown_target": _label_evidence(),
                }
            },
            "absent labels",
        ),
    ],
)
def test_provenance_cannot_reference_absent_data(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ModelError, match=message):
        replace(_observation(), **changes)


def test_rules_candidate_and_prediction_round_trip() -> None:
    candidate = _candidate()
    assert Candidate.from_dict(candidate.to_dict()) == candidate
    assert candidate.rules.predicts(frozenset({"uses_async"}))
    assert not candidate.rules.predicts(frozenset({"uses_async", "adds_widget_test"}))
    assert candidate.rules.clauses[0].to_prolog() == (
        "needs_extra_validation(A) :- uses_async(A), not_adds_widget_test(A)."
    )

    policies = _policies(candidate)
    prediction = _prediction(candidate)
    prediction.validate_identity()
    assert Prediction.from_dict(prediction.to_dict()) == prediction
    assert prediction.protocol == _protocol()
    assert prediction.protocol_hash == content_hash(prediction.protocol)
    assert prediction.policy_set_hash == content_hash(
        {
            "protocol_hash": prediction.protocol_hash,
            "target": TARGET,
            "policies": list(policies),
        }
    )
    assert set(candidate.baselines) == {
        "never_alert",
        "always_alert",
        "train_majority",
        "best_single_literal",
    }
    with pytest.raises(ModelError, match="abstained must agree"):
        replace(prediction, abstained=True)
    with pytest.raises(ModelError, match="unit_id does not match"):
        replace(prediction, unit_id="range.other")
    with pytest.raises(ModelError, match="match status"):
        replace(
            prediction,
            matches=({"candidate_id": candidate.id},),
        )
    with pytest.raises(ModelError, match="protocol snapshot is incomplete"):
        replace(
            prediction,
            protocol={key: value for key, value in prediction.protocol.items() if key != "pack"},
        )
    legacy_policy_set_hash = content_hash({"target": TARGET, "policies": list(policies)})
    with pytest.raises(ModelError, match="policy_set_hash"):
        replace(prediction, policy_set_hash=legacy_policy_set_hash)


def test_persisted_models_reject_unknown_fields() -> None:
    observation = _observation().to_dict()
    candidate = _candidate().to_dict()
    observation["unexpected"] = True
    candidate["unexpected"] = True

    with pytest.raises(ModelError, match="unknown observation fields"):
        Observation.from_dict(observation)
    with pytest.raises(ModelError, match="unknown candidate fields"):
        Candidate.from_dict(candidate)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"experiment_id": "Bad"}, "observation id"),
        ({"observation_unit": "ticket"}, "unsupported prediction observation unit"),
        ({"target": "other_target"}, "protocol target"),
        ({"pack": "other_pack"}, "protocol pack"),
        ({"extractor": "other-extractor"}, "protocol extractor"),
        ({"outcome_definition": " "}, "cannot be blank"),
        ({"config_hash": "short"}, "64 characters"),
    ],
)
def test_prediction_protocol_rejects_semantic_drift(
    change: dict[str, object], message: str
) -> None:
    with pytest.raises(ModelError, match=message):
        _with_protocol(_prediction(), **change)


def test_prediction_rejects_snapshot_time_outcome_and_source_drift() -> None:
    prediction = _prediction()
    with pytest.raises(ModelError, match="mature target outcome"):
        replace(prediction, observation=_observation())
    with pytest.raises(ModelError, match="cannot precede observation"):
        replace(prediction, predicted_at="2026-08-31T11:00:00Z")
    with pytest.raises(ModelError, match="repository does not match"):
        replace(
            prediction,
            observation=replace(
                prediction.observation,
                source={**prediction.observation.source, "repository": "repository.other"},
            ),
        )
    with pytest.raises(ModelError, match="unit does not match"):
        replace(
            prediction,
            observation=replace(
                prediction.observation,
                source={**prediction.observation.source, "kind": "git_worktree"},
            ),
        )
    with pytest.raises(ModelError, match="protocol_hash"):
        replace(prediction, protocol_hash="0" * 64)


def test_prediction_rejects_malformed_policy_and_match_snapshots() -> None:
    prediction = _prediction()
    policy = dict(prediction.policies[0])
    cases = [
        ({**policy, "status": "candidate"}, "unsupported prediction policy status"),
        ({**policy, "target": "other_target"}, "policy target"),
        ({**policy, "manifest_hash": ""}, "manifest_hash"),
        ({**policy, "rule_signatures": "bad"}, "rule_signatures"),
        (
            {**policy, "rule_signatures": [*policy["rule_signatures"], *policy["rule_signatures"]]},
            "cannot contain duplicates",
        ),
    ]
    for malformed, message in cases:
        with pytest.raises(ModelError, match=message):
            replace(prediction, policies=(malformed,))
    with pytest.raises(ModelError, match="duplicate prediction policy"):
        replace(prediction, policies=(policy, policy))

    match = dict(prediction.matches[0])
    with pytest.raises(ModelError, match="unsupported prediction match status"):
        replace(prediction, matches=({**match, "status": "candidate"},))
    with pytest.raises(ModelError, match="absent from its policy snapshot"):
        replace(prediction, matches=({**match, "candidate_id": "cand-deadbeefdeadbeef"},))
    with pytest.raises(ModelError, match="duplicate candidate/rule match"):
        replace(prediction, matches=(match, match))
    with pytest.raises(ModelError, match="prolog does not match"):
        replace(prediction, matches=({**match, "prolog": "wrong(A)."},))


def test_prediction_from_dict_and_identity_fail_closed() -> None:
    prediction = _prediction()
    with pytest.raises(ModelError, match="does not match content identity"):
        replace(prediction, id="prediction.tampered").validate_identity()
    for field, value, message in (
        ("matches", {}, "matches must be an array"),
        ("policies", {}, "policies must be an array"),
        ("abstained", 1, "abstained must be a boolean"),
        ("schema_version", True, "schema_version must be an integer"),
    ):
        payload = prediction.to_dict()
        payload[field] = value
        with pytest.raises(ModelError, match=message):
            Prediction.from_dict(payload)


def test_prediction_cohort_checks_expected_protocol_unit_and_repository() -> None:
    prediction = _prediction()
    validate_prediction_cohort(
        [prediction],
        expected_protocol_hash=prediction.protocol_hash,
        expected_observation_unit="git_range",
        expected_repository_id=REPOSITORY_ID,
    )
    for keyword, value, message in (
        ("expected_protocol_hash", "0" * 64, "prospective protocol"),
        ("expected_observation_unit", "git_worktree", "observation unit"),
        ("expected_repository_id", "repository.other", "configured repository"),
    ):
        with pytest.raises(ModelError, match=message):
            validate_prediction_cohort([prediction], **{keyword: value})


def test_metrics_compute_matthews_correlation_and_edge_cases() -> None:
    metrics = Metrics.from_counts(tp=30, fp=10, tn=50, fn=10)

    assert metrics.precision == pytest.approx(0.75)
    assert metrics.recall == pytest.approx(0.75)
    assert metrics.balanced_accuracy == pytest.approx((0.75 + 5 / 6) / 2)
    assert metrics.matthews_correlation == pytest.approx(7 / 12)
    assert Metrics.from_dict(metrics.to_dict()) == metrics

    assert Metrics.from_counts(5, 0, 7, 0).matthews_correlation == 1.0
    assert Metrics.from_counts(0, 7, 0, 5).matthews_correlation == -1.0
    assert Metrics.from_counts(0, 0, 12, 0).matthews_correlation == 0.0


def test_content_hash_is_canonical_but_sensitive_to_values() -> None:
    first = content_hash({"b": [2, 1], "a": {"x": True}})
    reordered = content_hash({"a": {"x": True}, "b": [2, 1]})
    changed = content_hash({"a": {"x": True}, "b": [1, 2]})

    assert first == reordered
    assert first != changed


def test_strict_json_rejects_duplicate_keys_nonfinite_values_and_bad_python_types() -> None:
    with pytest.raises(ModelError, match="duplicate object key"):
        strict_json_loads('{"key": 1, "key": 2}', "fixture")
    with pytest.raises(ModelError, match="invalid numeric constant"):
        strict_json_loads('{"value": NaN}', "fixture")
    with pytest.raises(ModelError, match="object keys must be strings"):
        validate_json_value({1: "bad"})
    with pytest.raises(ModelError, match="unsupported value type"):
        validate_json_value({"bad": object()})
    with pytest.raises(ModelError, match="surrogate"):
        strict_json_loads(r'{"value": "\ud800"}', "fixture")
    with pytest.raises(ModelError, match="surrogate"):
        validate_json_value({"\udfff": "bad"})
    assert strict_json_loads(r'{"value": "\ud83d\ude00"}') == {"value": "😀"}
    with pytest.raises(ValueError):
        canonical_json({"bad": float("inf")})
