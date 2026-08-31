from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from ruleloom.models import (
    HornClause,
    LabelEvidence,
    LabelValue,
    ModelError,
    Observation,
    Prediction,
    RuleLiteral,
    content_hash,
)
from ruleloom.reporting import build_pilot_report, build_pilot_reports

TARGET = "needs_extra_validation"
EXPERIMENT_ID = "ruleloom-pilot-v1"
REPOSITORY_ID = "repository.example"
PACK = "flutter_testing"
EXTRACTOR = "ruleloom.gitfacts/flutter_testing@1"
CONFIG_HASH = "c" * 64
EVIDENCE_PROTOCOL_HASH = "e" * 64
OUTCOME_DEFINITION = "Whether the change needs extra validation after prospective review."


def _protocol() -> dict[str, object]:
    return {
        "experiment_id": EXPERIMENT_ID,
        "repository_id": REPOSITORY_ID,
        "observation_unit": "git_commit",
        "outcome_definition": OUTCOME_DEFINITION,
        "target": TARGET,
        "pack": PACK,
        "extractor": EXTRACTOR,
        "config_hash": CONFIG_HASH,
        "evidence_protocol_hash": EVIDENCE_PROTOCOL_HASH,
    }


def _snapshot(item_id: str, *, unit_id: str | None = None) -> Observation:
    return Observation(
        id=item_id,
        observed_at="2026-01-01T08:00:00Z",
        protocol_hash=EVIDENCE_PROTOCOL_HASH,
        facts=frozenset({"changes_dart"}),
        labels={TARGET: LabelValue.UNKNOWN},
        source={
            "kind": "git_commit",
            "repository": REPOSITORY_ID,
            "pack": PACK,
            "extractor": EXTRACTOR,
            "change_id": unit_id or item_id,
        },
    )


def _outcome(
    item_id: str,
    value: LabelValue,
    *,
    available_at: str | None = None,
) -> Observation:
    snapshot = _snapshot(item_id)
    if value is LabelValue.UNKNOWN:
        return snapshot
    assert available_at is not None
    return snapshot.with_label(
        TARGET,
        value,
        LabelEvidence(
            kind="human",
            available_at=available_at,
            source="pilot-review",
            reason="mature outcome",
        ),
    )


def _prediction(
    item_id: str,
    *,
    predicted_at: str,
    matched: bool,
    suffix: str = "first",
    manifest_hash: str = "a" * 64,
    unit_id: str | None = None,
) -> Prediction:
    del suffix  # Identity is derived from immutable prediction content.
    clause = HornClause(TARGET, (RuleLiteral("changes_dart"),))
    policies = (
        {
            "candidate_id": "cand-0123456789abcdef",
            "status": "approved",
            "target": TARGET,
            "manifest_hash": manifest_hash,
            "rule_signatures": [clause.signature],
        },
    )
    matches = (
        {
            "candidate_id": "cand-0123456789abcdef",
            "status": "approved",
            "rule": clause.to_dict(),
            "prolog": clause.to_prolog(),
        },
    )
    protocol = _protocol()
    protocol_hash = content_hash(protocol)
    return Prediction(
        id="prediction.pending",
        predicted_at=predicted_at,
        observation=_snapshot(item_id, unit_id=unit_id),
        target=TARGET,
        unit_id=unit_id or item_id,
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


def _range_prediction(
    unit_id: str,
    base: str,
    head: str,
    *,
    observation_id: str | None = None,
) -> Prediction:
    prediction = _prediction(
        observation_id or unit_id,
        predicted_at="2026-01-01T10:00:00Z",
        matched=False,
        unit_id=unit_id,
    )
    observation = replace(
        prediction.observation,
        source={
            **prediction.observation.source,
            "kind": "git_range",
            "change_id": unit_id,
            "base": base,
            "head": head,
        },
    )
    protocol = {**prediction.protocol, "observation_unit": "git_range"}
    protocol_hash = content_hash(protocol)
    return replace(
        prediction,
        observation=observation,
        unit_id=unit_id,
        protocol=protocol,
        protocol_hash=protocol_hash,
        policy_set_hash=content_hash(
            {
                "protocol_hash": protocol_hash,
                "target": TARGET,
                "policies": list(prediction.policies),
            }
        ),
    ).with_identity()


def test_report_uses_only_earliest_prospective_prediction_and_mature_outcomes() -> None:
    observations = [
        _outcome("obs.pos", LabelValue.POSITIVE, available_at="2026-01-01T11:00:00Z"),
        _outcome("obs.neg", LabelValue.NEGATIVE, available_at="2026-01-01T11:00:00Z"),
        _outcome("obs.leaked", LabelValue.POSITIVE, available_at="2026-01-01T09:00:00Z"),
        _outcome("obs.open", LabelValue.UNKNOWN),
        _outcome("obs.missing", LabelValue.UNKNOWN),
    ]
    predictions = [
        _prediction("obs.pos", predicted_at="2026-01-01T10:00:00Z", matched=True, suffix="early"),
        _prediction("obs.pos", predicted_at="2026-01-01T10:30:00Z", matched=False, suffix="late"),
        _prediction("obs.neg", predicted_at="2026-01-01T10:00:00Z", matched=False),
        _prediction("obs.leaked", predicted_at="2026-01-01T10:00:00Z", matched=True),
        _prediction("obs.open", predicted_at="2026-01-01T10:00:00Z", matched=False),
        _prediction("obs.missing", predicted_at="2026-01-01T10:00:00Z", matched=True),
    ]

    report = build_pilot_report(observations, predictions, TARGET)

    assert report.predictions == 6
    assert report.unique_observations == 5
    assert report.duplicate_predictions == 1
    assert report.mature_after_prediction == 2
    assert report.still_unknown == 2
    assert report.excluded_preexisting_outcome == 1
    assert report.matched == 3
    assert report.abstained == 2
    assert report.evaluated_matched == 1
    assert report.evaluated_abstained == 1
    assert report.metrics.true_positive == 1
    assert report.metrics.true_negative == 1
    assert report.metrics.false_positive == 0
    assert report.metrics.false_negative == 0
    assert report.metrics.precision == 1.0
    assert report.metrics.recall == 1.0
    assert report.metrics.matthews_correlation == 1.0

    payload = report.to_dict()
    assert payload["readiness"]["positive"] == 2
    assert payload["readiness"]["negative"] == 1
    assert payload["readiness"]["unknown"] == 2
    assert payload["readiness"]["label_evidence_coverage"] == 1.0
    assert payload["prospective_metrics"] == report.metrics.to_dict()
    assert "not a causal estimate" in payload["interpretation"]


def test_report_compares_label_availability_as_instants_not_iso_text() -> None:
    observation = _outcome(
        "obs.offset",
        LabelValue.POSITIVE,
        available_at="2026-01-02T00:30:00+02:00",
    )
    prediction = _prediction(
        "obs.offset",
        predicted_at="2026-01-01T23:00:00Z",
        matched=True,
    )

    report = build_pilot_report([observation], [prediction], TARGET)

    # The outcome became available at 22:30Z, before the 23:00Z prediction.
    assert report.excluded_preexisting_outcome == 1
    assert report.mature_after_prediction == 0
    assert report.matched == 1
    assert report.evaluated_matched == 0


def test_report_excludes_outcome_available_at_exact_prediction_time() -> None:
    observation = _outcome(
        "obs.equal",
        LabelValue.NEGATIVE,
        available_at="2026-01-01T10:00:00Z",
    )
    prediction = _prediction(
        "obs.equal",
        predicted_at="2026-01-01T10:00:00Z",
        matched=False,
    )

    report = build_pilot_report([observation], [prediction], TARGET)

    assert report.excluded_preexisting_outcome == 1
    assert report.mature_after_prediction == 0
    assert report.still_unknown == 0


def test_report_joins_later_snapshot_outcome_to_first_prediction_of_unit() -> None:
    unit_id = "pr-123"
    first_snapshot = _snapshot("obs.pr-123.first", unit_id=unit_id)
    final_snapshot = replace(
        _outcome(
            "obs.pr-123.final",
            LabelValue.POSITIVE,
            available_at="2026-01-01T12:00:00Z",
        ),
        source={**_snapshot("obs.pr-123.final").source, "change_id": unit_id},
    )
    prediction = _prediction(
        first_snapshot.id,
        predicted_at="2026-01-01T10:00:00Z",
        matched=True,
        unit_id=unit_id,
    )

    report = build_pilot_report([first_snapshot, final_snapshot], [prediction], TARGET)

    assert report.unique_observations == 1
    assert report.mature_after_prediction == 1
    assert report.metrics.true_positive == 1


def test_report_rejects_bool_as_later_snapshot_pack_version() -> None:
    unit_id = "pr-versioned"
    original = _prediction(
        "obs.versioned.first",
        predicted_at="2026-01-01T10:00:00Z",
        matched=True,
        unit_id=unit_id,
    )
    first_snapshot = replace(
        original.observation,
        source={**original.observation.source, "pack_version": 1},
    )
    prediction = replace(original, observation=first_snapshot).with_identity()
    later = replace(
        _outcome(
            "obs.versioned.final",
            LabelValue.POSITIVE,
            available_at="2026-01-01T12:00:00Z",
        ),
        source={
            **_snapshot("obs.versioned.final").source,
            "change_id": unit_id,
            "pack_version": True,
        },
    )

    with pytest.raises(ModelError, match="mixes fact pack versions"):
        build_pilot_report([first_snapshot, later], [prediction], TARGET)


def test_reports_never_pool_different_policy_sets() -> None:
    first = _prediction("obs.first", predicted_at="2026-01-01T10:00:00Z", matched=True)
    second = _prediction(
        "obs.second",
        predicted_at="2026-01-01T10:00:00Z",
        matched=False,
        manifest_hash="b" * 64,
    )

    with pytest.raises(ModelError, match="mixes policy sets"):
        build_pilot_report([], [first, second], TARGET)
    grouped = build_pilot_reports(
        [_snapshot("obs.first"), _snapshot("obs.second")],
        [first, second],
        TARGET,
    )
    assert set(grouped) == {first.policy_set_hash, second.policy_set_hash}


def test_git_range_reports_require_root_and_reject_overlapping_commits(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    commits: list[str] = []
    for index in range(3):
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=RuleLoom Tests",
                "-c",
                "user.email=tests@example.invalid",
                "commit",
                "--allow-empty",
                "-m",
                f"commit {index}",
            ],
            check=True,
            capture_output=True,
        )
        commits.append(
            subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
    first = _range_prediction("change-one", commits[0], commits[2])
    second = _range_prediction("change-two", commits[1], commits[2])

    with pytest.raises(ModelError, match="requires the repository root"):
        build_pilot_report([], [first], TARGET)
    with pytest.raises(ModelError, match="ranges overlap"):
        build_pilot_report([], [first, second], TARGET, root=repo)

    repeated_same_unit = _range_prediction(
        "change-one",
        commits[1],
        commits[2],
        observation_id="range.change-one.iteration-two",
    )
    report = build_pilot_report(
        [first.observation, repeated_same_unit.observation],
        [first, repeated_same_unit],
        TARGET,
        root=repo,
    )
    assert report.unique_observations == 1
    assert report.duplicate_predictions == 1
