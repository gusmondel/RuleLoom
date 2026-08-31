from __future__ import annotations

import json
import re
from importlib.resources import files
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from ruleloom.config import RuleLoomConfig, default_config
from ruleloom.models import (
    Candidate,
    HornClause,
    LabelValue,
    Metrics,
    Observation,
    Prediction,
    RuleLiteral,
    RuleSet,
    content_hash,
)

TARGET = "needs_extra_validation"
EXPERIMENT_ID = "ruleloom-pilot-v1"
REPOSITORY_ID = "repository.example"
PACK = "flutter_testing"
EXTRACTOR = "ruleloom.gitfacts/flutter_testing@1"
CONFIG_HASH = "c" * 64
EVIDENCE_PROTOCOL_HASH = "e" * 64
OUTCOME_DEFINITION = "Whether the change needs extra validation after prospective review."
CHANGE_ID = "range.change.one"


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


def _schema(name: str) -> dict[str, object]:
    resource = files("ruleloom").joinpath("schemas", f"{name}.schema.json")
    value = json.loads(resource.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    Draft202012Validator.check_schema(value)
    return value


def _validate(name: str, instance: object) -> None:
    Draft202012Validator(_schema(name), format_checker=FormatChecker()).validate(instance)


def _observation() -> Observation:
    return Observation(
        id="change.one",
        observed_at="2026-08-31T12:00:00Z",
        protocol_hash=EVIDENCE_PROTOCOL_HASH,
        facts=frozenset({"changes_dart"}),
        labels={TARGET: LabelValue.UNKNOWN},
        source={
            "kind": "git_range",
            "repository": REPOSITORY_ID,
            "pack": PACK,
            "extractor": EXTRACTOR,
            "change_id": CHANGE_ID,
        },
    )


def _candidate() -> Candidate:
    metrics = Metrics.from_counts(2, 0, 2, 0)
    rules = RuleSet(TARGET, (HornClause(TARGET, (RuleLiteral("changes_dart"),)),))
    return Candidate(
        id="cand-schema",
        created_at="2026-08-31T12:00:00Z",
        engine="horn",
        engine_version="ruleloom-horn/0.1",
        dataset_hash="d" * 64,
        config_hash="c" * 64,
        rules=rules,
        metrics={"train": metrics, "test": metrics},
        baselines={
            "never_alert": Metrics.from_counts(0, 0, 2, 2),
            "always_alert": Metrics.from_counts(2, 2, 0, 0),
            "train_majority": Metrics.from_counts(0, 0, 2, 2),
            "best_single_literal": metrics,
        },
        stability=1.0,
        train_ids=("train.one",),
        test_ids=("test.one",),
        metadata={
            "pack": PACK,
            "extractors": [EXTRACTOR],
        },
    ).with_identity()


def _prediction() -> Prediction:
    observation = _observation()
    candidate = _candidate()
    policies = (
        {
            "candidate_id": candidate.id,
            "status": "approved",
            "target": TARGET,
            "manifest_hash": content_hash(candidate.to_dict()),
            "rule_signatures": sorted(candidate.rules.signatures),
        },
    )
    protocol = _protocol()
    protocol_hash = content_hash(protocol)
    return Prediction(
        id="prediction.pending",
        predicted_at="2026-08-31T12:01:00Z",
        observation=observation,
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
        matches=(),
        abstained=True,
    ).with_identity()


def test_public_schemas_accept_every_persisted_model_shape() -> None:
    observation = _observation()
    candidate = _candidate()
    prediction = _prediction()

    _validate("config", RuleLoomConfig(project="ExampleProject").to_dict())
    _validate("config", default_config("ExampleProject").to_dict())
    _validate("observation", observation.to_dict())
    _validate("candidate", candidate.to_dict())
    _validate("prediction", prediction.to_dict())


@pytest.mark.parametrize("name", ["config", "observation", "candidate", "prediction"])
def test_public_schemas_reject_unknown_top_level_fields(name: str) -> None:
    instances = {
        "config": RuleLoomConfig(project="ExampleProject").to_dict(),
        "observation": _observation().to_dict(),
        "candidate": _candidate().to_dict(),
        "prediction": _prediction().to_dict(),
    }
    instances[name]["unexpected"] = True

    with pytest.raises(ValidationError):
        _validate(name, instances[name])


def test_candidate_schema_requires_engine_provenance() -> None:
    payload = _candidate().to_dict()
    del payload["engine_version"]

    with pytest.raises(ValidationError):
        _validate("candidate", payload)


@pytest.mark.parametrize(
    "baseline",
    ["never_alert", "always_alert", "train_majority", "best_single_literal"],
)
def test_candidate_schema_requires_all_four_baselines(baseline: str) -> None:
    payload = _candidate().to_dict()
    del payload["baselines"][baseline]

    with pytest.raises(ValidationError):
        _validate("candidate", payload)


def test_prediction_schema_requires_the_exact_protocol_snapshot() -> None:
    payload = _prediction().to_dict()
    assert payload["unit_id"] == payload["observation"]["source"]["change_id"]
    assert payload["protocol"] == _protocol()
    assert payload["protocol_hash"] == content_hash(payload["protocol"])
    assert payload["policy_set_hash"] == content_hash(
        {
            "protocol_hash": payload["protocol_hash"],
            "target": TARGET,
            "policies": payload["policies"],
        }
    )

    del payload["protocol"]["extractor"]
    with pytest.raises(ValidationError):
        _validate("prediction", payload)

    payload = _prediction().to_dict()
    payload["protocol"]["unexpected"] = True
    with pytest.raises(ValidationError):
        _validate("prediction", payload)


def test_prediction_schema_requires_unit_id() -> None:
    payload = _prediction().to_dict()
    del payload["unit_id"]

    with pytest.raises(ValidationError):
        _validate("prediction", payload)


def test_documented_persisted_examples_match_the_public_contract() -> None:
    documentation = (Path(__file__).parents[1] / "docs" / "DATA-SCHEMA.md").read_text(
        encoding="utf-8"
    )
    blocks = [
        json.loads(block)
        for block in re.findall(r"```json\n(.*?)\n```", documentation, flags=re.DOTALL)
    ]

    config = next(block for block in blocks if "project" in block)
    observation = next(
        block for block in blocks if "observed_at" in block and "predicted_at" not in block
    )
    prediction = next(block for block in blocks if "predicted_at" in block)

    _validate("config", config)
    _validate("observation", observation)
    _validate("prediction", prediction)
    assert RuleLoomConfig.from_dict(config).to_dict() == config
    assert Observation.from_dict(observation).to_dict() == observation

    parsed_prediction = Prediction.from_dict(prediction)
    parsed_prediction.validate_identity()
    assert parsed_prediction.to_dict() == prediction
