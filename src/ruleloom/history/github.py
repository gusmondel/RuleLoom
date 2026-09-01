"""Conservative GitHub history normalization through an injectable ``gh api`` client.

The adapter deliberately keeps provider collection separate from outcome
derivation.  Archived pull-request state cannot prove the exact patch that was
visible when a pull request opened, especially after a force push.  Therefore
the change units produced here are always exploratory ``git_only`` units and
never confirmatory.

Free-form titles, bodies, review text, user names, check names, and mutable label
names are not persisted.  Provider identifiers are either numeric or
pseudonymized before they enter normalized history records.  Timeline label
names are current mutable objects, not point-in-time evidence, so the archive
adapter never derives outcomes from them.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import BinaryIO, Protocol
from urllib.parse import urlsplit

from ruleloom.history.models import ChangeUnit, HistoricalEvent, validate_git_sha
from ruleloom.models import (
    JsonObject,
    JsonValue,
    ModelError,
    content_hash,
    parse_timestamp,
    strict_json_loads,
    validate_subject,
)

GITHUB_ADAPTER_VERSION = "ruleloom-github/1"

_GH_TIMEOUT_SECONDS = 45.0
_GH_STDOUT_LIMIT = 16 * 1024 * 1024
_GH_STDERR_LIMIT = 1024 * 1024
_MAX_ENDPOINT_BYTES = 4096
_MAX_PARAMETERS = 32
_MAX_PARAMETER_BYTES = 4096
_MAX_PULL_REQUESTS = 10_000
_MAX_COMMITS_PER_PULL = 5_000
_MAX_REVIEWS_PER_PULL = 5_000
_MAX_CHECKS_PER_COMMIT = 5_000
_MAX_REPOSITORY_COMMITS = 100_000
_MAX_API_REQUESTS = 100_000
_MAX_PROVIDER_RECORDS = 2_000_000
_DEFAULT_MAX_API_REQUESTS = 20_000
_DEFAULT_MAX_PROVIDER_RECORDS = 250_000
_PAGE_SIZE = 100
_REPOSITORY_BINDINGS = frozenset(
    {"caller_asserted", "explicit_unverified_override", "verified_origin"}
)

_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}$")
_GITHUB_ORIGIN_HOSTS = frozenset({"github.com", "ssh.github.com", "www.github.com"})
_SCP_ORIGIN_RE = re.compile(
    r"^(?:[^@\s/:]+@)?(?P<host>[^\s/:]+):(?P<path>[^?#]+)$",
    re.IGNORECASE,
)
_REVERT_TRAILER_RE = re.compile(r"(?im)^this reverts commit ([0-9a-f]{40}|[0-9a-f]{64})\.?\s*$")
_ARCHIVE_LABEL_OUTCOME_POLICY = "ignored_mutable_timeline_label_names"


class GitHubHistoryError(RuntimeError):
    """Raised when GitHub history cannot be fetched or normalized safely."""


class GitHubApi(Protocol):
    """Minimal interface used by the normalizer and easily replaced in tests."""

    def get(self, endpoint: str, *, params: Mapping[str, str] | None = None) -> JsonValue:
        """Return one decoded response from a GitHub REST endpoint."""


GhRunner = Callable[[tuple[str, ...], float, int, int], tuple[bytes, bytes, int]]


def _run_bounded(
    command: tuple[str, ...],
    timeout_seconds: float,
    stdout_limit: int,
    stderr_limit: int,
) -> tuple[bytes, bytes, int]:
    """Run one command without a shell while bounding time and captured output."""
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise GitHubHistoryError(
            "GitHub CLI (gh) is not installed or is not available on PATH"
        ) from exc
    except OSError as exc:
        raise GitHubHistoryError(f"cannot start GitHub CLI: {exc}") from exc

    assert process.stdout is not None
    assert process.stderr is not None
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    violation = threading.Event()
    violation_messages: list[str] = []

    def drain(name: str, stream: BinaryIO, limit: int) -> None:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            if len(buffers[name]) + len(chunk) > limit:
                violation_messages.append(f"GitHub CLI {name} exceeds {limit} bytes")
                violation.set()
                return
            buffers[name].extend(chunk)

    readers = (
        threading.Thread(
            target=drain,
            args=("stdout", process.stdout, stdout_limit),
            daemon=True,
        ),
        threading.Thread(
            target=drain,
            args=("stderr", process.stderr, stderr_limit),
            daemon=True,
        ),
    )
    for reader in readers:
        reader.start()

    deadline = time.monotonic() + timeout_seconds
    try:
        while process.poll() is None:
            if violation.is_set():
                process.kill()
                process.wait()
                raise GitHubHistoryError(violation_messages[0])
            if time.monotonic() >= deadline:
                process.kill()
                process.wait()
                raise GitHubHistoryError(f"GitHub CLI exceeded {timeout_seconds:g} seconds")
            time.sleep(0.01)
        returncode = process.wait()
        for reader in readers:
            reader.join(timeout=1)
        if any(reader.is_alive() for reader in readers):
            process.kill()
            process.wait()
            raise GitHubHistoryError("GitHub CLI output readers did not terminate")
        if violation.is_set():
            raise GitHubHistoryError(violation_messages[0])
    finally:
        process.stdout.close()
        process.stderr.close()
    return bytes(buffers["stdout"]), bytes(buffers["stderr"]), returncode


@dataclass(frozen=True, slots=True)
class GhApiClient:
    """Bounded ``gh api`` transport with an injectable process runner."""

    executable: str = "gh"
    hostname: str = "github.com"
    timeout_seconds: float = _GH_TIMEOUT_SECONDS
    stdout_limit: int = _GH_STDOUT_LIMIT
    stderr_limit: int = _GH_STDERR_LIMIT
    runner: GhRunner = _run_bounded

    def __post_init__(self) -> None:
        if not self.executable or any(character in self.executable for character in "\x00\r\n"):
            raise GitHubHistoryError("GitHub CLI executable must be a safe non-empty argument")
        if self.hostname.casefold() != "github.com":
            raise GitHubHistoryError("GitHub adapter v1 supports only the explicit github.com host")
        if self.timeout_seconds <= 0:
            raise GitHubHistoryError("GitHub CLI timeout must be positive")
        if self.stdout_limit <= 0 or self.stderr_limit <= 0:
            raise GitHubHistoryError("GitHub CLI output limits must be positive")

    def get(self, endpoint: str, *, params: Mapping[str, str] | None = None) -> JsonValue:
        safe_endpoint = _validate_endpoint(endpoint)
        safe_parameters = _validate_parameters(params or {})
        command = [
            self.executable,
            "api",
            "--hostname",
            self.hostname,
            "--method",
            "GET",
            "--header",
            "Accept: application/vnd.github+json",
            "--header",
            "X-GitHub-Api-Version: 2022-11-28",
            safe_endpoint,
        ]
        for key, value in sorted(safe_parameters.items()):
            command.extend(("--raw-field", f"{key}={value}"))
        stdout, stderr, returncode = self.runner(
            tuple(command),
            self.timeout_seconds,
            self.stdout_limit,
            self.stderr_limit,
        )
        if len(stdout) > self.stdout_limit or len(stderr) > self.stderr_limit:
            raise GitHubHistoryError("GitHub CLI runner violated configured output limits")
        if returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            if len(detail) > 1000:
                detail = detail[:999] + "…"
            raise GitHubHistoryError(
                f"GitHub API request failed for {safe_endpoint}: {detail or 'unknown error'}"
            )
        try:
            content = stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GitHubHistoryError("GitHub CLI returned non-UTF-8 JSON") from exc
        try:
            return strict_json_loads(content, f"GitHub API {safe_endpoint}")
        except (json.JSONDecodeError, ModelError) as exc:
            raise GitHubHistoryError(
                f"GitHub API returned invalid JSON for {safe_endpoint}: {exc}"
            ) from exc


@dataclass(frozen=True, slots=True)
class GitHubHistoryReport:
    """Auditable result of one bounded GitHub archive collection."""

    events: tuple[HistoricalEvent, ...]
    units: tuple[ChangeUnit, ...]
    pull_requests_examined: int
    pull_requests_normalized: int
    pull_requests_skipped: int
    warnings: tuple[str, ...]
    truncated: bool
    provider_repository_key: str
    collected_at: str
    since: str | None
    until: str
    repository_id: str
    manifest_hash: str = ""
    provider_host: str = "github.com"
    repository_binding: str = "caller_asserted"
    adapter_version: str = GITHUB_ADAPTER_VERSION
    api_requests_used: int = 0
    provider_records_used: int = 0
    max_api_requests: int = _DEFAULT_MAX_API_REQUESTS
    max_provider_records: int = _DEFAULT_MAX_PROVIDER_RECORDS
    max_pull_requests: int = 1_000
    max_commits_per_pull: int = 1_000
    max_reviews_per_pull: int = 1_000
    max_checks_per_commit: int = 1_000
    max_repository_commits: int = 10_000
    budget_policy: str = "fail_closed"

    def __post_init__(self) -> None:
        expected = content_hash(self.manifest_payload())
        if self.manifest_hash and self.manifest_hash != expected:
            raise GitHubHistoryError("GitHub history report manifest_hash is inconsistent")
        if not self.manifest_hash:
            object.__setattr__(self, "manifest_hash", expected)

    def manifest_payload(self) -> JsonObject:
        """Return the compact, fully emitted payload bound by ``manifest_hash``."""

        return {
            "schema_version": 1,
            "adapter_version": self.adapter_version,
            "repository_id": self.repository_id,
            "provider_repository_key": self.provider_repository_key,
            "provider_host": self.provider_host,
            "repository_binding": self.repository_binding,
            "collected_at": self.collected_at,
            "since": self.since,
            "until": self.until,
            "cutoff_semantics": "append_only_collection_filter_no_rewind",
            "archive_label_outcome_policy": _ARCHIVE_LABEL_OUTCOME_POLICY,
            "limits": {
                "pull_requests": self.max_pull_requests,
                "commits_per_pull": self.max_commits_per_pull,
                "reviews_per_pull": self.max_reviews_per_pull,
                "checks_per_commit": self.max_checks_per_commit,
                "repository_commits": self.max_repository_commits,
                "api_requests": self.max_api_requests,
                "provider_records": self.max_provider_records,
            },
            "collection_budget": {
                "policy": self.budget_policy,
                "api_requests_used": self.api_requests_used,
                "provider_records_used": self.provider_records_used,
            },
            "truncated": self.truncated,
            "pull_requests_examined": self.pull_requests_examined,
            "pull_requests_normalized": self.pull_requests_normalized,
            "pull_requests_skipped": self.pull_requests_skipped,
            "warnings": list(self.warnings),
            "normalized_content": {
                "events": {
                    "count": len(self.events),
                    "content_hash": content_hash([event.to_dict() for event in self.events]),
                },
                "units": {
                    "count": len(self.units),
                    "content_hash": content_hash([unit.to_dict() for unit in self.units]),
                },
            },
        }

    def to_dict(self) -> JsonObject:
        manifest = self.manifest_payload()
        if content_hash(manifest) != self.manifest_hash:
            raise GitHubHistoryError("GitHub history report changed after manifest creation")
        evidence_grades: JsonObject = {}
        for event in self.events:
            grade = event.data.get("evidence_grade")
            if isinstance(grade, str):
                previous = evidence_grades.get(grade)
                evidence_grades[grade] = previous + 1 if isinstance(previous, int) else 1
        return {
            "adapter_version": self.adapter_version,
            "events": len(self.events),
            "units": len(self.units),
            "pull_requests_examined": self.pull_requests_examined,
            "pull_requests_normalized": self.pull_requests_normalized,
            "pull_requests_skipped": self.pull_requests_skipped,
            "warnings": list(self.warnings),
            "truncated": self.truncated,
            "provider_repository_key": self.provider_repository_key,
            "provider_host": self.provider_host,
            "collected_at": self.collected_at,
            "since": self.since,
            "until": self.until,
            "cutoff_semantics": "append_only_collection_filter_no_rewind",
            "archive_label_outcome_policy": _ARCHIVE_LABEL_OUTCOME_POLICY,
            "manifest_hash": self.manifest_hash,
            "manifest": manifest,
            "repository_binding": self.repository_binding,
            "collection_limits": {
                "pull_requests": self.max_pull_requests,
                "commits_per_pull": self.max_commits_per_pull,
                "reviews_per_pull": self.max_reviews_per_pull,
                "checks_per_commit": self.max_checks_per_commit,
                "repository_commits": self.max_repository_commits,
                "api_requests": self.max_api_requests,
                "provider_records": self.max_provider_records,
            },
            "collection_budget": {
                "policy": self.budget_policy,
                "api_requests": {
                    "used": self.api_requests_used,
                    "maximum": self.max_api_requests,
                },
                "provider_records": {
                    "used": self.provider_records_used,
                    "maximum": self.max_provider_records,
                },
            },
            "evidence_grade": "exploratory_git_only",
            "outcome_evidence_grades": evidence_grades,
        }


def _provider_record_count(value: JsonValue) -> int:
    """Count top-level provider entities without walking attacker-controlled JSON.

    Paginated REST responses are arrays, except check-run pages, which wrap the
    records in ``check_runs``.  Other objects (repository and pull details) are
    one provider record.  Scalars are invalid for every endpoint used by this
    adapter, but counting them as one keeps the transport budget fail-closed.
    """
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        check_runs = value.get("check_runs")
        if isinstance(check_runs, list):
            return len(check_runs)
    return 1


@dataclass(slots=True)
class _CollectionBudget:
    """Global deterministic budget shared by every request in one collection."""

    maximum_requests: int
    maximum_records: int
    requests_used: int = 0
    records_used: int = 0


@dataclass(frozen=True, slots=True)
class _BudgetedGitHubApi:
    """Fail closed before a response can exceed the global collection budget."""

    client: GitHubApi
    budget: _CollectionBudget

    def get(self, endpoint: str, *, params: Mapping[str, str] | None = None) -> JsonValue:
        if self.budget.requests_used >= self.budget.maximum_requests:
            raise GitHubHistoryError(
                "global GitHub API request budget exhausted "
                f"(used={self.budget.requests_used}, "
                f"maximum={self.budget.maximum_requests}); "
                "collection aborted without persistence"
            )
        self.budget.requests_used += 1
        value = self.client.get(endpoint, params=params)
        response_records = _provider_record_count(value)
        if self.budget.records_used + response_records > self.budget.maximum_records:
            raise GitHubHistoryError(
                "global GitHub provider-record budget exhausted "
                f"(used={self.budget.records_used}, response={response_records}, "
                f"maximum={self.budget.maximum_records}); "
                "collection aborted without persistence"
            )
        self.budget.records_used += response_records
        return value


@dataclass(frozen=True, slots=True)
class _PullRequestContext:
    number: int
    change_id: str
    source_ref: str
    author_id: int
    created_at: str
    finalized_at: str
    base_sha: str
    prediction_sha: str
    final_sha: str
    merge_sha: str | None
    commits: tuple[str, ...]
    prediction_event_id: str
    final_event_id: str


def _validate_endpoint(endpoint: str) -> str:
    if not isinstance(endpoint, str) or not endpoint:
        raise GitHubHistoryError("GitHub API endpoint must be a non-empty string")
    if endpoint.startswith("-") or any(character in endpoint for character in "\x00\r\n"):
        raise GitHubHistoryError("GitHub API endpoint contains unsafe characters")
    if len(endpoint.encode("utf-8")) > _MAX_ENDPOINT_BYTES:
        raise GitHubHistoryError(f"GitHub API endpoint exceeds {_MAX_ENDPOINT_BYTES} bytes")
    if not endpoint.startswith("repos/"):
        raise GitHubHistoryError("GitHub API endpoint must remain below repos/")
    if any(part in {".", ".."} for part in endpoint.split("/")):
        raise GitHubHistoryError("GitHub API endpoint cannot contain dot path segments")
    return endpoint


def _validate_parameters(parameters: Mapping[str, str]) -> dict[str, str]:
    if len(parameters) > _MAX_PARAMETERS:
        raise GitHubHistoryError(f"GitHub API request exceeds {_MAX_PARAMETERS} parameters")
    result: dict[str, str] = {}
    for key, value in parameters.items():
        if (
            not isinstance(key, str)
            or not isinstance(value, str)
            or not key
            or key.startswith("-")
            or any(character in key + value for character in "\x00\r\n")
        ):
            raise GitHubHistoryError("GitHub API parameter contains unsafe characters")
        if len(key.encode("utf-8")) + len(value.encode("utf-8")) > _MAX_PARAMETER_BYTES:
            raise GitHubHistoryError(f"GitHub API parameter exceeds {_MAX_PARAMETER_BYTES} bytes")
        result[key] = value
    return result


def _positive_limit(value: int, *, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise GitHubHistoryError(f"{name} must be between 1 and {maximum}")
    return value


def _normalize_timestamp(value: str | datetime | None, *, name: str) -> str | None:
    if value is None:
        return None
    try:
        parsed = value if isinstance(value, datetime) else parse_timestamp(value)
    except (ModelError, ValueError) as exc:
        raise GitHubHistoryError(f"{name} must be an aware ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise GitHubHistoryError(f"{name} must include a timezone")
    normalized = parsed.astimezone(UTC)
    timespec = "microseconds" if normalized.microsecond else "seconds"
    return normalized.isoformat(timespec=timespec).replace("+00:00", "Z")


def _at_or_before(timestamp: str, cutoff: str) -> bool:
    """Return whether a normalized provider timestamp is inside the archive cutoff."""
    return parse_timestamp(timestamp) <= parse_timestamp(cutoff)


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _object(value: JsonValue, name: str) -> JsonObject:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise GitHubHistoryError(f"GitHub {name} must be an object")
    return value


def _array(value: JsonValue, name: str) -> list[JsonValue]:
    if not isinstance(value, list):
        raise GitHubHistoryError(f"GitHub {name} must be an array")
    return value


def _string(value: JsonValue, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise GitHubHistoryError(f"GitHub {name} must be a non-empty string")
    return value


def _optional_string(value: JsonValue, name: str) -> str | None:
    if value is None:
        return None
    return _string(value, name)


def _integer(value: JsonValue, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GitHubHistoryError(f"GitHub {name} must be a non-negative integer")
    return value


def _provider_key(namespace: str, value: str | int) -> str:
    digest = hashlib.sha256(f"github\x00{namespace}\x00{value}".encode()).hexdigest()[:20]
    return f"github.{namespace}.{digest}"


def _repository_endpoint(repository: str, suffix: str = "") -> str:
    return f"repos/{repository}{suffix}"


def github_repository_from_origin(remote: str | None) -> str | None:
    """Extract ``OWNER/NAME`` only from an unambiguous public-GitHub origin."""

    if remote is None or not remote or any(character in remote for character in "\x00\r\n"):
        return None
    host: str | None
    path: str
    scp_match = _SCP_ORIGIN_RE.fullmatch(remote)
    if scp_match is not None and "://" not in remote:
        host = scp_match.group("host").lower()
        path = scp_match.group("path")
    else:
        parsed = urlsplit(remote)
        if parsed.scheme.casefold() not in {"https", "ssh"}:
            return None
        host = parsed.hostname.lower() if parsed.hostname is not None else None
        path = parsed.path
        if parsed.query or parsed.fragment:
            return None
    if host not in _GITHUB_ORIGIN_HOSTS:
        return None
    normalized = path.strip("/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    if not _REPOSITORY_RE.fullmatch(normalized):
        return None
    return normalized


def _page(
    client: GitHubApi,
    endpoint: str,
    *,
    page: int,
    params: Mapping[str, str] | None = None,
) -> list[JsonValue]:
    query = dict(params or {})
    query.update({"page": str(page), "per_page": str(_PAGE_SIZE)})
    return _array(client.get(endpoint, params=query), endpoint)


def _collect_pages(
    client: GitHubApi,
    endpoint: str,
    *,
    maximum: int,
    params: Mapping[str, str] | None = None,
) -> tuple[list[JsonValue], bool]:
    records: list[JsonValue] = []
    page_number = 1
    while True:
        page = _page(client, endpoint, page=page_number, params=params)
        remaining = maximum - len(records)
        if len(page) > remaining:
            records.extend(page[:remaining])
            return records, True
        records.extend(page)
        if len(page) < _PAGE_SIZE:
            return records, False
        if len(records) == maximum:
            probe = _page(client, endpoint, page=page_number + 1, params=params)
            return records, bool(probe)
        page_number += 1


def _event_id(repository_key: str, kind: str, value: str | int) -> str:
    safe_value = str(value).lower()
    if not re.fullmatch(r"[a-z0-9.-]+", safe_value):
        safe_value = hashlib.sha256(safe_value.encode()).hexdigest()
    return f"event.{repository_key}.{kind}.{safe_value}"


def _pull_source_ref(repository_key: str, number: int) -> str:
    return f"github:{repository_key}:pull:{number}"


def _commit_values(raw_commits: Sequence[JsonValue]) -> tuple[tuple[str, ...], str, str]:
    commits: list[str] = []
    initial_parent: str | None = None
    for index, raw_commit in enumerate(raw_commits):
        commit = _object(raw_commit, f"pull commit[{index}]")
        sha = validate_git_sha(_string(commit.get("sha"), f"pull commit[{index}].sha"))
        parents = _array(commit.get("parents"), f"pull commit[{index}].parents")
        if index == 0:
            if not parents:
                raise GitHubHistoryError("first pull-request commit has no parent")
            first_parent = _object(parents[0], "first pull-request commit parent")
            initial_parent = validate_git_sha(
                _string(first_parent.get("sha"), "first pull-request commit parent.sha")
            )
        commits.append(sha)
    if not commits or initial_parent is None:
        raise GitHubHistoryError("pull request has no usable commits")
    if len(commits) != len(set(commits)):
        raise GitHubHistoryError("pull request returned duplicate commit SHAs")
    return tuple(commits), initial_parent, commits[0]


def _force_push_present(raw_timeline: Sequence[JsonValue]) -> bool:
    for index, raw_event in enumerate(raw_timeline):
        event = _object(raw_event, f"pull timeline[{index}]")
        if event.get("event") == "head_ref_force_pushed":
            return True
    return False


def _normalize_pull_context(
    detail: JsonObject,
    raw_commits: Sequence[JsonValue],
    raw_timeline: Sequence[JsonValue],
    *,
    repository_key: str,
) -> _PullRequestContext | None:
    number = _integer(detail.get("number"), "pull request number")
    state = _string(detail.get("state"), f"pull request {number}.state").lower()
    if state != "closed":
        return None
    created_at = _normalize_timestamp(
        _string(detail.get("created_at"), f"pull request {number}.created_at"),
        name=f"pull request {number}.created_at",
    )
    assert created_at is not None
    merged_at = _normalize_timestamp(
        _optional_string(detail.get("merged_at"), f"pull request {number}.merged_at"),
        name=f"pull request {number}.merged_at",
    )
    closed_at = _normalize_timestamp(
        _optional_string(detail.get("closed_at"), f"pull request {number}.closed_at"),
        name=f"pull request {number}.closed_at",
    )
    finalized_at = merged_at or closed_at
    if finalized_at is None:
        raise GitHubHistoryError(f"closed pull request {number} lacks a final timestamp")
    if parse_timestamp(finalized_at) < parse_timestamp(created_at):
        raise GitHubHistoryError(f"pull request {number} closed before it was created")
    if _force_push_present(raw_timeline):
        return None
    commits, base_sha, prediction_sha = _commit_values(raw_commits)
    head = _object(detail.get("head"), f"pull request {number}.head")
    final_sha = validate_git_sha(_string(head.get("sha"), f"pull request {number}.head.sha"))
    if final_sha != commits[-1]:
        raise GitHubHistoryError(
            f"pull request {number} head does not match its final listed commit"
        )
    author = _object(detail.get("user"), f"pull request {number}.user")
    author_id = _integer(author.get("id"), f"pull request {number}.user.id")
    raw_merge_sha = detail.get("merge_commit_sha")
    merge_sha: str | None = None
    if merged_at is not None and isinstance(raw_merge_sha, str) and raw_merge_sha:
        merge_sha = validate_git_sha(raw_merge_sha, field_name="pull request merge_commit_sha")
    change_id = f"change.{repository_key}.pull.{number}"
    source_ref = _pull_source_ref(repository_key, number)
    return _PullRequestContext(
        number=number,
        change_id=change_id,
        source_ref=source_ref,
        author_id=author_id,
        created_at=created_at,
        finalized_at=finalized_at,
        base_sha=base_sha,
        prediction_sha=prediction_sha,
        final_sha=final_sha,
        merge_sha=merge_sha,
        commits=commits,
        prediction_event_id=_event_id(repository_key, "pull-snapshot", number),
        final_event_id=_event_id(repository_key, "pull-final", number),
    )


def _structural_records(
    context: _PullRequestContext,
    *,
    repository_id: str,
    merged: bool,
) -> tuple[tuple[HistoricalEvent, HistoricalEvent], ChangeUnit]:
    snapshot = HistoricalEvent(
        id=context.prediction_event_id,
        repository_id=repository_id,
        kind="change_snapshot",
        occurred_at=context.created_at,
        available_at=context.created_at,
        provider="github",
        source_ref=f"{context.source_ref}:archive-snapshot",
        change_id=context.change_id,
        independent_group=context.change_id,
        data={
            "adapter": GITHUB_ADAPTER_VERSION,
            "base_sha": context.base_sha,
            "head_sha": context.prediction_sha,
            "point_in_time": False,
            "reconstructed_from": "current_pull_commit_lineage",
            "commits": [context.prediction_sha],
        },
    )
    final_kind = "change_merged" if merged else "change_closed"
    final = HistoricalEvent(
        id=context.final_event_id,
        repository_id=repository_id,
        kind=final_kind,
        occurred_at=context.finalized_at,
        available_at=context.finalized_at,
        provider="github",
        source_ref=f"{context.source_ref}:final",
        change_id=context.change_id,
        independent_group=context.change_id,
        data={
            "adapter": GITHUB_ADAPTER_VERSION,
            "base_sha": context.base_sha,
            "head_sha": context.final_sha,
            "final_sha": context.final_sha,
            "commits": list(context.commits),
        },
    )
    unit = ChangeUnit(
        id=context.change_id,
        repository_id=repository_id,
        kind="github_archive_change",
        base_sha=context.base_sha,
        prediction_sha=context.prediction_sha,
        prediction_at=context.created_at,
        final_sha=context.final_sha,
        finalized_at=context.finalized_at,
        commits=context.commits,
        event_ids=(snapshot.id, final.id),
        provider="github",
        source_ref=context.source_ref,
        evidence_quality="git_only",
        confirmatory=False,
    )
    return (snapshot, final), unit


def _review_events(
    raw_reviews: Sequence[JsonValue],
    context: _PullRequestContext,
    *,
    repository_id: str,
    repository_key: str,
    until: str,
) -> tuple[HistoricalEvent, ...]:
    events: list[HistoricalEvent] = []
    for index, raw_review in enumerate(raw_reviews):
        review = _object(raw_review, f"pull review[{index}]")
        review_id = _integer(review.get("id"), f"pull review[{index}].id")
        _string(review.get("state"), f"pull review {review_id}.state")
        submitted_at_value = _optional_string(
            review.get("submitted_at"), f"pull review {review_id}.submitted_at"
        )
        if submitted_at_value is None:
            continue
        submitted_at = _normalize_timestamp(
            submitted_at_value, name=f"pull review {review_id}.submitted_at"
        )
        assert submitted_at is not None
        if parse_timestamp(submitted_at) <= parse_timestamp(context.created_at):
            continue
        if not _at_or_before(submitted_at, until):
            continue
        reviewer = _object(review.get("user"), f"pull review {review_id}.user")
        reviewer_id = _integer(
            reviewer.get("id"),
            f"pull review {review_id}.user.id",
        )
        reviewer_group = _provider_key(f"{repository_key}.reviewer", reviewer_id)
        commit_id_value = review.get("commit_id")
        commit_id: str | None = None
        if isinstance(commit_id_value, str) and commit_id_value:
            commit_id = validate_git_sha(commit_id_value, field_name="pull review commit_id")
        data: JsonObject = {
            "adapter": GITHUB_ADAPTER_VERSION,
            "decision": "unspecified",
            "category": "unspecified",
            "independent": reviewer_id != context.author_id,
            "evidence_grade": "provider_event",
        }
        if commit_id is not None:
            data["commit_sha"] = commit_id
        events.append(
            HistoricalEvent(
                id=_event_id(repository_key, "review", review_id),
                repository_id=repository_id,
                kind="review",
                occurred_at=submitted_at,
                available_at=submitted_at,
                provider="github",
                source_ref=f"{context.source_ref}:review:{review_id}",
                change_id=context.change_id,
                independent_group=reviewer_group,
                data=data,
            )
        )
    return tuple(events)


def _check_events(
    raw_checks: Sequence[JsonValue],
    context: _PullRequestContext,
    *,
    repository_id: str,
    repository_key: str,
    expected_sha: str,
    until: str,
    merge_result: bool = False,
) -> tuple[HistoricalEvent, ...]:
    events: list[HistoricalEvent] = []
    for index, raw_check in enumerate(raw_checks):
        check = _object(raw_check, f"check run[{index}]")
        check_id_value = _integer(check.get("id"), f"check run[{index}].id")
        status = _string(check.get("status"), f"check run {check_id_value}.status").lower()
        conclusion = _optional_string(
            check.get("conclusion"), f"check run {check_id_value}.conclusion"
        )
        completed_at_value = _optional_string(
            check.get("completed_at"), f"check run {check_id_value}.completed_at"
        )
        if status != "completed" or conclusion is None or completed_at_value is None:
            continue
        completed_at = _normalize_timestamp(
            completed_at_value, name=f"check run {check_id_value}.completed_at"
        )
        assert completed_at is not None
        if parse_timestamp(completed_at) <= parse_timestamp(context.created_at):
            continue
        if not _at_or_before(completed_at, until):
            continue
        head_sha = validate_git_sha(
            _string(check.get("head_sha"), f"check run {check_id_value}.head_sha")
        )
        if head_sha != expected_sha:
            continue
        app_value = check.get("app")
        app_id = 0
        if isinstance(app_value, dict) and app_value.get("id") is not None:
            app_id = _integer(app_value.get("id"), f"check run {check_id_value}.app.id")
        name = _string(check.get("name"), f"check run {check_id_value}.name")
        provider_check_id = _provider_key(
            f"{repository_key}.check",
            f"{app_id}:{name}",
        )
        weak_merge_failure = merge_result and conclusion.lower() == "failure"
        version_key = content_hash(
            {
                "pull_number": context.number,
                "provider_id": check_id_value,
                "check_key": provider_check_id,
                "conclusion": conclusion.lower(),
                "completed_at": completed_at,
                "head_sha": head_sha,
                "merge_result": merge_result,
            }
        )[:20]
        events.append(
            HistoricalEvent(
                id=_event_id(
                    repository_key,
                    "check",
                    f"{check_id_value}-{version_key}",
                ),
                repository_id=repository_id,
                kind="ci_run",
                occurred_at=completed_at,
                available_at=completed_at,
                provider="github",
                source_ref=(f"{context.source_ref}:check:{check_id_value}:version:{version_key}"),
                change_id=context.change_id,
                independent_group=provider_check_id,
                data={
                    "adapter": GITHUB_ADAPTER_VERSION,
                    "check_id": provider_check_id,
                    "conclusion": conclusion.lower(),
                    "head_sha": head_sha,
                    "attributable_to_change": False,
                    "attribution": (
                        "unattributed_merge_result"
                        if merge_result
                        else "unattributed_provider_result"
                    ),
                    "evidence_grade": (
                        "weak_heuristic" if weak_merge_failure else "provider_event"
                    ),
                },
            )
        )
    return tuple(events)


def _revert_events(
    raw_commits: Sequence[JsonValue],
    change_by_commit: Mapping[str, _PullRequestContext],
    *,
    repository_id: str,
    repository_key: str,
    until: str,
) -> tuple[HistoricalEvent, ...]:
    events: list[HistoricalEvent] = []
    for index, raw_commit in enumerate(raw_commits):
        commit = _object(raw_commit, f"repository commit[{index}]")
        sha = validate_git_sha(_string(commit.get("sha"), f"repository commit[{index}].sha"))
        metadata = _object(commit.get("commit"), f"repository commit {sha}.commit")
        raw_message = metadata.get("message")
        if raw_message == "":
            continue
        message = _string(raw_message, f"repository commit {sha}.message")
        matches = tuple(_REVERT_TRAILER_RE.finditer(message))
        if len(matches) != 1:
            continue
        reverted_sha = matches[0].group(1)
        context = change_by_commit.get(reverted_sha)
        if context is None:
            continue
        committer = _object(metadata.get("committer"), f"repository commit {sha}.committer")
        occurred_at = _normalize_timestamp(
            _string(committer.get("date"), f"repository commit {sha}.committer.date"),
            name=f"repository commit {sha}.committer.date",
        )
        assert occurred_at is not None
        if parse_timestamp(occurred_at) <= parse_timestamp(context.finalized_at):
            continue
        if not _at_or_before(occurred_at, until):
            continue
        events.append(
            HistoricalEvent(
                id=_event_id(repository_key, "revert-heuristic", sha),
                repository_id=repository_id,
                kind="revert",
                occurred_at=occurred_at,
                available_at=occurred_at,
                provider="github",
                source_ref=f"github:{repository_key}:commit:{sha}",
                change_id=context.change_id,
                independent_group=_provider_key(f"{repository_key}.revert", sha),
                data={
                    "adapter": GITHUB_ADAPTER_VERSION,
                    "linked_change_id": context.change_id,
                    "linked_commit_sha": reverted_sha,
                    "link_kind": "heuristic",
                    "evidence_grade": "weak_heuristic",
                    "heuristic_id": "git_revert_trailer@1",
                    "sha": sha,
                },
            )
        )
    return tuple(events)


def _collect_check_runs(
    client: GitHubApi,
    endpoint: str,
    *,
    maximum: int,
) -> tuple[list[JsonValue], bool]:
    records: list[JsonValue] = []
    page_number = 1
    declared_total: int | None = None
    while True:
        response_value = client.get(
            endpoint,
            params={
                "filter": "all",
                "page": str(page_number),
                "per_page": str(_PAGE_SIZE),
            },
        )
        response = _object(response_value, endpoint)
        total = _integer(response.get("total_count"), f"{endpoint}.total_count")
        if declared_total is None:
            declared_total = total
        elif total != declared_total:
            raise GitHubHistoryError(f"GitHub check-run total changed while paging {endpoint}")
        page = _array(response.get("check_runs"), f"{endpoint}.check_runs")
        remaining = maximum - len(records)
        if len(page) > remaining:
            records.extend(page[:remaining])
            return records, True
        records.extend(page)
        if len(records) >= total:
            return records, False
        if not page:
            raise GitHubHistoryError(
                f"GitHub check-run pagination ended before declared total for {endpoint}"
            )
        if len(records) == maximum:
            return records, True
        page_number += 1


def collect_github_history(
    client: GitHubApi,
    repository: str,
    repository_id: str,
    *,
    since: str | datetime | None = None,
    until: str | datetime | None = None,
    max_pull_requests: int = 1_000,
    max_commits_per_pull: int = 1_000,
    max_reviews_per_pull: int = 1_000,
    max_checks_per_commit: int = 1_000,
    max_repository_commits: int = 10_000,
    max_api_requests: int = _DEFAULT_MAX_API_REQUESTS,
    max_provider_records: int = _DEFAULT_MAX_PROVIDER_RECORDS,
    repository_binding: str = "caller_asserted",
    provider_host: str = "github.com",
    clock: Callable[[], datetime] = _now_utc,
) -> GitHubHistoryReport:
    """Collect a bounded, deterministic, non-confirmatory GitHub archive.

    Pull requests with force-push timeline events are skipped because the
    current commit list cannot reconstruct their opening lineage safely.  Open
    pull requests are not assembled: v1 change units are immutable and cannot
    later be upgraded with finalization state.  Per-endpoint limits may return
    an explicitly truncated report, but the global request and provider-record
    budgets fail closed: exhaustion raises before the CLI persistence phase, so
    a partially collected pull request can never enter normalized history.
    """
    repository_parts = repository.split("/")
    if (
        not _REPOSITORY_RE.fullmatch(repository)
        or len(repository_parts) != 2
        or repository_parts[1] in {".", ".."}
    ):
        raise GitHubHistoryError("repository must be a safe GitHub OWNER/NAME value")
    try:
        normalized_repository_id = validate_subject(repository_id)
    except ModelError as exc:
        raise GitHubHistoryError("repository_id must be a stable RuleLoom identifier") from exc
    if repository_binding not in _REPOSITORY_BINDINGS:
        raise GitHubHistoryError("repository_binding has an unsupported value")
    if provider_host.casefold() != "github.com":
        raise GitHubHistoryError("GitHub adapter v1 supports only provider_host='github.com'")
    provider_host = provider_host.casefold()
    pull_limit = _positive_limit(
        max_pull_requests, name="max_pull_requests", maximum=_MAX_PULL_REQUESTS
    )
    commit_limit = _positive_limit(
        max_commits_per_pull,
        name="max_commits_per_pull",
        maximum=_MAX_COMMITS_PER_PULL,
    )
    review_limit = _positive_limit(
        max_reviews_per_pull,
        name="max_reviews_per_pull",
        maximum=_MAX_REVIEWS_PER_PULL,
    )
    check_limit = _positive_limit(
        max_checks_per_commit,
        name="max_checks_per_commit",
        maximum=_MAX_CHECKS_PER_COMMIT,
    )
    repository_commit_limit = _positive_limit(
        max_repository_commits,
        name="max_repository_commits",
        maximum=_MAX_REPOSITORY_COMMITS,
    )
    api_request_limit = _positive_limit(
        max_api_requests,
        name="max_api_requests",
        maximum=_MAX_API_REQUESTS,
    )
    provider_record_limit = _positive_limit(
        max_provider_records,
        name="max_provider_records",
        maximum=_MAX_PROVIDER_RECORDS,
    )
    collected_at = _normalize_timestamp(clock(), name="collection time")
    assert collected_at is not None
    normalized_since = _normalize_timestamp(since, name="since")
    normalized_until = _normalize_timestamp(until, name="until") or collected_at
    if parse_timestamp(normalized_until) > parse_timestamp(collected_at):
        raise GitHubHistoryError("until cannot be later than collection time")
    if normalized_since is not None and parse_timestamp(normalized_since) > parse_timestamp(
        normalized_until
    ):
        raise GitHubHistoryError("since cannot be later than until")

    budget = _CollectionBudget(
        maximum_requests=api_request_limit,
        maximum_records=provider_record_limit,
    )
    client = _BudgetedGitHubApi(client, budget)

    repository_metadata = _object(
        client.get(_repository_endpoint(repository)), "repository metadata"
    )
    provider_repository_id = _integer(repository_metadata.get("id"), "repository id")
    repository_key = _provider_key(f"{provider_host}.repo", provider_repository_id)

    pull_values, pulls_truncated = _collect_pages(
        client,
        _repository_endpoint(repository, "/pulls"),
        maximum=pull_limit,
        params={"direction": "desc", "sort": "created", "state": "closed"},
    )
    events: list[HistoricalEvent] = []
    units: list[ChangeUnit] = []
    warnings: list[str] = []
    skipped = 0
    examined = 0
    normalized = 0
    truncated = pulls_truncated
    contexts: list[_PullRequestContext] = []
    if pulls_truncated:
        warnings.append(
            "pull request scan was truncated in newest-first order; coverage before "
            "the oldest fetched pull request is incomplete"
        )

    for raw_pull in pull_values:
        summary = _object(raw_pull, "pull request summary")
        number = _integer(summary.get("number"), "pull request summary.number")
        detail = _object(
            client.get(_repository_endpoint(repository, f"/pulls/{number}")),
            f"pull request {number}",
        )
        if _integer(detail.get("number"), f"pull request {number}.number") != number:
            raise GitHubHistoryError(f"GitHub returned the wrong pull request for {number}")
        created_at = _normalize_timestamp(
            _string(detail.get("created_at"), f"pull request {number}.created_at"),
            name=f"pull request {number}.created_at",
        )
        assert created_at is not None
        if parse_timestamp(created_at) > parse_timestamp(normalized_until):
            continue
        if normalized_since is not None and parse_timestamp(created_at) < parse_timestamp(
            normalized_since
        ):
            continue
        examined += 1
        state = _string(detail.get("state"), f"pull request {number}.state").lower()
        if state != "closed":
            skipped += 1
            warnings.append(
                f"pull request {number} was returned by the closed-only query in state "
                f"{state!r} and was not assembled"
            )
            continue
        merged_at = _normalize_timestamp(
            _optional_string(
                detail.get("merged_at"),
                f"pull request {number}.merged_at",
            ),
            name=f"pull request {number}.merged_at",
        )
        closed_at = _normalize_timestamp(
            _optional_string(
                detail.get("closed_at"),
                f"pull request {number}.closed_at",
            ),
            name=f"pull request {number}.closed_at",
        )
        finalized_at = merged_at or closed_at
        if finalized_at is None:
            raise GitHubHistoryError(f"closed pull request {number} lacks a final timestamp")
        if not _at_or_before(finalized_at, normalized_until):
            skipped += 1
            warnings.append(
                f"pull request {number} finalized after the requested cutoff and was not assembled"
            )
            continue
        raw_commits, commits_truncated = _collect_pages(
            client,
            _repository_endpoint(repository, f"/pulls/{number}/commits"),
            maximum=commit_limit,
        )
        raw_timeline, timeline_truncated = _collect_pages(
            client,
            _repository_endpoint(repository, f"/issues/{number}/timeline"),
            maximum=review_limit,
        )
        raw_reviews, reviews_truncated = _collect_pages(
            client,
            _repository_endpoint(repository, f"/pulls/{number}/reviews"),
            maximum=review_limit,
        )
        if commits_truncated or timeline_truncated or reviews_truncated:
            truncated = True
            skipped += 1
            warnings.append(
                f"pull request {number} exceeded an archive pagination limit and was skipped"
            )
            continue
        context = _normalize_pull_context(
            detail,
            raw_commits,
            raw_timeline,
            repository_key=repository_key,
        )
        if context is None:
            skipped += 1
            reason = "is still open" if state == "open" else "contains force-push history"
            warnings.append(f"pull request {number} {reason} and was not assembled")
            continue
        merged = detail.get("merged_at") is not None
        structural_events, unit = _structural_records(
            context,
            repository_id=normalized_repository_id,
            merged=merged,
        )
        per_pull_events: list[HistoricalEvent] = list(structural_events)
        per_pull_events.extend(
            _review_events(
                raw_reviews,
                context,
                repository_id=normalized_repository_id,
                repository_key=repository_key,
                until=normalized_until,
            )
        )
        checks_truncated = False
        seen_check_ids: set[str] = set()
        check_shas = tuple(
            dict.fromkeys(
                (*context.commits, *((context.merge_sha,) if context.merge_sha is not None else ()))
            )
        )
        for commit_sha in check_shas:
            endpoint = _repository_endpoint(repository, f"/commits/{commit_sha}/check-runs")
            raw_checks, commit_checks_truncated = _collect_check_runs(
                client,
                endpoint,
                maximum=check_limit,
            )
            if commit_checks_truncated:
                checks_truncated = True
                break
            for event in _check_events(
                raw_checks,
                context,
                repository_id=normalized_repository_id,
                repository_key=repository_key,
                expected_sha=commit_sha,
                until=normalized_until,
                merge_result=commit_sha == context.merge_sha,
            ):
                if event.id in seen_check_ids:
                    continue
                seen_check_ids.add(event.id)
                per_pull_events.append(event)
        if checks_truncated:
            truncated = True
            skipped += 1
            warnings.append(f"pull request {number} exceeded the check-run limit and was skipped")
            continue
        events.extend(per_pull_events)
        units.append(unit)
        contexts.append(context)
        normalized += 1

    commit_query: dict[str, str] = {}
    if normalized_since is not None:
        commit_query["since"] = normalized_since
    commit_query["until"] = normalized_until
    repository_commits, repository_commits_truncated = _collect_pages(
        client,
        _repository_endpoint(repository, "/commits"),
        maximum=repository_commit_limit,
        params=commit_query,
    )
    truncated = truncated or repository_commits_truncated
    if repository_commits_truncated:
        warnings.append(
            "repository commit scan was truncated; heuristic revert coverage is incomplete"
        )
    change_by_commit = {
        commit_sha: context for context in contexts for commit_sha in context.commits
    }
    events.extend(
        _revert_events(
            repository_commits,
            change_by_commit,
            repository_id=normalized_repository_id,
            repository_key=repository_key,
            until=normalized_until,
        )
    )

    event_by_id: dict[str, HistoricalEvent] = {}
    for event in events:
        previous_event = event_by_id.get(event.id)
        if previous_event is not None and previous_event != event:
            raise GitHubHistoryError(f"conflicting normalized GitHub event id {event.id!r}")
        event_by_id[event.id] = event
    unit_by_id: dict[str, ChangeUnit] = {}
    for unit in units:
        previous_unit = unit_by_id.get(unit.id)
        if previous_unit is not None and previous_unit != unit:
            raise GitHubHistoryError(f"conflicting normalized GitHub change id {unit.id!r}")
        unit_by_id[unit.id] = unit
    ordered_events = tuple(
        sorted(
            event_by_id.values(),
            key=lambda item: (
                parse_timestamp(item.available_at),
                parse_timestamp(item.occurred_at),
                item.id,
            ),
        )
    )
    ordered_units = tuple(
        sorted(
            unit_by_id.values(),
            key=lambda item: (parse_timestamp(item.prediction_at), item.id),
        )
    )
    return GitHubHistoryReport(
        events=ordered_events,
        units=ordered_units,
        pull_requests_examined=examined,
        pull_requests_normalized=normalized,
        pull_requests_skipped=skipped,
        warnings=tuple(warnings),
        truncated=truncated,
        provider_repository_key=repository_key,
        collected_at=collected_at,
        since=normalized_since,
        until=normalized_until,
        repository_id=normalized_repository_id,
        provider_host=provider_host,
        repository_binding=repository_binding,
        api_requests_used=budget.requests_used,
        provider_records_used=budget.records_used,
        max_api_requests=api_request_limit,
        max_provider_records=provider_record_limit,
        max_pull_requests=pull_limit,
        max_commits_per_pull=commit_limit,
        max_reviews_per_pull=review_limit,
        max_checks_per_commit=check_limit,
        max_repository_commits=repository_commit_limit,
    )


__all__ = [
    "GITHUB_ADAPTER_VERSION",
    "GhApiClient",
    "GitHubApi",
    "GitHubHistoryError",
    "GitHubHistoryReport",
    "collect_github_history",
]
