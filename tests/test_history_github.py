from __future__ import annotations

import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ruleloom.history import github as github_history
from ruleloom.history.github import (
    GhApiClient,
    GitHubHistoryError,
    collect_github_history,
    github_repository_from_origin,
)
from ruleloom.history.outcomes import (
    CHANGE_ATTRIBUTABLE_CI_FAILURE,
    POST_MERGE_DEFECT,
    POST_MERGE_REVERT_OR_HOTFIX,
    VALIDATION_REWORK_REQUIRED,
    derive_outcome,
)
from ruleloom.history.storage import load_history_snapshot, upsert_history_batch
from ruleloom.history.units import validate_change_unit_evidence
from ruleloom.models import JsonValue, LabelValue, canonical_json, content_hash

BASE_SHA = "1" * 40
FIRST_SHA = "2" * 40
FINAL_SHA = "3" * 40
REVERT_SHA = "4" * 40
MERGE_SHA = "5" * 40
REPOSITORY_ID = "repo.github-test"


class FakeGitHubApi:
    def __init__(self, responses: Mapping[str, JsonValue]) -> None:
        self.responses = dict(responses)
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(self, endpoint: str, *, params: Mapping[str, str] | None = None) -> JsonValue:
        self.calls.append((endpoint, dict(params or {})))
        try:
            return self.responses[endpoint]
        except KeyError as exc:
            raise AssertionError(f"unexpected GitHub endpoint: {endpoint}") from exc


def _commit(sha: str, parent: str) -> dict[str, object]:
    return {
        "sha": sha,
        "parents": [{"sha": parent}],
        "commit": {
            "committer": {"date": "2025-01-02T00:00:00Z"},
            "message": "ordinary change",
        },
    }


def _responses(*, timeline: list[dict[str, object]] | None = None) -> dict[str, JsonValue]:
    repository = "acme/widgets"
    return {
        f"repos/{repository}": {"id": 987654},
        f"repos/{repository}/pulls": [{"number": 7}],
        f"repos/{repository}/pulls/7": {
            "number": 7,
            "state": "closed",
            "created_at": "2025-01-02T00:00:00Z",
            "closed_at": "2025-01-05T00:00:00Z",
            "merged_at": "2025-01-05T00:00:00Z",
            "merge_commit_sha": MERGE_SHA,
            "head": {"sha": FINAL_SHA},
            "user": {"id": 101, "login": "pull-author"},
            "title": "untrusted title that must not be stored",
            "body": "untrusted body that must not be stored",
        },
        f"repos/{repository}/pulls/7/commits": [
            _commit(FIRST_SHA, BASE_SHA),
            _commit(FINAL_SHA, FIRST_SHA),
        ],
        f"repos/{repository}/issues/7/timeline": timeline or [],
        f"repos/{repository}/pulls/7/reviews": [
            {
                "id": 501,
                "state": "CHANGES_REQUESTED",
                "submitted_at": "2025-01-03T00:00:00Z",
                "commit_id": FIRST_SHA,
                "user": {"id": 202, "login": "independent-reviewer"},
                "body": "untrusted review body",
            },
            {
                "id": 502,
                "state": "APPROVED",
                "submitted_at": "2025-01-04T00:00:00Z",
                "commit_id": FINAL_SHA,
                "user": {"id": 101, "login": "pull-author"},
            },
        ],
        f"repos/{repository}/commits/{FIRST_SHA}/check-runs": {
            "total_count": 1,
            "check_runs": [
                {
                    "id": 601,
                    "status": "completed",
                    "conclusion": "failure",
                    "completed_at": "2025-01-02T12:00:00Z",
                    "head_sha": FIRST_SHA,
                    "name": "secret internal check name",
                    "app": {"id": 88},
                }
            ],
        },
        f"repos/{repository}/commits/{FINAL_SHA}/check-runs": {
            "total_count": 1,
            "check_runs": [
                {
                    "id": 602,
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": "2025-01-04T12:00:00Z",
                    "head_sha": FINAL_SHA,
                    "name": "secret internal check name",
                    "app": {"id": 88},
                }
            ],
        },
        f"repos/{repository}/commits/{MERGE_SHA}/check-runs": {
            "total_count": 1,
            "check_runs": [
                {
                    "id": 603,
                    "status": "completed",
                    "conclusion": "failure",
                    "completed_at": "2025-01-05T01:00:00Z",
                    "head_sha": MERGE_SHA,
                    "name": "secret internal check name",
                    "app": {"id": 88},
                }
            ],
        },
        f"repos/{repository}/commits": [
            {
                "sha": REVERT_SHA,
                "commit": {
                    "committer": {"date": "2025-01-06T00:00:00Z"},
                    "message": f'Revert "change"\n\nThis reverts commit {FINAL_SHA}.',
                },
            }
        ],
    }


def _outcome_label(
    event_id: int,
    value: str,
    *,
    actor_id: int = 202,
    target: str = VALIDATION_REWORK_REQUIRED,
    created_at: str = "2025-01-03T06:00:00Z",
) -> dict[str, object]:
    return {
        "id": event_id,
        "event": "labeled",
        "created_at": created_at,
        "actor": {"id": actor_id, "login": "must-not-be-persisted"},
        "label": {
            "name": f"ruleloom:outcome:{target}:{value}:complete",
            "color": "ffffff",
        },
    }


def _outcome_unlabel(
    event_id: int,
    value: str,
    *,
    target: str = VALIDATION_REWORK_REQUIRED,
    created_at: str = "2025-01-04T00:00:00Z",
) -> dict[str, object]:
    return {
        "id": event_id,
        "event": "unlabeled",
        "created_at": created_at,
        "actor": {"id": 101, "login": "must-not-be-persisted"},
        "label": {
            "name": f"ruleloom:outcome:{target}:{value}:complete",
            "color": "ffffff",
        },
    }


def _collect(client: FakeGitHubApi):
    return collect_github_history(
        client,
        "acme/widgets",
        REPOSITORY_ID,
        clock=lambda: datetime(2025, 1, 10, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    ("remote", "expected"),
    (
        ("https://github.com/Acme/widgets.git", "Acme/widgets"),
        ("ssh://git@github.com/acme/widgets.git", "acme/widgets"),
        ("git@github.com:acme/widgets.git", "acme/widgets"),
        ("https://example.com/acme/widgets.git", None),
        ("file:///tmp/widgets", None),
        ("file://github.com/acme/widgets", None),
        ("foo://github.com/acme/widgets", None),
        ("//github.com/acme/widgets", None),
        ("http://github.com/acme/widgets", None),
        ("git://github.com/acme/widgets", None),
        ("https://github.com/acme/widgets/extra", None),
        ("https://github.com/acme/widgets.git?token=secret", None),
        (None, None),
    ),
)
def test_github_repository_binding_parses_only_unambiguous_public_origins(
    remote: str | None,
    expected: str | None,
) -> None:
    assert github_repository_from_origin(remote) == expected


def test_gh_api_client_uses_bounded_injectable_runner() -> None:
    commands: list[tuple[str, ...]] = []

    def runner(
        command: tuple[str, ...],
        timeout_seconds: float,
        stdout_limit: int,
        stderr_limit: int,
    ) -> tuple[bytes, bytes, int]:
        commands.append(command)
        assert timeout_seconds == 12
        assert stdout_limit == 1024
        assert stderr_limit == 512
        return b'{"id":987654}', b"", 0

    client = GhApiClient(
        executable="test-gh",
        timeout_seconds=12,
        stdout_limit=1024,
        stderr_limit=512,
        runner=runner,
    )
    assert client.get("repos/acme/widgets", params={"z": "2", "a": "1"}) == {"id": 987654}
    assert commands == [
        (
            "test-gh",
            "api",
            "--hostname",
            "github.com",
            "--method",
            "GET",
            "--header",
            "Accept: application/vnd.github+json",
            "--header",
            "X-GitHub-Api-Version: 2022-11-28",
            "repos/acme/widgets",
            "--raw-field",
            "a=1",
            "--raw-field",
            "z=2",
        )
    ]


def test_bounded_runner_handles_success_failure_limits_and_timeout() -> None:
    stdout, stderr, returncode = github_history._run_bounded(
        (sys.executable, "-c", "import sys; print('ok'); print('note', file=sys.stderr)"),
        2,
        1024,
        1024,
    )
    assert stdout == b"ok\n"
    assert stderr == b"note\n"
    assert returncode == 0

    with pytest.raises(GitHubHistoryError, match="not installed"):
        github_history._run_bounded(
            ("ruleloom-command-that-does-not-exist",),
            1,
            1024,
            1024,
        )
    with pytest.raises(GitHubHistoryError, match="stdout exceeds"):
        github_history._run_bounded(
            (sys.executable, "-c", "print('x' * 4096)"),
            2,
            128,
            1024,
        )
    with pytest.raises(GitHubHistoryError, match="exceeded"):
        github_history._run_bounded(
            (sys.executable, "-c", "import time; time.sleep(1)"),
            0.01,
            1024,
            1024,
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"executable": ""},
        {"executable": "gh\nunsafe"},
        {"timeout_seconds": 0},
        {"stdout_limit": 0},
        {"stderr_limit": 0},
        {"hostname": "github.example.com"},
    ],
)
def test_gh_api_client_rejects_invalid_runtime_bounds(kwargs: dict[str, object]) -> None:
    with pytest.raises(GitHubHistoryError):
        GhApiClient(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("result", "error"),
    [
        ((b"", b"denied", 1), "request failed"),
        ((b"\xff", b"", 0), "non-UTF-8"),
    ],
)
def test_gh_api_client_reports_transport_failures(
    result: tuple[bytes, bytes, int],
    error: str,
) -> None:
    client = GhApiClient(runner=lambda *_args: result)
    with pytest.raises(GitHubHistoryError, match=error):
        client.get("repos/acme/widgets")


def test_internal_bounds_and_type_validators_fail_closed() -> None:
    with pytest.raises(GitHubHistoryError, match="non-empty"):
        github_history._validate_endpoint("")
    with pytest.raises(GitHubHistoryError, match="exceeds"):
        github_history._validate_endpoint("repos/" + "x" * 5000)
    with pytest.raises(GitHubHistoryError, match="parameters"):
        github_history._validate_parameters({f"key{index}": "x" for index in range(33)})
    with pytest.raises(GitHubHistoryError, match="parameter exceeds"):
        github_history._validate_parameters({"key": "x" * 5000})
    with pytest.raises(GitHubHistoryError, match="between"):
        github_history._positive_limit(0, name="limit", maximum=10)
    with pytest.raises(GitHubHistoryError, match="ISO-8601"):
        github_history._normalize_timestamp("not-a-time", name="time")
    with pytest.raises(GitHubHistoryError, match="timezone"):
        github_history._normalize_timestamp(datetime(2025, 1, 1), name="time")
    assert github_history._normalize_timestamp(None, name="time") is None
    assert (
        github_history._normalize_timestamp(
            datetime(2025, 1, 1, 0, 0, 0, 123, tzinfo=UTC),
            name="time",
        )
        == "2025-01-01T00:00:00.000123Z"
    )
    assert github_history._now_utc().tzinfo is UTC
    with pytest.raises(GitHubHistoryError, match="object"):
        github_history._object([], "value")
    with pytest.raises(GitHubHistoryError, match="array"):
        github_history._array({}, "value")
    with pytest.raises(GitHubHistoryError, match="string"):
        github_history._string(None, "value")
    assert github_history._optional_string(None, "value") is None
    with pytest.raises(GitHubHistoryError, match="integer"):
        github_history._integer(-1, "value")
    hashed_id = github_history._event_id("github.repo.abc", "kind", "unsafe_value")
    assert hashed_id.startswith("event.github.repo.abc.kind.")
    assert len(hashed_id.rsplit(".", 1)[-1]) == 64


class SequencedGitHubApi:
    def __init__(self, responses: list[JsonValue]) -> None:
        self.responses = list(responses)

    def get(self, endpoint: str, *, params: Mapping[str, str] | None = None) -> JsonValue:
        del endpoint, params
        if not self.responses:
            raise AssertionError("unexpected extra page")
        return self.responses.pop(0)


def test_check_run_pagination_is_complete_or_explicitly_truncated() -> None:
    first = {"id": 1}
    second = {"id": 2}
    complete = SequencedGitHubApi(
        [
            {"total_count": 2, "check_runs": [first]},
            {"total_count": 2, "check_runs": [second]},
        ]
    )
    assert github_history._collect_check_runs(complete, "repos/a/b/checks", maximum=2) == (
        [first, second],
        False,
    )

    truncated = SequencedGitHubApi([{"total_count": 2, "check_runs": [first]}])
    assert github_history._collect_check_runs(truncated, "repos/a/b/checks", maximum=1) == (
        [first],
        True,
    )

    oversized_page = SequencedGitHubApi([{"total_count": 2, "check_runs": [first, second]}])
    assert github_history._collect_check_runs(oversized_page, "repos/a/b/checks", maximum=1) == (
        [first],
        True,
    )

    changed_total = SequencedGitHubApi(
        [
            {"total_count": 2, "check_runs": [first]},
            {"total_count": 3, "check_runs": [second]},
        ]
    )
    with pytest.raises(GitHubHistoryError, match="total changed"):
        github_history._collect_check_runs(changed_total, "repos/a/b/checks", maximum=3)

    incomplete = SequencedGitHubApi(
        [
            {"total_count": 2, "check_runs": [first]},
            {"total_count": 2, "check_runs": []},
        ]
    )
    with pytest.raises(GitHubHistoryError, match="ended before"):
        github_history._collect_check_runs(incomplete, "repos/a/b/checks", maximum=3)


@pytest.mark.parametrize(
    ("endpoint", "params"),
    [
        ("-unsafe", {}),
        ("users/acme", {}),
        ("repos/acme/../other", {}),
        ("repos/acme/widgets\nmalicious", {}),
        ("repos/acme/widgets", {"-field": "value"}),
        ("repos/acme/widgets", {"field": "value\nmalicious"}),
    ],
)
def test_gh_api_client_rejects_unsafe_arguments(
    endpoint: str,
    params: dict[str, str],
) -> None:
    client = GhApiClient(runner=lambda *_args: (b"{}", b"", 0))
    with pytest.raises(GitHubHistoryError, match=r"unsafe|below repos|dot path"):
        client.get(endpoint, params=params)


def test_gh_api_client_rejects_invalid_or_oversized_runner_output() -> None:
    invalid = GhApiClient(runner=lambda *_args: (b'{"id":1,"id":2}', b"", 0))
    with pytest.raises(GitHubHistoryError, match="invalid JSON"):
        invalid.get("repos/acme/widgets")

    oversized = GhApiClient(
        stdout_limit=2,
        runner=lambda *_args: (b"{}\n", b"", 0),
    )
    with pytest.raises(GitHubHistoryError, match="violated configured output limits"):
        oversized.get("repos/acme/widgets")


def test_collects_deterministic_exploratory_events_without_free_form_text() -> None:
    client = FakeGitHubApi(_responses())
    first = _collect(client)
    second = _collect(FakeGitHubApi(_responses()))

    assert first == second
    assert first.pull_requests_examined == 1
    assert first.pull_requests_normalized == 1
    assert first.pull_requests_skipped == 0
    assert first.truncated is False
    assert first.to_dict()["evidence_grade"] == "exploratory_git_only"
    assert first.to_dict()["outcome_evidence_grades"] == {
        "provider_event": 4,
        "weak_heuristic": 2,
    }
    assert first.to_dict()["collection_budget"] == {
        "policy": "fail_closed",
        "api_requests": {"used": 10, "maximum": 20_000},
        "provider_records": {"used": 11, "maximum": 250_000},
    }
    assert len(first.units) == 1
    unit = first.units[0]
    assert unit.evidence_quality == "git_only"
    assert unit.confirmatory is False
    assert unit.base_sha == BASE_SHA
    assert unit.prediction_sha == FIRST_SHA
    assert unit.final_sha == FINAL_SHA
    assert unit.commits == (FIRST_SHA, FINAL_SHA)
    validate_change_unit_evidence(unit, list(first.events))

    reviews = [event for event in first.events if event.kind == "review"]
    assert len(reviews) == 2
    assert {event.data["decision"] for event in reviews} == {"unspecified"}
    assert all(event.data["category"] == "unspecified" for event in reviews)
    assert {event.data["independent"] for event in reviews} == {True, False}

    checks = [event for event in first.events if event.kind == "ci_run"]
    assert {event.data["conclusion"] for event in checks} == {"failure", "success"}
    assert all(event.data["attributable_to_change"] is False for event in checks)
    assert len({event.independent_group for event in checks}) == 1
    merge_check = next(event for event in checks if event.data["head_sha"] == MERGE_SHA)
    assert merge_check.data["attribution"] == "unattributed_merge_result"
    assert merge_check.data["evidence_grade"] == "weak_heuristic"

    revert = next(event for event in first.events if event.kind == "revert")
    assert revert.data["linked_change_id"] == unit.id
    assert revert.data["link_kind"] == "heuristic"
    assert revert.data["evidence_grade"] == "weak_heuristic"
    assert revert.data["heuristic_id"] == "git_revert_trailer@1"

    serialized = canonical_json(
        {
            "events": [event.to_dict() for event in first.events],
            "units": [item.to_dict() for item in first.units],
        }
    )
    for secret in (
        "pull-author",
        "independent-reviewer",
        "untrusted title",
        "untrusted body",
        "untrusted review body",
        "secret internal check name",
    ):
        assert secret not in serialized


def test_incremental_import_tolerates_mutable_review_and_check_snapshots(
    tmp_path: Path,
) -> None:
    event_path = tmp_path / "events.jsonl"
    unit_path = tmp_path / "change-units.jsonl"
    initial = _collect(FakeGitHubApi(_responses()))
    upsert_history_batch(event_path, initial.events, unit_path, initial.units)

    changed_responses = _responses(
        timeline=[_outcome_label(701, "positive", created_at="2025-01-06T00:00:00Z")]
    )
    reviews = changed_responses["repos/acme/widgets/pulls/7/reviews"]
    assert isinstance(reviews, list)
    assert isinstance(reviews[0], dict)
    reviews[0]["state"] = "DISMISSED"
    merge_checks = changed_responses[f"repos/acme/widgets/commits/{MERGE_SHA}/check-runs"]
    assert isinstance(merge_checks, dict)
    check_runs = merge_checks["check_runs"]
    assert isinstance(check_runs, list)
    assert isinstance(check_runs[0], dict)
    check_runs[0]["conclusion"] = "success"
    check_runs[0]["completed_at"] = "2025-01-05T02:00:00Z"

    updated = _collect(FakeGitHubApi(changed_responses))
    event_counts, unit_counts = upsert_history_batch(
        event_path,
        updated.events,
        unit_path,
        updated.units,
    )
    persisted_events, _persisted_units = load_history_snapshot(event_path, unit_path)

    assert event_counts[0] == 1
    assert unit_counts == (0, 1)
    assert (
        len([event for event in persisted_events if ":check:603:version:" in event.source_ref]) == 2
    )
    review_501 = [event for event in persisted_events if event.source_ref.endswith(":review:501")]
    assert len(review_501) == 1
    assert review_501[0].data["decision"] == "unspecified"
    assert all(":outcome-label:" not in event.source_ref for event in persisted_events)


def test_shared_commit_check_runs_are_scoped_to_each_pull_request() -> None:
    responses = _responses()
    pulls = responses["repos/acme/widgets/pulls"]
    assert isinstance(pulls, list)
    pulls.append({"number": 8})
    original_detail = responses["repos/acme/widgets/pulls/7"]
    original_commits = responses["repos/acme/widgets/pulls/7/commits"]
    assert isinstance(original_detail, dict)
    assert isinstance(original_commits, list)
    responses["repos/acme/widgets/pulls/8"] = {
        **original_detail,
        "number": 8,
        "user": {"id": 303},
    }
    responses["repos/acme/widgets/pulls/8/commits"] = list(original_commits)
    responses["repos/acme/widgets/issues/8/timeline"] = []
    responses["repos/acme/widgets/pulls/8/reviews"] = []

    report = _collect(FakeGitHubApi(responses))
    shared_checks = [event for event in report.events if ":check:601:version:" in event.source_ref]

    assert len(report.units) == 2
    assert len(shared_checks) == 2
    assert len({event.id for event in shared_checks}) == 2
    assert {event.change_id for event in shared_checks} == {
        report.units[0].id,
        report.units[1].id,
    }


def test_empty_git_commit_message_is_not_treated_as_import_failure() -> None:
    responses = _responses()
    responses["repos/acme/widgets/commits"] = [
        {
            "sha": REVERT_SHA,
            "commit": {
                "committer": {"date": "2025-01-06T00:00:00Z"},
                "message": "",
            },
        }
    ]

    report = _collect(FakeGitHubApi(responses))

    assert len(report.units) == 1
    assert all(event.kind != "revert" for event in report.events)


def test_global_collection_budgets_fail_closed_and_are_manifest_bound() -> None:
    request_limited_client = FakeGitHubApi(_responses())
    with pytest.raises(
        GitHubHistoryError,
        match=r"request budget exhausted.*without persistence",
    ):
        collect_github_history(
            request_limited_client,
            "acme/widgets",
            REPOSITORY_ID,
            max_api_requests=9,
            clock=lambda: datetime(2025, 1, 10, tzinfo=UTC),
        )
    assert len(request_limited_client.calls) == 9
    assert request_limited_client.calls[-1][0].endswith(f"/{MERGE_SHA}/check-runs")

    record_limited_client = FakeGitHubApi(_responses())
    with pytest.raises(
        GitHubHistoryError,
        match=r"provider-record budget exhausted.*without persistence",
    ):
        collect_github_history(
            record_limited_client,
            "acme/widgets",
            REPOSITORY_ID,
            max_provider_records=10,
            clock=lambda: datetime(2025, 1, 10, tzinfo=UTC),
        )
    assert len(record_limited_client.calls) == 10
    assert record_limited_client.calls[-1][0] == "repos/acme/widgets/commits"

    baseline = _collect(FakeGitHubApi(_responses()))
    customized = collect_github_history(
        FakeGitHubApi(_responses()),
        "acme/widgets",
        REPOSITORY_ID,
        max_api_requests=100,
        max_provider_records=100,
        clock=lambda: datetime(2025, 1, 10, tzinfo=UTC),
    )
    assert customized.api_requests_used == 10
    assert customized.provider_records_used == 11
    assert customized.max_api_requests == 100
    assert customized.max_provider_records == 100
    assert customized.manifest_hash != baseline.manifest_hash
    assert customized.to_dict()["collection_limits"] == {
        "pull_requests": 1_000,
        "commits_per_pull": 1_000,
        "reviews_per_pull": 1_000,
        "checks_per_commit": 1_000,
        "repository_commits": 10_000,
        "api_requests": 100,
        "provider_records": 100,
    }
    assert content_hash(customized.to_dict()["manifest"]) == customized.manifest_hash


def test_report_rejects_normalized_content_mutation_after_manifest_creation() -> None:
    report = _collect(FakeGitHubApi(_responses()))
    report.events[0].data["tampered"] = True

    with pytest.raises(GitHubHistoryError, match="changed after manifest creation"):
        report.to_dict()


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"max_api_requests": 0}, "max_api_requests"),
        ({"max_api_requests": 100_001}, "max_api_requests"),
        ({"max_provider_records": 0}, "max_provider_records"),
        ({"max_provider_records": 2_000_001}, "max_provider_records"),
    ],
)
def test_rejects_invalid_global_collection_budgets(
    kwargs: dict[str, int],
    error: str,
) -> None:
    client = FakeGitHubApi(_responses())
    with pytest.raises(GitHubHistoryError, match=error):
        collect_github_history(
            client,
            "acme/widgets",
            REPOSITORY_ID,
            clock=lambda: datetime(2025, 1, 10, tzinfo=UTC),
            **kwargs,
        )
    assert client.calls == []


def test_normalized_provider_events_do_not_silently_become_labels() -> None:
    report = _collect(FakeGitHubApi(_responses()))
    unit = report.units[0]

    validation = derive_outcome(
        unit,
        report.events,
        VALIDATION_REWORK_REQUIRED,
        include_weak=True,
    )
    strong_only_ci = derive_outcome(
        unit,
        report.events,
        CHANGE_ATTRIBUTABLE_CI_FAILURE,
    )
    opted_in_ci = derive_outcome(
        unit,
        report.events,
        CHANGE_ATTRIBUTABLE_CI_FAILURE,
        include_weak=True,
    )
    strong_only_revert = derive_outcome(
        unit,
        report.events,
        POST_MERGE_REVERT_OR_HOTFIX,
    )
    opted_in_revert = derive_outcome(
        unit,
        report.events,
        POST_MERGE_REVERT_OR_HOTFIX,
        include_weak=True,
    )
    assert validation.value is LabelValue.UNKNOWN
    assert strong_only_ci.value is LabelValue.UNKNOWN
    assert opted_in_ci.value is LabelValue.POSITIVE
    assert strong_only_revert.value is LabelValue.UNKNOWN
    assert opted_in_revert.value is LabelValue.POSITIVE


def test_archive_timeline_label_names_are_never_outcome_evidence() -> None:
    timeline = [_outcome_label(701, "positive")]
    report = _collect(FakeGitHubApi(_responses(timeline=timeline)))
    serialized = canonical_json(report.to_dict())

    assert all(event.kind != "change_finalized" for event in report.events)
    assert "ruleloom:outcome:" not in serialized
    assert "must-not-be-persisted" not in serialized
    assert report.to_dict()["archive_label_outcome_policy"] == (
        "ignored_mutable_timeline_label_names"
    )
    assert report.to_dict()["manifest"]["archive_label_outcome_policy"] == (
        "ignored_mutable_timeline_label_names"
    )
    assert report.to_dict()["outcome_evidence_grades"] == {
        "provider_event": 4,
        "weak_heuristic": 2,
    }

    derivation = derive_outcome(
        report.units[0],
        report.events,
        VALIDATION_REWORK_REQUIRED,
    )
    assert derivation.value is LabelValue.UNKNOWN
    assert derivation.evidence is None
    assert report.units[0].confirmatory is False


def test_later_label_rename_cannot_retrodate_an_outcome(tmp_path: Path) -> None:
    ordinary = _outcome_label(701, "positive")
    label = ordinary["label"]
    assert isinstance(label, dict)
    label["name"] = "triage"

    first = _collect(FakeGitHubApi(_responses(timeline=[ordinary])))
    renamed = _collect(FakeGitHubApi(_responses(timeline=[_outcome_label(701, "positive")])))

    assert first == renamed
    assert all(event.kind != "change_finalized" for event in renamed.events)

    event_path = tmp_path / "events.jsonl"
    unit_path = tmp_path / "change-units.jsonl"
    upsert_history_batch(event_path, first.events, unit_path, first.units)
    event_counts, unit_counts = upsert_history_batch(
        event_path,
        renamed.events,
        unit_path,
        renamed.units,
    )
    assert event_counts == (0, len(first.events))
    assert unit_counts == (0, len(first.units))


def test_outcome_label_from_pull_author_is_ignored() -> None:
    timeline = [_outcome_label(701, "positive", actor_id=101)]
    report = _collect(FakeGitHubApi(_responses(timeline=timeline)))
    assert all(event.kind != "change_finalized" for event in report.events)
    assert (
        derive_outcome(
            report.units[0],
            report.events,
            VALIDATION_REWORK_REQUIRED,
        ).value
        is LabelValue.UNKNOWN
    )


def test_archive_label_chronology_cannot_upgrade_mutable_names() -> None:
    premature = _collect(
        FakeGitHubApi(
            _responses(
                timeline=[
                    _outcome_label(
                        701,
                        "negative",
                        created_at="2025-01-04T23:59:59Z",
                    )
                ]
            )
        )
    )
    assert all(event.kind != "change_finalized" for event in premature.events)

    simultaneous = _collect(
        FakeGitHubApi(
            _responses(
                timeline=[
                    _outcome_label(
                        702,
                        "negative",
                        created_at="2025-01-05T00:00:00Z",
                    )
                ]
            )
        )
    )
    assert all(event.kind != "change_finalized" for event in simultaneous.events)

    mature = _collect(
        FakeGitHubApi(
            _responses(
                timeline=[
                    _outcome_label(
                        703,
                        "negative",
                        created_at="2025-01-06T00:00:00Z",
                    )
                ]
            )
        )
    )
    derivation = derive_outcome(
        mature.units[0],
        mature.events,
        VALIDATION_REWORK_REQUIRED,
    )
    assert all(event.kind != "change_finalized" for event in mature.events)
    assert derivation.value is LabelValue.UNKNOWN


def test_archive_label_application_and_removal_are_both_ignored() -> None:
    timeline = [
        _outcome_label(701, "positive", created_at="2025-01-03T00:00:00Z"),
        _outcome_unlabel(702, "positive", created_at="2025-01-04T00:00:00Z"),
    ]
    report = _collect(FakeGitHubApi(_responses(timeline=timeline)))

    assertions = [event for event in report.events if event.kind == "change_finalized"]
    assert assertions == []
    assert (
        derive_outcome(
            report.units[0],
            report.events,
            VALIDATION_REWORK_REQUIRED,
        ).value
        is LabelValue.UNKNOWN
    )

    malformed_removal = _outcome_unlabel(
        703,
        "positive",
        created_at="2025-01-04T00:00:00Z",
    )
    malformed_removal.pop("id")
    unaffected = _collect(
        FakeGitHubApi(
            _responses(
                timeline=[
                    _outcome_label(
                        701,
                        "positive",
                        created_at="2025-01-03T00:00:00Z",
                    ),
                    malformed_removal,
                ]
            )
        )
    )
    assert all(event.kind != "change_finalized" for event in unaffected.events)


def test_archive_label_reapplication_remains_non_evidence() -> None:
    timeline = [
        _outcome_label(701, "positive", created_at="2025-01-03T00:00:00Z"),
        _outcome_unlabel(702, "positive", created_at="2025-01-04T00:00:00Z"),
        _outcome_label(703, "positive", created_at="2025-01-06T00:00:00Z"),
    ]
    report = _collect(FakeGitHubApi(_responses(timeline=timeline)))
    assertions = [event for event in report.events if event.kind == "change_finalized"]

    assert assertions == []
    assert (
        derive_outcome(
            report.units[0],
            report.events,
            VALIDATION_REWORK_REQUIRED,
        ).value
        is LabelValue.UNKNOWN
    )


def test_archive_post_merge_label_names_remain_non_evidence() -> None:
    timeline = [
        _outcome_label(
            701,
            "positive",
            target=POST_MERGE_DEFECT,
            created_at="2025-01-03T00:00:00Z",
        ),
        _outcome_label(
            702,
            "positive",
            target=POST_MERGE_DEFECT,
            created_at="2025-01-06T00:00:00Z",
        ),
    ]
    report = _collect(FakeGitHubApi(_responses(timeline=timeline)))
    assertions = [event for event in report.events if event.kind == "change_finalized"]

    assert assertions == []
    assert (
        derive_outcome(report.units[0], report.events, POST_MERGE_DEFECT).value
        is LabelValue.UNKNOWN
    )


def test_conflicting_archive_label_names_remain_unknown_after_unlabel() -> None:
    conflict_timeline = [
        _outcome_label(701, "positive", created_at="2025-01-06T00:00:00Z"),
        _outcome_label(
            702,
            "negative",
            actor_id=303,
            created_at="2025-01-07T00:00:00Z",
        ),
    ]
    conflict = _collect(FakeGitHubApi(_responses(timeline=conflict_timeline)))
    assert (
        derive_outcome(
            conflict.units[0],
            conflict.events,
            VALIDATION_REWORK_REQUIRED,
        ).value
        is LabelValue.UNKNOWN
    )

    removed_timeline = [
        *conflict_timeline,
        _outcome_unlabel(703, "positive", created_at="2025-01-08T00:00:00Z"),
    ]
    removed = _collect(FakeGitHubApi(_responses(timeline=removed_timeline)))
    assertions = [event for event in removed.events if event.kind == "change_finalized"]
    assert assertions == []
    assert (
        derive_outcome(
            removed.units[0],
            removed.events,
            VALIDATION_REWORK_REQUIRED,
        ).value
        is LabelValue.UNKNOWN
    )


def test_archive_label_removal_and_cutoff_never_create_outcomes() -> None:
    timeline = [
        _outcome_label(701, "positive", created_at="2025-01-06T00:00:00Z"),
        _outcome_unlabel(702, "positive", created_at="2025-01-08T00:00:00Z"),
    ]
    report = collect_github_history(
        FakeGitHubApi(_responses(timeline=timeline)),
        "acme/widgets",
        REPOSITORY_ID,
        until="2025-01-07T00:00:00Z",
        clock=lambda: datetime(2025, 1, 10, tzinfo=UTC),
    )
    assertions = [event for event in report.events if event.kind == "change_finalized"]
    assert assertions == []

    later = _collect(FakeGitHubApi(_responses(timeline=timeline)))
    later_assertions = [event for event in later.events if event.kind == "change_finalized"]
    assert later_assertions == assertions


def test_malformed_or_premature_outcome_labels_are_ignored() -> None:
    malformed = [
        {"event": "commented"},
        {"event": "labeled", "label": None, "actor": {"id": 202}},
        {
            "event": "labeled",
            "label": {"name": "ordinary-label"},
            "actor": {"id": 202},
        },
        _outcome_label(702, "positive", target="unsupported_target"),
        _outcome_label(703, "unknown"),
        {**_outcome_label(704, "positive"), "id": True},
        {**_outcome_label(705, "positive"), "actor": {"id": True}},
        {**_outcome_label(706, "positive"), "created_at": "not-a-time"},
        _outcome_label(707, "positive", created_at="2025-01-02T00:00:00Z"),
    ]
    report = _collect(FakeGitHubApi(_responses(timeline=malformed)))
    assert all(event.kind != "change_finalized" for event in report.events)


def test_conflicting_complete_archive_label_names_are_ignored() -> None:
    timeline = [
        _outcome_label(701, "positive", actor_id=202),
        _outcome_label(
            702,
            "negative",
            actor_id=303,
            created_at="2025-01-06T00:00:00Z",
        ),
    ]
    report = _collect(FakeGitHubApi(_responses(timeline=timeline)))
    derivation = derive_outcome(
        report.units[0],
        report.events,
        VALIDATION_REWORK_REQUIRED,
    )
    assert all(event.kind != "change_finalized" for event in report.events)
    assert derivation.value is LabelValue.UNKNOWN
    assert derivation.evidence is None
    assert derivation.votes == ()


def test_skips_force_pushed_and_open_pull_requests() -> None:
    responses = _responses(timeline=[{"event": "head_ref_force_pushed"}])
    responses["repos/acme/widgets/pulls"].append({"number": 8})  # type: ignore[union-attr]
    responses["repos/acme/widgets/pulls/8"] = {
        "number": 8,
        "state": "open",
        "created_at": "2025-01-02T00:00:00Z",
        "closed_at": None,
        "merged_at": None,
        "head": {"sha": FIRST_SHA},
        "user": {"id": 303},
    }
    responses["repos/acme/widgets/pulls/8/commits"] = [_commit(FIRST_SHA, BASE_SHA)]
    responses["repos/acme/widgets/issues/8/timeline"] = []
    responses["repos/acme/widgets/pulls/8/reviews"] = []

    client = FakeGitHubApi(responses)
    report = _collect(client)
    assert report.pull_requests_examined == 2
    assert report.pull_requests_normalized == 0
    assert report.pull_requests_skipped == 2
    assert report.events == ()
    assert report.units == ()
    assert any("force-push" in warning for warning in report.warnings)
    assert any("closed-only query" in warning for warning in report.warnings)
    assert all("/pulls/8/commits" not in endpoint for endpoint, _params in client.calls)
    assert all("/issues/8/timeline" not in endpoint for endpoint, _params in client.calls)
    assert all("/pulls/8/reviews" not in endpoint for endpoint, _params in client.calls)


def test_pull_finalized_after_cutoff_is_explicitly_omitted() -> None:
    report = collect_github_history(
        FakeGitHubApi(_responses()),
        "acme/widgets",
        REPOSITORY_ID,
        until="2025-01-04T23:59:59Z",
        clock=lambda: datetime(2025, 1, 10, tzinfo=UTC),
    )

    assert report.pull_requests_examined == 1
    assert report.pull_requests_normalized == 0
    assert report.pull_requests_skipped == 1
    assert report.events == ()
    assert report.units == ()
    assert any("finalized after the requested cutoff" in item for item in report.warnings)


def test_until_is_a_local_cutoff_for_reviews_checks_labels_and_reverts() -> None:
    responses = _responses(
        timeline=[_outcome_label(701, "positive", created_at="2025-01-06T00:00:00Z")]
    )
    reviews = responses["repos/acme/widgets/pulls/7/reviews"]
    assert isinstance(reviews, list)
    reviews.append(
        {
            "id": 503,
            "state": "CHANGES_REQUESTED",
            "submitted_at": "2025-01-06T00:00:00Z",
            "commit_id": FINAL_SHA,
            "user": {"id": 202},
        }
    )
    cutoff = "2025-01-05T00:30:00Z"
    report = collect_github_history(
        FakeGitHubApi(responses),
        "acme/widgets",
        REPOSITORY_ID,
        until=cutoff,
        clock=lambda: datetime(2025, 1, 10, tzinfo=UTC),
    )

    assert len(report.units) == 1
    assert all(event.occurred_at <= cutoff for event in report.events)
    assert all(event.available_at <= cutoff for event in report.events)
    assert all(event.kind != "change_finalized" for event in report.events)
    assert all(event.kind != "revert" for event in report.events)
    assert all(event.data.get("head_sha") != MERGE_SHA for event in report.events)
    assert len([event for event in report.events if event.kind == "review"]) == 2


def test_fails_closed_when_head_and_commit_lineage_disagree() -> None:
    responses = _responses()
    detail = responses["repos/acme/widgets/pulls/7"]
    assert isinstance(detail, dict)
    detail["head"] = {"sha": "9" * 40}

    with pytest.raises(GitHubHistoryError, match="head does not match"):
        _collect(FakeGitHubApi(responses))


def test_reports_bounded_pull_request_truncation() -> None:
    responses = _responses()
    pulls = responses["repos/acme/widgets/pulls"]
    assert isinstance(pulls, list)
    pulls.append({"number": 8})

    client = FakeGitHubApi(responses)
    report = collect_github_history(
        client,
        "acme/widgets",
        REPOSITORY_ID,
        since="2025-01-01T00:00:00Z",
        max_pull_requests=1,
        clock=lambda: datetime(2025, 1, 10, tzinfo=UTC),
    )
    assert report.truncated is True
    assert report.pull_requests_examined == 1
    assert report.pull_requests_normalized == 1
    assert any("newest-first" in warning for warning in report.warnings)
    pulls_call = next(call for call in client.calls if call[0] == "repos/acme/widgets/pulls")
    assert pulls_call[1] == {
        "direction": "desc",
        "page": "1",
        "per_page": "100",
        "sort": "created",
        "state": "closed",
    }


@pytest.mark.parametrize(
    ("repository", "repository_id", "error"),
    [
        ("-owner/repo", REPOSITORY_ID, "OWNER/NAME"),
        ("owner/..", REPOSITORY_ID, "OWNER/NAME"),
        ("owner/repo\nmalicious", REPOSITORY_ID, "OWNER/NAME"),
        ("owner/repo", "Invalid Repository", "repository_id"),
    ],
)
def test_rejects_unsafe_repository_boundaries(
    repository: str,
    repository_id: str,
    error: str,
) -> None:
    with pytest.raises(GitHubHistoryError, match=error):
        collect_github_history(
            FakeGitHubApi({}),
            repository,
            repository_id,
            clock=lambda: datetime(2025, 1, 10, tzinfo=UTC),
        )
