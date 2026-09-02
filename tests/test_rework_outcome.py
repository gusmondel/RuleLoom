from __future__ import annotations

import json
import os
import re
import subprocess
from importlib.resources import files
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from ruleloom import cli
from ruleloom.config import OutcomesConfig, RuleLoomConfig, default_config
from ruleloom.history.git import collect_git_history
from ruleloom.history.materialize import materialize_history, resolve_rework_window
from ruleloom.history.models import ChangeUnit, HistoricalEvent
from ruleloom.history.outcomes import (
    ATOMIC_OUTCOME_TARGETS,
    GIT_LINE_CONTENT_LINK_KIND,
    GIT_REWORK_SCAN_EVENT_KIND,
    POST_MERGE_REWORK,
    ReworkWindow,
    derive_outcome,
    rework_window_from_events,
)
from ruleloom.history.rework import (
    MAX_LINES_PER_COMMIT,
    REWORK_SCAN_ADAPTER_VERSION,
    normalize_line,
    scan_rework,
)
from ruleloom.history.storage import change_units_path, events_path, load_history_snapshot
from ruleloom.models import LabelValue, ModelError
from ruleloom.project import initialize_project, validate_project
from ruleloom.storage import dataset_path, load_observations

REPOSITORY_ID = "repo.rework"
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_D = "d" * 40


def _git(repo: Path, *arguments: str, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return completed.stdout.strip()


def _commit(
    repo: Path,
    paths: dict[str, str],
    timestamp: str,
    *,
    author: str = "Alice",
    message: str = "change",
) -> str:
    for relative, content in paths.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        _git(repo, "add", "--", relative)
    _git(
        repo,
        "commit",
        "-m",
        message,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": author,
            "GIT_AUTHOR_EMAIL": f"{author.lower()}@example.invalid",
            "GIT_COMMITTER_NAME": author,
            "GIT_COMMITTER_EMAIL": f"{author.lower()}@example.invalid",
            "GIT_AUTHOR_DATE": timestamp,
            "GIT_COMMITTER_DATE": timestamp,
        },
    )
    return _git(repo, "rev-parse", "HEAD")


FUNCTION_BODY = """package registry

func Register(name string, flag Flag) error {
    if name == "" {
        return errValidationFailedForEmptyName
    }
    registeredFlags[name] = flag
    metrics.RecordRegistration(name, flag.Stage)
    return nil
}
"""

REWORKED_BODY = """package registry

func Register(name string, flag Flag) error {
    if name == "" {
        return errValidationFailedForEmptyName
    }
    registeredFlags[name] = normalizeFlag(flag)
    return nil
}
"""


@pytest.fixture
def rework_repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    repo = tmp_path / "rework-repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Rework")
    _git(repo, "config", "user.email", "rework@example.invalid")
    base = _commit(repo, {"README.md": "# Registry service\n"}, "2025-01-01T00:00:00Z")
    added = _commit(
        repo,
        {"pkg/registry.go": FUNCTION_BODY},
        "2025-01-02T00:00:00Z",
        author="Alice",
    )
    untouched = _commit(
        repo,
        {"docs/guide.md": "Operational guide for the registry service.\n"},
        "2025-01-03T00:00:00Z",
    )
    reworked_by_other = _commit(
        repo,
        {"pkg/registry.go": REWORKED_BODY},
        "2025-01-10T00:00:00Z",
        author="Bob",
    )
    moved = _commit(
        repo,
        {
            "pkg/registry.go": "package registry\n",
            "pkg/register.go": REWORKED_BODY.replace(
                "package registry\n\n", "package registry\n\n// moved\n"
            ),
        },
        "2025-01-12T00:00:00Z",
        author="Bob",
    )
    later = _commit(
        repo,
        {"docs/guide.md": "Operational guide for the registry service, revised.\n"},
        "2025-03-15T00:00:00Z",
    )
    return repo, {
        "base": base,
        "added": added,
        "untouched": untouched,
        "reworked_by_other": reworked_by_other,
        "moved": moved,
        "later": later,
    }


def test_normalize_line_drops_trivial_content_and_whitespace_differences() -> None:
    assert normalize_line(b"    registeredFlags[name] = flag   ") == b"registeredFlags[name] = flag"
    assert normalize_line(b"\tregisteredFlags[name]\t=\tflag") == b"registeredFlags[name] = flag"
    assert normalize_line(b"}") is None
    assert normalize_line(b"return nil") is None
    assert normalize_line(b"})) })) }))") is None


def test_scan_links_later_deletions_to_the_commit_that_added_the_lines(
    rework_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, shas = rework_repo
    initialize_project(repo, "Rework", target=POST_MERGE_REWORK, rework_window_days=30)
    config = RuleLoomConfig.load(repo)
    history = collect_git_history(
        repo, max_commits=None, repository_id=config.protocol.repository_id
    )

    report = scan_rework(repo, config, history.units, history.events)

    assert report.commits_examined == 6
    assert report.commits_scanned == 6
    assert report.window_days == 30
    assert report.scanned_until == history.units[-1].prediction_at
    rework = [event for event in report.events if event.kind == "rework"]
    assert len(rework) == 1
    event = rework[0]
    assert event.id == f"event.git_rework.{shas['reworked_by_other']}.{shas['added']}"
    assert event.change_id == f"change.git_commit.{shas['added']}"
    assert event.independent_group == f"change.git_commit.{shas['reworked_by_other']}"
    assert event.data["link_kind"] == GIT_LINE_CONTENT_LINK_KIND
    assert event.data["evidence_grade"] == "weak_heuristic"
    assert event.data["reworked_lines"] == 2
    assert event.data["files"] == ["pkg/registry.go"]
    assert event.data["same_author"] is False
    assert event.data["days_after"] == 8.0
    assert event.data["adapter"] == REWORK_SCAN_ADAPTER_VERSION
    scan = report.scan_event
    assert scan.kind == GIT_REWORK_SCAN_EVENT_KIND
    assert scan.change_id is None
    assert scan.data["scanned_until"] == report.scanned_until
    assert scan.data["skipped_shas"] == []
    # The move commit re-added every deleted line elsewhere, so it is not rework.
    assert not any(event.source_ref == shas["moved"] for event in rework)
    again = scan_rework(repo, config, history.units, history.events)
    assert again.events == report.events


def test_scan_skips_oversized_commits_and_records_them(
    rework_repo: tuple[Path, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, shas = rework_repo
    initialize_project(repo, "Rework", target=POST_MERGE_REWORK, rework_window_days=30)
    config = RuleLoomConfig.load(repo)
    history = collect_git_history(
        repo, max_commits=None, repository_id=config.protocol.repository_id
    )
    import ruleloom.history.rework as rework_module

    monkeypatch.setattr(rework_module, "MAX_LINES_PER_COMMIT", 5)

    report = scan_rework(repo, config, history.units, history.events)

    assert report.commits_skipped_large >= 1
    assert shas["added"] in report.scan_event.data["skipped_shas"]
    assert report.rework_events == 0
    assert any("not indexed" in warning for warning in report.warnings)
    assert MAX_LINES_PER_COMMIT == 5_000


def _unit(sha: str, prediction_at: str) -> ChangeUnit:
    return ChangeUnit(
        id=f"change.git_commit.{sha}",
        repository_id=REPOSITORY_ID,
        kind="git_commit",
        base_sha=SHA_A,
        prediction_sha=sha,
        prediction_at=prediction_at,
        commits=(sha,),
        event_ids=(f"event.git_commit.{sha}",),
        provider="git",
        source_ref=sha,
        evidence_quality="git_only",
        confirmatory=False,
    )


def _rework_event(lines: int, *, same_author: bool = False) -> HistoricalEvent:
    return HistoricalEvent(
        id=f"event.git_rework.{SHA_D}.{SHA_B}",
        repository_id=REPOSITORY_ID,
        kind="rework",
        occurred_at="2025-01-10T00:00:00Z",
        available_at="2025-01-10T00:00:00Z",
        provider="git",
        source_ref=SHA_D,
        independent_group=f"change.git_commit.{SHA_D}",
        change_id=f"change.git_commit.{SHA_B}",
        data={
            "linked_change_id": f"change.git_commit.{SHA_B}",
            "link_kind": GIT_LINE_CONTENT_LINK_KIND,
            "evidence_grade": "weak_heuristic",
            "reworked_lines": lines,
            "same_author": same_author,
        },
    )


def _window(**overrides: object) -> ReworkWindow:
    values: dict[str, object] = {
        "window_days": 30,
        "min_lines": 3,
        "ignore_same_author": True,
        "scanned_until": "2025-03-15T00:00:00Z",
        "scan_event_id": "event.git_rework_scan.abc",
    }
    values.update(overrides)
    return ReworkWindow(**values)  # type: ignore[arg-type]


def test_rework_votes_apply_registered_thresholds_and_author_policy() -> None:
    unit = _unit(SHA_B, "2025-01-02T00:00:00Z")

    assert POST_MERGE_REWORK in ATOMIC_OUTCOME_TARGETS
    strong_only = derive_outcome(unit, [_rework_event(4)], POST_MERGE_REWORK)
    weak = derive_outcome(
        unit, [_rework_event(4)], POST_MERGE_REWORK, include_weak=True, rework_window=_window()
    )
    too_few = derive_outcome(
        unit, [_rework_event(2)], POST_MERGE_REWORK, include_weak=True, rework_window=_window()
    )
    same_author = derive_outcome(
        unit,
        [_rework_event(4, same_author=True)],
        POST_MERGE_REWORK,
        include_weak=True,
        rework_window=_window(),
    )
    same_author_kept = derive_outcome(
        unit,
        [_rework_event(4, same_author=True)],
        POST_MERGE_REWORK,
        include_weak=True,
        rework_window=_window(ignore_same_author=False),
    )

    assert strong_only.value is LabelValue.UNKNOWN
    assert weak.value is LabelValue.POSITIVE
    assert weak.evidence is not None
    assert weak.evidence.kind == "incident"
    assert "deleted 4 line(s)" in weak.evidence.reason
    assert too_few.value is LabelValue.NEGATIVE
    assert too_few.evidence is not None
    assert too_few.evidence.available_at == "2025-02-01T00:00:00Z"
    assert too_few.evidence.source == "historical-events:event.git_rework_scan.abc"
    assert same_author.value is LabelValue.NEGATIVE
    assert same_author_kept.value is LabelValue.POSITIVE


def test_rework_window_negative_requires_scan_coverage_and_skips_unindexed_commits() -> None:
    unit = _unit(SHA_B, "2025-03-01T00:00:00Z")

    immature = derive_outcome(
        unit, [], POST_MERGE_REWORK, include_weak=True, rework_window=_window()
    )
    skipped = derive_outcome(
        _unit(SHA_B, "2025-01-02T00:00:00Z"),
        [],
        POST_MERGE_REWORK,
        include_weak=True,
        rework_window=_window(skipped_shas=frozenset({SHA_B})),
    )
    covered = derive_outcome(
        _unit(SHA_B, "2025-01-02T00:00:00Z"),
        [],
        POST_MERGE_REWORK,
        include_weak=True,
        rework_window=_window(),
    )

    assert immature.value is LabelValue.UNKNOWN and immature.votes == ()
    assert skipped.value is LabelValue.UNKNOWN and skipped.votes == ()
    assert covered.value is LabelValue.NEGATIVE
    events = [
        HistoricalEvent(
            id="event.git_rework_scan.one",
            repository_id=REPOSITORY_ID,
            kind=GIT_REWORK_SCAN_EVENT_KIND,
            occurred_at="2025-02-01T00:00:00Z",
            available_at="2025-02-01T00:00:00Z",
            provider="git",
            source_ref=SHA_D,
            independent_group="event.git_rework_scan.one",
            data={"scanned_until": "2025-02-01T00:00:00Z", "skipped_shas": [SHA_A]},
        ),
        HistoricalEvent(
            id="event.git_rework_scan.two",
            repository_id=REPOSITORY_ID,
            kind=GIT_REWORK_SCAN_EVENT_KIND,
            occurred_at="2025-03-01T00:00:00Z",
            available_at="2025-03-01T00:00:00Z",
            provider="git",
            source_ref=SHA_D,
            independent_group="event.git_rework_scan.two",
            data={"scanned_until": "2025-03-01T00:00:00Z", "skipped_shas": []},
        ),
    ]
    window = rework_window_from_events(
        events,
        window_days=14,
        min_lines=2,
        ignore_same_author=False,
        repository_id=REPOSITORY_ID,
    )
    assert window == ReworkWindow(
        window_days=14,
        min_lines=2,
        ignore_same_author=False,
        scanned_until="2025-03-01T00:00:00Z",
        scan_event_id="event.git_rework_scan.two",
    )
    assert (
        rework_window_from_events(
            events,
            window_days=None,
            min_lines=3,
            ignore_same_author=True,
            repository_id=REPOSITORY_ID,
        )
        is None
    )
    with pytest.raises(ModelError, match="min_lines"):
        _window(min_lines=0)


def test_cli_registers_scans_materializes_and_validates_rework_labels(
    rework_repo: tuple[Path, dict[str, str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, shas = rework_repo

    assert (
        cli.main(
            [
                "init",
                str(repo),
                "--target",
                POST_MERGE_REWORK,
                "--rework-window-days",
                "30",
                "--rework-min-lines",
                "2",
            ]
        )
        == 0
    )
    capsys.readouterr()
    config = RuleLoomConfig.load(repo)
    assert config.outcomes.rework_window_days == 30
    assert config.outcomes.rework_min_lines == 2
    assert config.outcomes.rework_ignore_same_author is True
    assert config.evidence_protocol["outcomes"]["rework_window_days"] == 30

    assert cli.main(["history", "--root", str(repo), "bootstrap-git", "--all"]) == 0
    capsys.readouterr()
    assert cli.main(["history", "--root", str(repo), "scan-rework"]) == 0
    scan = json.loads(capsys.readouterr().out)
    assert scan["rework_events"] == 1
    assert scan["reworked_commits"] == 1
    assert scan["events_inserted"] == 2
    assert cli.main(["history", "--root", str(repo), "scan-rework"]) == 0
    again = json.loads(capsys.readouterr().out)
    assert again["events_inserted"] == 0 and again["events_unchanged"] == 2

    assert cli.main(["history", "--root", str(repo), "materialize", "--include-weak"]) == 0
    materialized = json.loads(capsys.readouterr().out)
    assert materialized["rework_window"]["window_days"] == 30
    # base, untouched, reworked_by_other, and moved all closed their windows before the
    # scan horizon without being reworked; only the last commit is still immature.
    assert (materialized["positive"], materialized["negative"], materialized["unknown"]) == (
        1,
        4,
        1,
    )
    assert materialized["rework_window_negatives"] == 4

    observations = load_observations(dataset_path(repo, config))
    by_head = {item.source["head"]: item for item in observations}
    # generic_changes@4 is the schema-v5 default: materialization records the
    # privacy-preserving author hash and enrichment reads the persisted rework ledger.
    assert config.pack_version == 4
    assert all(
        re.fullmatch(r"[0-9a-f]{64}", str(item.metadata["historical_author_hash"]))
        for item in observations
    )
    assert all(
        item.metadata["historical_context"]["version"] == "ruleloom-history-features/3"
        and item.metadata["historical_context"]["rework_history_status"] == "available"
        for item in observations
    )
    assert by_head[shas["added"]].labels[config.target] is LabelValue.POSITIVE
    assert by_head[shas["base"]].labels[config.target] is LabelValue.NEGATIVE
    assert by_head[shas["untouched"]].labels[config.target] is LabelValue.NEGATIVE
    assert by_head[shas["reworked_by_other"]].labels[config.target] is LabelValue.NEGATIVE
    assert by_head[shas["moved"]].labels[config.target] is LabelValue.NEGATIVE
    assert by_head[shas["later"]].labels[config.target] is LabelValue.UNKNOWN
    assert by_head[shas["added"]].metadata["historical_rework_window"]["min_lines"] == 2
    assert all(item.source["confirmatory"] is False for item in observations)
    validate_project(repo, config)
    events, _units = load_history_snapshot(events_path(repo), change_units_path(repo))
    assert resolve_rework_window(config, events) is not None

    strong = materialize_history(
        repo,
        config,
        *reversed(load_history_snapshot(events_path(repo), change_units_path(repo))),
    )
    assert (strong.positive, strong.negative, strong.unknown) == (0, 0, 6)


def _validate_config(payload: dict[str, object]) -> None:
    resource = files("ruleloom").joinpath("schemas", "config.schema.json")
    schema = json.loads(resource.read_text(encoding="utf-8"))
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(payload)


def test_rework_settings_are_bound_into_the_protocol_and_schema() -> None:
    windowed = default_config(
        "Example",
        schema_version=5,
        test_start_at="2026-09-01T00:00:00Z",
        rework_window_days=21,
        rework_min_lines=5,
        rework_ignore_same_author=False,
    )
    plain = default_config("Example", schema_version=5, test_start_at="2026-09-01T00:00:00Z")

    assert windowed.to_dict()["outcomes"] == {
        "git_window_days": None,
        "rework_window_days": 21,
        "rework_min_lines": 5,
        "rework_ignore_same_author": False,
    }
    assert plain.to_dict()["outcomes"] == {"git_window_days": None}
    assert windowed.evidence_protocol_hash != plain.evidence_protocol_hash
    assert RuleLoomConfig.from_dict(windowed.to_dict()) == windowed
    _validate_config(windowed.to_dict())
    with pytest.raises(ModelError, match="rework_min_lines"):
        OutcomesConfig(rework_window_days=10, rework_min_lines=0)
    with pytest.raises(ModelError, match="require"):
        OutcomesConfig.from_dict({"git_window_days": None, "rework_min_lines": 4})
    broken = windowed.to_dict()
    broken["outcomes"] = {"git_window_days": None, "rework_min_lines": 4}
    with pytest.raises(ValidationError):
        _validate_config(broken)
