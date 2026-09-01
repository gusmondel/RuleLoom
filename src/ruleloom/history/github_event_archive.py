"""Point-in-time GitHub event-archive normalization.

The normalizer consumes a deliberately small JSONL projection of GH Archive.
It does not persist repository prose, labels, paths, review bodies, or account
names.  Account names are pseudonymized by the exporter before they cross this
boundary.

Unlike :mod:`ruleloom.history.github`, an opening event contains the exact base
and head SHA that were public at prediction time.  A complete, bounded event
window can also establish the negative atomic outcome used by the public case
study: a pull request was merged after an independent approval and no
independent reviewer requested changes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from ruleloom.history.models import ChangeUnit, HistoricalEvent, validate_git_sha
from ruleloom.history.outcomes import INDEPENDENT_REVIEW_CHANGES_REQUESTED
from ruleloom.history.storage import (
    HISTORY_JSONL_MAX_BYTES,
    HISTORY_JSONL_MAX_LINE_BYTES,
    HISTORY_JSONL_MAX_RECORDS,
)
from ruleloom.history.units import assemble_change_units, validate_history_snapshot
from ruleloom.models import (
    JsonObject,
    JsonValue,
    ModelError,
    content_hash,
    parse_timestamp,
    strict_json_loads,
    validate_subject,
)

GITHUB_EVENT_ARCHIVE_ADAPTER_VERSION = "ruleloom-github-event-archive/2"
GITHUB_EVENT_ARCHIVE_EXPORTER_VERSION = "ruleloom-gharchive-clickhouse/2"
GITHUB_EVENT_ARCHIVE_MANIFEST_SCHEMA_VERSION = 1
GITHUB_EVENT_ARCHIVE_ROW_SCHEMA_VERSION = 2
GITHUB_EVENT_ARCHIVE_MAX_MANIFEST_BYTES = 64 * 1024
GITHUB_EVENT_ARCHIVE_MAX_MISSING_HOURS = 2_048

_SOURCE = "gharchive-clickhouse-public"
_EVENT_TYPES = frozenset({"PullRequestEvent", "PullRequestReviewEvent"})
_PULL_ACTIONS = frozenset({"opened", "merged"})
_REVIEW_ACTIONS = frozenset({"created"})
_REVIEW_STATES = frozenset({"approved", "changes_requested"})
_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
_ACTOR_KEY_RE = re.compile(r"^github\.login\.[0-9a-f]{64}$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}$")


class GitHubEventArchiveError(ModelError):
    """Raised when an event archive cannot be verified or normalized safely."""


def _expect_exact_fields(value: JsonObject, expected: frozenset[str], name: str) -> None:
    unknown = set(value).difference(expected)
    missing = expected.difference(value)
    if unknown or missing:
        detail = ", ".join(sorted(unknown or missing))
        qualifier = "unknown" if unknown else "missing"
        raise GitHubEventArchiveError(f"{qualifier} {name} fields: {detail}")


def _string(value: JsonValue, name: str, *, maximum_bytes: int = 4096) -> str:
    if not isinstance(value, str) or not value:
        raise GitHubEventArchiveError(f"{name} must be a non-empty string")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise GitHubEventArchiveError(f"{name} cannot contain control characters")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise GitHubEventArchiveError(f"{name} exceeds {maximum_bytes} bytes")
    return value


def _integer(value: JsonValue, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2**63 - 1:
        raise GitHubEventArchiveError(f"{name} must be a positive integer")
    return value


def _nonnegative_integer(value: JsonValue, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**63 - 1:
        raise GitHubEventArchiveError(f"{name} must be a non-negative integer")
    return value


def _digest(value: JsonValue, name: str) -> str:
    digest = _string(value, name, maximum_bytes=64)
    if _HEX_64_RE.fullmatch(digest) is None:
        raise GitHubEventArchiveError(f"{name} must be a lowercase SHA-256 digest")
    return digest


def _timestamp(value: JsonValue, name: str) -> str:
    timestamp = _string(value, name, maximum_bytes=64)
    try:
        parse_timestamp(timestamp)
    except ModelError as exc:
        raise GitHubEventArchiveError(f"invalid {name}: {exc}") from exc
    return timestamp


def _read_regular_file(path: Path, *, maximum: int, name: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise GitHubEventArchiveError(
            f"{name} must be a readable regular, non-symlink file: {path}: {exc}"
        ) from exc
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise GitHubEventArchiveError(f"{name} must be a regular, non-symlink file: {path}")
        if file_stat.st_size > maximum:
            raise GitHubEventArchiveError(f"{name} exceeds {maximum} bytes: {path}")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            content = handle.read(maximum + 1)
        if len(content) > maximum:
            raise GitHubEventArchiveError(f"{name} exceeds {maximum} bytes: {path}")
        return content
    except OSError as exc:
        raise GitHubEventArchiveError(f"cannot read {name} {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


@dataclass(frozen=True, slots=True)
class GitHubEventArchiveManifest:
    """Collection boundary and content hashes for one event-archive export."""

    repository: str
    provider_repository_id: int
    collection_start: str
    collection_end: str
    dataset_max_at: str
    collected_at: str
    query_sha256: str
    events_sha256: str
    preregistration_sha256: str
    window_complete: bool
    coverage_query_sha256: str
    expected_hours: int
    observed_hours: int
    missing_hours: tuple[str, ...]
    source_url: str = "https://play.clickhouse.com/"
    source: str = _SOURCE
    adapter_version: str = GITHUB_EVENT_ARCHIVE_ADAPTER_VERSION
    exporter_version: str = GITHUB_EVENT_ARCHIVE_EXPORTER_VERSION
    event_schema_version: int = GITHUB_EVENT_ARCHIVE_ROW_SCHEMA_VERSION
    schema_version: int = GITHUB_EVENT_ARCHIVE_MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != GITHUB_EVENT_ARCHIVE_MANIFEST_SCHEMA_VERSION:
            raise GitHubEventArchiveError("unsupported GitHub event-archive manifest version")
        if self.event_schema_version != GITHUB_EVENT_ARCHIVE_ROW_SCHEMA_VERSION:
            raise GitHubEventArchiveError("unsupported GitHub event-archive row version")
        if self.adapter_version != GITHUB_EVENT_ARCHIVE_ADAPTER_VERSION:
            raise GitHubEventArchiveError("unexpected GitHub event-archive adapter version")
        if self.exporter_version != GITHUB_EVENT_ARCHIVE_EXPORTER_VERSION:
            raise GitHubEventArchiveError("unexpected GitHub event-archive exporter version")
        if self.source != _SOURCE:
            raise GitHubEventArchiveError("unexpected GitHub event-archive source")
        if _REPOSITORY_RE.fullmatch(self.repository) is None:
            raise GitHubEventArchiveError("manifest repository must be OWNER/NAME")
        _integer(self.provider_repository_id, "manifest provider_repository_id")
        start = parse_timestamp(_timestamp(self.collection_start, "manifest collection_start"))
        end = parse_timestamp(_timestamp(self.collection_end, "manifest collection_end"))
        dataset_max = parse_timestamp(_timestamp(self.dataset_max_at, "manifest dataset_max_at"))
        collected = parse_timestamp(_timestamp(self.collected_at, "manifest collected_at"))
        if end <= start:
            raise GitHubEventArchiveError("manifest collection_end must follow collection_start")
        if dataset_max < end:
            raise GitHubEventArchiveError(
                "manifest dataset_max_at must cover the complete collection window"
            )
        if collected < end:
            raise GitHubEventArchiveError("manifest collected_at cannot predate collection_end")
        duration_seconds = int((end - start).total_seconds())
        if (
            start.minute
            or start.second
            or start.microsecond
            or end.minute
            or end.second
            or end.microsecond
            or duration_seconds % 3600
        ):
            raise GitHubEventArchiveError(
                "manifest collection window must use exact whole-hour boundaries"
            )
        expected_hours = duration_seconds // 3600
        if self.expected_hours != expected_hours:
            raise GitHubEventArchiveError(
                "manifest expected_hours does not match the collection window"
            )
        if not 0 <= self.observed_hours <= self.expected_hours:
            raise GitHubEventArchiveError(
                "manifest observed_hours must be between zero and expected_hours"
            )
        if len(self.missing_hours) > GITHUB_EVENT_ARCHIVE_MAX_MISSING_HOURS:
            raise GitHubEventArchiveError(
                "manifest exceeds the bounded missing-hour audit capacity"
            )
        if len(self.missing_hours) != self.expected_hours - self.observed_hours:
            raise GitHubEventArchiveError(
                "manifest missing_hours does not reconcile expected and observed hours"
            )
        normalized_missing: list[str] = []
        for index, value in enumerate(self.missing_hours):
            missing = parse_timestamp(_timestamp(value, f"manifest missing_hours[{index}]"))
            if (
                missing.minute
                or missing.second
                or missing.microsecond
                or not start <= missing < end
            ):
                raise GitHubEventArchiveError(
                    "manifest missing_hours must be unique whole hours inside the window"
                )
            normalized_missing.append(value)
        if tuple(sorted(set(normalized_missing))) != self.missing_hours:
            raise GitHubEventArchiveError(
                "manifest missing_hours must be unique and chronologically sorted"
            )
        for value, name in (
            (self.query_sha256, "manifest query_sha256"),
            (self.coverage_query_sha256, "manifest coverage_query_sha256"),
            (self.events_sha256, "manifest events_sha256"),
            (self.preregistration_sha256, "manifest preregistration_sha256"),
        ):
            _digest(value, name)
        if not isinstance(self.window_complete, bool):
            raise GitHubEventArchiveError("manifest window_complete must be a boolean")
        parsed_url = urlsplit(self.source_url)
        try:
            _ = parsed_url.port
        except ValueError as exc:
            raise GitHubEventArchiveError("manifest source_url contains an invalid port") from exc
        if (
            parsed_url.scheme != "https"
            or not parsed_url.netloc
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.path not in {"", "/"}
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise GitHubEventArchiveError(
                "manifest source_url must be an HTTPS origin without credentials or query"
            )

    def to_dict(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "event_schema_version": self.event_schema_version,
            "adapter_version": self.adapter_version,
            "exporter_version": self.exporter_version,
            "source": self.source,
            "source_url": self.source_url,
            "repository": self.repository,
            "provider_repository_id": self.provider_repository_id,
            "collection_start": self.collection_start,
            "collection_end": self.collection_end,
            "dataset_max_at": self.dataset_max_at,
            "collected_at": self.collected_at,
            "query_sha256": self.query_sha256,
            "coverage_query_sha256": self.coverage_query_sha256,
            "events_sha256": self.events_sha256,
            "preregistration_sha256": self.preregistration_sha256,
            "window_complete": self.window_complete,
            "expected_hours": self.expected_hours,
            "observed_hours": self.observed_hours,
            "missing_hours": list(self.missing_hours),
        }

    @classmethod
    def from_dict(cls, value: JsonObject) -> GitHubEventArchiveManifest:
        expected = frozenset(
            {
                "schema_version",
                "event_schema_version",
                "adapter_version",
                "exporter_version",
                "source",
                "source_url",
                "repository",
                "provider_repository_id",
                "collection_start",
                "collection_end",
                "dataset_max_at",
                "collected_at",
                "query_sha256",
                "coverage_query_sha256",
                "events_sha256",
                "preregistration_sha256",
                "window_complete",
                "expected_hours",
                "observed_hours",
                "missing_hours",
            }
        )
        _expect_exact_fields(value, expected, "GitHub event-archive manifest")
        raw_complete = value["window_complete"]
        if not isinstance(raw_complete, bool):
            raise GitHubEventArchiveError("manifest window_complete must be a boolean")
        raw_schema = value["schema_version"]
        raw_event_schema = value["event_schema_version"]
        if isinstance(raw_schema, bool) or not isinstance(raw_schema, int):
            raise GitHubEventArchiveError("manifest schema_version must be an integer")
        if isinstance(raw_event_schema, bool) or not isinstance(raw_event_schema, int):
            raise GitHubEventArchiveError("manifest event_schema_version must be an integer")
        raw_missing_hours = value["missing_hours"]
        if not isinstance(raw_missing_hours, list) or not all(
            isinstance(item, str) for item in raw_missing_hours
        ):
            raise GitHubEventArchiveError("manifest missing_hours must be an array of strings")
        return cls(
            schema_version=raw_schema,
            event_schema_version=raw_event_schema,
            adapter_version=_string(value["adapter_version"], "manifest adapter_version"),
            exporter_version=_string(value["exporter_version"], "manifest exporter_version"),
            source=_string(value["source"], "manifest source"),
            source_url=_string(value["source_url"], "manifest source_url"),
            repository=_string(value["repository"], "manifest repository"),
            provider_repository_id=_integer(
                value["provider_repository_id"], "manifest provider_repository_id"
            ),
            collection_start=_timestamp(value["collection_start"], "manifest collection_start"),
            collection_end=_timestamp(value["collection_end"], "manifest collection_end"),
            dataset_max_at=_timestamp(value["dataset_max_at"], "manifest dataset_max_at"),
            collected_at=_timestamp(value["collected_at"], "manifest collected_at"),
            query_sha256=_digest(value["query_sha256"], "manifest query_sha256"),
            coverage_query_sha256=_digest(
                value["coverage_query_sha256"], "manifest coverage_query_sha256"
            ),
            events_sha256=_digest(value["events_sha256"], "manifest events_sha256"),
            preregistration_sha256=_digest(
                value["preregistration_sha256"], "manifest preregistration_sha256"
            ),
            window_complete=raw_complete,
            expected_hours=_nonnegative_integer(value["expected_hours"], "manifest expected_hours"),
            observed_hours=_nonnegative_integer(value["observed_hours"], "manifest observed_hours"),
            missing_hours=tuple(cast(list[str], raw_missing_hours)),
        )


@dataclass(frozen=True, slots=True)
class _ArchiveRow:
    event_type: str
    repository: str
    occurred_at: str
    available_at: str
    action: str
    actor_key: str
    number: int
    base_sha: str
    head_sha: str
    review_state: str
    additions: int
    deletions: int
    changed_files: int
    statistics_complete: bool
    schema_version: int = GITHUB_EVENT_ARCHIVE_ROW_SCHEMA_VERSION

    @classmethod
    def from_dict(cls, value: JsonObject) -> _ArchiveRow:
        expected = frozenset(
            {
                "schema_version",
                "event_type",
                "repository",
                "occurred_at",
                "available_at",
                "action",
                "actor_key",
                "number",
                "base_sha",
                "head_sha",
                "review_state",
                "additions",
                "deletions",
                "changed_files",
                "statistics_complete",
            }
        )
        _expect_exact_fields(value, expected, "GitHub event-archive row")
        raw_schema = value["schema_version"]
        if (
            isinstance(raw_schema, bool)
            or not isinstance(raw_schema, int)
            or raw_schema != GITHUB_EVENT_ARCHIVE_ROW_SCHEMA_VERSION
        ):
            raise GitHubEventArchiveError("unsupported GitHub event-archive row version")
        event_type = _string(value["event_type"], "event_type", maximum_bytes=64)
        if event_type not in _EVENT_TYPES:
            raise GitHubEventArchiveError(f"unsupported event_type: {event_type!r}")
        action = _string(value["action"], "action", maximum_bytes=32).lower()
        review_state = _string(value["review_state"], "review_state", maximum_bytes=32).lower()
        if event_type == "PullRequestEvent":
            if action not in _PULL_ACTIONS or review_state != "none":
                raise GitHubEventArchiveError("invalid pull-request archive action/state")
        elif action not in _REVIEW_ACTIONS or review_state not in _REVIEW_STATES:
            raise GitHubEventArchiveError("invalid pull-request-review archive action/state")
        actor_key = _string(value["actor_key"], "actor_key", maximum_bytes=128)
        if _ACTOR_KEY_RE.fullmatch(actor_key) is None:
            raise GitHubEventArchiveError("actor_key must be a pseudonymized GitHub login key")
        occurred_at = _timestamp(value["occurred_at"], "occurred_at")
        available_at = _timestamp(value["available_at"], "available_at")
        if parse_timestamp(available_at) < parse_timestamp(occurred_at):
            raise GitHubEventArchiveError("available_at cannot predate occurred_at")
        statistics_complete = value["statistics_complete"]
        if not isinstance(statistics_complete, bool):
            raise GitHubEventArchiveError("statistics_complete must be a boolean")
        changed_files = _nonnegative_integer(value["changed_files"], "changed_files")
        if statistics_complete != (
            event_type == "PullRequestEvent" and action == "opened" and changed_files > 0
        ):
            raise GitHubEventArchiveError(
                "statistics_complete must identify an opening row with non-zero changed_files"
            )
        return cls(
            schema_version=raw_schema,
            event_type=event_type,
            repository=_string(value["repository"], "repository"),
            occurred_at=occurred_at,
            available_at=available_at,
            action=action,
            actor_key=actor_key,
            number=_integer(value["number"], "number"),
            base_sha=validate_git_sha(
                _string(value["base_sha"], "base_sha", maximum_bytes=64),
                field_name="GitHub event-archive base_sha",
            ),
            head_sha=validate_git_sha(
                _string(value["head_sha"], "head_sha", maximum_bytes=64),
                field_name="GitHub event-archive head_sha",
            ),
            review_state=review_state,
            additions=_nonnegative_integer(value["additions"], "additions"),
            deletions=_nonnegative_integer(value["deletions"], "deletions"),
            changed_files=changed_files,
            statistics_complete=statistics_complete,
        )

    def to_dict(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "repository": self.repository,
            "occurred_at": self.occurred_at,
            "available_at": self.available_at,
            "action": self.action,
            "actor_key": self.actor_key,
            "number": self.number,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "review_state": self.review_state,
            "additions": self.additions,
            "deletions": self.deletions,
            "changed_files": self.changed_files,
            "statistics_complete": self.statistics_complete,
        }


@dataclass(frozen=True, slots=True)
class GitHubEventArchiveReport:
    """Verified normalized records produced without mutating project state."""

    manifest: GitHubEventArchiveManifest
    manifest_sha256: str
    rows_read: int
    duplicate_rows: int
    pulls_opened: int
    pulls_merged: int
    reviews: int
    negative_outcomes: int
    opening_statistics_complete: int
    events: tuple[HistoricalEvent, ...]
    units: tuple[ChangeUnit, ...]

    def to_dict(self) -> JsonObject:
        return {
            "adapter_version": GITHUB_EVENT_ARCHIVE_ADAPTER_VERSION,
            "manifest_sha256": self.manifest_sha256,
            "repository": self.manifest.repository,
            "provider_repository_id": self.manifest.provider_repository_id,
            "collection_start": self.manifest.collection_start,
            "collection_end": self.manifest.collection_end,
            "window_complete": self.manifest.window_complete,
            "expected_source_hours": self.manifest.expected_hours,
            "observed_source_hours": self.manifest.observed_hours,
            "missing_source_hours": len(self.manifest.missing_hours),
            "rows_read": self.rows_read,
            "duplicate_rows": self.duplicate_rows,
            "pulls_opened": self.pulls_opened,
            "pulls_merged": self.pulls_merged,
            "reviews": self.reviews,
            "negative_outcomes": self.negative_outcomes,
            "opening_statistics_complete": self.opening_statistics_complete,
            "events": len(self.events),
            "units": len(self.units),
            "confirmatory_units": sum(unit.confirmatory for unit in self.units),
        }


def load_github_event_archive_manifest(path: Path) -> GitHubEventArchiveManifest:
    """Load one bounded strict manifest without following symlinks."""

    content = _read_regular_file(
        path,
        maximum=GITHUB_EVENT_ARCHIVE_MAX_MANIFEST_BYTES,
        name="GitHub event-archive manifest",
    )
    try:
        decoded = content.decode("utf-8")
        raw = strict_json_loads(decoded, str(path))
    except (UnicodeDecodeError, json.JSONDecodeError, ModelError) as exc:
        raise GitHubEventArchiveError(f"invalid GitHub event-archive manifest: {exc}") from exc
    if not isinstance(raw, dict):
        raise GitHubEventArchiveError("GitHub event-archive manifest must be an object")
    return GitHubEventArchiveManifest.from_dict(raw)


def _load_rows(path: Path, manifest: GitHubEventArchiveManifest) -> tuple[_ArchiveRow, ...]:
    content = _read_regular_file(
        path,
        maximum=HISTORY_JSONL_MAX_BYTES,
        name="GitHub event-archive JSONL",
    )
    actual_digest = hashlib.sha256(content).hexdigest()
    if actual_digest != manifest.events_sha256:
        raise GitHubEventArchiveError("GitHub event-archive JSONL hash does not match its manifest")
    try:
        decoded = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GitHubEventArchiveError("GitHub event-archive JSONL must be UTF-8") from exc
    lines = decoded.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if len(lines) > HISTORY_JSONL_MAX_RECORDS:
        raise GitHubEventArchiveError(
            f"GitHub event-archive JSONL exceeds {HISTORY_JSONL_MAX_RECORDS} records"
        )
    rows: list[_ArchiveRow] = []
    start = parse_timestamp(manifest.collection_start)
    end = parse_timestamp(manifest.collection_end)
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            raise GitHubEventArchiveError(f"blank archive row at {path}:{line_number}")
        if len(line.encode("utf-8")) > HISTORY_JSONL_MAX_LINE_BYTES:
            raise GitHubEventArchiveError(f"archive row is too large at {path}:{line_number}")
        try:
            raw = strict_json_loads(line, f"{path}:{line_number}")
        except (json.JSONDecodeError, ModelError) as exc:
            raise GitHubEventArchiveError(
                f"invalid archive JSON at {path}:{line_number}: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise GitHubEventArchiveError(f"archive row must be an object at {path}:{line_number}")
        row = _ArchiveRow.from_dict(raw)
        occurred = parse_timestamp(row.occurred_at)
        if not start <= occurred < end:
            raise GitHubEventArchiveError(
                f"archive row falls outside the manifest window at {path}:{line_number}"
            )
        if row.repository.casefold() != manifest.repository.casefold():
            raise GitHubEventArchiveError(
                f"archive row repository does not match the manifest at {path}:{line_number}"
            )
        rows.append(row)
    return tuple(rows)


def _provider_key(provider_repository_id: int) -> str:
    digest = hashlib.sha256(
        f"github\x00github.com.repo\x00{provider_repository_id}".encode()
    ).hexdigest()[:20]
    return f"github.github.com.repo.{digest}"


def _row_digest(row: _ArchiveRow) -> str:
    return content_hash(row.to_dict())


def _event_id(repository_key: str, kind: str, identity: str) -> str:
    identifier = f"event.{repository_key}.eventarchive.{kind}.{identity[:24]}"
    validate_subject(identifier)
    return identifier


def _change_id(repository_key: str, number: int) -> str:
    identifier = f"change.{repository_key}.pull.{number}"
    validate_subject(identifier)
    return identifier


def _source_ref(repository_key: str, number: int) -> str:
    return f"github-event-archive:{repository_key}:pull:{number}"


def _base_data(
    manifest: GitHubEventArchiveManifest,
    manifest_sha256: str,
) -> JsonObject:
    return {
        "adapter": GITHUB_EVENT_ARCHIVE_ADAPTER_VERSION,
        "archive_manifest_sha256": manifest_sha256,
        "archive_query_sha256": manifest.query_sha256,
        "archive_source": manifest.source,
        "provider_repository_id": manifest.provider_repository_id,
        "preregistration_sha256": manifest.preregistration_sha256,
        "point_in_time": True,
    }


def _outcome_interval_is_complete(
    manifest: GitHubEventArchiveManifest,
    opening: _ArchiveRow,
    merged: _ArchiveRow,
) -> bool:
    """Return whether every archive hour that could carry a later review exists."""

    opening_available = parse_timestamp(opening.available_at)
    merged_available = parse_timestamp(merged.available_at)
    for missing_hour in manifest.missing_hours:
        missing_available = parse_timestamp(missing_hour) + timedelta(hours=1)
        if opening_available < missing_available <= merged_available:
            return False
    return True


def normalize_github_event_archive(
    events_path: Path,
    manifest_path: Path,
    *,
    repository_id: str,
) -> GitHubEventArchiveReport:
    """Verify and normalize one complete repository-filtered event export."""

    validate_subject(repository_id)
    manifest = load_github_event_archive_manifest(manifest_path)
    rows = _load_rows(events_path, manifest)
    manifest_sha256 = content_hash(manifest.to_dict())
    repository_key = _provider_key(manifest.provider_repository_id)
    unique_rows: dict[str, _ArchiveRow] = {}
    for row in rows:
        digest = _row_digest(row)
        previous = unique_rows.get(digest)
        if previous is not None and previous != row:
            raise GitHubEventArchiveError("conflicting archive rows share one content identity")
        unique_rows[digest] = row

    grouped: dict[int, list[_ArchiveRow]] = defaultdict(list)
    for row in unique_rows.values():
        grouped[row.number].append(row)

    events: list[HistoricalEvent] = []
    pulls_opened = 0
    pulls_merged = 0
    review_count = 0
    negative_outcomes = 0
    opening_statistics_complete = 0
    for number, group in sorted(grouped.items()):
        ordered = sorted(
            group,
            key=lambda row: (
                parse_timestamp(row.occurred_at),
                parse_timestamp(row.available_at),
                _row_digest(row),
            ),
        )
        opened = [
            row
            for row in ordered
            if row.event_type == "PullRequestEvent" and row.action == "opened"
        ]
        if not opened:
            continue
        opening_shapes = {
            (
                row.occurred_at,
                row.available_at,
                row.actor_key,
                row.base_sha,
                row.head_sha,
                row.additions,
                row.deletions,
                row.changed_files,
                row.statistics_complete,
            )
            for row in opened
        }
        if len(opening_shapes) != 1:
            raise GitHubEventArchiveError(
                f"pull request {number} has conflicting point-in-time opening events"
            )
        opening = opened[0]
        author_key = opening.actor_key
        change_id = _change_id(repository_key, number)
        source_ref = _source_ref(repository_key, number)
        base_data = _base_data(manifest, manifest_sha256)
        snapshot_data = dict(base_data)
        snapshot_data.update(
            {
                "base_sha": opening.base_sha,
                "head_sha": opening.head_sha,
                "commits": [opening.head_sha],
                "actor_key": author_key,
                "author_key": author_key,
                "archive_action": "opened",
                "provider_occurred_at": opening.occurred_at,
                "diff_statistics": {
                    "additions": opening.additions,
                    "deletions": opening.deletions,
                    "files_changed": opening.changed_files,
                    "complete": opening.statistics_complete,
                    "source": "github_event_archive_opened_event",
                },
            }
        )
        snapshot = HistoricalEvent(
            id=_event_id(repository_key, "snapshot", _row_digest(opening)),
            repository_id=repository_id,
            kind="change_snapshot",
            occurred_at=opening.available_at,
            available_at=opening.available_at,
            provider="github",
            source_ref=source_ref,
            change_id=change_id,
            independent_group=change_id,
            data=snapshot_data,
        )
        events.append(snapshot)
        pulls_opened += 1
        opening_statistics_complete += int(opening.statistics_complete)

        merged_rows = [
            row
            for row in ordered
            if row.event_type == "PullRequestEvent" and row.action == "merged"
        ]
        if (
            len(
                {
                    (
                        row.occurred_at,
                        row.available_at,
                        row.actor_key,
                        row.base_sha,
                        row.head_sha,
                    )
                    for row in merged_rows
                }
            )
            > 1
        ):
            raise GitHubEventArchiveError(
                f"pull request {number} has conflicting merge finalization events"
            )
        merged = merged_rows[0] if merged_rows else None
        if merged is not None and parse_timestamp(merged.available_at) < parse_timestamp(
            opening.available_at
        ):
            raise GitHubEventArchiveError(f"pull request {number} merged before it opened")

        eligible_reviews: list[_ArchiveRow] = []
        for review in ordered:
            if review.event_type != "PullRequestReviewEvent":
                continue
            occurred = parse_timestamp(review.available_at)
            if occurred <= parse_timestamp(opening.available_at):
                continue
            if merged is not None and occurred > parse_timestamp(merged.available_at):
                continue
            eligible_reviews.append(review)
            review_data = dict(base_data)
            review_data.update(
                {
                    "decision": review.review_state,
                    "category": "unspecified",
                    "independent": review.actor_key != author_key,
                    "reviewer_key": review.actor_key,
                    "author_key": author_key,
                    "archive_action": review.action,
                    "commit_sha": review.head_sha,
                    "provider_occurred_at": review.occurred_at,
                }
            )
            events.append(
                HistoricalEvent(
                    id=_event_id(repository_key, "review", _row_digest(review)),
                    repository_id=repository_id,
                    kind="review",
                    occurred_at=review.available_at,
                    available_at=review.available_at,
                    provider="github",
                    source_ref=source_ref,
                    change_id=change_id,
                    independent_group=review.actor_key,
                    data=review_data,
                )
            )
            review_count += 1

        if merged is None:
            continue
        final_data = dict(base_data)
        final_data.update(
            {
                "base_sha": merged.base_sha,
                "head_sha": merged.head_sha,
                "final_sha": merged.head_sha,
                "commits": [merged.head_sha],
                "actor_key": merged.actor_key,
                "author_key": author_key,
                "archive_action": "merged",
                "provider_occurred_at": merged.occurred_at,
            }
        )
        final = HistoricalEvent(
            id=_event_id(repository_key, "final", _row_digest(merged)),
            repository_id=repository_id,
            kind="change_merged",
            occurred_at=merged.available_at,
            available_at=merged.available_at,
            provider="github",
            source_ref=source_ref,
            change_id=change_id,
            independent_group=change_id,
            data=final_data,
        )
        events.append(final)
        pulls_merged += 1

        independent_states = {
            review.review_state for review in eligible_reviews if review.actor_key != author_key
        }
        if (
            manifest.window_complete
            and _outcome_interval_is_complete(manifest, opening, merged)
            and "approved" in independent_states
            and "changes_requested" not in independent_states
        ):
            outcome_data = dict(base_data)
            outcome_data.update(
                {
                    "target": INDEPENDENT_REVIEW_CHANGES_REQUESTED,
                    "value": "negative",
                    "evidence_complete": True,
                    "strength": "strong",
                    "confidence": 1.0,
                    "reason": (
                        "complete point-in-time review window ended at merge after an "
                        "independent approval and no independent changes-requested review"
                    ),
                }
            )
            events.append(
                HistoricalEvent(
                    id=_event_id(
                        repository_key,
                        "outcome",
                        content_hash(
                            {
                                "change_id": change_id,
                                "target": INDEPENDENT_REVIEW_CHANGES_REQUESTED,
                                "value": "negative",
                            }
                        ),
                    ),
                    repository_id=repository_id,
                    kind="change_finalized",
                    occurred_at=merged.available_at,
                    available_at=merged.available_at,
                    provider="github",
                    source_ref=source_ref,
                    change_id=change_id,
                    independent_group=f"archive.{manifest_sha256[:20]}",
                    data=outcome_data,
                )
            )
            negative_outcomes += 1

    normalized_events = tuple(
        sorted(
            events,
            key=lambda event: (
                parse_timestamp(event.occurred_at),
                parse_timestamp(event.available_at),
                event.id,
            ),
        )
    )
    units = assemble_change_units(normalized_events)
    validate_history_snapshot(normalized_events, units)
    return GitHubEventArchiveReport(
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        rows_read=len(rows),
        duplicate_rows=len(rows) - len(unique_rows),
        pulls_opened=pulls_opened,
        pulls_merged=pulls_merged,
        reviews=review_count,
        negative_outcomes=negative_outcomes,
        opening_statistics_complete=opening_statistics_complete,
        events=normalized_events,
        units=units,
    )


def _utc_literal(timestamp: str, name: str) -> str:
    parsed = parse_timestamp(timestamp).astimezone(UTC)
    if parsed.microsecond:
        raise GitHubEventArchiveError(f"{name} must use whole-second precision")
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def build_clickhouse_gharchive_query(repository: str, since: str, until: str) -> str:
    """Build the frozen, prose-free ClickHouse projection used by the exporter."""

    if _REPOSITORY_RE.fullmatch(repository) is None:
        raise GitHubEventArchiveError("repository must be a safe OWNER/NAME")
    start = parse_timestamp(since)
    end = parse_timestamp(until)
    if end <= start:
        raise GitHubEventArchiveError("until must follow since")
    start_literal = _utc_literal(since, "since")
    end_literal = _utc_literal(until, "until")
    return f"""SELECT DISTINCT
    {GITHUB_EVENT_ARCHIVE_ROW_SCHEMA_VERSION} AS schema_version,
    toString(event_type) AS event_type,
    '{repository}' AS repository,
    formatDateTime(created_at, '%FT%TZ', 'UTC') AS occurred_at,
    formatDateTime(
        greatest(created_at, toStartOfHour(file_time) + INTERVAL 1 HOUR),
        '%FT%TZ',
        'UTC'
    ) AS available_at,
    if(
        event_type = 'PullRequestEvent'
            AND (action = 'merged' OR merged_at > toDateTime('1971-01-01 00:00:00', 'UTC')),
        'merged',
        toString(action)
    ) AS action,
    concat('github.login.', lower(hex(SHA256(actor_login)))) AS actor_key,
    number,
    base_sha,
    head_sha,
    toString(review_state) AS review_state,
    toUInt64(additions) AS additions,
    toUInt64(deletions) AS deletions,
    toUInt64(changed_files) AS changed_files,
    toBool(event_type = 'PullRequestEvent' AND action = 'opened' AND changed_files > 0)
        AS statistics_complete
FROM github_events
PREWHERE event_type IN ('PullRequestEvent', 'PullRequestReviewEvent')
    AND repo_name = '{repository}'
WHERE created_at >= toDateTime('{start_literal}', 'UTC')
    AND created_at < toDateTime('{end_literal}', 'UTC')
    AND actor_login != ''
    AND number > 0
    AND base_sha != ''
    AND head_sha != ''
    AND (
        (event_type = 'PullRequestEvent' AND (
            action IN ('opened', 'merged')
            OR (action = 'closed'
                AND merged_at > toDateTime('1971-01-01 00:00:00', 'UTC'))
        ))
        OR
        (event_type = 'PullRequestReviewEvent' AND action = 'created'
            AND review_state IN ('approved', 'changes_requested'))
    )
ORDER BY occurred_at, event_type, number, actor_key, review_state
FORMAT JSONEachRow
"""


def build_clickhouse_file_hours_query(since: str, until: str) -> str:
    """Build the source-wide hourly continuity audit for one collection window."""

    start = parse_timestamp(since)
    end = parse_timestamp(until)
    if end <= start:
        raise GitHubEventArchiveError("until must follow since")
    start_literal = _utc_literal(since, "since")
    end_literal = _utc_literal(until, "until")
    return f"""SELECT DISTINCT
    formatDateTime(toStartOfHour(file_time), '%FT%TZ', 'UTC') AS file_hour
FROM github_events
WHERE file_time >= toDateTime('{start_literal}', 'UTC')
    AND file_time < toDateTime('{end_literal}', 'UTC')
ORDER BY file_hour
FORMAT TSVRaw
"""


def clickhouse_dataset_max_query() -> str:
    """Return the small projection-backed freshness query used by the exporter."""

    return (
        "SELECT formatDateTime(max(created_at), '%FT%TZ', 'UTC') AS dataset_max_at "
        "FROM github_events FORMAT TSVRaw\n"
    )


def utc_now() -> str:
    """Return an aware timestamp for export manifests."""

    return datetime.now(tz=UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = [
    "GITHUB_EVENT_ARCHIVE_ADAPTER_VERSION",
    "GITHUB_EVENT_ARCHIVE_EXPORTER_VERSION",
    "GITHUB_EVENT_ARCHIVE_MANIFEST_SCHEMA_VERSION",
    "GITHUB_EVENT_ARCHIVE_MAX_MISSING_HOURS",
    "GITHUB_EVENT_ARCHIVE_ROW_SCHEMA_VERSION",
    "GitHubEventArchiveError",
    "GitHubEventArchiveManifest",
    "GitHubEventArchiveReport",
    "build_clickhouse_file_hours_query",
    "build_clickhouse_gharchive_query",
    "clickhouse_dataset_max_query",
    "load_github_event_archive_manifest",
    "normalize_github_event_archive",
    "utc_now",
]
