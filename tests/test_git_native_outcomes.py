from __future__ import annotations

import json
import os
import subprocess
from importlib.resources import files
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from ruleloom import cli
from ruleloom.config import OutcomesConfig, RuleLoomConfig, default_config
from ruleloom.history.git import GIT_HISTORY_ADAPTER_VERSION, collect_git_history
from ruleloom.history.materialize import materialize_history, resolve_git_window
from ruleloom.history.models import ChangeUnit, HistoricalEvent
from ruleloom.history.outcomes import (
    GIT_HISTORY_HORIZON_EVENT_KIND,
    GIT_TRAILER_LINK_KIND,
    POST_MERGE_REVERT_OR_HOTFIX,
    GitWindow,
    derive_outcome,
    git_window_from_events,
)
from ruleloom.history.storage import (
    change_units_path,
    events_path,
    load_history_snapshot,
    upsert_history_batch,
)
from ruleloom.models import LabelValue, ModelError
from ruleloom.project import initialize_project, validate_project
from ruleloom.storage import dataset_path, load_observations

REPOSITORY_ID = "repo.git-native"
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_R = "c" * 40


def _git(repo: Path, *arguments: str, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return completed.stdout.strip()


def _dated_env(timestamp: str) -> dict[str, str]:
    return {**os.environ, "GIT_AUTHOR_DATE": timestamp, "GIT_COMMITTER_DATE": timestamp}


def _commit(repo: Path, filename: str, timestamp: str) -> str:
    (repo / filename).write_text(f"payload {filename}\n", encoding="utf-8")
    _git(repo, "add", "--", filename)
    _git(repo, "commit", "-m", f"change {filename}", env=_dated_env(timestamp))
    return _git(repo, "rev-parse", "HEAD")


def _revert(repo: Path, sha: str, timestamp: str) -> str:
    _git(repo, "revert", "--no-edit", sha, env=_dated_env(timestamp))
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def revert_repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    repo = tmp_path / "revert-repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Git Native")
    _git(repo, "config", "user.email", "native@example.invalid")
    first = _commit(repo, "a.txt", "2025-01-01T00:00:00Z")
    second = _commit(repo, "b.txt", "2025-01-02T00:00:00Z")
    reverting = _revert(repo, second, "2025-01-03T00:00:00Z")
    later = _commit(repo, "c.txt", "2025-03-01T00:00:00Z")
    return repo, {"first": first, "second": second, "reverting": reverting, "later": later}


def test_bootstrap_emits_exact_revert_trailer_and_history_horizon_events(
    revert_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, shas = revert_repo

    report = collect_git_history(repo, max_commits=None, repository_id=REPOSITORY_ID)
    again = collect_git_history(repo, max_commits=None, repository_id=REPOSITORY_ID)

    assert report == again
    assert report.examined == report.unit_count == 4
    assert report.event_count == 6
    assert report.revert_events == 1
    assert report.horizon_at == "2025-03-01T00:00:00Z"
    assert report.to_dict()["adapter"] == GIT_HISTORY_ADAPTER_VERSION
    assert report.to_dict()["revert_events"] == 1

    revert = next(event for event in report.events if event.kind == "revert")
    assert revert.id == f"event.git_revert.{shas['reverting']}.{shas['second']}"
    assert revert.change_id == f"change.git_commit.{shas['second']}"
    assert revert.provider == "git"
    assert revert.source_ref == shas["reverting"]
    assert revert.occurred_at == revert.available_at == "2025-01-03T00:00:00Z"
    assert revert.independent_group == f"change.git_commit.{shas['reverting']}"
    assert revert.data["link_kind"] == GIT_TRAILER_LINK_KIND
    assert revert.data["evidence_grade"] == "weak_heuristic"
    assert revert.data["reverted_sha"] == shas["second"]
    assert revert.data["linked_change_id"] == revert.change_id
    assert revert.data["adapter"] == GIT_HISTORY_ADAPTER_VERSION

    horizon = next(event for event in report.events if event.kind == GIT_HISTORY_HORIZON_EVENT_KIND)
    assert horizon.change_id is None
    assert horizon.occurred_at == horizon.available_at == report.horizon_at
    assert horizon.data["horizon_at"] == report.horizon_at
    assert horizon.data["resolved_ref"] == shas["later"]
    assert horizon.independent_group == horizon.id

    limited = collect_git_history(repo, max_commits=2, repository_id=REPOSITORY_ID)
    limited_horizon = next(
        event for event in limited.events if event.kind == GIT_HISTORY_HORIZON_EVENT_KIND
    )
    assert limited_horizon == horizon
    assert limited.revert_events == 1


def test_revert_events_may_reference_units_outside_the_retained_prefix(
    revert_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, shas = revert_repo
    report = collect_git_history(
        repo,
        max_commits=None,
        since="2025-01-02T12:00:00Z",
        repository_id=REPOSITORY_ID,
    )

    assert {unit.prediction_sha for unit in report.units} == {shas["reverting"], shas["later"]}
    revert = next(event for event in report.events if event.kind == "revert")
    assert revert.change_id == f"change.git_commit.{shas['second']}"

    initialize_project(repo, "Ledger", target=POST_MERGE_REVERT_OR_HOTFIX)
    upsert_history_batch(
        events_path(repo),
        report.events,
        change_units_path(repo),
        report.units,
    )
    events, units = load_history_snapshot(events_path(repo), change_units_path(repo))
    assert len(units) == 2
    assert {event.kind for event in events} == {
        "git_commit",
        "revert",
        GIT_HISTORY_HORIZON_EVENT_KIND,
    }


def _git_unit(prediction_at: str = "2025-01-01T00:00:00+00:00") -> ChangeUnit:
    return ChangeUnit(
        id=f"change.git_commit.{SHA_B}",
        repository_id=REPOSITORY_ID,
        kind="git_commit",
        base_sha=SHA_A,
        prediction_sha=SHA_B,
        prediction_at=prediction_at,
        commits=(SHA_B,),
        event_ids=(f"event.git_commit.{SHA_B}",),
        provider="git",
        source_ref=SHA_B,
        evidence_quality="git_only",
        confirmatory=False,
    )


def _trailer_revert(occurred_at: str = "2025-01-03T00:00:00+00:00") -> HistoricalEvent:
    return HistoricalEvent(
        id=f"event.git_revert.{SHA_R}.{SHA_B}",
        repository_id=REPOSITORY_ID,
        kind="revert",
        occurred_at=occurred_at,
        available_at=occurred_at,
        provider="git",
        source_ref=SHA_R,
        independent_group=f"change.git_commit.{SHA_R}",
        change_id=f"change.git_commit.{SHA_B}",
        data={
            "sha": SHA_R,
            "reverted_sha": SHA_B,
            "linked_change_id": f"change.git_commit.{SHA_B}",
            "link_kind": GIT_TRAILER_LINK_KIND,
            "evidence_grade": "weak_heuristic",
        },
    )


def _horizon_event(
    horizon_at: str, identifier: str = "event.git_history_horizon.abc"
) -> HistoricalEvent:
    return HistoricalEvent(
        id=identifier,
        repository_id=REPOSITORY_ID,
        kind=GIT_HISTORY_HORIZON_EVENT_KIND,
        occurred_at=horizon_at,
        available_at=horizon_at,
        provider="git",
        source_ref=SHA_R,
        independent_group=identifier,
        data={"horizon_at": horizon_at},
    )


def test_git_trailer_revert_is_a_weak_positive_for_landed_commits() -> None:
    unit = _git_unit()
    strong_only = derive_outcome(unit, [_trailer_revert()], POST_MERGE_REVERT_OR_HOTFIX)
    weak = derive_outcome(
        unit,
        [_trailer_revert()],
        POST_MERGE_REVERT_OR_HOTFIX,
        include_weak=True,
    )

    assert strong_only.value is LabelValue.UNKNOWN
    assert [vote.strength for vote in strong_only.votes] == ["weak"]
    assert weak.value is LabelValue.POSITIVE
    assert weak.evidence is not None
    assert weak.evidence.kind == "incident"
    assert weak.evidence.source == f"historical-events:event.git_revert.{SHA_R}.{SHA_B}"
    assert "Git revert trailer" in weak.evidence.reason
    assert weak.evidence.confidence == 0.6


def test_registered_window_yields_weak_negative_only_after_the_horizon_proves_it() -> None:
    unit = _git_unit()
    window = GitWindow(
        window_days=30,
        horizon_at="2025-03-01T00:00:00Z",
        horizon_event_id="event.git_history_horizon.abc",
    )

    negative = derive_outcome(
        unit, [], POST_MERGE_REVERT_OR_HOTFIX, include_weak=True, git_window=window
    )
    strong_only = derive_outcome(unit, [], POST_MERGE_REVERT_OR_HOTFIX, git_window=window)
    immature = derive_outcome(
        unit,
        [],
        POST_MERGE_REVERT_OR_HOTFIX,
        include_weak=True,
        git_window=GitWindow(
            window_days=30,
            horizon_at="2025-01-15T00:00:00Z",
            horizon_event_id="event.git_history_horizon.abc",
        ),
    )
    reverted = derive_outcome(
        unit,
        [_trailer_revert()],
        POST_MERGE_REVERT_OR_HOTFIX,
        include_weak=True,
        git_window=window,
    )

    assert negative.value is LabelValue.NEGATIVE
    assert negative.evidence is not None
    assert negative.evidence.available_at == "2025-01-31T00:00:00Z"
    assert negative.evidence.kind == "imported"
    assert negative.evidence.source == "historical-events:event.git_history_horizon.abc"
    assert negative.evidence.confidence == 0.5
    assert strong_only.value is LabelValue.UNKNOWN
    assert immature.value is LabelValue.UNKNOWN
    assert immature.votes == ()
    assert reverted.value is LabelValue.POSITIVE
    assert all(vote.value == "positive" for vote in reverted.votes)


def test_window_negative_never_applies_to_provider_units_or_other_targets() -> None:
    provider_unit = ChangeUnit(
        id="github.pr.7",
        repository_id=REPOSITORY_ID,
        kind="provider_change",
        base_sha=SHA_A,
        prediction_sha=SHA_B,
        prediction_at="2025-01-01T00:00:00+00:00",
        final_sha=SHA_R,
        finalized_at="2025-01-02T00:00:00+00:00",
        commits=(SHA_B,),
        event_ids=(),
        provider="github",
        source_ref="github:pull/7",
        evidence_quality="rich",
        confirmatory=True,
    )
    window = GitWindow(
        window_days=7,
        horizon_at="2025-03-01T00:00:00Z",
        horizon_event_id="event.git_history_horizon.abc",
    )

    assert (
        derive_outcome(
            provider_unit, [], POST_MERGE_REVERT_OR_HOTFIX, include_weak=True, git_window=window
        ).votes
        == ()
    )
    assert (
        derive_outcome(
            _git_unit(), [], "post_merge_defect", include_weak=True, git_window=window
        ).votes
        == ()
    )


def test_git_window_resolution_uses_the_newest_horizon_of_the_repository() -> None:
    events = [
        _horizon_event("2025-02-01T00:00:00+00:00", "event.git_history_horizon.one"),
        _horizon_event("2025-03-01T00:00:00+00:00", "event.git_history_horizon.two"),
        HistoricalEvent(
            id="event.git_history_horizon.other",
            repository_id="repo.other",
            kind=GIT_HISTORY_HORIZON_EVENT_KIND,
            occurred_at="2025-04-01T00:00:00+00:00",
            available_at="2025-04-01T00:00:00+00:00",
            provider="git",
            source_ref=SHA_R,
            independent_group="event.git_history_horizon.other",
            data={"horizon_at": "2025-04-01T00:00:00+00:00"},
        ),
    ]

    window = git_window_from_events(events, window_days=14, repository_id=REPOSITORY_ID)

    assert window == GitWindow(
        window_days=14,
        horizon_at="2025-03-01T00:00:00+00:00",
        horizon_event_id="event.git_history_horizon.two",
    )
    assert git_window_from_events(events, window_days=None, repository_id=REPOSITORY_ID) is None
    assert git_window_from_events([], window_days=14, repository_id=REPOSITORY_ID) is None
    with pytest.raises(ModelError, match="window_days"):
        GitWindow(window_days=0, horizon_at="2025-03-01T00:00:00Z", horizon_event_id="x")


def test_materialization_derives_exploratory_labels_from_git_alone(
    revert_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, shas = revert_repo
    result = initialize_project(
        repo,
        "GitNative",
        target=POST_MERGE_REVERT_OR_HOTFIX,
        git_window_days=30,
    )
    config = result.config
    report = collect_git_history(
        repo, max_commits=None, repository_id=config.protocol.repository_id
    )

    strong_only = materialize_history(repo, config, report.units, report.events)
    weak = materialize_history(repo, config, report.units, report.events, include_weak=True)

    assert (strong_only.positive, strong_only.negative, strong_only.unknown) == (0, 0, 4)
    assert strong_only.git_window_negatives == 0
    assert (weak.positive, weak.negative, weak.unknown) == (1, 2, 1)
    assert weak.confirmatory == 0
    assert weak.git_window_negatives == 2
    assert weak.git_window is not None
    assert weak.git_window.window_days == 30
    assert weak.to_dict()["git_window"]["horizon_at"] == "2025-03-01T00:00:00Z"
    by_head = {item.source["head"]: item for item in weak.observations}
    assert by_head[shas["second"]].labels[config.target] is LabelValue.POSITIVE
    assert by_head[shas["first"]].labels[config.target] is LabelValue.NEGATIVE
    assert by_head[shas["reverting"]].labels[config.target] is LabelValue.NEGATIVE
    assert by_head[shas["later"]].labels[config.target] is LabelValue.UNKNOWN
    assert all(item.source["confirmatory"] is False for item in weak.observations)
    assert by_head[shas["first"]].metadata["historical_git_window"]["window_days"] == 30
    assert (
        by_head[shas["first"]].label_evidence[config.target].available_at == "2025-01-31T00:00:00Z"
    )
    assert weak.manifest_hash != strong_only.manifest_hash


def test_cli_registers_window_bootstraps_reverts_and_validates_persisted_labels(
    revert_repo: tuple[Path, dict[str, str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, _ = revert_repo

    assert (
        cli.main(
            [
                "init",
                str(repo),
                "--target",
                POST_MERGE_REVERT_OR_HOTFIX,
                "--git-window-days",
                "30",
            ]
        )
        == 0
    )
    capsys.readouterr()
    config = RuleLoomConfig.load(repo)
    assert config.outcomes.git_window_days == 30
    assert config.evidence_protocol["outcomes"] == {"git_window_days": 30}

    assert cli.main(["history", "--root", str(repo), "bootstrap-git", "--all"]) == 0
    bootstrap = json.loads(capsys.readouterr().out)
    assert bootstrap["revert_events"] == 1
    assert bootstrap["horizon_at"] == "2025-03-01T00:00:00Z"
    assert bootstrap["events_inserted"] == 6

    assert cli.main(["history", "--root", str(repo), "materialize", "--include-weak"]) == 0
    materialized = json.loads(capsys.readouterr().out)
    assert (materialized["positive"], materialized["negative"], materialized["unknown"]) == (
        1,
        2,
        1,
    )
    assert materialized["git_window_negatives"] == 2
    assert materialized["git_window"]["window_days"] == 30

    validate_project(repo, config)
    observations = load_observations(dataset_path(repo, config))
    assert sum(item.labels[config.target] is LabelValue.NEGATIVE for item in observations) == 2
    assert (
        resolve_git_window(
            config, load_history_snapshot(events_path(repo), change_units_path(repo))[0]
        )
        is not None
    )

    assert cli.main(["history", "--root", str(repo), "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["labels"] == {"negative": 2, "positive": 1, "unknown": 1}
    assert status["event_evidence_grade"] == {"weak_heuristic": 1}


def _validate_config(payload: dict[str, object]) -> None:
    resource = files("ruleloom").joinpath("schemas", "config.schema.json")
    schema = json.loads(resource.read_text(encoding="utf-8"))
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(payload)


def test_schema_v5_binds_the_git_window_into_the_evidence_protocol() -> None:
    windowed = default_config(
        "Example", schema_version=5, test_start_at="2026-09-01T00:00:00Z", git_window_days=30
    )
    unwindowed = default_config("Example", schema_version=5, test_start_at="2026-09-01T00:00:00Z")

    assert windowed.to_dict()["outcomes"] == {"git_window_days": 30}
    assert unwindowed.to_dict()["outcomes"] == {"git_window_days": None}
    assert windowed.evidence_protocol_hash != unwindowed.evidence_protocol_hash
    assert RuleLoomConfig.from_dict(windowed.to_dict()) == windowed
    _validate_config(windowed.to_dict())

    with pytest.raises(ModelError, match="schema_version 5"):
        RuleLoomConfig(
            schema_version=4,
            project="Example",
            pack="generic_changes",
            pack_version=1,
            outcomes=OutcomesConfig(git_window_days=30),
        )
    with pytest.raises(ModelError, match="git_window_days"):
        OutcomesConfig(git_window_days=0)
    legacy = default_config("Example", schema_version=4, test_start_at="2026-09-01T00:00:00Z")
    assert "outcomes" not in legacy.to_dict()
    legacy_payload = legacy.to_dict()
    legacy_payload["outcomes"] = {"git_window_days": 30}
    with pytest.raises(ValidationError):
        _validate_config(legacy_payload)
    missing = windowed.to_dict()
    missing.pop("outcomes")
    with pytest.raises(ValidationError):
        _validate_config(missing)
