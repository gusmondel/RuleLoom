"""Pack-neutral, deterministic Git evidence collection."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from ruleloom.config import EvidenceConfig
from ruleloom.models import (
    FactEvidence,
    JsonObject,
    LabelValue,
    ModelError,
    Observation,
    validate_predicate,
    validate_subject,
)
from ruleloom.packs import ConfiguredPathsConfig, DiffEvidence, EvidencePack, FileChange, get_pack
from ruleloom.packs.base import INTERNAL_PREFIXES, PackExtraction, is_internal_path
from ruleloom.packs.flutter_testing import (
    extract_flutter_testing_facts as _extract_flutter_testing_facts,
)
from ruleloom.packs.flutter_testing_v1 import EXTRACTOR as FLUTTER_V1_EXTRACTOR
from ruleloom.packs.generic import extract_generic_change_facts as _extract_generic_change_facts

# Compatibility aliases for callers of the original 0.1 module API.
EXTRACTOR = FLUTTER_V1_EXTRACTOR
SUPPORTED_PACK = "flutter_testing"
DEFAULT_PACK = SUPPORTED_PACK
DEFAULT_EXTRACTOR = FLUTTER_V1_EXTRACTOR
LARGE_CHANGE_CHURN = 200
MULTI_FILE_COUNT = 3
_MAX_GIT_OUTPUT_BYTES = 64 * 1024 * 1024
_MAX_CONTENT_PATCH_BYTES = 64 * 1024 * 1024
_MAX_UNTRACKED_FILE_BYTES = 16 * 1024 * 1024
_MAX_UNTRACKED_TOTAL_BYTES = 64 * 1024 * 1024
_MAX_CHANGED_FILES = 100_000
_MAX_GIT_STDERR_BYTES = 4 * 1024 * 1024
_GIT_TIMEOUT_SECONDS = 30
_PATCH_PATH_BATCH = 256
_MAX_PATCH_PATHSPEC_BYTES = 128 * 1024
_MAX_CONTENT_PATCH_BATCHES = 128
_MAX_CONTENT_PATCH_SECONDS = 45.0
_MAX_COMMIT_SUBJECT_BYTES = 4096
_MAX_BACKFILL_SKIP_PREVIEW = 128
_MAX_OBJECT_PREFLIGHT_IDS = 250_000
_OBJECT_PREFLIGHT_BATCH = 10_000
_FULL_OBJECT_ID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class GitFactsError(RuntimeError):
    """Raised when Git evidence cannot be collected safely."""


class MissingPromisorObjectsError(GitFactsError):
    """Raised when exact evidence would trigger hidden partial-clone fetching."""


class _ScopeIneligibleError(GitFactsError):
    """Raised when a change cannot receive an outcome for the configured scope."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class BackfillReport:
    """Auditable result for a first-parent backfill with scope exclusions."""

    observations: tuple[Observation, ...]
    examined: int
    skipped_no_in_scope_files: int
    skipped_mixed_scope: int
    skipped_preview: tuple[tuple[str, str], ...]
    skipped_manifest_hash: str

    @property
    def eligible(self) -> int:
        return len(self.observations)

    @property
    def skipped(self) -> int:
        return self.examined - self.eligible

    def to_dict(self) -> JsonObject:
        return {
            "examined": self.examined,
            "eligible": self.eligible,
            "skipped": self.skipped,
            "skipped_by_reason": {
                "mixed_scope": self.skipped_mixed_scope,
                "no_in_scope_files": self.skipped_no_in_scope_files,
            },
            "skipped_preview": [
                {"commit": commit, "reason": reason} for commit, reason in self.skipped_preview
            ],
            "skipped_preview_truncated": self.skipped - len(self.skipped_preview),
            "skipped_manifest_hash": self.skipped_manifest_hash,
        }


@dataclass(frozen=True, slots=True)
class SnapshotRepositoryContext:
    """Repository identity and commit availability verified once for a bounded batch."""

    root: Path
    repository_id: str
    available_object_ids: frozenset[str]
    missing_object_ids: frozenset[str]


def _run_git_capped(
    repo: Path,
    arguments: tuple[str, ...],
    *,
    input_bytes: bytes | None = None,
    timeout_seconds: float = _GIT_TIMEOUT_SECONDS,
    allow_lazy_fetch: bool = True,
) -> tuple[bytes, bytes, int]:
    """Run Git with bounded wall time and incremental output caps."""
    if input_bytes is not None and len(input_bytes) > 1024 * 1024:
        raise GitFactsError("Git stdin exceeds 1048576 bytes")
    command = ["git", "-C", str(repo), *arguments]
    environment = None
    if not allow_lazy_fetch:
        environment = dict(os.environ)
        environment["GIT_NO_LAZY_FETCH"] = "1"
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
    except FileNotFoundError as exc:
        raise GitFactsError("Git is not installed or is not available on PATH") from exc
    assert process.stdout is not None
    assert process.stderr is not None
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    violation = threading.Event()
    violation_message: list[str] = []

    def drain(name: str, stream: BinaryIO, limit: int) -> None:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            buffer = buffers[name]
            if len(buffer) + len(chunk) > limit:
                violation_message.append(f"git {' '.join(arguments)} {name} exceeds {limit} bytes")
                violation.set()
                return
            buffer.extend(chunk)

    def feed(stream: BinaryIO, content: bytes) -> None:
        try:
            stream.write(content)
            stream.flush()
        except (OSError, ValueError):
            violation_message.append(f"git {' '.join(arguments)} closed stdin early")
            violation.set()
        finally:
            with suppress(OSError):
                stream.close()

    threads: list[threading.Thread] = [
        threading.Thread(
            target=drain,
            args=("stdout", process.stdout, _MAX_GIT_OUTPUT_BYTES),
            daemon=True,
        ),
        threading.Thread(
            target=drain,
            args=("stderr", process.stderr, _MAX_GIT_STDERR_BYTES),
            daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()
    if input_bytes is not None:
        assert process.stdin is not None
        writer = threading.Thread(
            target=feed,
            args=(process.stdin, input_bytes),
            daemon=True,
        )
        threads.append(writer)
        writer.start()
    deadline = time.monotonic() + timeout_seconds
    failure: str | None = None
    try:
        while process.poll() is None:
            if violation.is_set():
                failure = violation_message[0]
            elif time.monotonic() >= deadline:
                failure = f"git {' '.join(arguments)} exceeded {timeout_seconds:g} seconds"
            if failure is not None:
                process.kill()
                process.wait()
                break
            time.sleep(0.01)
        returncode = process.wait()
        for thread in threads:
            thread.join(timeout=1)
        if any(thread.is_alive() for thread in threads):
            if process.poll() is None:
                process.kill()
                process.wait()
            raise GitFactsError(f"git {' '.join(arguments)} I/O workers did not terminate")
        if failure is not None:
            raise GitFactsError(failure)
        if violation.is_set() and violation_message:
            raise GitFactsError(violation_message[0])
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        for thread in threads:
            thread.join(timeout=1)
        process.stdout.close()
        process.stderr.close()
    return bytes(buffers["stdout"]), bytes(buffers["stderr"]), returncode


def _git(
    repo: Path,
    *arguments: str,
    input_text: str | None = None,
    timeout_seconds: float = _GIT_TIMEOUT_SECONDS,
    allow_lazy_fetch: bool = True,
) -> str:
    stdout, stderr, returncode = _run_git_capped(
        repo,
        arguments,
        input_bytes=input_text.encode() if input_text is not None else None,
        timeout_seconds=timeout_seconds,
        allow_lazy_fetch=allow_lazy_fetch,
    )
    if returncode != 0:
        detail = (
            stderr.decode("utf-8", errors="replace").strip()
            or stdout.decode("utf-8", errors="replace").strip()
            or "unknown Git error"
        )
        if not allow_lazy_fetch and "promisor remote" in detail.lower():
            raise MissingPromisorObjectsError(
                "required Git blobs are absent from this partial clone and lazy fetching "
                "is disabled; hydrate the selected history in a trusted observer clone "
                "(for example with `git backfill` when supported) or use a full clone"
            )
        raise GitFactsError(f"git {' '.join(arguments)} failed: {detail}")
    try:
        return stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GitFactsError(
            "Git returned non-UTF-8 text or paths; RuleLoom refuses lossy evidence decoding"
        ) from exc


def _git_bytes(repo: Path, *arguments: str) -> bytes:
    stdout, stderr, returncode = _run_git_capped(repo, arguments)
    if returncode != 0:
        detail = stderr.decode(errors="replace").strip() or "unknown Git error"
        raise GitFactsError(f"git {' '.join(arguments)} failed: {detail}")
    return stdout


def repository_origin_url(repo: Path) -> str | None:
    """Return the configured origin URL without persisting or displaying it."""

    resolved = repo.resolve()
    if not resolved.is_dir():
        raise GitFactsError(f"repository directory does not exist: {resolved}")
    top_level = Path(_git(resolved, "rev-parse", "--show-toplevel").strip()).resolve()
    try:
        remote = _git(top_level, "config", "--get", "remote.origin.url").strip()
    except GitFactsError:
        return None
    if not remote:
        return None
    if len(remote.encode("utf-8")) > 8192 or any(character in remote for character in "\x00\r\n"):
        raise GitFactsError("remote.origin.url is invalid or exceeds 8192 bytes")
    return remote


def missing_commit_objects(
    repo: Path,
    object_ids: list[str] | tuple[str, ...],
    *,
    allow_empty_tree: bool = False,
) -> tuple[str, ...]:
    """Return full object IDs that are absent locally or are not valid snapshot bases."""

    if not isinstance(allow_empty_tree, bool):
        raise GitFactsError("allow_empty_tree must be a boolean")

    unique = tuple(sorted(set(object_ids)))
    if len(unique) > _MAX_OBJECT_PREFLIGHT_IDS:
        raise GitFactsError(
            f"Git object preflight exceeds {_MAX_OBJECT_PREFLIGHT_IDS} unique object IDs"
        )
    if any(_FULL_OBJECT_ID_RE.fullmatch(identifier) is None for identifier in unique):
        raise GitFactsError("Git object preflight requires lowercase full SHA-1/SHA-256 IDs")
    if not unique:
        return ()
    resolved = repo.resolve()
    top_level = Path(_git(resolved, "rev-parse", "--show-toplevel").strip()).resolve()
    deadline = time.monotonic() + _GIT_TIMEOUT_SECONDS
    lines: list[str] = []
    for offset in range(0, len(unique), _OBJECT_PREFLIGHT_BATCH):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GitFactsError(f"Git object preflight exceeded {_GIT_TIMEOUT_SECONDS:g} seconds")
        batch = unique[offset : offset + _OBJECT_PREFLIGHT_BATCH]
        stdout, stderr, returncode = _run_git_capped(
            top_level,
            ("cat-file", "--batch-check=%(objectname) %(objecttype)"),
            input_bytes=("\n".join(batch) + "\n").encode(),
            timeout_seconds=remaining,
            allow_lazy_fetch=False,
        )
        if returncode != 0:
            detail = stderr.decode(errors="replace").strip() or "unknown Git error"
            raise GitFactsError(f"Git object preflight failed: {detail}")
        try:
            lines.extend(stdout.decode("utf-8").splitlines())
        except UnicodeDecodeError as exc:
            raise GitFactsError("Git object preflight returned non-UTF-8 output") from exc
    if len(lines) != len(unique):
        raise GitFactsError("Git object preflight returned an incomplete response")
    accepted_empty_tree = _empty_tree(top_level) if allow_empty_tree else None
    missing: list[str] = []
    for requested, line in zip(unique, lines, strict=True):
        fields = line.split()
        if fields != [requested, "commit"] and not (
            requested == accepted_empty_tree and fields == [requested, "tree"]
        ):
            missing.append(requested)
    return tuple(missing)


def repository_identity(repo: Path) -> str:
    """Derive a non-secret, stable identifier without persisting remote URLs."""
    resolved = repo.resolve()
    if not resolved.is_dir():
        raise GitFactsError(f"repository directory does not exist: {resolved}")
    top_level = Path(_git(resolved, "rev-parse", "--show-toplevel").strip()).resolve()
    anchor: str
    remote = repository_origin_url(top_level) or ""
    if remote:
        anchor = f"remote\x00{remote}"
    else:
        try:
            roots = sorted(_git(top_level, "rev-list", "--max-parents=0", "HEAD").splitlines())
        except GitFactsError:
            roots = []
        if not roots:
            raise GitFactsError(
                "repository identity requires remote.origin.url or at least one commit; "
                "initialize RuleLoom after either exists"
            )
        anchor = "roots\x00" + "\x00".join(roots)
    return f"repo.{hashlib.sha256(anchor.encode()).hexdigest()[:20]}"


def _repository(repo: Path, repository_id: str | None = None) -> tuple[Path, str]:
    resolved = repo.resolve()
    if not resolved.is_dir():
        raise GitFactsError(f"repository directory does not exist: {resolved}")
    top_level = Path(_git(resolved, "rev-parse", "--show-toplevel").strip()).resolve()
    actual = repository_identity(top_level)
    if repository_id is not None:
        expected = validate_subject(repository_id)
        if expected != actual:
            raise GitFactsError(
                f"configured repository id {expected!r} does not match this Git repository "
                f"{actual!r}"
            )
    return top_level, actual


def prepare_snapshot_repository(
    repo: Path,
    repository_id: str,
    object_ids: list[str] | tuple[str, ...],
) -> SnapshotRepositoryContext:
    """Verify one repository boundary and all snapshot objects without lazy fetching."""

    root, actual = _repository(repo, repository_id)
    requested = frozenset(object_ids)
    missing = frozenset(missing_commit_objects(root, tuple(requested), allow_empty_tree=True))
    return SnapshotRepositoryContext(
        root=root,
        repository_id=actual,
        available_object_ids=requested.difference(missing),
        missing_object_ids=missing,
    )


def _resolve_commit(repo: Path, revision: str) -> str:
    if not revision or revision.startswith("-") or "\x00" in revision:
        raise GitFactsError(f"unsafe or empty Git revision: {revision!r}")
    return _git(repo, "rev-parse", "--verify", f"{revision}^{{commit}}").strip()


def _resolve_diff_base(repo: Path, revision: str) -> str:
    """Resolve a committed base or the repository's canonical empty tree."""
    if not revision or revision.startswith("-") or "\x00" in revision:
        raise GitFactsError(f"unsafe or empty Git revision: {revision!r}")
    empty_tree = _empty_tree(repo)
    if revision == empty_tree:
        return empty_tree
    return _resolve_commit(repo, revision)


def _parse_numstat(raw: str) -> tuple[FileChange, ...]:
    changes: list[FileChange] = []
    for record in raw.split("\x00"):
        if not record:
            continue
        fields = record.split("\t", 2)
        if len(fields) != 3:
            raise GitFactsError("Git returned malformed --numstat output")
        raw_additions, raw_deletions, path = fields
        additions = 0 if raw_additions == "-" else int(raw_additions)
        deletions = 0 if raw_deletions == "-" else int(raw_deletions)
        changes.append(
            FileChange(
                path=path,
                additions=additions,
                deletions=deletions,
            )
        )
    return tuple(sorted(changes, key=lambda item: item.path))


def _scope_pathspecs(
    config: EvidenceConfig,
    *,
    apply_exclusions: bool = True,
) -> tuple[str, ...]:
    includes = (
        (".",)
        if config.include_paths == ("**",)
        else tuple(f":(glob){pattern}" for pattern in config.include_paths)
    )
    exclusions = (
        tuple(f":(exclude,glob){pattern}" for pattern in config.exclude_paths)
        if apply_exclusions
        else ()
    )
    internal = tuple(f":(exclude,glob){prefix}**" for prefix in INTERNAL_PREFIXES)
    return (*includes, *exclusions, *internal)


def _universe_pathspecs() -> tuple[str, ...]:
    internal = tuple(f":(exclude,glob){prefix}**" for prefix in INTERNAL_PREFIXES)
    return (".", *internal)


def _internal_pathspecs() -> tuple[str, ...]:
    return tuple(f":(glob){prefix}**" for prefix in INTERNAL_PREFIXES)


def _scoped_tracked_changes(
    repo: Path,
    common: tuple[str, ...],
    config: EvidenceConfig,
) -> tuple[tuple[FileChange, ...], int, int, int]:
    """Return scoped changes and exact eligibility counts using Git pathspecs only."""

    scoped = _parse_numstat(
        _git(
            repo,
            "diff",
            "--numstat",
            "-z",
            *common,
            "--",
            *_scope_pathspecs(config),
            allow_lazy_fetch=False,
        )
    )
    if config.include_paths == ("**",) and not config.exclude_paths:
        if len(scoped) > _MAX_CHANGED_FILES:
            raise GitFactsError(f"Git diff exceeds {_MAX_CHANGED_FILES} changed files")
        return scoped, len(scoped), 0, 0
    all_changes = _parse_numstat(
        _git(
            repo,
            "diff",
            "--numstat",
            "-z",
            *common,
            "--",
            *_universe_pathspecs(),
            allow_lazy_fetch=False,
        )
    )
    included = (
        all_changes
        if config.include_paths == ("**",)
        else _parse_numstat(
            _git(
                repo,
                "diff",
                "--numstat",
                "-z",
                *common,
                "--",
                *_scope_pathspecs(config, apply_exclusions=False),
                allow_lazy_fetch=False,
            )
        )
    )
    if max(len(scoped), len(all_changes), len(included)) > _MAX_CHANGED_FILES:
        raise GitFactsError(f"Git diff exceeds {_MAX_CHANGED_FILES} changed files")
    all_paths = {change.path for change in all_changes}
    included_paths = {change.path for change in included}
    scoped_paths = {change.path for change in scoped}
    return (
        scoped,
        len(all_paths),
        len(all_paths.difference(included_paths)),
        len(included_paths.difference(scoped_paths)),
    )


def _parse_name_only(raw: str) -> tuple[str, ...]:
    paths = tuple(sorted(path for path in raw.split("\x00") if path))
    if len(paths) != len(set(paths)):
        raise GitFactsError("Git returned duplicate paths for one diff")
    if len(paths) > _MAX_CHANGED_FILES:
        raise GitFactsError(f"Git diff exceeds {_MAX_CHANGED_FILES} changed files")
    return paths


def _scoped_tracked_paths(
    repo: Path,
    common: tuple[str, ...],
    config: EvidenceConfig,
) -> tuple[tuple[str, ...], int, int, int]:
    """Return exact paths from Git trees without fetching blob contents."""

    def paths(pathspecs: tuple[str, ...]) -> tuple[str, ...]:
        return _parse_name_only(
            _git(
                repo,
                "diff",
                "--name-only",
                "-z",
                *common,
                "--",
                *pathspecs,
                allow_lazy_fetch=False,
            )
        )

    scoped = paths(_scope_pathspecs(config))
    if config.include_paths == ("**",) and not config.exclude_paths:
        return scoped, len(scoped), 0, 0
    all_paths = set(paths(_universe_pathspecs()))
    included = (
        all_paths
        if config.include_paths == ("**",)
        else set(paths(_scope_pathspecs(config, apply_exclusions=False)))
    )
    scoped_set = set(scoped)
    return (
        scoped,
        len(all_paths),
        len(all_paths.difference(included)),
        len(included.difference(scoped_set)),
    )


def _untracked_paths(repo: Path, pathspecs: tuple[str, ...]) -> tuple[str, ...]:
    raw = _git(
        repo,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        "--",
        *pathspecs,
    )
    return tuple(sorted(path for path in raw.split("\x00") if path))


def _scope_counts_eligibility(included_files: int, outside_files: int) -> None:
    if included_files == 0:
        raise _ScopeIneligibleError(
            "no_in_scope_files",
            "change has no files in the configured evidence scope",
        )
    if outside_files:
        raise _ScopeIneligibleError(
            "mixed_scope",
            "change mixes files inside and outside the configured evidence scope; "
            "widen the scope or use a component-specific change/outcome unit",
        )


def _scope_eligibility(evidence: DiffEvidence) -> None:
    _scope_counts_eligibility(len(evidence.changes), evidence.scope_outside_files)


def _content_patch(
    repo: Path,
    common: tuple[str, ...],
    paths: list[str],
) -> str:
    parts: list[str] = []
    total = 0
    started = time.monotonic()
    for batch_index, batch in enumerate(_content_path_batches(paths), start=1):
        if batch_index > _MAX_CONTENT_PATCH_BATCHES:
            raise GitFactsError(
                f"content evidence requires more than {_MAX_CONTENT_PATCH_BATCHES} Git batches"
            )
        remaining = _MAX_CONTENT_PATCH_SECONDS - (time.monotonic() - started)
        if remaining <= 0:
            raise GitFactsError(f"content evidence exceeded {_MAX_CONTENT_PATCH_SECONDS:g} seconds")
        patch = _git(
            repo,
            "diff",
            "--no-color",
            "--unified=0",
            *common,
            "--",
            *(f":(literal){path}" for path in batch),
            timeout_seconds=min(_GIT_TIMEOUT_SECONDS, remaining),
            allow_lazy_fetch=False,
        )
        total += len(patch.encode("utf-8"))
        if total > _MAX_CONTENT_PATCH_BYTES:
            raise GitFactsError(
                f"content evidence exceeds {_MAX_CONTENT_PATCH_BYTES} bytes; refusing to "
                "record partial facts"
            )
        parts.append(patch)
    return "\n".join(parts)


def _content_path_batches(paths: list[str]) -> tuple[tuple[str, ...], ...]:
    """Batch literal pathspecs under both count and conservative argv byte limits."""

    batches: list[tuple[str, ...]] = []
    current: list[str] = []
    current_bytes = 0
    for path in paths:
        pathspec_bytes = len(f":(literal){path}".encode()) + 1
        if pathspec_bytes > _MAX_PATCH_PATHSPEC_BYTES:
            raise GitFactsError("one content path exceeds the safe Git argument budget")
        if current and (
            len(current) >= _PATCH_PATH_BATCH
            or current_bytes + pathspec_bytes > _MAX_PATCH_PATHSPEC_BYTES
        ):
            batches.append(tuple(current))
            current = []
            current_bytes = 0
        current.append(path)
        current_bytes += pathspec_bytes
    if current:
        batches.append(tuple(current))
    return tuple(batches)


def _pack(
    name: str,
    version: int,
    pack_config: ConfiguredPathsConfig | None = None,
) -> EvidencePack:
    try:
        return get_pack(name, version, pack_config)
    except ModelError as exc:
        raise GitFactsError(str(exc)) from exc


def _evidence_profile(
    pack: str,
    pack_version: int,
    evidence_config: EvidenceConfig | None,
) -> EvidenceConfig:
    profile = evidence_config or EvidenceConfig()
    if pack == SUPPORTED_PACK and pack_version == 1 and profile != EvidenceConfig():
        raise GitFactsError(
            "flutter_testing@1 only supports the default evidence configuration; "
            "use flutter_testing@2 for scoped or configurable collection"
        )
    return profile


def _read_diff(
    repo: Path,
    base: str,
    head: str,
    *,
    pack: str,
    pack_version: int,
    pack_config: ConfiguredPathsConfig | None,
    evidence_config: EvidenceConfig,
) -> DiffEvidence:
    common = ("--no-ext-diff", "--no-textconv", "--no-renames", base, head)
    raw_changes, total_files, outside_files, excluded_files = _scoped_tracked_changes(
        repo,
        common,
        evidence_config,
    )
    if len(raw_changes) > _MAX_CHANGED_FILES:
        raise GitFactsError(f"Git diff exceeds {_MAX_CHANGED_FILES} changed files")
    descriptor = _pack(pack, pack_version, pack_config)
    content_paths = [change.path for change in raw_changes if descriptor.content_path(change.path)]
    changes = (
        tuple(
            FileChange(
                path=change.path.replace("\\", "/"),
                additions=change.additions,
                deletions=change.deletions,
            )
            for change in raw_changes
        )
        if pack == SUPPORTED_PACK and pack_version == 1
        else raw_changes
    )
    if not (pack == SUPPORTED_PACK and pack_version == 1):
        _scope_counts_eligibility(len(changes), outside_files)
    internal = _git(
        repo,
        "diff",
        "--name-only",
        "-z",
        *common,
        "--",
        *INTERNAL_PREFIXES,
        allow_lazy_fetch=False,
    )
    return DiffEvidence(
        changes=changes,
        content_patch=_content_patch(repo, common, content_paths),
        excluded_paths=tuple(sorted(path for path in internal.split("\x00") if path)),
        scope_total_files=total_files,
        scope_outside_files=outside_files,
        scope_excluded_files=excluded_files,
    )


def _read_aggregate_diff(
    repo: Path,
    base: str,
    head: str,
    *,
    pack: str,
    pack_version: int,
    pack_config: ConfiguredPathsConfig | None,
    evidence_config: EvidenceConfig,
    additions: int,
    deletions: int,
    files_changed: int,
    statistics_source: str,
) -> DiffEvidence:
    common = ("--no-ext-diff", "--no-textconv", "--no-renames", base, head)
    paths, total_files, outside_files, excluded_files = _scoped_tracked_paths(
        repo, common, evidence_config
    )
    _scope_counts_eligibility(len(paths), outside_files)
    if excluded_files:
        raise GitFactsError(
            "aggregate diff statistics include files excluded by the configured evidence scope"
        )
    if files_changed != total_files or files_changed != len(paths):
        raise GitFactsError(
            "aggregate changed_files does not match the exact prediction-time Git path manifest"
        )
    descriptor = _pack(pack, pack_version, pack_config)
    if any(descriptor.content_path(path) for path in paths):
        raise GitFactsError(
            f"evidence pack {pack}@{pack_version} requires content unavailable in aggregate mode"
        )
    return DiffEvidence(
        changes=tuple(FileChange(path=path, additions=0, deletions=0) for path in paths),
        excluded_paths=(),
        scope_total_files=total_files,
        scope_outside_files=outside_files,
        scope_excluded_files=excluded_files,
        aggregate_additions=additions,
        aggregate_deletions=deletions,
        aggregate_files_changed=files_changed,
        statistics_source=statistics_source,
    )


def _read_worktree_diff(
    repo: Path,
    base: str,
    *,
    pack: str,
    pack_version: int,
    pack_config: ConfiguredPathsConfig | None,
    evidence_config: EvidenceConfig,
) -> tuple[DiffEvidence, str]:
    common = ("--no-ext-diff", "--no-textconv", "--no-renames", base)
    scope = _scope_pathspecs(evidence_config)
    tracked, tracked_total, tracked_outside, tracked_excluded = _scoped_tracked_changes(
        repo,
        common,
        evidence_config,
    )
    changes = list(tracked)
    descriptor = _pack(pack, pack_version, pack_config)
    legacy_v1 = pack == SUPPORTED_PACK and pack_version == 1
    content_paths = [change.path for change in changes if descriptor.content_path(change.path)]
    content_parts = [_content_patch(repo, common, content_paths)]
    content_bytes = len(content_parts[0].encode("utf-8"))
    fingerprint = hashlib.sha256(
        _git_bytes(
            repo,
            "diff",
            "--full-index",
            "--binary",
            *common,
            "--",
            *scope,
        )
    )
    excluded_paths: list[str] = []
    if legacy_v1:
        internal = _git(
            repo,
            "diff",
            "--name-only",
            "-z",
            *common,
            "--",
            *INTERNAL_PREFIXES,
        )
        excluded_paths.extend(path for path in internal.split("\x00") if path)
        untracked_internal = _untracked_paths(repo, _internal_pathspecs())
        if len(untracked_internal) > _MAX_CHANGED_FILES:
            raise GitFactsError(f"working tree exceeds {_MAX_CHANGED_FILES} internal files")
        excluded_paths.extend(untracked_internal)
    untracked_paths = _untracked_paths(repo, scope)
    if evidence_config.include_paths == ("**",) and not evidence_config.exclude_paths:
        untracked_total_files = len(untracked_paths)
        untracked_outside = 0
        untracked_excluded = 0
    else:
        all_untracked = set(_untracked_paths(repo, _universe_pathspecs()))
        included_untracked = (
            all_untracked
            if evidence_config.include_paths == ("**",)
            else set(
                _untracked_paths(
                    repo,
                    _scope_pathspecs(evidence_config, apply_exclusions=False),
                )
            )
        )
        scoped_untracked = set(untracked_paths)
        untracked_total_files = len(all_untracked)
        untracked_outside = len(all_untracked.difference(included_untracked))
        untracked_excluded = len(included_untracked.difference(scoped_untracked))
    if tracked_total + untracked_total_files > _MAX_CHANGED_FILES:
        raise GitFactsError(f"working tree exceeds {_MAX_CHANGED_FILES} changed files")
    if not legacy_v1:
        _scope_counts_eligibility(
            len(changes) + len(untracked_paths),
            tracked_outside + untracked_outside,
        )
        fingerprint.update(
            (
                f"\x00scope:{tracked_total + untracked_total_files}:"
                f"{tracked_outside + untracked_outside}:"
                f"{tracked_excluded + untracked_excluded}"
            ).encode()
        )
    untracked_total = 0
    for raw_path in untracked_paths:
        if is_internal_path(raw_path):
            if legacy_v1:
                excluded_paths.append(raw_path)
            continue
        if len(changes) >= _MAX_CHANGED_FILES:
            raise GitFactsError(f"working tree exceeds {_MAX_CHANGED_FILES} changed files")
        unresolved = repo / raw_path
        if unresolved.is_symlink():
            raise GitFactsError(f"refusing to read untracked symlink: {raw_path!r}")
        candidate = unresolved.resolve()
        if not candidate.is_relative_to(repo) or not candidate.is_file():
            raise GitFactsError(f"unsafe untracked path returned by Git: {raw_path!r}")
        size = candidate.stat().st_size
        if size > _MAX_UNTRACKED_FILE_BYTES:
            raise GitFactsError(
                f"untracked file exceeds {_MAX_UNTRACKED_FILE_BYTES} bytes: {raw_path}"
            )
        untracked_total += size
        if untracked_total > _MAX_UNTRACKED_TOTAL_BYTES:
            raise GitFactsError(
                f"untracked files exceed {_MAX_UNTRACKED_TOTAL_BYTES} bytes in total"
            )
        try:
            payload = candidate.read_bytes()
        except OSError as exc:
            raise GitFactsError(f"cannot read untracked file {raw_path}: {exc}") from exc
        additions = payload.count(b"\n") + bool(payload and not payload.endswith(b"\n"))
        changes.append(FileChange(path=raw_path, additions=additions, deletions=0))
        fingerprint.update(raw_path.encode())
        fingerprint.update(b"\x00")
        fingerprint.update(hashlib.sha256(payload).digest())
        if descriptor.content_path(raw_path):
            try:
                decoded = payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise GitFactsError(f"content file is not valid UTF-8: {raw_path}") from exc
            rendered = "\n".join(f"+{line}" for line in decoded.splitlines())
            content_bytes += len(rendered.encode("utf-8"))
            if content_bytes > _MAX_CONTENT_PATCH_BYTES:
                raise GitFactsError(
                    f"content evidence exceeds {_MAX_CONTENT_PATCH_BYTES} bytes; refusing to "
                    "record partial facts"
                )
            content_parts.append(rendered)
    evidence_changes = (
        tuple(
            FileChange(
                path=change.path.replace("\\", "/"),
                additions=change.additions,
                deletions=change.deletions,
            )
            for change in changes
        )
        if legacy_v1
        else tuple(changes)
    )
    evidence = DiffEvidence(
        changes=tuple(sorted(evidence_changes, key=lambda item: item.path)),
        content_patch="\n".join(content_parts),
        excluded_paths=tuple(sorted(set(excluded_paths))),
        scope_total_files=tracked_total + untracked_total_files,
        scope_outside_files=tracked_outside + untracked_outside,
        scope_excluded_files=tracked_excluded + untracked_excluded,
    )
    return evidence, fingerprint.hexdigest()[:20]


def _extract(
    evidence: DiffEvidence,
    *,
    pack: str,
    pack_version: int,
    pack_config: ConfiguredPathsConfig | None,
    evidence_config: EvidenceConfig,
) -> PackExtraction:
    try:
        return _pack(pack, pack_version, pack_config).run(evidence, evidence_config.pack_options)
    except ValueError as exc:
        raise GitFactsError(str(exc)) from exc


def extract_flutter_testing_facts(
    evidence: DiffEvidence,
    evidence_config: EvidenceConfig | None = None,
) -> tuple[frozenset[str], dict[str, FactEvidence], JsonObject]:
    """Compatibility facade for the current Flutter v2 pure extractor."""

    result = _extract_flutter_testing_facts(
        evidence,
        (evidence_config or EvidenceConfig()).pack_options,
    )
    return result.facts, dict(result.provenance), result.metadata


def extract_generic_change_facts(
    evidence: DiffEvidence,
    evidence_config: EvidenceConfig | None = None,
) -> tuple[frozenset[str], dict[str, FactEvidence], JsonObject]:
    result = _extract_generic_change_facts(
        evidence,
        (evidence_config or EvidenceConfig()).pack_options,
    )
    return result.facts, dict(result.provenance), result.metadata


def _commit_metadata(
    repo: Path,
    commit: str,
    *,
    legacy_full_message: bool = False,
) -> tuple[str, str, str, bool]:
    message_format = "%B" if legacy_full_message else "%s"
    raw = _git(repo, "show", "-s", f"--format=%cI%x00{message_format}", commit)
    timestamp, separator, subject = raw.partition("\x00")
    if not separator:
        raise GitFactsError("Git returned malformed commit metadata")
    try:
        parsed = datetime.fromisoformat(timestamp.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise GitFactsError(f"Git returned an invalid commit timestamp: {timestamp!r}") from exc
    if parsed.tzinfo is None:
        raise GitFactsError("Git returned a commit timestamp without a timezone")
    subject = subject.strip()
    subject_hash = hashlib.sha256(subject.encode("utf-8")).hexdigest()
    if legacy_full_message:
        return timestamp.strip(), subject, subject_hash, False
    encoded = subject.encode("utf-8")
    truncated = len(encoded) > _MAX_COMMIT_SUBJECT_BYTES
    if truncated:
        subject = encoded[:_MAX_COMMIT_SUBJECT_BYTES].decode("utf-8", errors="ignore")
    return timestamp.strip(), subject, subject_hash, truncated


def _observation(
    repo: Path,
    repository_name: str,
    *,
    base: str,
    head: str,
    target: str,
    protocol_hash: str,
    pack: str,
    pack_version: int,
    pack_config: ConfiguredPathsConfig | None,
    evidence_config: EvidenceConfig,
    observation_id: str,
    source_kind: str,
    topological_index: int | None = None,
    diff_evidence: DiffEvidence | None = None,
    include_commit_metadata: bool = True,
    observed_at_override: str | None = None,
) -> Observation:
    validate_predicate(target, field_name="target")
    descriptor = _pack(pack, pack_version, pack_config)
    legacy_v1 = pack == SUPPORTED_PACK and pack_version == 1
    evidence = diff_evidence or _read_diff(
        repo,
        base,
        head,
        pack=pack,
        pack_version=pack_version,
        pack_config=pack_config,
        evidence_config=evidence_config,
    )
    if not legacy_v1:
        _scope_eligibility(evidence)
    result = _extract(
        evidence,
        pack=pack,
        pack_version=pack_version,
        pack_config=pack_config,
        evidence_config=evidence_config,
    )
    metadata = result.metadata
    if include_commit_metadata:
        timestamp, message, message_hash, message_truncated = _commit_metadata(
            repo,
            head,
            legacy_full_message=legacy_v1,
        )
        metadata.update({"commit_timestamp": timestamp, "commit_message": message})
        if not legacy_v1:
            metadata.update(
                {
                    "commit_message_hash": message_hash,
                    "commit_message_truncated": message_truncated,
                }
            )
        observed_at = timestamp
    else:
        metadata["commit_metadata_available"] = False
        if observed_at_override is None:
            raise GitFactsError("observed_at is required when commit metadata is omitted")
        observed_at = observed_at_override
    if not legacy_v1:
        metadata.update(
            {
                "scope_include": list(evidence_config.include_paths),
                "scope_exclude": list(evidence_config.exclude_paths),
            }
        )
    if topological_index is not None:
        metadata["topological_index"] = topological_index
    source: JsonObject = {
        "kind": source_kind,
        "repository": repository_name,
        "base": base,
        "head": head,
        "pack": pack,
        "extractor": descriptor.extractor,
    }
    if not legacy_v1:
        source["pack_version"] = pack_version
    if descriptor.configuration_hash is not None:
        source["pack_config_hash"] = descriptor.configuration_hash
    return Observation(
        id=observation_id,
        observed_at=observed_at,
        protocol_hash=protocol_hash,
        facts=result.facts,
        labels={target: LabelValue.UNKNOWN},
        fact_evidence=dict(result.provenance),
        source=source,
        metadata=metadata,
    )


def collect_snapshot(
    repo: Path,
    base: str,
    head: str = "HEAD",
    *,
    protocol_hash: str,
    target: str = "needs_extra_validation",
    pack: str = DEFAULT_PACK,
    pack_version: int = 1,
    pack_config: ConfiguredPathsConfig | None = None,
    evidence_config: EvidenceConfig | None = None,
    repository_id: str | None = None,
    include_topological_index: bool = True,
    context: SnapshotRepositoryContext | None = None,
) -> Observation:
    """Collect one immutable observation for a committed ``base``/``head`` range.

    ``include_topological_index=False`` is reserved for callers, such as the
    historical materializer, that deliberately omit the field from their final
    observation. Standalone collection preserves the existing indexed behavior.
    """

    if not isinstance(include_topological_index, bool):
        raise GitFactsError("include_topological_index must be a boolean")
    _pack(pack, pack_version, pack_config)
    extraction = _evidence_profile(pack, pack_version, evidence_config)
    if context is None:
        root, repository_name = _repository(repo, repository_id)
        base_commit = _resolve_diff_base(root, base)
        head_commit = _resolve_commit(root, head)
    else:
        if repo.resolve() != context.root or repository_id != context.repository_id:
            raise GitFactsError("snapshot repository context does not match this collection")
        missing = [
            object_id for object_id in (base, head) if object_id not in context.available_object_ids
        ]
        if missing:
            raise GitFactsError(
                "snapshot repository context lacks commit objects: " + ",".join(missing)
            )
        root = context.root
        repository_name = context.repository_id
        base_commit = base
        head_commit = head
    digest = hashlib.sha256(f"{base_commit}\x00{head_commit}".encode()).hexdigest()[:20]
    return _observation(
        root,
        repository_name,
        base=base_commit,
        head=head_commit,
        target=target,
        protocol_hash=protocol_hash,
        pack=pack,
        pack_version=pack_version,
        pack_config=pack_config,
        evidence_config=extraction,
        observation_id=f"range.{digest}",
        source_kind="git_range",
        topological_index=(
            _first_parent_position(root, head_commit) if include_topological_index else None
        ),
    )


def collect_snapshot_with_aggregate_stats(
    repo: Path,
    base: str,
    head: str,
    *,
    additions: int,
    deletions: int,
    files_changed: int,
    statistics_source: str,
    observed_at: str,
    protocol_hash: str,
    target: str = "needs_extra_validation",
    pack: str = DEFAULT_PACK,
    pack_version: int = 1,
    pack_config: ConfiguredPathsConfig | None = None,
    evidence_config: EvidenceConfig | None = None,
    repository_id: str | None = None,
    context: SnapshotRepositoryContext | None = None,
) -> Observation:
    """Collect path facts without blobs, using point-in-time aggregate churn."""

    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in (additions, deletions, files_changed)
    ):
        raise GitFactsError("aggregate diff statistics must be non-negative integers")
    if not statistics_source or any(character in statistics_source for character in "\x00\r\n"):
        raise GitFactsError("statistics_source must be a non-empty single-line string")
    _pack(pack, pack_version, pack_config)
    extraction = _evidence_profile(pack, pack_version, evidence_config)
    if context is None:
        root, repository_name = _repository(repo, repository_id)
        base_commit = _resolve_diff_base(root, base)
        head_commit = _resolve_commit(root, head)
    else:
        if repo.resolve() != context.root or repository_id != context.repository_id:
            raise GitFactsError("snapshot repository context does not match this collection")
        missing = [
            object_id for object_id in (base, head) if object_id not in context.available_object_ids
        ]
        if missing:
            raise GitFactsError(
                "snapshot repository context lacks commit objects: " + ",".join(missing)
            )
        root = context.root
        repository_name = context.repository_id
        base_commit = base
        head_commit = head
    diff_base = _git(
        root,
        "merge-base",
        base_commit,
        head_commit,
        allow_lazy_fetch=False,
    ).strip()
    if _FULL_OBJECT_ID_RE.fullmatch(diff_base) is None:
        raise GitFactsError("Git returned an invalid merge base for aggregate snapshot")
    evidence = _read_aggregate_diff(
        root,
        diff_base,
        head_commit,
        pack=pack,
        pack_version=pack_version,
        pack_config=pack_config,
        evidence_config=extraction,
        additions=additions,
        deletions=deletions,
        files_changed=files_changed,
        statistics_source=statistics_source,
    )
    digest = hashlib.sha256(f"{base_commit}\x00{head_commit}".encode()).hexdigest()[:20]
    observation = _observation(
        root,
        repository_name,
        base=diff_base,
        head=head_commit,
        target=target,
        protocol_hash=protocol_hash,
        pack=pack,
        pack_version=pack_version,
        pack_config=pack_config,
        evidence_config=extraction,
        observation_id=f"range.{digest}",
        source_kind="git_range",
        topological_index=None,
        diff_evidence=evidence,
        include_commit_metadata=False,
        observed_at_override=observed_at,
    )
    return replace(
        observation,
        source={
            **observation.source,
            "provider_base": base_commit,
            "diff_base_kind": "merge_base",
        },
    )


def collect_worktree(
    repo: Path,
    base: str = "HEAD",
    *,
    protocol_hash: str,
    target: str = "needs_extra_validation",
    pack: str = DEFAULT_PACK,
    pack_version: int = 1,
    pack_config: ConfiguredPathsConfig | None = None,
    evidence_config: EvidenceConfig | None = None,
    repository_id: str | None = None,
) -> Observation:
    """Collect staged, unstaged, and untracked changes against a committed base."""
    descriptor = _pack(pack, pack_version, pack_config)
    legacy_v1 = pack == SUPPORTED_PACK and pack_version == 1
    extraction = _evidence_profile(pack, pack_version, evidence_config)
    validate_predicate(target, field_name="target")
    root, repository_name = _repository(repo, repository_id)
    base_commit = _resolve_commit(root, base)
    diff, digest = _read_worktree_diff(
        root,
        base_commit,
        pack=pack,
        pack_version=pack_version,
        pack_config=pack_config,
        evidence_config=extraction,
    )
    if not legacy_v1:
        _scope_eligibility(diff)
    digest = hashlib.sha256(f"{base_commit}\x00{digest}".encode()).hexdigest()[:20]
    result = _extract(
        diff,
        pack=pack,
        pack_version=pack_version,
        pack_config=pack_config,
        evidence_config=extraction,
    )
    metadata = result.metadata
    observed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    metadata.update(
        {
            "snapshot_kind": "working_tree",
            "base_commit": base_commit,
            "snapshot_fingerprint": digest,
            "topological_index": _first_parent_position(root, base_commit) + 1,
        }
    )
    if not legacy_v1:
        metadata.update(
            {
                "scope_include": list(extraction.include_paths),
                "scope_exclude": list(extraction.exclude_paths),
            }
        )
    source: JsonObject = {
        "kind": "git_worktree",
        "repository": repository_name,
        "base": base_commit,
        "head": "WORKTREE",
        "pack": pack,
        "extractor": descriptor.extractor,
    }
    if not legacy_v1:
        source["pack_version"] = pack_version
    if descriptor.configuration_hash is not None:
        source["pack_config_hash"] = descriptor.configuration_hash
    return Observation(
        id=f"worktree.{digest}",
        observed_at=observed_at,
        protocol_hash=protocol_hash,
        facts=result.facts,
        labels={target: LabelValue.UNKNOWN},
        fact_evidence=dict(result.provenance),
        source=source,
        metadata=metadata,
    )


def _empty_tree(repo: Path) -> str:
    return _git(repo, "hash-object", "-t", "tree", "--stdin", input_text="").strip()


def _first_parent(repo: Path, commit: str) -> str:
    fields = _git(repo, "rev-list", "--parents", "-n", "1", commit).strip().split()
    if not fields or fields[0] != commit:
        raise GitFactsError(f"Git could not resolve parents for commit {commit}")
    return fields[1] if len(fields) > 1 else _empty_tree(repo)


def _first_parent_position(repo: Path, commit: str) -> int:
    raw = _git(repo, "rev-list", "--count", "--first-parent", commit).strip()
    try:
        position = int(raw)
    except ValueError as exc:
        raise GitFactsError(f"Git returned an invalid first-parent position: {raw!r}") from exc
    if position < 1:
        raise GitFactsError(f"Git returned an invalid first-parent position: {position}")
    return position


def backfill_commits_detailed(
    repo: Path,
    limit: int,
    *,
    protocol_hash: str,
    target: str = "needs_extra_validation",
    ref: str = "HEAD",
    pack: str = DEFAULT_PACK,
    pack_version: int = 1,
    pack_config: ConfiguredPathsConfig | None = None,
    evidence_config: EvidenceConfig | None = None,
    repository_id: str | None = None,
) -> BackfillReport:
    """Collect a first-parent backfill and retain an auditable scope denominator."""

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise GitFactsError("backfill limit must be an integer >= 1")
    _pack(pack, pack_version, pack_config)
    extraction = _evidence_profile(pack, pack_version, evidence_config)
    root, repository_name = _repository(repo, repository_id)
    resolved_ref = _resolve_commit(root, ref)
    commits = _git(
        root,
        "rev-list",
        "--first-parent",
        f"--max-count={limit}",
        resolved_ref,
    ).splitlines()
    observations: list[Observation] = []
    skipped_counts = {"no_in_scope_files": 0, "mixed_scope": 0}
    skipped_preview: list[tuple[str, str]] = []
    skipped_manifest = hashlib.sha256()
    for commit in reversed(commits):
        try:
            observation = _observation(
                root,
                repository_name,
                base=_first_parent(root, commit),
                head=commit,
                target=target,
                protocol_hash=protocol_hash,
                pack=pack,
                pack_version=pack_version,
                pack_config=pack_config,
                evidence_config=extraction,
                observation_id=f"commit.{commit}",
                source_kind="git_commit",
                topological_index=_first_parent_position(root, commit),
            )
        except _ScopeIneligibleError as exc:
            skipped_counts[exc.reason] += 1
            skipped_manifest.update(commit.encode())
            skipped_manifest.update(b"\x00")
            skipped_manifest.update(exc.reason.encode())
            skipped_manifest.update(b"\n")
            if len(skipped_preview) < _MAX_BACKFILL_SKIP_PREVIEW:
                skipped_preview.append((commit, exc.reason))
            continue
        observations.append(observation)
    return BackfillReport(
        observations=tuple(observations),
        examined=len(commits),
        skipped_no_in_scope_files=skipped_counts["no_in_scope_files"],
        skipped_mixed_scope=skipped_counts["mixed_scope"],
        skipped_preview=tuple(skipped_preview),
        skipped_manifest_hash=skipped_manifest.hexdigest(),
    )


def backfill_commits(
    repo: Path,
    limit: int,
    *,
    protocol_hash: str,
    target: str = "needs_extra_validation",
    ref: str = "HEAD",
    pack: str = DEFAULT_PACK,
    pack_version: int = 1,
    pack_config: ConfiguredPathsConfig | None = None,
    evidence_config: EvidenceConfig | None = None,
    repository_id: str | None = None,
) -> list[Observation]:
    """Collect eligible commits; use ``backfill_commits_detailed`` for skip telemetry."""

    report = backfill_commits_detailed(
        repo,
        limit,
        protocol_hash=protocol_hash,
        target=target,
        ref=ref,
        pack=pack,
        pack_version=pack_version,
        pack_config=pack_config,
        evidence_config=evidence_config,
        repository_id=repository_id,
    )
    return list(report.observations)
