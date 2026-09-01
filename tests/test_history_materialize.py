from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from ruleloom.config import RuleLoomConfig
from ruleloom.history.materialize import (
    materialize_history,
    resolve_outcome_target,
    validate_materialized_outcome,
)
from ruleloom.history.models import ChangeUnit, HistoricalEvent
from ruleloom.history.outcomes import POST_MERGE_DEFECT, VALIDATION_REWORK_REQUIRED
from ruleloom.models import LabelEvidence, LabelValue, ModelError
from ruleloom.project import initialize_project, validate_observations


def _git(repo: Path, *arguments: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout.strip()


def _commit(repo: Path, filename: str, timestamp: str) -> str:
    (repo / filename).write_text(filename + "\n", encoding="utf-8")
    _git(repo, "add", "--", filename)
    _git(
        repo,
        "commit",
        "-m",
        f"change {filename}",
        env={
            **os.environ,
            "GIT_AUTHOR_DATE": timestamp,
            "GIT_COMMITTER_DATE": timestamp,
        },
    )
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def materialization_repo(tmp_path: Path) -> tuple[Path, RuleLoomConfig, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    base = _commit(repo, "base.txt", "2025-01-01T00:00:00Z")
    head = _commit(repo, "feature.txt", "2025-01-02T00:00:00Z")
    initialize_project(repo, "HistoryTest")
    return repo, RuleLoomConfig.load(repo), base, head


def _event(
    identifier: str,
    kind: str,
    timestamp: str,
    data: dict[str, object],
) -> HistoricalEvent:
    return HistoricalEvent(
        id=identifier,
        repository_id="placeholder",
        kind=kind,
        occurred_at=timestamp,
        available_at=timestamp,
        provider="forge",
        source_ref=identifier,
        change_id="pr-1",
        independent_group="review-1",
        data=data,  # type: ignore[arg-type]
    )


def test_materializes_point_in_time_facts_and_strong_outcome(
    materialization_repo: tuple[Path, RuleLoomConfig, str, str],
) -> None:
    repo, config, base, head = materialization_repo
    snapshot = replace(
        _event(
            "snapshot-1",
            "change_snapshot",
            "2025-01-02T01:00:00Z",
            {"base_sha": base, "head_sha": head, "point_in_time": True},
        ),
        repository_id=config.protocol.repository_id,
    )
    review = replace(
        _event(
            "review-1",
            "review",
            "2025-01-03T00:00:00Z",
            {
                "decision": "changes_requested",
                "category": "validation",
                "independent": True,
            },
        ),
        repository_id=config.protocol.repository_id,
        change_id=None,
    )
    unit = ChangeUnit(
        id="pr-1",
        repository_id=config.protocol.repository_id,
        kind="provider_change",
        base_sha=base,
        prediction_sha=head,
        prediction_at=snapshot.occurred_at,
        commits=(head,),
        event_ids=(snapshot.id, review.id),
        provider="forge",
        source_ref=snapshot.source_ref,
        evidence_quality="rich",
        confirmatory=True,
    )

    report = materialize_history(repo, config, [unit], [snapshot, review])

    assert report.positive == report.confirmatory == 1
    assert report.outcome_target == VALIDATION_REWORK_REQUIRED
    observation = report.observations[0]
    assert observation.id == "history.pr-1"
    assert observation.observed_at == snapshot.occurred_at
    assert observation.labels[config.target] is LabelValue.POSITIVE
    assert observation.source["kind"] == "historical_change"
    assert observation.source["confirmatory"] is True
    assert observation.source["repository"] == config.protocol.repository_id
    assert "topological_index" not in observation.metadata
    validate_observations([observation], config)
    forged = replace(
        observation,
        label_evidence={
            config.target: LabelEvidence(
                kind="synthetic",
                available_at=review.available_at,
                source="manual/override",
                reason="not derived from the event log",
            )
        },
    )
    with pytest.raises(ModelError, match="recomputed outcome evidence"):
        validate_materialized_outcome(config, forged, unit, [snapshot, review])


def test_weak_label_is_opt_in_and_never_confirmatory(
    materialization_repo: tuple[Path, RuleLoomConfig, str, str],
) -> None:
    repo, config, base, head = materialization_repo
    snapshot = replace(
        _event(
            "snapshot-prediction",
            "change_snapshot",
            "2025-01-02T01:00:00Z",
            {
                "base_sha": base,
                "head_sha": head,
                "point_in_time": True,
            },
        ),
        repository_id=config.protocol.repository_id,
    )
    weak = replace(
        _event(
            "snapshot-tests",
            "change_snapshot",
            "2025-01-03T00:00:00Z",
            {"test_changed": True},
        ),
        repository_id=config.protocol.repository_id,
    )
    unit = ChangeUnit(
        id="pr-1",
        repository_id=config.protocol.repository_id,
        kind="provider_change",
        base_sha=base,
        prediction_sha=head,
        prediction_at="2025-01-02T01:00:00Z",
        commits=(head,),
        event_ids=(snapshot.id, weak.id),
        provider="forge",
        source_ref=snapshot.source_ref,
        evidence_quality="rich",
        confirmatory=True,
    )

    conservative = materialize_history(repo, config, [unit], [snapshot, weak])
    opted_in = materialize_history(repo, config, [unit], [snapshot, weak], include_weak=True)

    assert conservative.unknown == 1
    assert opted_in.positive == 1
    assert opted_in.confirmatory == 0
    assert opted_in.observations[0].source["confirmatory"] is False


def test_target_resolution_and_repository_boundary(
    materialization_repo: tuple[Path, RuleLoomConfig, str, str],
) -> None:
    repo, config, base, head = materialization_repo
    assert resolve_outcome_target("needs_extra_validation") == VALIDATION_REWORK_REQUIRED
    assert resolve_outcome_target(POST_MERGE_DEFECT, POST_MERGE_DEFECT) == POST_MERGE_DEFECT
    with pytest.raises(ModelError, match="does not match the frozen"):
        resolve_outcome_target("needs_extra_validation", POST_MERGE_DEFECT)
    with pytest.raises(ModelError, match="no registered historical outcome"):
        resolve_outcome_target("custom_target")
    foreign = ChangeUnit(
        id="change-1",
        repository_id="repository.other",
        kind="git_only",
        base_sha=base,
        prediction_sha=head,
        prediction_at="2025-01-02T00:00:00Z",
        commits=(head,),
        event_ids=(),
        provider="git",
        source_ref=head,
        evidence_quality="git_only",
        confirmatory=False,
    )
    with pytest.raises(ModelError, match="belongs to repository"):
        materialize_history(repo, config, [foreign], [])

    local = replace(foreign, repository_id=config.protocol.repository_id)
    foreign_event = replace(
        _event("foreign", "review", "2025-01-03T00:00:00Z", {}),
        repository_id="repository.other",
    )
    with pytest.raises(ModelError, match=r"historical event.*belongs to repository"):
        materialize_history(repo, config, [local], [foreign_event])


def test_materialization_rejects_duplicate_record_ids(
    materialization_repo: tuple[Path, RuleLoomConfig, str, str],
) -> None:
    repo, config, base, head = materialization_repo
    unit = ChangeUnit(
        id="change-1",
        repository_id=config.protocol.repository_id,
        kind="git_only",
        base_sha=base,
        prediction_sha=head,
        prediction_at="2025-01-02T00:00:00Z",
        commits=(head,),
        event_ids=(),
        provider="git",
        source_ref=head,
        evidence_quality="git_only",
        confirmatory=False,
    )
    event = replace(
        _event("event-1", "review", "2025-01-03T00:00:00Z", {}),
        repository_id=config.protocol.repository_id,
        change_id=unit.id,
    )

    with pytest.raises(ModelError, match="events must have unique ids"):
        materialize_history(repo, config, [unit], [event, event])
    with pytest.raises(ModelError, match="change units must have unique ids"):
        materialize_history(repo, config, [unit, unit], [event])

    linked = replace(unit, event_ids=(event.id,))
    with pytest.raises(ModelError, match="missing historical event"):
        materialize_history(repo, config, [replace(linked, event_ids=("event.missing",))], [event])
    with pytest.raises(ModelError, match="not owned by change unit"):
        materialize_history(
            repo,
            config,
            [linked],
            [replace(event, change_id="change-other")],
        )


def test_materialization_manifest_is_independent_of_input_unit_order(
    materialization_repo: tuple[Path, RuleLoomConfig, str, str],
) -> None:
    repo, config, base, head = materialization_repo
    first = ChangeUnit(
        id="change-1",
        repository_id=config.protocol.repository_id,
        kind="git_only",
        base_sha=base,
        prediction_sha=head,
        prediction_at="2025-01-02T00:00:00Z",
        commits=(head,),
        event_ids=(),
        provider="git",
        source_ref=head,
        evidence_quality="git_only",
        confirmatory=False,
    )
    second = replace(first, id="change-2")

    forward = materialize_history(repo, config, [first, second], [])
    reverse = materialize_history(repo, config, [second, first], [])

    assert forward.manifest_hash == reverse.manifest_hash
    assert forward.observations == reverse.observations
