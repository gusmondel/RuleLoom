from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

import ruleloom.history.git as history_git
import ruleloom.history.storage as history_storage
from ruleloom.history.git import (
    GitHistoryBudgets,
    GitHistoryError,
    collect_git_history,
    ingest_git_history,
)
from ruleloom.history.storage import save_change_units, save_events
from ruleloom.models import canonical_json

REPOSITORY_ID = "repo.history-test"
_COMMIT_EVENT_KINDS = {"git_commit", "git_merge"}


def _commit_shas(report: history_git.GitHistoryReport) -> set[str]:
    return {str(event.data["sha"]) for event in report.events if event.kind in _COMMIT_EVENT_KINDS}


def _git(
    repo: Path,
    *arguments: str,
    env: dict[str, str] | None = None,
    input_bytes: bytes | None = None,
) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        input=input_bytes,
        env=env,
    )
    return completed.stdout.decode("utf-8").strip()


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _commit(repo: Path, message: str, timestamp: str, filename: str) -> str:
    _write(repo / filename, f"opaque payload for {filename}\n")
    _git(repo, "add", "--", filename)
    _git(
        repo,
        "commit",
        "-m",
        message,
        env={
            **os.environ,
            "GIT_AUTHOR_DATE": timestamp,
            "GIT_COMMITTER_DATE": timestamp,
        },
    )
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def history_repo(tmp_path: Path) -> tuple[Path, tuple[str, ...], str]:
    repo = tmp_path / "history"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "History Person")
    _git(repo, "config", "user.email", "private@example.test")
    root = _commit(repo, "Initial", "2026-01-01T10:00:00+00:00", "root.asset")

    _git(repo, "switch", "-c", "feature")
    long_subject = "é" * 600
    feature = _commit(repo, long_subject, "2026-01-03T10:00:00+00:00", "feature.data")

    _git(repo, "switch", "main")
    main = _commit(repo, "Main work", "2026-01-02T10:00:00+00:00", "main.blob")
    _git(
        repo,
        "merge",
        "--no-ff",
        "feature",
        "-m",
        "Merge feature",
        env={
            **os.environ,
            "GIT_AUTHOR_DATE": "2026-01-04T10:00:00+00:00",
            "GIT_COMMITTER_DATE": "2026-01-04T10:00:00+00:00",
        },
    )
    merge = _git(repo, "rev-parse", "HEAD")
    return repo, (root, main, feature, merge), long_subject


def test_collects_deterministic_topological_git_metadata_only(
    history_repo: tuple[Path, tuple[str, ...], str],
) -> None:
    repo, commits, long_subject = history_repo

    first = collect_git_history(repo, max_commits=None, repository_id=REPOSITORY_ID)
    second = ingest_git_history(repo, max_commits=None, repository_id=REPOSITORY_ID)

    assert first == second
    assert first.examined == first.unit_count == 4
    # Four commit events plus the single history-horizon event; no revert trailers.
    assert first.event_count == 5
    assert first.revert_events == 0
    assert first.horizon_at == "2026-01-04T10:00:00Z"
    assert not first.shallow
    assert not first.truncated
    assert first.warnings == ()
    assert len(first.manifest_hash) == 64
    assert first.to_dict()["events"] == 5
    assert first.to_dict()["units"] == 4
    assert first.storage_byte_limit == history_storage.HISTORY_JSONL_MAX_BYTES
    assert first.storage_line_byte_limit == history_storage.HISTORY_JSONL_MAX_LINE_BYTES
    assert first.event_log_bytes == sum(
        len((canonical_json(event.to_dict()) + "\n").encode("utf-8")) for event in first.events
    )
    assert first.change_unit_log_bytes == sum(
        len((canonical_json(unit.to_dict()) + "\n").encode("utf-8")) for unit in first.units
    )

    commit_events = [event for event in first.events if event.kind in {"git_commit", "git_merge"}]
    positions = {event.data["sha"]: index for index, event in enumerate(commit_events)}
    for event in commit_events:
        sha = event.data["sha"]
        assert isinstance(sha, str)
        parents = event.data["parents"]
        assert isinstance(parents, list)
        for parent in parents:
            if parent in positions:
                assert positions[parent] < positions[sha]
    assert set(positions) == set(commits)
    assert all("topological_index" not in event.data for event in first.events)

    feature_event = next(event for event in first.events if event.data["sha"] == commits[2])
    assert feature_event.data["subject_truncated"] is True
    subject = feature_event.data["subject"]
    assert isinstance(subject, str)
    assert len(subject.encode("utf-8")) <= 512
    assert (
        feature_event.data["subject_hash"]
        == hashlib.sha256(long_subject.encode("utf-8")).hexdigest()
    )

    serialized = json.dumps(
        [event.to_dict() for event in first.events],
        ensure_ascii=False,
        sort_keys=True,
    )
    assert "private@example.test" not in serialized
    assert "History Person" not in serialized
    assert all(len(str(event.data["author_hash"])) == 64 for event in commit_events)


def test_root_and_merge_units_preserve_only_defensible_git_semantics(
    history_repo: tuple[Path, tuple[str, ...], str],
) -> None:
    repo, commits, _ = history_repo
    report = collect_git_history(repo, max_commits=None, repository_id=REPOSITORY_ID)

    empty_tree = _git(repo, "hash-object", "-t", "tree", "--stdin", input_bytes=b"")
    root = next(unit for unit in report.units if unit.prediction_sha == commits[0])
    assert root.kind == "git_commit"
    assert root.base_sha == empty_tree
    assert root.evidence_quality == "git_only"
    assert root.confirmatory is False
    assert root.final_sha is None
    assert root.finalized_at is None

    merge = next(unit for unit in report.units if unit.prediction_sha == commits[3])
    assert merge.kind == "git_merge"
    assert merge.base_sha == commits[1]
    assert merge.evidence_quality == "final_only"
    assert merge.confirmatory is False
    assert merge.final_sha == commits[3]
    assert merge.finalized_at == merge.prediction_at
    merge_event = next(event for event in report.events if event.change_id == merge.id)
    parents = merge_event.data["parents"]
    assert isinstance(parents, list)
    assert len(parents) == 2


def test_max_commits_and_since_are_bounded_and_report_truncation(
    history_repo: tuple[Path, tuple[str, ...], str],
) -> None:
    repo, commits, _ = history_repo

    limited = collect_git_history(repo, max_commits=2, repository_id=REPOSITORY_ID)
    assert limited.examined == 2
    assert limited.truncated
    assert _commit_shas(limited) == {commits[2], commits[3]}
    assert any("max_commits" in warning for warning in limited.warnings)

    recent = collect_git_history(
        repo,
        max_commits=None,
        since="2026-01-02T12:00:00Z",
        repository_id=REPOSITORY_ID,
    )
    assert recent.since == "2026-01-02T12:00:00Z"
    assert _commit_shas(recent) == {commits[2], commits[3]}
    assert not recent.truncated


def test_overlapping_windows_keep_commit_records_immutable(
    history_repo: tuple[Path, tuple[str, ...], str],
) -> None:
    repo, _, _ = history_repo
    first = collect_git_history(repo, max_commits=1, repository_id=REPOSITORY_ID)
    previous_tip = first.events[0]
    _commit(repo, "Later", "2026-01-05T10:00:00+00:00", "later.asset")

    expanded = collect_git_history(repo, max_commits=2, repository_id=REPOSITORY_ID)
    overlapping = next(event for event in expanded.events if event.id == previous_tip.id)

    assert overlapping == previous_tip


def test_incremental_cursor_is_exclusive_and_reproducible(
    history_repo: tuple[Path, tuple[str, ...], str],
) -> None:
    repo, commits, _ = history_repo

    first = collect_git_history(
        repo,
        max_commits=None,
        after=commits[1],
        repository_id=REPOSITORY_ID,
    )
    second = collect_git_history(
        repo,
        max_commits=None,
        after=commits[1],
        repository_id=REPOSITORY_ID,
    )

    assert first == second
    assert first.after == commits[1]
    assert first.incremental_boundary_is_ancestor is True
    assert _commit_shas(first) == {commits[2], commits[3]}
    assert commits[1] not in {unit.prediction_sha for unit in first.units}
    assert first.to_dict()["incremental"] is True

    empty = collect_git_history(
        repo,
        max_commits=None,
        after=commits[3],
        repository_id=REPOSITORY_ID,
    )
    assert empty.examined == 0
    assert empty.after == commits[3]
    assert empty.incremental_boundary_is_ancestor is True


def test_incremental_cursor_refuses_commit_or_storage_truncation(
    history_repo: tuple[Path, tuple[str, ...], str],
) -> None:
    repo, commits, _ = history_repo

    with pytest.raises(GitHistoryError, match="no partial cursor range"):
        collect_git_history(
            repo,
            max_commits=1,
            after=commits[0],
            repository_id=REPOSITORY_ID,
        )
    with pytest.raises(GitHistoryError, match="no partial cursor range"):
        collect_git_history(
            repo,
            max_commits=None,
            after=commits[0],
            repository_id=REPOSITORY_ID,
            budgets=GitHistoryBudgets(storage_bytes=1),
        )


def test_incremental_cursor_rejects_since_filtering(
    history_repo: tuple[Path, tuple[str, ...], str],
) -> None:
    repo, commits, _ = history_repo

    with pytest.raises(GitHistoryError, match="mutually exclusive"):
        collect_git_history(
            repo,
            max_commits=None,
            after=commits[0],
            since="2026-01-02T00:00:00Z",
            repository_id=REPOSITORY_ID,
        )


def test_incremental_cursor_fails_closed_on_divergent_history(
    history_repo: tuple[Path, tuple[str, ...], str],
) -> None:
    repo, commits, _ = history_repo
    _git(repo, "switch", "-c", "divergent", commits[0])
    divergent = _commit(
        repo,
        "Divergent work",
        "2026-01-05T10:00:00+00:00",
        "divergent.asset",
    )
    _git(repo, "switch", "main")

    with pytest.raises(GitHistoryError, match="not an ancestor"):
        collect_git_history(
            repo,
            max_commits=None,
            after=divergent,
            repository_id=REPOSITORY_ID,
        )


def test_explicit_budgets_only_reduce_global_safety_caps(
    history_repo: tuple[Path, tuple[str, ...], str],
) -> None:
    repo, _, _ = history_repo

    with pytest.raises(GitHistoryError, match="timeout_seconds"):
        GitHistoryBudgets(timeout_seconds=history_git._GIT_TIMEOUT_SECONDS + 1)
    with pytest.raises(GitHistoryError, match="git_stdout_bytes"):
        GitHistoryBudgets(git_stdout_bytes=history_git._MAX_GIT_STDOUT_BYTES + 1)
    with pytest.raises(GitHistoryError, match="storage_bytes"):
        GitHistoryBudgets(storage_bytes=history_storage.HISTORY_JSONL_MAX_BYTES + 1)

    with pytest.raises(GitHistoryError, match="stdout exceeds 1 bytes"):
        collect_git_history(
            repo,
            repository_id=REPOSITORY_ID,
            budgets=GitHistoryBudgets(git_stdout_bytes=1),
        )

    storage_limited = collect_git_history(
        repo,
        max_commits=None,
        repository_id=REPOSITORY_ID,
        budgets=GitHistoryBudgets(storage_bytes=1),
    )
    assert storage_limited.examined == 0
    assert storage_limited.storage_truncated is True
    assert storage_limited.storage_byte_limit == 1
    assert storage_limited.budgets.to_dict()["storage_bytes"] == 1


def test_canonical_byte_budget_keeps_persistible_most_recent_prefix(
    history_repo: tuple[Path, tuple[str, ...], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, commits, _ = history_repo
    complete = collect_git_history(repo, max_commits=None, repository_id=REPOSITORY_ID)
    # The horizon event is appended after the commit prefix; budget the commits only.
    newest_events = tuple(
        reversed([event for event in complete.events if event.kind in _COMMIT_EVENT_KINDS])
    )
    newest_units = tuple(reversed(complete.units))

    event_prefix_bytes = sum(
        len((canonical_json(event.to_dict()) + "\n").encode("utf-8")) for event in newest_events[:2]
    )
    unit_prefix_bytes = sum(
        len((canonical_json(unit.to_dict()) + "\n").encode("utf-8")) for unit in newest_units[:2]
    )
    byte_budget = max(event_prefix_bytes, unit_prefix_bytes)
    assert (
        sum(
            len((canonical_json(event.to_dict()) + "\n").encode("utf-8"))
            for event in newest_events[:3]
        )
        > byte_budget
    )

    monkeypatch.setattr(history_git, "HISTORY_JSONL_MAX_BYTES", byte_budget)
    monkeypatch.setattr(history_storage, "HISTORY_JSONL_MAX_BYTES", byte_budget)
    report = collect_git_history(repo, max_commits=None, repository_id=REPOSITORY_ID)
    repeated = collect_git_history(repo, max_commits=None, repository_id=REPOSITORY_ID)

    assert repeated == report
    assert report.storage_truncated
    assert report.truncated
    assert report.examined == 2
    assert _commit_shas(report) == {commits[2], commits[3]}
    assert report.event_log_bytes <= byte_budget
    assert report.change_unit_log_bytes <= byte_budget
    assert report.storage_byte_limit == byte_budget
    assert len(report.manifest_hash) == 64
    assert any("canonical storage limits" in warning for warning in report.warnings)

    event_path = tmp_path / "events.jsonl"
    unit_path = tmp_path / "change-units.jsonl"
    save_events(event_path, report.events)
    save_change_units(unit_path, report.units)
    assert event_path.stat().st_size == report.event_log_bytes
    assert unit_path.stat().st_size == report.change_unit_log_bytes


def test_canonical_line_budget_truncates_before_an_unpersistible_record(
    history_repo: tuple[Path, tuple[str, ...], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _, _ = history_repo
    monkeypatch.setattr(history_git, "HISTORY_JSONL_MAX_LINE_BYTES", 1)

    report = collect_git_history(repo, max_commits=None, repository_id=REPOSITORY_ID)

    assert report.storage_truncated
    assert report.truncated
    assert report.examined == 0
    assert report.event_log_bytes == 0
    assert report.change_unit_log_bytes == 0
    assert any("canonical record exceeds 1 bytes" in warning for warning in report.warnings)


@pytest.mark.parametrize("max_commits", [0, -1, True, 100_001, 1.5])
def test_rejects_unsafe_max_commits(
    history_repo: tuple[Path, tuple[str, ...], str],
    max_commits: object,
) -> None:
    repo, _, _ = history_repo

    with pytest.raises(GitHistoryError, match="max_commits"):
        collect_git_history(
            repo,
            max_commits=max_commits,  # type: ignore[arg-type]
            repository_id=REPOSITORY_ID,
        )


@pytest.mark.parametrize("ref", ["--all", "HEAD\nmain", "HEAD\x00main", "x" * 1025])
def test_rejects_unsafe_refs(
    history_repo: tuple[Path, tuple[str, ...], str],
    ref: str,
) -> None:
    repo, _, _ = history_repo

    with pytest.raises(GitHistoryError, match="ref"):
        collect_git_history(repo, ref=ref, repository_id=REPOSITORY_ID)


def test_rejects_naive_or_invalid_since(
    history_repo: tuple[Path, tuple[str, ...], str],
) -> None:
    repo, _, _ = history_repo

    for since in ("2026-01-01T00:00:00", "yesterday"):
        with pytest.raises(GitHistoryError, match="since"):
            collect_git_history(repo, since=since, repository_id=REPOSITORY_ID)


def test_rejects_invalid_repository_identity_even_for_an_empty_time_window(
    history_repo: tuple[Path, tuple[str, ...], str],
) -> None:
    repo, _, _ = history_repo

    with pytest.raises(GitHistoryError, match="repository_id"):
        collect_git_history(
            repo,
            since="2099-01-01T00:00:00Z",
            repository_id="Not a valid identity",
        )


def test_detects_shallow_history_and_excludes_grafted_boundary_commits(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.name", "History Person")
    _git(source, "config", "user.email", "private@example.test")
    _commit(source, "First", "2026-01-01T10:00:00Z", "first")
    second = _commit(source, "Second", "2026-01-02T10:00:00Z", "second")
    third = _commit(source, "Third", "2026-01-03T10:00:00Z", "third")

    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "--depth", "2", f"file://{source}", str(shallow)],
        check=True,
        capture_output=True,
    )
    report = collect_git_history(shallow, max_commits=None, repository_id=REPOSITORY_ID)

    assert report.shallow
    # Git grafts ``Second`` as a parentless boundary; its "diff" would be the whole
    # tree, so it is excluded and only ``Third`` (whose base is still local) remains.
    assert report.shallow_boundary_commits == 1
    assert report.examined == 1
    assert [unit.prediction_sha for unit in report.units] == [third]
    assert report.units[0].base_sha == second
    assert second not in {event.source_ref for event in report.events}
    assert any("shallow" in warning for warning in report.warnings)
    assert any("boundary commit(s) were excluded" in warning for warning in report.warnings)
    assert report.to_dict()["shallow_boundary_commits"] == 1

    single = tmp_path / "single"
    subprocess.run(
        ["git", "clone", "--depth", "1", f"file://{source}", str(single)],
        check=True,
        capture_output=True,
    )
    lone = collect_git_history(single, max_commits=None, repository_id=REPOSITORY_ID)
    assert lone.shallow_boundary_commits == 1
    assert lone.examined == 0


def test_incremental_cursor_rejects_locally_valid_shallow_ancestry(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.name", "History Person")
    _git(source, "config", "user.email", "private@example.test")
    _commit(source, "First", "2026-01-01T10:00:00Z", "first")
    _commit(source, "Second", "2026-01-02T10:00:00Z", "second")
    _commit(source, "Third", "2026-01-03T10:00:00Z", "third")

    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "--depth", "2", f"file://{source}", str(shallow)],
        check=True,
        capture_output=True,
    )
    boundary = _git(shallow, "rev-parse", "HEAD^")
    assert _git(shallow, "rev-parse", "--is-shallow-repository") == "true"
    assert (
        subprocess.run(
            ["git", "-C", str(shallow), "merge-base", "--is-ancestor", boundary, "HEAD"],
            check=False,
            capture_output=True,
        ).returncode
        == 0
    )

    with pytest.raises(GitHistoryError, match="shallow repository"):
        collect_git_history(
            shallow,
            max_commits=None,
            after=boundary,
            repository_id=REPOSITORY_ID,
        )


def test_deduplicates_unexpected_git_records(
    history_repo: tuple[Path, tuple[str, ...], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _, _ = history_repo
    original = history_git._run_git_bounded

    def duplicate_log(
        target: Path,
        arguments: tuple[str, ...],
        *,
        input_bytes: bytes | None = None,
        stdout_limit: int = history_git._MAX_GIT_STDOUT_BYTES,
        budgets: history_git.GitHistoryBudgets | None = None,
    ) -> tuple[bytes, bytes, int]:
        stdout, stderr, returncode = original(
            target,
            arguments,
            input_bytes=input_bytes,
            stdout_limit=stdout_limit,
            budgets=budgets,
        )
        if arguments and arguments[0] == "log":
            stdout += stdout
        return stdout, stderr, returncode

    monkeypatch.setattr(history_git, "_run_git_bounded", duplicate_log)
    report = collect_git_history(repo, max_commits=10, repository_id=REPOSITORY_ID)

    assert report.event_count == 5
    assert len({event.id for event in report.events}) == 5
    assert any("duplicate" in warning for warning in report.warnings)


def test_rejects_non_utf8_commit_metadata(
    history_repo: tuple[Path, tuple[str, ...], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _, _ = history_repo
    original = history_git._run_git_bounded

    def corrupt_log(
        target: Path,
        arguments: tuple[str, ...],
        *,
        input_bytes: bytes | None = None,
        stdout_limit: int = history_git._MAX_GIT_STDOUT_BYTES,
        budgets: history_git.GitHistoryBudgets | None = None,
    ) -> tuple[bytes, bytes, int]:
        stdout, stderr, returncode = original(
            target,
            arguments,
            input_bytes=input_bytes,
            stdout_limit=stdout_limit,
            budgets=budgets,
        )
        if arguments and arguments[0] == "log":
            stdout = stdout.replace(b"Merge feature", b"\xff", 1)
        return stdout, stderr, returncode

    monkeypatch.setattr(history_git, "_run_git_bounded", corrupt_log)
    with pytest.raises(GitHistoryError, match="non-UTF-8"):
        collect_git_history(repo, repository_id=REPOSITORY_ID)


def test_collection_never_invokes_content_or_language_inspection(
    history_repo: tuple[Path, tuple[str, ...], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _, _ = history_repo
    original = history_git._run_git_bounded
    commands: list[tuple[str, ...]] = []

    def record_commands(
        target: Path,
        arguments: tuple[str, ...],
        *,
        input_bytes: bytes | None = None,
        stdout_limit: int = history_git._MAX_GIT_STDOUT_BYTES,
        budgets: history_git.GitHistoryBudgets | None = None,
    ) -> tuple[bytes, bytes, int]:
        commands.append(arguments)
        return original(
            target,
            arguments,
            input_bytes=input_bytes,
            stdout_limit=stdout_limit,
            budgets=budgets,
        )

    monkeypatch.setattr(history_git, "_run_git_bounded", record_commands)
    collect_git_history(repo, repository_id=REPOSITORY_ID)

    assert {arguments[0] for arguments in commands} <= {"rev-parse", "hash-object", "log"}
    assert not any(
        forbidden in arguments
        for arguments in commands
        for forbidden in ("diff", "show", "ls-tree", "cat-file")
    )
