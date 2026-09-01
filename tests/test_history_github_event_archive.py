from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ruleloom.history.github_event_archive import (
    GITHUB_EVENT_ARCHIVE_ADAPTER_VERSION,
    GITHUB_EVENT_ARCHIVE_EXPORTER_VERSION,
    GITHUB_EVENT_ARCHIVE_ROW_SCHEMA_VERSION,
    GitHubEventArchiveError,
    build_clickhouse_file_hours_query,
    build_clickhouse_gharchive_query,
    normalize_github_event_archive,
)
from ruleloom.history.outcomes import (
    INDEPENDENT_REVIEW_CHANGES_REQUESTED,
    derive_outcome,
)
from ruleloom.models import LabelValue, canonical_json

BASE_SHA = "1" * 40
OPEN_SHA = "2" * 40
FINAL_SHA = "3" * 40
REPOSITORY_ID = "repo.event-archive-test"
REPOSITORY = "acme/widgets"


def _actor(value: str) -> str:
    return "github.login." + hashlib.sha256(value.encode()).hexdigest()


def _row(
    number: int,
    event_type: str,
    action: str,
    occurred_at: str,
    actor: str,
    *,
    head_sha: str = OPEN_SHA,
    review_state: str = "none",
    additions: int = 10,
    deletions: int = 2,
    changed_files: int = 1,
) -> dict[str, object]:
    return {
        "schema_version": GITHUB_EVENT_ARCHIVE_ROW_SCHEMA_VERSION,
        "event_type": event_type,
        "repository": REPOSITORY,
        "occurred_at": occurred_at,
        "available_at": occurred_at,
        "action": action,
        "actor_key": _actor(actor),
        "number": number,
        "base_sha": BASE_SHA,
        "head_sha": head_sha,
        "review_state": review_state,
        "additions": additions,
        "deletions": deletions,
        "changed_files": changed_files,
        "statistics_complete": event_type == "PullRequestEvent" and action == "opened",
    }


def _write_archive(
    tmp_path: Path,
    rows: list[dict[str, object]],
    *,
    complete: bool = True,
) -> tuple[Path, Path]:
    events = tmp_path / "events.jsonl"
    content = "".join(canonical_json(row) + "\n" for row in rows).encode()
    events.write_bytes(content)
    manifest = {
        "schema_version": 1,
        "event_schema_version": GITHUB_EVENT_ARCHIVE_ROW_SCHEMA_VERSION,
        "adapter_version": GITHUB_EVENT_ARCHIVE_ADAPTER_VERSION,
        "exporter_version": GITHUB_EVENT_ARCHIVE_EXPORTER_VERSION,
        "source": "gharchive-clickhouse-public",
        "source_url": "https://play.clickhouse.com/",
        "repository": REPOSITORY,
        "provider_repository_id": 123456,
        "collection_start": "2025-01-01T00:00:00Z",
        "collection_end": "2025-03-01T00:00:00Z",
        "dataset_max_at": "2025-03-02T00:00:00Z",
        "collected_at": "2025-03-03T00:00:00Z",
        "query_sha256": "a" * 64,
        "coverage_query_sha256": "c" * 64,
        "events_sha256": hashlib.sha256(content).hexdigest(),
        "preregistration_sha256": "b" * 64,
        "window_complete": complete,
        "expected_hours": 1_416,
        "observed_hours": 1_416,
        "missing_hours": [],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(canonical_json(manifest) + "\n", encoding="utf-8")
    return events, manifest_path


def test_event_archive_produces_temporal_positive_negative_and_unknown(
    tmp_path: Path,
) -> None:
    rows = [
        _row(1, "PullRequestEvent", "opened", "2025-01-02T10:00:00Z", "author-one"),
        _row(
            1,
            "PullRequestReviewEvent",
            "created",
            "2025-01-03T10:00:00Z",
            "reviewer-one",
            review_state="changes_requested",
        ),
        _row(
            1,
            "PullRequestEvent",
            "merged",
            "2025-01-04T10:00:00Z",
            "merger",
            head_sha=FINAL_SHA,
        ),
        _row(2, "PullRequestEvent", "opened", "2025-01-05T10:00:00Z", "author-two"),
        _row(
            2,
            "PullRequestReviewEvent",
            "created",
            "2025-01-06T10:00:00Z",
            "reviewer-two",
            review_state="approved",
        ),
        _row(
            2,
            "PullRequestEvent",
            "merged",
            "2025-01-07T10:00:00Z",
            "merger",
            head_sha=FINAL_SHA,
        ),
        _row(3, "PullRequestEvent", "opened", "2025-01-08T10:00:00Z", "author-three"),
        _row(
            3,
            "PullRequestEvent",
            "merged",
            "2025-01-09T10:00:00Z",
            "merger",
            head_sha=FINAL_SHA,
        ),
    ]
    events_path, manifest_path = _write_archive(tmp_path, rows)

    report = normalize_github_event_archive(
        events_path,
        manifest_path,
        repository_id=REPOSITORY_ID,
    )

    assert report.rows_read == 8
    assert report.pulls_opened == 3
    assert report.pulls_merged == 3
    assert report.reviews == 2
    assert report.negative_outcomes == 1
    assert len(report.units) == 3
    assert all(unit.confirmatory and unit.evidence_quality == "rich" for unit in report.units)
    assert report.opening_statistics_complete == 3
    snapshots = [event for event in report.events if event.kind == "change_snapshot"]
    assert snapshots[0].data["diff_statistics"] == {
        "additions": 10,
        "deletions": 2,
        "files_changed": 1,
        "complete": True,
        "source": "github_event_archive_opened_event",
    }
    by_number = {int(unit.source_ref.rsplit(":", 1)[1]): unit for unit in report.units}
    positive = derive_outcome(by_number[1], report.events, INDEPENDENT_REVIEW_CHANGES_REQUESTED)
    negative = derive_outcome(by_number[2], report.events, INDEPENDENT_REVIEW_CHANGES_REQUESTED)
    unknown = derive_outcome(by_number[3], report.events, INDEPENDENT_REVIEW_CHANGES_REQUESTED)
    assert positive.value is LabelValue.POSITIVE
    assert positive.evidence is not None and positive.evidence.kind == "review"
    assert negative.value is LabelValue.NEGATIVE
    assert negative.evidence is not None and negative.evidence.kind == "imported"
    assert unknown.value is LabelValue.UNKNOWN


def test_author_review_is_not_independent_and_incomplete_window_never_implies_negative(
    tmp_path: Path,
) -> None:
    rows = [
        _row(1, "PullRequestEvent", "opened", "2025-01-02T10:00:00Z", "author"),
        _row(
            1,
            "PullRequestReviewEvent",
            "created",
            "2025-01-03T10:00:00Z",
            "author",
            review_state="changes_requested",
        ),
        _row(
            1,
            "PullRequestReviewEvent",
            "created",
            "2025-01-03T11:00:00Z",
            "reviewer",
            review_state="approved",
        ),
        _row(
            1,
            "PullRequestEvent",
            "merged",
            "2025-01-04T10:00:00Z",
            "merger",
            head_sha=FINAL_SHA,
        ),
    ]
    events_path, manifest_path = _write_archive(tmp_path, rows, complete=False)
    report = normalize_github_event_archive(
        events_path,
        manifest_path,
        repository_id=REPOSITORY_ID,
    )

    result = derive_outcome(report.units[0], report.events, INDEPENDENT_REVIEW_CHANGES_REQUESTED)
    assert result.value is LabelValue.UNKNOWN
    assert report.negative_outcomes == 0


def test_missing_source_hour_inside_review_interval_prevents_negative(tmp_path: Path) -> None:
    rows = [
        _row(1, "PullRequestEvent", "opened", "2025-01-02T10:00:00Z", "author"),
        _row(
            1,
            "PullRequestReviewEvent",
            "created",
            "2025-01-03T10:00:00Z",
            "reviewer",
            review_state="approved",
        ),
        _row(
            1,
            "PullRequestEvent",
            "merged",
            "2025-01-04T10:00:00Z",
            "merger",
            head_sha=FINAL_SHA,
        ),
    ]
    events_path, manifest_path = _write_archive(tmp_path, rows)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["observed_hours"] = 1_415
    manifest["missing_hours"] = ["2025-01-03T11:00:00Z"]
    manifest_path.write_text(canonical_json(manifest) + "\n", encoding="utf-8")

    report = normalize_github_event_archive(
        events_path,
        manifest_path,
        repository_id=REPOSITORY_ID,
    )

    result = derive_outcome(report.units[0], report.events, INDEPENDENT_REVIEW_CHANGES_REQUESTED)
    assert result.value is LabelValue.UNKNOWN
    assert report.negative_outcomes == 0


def test_missing_source_hours_outside_review_interval_preserve_negative(tmp_path: Path) -> None:
    rows = [
        _row(1, "PullRequestEvent", "opened", "2025-01-03T10:00:00Z", "author"),
        _row(
            1,
            "PullRequestReviewEvent",
            "created",
            "2025-01-04T10:00:00Z",
            "reviewer",
            review_state="approved",
        ),
        _row(
            1,
            "PullRequestEvent",
            "merged",
            "2025-01-05T10:00:00Z",
            "merger",
            head_sha=FINAL_SHA,
        ),
    ]
    events_path, manifest_path = _write_archive(tmp_path, rows)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["observed_hours"] = 1_414
    manifest["missing_hours"] = [
        "2025-01-01T00:00:00Z",
        "2025-02-01T00:00:00Z",
    ]
    manifest_path.write_text(canonical_json(manifest) + "\n", encoding="utf-8")

    report = normalize_github_event_archive(
        events_path,
        manifest_path,
        repository_id=REPOSITORY_ID,
    )

    result = derive_outcome(report.units[0], report.events, INDEPENDENT_REVIEW_CHANGES_REQUESTED)
    assert result.value is LabelValue.NEGATIVE
    assert report.negative_outcomes == 1


def test_duplicate_rows_are_idempotent_and_content_hash_is_enforced(tmp_path: Path) -> None:
    opening = _row(1, "PullRequestEvent", "opened", "2025-01-02T10:00:00Z", "author")
    events_path, manifest_path = _write_archive(tmp_path, [opening, opening])

    report = normalize_github_event_archive(
        events_path,
        manifest_path,
        repository_id=REPOSITORY_ID,
    )
    assert report.rows_read == 2
    assert report.duplicate_rows == 1
    assert len(report.units) == 1

    events_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(GitHubEventArchiveError, match="hash does not match"):
        normalize_github_event_archive(
            events_path,
            manifest_path,
            repository_id=REPOSITORY_ID,
        )


def test_clickhouse_query_is_bounded_and_excludes_prose_and_mutable_labels() -> None:
    query = build_clickhouse_gharchive_query(
        "apache/airflow",
        "2024-07-01T00:00:00Z",
        "2026-07-01T00:00:00Z",
    )

    assert "repo_name = 'apache/airflow'" in query
    assert "created_at >=" in query and "created_at <" in query
    assert "PullRequestReviewEvent" in query
    assert "changes_requested" in query
    assert "SHA256(actor_login)" in query
    assert "toUInt64(additions)" in query
    assert "statistics_complete" in query
    for forbidden in ("body", "title", "labels", "actor_login AS", "path"):
        assert forbidden not in query
    assert query == build_clickhouse_gharchive_query(
        "apache/airflow",
        "2024-07-01T00:00:00Z",
        "2026-07-01T00:00:00Z",
    )
    coverage_query = build_clickhouse_file_hours_query(
        "2024-07-01T00:00:00Z",
        "2026-07-01T00:00:00Z",
    )
    assert "toStartOfHour(file_time)" in coverage_query
    assert "repo_name" not in coverage_query


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_version", 9, "unsupported.*manifest version"),
        ("event_schema_version", 9, "unsupported.*row version"),
        ("adapter_version", "other", "unexpected.*adapter"),
        ("exporter_version", "other", "unexpected.*exporter"),
        ("source", "other", "unexpected.*source"),
        ("repository", "unsafe repository", "OWNER/NAME"),
        ("collection_end", "2024-01-01T00:00:00Z", "must follow"),
        ("dataset_max_at", "2025-01-01T00:00:00Z", "must cover"),
        ("collected_at", "2025-01-01T00:00:00Z", "cannot predate"),
        ("query_sha256", "A" * 64, "lowercase SHA-256"),
        ("source_url", "http://play.clickhouse.com/", "HTTPS origin"),
        ("source_url", "https://play.clickhouse.com/query", "HTTPS origin"),
        ("source_url", "https://play.clickhouse.com:bad/", "invalid port"),
        ("window_complete", "true", "must be a boolean"),
        ("expected_hours", 1_415, "does not match the collection window"),
        ("observed_hours", 1_415, "does not reconcile"),
    ],
)
def test_manifest_rejects_unsafe_or_incoherent_fields(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    events_path, manifest_path = _write_archive(
        tmp_path,
        [_row(1, "PullRequestEvent", "opened", "2025-01-02T10:00:00Z", "author")],
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[field] = value
    manifest_path.write_text(canonical_json(manifest) + "\n", encoding="utf-8")

    with pytest.raises(GitHubEventArchiveError, match=message):
        normalize_github_event_archive(events_path, manifest_path, repository_id=REPOSITORY_ID)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"schema_version": 9}, "unsupported.*row version"),
        ({"event_type": "IssuesEvent"}, "unsupported event_type"),
        ({"review_state": "approved"}, "invalid pull-request archive"),
        ({"actor_key": "github.login.plaintext"}, "pseudonymized"),
        ({"available_at": "2025-01-01T09:00:00Z"}, "cannot predate"),
        ({"statistics_complete": "true"}, "must be a boolean"),
        ({"statistics_complete": False}, "must identify an opening row"),
        ({"number": 0}, "positive integer"),
        ({"additions": -1}, "non-negative integer"),
    ],
)
def test_archive_rows_reject_unsafe_or_incoherent_values(
    tmp_path: Path,
    mutation: dict[str, object],
    message: str,
) -> None:
    row = _row(1, "PullRequestEvent", "opened", "2025-01-02T10:00:00Z", "author")
    row.update(mutation)
    events_path, manifest_path = _write_archive(tmp_path, [row])

    with pytest.raises(GitHubEventArchiveError, match=message):
        normalize_github_event_archive(events_path, manifest_path, repository_id=REPOSITORY_ID)


def test_archive_rejects_bad_json_window_repository_and_query_bounds(tmp_path: Path) -> None:
    row = _row(1, "PullRequestEvent", "opened", "2025-01-02T10:00:00Z", "author")
    events_path, manifest_path = _write_archive(tmp_path, [row])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    for bad_row, message in (
        ([], "must be an object"),
        (
            {
                **row,
                "occurred_at": "2026-01-02T10:00:00Z",
                "available_at": "2026-01-02T10:00:00Z",
            },
            "outside the manifest window",
        ),
        ({**row, "repository": "acme/other"}, "does not match the manifest"),
    ):
        content = (canonical_json(bad_row) + "\n").encode()
        events_path.write_bytes(content)
        manifest["events_sha256"] = hashlib.sha256(content).hexdigest()
        manifest_path.write_text(canonical_json(manifest) + "\n", encoding="utf-8")
        with pytest.raises(GitHubEventArchiveError, match=message):
            normalize_github_event_archive(events_path, manifest_path, repository_id=REPOSITORY_ID)

    with pytest.raises(GitHubEventArchiveError, match="safe OWNER/NAME"):
        build_clickhouse_gharchive_query(
            "apache/airflow'", "2025-01-01T00:00:00Z", "2026-01-01T00:00:00Z"
        )
    with pytest.raises(GitHubEventArchiveError, match="until must follow"):
        build_clickhouse_gharchive_query(
            "apache/airflow", "2026-01-01T00:00:00Z", "2025-01-01T00:00:00Z"
        )
    with pytest.raises(GitHubEventArchiveError, match="whole-second precision"):
        build_clickhouse_gharchive_query(
            "apache/airflow", "2025-01-01T00:00:00.1Z", "2026-01-01T00:00:00Z"
        )
