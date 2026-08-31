"""Deterministic Git evidence extraction for RuleLoom's Flutter testing pack."""

from __future__ import annotations

import hashlib
import math
import re
import subprocess
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, cast

from ruleloom.models import (
    FactEvidence,
    JsonObject,
    JsonValue,
    LabelValue,
    Observation,
    validate_predicate,
    validate_subject,
)

EXTRACTOR = "ruleloom.flutter_testing.git.v1"
SUPPORTED_PACK = "flutter_testing"
LARGE_CHANGE_CHURN = 200
MULTI_FILE_COUNT = 3
_EVIDENCE_LIMIT = 12
_MAX_GIT_OUTPUT_BYTES = 64 * 1024 * 1024
_MAX_UNTRACKED_FILE_BYTES = 16 * 1024 * 1024
_MAX_UNTRACKED_TOTAL_BYTES = 64 * 1024 * 1024
_MAX_CHANGED_FILES = 100_000
_MAX_GIT_STDERR_BYTES = 4 * 1024 * 1024
_GIT_TIMEOUT_SECONDS = 30
_INTERNAL_PREFIXES = (
    ".ruleloom/",
    ".agents/skills/ruleloom/",
    ".claude/skills/ruleloom/",
)


class GitFactsError(RuntimeError):
    """Raised when Git evidence cannot be collected safely."""


@dataclass(frozen=True, slots=True)
class FileChange:
    """Line-level churn reported by Git for one path."""

    path: str
    additions: int
    deletions: int

    @property
    def churn(self) -> int:
        return self.additions + self.deletions


@dataclass(frozen=True, slots=True)
class DiffEvidence:
    """Normalized, deterministic evidence from a Git diff."""

    changes: tuple[FileChange, ...]
    dart_patch: str
    excluded_paths: tuple[str, ...] = ()


def _is_ruleloom_internal(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return any(normalized.startswith(prefix) for prefix in _INTERNAL_PREFIXES)


_CONTENT_PATTERNS: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
    "touches_widget": (
        (
            "widget superclass",
            re.compile(r"\bextends\s+(?:StatelessWidget|StatefulWidget|ConsumerWidget)\b"),
        ),
        ("widget build method", re.compile(r"\bWidget\s+build\s*\(")),
    ),
    "user_input": (
        (
            "input widget",
            re.compile(
                r"\b(?:TextField|TextFormField|Form|GestureDetector|InkWell|"
                r"ElevatedButton|TextButton|IconButton)\s*\("
            ),
        ),
        ("input callback", re.compile(r"\b(?:onTap|onPressed|onChanged|onSubmitted)\s*:")),
    ),
    "mutates_state": (
        ("setState", re.compile(r"\bsetState\s*\(")),
        ("notifier mutation", re.compile(r"\b(?:notifyListeners|emit)\s*\(")),
        ("provider state assignment", re.compile(r"\.state\s*=")),
    ),
    "uses_async": (
        ("async keyword", re.compile(r"\basync\b")),
        ("await keyword", re.compile(r"\bawait\b")),
        ("asynchronous type", re.compile(r"\b(?:Future|Stream)\s*<")),
    ),
    "navigation": (
        ("Navigator API", re.compile(r"\bNavigator\s*\.")),
        ("router API", re.compile(r"\b(?:GoRouter|AutoRouter|MaterialPageRoute)\b")),
        ("context navigation", re.compile(r"\bcontext\s*\.\s*(?:go|push|pop)\s*\(")),
    ),
    "backend_contract": (
        (
            "network or database API",
            re.compile(
                r"\b(?:Dio|GraphQLClient|FirebaseFirestore|SupabaseClient)\b|"
                r"\bhttp\s*\.\s*(?:get|post|put|patch|delete)\s*\("
            ),
        ),
        ("JSON boundary", re.compile(r"\b(?:fromJson|toJson)\s*\(")),
    ),
    "auth": (
        ("authentication provider", re.compile(r"\b(?:FirebaseAuth|OAuth|Auth0)\b", re.I)),
        (
            "authentication operation",
            re.compile(r"\b(?:signIn|signOut|logIn|logOut|login|logout|accessToken|idToken)\b"),
        ),
    ),
    "payment": (
        (
            "payment integration",
            re.compile(r"\b(?:Stripe|RevenueCat|payment|checkout|purchase|subscription)\b", re.I),
        ),
    ),
}

_PATH_PATTERNS: dict[str, re.Pattern[str]] = {
    "navigation": re.compile(r"(?:^|/)(?:routes?|router|navigation)(?:[./_]|$)", re.I),
    "backend_contract": re.compile(
        r"(?:^|/)(?:api|clients?|repositories|services?|models?)(?:/|[._])", re.I
    ),
    "auth": re.compile(r"(?:^|/)(?:auth|authentication)(?:/|[._])", re.I),
    "payment": re.compile(r"(?:^|/)(?:payments?|checkout|billing)(?:/|[._])", re.I),
}


def _run_git_capped(
    repo: Path,
    arguments: tuple[str, ...],
    *,
    input_bytes: bytes | None = None,
) -> tuple[bytes, bytes, int]:
    """Run Git with bounded wall time and incremental output caps."""
    command = ["git", "-C", str(repo), *arguments]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise GitFactsError("Git is not installed or is not available on PATH") from exc
    if input_bytes is not None:
        if len(input_bytes) > 1024 * 1024:
            process.kill()
            process.wait()
            raise GitFactsError("Git stdin exceeds 1048576 bytes")
        assert process.stdin is not None
        try:
            process.stdin.write(input_bytes)
        except BrokenPipeError as exc:
            process.wait()
            raise GitFactsError(f"git {' '.join(arguments)} closed stdin early") from exc
        finally:
            process.stdin.close()
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

    threads = (
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
    )
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + _GIT_TIMEOUT_SECONDS
    try:
        while process.poll() is None:
            if violation.is_set():
                process.kill()
                process.wait()
                raise GitFactsError(violation_message[0])
            if time.monotonic() >= deadline:
                process.kill()
                process.wait()
                raise GitFactsError(
                    f"git {' '.join(arguments)} exceeded {_GIT_TIMEOUT_SECONDS} seconds"
                )
            time.sleep(0.01)
        returncode = process.wait()
        for thread in threads:
            thread.join(timeout=1)
        if any(thread.is_alive() for thread in threads):
            process.kill()
            process.wait()
            raise GitFactsError(f"git {' '.join(arguments)} output readers did not terminate")
        if violation.is_set():
            raise GitFactsError(violation_message[0])
    finally:
        process.stdout.close()
        process.stderr.close()
    return bytes(buffers["stdout"]), bytes(buffers["stderr"]), returncode


def _git(repo: Path, *arguments: str, input_text: str | None = None) -> str:
    stdout, stderr, returncode = _run_git_capped(
        repo,
        arguments,
        input_bytes=input_text.encode() if input_text is not None else None,
    )
    if returncode != 0:
        detail = (
            stderr.decode("utf-8", errors="replace").strip()
            or stdout.decode("utf-8", errors="replace").strip()
            or "unknown Git error"
        )
        raise GitFactsError(f"git {' '.join(arguments)} failed: {detail}")
    return stdout.decode("utf-8", errors="replace")


def _git_bytes(repo: Path, *arguments: str) -> bytes:
    stdout, stderr, returncode = _run_git_capped(repo, arguments)
    if returncode != 0:
        detail = stderr.decode(errors="replace").strip() or "unknown Git error"
        raise GitFactsError(f"git {' '.join(arguments)} failed: {detail}")
    return stdout


def repository_identity(repo: Path) -> str:
    """Derive a non-secret, stable identifier without persisting remote URLs."""
    resolved = repo.resolve()
    if not resolved.is_dir():
        raise GitFactsError(f"repository directory does not exist: {resolved}")
    top_level = Path(_git(resolved, "rev-parse", "--show-toplevel").strip()).resolve()
    anchor: str
    try:
        remote = _git(top_level, "config", "--get", "remote.origin.url").strip()
    except GitFactsError:
        remote = ""
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


def _resolve_commit(repo: Path, revision: str) -> str:
    if not revision or revision.startswith("-") or "\x00" in revision:
        raise GitFactsError(f"unsafe or empty Git revision: {revision!r}")
    return _git(repo, "rev-parse", "--verify", f"{revision}^{{commit}}").strip()


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
                path=path.replace("\\", "/"),
                additions=additions,
                deletions=deletions,
            )
        )
    return tuple(sorted(changes, key=lambda item: item.path))


def _read_diff(repo: Path, base: str, head: str) -> DiffEvidence:
    common = ("--no-ext-diff", "--no-textconv", "--no-renames", base, head)
    exclusions = tuple(f":(exclude){prefix}**" for prefix in _INTERNAL_PREFIXES)
    numstat = _git(repo, "diff", "--numstat", "-z", *common, "--", ".", *exclusions)
    dart_patch = _git(
        repo,
        "diff",
        "--no-color",
        "--unified=0",
        *common,
        "--",
        "*.dart",
        *exclusions,
    )
    internal = _git(
        repo,
        "diff",
        "--name-only",
        "-z",
        *common,
        "--",
        *_INTERNAL_PREFIXES,
    )
    changes = _parse_numstat(numstat)
    if len(changes) > _MAX_CHANGED_FILES:
        raise GitFactsError(f"Git diff exceeds {_MAX_CHANGED_FILES} changed files")
    return DiffEvidence(
        changes=changes,
        dart_patch=dart_patch,
        excluded_paths=tuple(sorted(path for path in internal.split("\x00") if path)),
    )


def _read_worktree_diff(repo: Path, base: str) -> tuple[DiffEvidence, str]:
    common = ("--no-ext-diff", "--no-textconv", "--no-renames", base)
    exclusions = tuple(f":(exclude){prefix}**" for prefix in _INTERNAL_PREFIXES)
    tracked_numstat = _git(repo, "diff", "--numstat", "-z", *common, "--", ".", *exclusions)
    tracked_patch = _git(
        repo,
        "diff",
        "--no-color",
        "--unified=0",
        *common,
        "--",
        "*.dart",
        *exclusions,
    )
    changes = list(_parse_numstat(tracked_numstat))
    if len(changes) > _MAX_CHANGED_FILES:
        raise GitFactsError(f"Git diff exceeds {_MAX_CHANGED_FILES} changed files")
    dart_patch_parts = [tracked_patch]
    fingerprint = hashlib.sha256(
        _git_bytes(
            repo,
            "diff",
            "--full-index",
            "--binary",
            *common,
            "--",
            ".",
            *exclusions,
        )
    )
    internal = _git(
        repo,
        "diff",
        "--name-only",
        "-z",
        *common,
        "--",
        *_INTERNAL_PREFIXES,
    )
    excluded_paths = [path for path in internal.split("\x00") if path]
    untracked_raw = _git(repo, "ls-files", "--others", "--exclude-standard", "-z")
    untracked_paths = sorted(path for path in untracked_raw.split("\x00") if path)
    if len(changes) + len(untracked_paths) > _MAX_CHANGED_FILES:
        raise GitFactsError(f"working tree exceeds {_MAX_CHANGED_FILES} changed files")
    untracked_total = 0
    for raw_path in untracked_paths:
        if _is_ruleloom_internal(raw_path):
            excluded_paths.append(raw_path)
            continue
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
        changes.append(
            FileChange(path=raw_path.replace("\\", "/"), additions=additions, deletions=0)
        )
        fingerprint.update(raw_path.encode())
        fingerprint.update(b"\x00")
        fingerprint.update(hashlib.sha256(payload).digest())
        if raw_path.lower().endswith(".dart"):
            text = payload.decode("utf-8", errors="replace")
            dart_patch_parts.append("\n".join(f"+{line}" for line in text.splitlines()))
    evidence = DiffEvidence(
        changes=tuple(sorted(changes, key=lambda item: item.path)),
        dart_patch="\n".join(dart_patch_parts),
        excluded_paths=tuple(excluded_paths),
    )
    return evidence, fingerprint.hexdigest()[:20]


def _changed_payload(patch: str) -> tuple[str, str]:
    changed: list[str] = []
    added: list[str] = []
    for line in patch.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            payload = line[1:]
            changed.append(payload)
            added.append(payload)
        elif line.startswith("-"):
            changed.append(line[1:])
    return "\n".join(changed), "\n".join(added)


def _entropy(churn_by_file: Sequence[int]) -> tuple[float, float]:
    total = sum(churn_by_file)
    if total <= 0:
        return 0.0, 0.0
    entropy = -sum(
        (churn / total) * math.log2(churn / total) for churn in churn_by_file if churn > 0
    )
    nonzero_files = sum(churn > 0 for churn in churn_by_file)
    normalized = entropy / math.log2(nonzero_files) if nonzero_files > 1 else 0.0
    return round(entropy, 6), round(normalized, 6)


def extract_flutter_testing_facts(
    evidence: DiffEvidence,
) -> tuple[frozenset[str], dict[str, FactEvidence], JsonObject]:
    """Turn normalized diff evidence into facts, provenance, and churn metadata."""

    reasons: dict[str, set[str]] = {}

    def record(fact: str, reason: str) -> None:
        reasons.setdefault(fact, set()).add(reason)

    visible_changes = tuple(
        change for change in evidence.changes if not _is_ruleloom_internal(change.path)
    )
    internal_paths = sorted(
        {
            *evidence.excluded_paths,
            *(change.path for change in evidence.changes if _is_ruleloom_internal(change.path)),
        }
    )
    paths = [change.path for change in visible_changes]
    for path in paths:
        lowered = path.lower()
        is_dart = lowered.endswith(".dart")
        if is_dart:
            record("changes_dart", f"path:{path}")
        parts = lowered.split("/")
        if lowered.endswith("_test.dart") or "test" in parts or "integration_test" in parts:
            record("touches_test", f"path:{path}")
        if is_dart:
            for fact, pattern in _PATH_PATTERNS.items():
                if pattern.search(path):
                    record(fact, f"path:{path}")

    changed_payload, added_payload = _changed_payload(evidence.dart_patch)
    for fact, patterns in _CONTENT_PATTERNS.items():
        for marker, pattern in patterns:
            if pattern.search(changed_payload):
                record(fact, f"diff-pattern:{marker}")
    if re.search(r"\btestWidgets\s*\(", added_payload):
        record("adds_widget_test", "added-pattern:testWidgets")

    additions = sum(change.additions for change in visible_changes)
    deletions = sum(change.deletions for change in visible_changes)
    churn = additions + deletions
    files_changed = len(visible_changes)
    if churn >= LARGE_CHANGE_CHURN:
        record("large_change", f"churn:{churn}>={LARGE_CHANGE_CHURN}")
    if files_changed >= MULTI_FILE_COUNT:
        record("multi_file_change", f"files:{files_changed}>={MULTI_FILE_COUNT}")

    entropy, normalized_entropy = _entropy([change.churn for change in visible_changes])
    metadata: JsonObject = {
        "additions": additions,
        "deletions": deletions,
        "churn": churn,
        "files_changed": files_changed,
        "change_entropy": entropy,
        "normalized_change_entropy": normalized_entropy,
        "changed_files": cast(JsonValue, paths),
        "file_churn": cast(JsonValue, {change.path: change.churn for change in visible_changes}),
        "excluded_internal_files": len(internal_paths),
        "excluded_internal_paths": cast(JsonValue, internal_paths),
    }
    provenance = {
        fact: FactEvidence(
            kind="deterministic",
            extractor=EXTRACTOR,
            evidence=tuple(sorted(fact_reasons)[:_EVIDENCE_LIMIT]),
        )
        for fact, fact_reasons in reasons.items()
    }
    return frozenset(reasons), provenance, metadata


def _commit_metadata(repo: Path, commit: str) -> tuple[str, str]:
    raw = _git(repo, "show", "-s", "--format=%cI%x00%B", commit)
    timestamp, separator, message = raw.partition("\x00")
    if not separator:
        raise GitFactsError("Git returned malformed commit metadata")
    try:
        parsed = datetime.fromisoformat(timestamp.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise GitFactsError(f"Git returned an invalid commit timestamp: {timestamp!r}") from exc
    if parsed.tzinfo is None:
        raise GitFactsError("Git returned a commit timestamp without a timezone")
    return timestamp.strip(), message.strip()


def _observation(
    repo: Path,
    repository_name: str,
    *,
    base: str,
    head: str,
    target: str,
    protocol_hash: str,
    observation_id: str,
    source_kind: str,
    topological_index: int | None = None,
) -> Observation:
    validate_predicate(target, field_name="target")
    facts, fact_evidence, metadata = extract_flutter_testing_facts(_read_diff(repo, base, head))
    timestamp, message = _commit_metadata(repo, head)
    metadata.update({"commit_timestamp": timestamp, "commit_message": message})
    if topological_index is not None:
        metadata["topological_index"] = topological_index
    source: JsonObject = {
        "kind": source_kind,
        "repository": repository_name,
        "base": base,
        "head": head,
        "pack": SUPPORTED_PACK,
        "extractor": EXTRACTOR,
    }
    return Observation(
        id=observation_id,
        observed_at=timestamp,
        protocol_hash=protocol_hash,
        facts=facts,
        labels={target: LabelValue.UNKNOWN},
        fact_evidence=fact_evidence,
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
    pack: str = SUPPORTED_PACK,
    repository_id: str | None = None,
) -> Observation:
    """Collect one immutable observation for a committed ``base``/``head`` range."""

    if pack != SUPPORTED_PACK:
        raise GitFactsError(f"unsupported fact pack: {pack!r}")
    root, repository_name = _repository(repo, repository_id)
    base_commit = _resolve_commit(root, base)
    head_commit = _resolve_commit(root, head)
    digest = hashlib.sha256(f"{base_commit}\x00{head_commit}".encode()).hexdigest()[:20]
    return _observation(
        root,
        repository_name,
        base=base_commit,
        head=head_commit,
        target=target,
        protocol_hash=protocol_hash,
        observation_id=f"range.{digest}",
        source_kind="git_range",
        topological_index=_first_parent_position(root, head_commit),
    )


def collect_worktree(
    repo: Path,
    base: str = "HEAD",
    *,
    protocol_hash: str,
    target: str = "needs_extra_validation",
    pack: str = SUPPORTED_PACK,
    repository_id: str | None = None,
) -> Observation:
    """Collect staged, unstaged, and untracked changes against a committed base."""
    if pack != SUPPORTED_PACK:
        raise GitFactsError(f"unsupported fact pack: {pack!r}")
    validate_predicate(target, field_name="target")
    root, repository_name = _repository(repo, repository_id)
    base_commit = _resolve_commit(root, base)
    diff, digest = _read_worktree_diff(root, base_commit)
    digest = hashlib.sha256(f"{base_commit}\x00{digest}".encode()).hexdigest()[:20]
    facts, fact_evidence, metadata = extract_flutter_testing_facts(diff)
    observed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    metadata.update(
        {
            "snapshot_kind": "working_tree",
            "base_commit": base_commit,
            "snapshot_fingerprint": digest,
            "topological_index": _first_parent_position(root, base_commit) + 1,
        }
    )
    source: JsonObject = {
        "kind": "git_worktree",
        "repository": repository_name,
        "base": base_commit,
        "head": "WORKTREE",
        "pack": SUPPORTED_PACK,
        "extractor": EXTRACTOR,
    }
    return Observation(
        id=f"worktree.{digest}",
        observed_at=observed_at,
        protocol_hash=protocol_hash,
        facts=facts,
        labels={target: LabelValue.UNKNOWN},
        fact_evidence=fact_evidence,
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


def backfill_commits(
    repo: Path,
    limit: int,
    *,
    protocol_hash: str,
    target: str = "needs_extra_validation",
    ref: str = "HEAD",
    pack: str = SUPPORTED_PACK,
    repository_id: str | None = None,
) -> list[Observation]:
    """Collect the last ``limit`` first-parent commits in chronological order."""

    if isinstance(limit, bool) or limit < 1:
        raise GitFactsError("backfill limit must be an integer >= 1")
    if pack != SUPPORTED_PACK:
        raise GitFactsError(f"unsupported fact pack: {pack!r}")
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
    for commit in reversed(commits):
        observations.append(
            _observation(
                root,
                repository_name,
                base=_first_parent(root, commit),
                head=commit,
                target=target,
                protocol_hash=protocol_hash,
                observation_id=f"commit.{commit}",
                source_kind="git_commit",
                topological_index=_first_parent_position(root, commit),
            )
        )
    return observations
