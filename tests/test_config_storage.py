from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event, Thread

import pytest

from ruleloom import storage
from ruleloom.config import (
    CONFIG_PATH,
    EvaluationConfig,
    EvidenceConfig,
    LearnerConfig,
    PromotionConfig,
    ProtocolConfig,
    RuleLoomConfig,
    default_config,
)
from ruleloom.models import (
    Candidate,
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
)
from ruleloom.packs import ConfiguredPathsConfig, PathPredicateConfig
from ruleloom.storage import (
    append_prediction,
    load_candidate,
    load_candidates,
    load_observations,
    load_predictions,
    load_trusted_predictions,
    predictions_path,
    project_path,
    read_json,
    save_candidate,
    save_observations,
    trusted_state_path,
    upsert_observations,
    write_json,
)

TARGET = "needs_extra_validation"
REPOSITORY_ID = "repo.test"
CONFIG_HASH = "c" * 64
TEST_CONFIG = RuleLoomConfig(
    schema_version=1,
    project="StorageTrust",
    pack="flutter_testing",
    pack_version=1,
    protocol=ProtocolConfig(
        experiment_id="test-pilot-v1",
        repository_id=REPOSITORY_ID,
        prediction_unit="git_worktree",
        outcome_definition="synthetic outcome available after prediction",
    ),
)
EVIDENCE_PROTOCOL_HASH = TEST_CONFIG.evidence_protocol_hash


def _observation(
    item_id: str,
    day: int,
    label: LabelValue,
    facts: set[str] | None = None,
) -> Observation:
    observed_at = f"2026-08-{day:02d}T12:00:00+00:00"
    evidence = (
        {}
        if label is LabelValue.UNKNOWN
        else {
            TARGET: LabelEvidence(
                kind="synthetic",
                available_at=f"2026-08-{day:02d}T13:00:00+00:00",
                source="tests",
            )
        }
    )
    return Observation(
        id=item_id,
        observed_at=observed_at,
        protocol_hash=EVIDENCE_PROTOCOL_HASH,
        facts=frozenset(facts or set()),
        labels={TARGET: label},
        label_evidence=evidence,
        source={
            "kind": "git_worktree",
            "repository": REPOSITORY_ID,
            "change_id": item_id,
            "pack": "flutter_testing",
            "extractor": "ruleloom.flutter_testing.git.v1",
        },
    )


def _candidate() -> Candidate:
    rules = RuleSet(
        target=TARGET,
        clauses=(HornClause(TARGET, (RuleLiteral("risk"),)),),
    )
    return Candidate(
        id="cand-storage",
        created_at="2026-08-31T12:00:00Z",
        engine="horn",
        engine_version="test",
        dataset_hash="d" * 64,
        config_hash="c" * 64,
        rules=rules,
        metrics={"test": Metrics.from_counts(3, 1, 4, 1)},
        baselines={"never_alert": Metrics.from_counts(0, 0, 5, 4)},
        stability=0.8,
        train_ids=("train",),
        test_ids=("test",),
    ).with_identity()


def _policies(candidate: Candidate) -> tuple[dict[str, object], ...]:
    return (
        {
            "candidate_id": candidate.id,
            "status": "approved",
            "target": TARGET,
            "manifest_hash": content_hash(candidate.to_dict()),
            "rule_signatures": sorted(candidate.rules.signatures),
        },
    )


def _prediction(
    observation: Observation,
    *,
    predicted_at: str = "2026-08-31T14:00:00Z",
    matched: bool,
) -> Prediction:
    candidate = _candidate()
    policies = _policies(candidate)
    clause = candidate.rules.clauses[0]
    matches = (
        {
            "candidate_id": candidate.id,
            "status": "approved",
            "rule": clause.to_dict(),
            "prolog": clause.to_prolog(),
        },
    )
    protocol = {
        "experiment_id": "test-pilot-v1",
        "repository_id": REPOSITORY_ID,
        "observation_unit": "git_worktree",
        "outcome_definition": "synthetic outcome available after prediction",
        "target": TARGET,
        "pack": "flutter_testing",
        "extractor": "ruleloom.flutter_testing.git.v1",
        "config_hash": CONFIG_HASH,
        "evidence_protocol_hash": EVIDENCE_PROTOCOL_HASH,
    }
    protocol_hash = content_hash(protocol)
    return Prediction(
        id="prediction.pending",
        predicted_at=predicted_at,
        observation=observation,
        target=TARGET,
        unit_id=observation.id,
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
        matches=matches if matched else (),
        abstained=not matched,
    ).with_identity()


def test_config_round_trip_load_and_hash(tmp_path: Path) -> None:
    config = RuleLoomConfig(
        project="ExampleProject",
        target="regression_risk",
        learner=LearnerConfig(max_body=2, bootstrap_runs=7),
        evaluation=EvaluationConfig(
            test_fraction=0.4,
            min_train_examples=4,
            min_test_examples=2,
            seed=23,
        ),
    )
    config_path = tmp_path / CONFIG_PATH
    write_json(config_path, config.to_dict())

    loaded = RuleLoomConfig.load(tmp_path)

    assert loaded == config
    assert loaded.hash == config.hash
    assert RuleLoomConfig.from_dict(config.to_dict()) == config


def test_pack_config_addition_preserves_pre_v3_positional_constructor_order() -> None:
    config = RuleLoomConfig(
        "ExampleProject",
        "needs_extra_validation",
        "generic_changes",
        1,
        ".ruleloom/observations.jsonl",
        ".ruleloom/candidates",
        ".ruleloom/shadow",
        ".ruleloom/approved",
        ".ruleloom/deprecated",
        ".ruleloom/predictions.jsonl",
        ProtocolConfig(),
        EvidenceConfig(),
        LearnerConfig(),
        EvaluationConfig(),
        PromotionConfig(),
        2,
    )

    assert config.schema_version == 2
    assert config.pack_config is None


def test_schema_v1_config_identity_remains_stable() -> None:
    config = RuleLoomConfig(
        schema_version=1,
        project="ExampleProject",
        pack="flutter_testing",
        pack_version=1,
    )

    assert config.schema_version == 1
    assert "pack_version" not in config.to_dict()
    assert "evidence" not in config.to_dict()
    assert config.hash == "c2432c752b22fc5f9c178c65e7e0157d9b2e034dc0d14eee650c8b3459487db3"
    assert (
        config.evidence_protocol_hash
        == "0b8c8c7e25b4975d8c84a3b83dbf160902760c9b7bc2212e80dfc85008697188"
    )
    assert RuleLoomConfig.from_dict(config.to_dict()) == config


def test_constructor_defaults_to_schema_v2_generic_changes() -> None:
    config = RuleLoomConfig(project="ExampleProject")

    assert config == default_config("ExampleProject")
    assert (config.schema_version, config.pack, config.pack_version) == (
        2,
        "generic_changes",
        1,
    )
    assert config.evidence_protocol["extractor"] == "ruleloom.generic_changes.git.v1"
    assert "pack_version" in config.to_dict()
    assert "evidence" in config.to_dict()


def test_schema_v2_binds_pack_version_scope_and_thresholds_to_protocol() -> None:
    baseline = default_config("ExampleProject")
    scoped = replace(
        baseline,
        evidence=EvidenceConfig(
            include_paths=("apps/mobile/**",),
            exclude_paths=("apps/mobile/generated/**",),
            large_change_churn=500,
            multi_file_count=8,
        ),
    )
    flutter = replace(baseline, pack="flutter_testing", pack_version=2)

    assert baseline.schema_version == 2
    assert baseline.pack == "generic_changes"
    assert baseline.to_dict()["pack_version"] == 1
    assert baseline.evidence_protocol_hash != scoped.evidence_protocol_hash
    assert baseline.evidence_protocol_hash != flutter.evidence_protocol_hash
    assert baseline.evidence_protocol["extractor"] == "ruleloom.generic_changes.git.v1"
    assert flutter.evidence_protocol["extractor"] == "ruleloom.flutter_testing.git.v2"
    assert baseline.hash == "d7b8484827c0e74856263ba95b6d436637f27bdff9fc7687e0cba4f621d14c60"
    assert (
        baseline.evidence_protocol_hash
        == "7dbeeeb7220d885bbbb86617915e436712ebb29af24fc8aa65ee79b5850c02f7"
    )


def test_schema_v3_pack_config_is_canonical_hash_bound_and_round_trips() -> None:
    first_pack_config = ConfiguredPathsConfig(
        (
            PathPredicateConfig(
                "touches_surface_web",
                ("apps/web/**", "packages/ui/**"),
                ("apps/web/generated/**",),
            ),
            PathPredicateConfig("touches_shared_contract", ("packages/contracts/**",)),
        )
    )
    reordered_pack_config = ConfiguredPathsConfig(
        (
            PathPredicateConfig("touches_shared_contract", ("packages/contracts/**",)),
            PathPredicateConfig(
                "touches_surface_web",
                ("packages/ui/**", "apps/web/**"),
                ("apps/web/generated/**",),
            ),
        )
    )
    first = RuleLoomConfig(
        schema_version=3,
        project="ExampleProject",
        pack="configured_paths",
        pack_version=1,
        pack_config=first_pack_config,
    )
    reordered = replace(first, pack_config=reordered_pack_config)
    changed = replace(
        first,
        pack_config=ConfiguredPathsConfig(
            (
                PathPredicateConfig("touches_shared_contract", ("contracts/**",)),
                PathPredicateConfig("touches_surface_web", ("apps/web/**",)),
            )
        ),
    )

    assert first == reordered
    assert first.hash == reordered.hash
    assert first.evidence_protocol_hash == reordered.evidence_protocol_hash
    assert first.hash != changed.hash
    assert first.evidence_protocol_hash != changed.evidence_protocol_hash
    assert first.pack_config_hash == first_pack_config.hash
    assert first.evidence_protocol["pack_config"] == first_pack_config.to_dict()
    assert RuleLoomConfig.from_dict(first.to_dict()) == first


def test_schema_v3_static_pack_uses_explicit_empty_pack_config() -> None:
    config = RuleLoomConfig(
        schema_version=3,
        project="ExampleProject",
        pack="generic_changes",
        pack_version=1,
    )

    assert config.to_dict()["pack_config"] == {}
    assert config.evidence_protocol["pack_config"] == {}
    assert RuleLoomConfig.from_dict(config.to_dict()) == config


def test_pack_config_is_required_only_for_configurable_schema_v3_packs() -> None:
    pack_config = ConfiguredPathsConfig(
        (PathPredicateConfig("touches_surface_web", ("apps/web/**",)),)
    )
    with pytest.raises(ModelError, match="schema-v2 configs cannot define pack_config"):
        RuleLoomConfig(
            schema_version=2,
            project="ExampleProject",
            pack="configured_paths",
            pack_version=1,
            pack_config=pack_config,
        )
    with pytest.raises(ModelError, match="requires a valid pack_config"):
        RuleLoomConfig(
            schema_version=3,
            project="ExampleProject",
            pack="configured_paths",
            pack_version=1,
        )
    with pytest.raises(ModelError, match="collides"):
        RuleLoomConfig(
            schema_version=3,
            project="ExampleProject",
            target="touches_surface_web",
            pack="configured_paths",
            pack_version=1,
            pack_config=pack_config,
        )

    static = RuleLoomConfig(
        schema_version=3,
        project="ExampleProject",
        pack="generic_changes",
        pack_version=1,
    ).to_dict()
    static["pack_config"] = {"path_predicates": []}
    with pytest.raises(ModelError, match="does not accept pack_config fields"):
        RuleLoomConfig.from_dict(static)

    configured = RuleLoomConfig(
        schema_version=3,
        project="ExampleProject",
        pack="configured_paths",
        pack_version=1,
        pack_config=pack_config,
    ).to_dict()
    configured.pop("pack_config")
    with pytest.raises(ModelError, match="schema-v3 config is missing required fields"):
        RuleLoomConfig.from_dict(configured)


def test_config_loader_rejects_isolated_unicode_surrogate_in_pack_glob(tmp_path: Path) -> None:
    config = RuleLoomConfig(
        schema_version=3,
        project="ExampleProject",
        pack="configured_paths",
        pack_version=1,
        pack_config=ConfiguredPathsConfig(
            (PathPredicateConfig("touches_surface_web", ("apps/web/**",)),)
        ),
    )
    config_path = tmp_path / CONFIG_PATH
    config_path.parent.mkdir(parents=True)
    content = canonical_json(config.to_dict()).replace("apps/web/**", r"apps/\ud800/**")
    config_path.write_text(content + "\n", encoding="utf-8")

    with pytest.raises(ModelError, match="surrogate"):
        RuleLoomConfig.load(tmp_path)


def test_schema_v2_deserialization_requires_the_complete_persisted_profile() -> None:
    serialized = default_config("ExampleProject").to_dict()
    required_top_level = {
        "project",
        "target",
        "pack",
        "pack_version",
        "dataset",
        "candidates_dir",
        "shadow_dir",
        "approved_dir",
        "deprecated_dir",
        "predictions",
        "protocol",
        "evidence",
        "learner",
        "evaluation",
        "promotion",
    }
    for field in required_top_level:
        incomplete = dict(serialized)
        incomplete.pop(field)
        with pytest.raises(ModelError, match="schema-v2 config is missing required fields"):
            RuleLoomConfig.from_dict(incomplete)

    incomplete_evidence = dict(serialized)
    evidence = dict(serialized["evidence"])  # type: ignore[arg-type]
    evidence.pop("include_paths")
    incomplete_evidence["evidence"] = evidence
    with pytest.raises(ModelError, match="schema-v2 evidence is missing required fields"):
        RuleLoomConfig.from_dict(incomplete_evidence)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"include_paths": "**"},
        {"include_paths": ("apps/**", 7)},
        {"exclude_paths": "generated/**"},
        {"include_paths": ()},
        {"include_paths": ("../outside/**",)},
        {"include_paths": ("/absolute/**",)},
        {"exclude_paths": (":(top)secret/**",)},
        {"large_change_churn": 0},
        {"large_change_churn": True},
        {"multi_file_count": 0},
        {"multi_file_count": "3"},
        {"metadata_file_limit": 0},
        {"metadata_file_limit": 3.5},
    ],
)
def test_evidence_config_rejects_unsafe_profiles(kwargs: dict[str, object]) -> None:
    with pytest.raises(ModelError):
        EvidenceConfig(**kwargs)  # type: ignore[arg-type]


def test_config_rejects_non_integer_pack_version() -> None:
    with pytest.raises(ModelError, match="pack_version"):
        RuleLoomConfig(project="ExampleProject", pack_version="1")  # type: ignore[arg-type]


@pytest.mark.parametrize("schema_version", [True, 1.0, 2.0])
def test_config_rejects_non_integer_schema_version(schema_version: object) -> None:
    with pytest.raises(ModelError, match="schema_version"):
        RuleLoomConfig(
            project="ExampleProject",
            schema_version=schema_version,  # type: ignore[arg-type]
        )
    with pytest.raises(ModelError, match="schema_version"):
        default_config(
            "ExampleProject",
            schema_version=schema_version,  # type: ignore[arg-type]
        )


def test_default_config_does_not_replace_an_explicit_empty_pack() -> None:
    with pytest.raises(ModelError, match="unsupported evidence pack"):
        default_config("ExampleProject", pack="")


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (
            {"schema_version": 1, "project": "ExampleProject", "dataset": "../outside.jsonl"},
            "project-relative",
        ),
        (
            {
                "schema_version": 1,
                "project": "ExampleProject",
                "learner": {"engine": "neural"},
            },
            "learner.engine",
        ),
        (
            {
                "schema_version": 1,
                "project": "ExampleProject",
                "evaluation": {"test_fraction": 1.0},
            },
            "test_fraction",
        ),
        ({"schema_version": True, "project": "ExampleProject"}, "schema_version"),
        ({"schema_version": 1, "project": ""}, "project"),
        ({"schema_version": 1, "project": "ExampleProject\nInjected"}, "control characters"),
        (
            {"schema_version": 1, "project": "ExampleProject", "unexpected": True},
            "unknown config fields",
        ),
        (
            {
                "schema_version": 1,
                "project": "ExampleProject",
                "learner": {"unexpected": True},
            },
            "unknown learner fields",
        ),
        (
            {"schema_version": 1, "project": "ExampleProject", "dataset": ""},
            "non-empty string",
        ),
        (
            {
                "schema_version": 1,
                "project": "ExampleProject",
                "dataset": ".ruleloom/shared.jsonl",
                "predictions": ".ruleloom/shared.jsonl",
            },
            "must not overlap",
        ),
        (
            {
                "schema_version": 1,
                "project": "ExampleProject",
                "dataset": ".ruleloom/shared",
                "predictions": ".ruleloom/shared/",
            },
            "must not overlap",
        ),
        (
            {
                "schema_version": 1,
                "project": "ExampleProject",
                "dataset": ".ruleloom/store/data.jsonl",
                "candidates_dir": ".ruleloom/store",
            },
            "must not overlap",
        ),
        (
            {
                "schema_version": 1,
                "project": "ExampleProject",
                "candidates_dir": ".ruleloom/policies",
                "approved_dir": ".ruleloom/policies/approved",
            },
            "must not overlap",
        ),
        (
            {
                "schema_version": 1,
                "project": "ExampleProject",
                "candidates_dir": ".ruleloom",
            },
            "must not overlap",
        ),
        (
            {
                "schema_version": 1,
                "project": "ExampleProject",
                "dataset": ".ruleloom/history/events.jsonl",
            },
            "must not overlap",
        ),
        (
            {
                "schema_version": 1,
                "project": "ExampleProject",
                "candidates_dir": ".ruleloom/HISTORY",
            },
            "must not overlap",
        ),
    ],
)
def test_config_rejects_unsafe_or_invalid_values(value: dict[str, object], message: str) -> None:
    with pytest.raises(ModelError, match=message):
        RuleLoomConfig.from_dict(value)


def test_config_load_reports_missing_and_invalid_json(tmp_path: Path) -> None:
    with pytest.raises(ModelError, match="not initialized"):
        RuleLoomConfig.load(tmp_path)

    path = tmp_path / CONFIG_PATH
    path.parent.mkdir(parents=True)
    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(ModelError, match="invalid JSON"):
        RuleLoomConfig.load(tmp_path)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_body": 0}, "max_body"),
        ({"max_rules": 11}, "max_rules"),
        ({"min_precision": 1.1}, "min_precision"),
        ({"min_support": 0}, "min_support"),
        ({"false_positive_cost": float("inf")}, "false_positive_cost"),
        ({"bootstrap_runs": 101}, "bootstrap_runs"),
        ({"max_predicates": 33}, "max_predicates"),
        ({"popper_timeout_seconds": 0}, "popper_timeout_seconds"),
        ({"max_body": 4, "max_predicates": 32, "bootstrap_runs": 100}, "search budget"),
        ({"engine": "popper", "max_rules": 2, "bootstrap_runs": 0}, "max_rules"),
        ({"engine": "popper", "max_rules": 1}, "bootstrap_runs"),
        (
            {
                "engine": "popper",
                "max_rules": 1,
                "bootstrap_runs": 0,
                "min_support": 3,
            },
            "built-in Horn settings",
        ),
    ],
)
def test_learner_config_rejects_unsafe_search_profiles(
    kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises(ModelError, match=message):
        LearnerConfig(**kwargs)


def test_protocol_evaluation_and_promotion_configs_are_strict() -> None:
    with pytest.raises(ModelError, match="prediction_unit"):
        ProtocolConfig(prediction_unit="pull_request")
    with pytest.raises(ModelError, match="outcome_definition"):
        ProtocolConfig(outcome_definition="bad\nline")
    with pytest.raises(ModelError, match="minimum sizes"):
        EvaluationConfig(min_train_examples=1)
    with pytest.raises(ModelError, match="min_shadow_mcc"):
        PromotionConfig(min_shadow_mcc=2)
    with pytest.raises(ModelError, match="min_shadow_predictions"):
        PromotionConfig(min_shadow_predictions_for_approval=0)
    with pytest.raises(ModelError, match="predicate_count"):
        LearnerConfig().hypothesis_count(13)


def test_config_rejects_casefolded_and_unicode_normalized_path_collisions() -> None:
    with pytest.raises(ModelError, match="must not overlap"):
        RuleLoomConfig(
            project="ExampleProject",
            dataset=".ruleloom/Data.jsonl",
            predictions=".ruleloom/data.jsonl",
        )
    with pytest.raises(ModelError, match="must not overlap"):
        RuleLoomConfig(
            project="ExampleProject",
            dataset=".ruleloom/caf\N{LATIN SMALL LETTER E WITH ACUTE}.jsonl",
            predictions=".ruleloom/cafe\N{COMBINING ACUTE ACCENT}.jsonl",
        )


def test_observation_storage_is_sorted_round_trips_and_upserts(tmp_path: Path) -> None:
    path = tmp_path / "observations.jsonl"
    later = _observation("later", 3, LabelValue.NEGATIVE, {"safe"})
    earlier = _observation("earlier", 1, LabelValue.POSITIVE, {"risk"})
    middle = _observation("middle", 2, LabelValue.UNKNOWN)

    save_observations(path, [later, earlier])

    assert load_observations(path) == [earlier, later]
    inserted, updated = upsert_observations(path, [middle, earlier])
    assert (inserted, updated) == (1, 0)
    changed = replace(earlier, metadata={"reviewed": True})
    inserted, updated = upsert_observations(path, [changed])
    assert (inserted, updated) == (0, 1)
    assert load_observations(path) == [changed, middle, later]


def test_observation_storage_rejects_duplicates_and_bad_rows(tmp_path: Path) -> None:
    path = tmp_path / "observations.jsonl"
    item = _observation("duplicate", 1, LabelValue.POSITIVE, {"risk"})
    with pytest.raises(ModelError, match="duplicate observation ids"):
        save_observations(path, [item, item])

    serialized = json.dumps(item.to_dict())
    path.write_text(f"{serialized}\n{serialized}\n", encoding="utf-8")
    with pytest.raises(ModelError, match="duplicate observation id"):
        load_observations(path)

    path.write_text("[]\n", encoding="utf-8")
    with pytest.raises(ModelError, match="expected an object"):
        load_observations(path)


def test_candidate_storage_is_immutable(tmp_path: Path) -> None:
    path = tmp_path / "candidate.json"
    candidate = _candidate()

    save_candidate(path, candidate)
    save_candidate(path, candidate)
    assert load_candidate(path) == candidate

    with pytest.raises(ModelError, match="refusing to overwrite immutable"):
        save_candidate(path, replace(candidate, stability=0.1).with_identity())


def test_prediction_storage_appends_round_trips_and_rejects_duplicate_ids(
    tmp_path: Path,
) -> None:
    path = tmp_path / "predictions.jsonl"
    observation = _observation("pending-change", 31, LabelValue.UNKNOWN, {"risk"})
    prediction = _prediction(observation, matched=True)

    append_prediction(path, prediction)

    assert load_predictions(path) == [prediction]
    with pytest.raises(ModelError, match="already exists"):
        append_prediction(path, prediction)


def test_prediction_storage_serializes_concurrent_appends(tmp_path: Path) -> None:
    path = tmp_path / "predictions.jsonl"
    observation = _observation("pending-concurrent", 31, LabelValue.UNKNOWN, {"risk"})
    first = _prediction(observation, predicted_at="2026-08-31T14:00:00Z", matched=False)
    second = _prediction(observation, predicted_at="2026-08-31T14:00:01Z", matched=False)

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda item: append_prediction(path, item), [first, second]))

    assert {item.id for item in load_predictions(path)} == {first.id, second.id}


def _git_project(tmp_path: Path, name: str = "repo") -> tuple[Path, RuleLoomConfig, Path]:
    root = tmp_path / name
    root.mkdir()
    subprocess.run(
        ("git", "init", "-q", str(root)),
        check=True,
        capture_output=True,
        text=True,
    )
    config = TEST_CONFIG
    return root, config, predictions_path(root, config)


def _prediction_ledger_dir(root: Path, config: RuleLoomConfig, path: Path) -> Path:
    key = storage._prediction_ledger_key(root, path, config.evidence_protocol_hash)
    return trusted_state_path(root) / "prediction-ledgers" / key


def _recorded_at(prediction: Prediction) -> datetime:
    return datetime.fromisoformat(prediction.predicted_at.replace("Z", "+00:00")) + timedelta(
        seconds=1
    )


def _append_trusted_series(
    root: Path,
    config: RuleLoomConfig,
    count: int,
) -> tuple[Path, list[Prediction]]:
    path = predictions_path(root, config)
    predictions: list[Prediction] = []
    for index in range(count):
        observation = _observation(
            f"pending-ledger-{index}",
            31,
            LabelValue.UNKNOWN,
            {"risk"},
        )
        prediction = _prediction(
            observation,
            predicted_at=f"2026-08-31T14:00:{index:02d}Z",
            matched=False,
        )
        append_prediction(
            path,
            prediction,
            root=root,
            recorded_at=_recorded_at(prediction),
        )
        predictions.append(prediction)
    return path, predictions


def test_trusted_prediction_append_recovers_failure_after_log_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, config, path = _git_project(tmp_path)
    prediction = _prediction(
        _observation("pending-recovery", 31, LabelValue.UNKNOWN, {"risk"}),
        matched=False,
    )
    original = storage.record_prediction_attestation
    calls = 0

    def fail_once(
        attestation_root: Path,
        item: Prediction,
        *,
        ledger_key: str,
        sequence: int,
        previous: str,
        recorded_at: datetime | None = None,
    ) -> Path:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated attestation write failure")
        return original(
            attestation_root,
            item,
            ledger_key=ledger_key,
            sequence=sequence,
            previous=previous,
            recorded_at=recorded_at,
        )

    monkeypatch.setattr(storage, "record_prediction_attestation", fail_once)

    with pytest.raises(OSError, match="simulated attestation write failure"):
        append_prediction(
            path,
            prediction,
            root=root,
            recorded_at=_recorded_at(prediction),
        )

    ledger = _prediction_ledger_dir(root, config, path)
    transaction = ledger / "transaction.json"
    attestation = ledger / "records" / f"{prediction.id}.json"
    assert load_predictions(path) == [prediction]
    assert transaction.is_file()
    assert not attestation.exists()

    assert load_trusted_predictions(root, config) == [prediction]
    assert attestation.is_file()
    assert not transaction.exists()


def test_trusted_prediction_append_recovers_failure_before_log_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, config, path = _git_project(tmp_path)
    prediction = _prediction(
        _observation("pending-prewrite-recovery", 31, LabelValue.UNKNOWN, {"risk"}),
        matched=False,
    )
    original = storage._atomic_write
    failed = False

    def fail_log_once(destination: Path, content: str) -> None:
        nonlocal failed
        if destination == path and not failed:
            failed = True
            raise OSError("simulated prediction log write failure")
        original(destination, content)

    monkeypatch.setattr(storage, "_atomic_write", fail_log_once)

    with pytest.raises(OSError, match="simulated prediction log write failure"):
        append_prediction(
            path,
            prediction,
            root=root,
            recorded_at=_recorded_at(prediction),
        )

    transaction = _prediction_ledger_dir(root, config, path) / "transaction.json"
    assert load_predictions(path) == []
    assert transaction.is_file()

    assert load_trusted_predictions(root, config) == [prediction]
    assert not transaction.exists()


@pytest.mark.parametrize(
    ("tampering", "message"),
    [
        ("delete", "prediction log and trusted local records differ"),
        ("reorder", "does not match its trusted local attestation"),
        ("inject", "prediction log and trusted local records differ"),
    ],
)
def test_trusted_prediction_ledger_detects_log_tampering(
    tmp_path: Path,
    tampering: str,
    message: str,
) -> None:
    root, config, _ = _git_project(tmp_path)
    path, predictions = _append_trusted_series(root, config, 3)
    lines = path.read_text(encoding="utf-8").splitlines()

    if tampering == "delete":
        del lines[1]
    elif tampering == "reorder":
        lines[0], lines[1] = lines[1], lines[0]
    else:
        injected = _prediction(
            _observation("pending-injected", 31, LabelValue.UNKNOWN, {"risk"}),
            predicted_at="2026-08-31T14:00:03Z",
            matched=False,
        )
        assert injected.id not in {item.id for item in predictions}
        lines.append(canonical_json(injected.to_dict()))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ModelError, match=message):
        load_trusted_predictions(root, config)


def test_prediction_capacity_cap_is_enforced_without_corrupting_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, config, _ = _git_project(tmp_path)
    monkeypatch.setattr(storage, "_MAX_PREDICTION_RECORDS", 2)
    path, predictions = _append_trusted_series(root, config, 2)
    overflow = _prediction(
        _observation("pending-overflow", 31, LabelValue.UNKNOWN, {"risk"}),
        predicted_at="2026-08-31T14:00:02Z",
        matched=False,
    )

    with pytest.raises(ModelError, match="2-record safety cap"):
        append_prediction(
            path,
            overflow,
            root=root,
            recorded_at=_recorded_at(overflow),
        )

    assert load_trusted_predictions(root, config) == predictions
    assert not (_prediction_ledger_dir(root, config, path) / "transaction.json").exists()


def test_trusted_state_survives_moving_the_checkout(tmp_path: Path) -> None:
    root, config, _ = _git_project(tmp_path, "original")
    _, predictions = _append_trusted_series(root, config, 1)
    original_relative = trusted_state_path(root).relative_to(root)
    moved = tmp_path / "moved"

    root.rename(moved)

    assert trusted_state_path(moved).relative_to(moved) == original_relative
    assert load_trusted_predictions(moved, config) == predictions


def test_prediction_trust_is_namespaced_by_experiment_and_log(tmp_path: Path) -> None:
    root, first_config, _ = _git_project(tmp_path)
    first_path, first_predictions = _append_trusted_series(root, first_config, 1)
    second_config = replace(
        first_config,
        dataset=".ruleloom/observations-v2.jsonl",
        predictions=".ruleloom/predictions-v2.jsonl",
        protocol=replace(
            first_config.protocol,
            experiment_id="test-pilot-v2",
            outcome_definition="a new prospective outcome contract",
        ),
    )
    second_path = predictions_path(root, second_config)

    assert load_trusted_predictions(root, second_config) == []
    assert _prediction_ledger_dir(root, first_config, first_path) != _prediction_ledger_dir(
        root, second_config, second_path
    )
    assert load_trusted_predictions(root, first_config) == first_predictions


def test_managed_and_trusted_paths_reject_symlink_artifacts(tmp_path: Path) -> None:
    root, config, _ = _git_project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    managed = root / "managed"
    managed.mkdir()
    (managed / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ModelError, match="managed-path symlink"):
        project_path(root, "managed/escape/artifact.json")

    candidates = root / config.candidates_dir
    candidates.mkdir(parents=True)
    outside_candidate = outside / "candidate.json"
    outside_candidate.write_text("{}\n", encoding="utf-8")
    (candidates / "candidate.json").symlink_to(outside_candidate)
    with pytest.raises(ModelError, match="symlink"):
        load_candidates(root, config)

    (candidates / "candidate.json").unlink()
    records = _prediction_ledger_dir(root, config, predictions_path(root, config)) / "records"
    records.mkdir(parents=True)
    outside_record = outside / "prediction.record.json"
    outside_record.write_text("{}\n", encoding="utf-8")
    (records / "prediction.record.json").symlink_to(outside_record)
    with pytest.raises(ModelError, match="not a regular file"):
        load_trusted_predictions(root, config)


def test_storage_lock_refuses_final_symlink_without_touching_target(tmp_path: Path) -> None:
    path = tmp_path / "observations.jsonl"
    victim = tmp_path / "victim.txt"
    victim.write_text("unchanged\n", encoding="utf-8")
    path.with_name(f".{path.name}.lock").symlink_to(victim)

    with pytest.raises(ModelError, match="safely open storage lock"):
        save_observations(path, [_observation("obs.safe", 1, LabelValue.UNKNOWN)])

    assert victim.read_text(encoding="utf-8") == "unchanged\n"


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ('{"schema_version":1,"schema_version":1}\n', "duplicate object key"),
        ('{"value":NaN}\n', "invalid numeric constant NaN"),
    ],
)
def test_storage_uses_strict_json_decoding(
    tmp_path: Path,
    content: str,
    message: str,
) -> None:
    path = tmp_path / "strict.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ModelError, match=message):
        read_json(path)

    jsonl_path = tmp_path / "strict.jsonl"
    jsonl_path.write_text(content, encoding="utf-8")
    with pytest.raises(ModelError, match=message):
        load_observations(jsonl_path)


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
def test_jsonl_round_trip_preserves_unicode_non_record_separators(
    tmp_path: Path,
    separator: str,
) -> None:
    observation = replace(
        _observation("obs.unicode", 1, LabelValue.UNKNOWN, {"risk"}),
        metadata={"path": f"apps{separator}web/file.ts"},
    )
    observation_path = tmp_path / "observations.jsonl"
    save_observations(observation_path, [observation])

    assert load_observations(observation_path) == [observation]

    prediction = _prediction(observation, matched=True)
    prediction_path = tmp_path / "predictions.jsonl"
    prediction_path.write_text(
        canonical_json(prediction.to_dict()) + "\n",
        encoding="utf-8",
    )

    assert load_predictions(prediction_path) == [prediction]


def test_live_lock_owner_is_not_evicted_when_lock_file_looks_stale(tmp_path: Path) -> None:
    path = tmp_path / "locked.jsonl"
    entered = Event()
    release = Event()
    errors: list[BaseException] = []

    def hold_lock() -> None:
        try:
            with storage._file_lock(path, timeout_seconds=1):
                entered.set()
                release.wait(timeout=5)
        except BaseException as exc:
            errors.append(exc)

    owner = Thread(target=hold_lock)
    owner.start()
    assert entered.wait(timeout=1)
    lock_path = path.with_name(f".{path.name}.lock")
    os.utime(lock_path, (0, 0))

    try:
        with (
            pytest.raises(ModelError, match="timed out waiting for storage lock"),
            storage._file_lock(path, timeout_seconds=0.1),
        ):
            pytest.fail("a live lock owner must not be evicted")
    finally:
        release.set()
        owner.join(timeout=2)

    assert not owner.is_alive()
    assert errors == []
    with storage._file_lock(path, timeout_seconds=0.1):
        pass
