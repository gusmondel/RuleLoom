from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from ruleloom import cli
from ruleloom.config import RuleLoomConfig
from ruleloom.history.github import GitHubHistoryError, GitHubHistoryReport
from ruleloom.history.github_event_archive import (
    GITHUB_EVENT_ARCHIVE_ADAPTER_VERSION,
    GITHUB_EVENT_ARCHIVE_EXPORTER_VERSION,
    GITHUB_EVENT_ARCHIVE_ROW_SCHEMA_VERSION,
)
from ruleloom.history.github_webhooks import GitHubCaptureDirectoryReport
from ruleloom.history.models import ChangeUnit, HistoricalEvent
from ruleloom.history.storage import change_units_path, events_path, load_change_units, load_events
from ruleloom.models import LabelValue, canonical_json
from ruleloom.storage import dataset_path, load_observations


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
        filename,
        env={
            **os.environ,
            "GIT_AUTHOR_DATE": timestamp,
            "GIT_COMMITTER_DATE": timestamp,
        },
    )
    return _git(repo, "rev-parse", "HEAD")


def _run(arguments: list[str], capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    assert cli.main(arguments) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    result = json.loads(captured.out)
    assert isinstance(result, dict)
    return result


def test_history_cli_exposes_fail_closed_incremental_git_cursor(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "incremental-repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "History Test")
    _git(repo, "config", "user.email", "history@example.invalid")
    boundary = _commit(repo, "base.txt", "2025-01-01T00:00:00Z")
    assert cli.main(["init", str(repo)]) == 0
    capsys.readouterr()
    initial = _run(
        [
            "history",
            "--root",
            str(repo),
            "bootstrap-git",
            "--all",
            "--ref",
            boundary,
        ],
        capsys,
    )
    # One commit event plus the history-horizon event of this bootstrap run.
    assert initial["events_inserted"] == 2
    expected = _commit(repo, "next.txt", "2025-01-02T00:00:00Z")

    report = _run(
        [
            "history",
            "--root",
            str(repo),
            "bootstrap-git",
            "--after",
            boundary,
            "--ref",
            expected,
        ],
        capsys,
    )

    assert report["after"] == boundary
    assert report["resolved_ref"] == expected
    assert report["incremental"] is True
    assert report["incremental_boundary_is_ancestor"] is True
    assert report["events_inserted"] == 2
    assert report["units_inserted"] == 1


def test_history_cli_rejects_unrecorded_incremental_boundary_before_collection(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "unrecorded-cursor"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "History Test")
    _git(repo, "config", "user.email", "history@example.invalid")
    boundary = _commit(repo, "base.txt", "2025-01-01T00:00:00Z")
    assert cli.main(["init", str(repo)]) == 0
    capsys.readouterr()
    collection_called = False

    def collect(*_args: object, **_kwargs: object) -> object:
        nonlocal collection_called
        collection_called = True
        raise AssertionError("unrecorded cursor reached Git collection")

    monkeypatch.setattr(cli, "collect_git_history", collect)

    assert (
        cli.main(
            [
                "history",
                "--root",
                str(repo),
                "bootstrap-git",
                "--after",
                boundary,
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert "already-recorded Git commit or merge" in captured.err
    assert collection_called is False
    assert load_events(events_path(repo)) == []
    assert load_change_units(change_units_path(repo)) == []


def test_history_cli_rejects_a_different_cursor_in_a_nonempty_ledger(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "different-cursor"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "History Test")
    _git(repo, "config", "user.email", "history@example.invalid")
    recorded = _commit(repo, "recorded.txt", "2025-01-01T00:00:00Z")
    assert cli.main(["init", str(repo)]) == 0
    capsys.readouterr()
    _run(
        [
            "history",
            "--root",
            str(repo),
            "bootstrap-git",
            "--all",
            "--ref",
            recorded,
        ],
        capsys,
    )
    unrecorded = _commit(repo, "unrecorded.txt", "2025-01-02T00:00:00Z")

    assert (
        cli.main(
            [
                "history",
                "--root",
                str(repo),
                "bootstrap-git",
                "--after",
                unrecorded,
            ]
        )
        == 2
    )
    assert "already-recorded Git commit or merge" in capsys.readouterr().err
    assert {event.source_ref for event in load_events(events_path(repo))} == {recorded}


def test_history_cli_never_persists_a_truncated_incremental_range(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "truncated-cursor"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "History Test")
    _git(repo, "config", "user.email", "history@example.invalid")
    boundary = _commit(repo, "x.txt", "2025-01-01T00:00:00Z")
    assert cli.main(["init", str(repo)]) == 0
    capsys.readouterr()
    _run(
        [
            "history",
            "--root",
            str(repo),
            "bootstrap-git",
            "--all",
            "--ref",
            boundary,
        ],
        capsys,
    )
    _commit(repo, "a.txt", "2025-01-02T00:00:00Z")
    _commit(repo, "b.txt", "2025-01-03T00:00:00Z")
    _commit(repo, "c.txt", "2025-01-04T00:00:00Z")

    assert (
        cli.main(
            [
                "history",
                "--root",
                str(repo),
                "bootstrap-git",
                "--after",
                boundary,
                "--max-commits",
                "1",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert "no partial cursor range" in captured.err
    assert {event.source_ref for event in load_events(events_path(repo))} == {boundary}
    assert {unit.prediction_sha for unit in load_change_units(change_units_path(repo))} == {
        boundary
    }


def test_history_cli_makes_after_and_since_mutually_exclusive(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.build_parser().parse_args(
            [
                "history",
                "--root",
                str(tmp_path),
                "bootstrap-git",
                "--after",
                "1" * 40,
                "--since",
                "2025-01-01T00:00:00Z",
            ]
        )

    assert raised.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


def test_history_cli_ingests_capture_inbox_with_repository_pin_and_secret_env(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "capture-repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "History Test")
    _git(repo, "config", "user.email", "history@example.invalid")
    _commit(repo, "base.txt", "2025-01-01T00:00:00Z")
    assert cli.main(["init", str(repo)]) == 0
    capsys.readouterr()
    config = RuleLoomConfig.load(repo)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setenv("RULELOOM_TEST_ENVELOPE", "a-secret-key-long-enough")

    def ingest(
        root: Path,
        selected_inbox: Path,
        *,
        expected_repository_id: str,
        expected_label_policy_hash: str,
        envelope_key: bytes,
        max_bundles: int,
    ) -> GitHubCaptureDirectoryReport:
        assert root == repo.resolve()
        assert selected_inbox == inbox
        assert expected_repository_id == config.protocol.repository_id
        assert expected_label_policy_hash == "a" * 64
        assert envelope_key == b"a-secret-key-long-enough"
        assert max_bundles == 7
        return GitHubCaptureDirectoryReport(
            processed_bundles=("delivery.json",),
            unique_deliveries=1,
            duplicate_replays=0,
            events_inserted=2,
            events_unchanged=0,
            units_inserted=0,
            units_unchanged=0,
        )

    monkeypatch.setattr(cli, "ingest_github_capture_directory", ingest)
    report = _run(
        [
            "history",
            "--root",
            str(repo),
            "ingest-github-captures",
            str(inbox),
            "--envelope-key-env",
            "RULELOOM_TEST_ENVELOPE",
            "--expected-label-policy-hash",
            "a" * 64,
            "--max-bundles",
            "7",
        ],
        capsys,
    )

    assert report["unique_deliveries"] == 1
    assert report["events_inserted"] == 2


def test_history_cli_requires_an_independent_label_policy_pin() -> None:
    parser = cli.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "history",
                "ingest-github-captures",
                "/tmp/capture-inbox",
            ]
        )


def test_history_cli_bootstraps_imports_materializes_and_reports(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "History Test")
    _git(repo, "config", "user.email", "history@example.invalid")
    base = _commit(repo, "base.txt", "2025-01-01T00:00:00Z")
    head = _commit(repo, "head.txt", "2025-01-02T00:00:00Z")
    assert cli.main(["init", str(repo)]) == 0
    capsys.readouterr()
    config = RuleLoomConfig.load(repo)

    bootstrapped = _run(
        [
            "history",
            "--root",
            str(repo),
            "bootstrap-git",
            "--max-commits",
            "1",
        ],
        capsys,
    )
    assert bootstrapped["events_inserted"] == 2
    assert bootstrapped["units_inserted"] == 1
    assert bootstrapped["evidence_grade"] == "exploratory_git_only"
    assert bootstrapped["storage_byte_limit"] == 64 * 1024 * 1024
    assert bootstrapped["event_log_bytes"] > 0
    assert bootstrapped["change_unit_log_bytes"] > 0

    snapshot = HistoricalEvent(
        id="snapshot-pr-1",
        repository_id=config.protocol.repository_id,
        kind="change_snapshot",
        occurred_at="2025-01-02T01:00:00Z",
        available_at="2025-01-02T01:00:00Z",
        provider="forge",
        source_ref="pr/1/open",
        change_id="pr-1",
        independent_group="pr-1",
        data={
            "base_sha": base,
            "head_sha": head,
            "point_in_time": True,
        },
    )
    review = HistoricalEvent(
        id="review-pr-1",
        repository_id=config.protocol.repository_id,
        kind="review",
        occurred_at="2025-01-03T00:00:00Z",
        available_at="2025-01-03T00:00:00Z",
        provider="review-system",
        source_ref="review/1",
        change_id="pr-1",
        independent_group="reviewer-1",
        data={
            "decision": "changes_requested",
            "category": "validation",
            "independent": True,
        },
    )
    import_file = tmp_path / "events.jsonl"
    import_file.write_text(json.dumps(snapshot.to_dict()) + "\n", encoding="utf-8")
    imported = _run(
        [
            "history",
            "--root",
            str(repo),
            "import",
            "--events",
            str(import_file),
        ],
        capsys,
    )
    assert imported["events_inserted"] == 1
    assert imported["units_assembled"] == 1

    initial = _run(
        ["history", "--root", str(repo), "materialize"],
        capsys,
    )
    assert initial["positive"] == 0
    assert initial["unknown"] == 2

    import_file.write_text(json.dumps(review.to_dict()) + "\n", encoding="utf-8")
    later_import = _run(
        [
            "history",
            "--root",
            str(repo),
            "import",
            "--events",
            str(import_file),
        ],
        capsys,
    )
    assert later_import["events_inserted"] == 1
    assert later_import["units_inserted"] == 0
    assert later_import["units_unchanged"] == 1

    materialized = _run(
        ["history", "--root", str(repo), "materialize"],
        capsys,
    )
    assert materialized["positive"] == 1
    assert materialized["unknown"] == 1
    assert materialized["confirmatory"] == 1
    assert materialized["inserted"] == 0
    assert materialized["updated"] == 1

    unrelated = HistoricalEvent(
        id="review-unrelated",
        repository_id=config.protocol.repository_id,
        kind="review",
        occurred_at="2025-01-04T00:00:00Z",
        available_at="2025-01-04T00:00:00Z",
        provider="review-system",
        source_ref="review/other",
        change_id="pr-other",
        independent_group="reviewer-other",
        data={
            "decision": "changes_requested",
            "category": "validation",
            "independent": True,
        },
    )
    import_file.write_text(json.dumps(unrelated.to_dict()) + "\n", encoding="utf-8")
    _run(
        [
            "history",
            "--root",
            str(repo),
            "import",
            "--events",
            str(import_file),
        ],
        capsys,
    )
    unchanged = _run(
        ["history", "--root", str(repo), "materialize"],
        capsys,
    )
    assert unchanged["inserted"] == 0
    assert unchanged["updated"] == 0

    status = _run(["history", "--root", str(repo), "status"], capsys)
    assert status["events"] == 5
    assert status["change_units"] == 2
    assert status["language_neutral_core"] is True
    assert status["labels"] == {"negative": 0, "positive": 1, "unknown": 1}
    observations = load_observations(dataset_path(repo, config))
    positive = next(
        item for item in observations if item.labels[config.target] is LabelValue.POSITIVE
    )
    assert positive.source["change_id"] == "pr-1"

    assert (
        cli.main(
            [
                "label",
                "--root",
                str(repo),
                positive.id,
                "negative",
                "--kind",
                "synthetic",
                "--source",
                "manual/override",
                "--available-at",
                "2025-01-05T00:00:00Z",
            ]
        )
        == 2
    )
    blocked = capsys.readouterr()
    assert "labels are derived from immutable events" in blocked.err


def test_history_cli_imports_first_class_github_archive_without_assembling_duplicates(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "github-repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "History Test")
    _git(repo, "config", "user.email", "history@example.invalid")
    _git(repo, "remote", "add", "origin", "git@github.com:acme/widgets.git")
    base = _commit(repo, "base.txt", "2025-01-01T00:00:00Z")
    head = _commit(repo, "head.txt", "2025-01-02T00:00:00Z")
    assert cli.main(["init", str(repo)]) == 0
    capsys.readouterr()
    config = RuleLoomConfig.load(repo)
    provider_key = f"github.github.com.repo.{'a' * 20}"
    change_id = "change.github.repo.test.pull.1"
    snapshot = HistoricalEvent(
        id="event.github.repo.test.snapshot.1",
        repository_id=config.protocol.repository_id,
        kind="change_snapshot",
        occurred_at="2025-01-02T00:00:00Z",
        available_at="2025-01-02T00:00:00Z",
        provider="github",
        source_ref=f"github:{provider_key}:pull:1:archive-snapshot",
        change_id=change_id,
        independent_group=change_id,
        data={"base_sha": base, "head_sha": head, "point_in_time": False},
    )
    final = HistoricalEvent(
        id="event.github.repo.test.final.1",
        repository_id=config.protocol.repository_id,
        kind="change_merged",
        occurred_at="2025-01-03T00:00:00Z",
        available_at="2025-01-03T00:00:00Z",
        provider="github",
        source_ref=f"github:{provider_key}:pull:1:final",
        change_id=change_id,
        independent_group=change_id,
        data={"base_sha": base, "head_sha": head, "final_sha": head},
    )
    unit = ChangeUnit(
        id=change_id,
        repository_id=config.protocol.repository_id,
        kind="github_archive_change",
        base_sha=base,
        prediction_sha=head,
        prediction_at=snapshot.occurred_at,
        final_sha=head,
        finalized_at=final.occurred_at,
        commits=(head,),
        event_ids=(snapshot.id, final.id),
        provider="github",
        source_ref=f"github:{provider_key}:pull:1",
        evidence_quality="git_only",
        confirmatory=False,
    )
    report = GitHubHistoryReport(
        events=(snapshot, final),
        units=(unit,),
        pull_requests_examined=1,
        pull_requests_normalized=1,
        pull_requests_skipped=0,
        warnings=(),
        truncated=False,
        provider_repository_key=provider_key,
        collected_at="2025-01-04T00:00:00Z",
        since=None,
        until="2025-01-04T00:00:00Z",
        repository_id=config.protocol.repository_id,
        repository_binding="verified_origin",
        api_requests_used=12,
        provider_records_used=16,
        max_api_requests=13,
        max_provider_records=17,
    )
    received_kwargs: dict[str, object] = {}

    def collect(*_args: object, **kwargs: object) -> GitHubHistoryReport:
        received_kwargs.update(kwargs)
        return report

    monkeypatch.setattr(cli, "GhApiClient", lambda: object())
    monkeypatch.setattr(cli, "collect_github_history", collect)

    imported = _run(
        [
            "history",
            "--root",
            str(repo),
            "import-github",
            "--repository",
            "acme/widgets",
            "--max-api-requests",
            "13",
            "--max-provider-records",
            "17",
        ],
        capsys,
    )

    assert imported["events_inserted"] == 2
    assert imported["units_inserted"] == 1
    assert imported["evidence_grade"] == "exploratory_git_only"
    assert imported["collection_budget"] == {
        "policy": "fail_closed",
        "api_requests": {"used": 12, "maximum": 13},
        "provider_records": {"used": 16, "maximum": 17},
    }
    assert imported["collection_limits"] == {
        "pull_requests": 1_000,
        "commits_per_pull": 1_000,
        "reviews_per_pull": 1_000,
        "checks_per_commit": 1_000,
        "repository_commits": 10_000,
        "api_requests": 13,
        "provider_records": 17,
    }
    assert imported["local_git_preflight"] == {
        "required_commit_objects": 2,
        "available_commit_objects": 2,
        "missing_commit_objects": 0,
        "affected_change_units": 0,
        "missing_preview": [],
        "preview_truncated": False,
    }
    assert received_kwargs["max_api_requests"] == 13
    assert received_kwargs["max_provider_records"] == 17
    assert received_kwargs["repository_binding"] == "verified_origin"
    assert load_events(events_path(repo)) == [snapshot, final]
    assert load_change_units(change_units_path(repo)) == [unit]


def test_history_cli_imports_verified_github_event_archive(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "event-archive-repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "History Test")
    _git(repo, "config", "user.email", "history@example.invalid")
    _git(repo, "remote", "add", "origin", "https://github.com/acme/widgets.git")
    base = _commit(repo, "base.txt", "2025-01-01T00:00:00Z")
    head = _commit(repo, "head.txt", "2025-01-02T00:00:00Z")
    assert cli.main(["init", str(repo)]) == 0
    capsys.readouterr()

    row = {
        "schema_version": GITHUB_EVENT_ARCHIVE_ROW_SCHEMA_VERSION,
        "event_type": "PullRequestEvent",
        "repository": "acme/widgets",
        "occurred_at": "2025-01-02T01:00:00Z",
        "available_at": "2025-01-02T02:00:00Z",
        "action": "opened",
        "actor_key": "github.login." + hashlib.sha256(b"author").hexdigest(),
        "number": 1,
        "base_sha": base,
        "head_sha": head,
        "review_state": "none",
        "additions": 1,
        "deletions": 0,
        "changed_files": 1,
        "statistics_complete": True,
    }
    content = (canonical_json(row) + "\n").encode()
    archive_path = tmp_path / "events.jsonl"
    archive_path.write_bytes(content)
    manifest = {
        "schema_version": 1,
        "event_schema_version": GITHUB_EVENT_ARCHIVE_ROW_SCHEMA_VERSION,
        "adapter_version": GITHUB_EVENT_ARCHIVE_ADAPTER_VERSION,
        "exporter_version": GITHUB_EVENT_ARCHIVE_EXPORTER_VERSION,
        "source": "gharchive-clickhouse-public",
        "source_url": "https://play.clickhouse.com/",
        "repository": "acme/widgets",
        "provider_repository_id": 123456,
        "collection_start": "2025-01-01T00:00:00Z",
        "collection_end": "2025-02-01T00:00:00Z",
        "dataset_max_at": "2025-02-02T00:00:00Z",
        "collected_at": "2025-02-03T00:00:00Z",
        "query_sha256": "a" * 64,
        "coverage_query_sha256": "c" * 64,
        "events_sha256": hashlib.sha256(content).hexdigest(),
        "preregistration_sha256": "b" * 64,
        "window_complete": True,
        "expected_hours": 744,
        "observed_hours": 744,
        "missing_hours": [],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(canonical_json(manifest) + "\n", encoding="utf-8")

    imported = _run(
        [
            "history",
            "--root",
            str(repo),
            "import-github-event-archive",
            "--events",
            str(archive_path),
            "--manifest",
            str(manifest_path),
        ],
        capsys,
    )

    assert imported["rows_read"] == 1
    assert imported["events_inserted"] == 1
    assert imported["units_inserted"] == 1
    assert imported["confirmatory_units"] == 1
    assert imported["local_git_preflight"] == {
        "required_commit_objects": 2,
        "available_commit_objects": 2,
        "missing_commit_objects": 0,
        "affected_change_units": 0,
        "missing_preview": [],
        "preview_truncated": False,
    }


def test_history_cli_budget_exhaustion_never_reaches_persistence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "budget-repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "History Test")
    _git(repo, "config", "user.email", "history@example.invalid")
    _git(repo, "remote", "add", "origin", "https://github.com/acme/widgets.git")
    _commit(repo, "base.txt", "2025-01-01T00:00:00Z")
    assert cli.main(["init", str(repo)]) == 0
    capsys.readouterr()

    def exhaust(*_args: object, **kwargs: object) -> GitHubHistoryReport:
        assert kwargs["max_api_requests"] == 1
        assert kwargs["max_provider_records"] == 2
        raise GitHubHistoryError(
            "global GitHub API request budget exhausted; collection aborted without persistence"
        )

    persistence_called = False

    def persist(*_args: object, **_kwargs: object) -> dict[str, int]:
        nonlocal persistence_called
        persistence_called = True
        return {}

    monkeypatch.setattr(cli, "GhApiClient", lambda: object())
    monkeypatch.setattr(cli, "collect_github_history", exhaust)
    monkeypatch.setattr(cli, "_persist_history_import", persist)

    assert (
        cli.main(
            [
                "history",
                "--root",
                str(repo),
                "import-github",
                "--repository",
                "acme/widgets",
                "--max-api-requests",
                "1",
                "--max-provider-records",
                "2",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert "aborted without persistence" in captured.err
    assert persistence_called is False
    assert load_events(events_path(repo)) == []
    assert load_change_units(change_units_path(repo)) == []


def test_history_cli_rejects_cross_repository_import_unless_explicitly_overridden(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "binding-repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "History Test")
    _git(repo, "config", "user.email", "history@example.invalid")
    _git(repo, "remote", "add", "origin", "https://github.com/acme/actual.git")
    _commit(repo, "base.txt", "2025-01-01T00:00:00Z")
    assert cli.main(["init", str(repo)]) == 0
    capsys.readouterr()

    called = False

    def stop_after_binding(*_args: object, **kwargs: object) -> GitHubHistoryReport:
        nonlocal called
        called = True
        assert kwargs["repository_binding"] == "explicit_unverified_override"
        raise GitHubHistoryError("stop after verified override")

    monkeypatch.setattr(cli, "GhApiClient", lambda: object())
    monkeypatch.setattr(cli, "collect_github_history", stop_after_binding)
    base_args = [
        "history",
        "--root",
        str(repo),
        "import-github",
        "--repository",
        "acme/other",
    ]
    assert cli.main(base_args) == 2
    rejected = capsys.readouterr()
    assert "not verifiably equal" in rejected.err
    assert called is False

    assert cli.main([*base_args, "--allow-unverified-repository"]) == 2
    overridden = capsys.readouterr()
    assert "stop after verified override" in overridden.err
    assert called is True


def test_history_import_rejects_foreign_repository_without_partial_write(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "History Test")
    _git(repo, "config", "user.email", "history@example.invalid")
    _commit(repo, "base.txt", "2025-01-01T00:00:00Z")
    assert cli.main(["init", str(repo)]) == 0
    capsys.readouterr()
    event = HistoricalEvent(
        id="foreign-event",
        repository_id="repository.other",
        kind="review",
        occurred_at="2025-01-02T00:00:00Z",
        available_at="2025-01-02T00:00:00Z",
        provider="forge",
        source_ref="review/1",
        change_id="pr-1",
        independent_group="reviewer-1",
        data={},
    )
    import_file = tmp_path / "foreign.jsonl"
    import_file.write_text(json.dumps(event.to_dict()) + "\n", encoding="utf-8")

    assert (
        cli.main(
            [
                "history",
                "--root",
                str(repo),
                "import",
                "--events",
                str(import_file),
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert "different repository" in captured.err
    assert load_events(events_path(repo)) == []
    assert load_change_units(change_units_path(repo)) == []


def test_history_import_rejects_invalid_event_links_without_partial_write(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "History Test")
    _git(repo, "config", "user.email", "history@example.invalid")
    base = _commit(repo, "base.txt", "2025-01-01T00:00:00Z")
    head = _commit(repo, "head.txt", "2025-01-02T00:00:00Z")
    assert cli.main(["init", str(repo)]) == 0
    capsys.readouterr()
    config = RuleLoomConfig.load(repo)
    snapshot = HistoricalEvent(
        id="snapshot-pr-1",
        repository_id=config.protocol.repository_id,
        kind="change_snapshot",
        occurred_at="2025-01-02T01:00:00Z",
        available_at="2025-01-02T01:00:00Z",
        provider="forge",
        source_ref="pr/1/open",
        change_id="pr-1",
        independent_group="pr-1",
        data={"base_sha": base, "head_sha": head, "point_in_time": True},
    )
    unit = ChangeUnit(
        id="pr-1",
        repository_id=config.protocol.repository_id,
        kind="provider_change",
        base_sha=base,
        prediction_sha=head,
        prediction_at=snapshot.occurred_at,
        commits=(head,),
        event_ids=(snapshot.id, "event.missing"),
        provider=snapshot.provider,
        source_ref=snapshot.source_ref,
        evidence_quality="rich",
        confirmatory=True,
    )
    events_file = tmp_path / "events.jsonl"
    units_file = tmp_path / "units.jsonl"

    for event, candidate, expected in (
        (snapshot, unit, "missing historical event"),
        (
            replace(snapshot, change_id="pr-other"),
            replace(unit, event_ids=(snapshot.id,)),
            "not owned by change unit",
        ),
    ):
        events_file.write_text(json.dumps(event.to_dict()) + "\n", encoding="utf-8")
        units_file.write_text(json.dumps(candidate.to_dict()) + "\n", encoding="utf-8")
        assert (
            cli.main(
                [
                    "history",
                    "--root",
                    str(repo),
                    "import",
                    "--events",
                    str(events_file),
                    "--units",
                    str(units_file),
                    "--no-assemble",
                ]
            )
            == 2
        )
        assert expected in capsys.readouterr().err
        assert load_events(events_path(repo)) == []
        assert load_change_units(change_units_path(repo)) == []
