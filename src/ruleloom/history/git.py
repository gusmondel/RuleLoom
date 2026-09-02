"""Deterministic, language-agnostic bootstrap from the Git object graph.

This module deliberately collects commit topology and headers only.  It does
not inspect paths, patches, source files, or programming-language metadata.
Provider-specific PR, CI, review, and incident evidence belongs in separate
adapters that can link back to the emitted change units.

Two Git-native outcome signals are additionally recorded without reading prose
as instructions: the exact ``This reverts commit <sha>`` trailer that ``git
revert`` generates becomes a weak ``revert`` event, and one
``git_history_horizon`` event records the newest committer timestamp of the
complete reachable prefix so a registered revert window can later prove it
was fully observable.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from ruleloom.gitfacts import repository_identity
from ruleloom.history.models import ChangeUnit, HistoricalEvent
from ruleloom.history.storage import HISTORY_JSONL_MAX_BYTES, HISTORY_JSONL_MAX_LINE_BYTES
from ruleloom.models import (
    JsonObject,
    ModelError,
    canonical_json,
    content_hash,
    parse_timestamp,
    validate_subject,
)

_GIT_TIMEOUT_SECONDS = 45.0
_MAX_GIT_STDOUT_BYTES = 64 * 1024 * 1024
_MAX_GIT_STDERR_BYTES = 1024 * 1024
_MAX_GIT_INPUT_BYTES = 1024
_MAX_REF_BYTES = 1024
_MAX_SUBJECT_BYTES = 512
_MAX_COMMITS = 100_000
_LOG_FIELD_SEPARATOR = b"\x1f"
_LOG_RECORD_SEPARATOR = b"\x00"
_LOG_FORMAT = "%H%x1f%P%x1f%cI%x1f%an%x1f%ae%x1f%s%x00"
_REVERT_LOG_FORMAT = "%H%x1f%B%x00"
_REVERT_GREP = "^This reverts commit [0-9a-f]{40}"
_REVERT_TRAILER_RE = re.compile(
    r"(?im)^this reverts commit ([0-9a-f]{40}|[0-9a-f]{64})(?=[.,;:\s]|$)"
)
_MAX_REVERT_TRAILERS_PER_COMMIT = 8
_OID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")

GIT_HISTORY_ADAPTER_VERSION = "ruleloom-git/2"
REVERT_TRAILER_LINK_KIND = "git_trailer"
HISTORY_HORIZON_EVENT_KIND = "git_history_horizon"


class GitHistoryError(RuntimeError):
    """Raised when historical Git metadata cannot be collected safely."""


@dataclass(frozen=True, slots=True)
class GitHistoryBudgets:
    """Caller-reducible work budgets for one read-only Git traversal.

    Defaults preserve the public v0.7 safety ceilings. Every value may only
    reduce its built-in process or storage cap; this object cannot raise the
    global limits.
    """

    timeout_seconds: float = _GIT_TIMEOUT_SECONDS
    git_stdout_bytes: int = _MAX_GIT_STDOUT_BYTES
    git_stderr_bytes: int = _MAX_GIT_STDERR_BYTES
    storage_bytes: int | None = None
    storage_line_bytes: int | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int | float)
            or not 0 < float(self.timeout_seconds) <= _GIT_TIMEOUT_SECONDS
        ):
            raise GitHistoryError(
                "history timeout_seconds must be greater than zero and at most "
                f"{_GIT_TIMEOUT_SECONDS:g}"
            )
        for name, value, maximum in (
            ("git_stdout_bytes", self.git_stdout_bytes, _MAX_GIT_STDOUT_BYTES),
            ("git_stderr_bytes", self.git_stderr_bytes, _MAX_GIT_STDERR_BYTES),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise GitHistoryError(f"history {name} must be between 1 and {maximum} bytes")
        for storage_name, optional_value, storage_maximum in (
            ("storage_bytes", self.storage_bytes, HISTORY_JSONL_MAX_BYTES),
            ("storage_line_bytes", self.storage_line_bytes, HISTORY_JSONL_MAX_LINE_BYTES),
        ):
            if optional_value is not None and (
                isinstance(optional_value, bool)
                or not isinstance(optional_value, int)
                or not 1 <= optional_value <= storage_maximum
            ):
                raise GitHistoryError(
                    f"history {storage_name} must be null or between 1 and {storage_maximum} bytes"
                )

    @property
    def effective_storage_bytes(self) -> int:
        return min(self.storage_bytes or HISTORY_JSONL_MAX_BYTES, HISTORY_JSONL_MAX_BYTES)

    @property
    def effective_storage_line_bytes(self) -> int:
        return min(
            self.storage_line_bytes or HISTORY_JSONL_MAX_LINE_BYTES,
            HISTORY_JSONL_MAX_LINE_BYTES,
        )

    def to_dict(self) -> JsonObject:
        return {
            "timeout_seconds": float(self.timeout_seconds),
            "git_stdout_bytes": self.git_stdout_bytes,
            "git_stderr_bytes": self.git_stderr_bytes,
            "storage_bytes": self.effective_storage_bytes,
            "storage_line_bytes": self.effective_storage_line_bytes,
        }


@dataclass(frozen=True, slots=True)
class GitHistoryReport:
    """Auditable result of a bounded traversal of one Git revision."""

    events: tuple[HistoricalEvent, ...]
    units: tuple[ChangeUnit, ...]
    examined: int
    shallow: bool
    truncated: bool
    warnings: tuple[str, ...]
    manifest_hash: str
    resolved_ref: str
    after: str | None
    incremental_boundary_is_ancestor: bool | None
    since: str | None
    requested_max_commits: int | None
    budgets: GitHistoryBudgets
    storage_truncated: bool
    storage_byte_limit: int
    storage_line_byte_limit: int
    event_log_bytes: int
    change_unit_log_bytes: int
    revert_events: int = 0
    horizon_at: str | None = None
    shallow_boundary_commits: int = 0

    @property
    def event_count(self) -> int:
        return len(self.events)

    @property
    def unit_count(self) -> int:
        return len(self.units)

    def to_dict(self) -> JsonObject:
        return {
            "examined": self.examined,
            "events": self.event_count,
            "units": self.unit_count,
            "shallow": self.shallow,
            "truncated": self.truncated,
            "warnings": list(self.warnings),
            "manifest_hash": self.manifest_hash,
            "resolved_ref": self.resolved_ref,
            "after": self.after,
            "incremental": self.after is not None,
            "incremental_boundary_is_ancestor": self.incremental_boundary_is_ancestor,
            "since": self.since,
            "requested_max_commits": self.requested_max_commits,
            "budgets": self.budgets.to_dict(),
            "storage_truncated": self.storage_truncated,
            "storage_byte_limit": self.storage_byte_limit,
            "storage_line_byte_limit": self.storage_line_byte_limit,
            "event_log_bytes": self.event_log_bytes,
            "change_unit_log_bytes": self.change_unit_log_bytes,
            "revert_events": self.revert_events,
            "horizon_at": self.horizon_at,
            "shallow_boundary_commits": self.shallow_boundary_commits,
            "adapter": GIT_HISTORY_ADAPTER_VERSION,
        }


@dataclass(frozen=True, slots=True)
class _CommitHeader:
    sha: str
    parents: tuple[str, ...]
    committed_at: str
    author_name: str
    author_email: str
    subject: str


def _run_git_bounded(
    repo: Path,
    arguments: tuple[str, ...],
    *,
    input_bytes: bytes | None = None,
    stdout_limit: int = _MAX_GIT_STDOUT_BYTES,
    budgets: GitHistoryBudgets | None = None,
) -> tuple[bytes, bytes, int]:
    """Execute Git without a shell while enforcing time and output budgets."""
    if input_bytes is not None and len(input_bytes) > _MAX_GIT_INPUT_BYTES:
        raise GitHistoryError(f"Git stdin exceeds {_MAX_GIT_INPUT_BYTES} bytes")
    selected_budgets = budgets or GitHistoryBudgets()
    effective_stdout_limit = min(stdout_limit, selected_budgets.git_stdout_bytes)
    try:
        process = subprocess.Popen(
            ("git", "-C", str(repo), *arguments),
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise GitHistoryError("Git is not installed or is not available on PATH") from exc

    if input_bytes is not None:
        assert process.stdin is not None
        try:
            process.stdin.write(input_bytes)
        except BrokenPipeError as exc:
            process.wait()
            raise GitHistoryError(f"git {' '.join(arguments)} closed stdin early") from exc
        finally:
            process.stdin.close()

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
                violation_messages.append(f"git {' '.join(arguments)} {name} exceeds {limit} bytes")
                violation.set()
                return
            buffers[name].extend(chunk)

    readers = (
        threading.Thread(
            target=drain,
            args=("stdout", process.stdout, effective_stdout_limit),
            daemon=True,
        ),
        threading.Thread(
            target=drain,
            args=("stderr", process.stderr, selected_budgets.git_stderr_bytes),
            daemon=True,
        ),
    )
    for reader in readers:
        reader.start()

    deadline = time.monotonic() + float(selected_budgets.timeout_seconds)
    try:
        while process.poll() is None:
            if violation.is_set():
                process.kill()
                process.wait()
                raise GitHistoryError(violation_messages[0])
            if time.monotonic() >= deadline:
                process.kill()
                process.wait()
                raise GitHistoryError(
                    f"git {' '.join(arguments)} exceeded "
                    f"{float(selected_budgets.timeout_seconds):g} seconds"
                )
            time.sleep(0.01)

        returncode = process.wait()
        for reader in readers:
            reader.join(timeout=1)
        if any(reader.is_alive() for reader in readers):
            process.kill()
            process.wait()
            raise GitHistoryError(f"git {' '.join(arguments)} output readers did not terminate")
        if violation.is_set():
            raise GitHistoryError(violation_messages[0])
    finally:
        process.stdout.close()
        process.stderr.close()

    return bytes(buffers["stdout"]), bytes(buffers["stderr"]), returncode


def _git_bytes(
    repo: Path,
    *arguments: str,
    input_bytes: bytes | None = None,
    stdout_limit: int = _MAX_GIT_STDOUT_BYTES,
    budgets: GitHistoryBudgets | None = None,
) -> bytes:
    stdout, stderr, returncode = _run_git_bounded(
        repo,
        arguments,
        input_bytes=input_bytes,
        stdout_limit=stdout_limit,
        budgets=budgets,
    )
    if returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip() or "unknown Git error"
        raise GitHistoryError(f"git {' '.join(arguments)} failed: {detail}")
    return stdout


def _decode_git_text(value: bytes, *, field_name: str) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GitHistoryError(
            f"Git returned non-UTF-8 {field_name}; RuleLoom refuses lossy historical evidence"
        ) from exc


def _git_text(
    repo: Path,
    *arguments: str,
    input_bytes: bytes | None = None,
    stdout_limit: int = _MAX_GIT_STDOUT_BYTES,
    budgets: GitHistoryBudgets | None = None,
) -> str:
    return _decode_git_text(
        _git_bytes(
            repo,
            *arguments,
            input_bytes=input_bytes,
            stdout_limit=stdout_limit,
            budgets=budgets,
        ),
        field_name="metadata",
    )


def _validate_ref(ref: str) -> str:
    if not isinstance(ref, str) or not ref:
        raise GitHistoryError("ref must be a non-empty string")
    try:
        encoded = ref.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise GitHistoryError("ref must be valid UTF-8") from exc
    if len(encoded) > _MAX_REF_BYTES:
        raise GitHistoryError(f"ref exceeds {_MAX_REF_BYTES} UTF-8 bytes")
    if ref.startswith("-") or any(character in ref for character in ("\x00", "\n", "\r")):
        raise GitHistoryError("ref contains unsafe characters")
    return ref


def _validate_max_commits(max_commits: int | None) -> int:
    if max_commits is None:
        return _MAX_COMMITS
    if isinstance(max_commits, bool) or not isinstance(max_commits, int):
        raise GitHistoryError("max_commits must be a positive integer or null")
    if not 1 <= max_commits <= _MAX_COMMITS:
        raise GitHistoryError(f"max_commits must be between 1 and {_MAX_COMMITS}")
    return max_commits


def _normalize_since(since: str | datetime | None) -> str | None:
    if since is None:
        return None
    if isinstance(since, datetime):
        parsed = since
        if parsed.tzinfo is None:
            raise GitHistoryError("since must include a timezone")
    elif isinstance(since, str):
        try:
            parsed = parse_timestamp(since)
        except ValueError as exc:
            raise GitHistoryError("since must be an aware ISO-8601 timestamp") from exc
    else:
        raise GitHistoryError("since must be an aware ISO-8601 timestamp or null")
    normalized = parsed.astimezone(UTC)
    timespec = "microseconds" if normalized.microsecond else "seconds"
    return normalized.isoformat(timespec=timespec).replace("+00:00", "Z")


def _validate_repository_id(repository_id: str) -> str:
    if not isinstance(repository_id, str):
        raise GitHistoryError("repository_id must be a stable RuleLoom identifier")
    try:
        return validate_subject(repository_id)
    except ModelError as exc:
        raise GitHistoryError("repository_id must be a stable RuleLoom identifier") from exc


def _validate_oid(value: str, *, field_name: str) -> str:
    if not _OID_RE.fullmatch(value):
        raise GitHistoryError(f"Git returned an invalid {field_name}: {value!r}")
    return value


def _parse_log(stdout: bytes) -> tuple[_CommitHeader, ...]:
    records: list[_CommitHeader] = []
    for raw_record in stdout.split(_LOG_RECORD_SEPARATOR):
        if raw_record.startswith(b"\n"):
            raw_record = raw_record[1:]
        if not raw_record or raw_record == b"\n":
            continue
        if raw_record.endswith(b"\n"):
            raw_record = raw_record[:-1]
        fields = raw_record.split(_LOG_FIELD_SEPARATOR)
        if len(fields) != 6:
            raise GitHistoryError("Git returned a malformed historical commit record")
        sha, raw_parents, committed_at, author_name, author_email, subject = (
            _decode_git_text(field, field_name="commit metadata") for field in fields
        )
        parents = tuple(raw_parents.split()) if raw_parents else ()
        _validate_oid(sha, field_name="commit object ID")
        for parent in parents:
            _validate_oid(parent, field_name="parent object ID")
        try:
            parse_timestamp(committed_at)
        except ValueError as exc:
            raise GitHistoryError(
                f"Git returned an invalid commit timestamp: {committed_at!r}"
            ) from exc
        records.append(
            _CommitHeader(
                sha=sha,
                parents=parents,
                committed_at=committed_at,
                author_name=author_name,
                author_email=author_email,
                subject=subject,
            )
        )
    return tuple(records)


def _bounded_subject(subject: str) -> tuple[str, bool]:
    encoded = subject.encode("utf-8")
    if len(encoded) <= _MAX_SUBJECT_BYTES:
        return subject, False
    prefix = encoded[: _MAX_SUBJECT_BYTES - len("…".encode())]
    while True:
        try:
            return prefix.decode("utf-8") + "…", True
        except UnicodeDecodeError:
            prefix = prefix[:-1]


def _author_hash(repository_id: str, name: str, email: str) -> str:
    identity = (
        b"ruleloom.git.author.v1\x00"
        + repository_id.encode("utf-8")
        + b"\x00"
        + name.encode("utf-8")
        + b"\x00"
        + email.encode("utf-8")
    )
    return hashlib.sha256(identity).hexdigest()


def _event_and_unit(
    header: _CommitHeader,
    *,
    repository_id: str,
    empty_tree: str,
) -> tuple[HistoricalEvent, ChangeUnit]:
    is_merge = len(header.parents) > 1
    kind = "git_merge" if is_merge else "git_commit"
    event_id = f"event.{kind}.{header.sha}"
    change_id = f"change.{kind}.{header.sha}"
    subject, subject_truncated = _bounded_subject(header.subject)
    event = HistoricalEvent(
        schema_version=1,
        id=event_id,
        repository_id=repository_id,
        kind=kind,
        occurred_at=header.committed_at,
        available_at=header.committed_at,
        provider="git",
        source_ref=header.sha,
        independent_group=change_id,
        data={
            "sha": header.sha,
            "parents": list(header.parents),
            "committed_at": header.committed_at,
            "subject": subject,
            "subject_hash": hashlib.sha256(header.subject.encode("utf-8")).hexdigest(),
            "subject_truncated": subject_truncated,
            "author_hash": _author_hash(
                repository_id,
                header.author_name,
                header.author_email,
            ),
        },
        change_id=change_id,
    )
    unit = ChangeUnit(
        schema_version=1,
        id=change_id,
        repository_id=repository_id,
        kind=kind,
        base_sha=header.parents[0] if header.parents else empty_tree,
        prediction_sha=header.sha,
        prediction_at=header.committed_at,
        commits=(header.sha,),
        event_ids=(event_id,),
        provider="git",
        source_ref=header.sha,
        evidence_quality="final_only" if is_merge else "git_only",
        confirmatory=False,
        final_sha=header.sha if is_merge else None,
        finalized_at=header.committed_at if is_merge else None,
    )
    return event, unit


def _revert_events(
    header: _CommitHeader,
    reverted_shas: tuple[str, ...],
    *,
    repository_id: str,
    kinds_by_sha: dict[str, str],
) -> tuple[HistoricalEvent, ...]:
    """Emit one weak ``revert`` event per exact trailer target of a reverting commit."""
    reverting_kind = kinds_by_sha.get(header.sha, "git_commit")
    events: list[HistoricalEvent] = []
    for reverted in reverted_shas:
        reverted_kind = kinds_by_sha.get(reverted, "git_commit")
        linked_change = f"change.{reverted_kind}.{reverted}"
        events.append(
            HistoricalEvent(
                schema_version=1,
                id=f"event.git_revert.{header.sha}.{reverted}",
                repository_id=repository_id,
                kind="revert",
                occurred_at=header.committed_at,
                available_at=header.committed_at,
                provider="git",
                source_ref=header.sha,
                independent_group=f"change.{reverting_kind}.{header.sha}",
                data={
                    "adapter": GIT_HISTORY_ADAPTER_VERSION,
                    "sha": header.sha,
                    "reverted_sha": reverted,
                    "linked_change_id": linked_change,
                    "link_kind": REVERT_TRAILER_LINK_KIND,
                    "evidence_grade": "weak_heuristic",
                    "heuristic_id": "git_revert_trailer@1",
                    "committed_at": header.committed_at,
                },
                change_id=linked_change,
            )
        )
    return tuple(events)


def _horizon_event(
    *,
    repository_id: str,
    resolved_ref: str,
    horizon_at: str,
) -> HistoricalEvent:
    digest = hashlib.sha256(
        f"{repository_id}\x00{resolved_ref}\x00{horizon_at}".encode()
    ).hexdigest()[:20]
    identifier = f"event.{HISTORY_HORIZON_EVENT_KIND}.{digest}"
    return HistoricalEvent(
        schema_version=1,
        id=identifier,
        repository_id=repository_id,
        kind=HISTORY_HORIZON_EVENT_KIND,
        occurred_at=horizon_at,
        available_at=horizon_at,
        provider="git",
        source_ref=resolved_ref,
        independent_group=identifier,
        data={
            "adapter": GIT_HISTORY_ADAPTER_VERSION,
            "resolved_ref": resolved_ref,
            "horizon_at": horizon_at,
            "selection": "newest_committer_timestamp_of_complete_reachable_prefix",
        },
        change_id=None,
    )


def _parse_revert_log(stdout: bytes, selected: set[str]) -> dict[str, tuple[str, ...]]:
    """Map selected reverting commits to the exact trailer targets in their bodies."""
    reverts: dict[str, tuple[str, ...]] = {}
    for raw_record in stdout.split(_LOG_RECORD_SEPARATOR):
        if raw_record.startswith(b"\n"):
            raw_record = raw_record[1:]
        if not raw_record.strip():
            continue
        fields = raw_record.split(_LOG_FIELD_SEPARATOR, 1)
        if len(fields) != 2:
            raise GitHistoryError("Git returned a malformed revert trailer record")
        sha = _decode_git_text(fields[0], field_name="commit object ID").strip()
        _validate_oid(sha, field_name="commit object ID")
        if sha not in selected:
            continue
        try:
            body = fields[1].decode("utf-8")
        except UnicodeDecodeError:
            # A non-UTF-8 body cannot carry a trusted trailer; abstain for this commit.
            continue
        targets: list[str] = []
        for match in _REVERT_TRAILER_RE.finditer(body):
            target = match.group(1)
            if target == sha or target in targets:
                continue
            targets.append(target)
            if len(targets) >= _MAX_REVERT_TRAILERS_PER_COMMIT:
                break
        if targets:
            reverts[sha] = tuple(targets)
    return reverts


def _canonical_record_bytes(record: HistoricalEvent | ChangeUnit) -> int:
    """Return canonical UTF-8 record bytes, excluding the JSONL newline."""
    return len(canonical_json(record.to_dict()).encode("utf-8"))


def collect_git_history(
    repo: Path,
    *,
    ref: str = "HEAD",
    max_commits: int | None = 1_000,
    since: str | datetime | None = None,
    after: str | None = None,
    repository_id: str | None = None,
    budgets: GitHistoryBudgets | None = None,
) -> GitHistoryReport:
    """Collect bounded historical evidence from one revision.

    The most recent ``max_commits`` reachable commits are selected and emitted
    oldest-first in reverse date/topological order.  ``None`` requests all
    reachable history up to the hard safety cap; truncation is always explicit
    in the returned report. ``after`` is an exclusive commit cursor and must be
    an ancestor of the selected ref; divergent or rewritten history fails
    closed. Incremental collection from a shallow repository is rejected because
    local ancestry cannot prove that the interval is complete. Incremental ranges
    must be complete: combining ``after`` with ``since``, or hitting a
    commit/storage truncation after a cursor, raises instead of returning a range
    whose tip could skip unseen commits. Explicit ``budgets`` may reduce, but
    never raise, hard safety caps.
    """
    requested_ref = _validate_ref(ref)
    requested_after = None if after is None else _validate_ref(after)
    effective_limit = _validate_max_commits(max_commits)
    normalized_since = _normalize_since(since)
    if requested_after is not None and normalized_since is not None:
        raise GitHistoryError(
            "after and since are mutually exclusive; timestamp filtering can omit "
            "intermediate commits from an incremental cursor range"
        )
    selected_budgets = budgets or GitHistoryBudgets()
    resolved_repo = repo.resolve()
    if not resolved_repo.is_dir():
        raise GitHistoryError(f"repository directory does not exist: {resolved_repo}")
    top_level_text = _git_text(
        resolved_repo,
        "rev-parse",
        "--show-toplevel",
        stdout_limit=16 * 1024,
        budgets=selected_budgets,
    ).strip()
    if not top_level_text:
        raise GitHistoryError("Git returned an empty repository root")
    top_level = Path(top_level_text).resolve()

    resolved_ref = _git_text(
        top_level,
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{requested_ref}^{{commit}}",
        stdout_limit=1024,
        budgets=selected_budgets,
    ).strip()
    _validate_oid(resolved_ref, field_name="resolved revision")
    resolved_after: str | None = None
    boundary_is_ancestor: bool | None = None
    if requested_after is not None:
        resolved_after = _git_text(
            top_level,
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{requested_after}^{{commit}}",
            stdout_limit=1024,
            budgets=selected_budgets,
        ).strip()
        _validate_oid(resolved_after, field_name="incremental boundary")
        _stdout, stderr, ancestry_returncode = _run_git_bounded(
            top_level,
            ("merge-base", "--is-ancestor", resolved_after, resolved_ref),
            stdout_limit=1024,
            budgets=selected_budgets,
        )
        if ancestry_returncode not in {0, 1}:
            detail = stderr.decode("utf-8", errors="replace").strip() or "unknown Git error"
            raise GitHistoryError(f"git merge-base --is-ancestor failed: {detail}")
        if ancestry_returncode == 1:
            raise GitHistoryError(
                "incremental boundary is not an ancestor of the selected ref; refusing "
                "to mix divergent or rewritten history"
            )
        boundary_is_ancestor = True
    effective_repository_id = _validate_repository_id(
        repository_id if repository_id is not None else repository_identity(top_level)
    )

    shallow_text = _git_text(
        top_level,
        "rev-parse",
        "--is-shallow-repository",
        stdout_limit=128,
        budgets=selected_budgets,
    ).strip()
    if shallow_text not in {"true", "false"}:
        raise GitHistoryError(f"Git returned an invalid shallow status: {shallow_text!r}")
    shallow = shallow_text == "true"
    if resolved_after is not None and shallow:
        raise GitHistoryError(
            "incremental collection is not supported in a shallow repository; "
            "fetch complete history before advancing a cursor"
        )
    shallow_boundary = (
        _shallow_boundary(top_level, budgets=selected_budgets) if shallow else frozenset()
    )

    empty_tree = _git_text(
        top_level,
        "hash-object",
        "-t",
        "tree",
        "--stdin",
        input_bytes=b"",
        stdout_limit=1024,
        budgets=selected_budgets,
    ).strip()
    _validate_oid(empty_tree, field_name="empty-tree object ID")

    def log_arguments(log_format: str, *extra: str) -> list[str]:
        arguments = [
            "log",
            "--date-order",
            f"--max-count={effective_limit + 1}",
            f"--format={log_format}",
            "--no-decorate",
            *extra,
        ]
        if normalized_since is not None:
            arguments.append(f"--since={normalized_since}")
        arguments.extend(("--end-of-options", resolved_ref))
        if resolved_after is not None:
            arguments.append(f"^{resolved_after}")
        return arguments

    newest_first = _parse_log(
        _git_bytes(top_level, *log_arguments(_LOG_FORMAT), budgets=selected_budgets)
    )

    unique_newest_first: list[_CommitHeader] = []
    seen: set[str] = set()
    duplicate_count = 0
    for header in newest_first:
        if header.sha in seen:
            duplicate_count += 1
            continue
        seen.add(header.sha)
        unique_newest_first.append(header)

    shallow_boundary_count = 0
    if shallow_boundary:
        # A grafted boundary commit has no parent locally, so Git would report its
        # whole tree as the diff. That is not a change; it must not become a unit.
        kept_headers: list[_CommitHeader] = []
        for header in unique_newest_first:
            if header.sha in shallow_boundary:
                shallow_boundary_count += 1
                continue
            kept_headers.append(header)
        unique_newest_first = kept_headers

    commit_limit_truncated = len(unique_newest_first) > effective_limit
    retained_headers = unique_newest_first[:effective_limit]
    kinds_by_sha = {
        header.sha: "git_merge" if len(header.parents) > 1 else "git_commit"
        for header in retained_headers
    }
    reverts_by_sha = _parse_revert_log(
        _git_bytes(
            top_level,
            *log_arguments(
                _REVERT_LOG_FORMAT,
                "--extended-regexp",
                "--regexp-ignore-case",
                f"--grep={_REVERT_GREP}",
            ),
            budgets=selected_budgets,
        ),
        set(kinds_by_sha),
    )
    selected_newest_first: list[tuple[tuple[HistoricalEvent, ...], ChangeUnit]] = []
    event_log_bytes = 0
    change_unit_log_bytes = 0
    storage_truncated = False
    storage_truncation_detail = ""
    storage_byte_limit = selected_budgets.effective_storage_bytes
    storage_line_byte_limit = selected_budgets.effective_storage_line_bytes
    revert_event_count = 0
    for header in retained_headers:
        event, unit = _event_and_unit(
            header,
            repository_id=effective_repository_id,
            empty_tree=empty_tree,
        )
        commit_events = (
            event,
            *_revert_events(
                header,
                reverts_by_sha.get(header.sha, ()),
                repository_id=effective_repository_id,
                kinds_by_sha=kinds_by_sha,
            ),
        )
        event_record_bytes = [_canonical_record_bytes(item) for item in commit_events]
        unit_record_bytes = _canonical_record_bytes(unit)
        if (
            any(size > storage_line_byte_limit for size in event_record_bytes)
            or unit_record_bytes > storage_line_byte_limit
        ):
            storage_truncated = True
            storage_truncation_detail = (
                f"the next canonical record exceeds {storage_line_byte_limit} bytes"
            )
            break
        event_line_bytes = sum(size + 1 for size in event_record_bytes)
        unit_line_bytes = unit_record_bytes + 1
        if (
            event_log_bytes + event_line_bytes > storage_byte_limit
            or change_unit_log_bytes + unit_line_bytes > storage_byte_limit
        ):
            storage_truncated = True
            storage_truncation_detail = (
                f"the next record would exceed {storage_byte_limit} bytes per log"
            )
            break
        selected_newest_first.append((commit_events, unit))
        event_log_bytes += event_line_bytes
        change_unit_log_bytes += unit_line_bytes
        revert_event_count += len(commit_events) - 1

    truncated = commit_limit_truncated or storage_truncated
    if resolved_after is not None and truncated:
        reasons: list[str] = []
        if commit_limit_truncated:
            reasons.append("the commit limit")
        if storage_truncated:
            reasons.append("the canonical storage budget")
        raise GitHistoryError(
            "incremental collection after the recorded boundary is incomplete; one or more "
            f"safety limits were reached ({' and '.join(reasons)}); no partial cursor range may be "
            "persisted or used to advance the cursor"
        )
    warnings: list[str] = []
    if shallow:
        warnings.append("repository is shallow; history before the shallow boundary is unavailable")
    if shallow_boundary_count:
        warnings.append(
            f"{shallow_boundary_count} shallow boundary commit(s) were excluded: a grafted "
            "boundary has no parent locally, so its diff would be the whole tree, not a change"
        )
    if commit_limit_truncated:
        requested = "the hard safety cap" if max_commits is None else "max_commits"
        warnings.append(
            f"history was truncated by {requested} to {effective_limit} most recent commits"
        )
    if storage_truncated:
        warnings.append(
            "history was truncated by canonical storage limits to "
            f"{len(selected_newest_first)} most recent commits: {storage_truncation_detail}"
        )
    if duplicate_count:
        warnings.append(
            f"Git returned {duplicate_count} duplicate commit record(s); duplicates were ignored"
        )

    selected_oldest_first = tuple(reversed(selected_newest_first))
    events = [event for commit_events, _unit in selected_oldest_first for event in commit_events]
    units = [unit for _events, unit in selected_oldest_first]
    horizon_at: str | None = None
    if units:
        horizon_at = max(
            (unit.prediction_at for unit in units),
            key=lambda value: (parse_timestamp(value), value),
        )
        horizon = _horizon_event(
            repository_id=effective_repository_id,
            resolved_ref=resolved_ref,
            horizon_at=horizon_at,
        )
        horizon_line_bytes = _canonical_record_bytes(horizon) + 1
        if (
            horizon_line_bytes - 1 <= storage_line_byte_limit
            and event_log_bytes + horizon_line_bytes <= storage_byte_limit
        ):
            events.append(horizon)
            event_log_bytes += horizon_line_bytes
        else:
            horizon_at = None
            warnings.append(
                "history horizon event omitted because it would exceed the canonical "
                "storage budget; registered Git revert windows cannot mature from this run"
            )

    manifest: JsonObject = {
        "schema_version": 1,
        "repository_id": effective_repository_id,
        "resolved_ref": resolved_ref,
        "after": resolved_after,
        "incremental_boundary_is_ancestor": boundary_is_ancestor,
        "since": normalized_since,
        "requested_max_commits": max_commits,
        "budgets": selected_budgets.to_dict(),
        "shallow": shallow,
        "truncated": truncated,
        "storage_truncated": storage_truncated,
        "storage_byte_limit": storage_byte_limit,
        "storage_line_byte_limit": storage_line_byte_limit,
        "event_log_bytes": event_log_bytes,
        "change_unit_log_bytes": change_unit_log_bytes,
        "revert_events": revert_event_count,
        "horizon_at": horizon_at,
        "adapter": GIT_HISTORY_ADAPTER_VERSION,
        "events": [event.to_dict() for event in events],
        "change_units": [unit.to_dict() for unit in units],
    }
    return GitHistoryReport(
        events=tuple(events),
        units=tuple(units),
        examined=len(units),
        shallow=shallow,
        truncated=truncated,
        warnings=tuple(warnings),
        manifest_hash=content_hash(manifest),
        resolved_ref=resolved_ref,
        after=resolved_after,
        incremental_boundary_is_ancestor=boundary_is_ancestor,
        since=normalized_since,
        requested_max_commits=max_commits,
        budgets=selected_budgets,
        storage_truncated=storage_truncated,
        storage_byte_limit=storage_byte_limit,
        storage_line_byte_limit=storage_line_byte_limit,
        event_log_bytes=event_log_bytes,
        change_unit_log_bytes=change_unit_log_bytes,
        revert_events=revert_event_count,
        horizon_at=horizon_at,
        shallow_boundary_commits=shallow_boundary_count,
    )


def _shallow_boundary(top_level: Path, *, budgets: GitHistoryBudgets) -> frozenset[str]:
    """Return the grafted boundary commits recorded by Git for a shallow repository."""
    shallow_path_text = _git_text(
        top_level,
        "rev-parse",
        "--git-path",
        "shallow",
        stdout_limit=16 * 1024,
        budgets=budgets,
    ).strip()
    if not shallow_path_text:
        raise GitHistoryError("Git returned an empty shallow file path")
    shallow_file = Path(shallow_path_text)
    if not shallow_file.is_absolute():
        shallow_file = top_level / shallow_file
    try:
        raw = shallow_file.read_text(encoding="ascii")
    except FileNotFoundError:
        return frozenset()
    except (OSError, UnicodeDecodeError) as exc:
        raise GitHistoryError(f"shallow boundary file is unreadable: {exc}") from exc
    return frozenset(
        _validate_oid(line.strip(), field_name="shallow boundary commit")
        for line in raw.splitlines()
        if line.strip()
    )


def ingest_git_history(
    repo: Path,
    *,
    ref: str = "HEAD",
    max_commits: int | None = 1_000,
    since: str | datetime | None = None,
    after: str | None = None,
    repository_id: str | None = None,
    budgets: GitHistoryBudgets | None = None,
) -> GitHistoryReport:
    """Compatibility alias using ingestion terminology."""
    return collect_git_history(
        repo,
        ref=ref,
        max_commits=max_commits,
        since=since,
        after=after,
        repository_id=repository_id,
        budgets=budgets,
    )
