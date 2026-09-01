"""Persistence helpers for append-friendly evidence and immutable candidates."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import subprocess
import tempfile
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from ruleloom.config import RuleLoomConfig
from ruleloom.models import (
    Candidate,
    JsonObject,
    ModelError,
    Observation,
    Prediction,
    canonical_json,
    content_hash,
    parse_timestamp,
    strict_json_loads,
    validate_json_value,
    validate_subject,
)
from ruleloom.packs import validate_policy_pack_contract

_MAX_JSON_BYTES = 8 * 1024 * 1024
_MAX_JSONL_BYTES = 64 * 1024 * 1024
_MAX_JSONL_RECORDS = 250_000
_MAX_PREDICTION_RECORDS = 10_000
_MAX_JSONL_LINE_BYTES = 1024 * 1024
_MAX_PREDICTION_RECORDING_LAG_SECONDS = 300
_MAX_MANAGED_JSON_FILES = 10_000
_MAX_MANAGED_JSON_TOTAL_BYTES = 256 * 1024 * 1024


def project_path(root: Path, relative: str | Path) -> Path:
    """Resolve a managed path without following repository-owned symlink components."""
    resolved_root = root.resolve()
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ModelError("managed paths must remain inside the initialized project")
    candidate = resolved_root / relative_path
    current = resolved_root
    for component in relative_path.parts:
        current /= component
        if current.is_symlink():
            raise ModelError(f"refusing to follow managed-path symlink: {current}")
    try:
        candidate.resolve(strict=False).relative_to(resolved_root)
    except ValueError as exc:
        raise ModelError(f"managed path escapes initialized project: {candidate}") from exc
    return candidate


@contextmanager
def _file_lock(path: Path, *, timeout_seconds: float = 10.0) -> Iterator[None]:
    """Use a crash-safe OS lock; a live owner is never evicted by wall-clock age."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    deadline = time.monotonic() + timeout_seconds
    flags = os.O_CREAT | os.O_RDWR
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise ModelError(f"cannot safely open storage lock {lock_path}: {exc}") from exc
    lock_stat = os.fstat(descriptor)
    if not stat.S_ISREG(lock_stat.st_mode):
        os.close(descriptor)
        raise ModelError(f"storage lock must be a regular file: {lock_path}")
    if lock_stat.st_uid != os.getuid():
        os.close(descriptor)
        raise ModelError(f"storage lock must be owned by the current user: {lock_path}")
    if stat.S_IMODE(lock_stat.st_mode) & 0o077:
        os.close(descriptor)
        raise ModelError(f"storage lock permissions are too broad: {lock_path}")
    acquired = False
    while not acquired:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(descriptor)
                raise ModelError(f"timed out waiting for storage lock: {lock_path}") from None
            time.sleep(0.05)
    try:
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, f"pid={os.getpid()}\n".encode())
        os.fsync(descriptor)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        temp_path.replace(path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def write_json(path: Path, value: JsonObject) -> None:
    validate_json_value(value)
    content = (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    )
    if len(content.encode("utf-8")) > _MAX_JSON_BYTES:
        raise ModelError(f"JSON artifact exceeds {_MAX_JSON_BYTES} bytes: {path}")
    _atomic_write(path, content)


def write_text(path: Path, content: str) -> None:
    _atomic_write(path, content)


def _validate_jsonl_content(path: Path, content: str, kind: str) -> None:
    if len(content.encode("utf-8")) > _MAX_JSONL_BYTES:
        raise ModelError(f"{kind} log exceeds {_MAX_JSONL_BYTES} bytes: {path}")
    lines = _jsonl_lines(content)
    if len(lines) > _MAX_JSONL_RECORDS:
        raise ModelError(f"{kind} log exceeds {_MAX_JSONL_RECORDS} records: {path}")
    for line_number, line in enumerate(lines, 1):
        if len(line.encode("utf-8")) > _MAX_JSONL_LINE_BYTES:
            raise ModelError(f"{kind} record is too large at {path}:{line_number}")


def _jsonl_lines(content: str) -> list[str]:
    """Split JSON Lines only on its ASCII record delimiter.

    ``str.splitlines`` also treats valid JSON characters such as U+0085, U+2028,
    and U+2029 as separators, which can corrupt otherwise canonical records.
    """

    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def read_json(path: Path) -> JsonObject:
    try:
        if path.stat().st_size > _MAX_JSON_BYTES:
            raise ModelError(f"JSON file exceeds {_MAX_JSON_BYTES} bytes: {path}")
        value = strict_json_loads(path.read_text(encoding="utf-8"), str(path))
    except FileNotFoundError as exc:
        raise ModelError(f"file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ModelError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ModelError(f"expected a JSON object in {path}")
    return value


def load_observations(path: Path) -> list[Observation]:
    if not path.exists():
        return []
    if path.stat().st_size > _MAX_JSONL_BYTES:
        raise ModelError(f"observation log exceeds {_MAX_JSONL_BYTES} bytes: {path}")
    observations: list[Observation] = []
    seen: set[str] = set()
    for line_number, line in enumerate(_jsonl_lines(path.read_text(encoding="utf-8")), 1):
        if line_number > _MAX_JSONL_RECORDS:
            raise ModelError(f"observation log exceeds {_MAX_JSONL_RECORDS} records: {path}")
        if len(line.encode("utf-8")) > _MAX_JSONL_LINE_BYTES:
            raise ModelError(f"observation record is too large at {path}:{line_number}")
        if not line.strip():
            continue
        try:
            raw = strict_json_loads(line, f"{path}:{line_number}")
        except json.JSONDecodeError as exc:
            raise ModelError(f"invalid JSON at {path}:{line_number}: {exc}") from exc
        if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
            raise ModelError(f"expected an object at {path}:{line_number}")
        observation = Observation.from_dict(raw)
        if observation.id in seen:
            raise ModelError(f"duplicate observation id {observation.id!r} in {path}")
        seen.add(observation.id)
        observations.append(observation)
    return observations


def _save_observations_unlocked(path: Path, observations: Iterable[Observation]) -> None:
    ordered = list(observations)
    topology = [
        (item.source.get("repository"), item.metadata.get("topological_index")) for item in ordered
    ]
    if (
        ordered
        and all(
            isinstance(repository, str)
            and isinstance(position, int)
            and not isinstance(position, bool)
            and position >= 1
            for repository, position in topology
        )
        and len({repository for repository, _ in topology}) == 1
    ):
        ordered.sort(key=lambda item: (cast(int, item.metadata["topological_index"]), item.id))
    else:
        ordered.sort(key=lambda item: (parse_timestamp(item.observed_at), item.id))
    ids = [item.id for item in ordered]
    if len(ids) != len(set(ids)):
        raise ModelError("cannot persist duplicate observation ids")
    content = "".join(canonical_json(item.to_dict()) + "\n" for item in ordered)
    _validate_jsonl_content(path, content, "observation")
    _atomic_write(path, content)


def save_observations(path: Path, observations: Iterable[Observation]) -> None:
    with _file_lock(path):
        _save_observations_unlocked(path, observations)


@contextmanager
def edit_observations(path: Path) -> Iterator[list[Observation]]:
    """Lock, load, and atomically persist an in-place observation edit."""
    with _file_lock(path):
        observations = load_observations(path)
        yield observations
        _save_observations_unlocked(path, observations)


def upsert_observations(path: Path, incoming: Iterable[Observation]) -> tuple[int, int]:
    with _file_lock(path):
        existing = {item.id: item for item in load_observations(path)}
        inserted = 0
        updated = 0
        for observation in incoming:
            if observation.id in existing:
                updated += existing[observation.id] != observation
            else:
                inserted += 1
            existing[observation.id] = observation
        _save_observations_unlocked(path, existing.values())
    return inserted, updated


def dataset_path(root: Path, config: RuleLoomConfig) -> Path:
    return project_path(root, config.dataset)


def predictions_path(root: Path, config: RuleLoomConfig) -> Path:
    return project_path(root, config.predictions)


def candidate_path(root: Path, config: RuleLoomConfig, candidate_id: str) -> Path:
    validate_subject(candidate_id)
    return project_path(root, Path(config.candidates_dir) / f"{candidate_id}.json")


def approved_path(root: Path, config: RuleLoomConfig, candidate_id: str) -> Path:
    validate_subject(candidate_id)
    return project_path(root, Path(config.approved_dir) / f"{candidate_id}.json")


def shadow_path(root: Path, config: RuleLoomConfig, candidate_id: str) -> Path:
    validate_subject(candidate_id)
    return project_path(root, Path(config.shadow_dir) / f"{candidate_id}.json")


def deprecated_path(root: Path, config: RuleLoomConfig, candidate_id: str) -> Path:
    validate_subject(candidate_id)
    return project_path(root, Path(config.deprecated_dir) / f"{candidate_id}.json")


def save_candidate(path: Path, candidate: Candidate) -> None:
    candidate.validate_identity()
    with _file_lock(path):
        if path.exists():
            persisted = Candidate.from_dict(read_json(path))
            if persisted != candidate:
                raise ModelError(f"refusing to overwrite immutable candidate: {path}")
            return
        write_json(path, candidate.to_dict())


def _git_dir(root: Path) -> Path:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root.resolve()), "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise ModelError(f"trusted review state requires Git: {exc}") from exc
    raw = completed.stdout.strip()
    if completed.returncode != 0 or not raw:
        raise ModelError(
            "trusted review state requires an initialized Git repository; "
            "RuleLoom will not trust approval fields from repository files alone"
        )
    path = Path(raw)
    if not path.is_absolute():
        path = root.resolve() / path
    return path.resolve()


def _attestation_path(root: Path, candidate: Candidate, status: str) -> Path:
    validate_subject(candidate.id)
    if len(candidate.config_hash) != 64 or any(
        character not in "0123456789abcdef" for character in candidate.config_hash
    ):
        raise ModelError("candidate config hash is not a safe attestation namespace")
    if status not in {"shadow", "approved", "deprecated"}:
        raise ModelError(f"unsupported trusted transition status: {status!r}")
    return (
        _trusted_project_dir(root)
        / "transition-records"
        / candidate.config_hash
        / status
        / f"{candidate.id}.json"
    )


def _trusted_project_dir(root: Path) -> Path:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root.resolve()), "rev-parse", "--show-prefix"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise ModelError(f"trusted review state requires Git: {exc}") from exc
    if completed.returncode != 0:
        raise ModelError("trusted review state cannot determine the project Git prefix")
    project_key = content_hash(completed.stdout.strip() or ".")[:16]
    return _git_dir(root) / "ruleloom" / "projects" / project_key


def trusted_state_path(root: Path) -> Path:
    """Return the worktree-local trust directory stored outside versioned files."""
    return _trusted_project_dir(root)


def _local_attestation_ids(root: Path, *parts: str) -> set[str]:
    try:
        directory = _trusted_project_dir(root).joinpath(*parts)
    except ModelError:
        return set()
    if not directory.exists():
        return set()
    if not directory.is_dir():
        raise ModelError(f"trusted state path is not a directory: {directory}")
    identifiers: set[str] = set()
    total_bytes = 0
    with os.scandir(directory) as entries:
        for entry in entries:
            if not entry.name.endswith(".json"):
                continue
            if len(identifiers) >= _MAX_MANAGED_JSON_FILES:
                raise ModelError(f"trusted state exceeds {_MAX_MANAGED_JSON_FILES} JSON artifacts")
            path = Path(entry.path)
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                raise ModelError(f"trusted state artifact is not a regular file: {path}")
            total_bytes += entry.stat(follow_symlinks=False).st_size
            if total_bytes > _MAX_MANAGED_JSON_TOTAL_BYTES:
                raise ModelError(
                    f"trusted state JSON artifacts exceed {_MAX_MANAGED_JSON_TOTAL_BYTES} bytes"
                )
            validate_subject(path.stem)
            identifiers.add(path.stem)
    return identifiers


def record_transition_attestation(
    root: Path,
    candidate: Candidate,
    *,
    reviewer: str | None = None,
    note: str | None = None,
    trusted_at: str | None = None,
) -> Path:
    """Record a local, non-versioned attestation for a reviewed transition."""
    trusted_by = reviewer if reviewer is not None else candidate.review.get("reviewer")
    trust_note = note if note is not None else candidate.review.get("note", "")
    trust_time = trusted_at if trusted_at is not None else candidate.review.get("reviewed_at")
    if (
        not isinstance(trusted_by, str)
        or not trusted_by.strip()
        or not isinstance(trust_note, str)
        or not isinstance(trust_time, str)
    ):
        raise ModelError("trusted transition requires reviewer, note, and timestamp strings")
    parse_timestamp(trust_time)
    path = _attestation_path(root, candidate, candidate.status)
    value: JsonObject = {
        "schema_version": 1,
        "candidate_id": candidate.id,
        "status": candidate.status,
        "artifact_hash": content_hash(candidate.to_dict()),
        "trusted_at": trust_time,
        "trusted_by": trusted_by.strip(),
        "note": trust_note,
    }
    if path.exists():
        if read_json(path) != value:
            raise ModelError(f"refusing to overwrite trusted transition attestation: {path}")
        return path
    write_json(path, value)
    return path


def _verify_transition_attestation(root: Path, candidate: Candidate, status: str) -> None:
    path = _attestation_path(root, candidate, status)
    try:
        value = read_json(path)
    except ModelError as exc:
        raise ModelError(
            f"active {status} policy {candidate.id} lacks a trusted local transition "
            "attestation; review and promote it in this clone"
        ) from exc
    expected_fields = {
        "schema_version",
        "candidate_id",
        "status",
        "artifact_hash",
        "trusted_at",
        "trusted_by",
        "note",
    }
    trusted_at = value.get("trusted_at")
    trusted_by = value.get("trusted_by")
    note = value.get("note")
    if (
        set(value) != expected_fields
        or value.get("schema_version") != 1
        or value.get("candidate_id") != candidate.id
        or value.get("status") != status
        or value.get("artifact_hash") != content_hash(candidate.to_dict())
        or not isinstance(trusted_at, str)
        or not isinstance(trusted_by, str)
        or not trusted_by.strip()
        or not isinstance(note, str)
    ):
        raise ModelError(
            f"active {status} policy {candidate.id} does not match its trusted local attestation"
        )
    parse_timestamp(trusted_at)


def load_candidate(path: Path) -> Candidate:
    candidate = Candidate.from_dict(read_json(path))
    candidate.validate_identity()
    return candidate


def _validate_manual_candidate_runtime(
    candidate: Candidate,
    config: RuleLoomConfig,
) -> None:
    """Import lazily to preserve ``manual_rules -> storage`` low-level IO use."""

    from ruleloom.manual_rules import validate_manual_candidate

    validate_manual_candidate(candidate, config)


def _claims_manual_provenance(candidate: Candidate) -> bool:
    return (
        candidate.metadata.get("candidate_origin") == "manual_declaration"
        or "manual_declaration" in candidate.metadata
        or "manual_audit" in candidate.metadata
    )


def load_approved(root: Path, config: RuleLoomConfig) -> list[Candidate]:
    deprecated = _deprecated_ids(root, config)
    candidates: list[Candidate] = []
    paths = _managed_json_paths(root, config.approved_dir)
    artifact_ids = {path.stem for path in paths}
    attested_ids = _local_attestation_ids(root, "transition-records", config.hash, "approved")
    if artifact_ids != attested_ids:
        raise ModelError(
            "approved artifacts and trusted local attestations differ; missing artifacts: "
            + ", ".join(sorted(attested_ids - artifact_ids))
            + "; untrusted artifacts: "
            + ", ".join(sorted(artifact_ids - attested_ids))
        )
    for path in paths:
        candidate = load_candidate(path)
        _validate_active(candidate, config, "approved", expected_id=path.stem)
        _verify_transition_attestation(root, candidate, "approved")
        if path.stem in deprecated:
            continue
        candidates.append(candidate)
    return candidates


def load_shadow(root: Path, config: RuleLoomConfig) -> list[Candidate]:
    deprecated = _deprecated_ids(root, config)
    candidates: list[Candidate] = []
    paths = _managed_json_paths(root, config.shadow_dir)
    artifact_ids = {path.stem for path in paths}
    attested_ids = _local_attestation_ids(root, "transition-records", config.hash, "shadow")
    if artifact_ids != attested_ids:
        raise ModelError(
            "shadow artifacts and trusted local attestations differ; missing artifacts: "
            + ", ".join(sorted(attested_ids - artifact_ids))
            + "; untrusted artifacts: "
            + ", ".join(sorted(artifact_ids - attested_ids))
        )
    for path in paths:
        candidate = load_candidate(path)
        _validate_active(candidate, config, "shadow", expected_id=path.stem)
        _verify_transition_attestation(root, candidate, "shadow")
        if path.stem in deprecated:
            continue
        candidates.append(candidate)
    return candidates


def _deprecated_ids(root: Path, config: RuleLoomConfig) -> set[str]:
    paths = _managed_json_paths(root, config.deprecated_dir)
    artifact_ids = {path.stem for path in paths}
    attested_ids = _local_attestation_ids(root, "transition-records", config.hash, "deprecated")
    if artifact_ids != attested_ids:
        raise ModelError(
            "deprecated artifacts and trusted local attestations differ; missing artifacts: "
            + ", ".join(sorted(attested_ids - artifact_ids))
            + "; untrusted artifacts: "
            + ", ".join(sorted(artifact_ids - attested_ids))
        )
    identifiers: set[str] = set()
    for path in paths:
        candidate = load_candidate(path)
        _validate_deprecated(candidate, config, expected_id=path.stem, path=path)
        _verify_transition_attestation(root, candidate, "deprecated")
        identifiers.add(candidate.id)
    return identifiers


def _validate_deprecated(
    candidate: Candidate,
    config: RuleLoomConfig,
    *,
    expected_id: str,
    path: Path,
) -> None:
    if candidate.id != expected_id or candidate.status != "deprecated":
        raise ModelError(f"invalid deprecation tombstone identity or status: {path}")
    if candidate.config_hash != config.hash:
        raise ModelError(f"deprecation tombstone belongs to a different configuration: {path}")
    if candidate.rules.target != config.target:
        raise ModelError(f"deprecation tombstone has incompatible learner provenance: {path}")
    if candidate.engine == "manual":
        _validate_manual_candidate_runtime(candidate, config)
    elif candidate.engine != config.learner.engine:
        raise ModelError(f"deprecation tombstone has incompatible learner provenance: {path}")
    elif _claims_manual_provenance(candidate):
        raise ModelError(f"deprecation tombstone has incompatible manual provenance: {path}")
    if (
        candidate.metadata.get("pack") != config.pack
        or candidate.metadata.get("repository_id") != config.protocol.repository_id
    ):
        raise ModelError(f"deprecation tombstone has incompatible repository provenance: {path}")
    review = candidate.review.get("deprecation")
    if not isinstance(review, dict):
        raise ModelError(f"deprecation tombstone lacks review provenance: {path}")
    reviewer = review.get("reviewer")
    note = review.get("note")
    reviewed_at = review.get("reviewed_at")
    if (
        not isinstance(reviewer, str)
        or not reviewer.strip()
        or not isinstance(note, str)
        or not note.strip()
        or not isinstance(reviewed_at, str)
    ):
        raise ModelError(f"deprecation tombstone has invalid review provenance: {path}")
    parse_timestamp(reviewed_at)


def load_reviewed_artifact_untrusted(
    root: Path,
    config: RuleLoomConfig,
    candidate_id: str,
    status: str,
) -> Candidate:
    """Load a reviewed artifact without local trust, solely for explicit re-attestation."""
    if status == "shadow":
        path = shadow_path(root, config, candidate_id)
    elif status == "approved":
        path = approved_path(root, config, candidate_id)
    elif status == "deprecated":
        path = deprecated_path(root, config, candidate_id)
    else:
        raise ModelError(f"unsupported reviewed artifact status: {status!r}")
    candidate = load_candidate(path)
    if status == "deprecated":
        _validate_deprecated(candidate, config, expected_id=candidate_id, path=path)
    else:
        _validate_active(candidate, config, status, expected_id=candidate_id)
    return candidate


def _managed_json_paths(root: Path, relative_directory: str) -> list[Path]:
    """List regular managed JSON files and reject final-component symlinks."""
    directory = project_path(root, relative_directory)
    if not directory.exists():
        return []
    if not directory.is_dir():
        raise ModelError(f"managed candidate path is not a directory: {directory}")
    paths: list[Path] = []
    total_bytes = 0
    with os.scandir(directory) as entries:
        for entry in entries:
            if not entry.name.endswith(".json"):
                continue
            if len(paths) >= _MAX_MANAGED_JSON_FILES:
                raise ModelError(
                    f"managed directory exceeds {_MAX_MANAGED_JSON_FILES} JSON artifacts: "
                    f"{directory}"
                )
            path = project_path(root, Path(relative_directory) / entry.name)
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                raise ModelError(f"managed candidate artifact is not a regular file: {path}")
            total_bytes += entry.stat(follow_symlinks=False).st_size
            if total_bytes > _MAX_MANAGED_JSON_TOTAL_BYTES:
                raise ModelError(
                    f"managed JSON artifacts exceed {_MAX_MANAGED_JSON_TOTAL_BYTES} bytes: "
                    f"{directory}"
                )
            paths.append(path)
    return sorted(paths)


def load_candidates(root: Path, config: RuleLoomConfig) -> list[Candidate]:
    """Load content-addressed candidate manifests from the managed directory."""
    candidates: list[Candidate] = []
    for path in _managed_json_paths(root, config.candidates_dir):
        candidate = load_candidate(path)
        if candidate.id != path.stem or candidate.status != "candidate":
            raise ModelError(f"invalid candidate identity or status: {path}")
        candidates.append(candidate)
    return candidates


def _validate_active(
    candidate: Candidate,
    config: RuleLoomConfig,
    status: str,
    *,
    expected_id: str,
) -> None:
    if candidate.id != expected_id:
        raise ModelError(
            f"active policy filename {expected_id!r} does not match id {candidate.id!r}"
        )
    if candidate.status != status:
        raise ModelError(
            f"active {status} policy {candidate.id} has persisted status {candidate.status!r}"
        )
    if candidate.rules.target != config.target:
        raise ModelError(
            f"active policy {candidate.id} targets {candidate.rules.target!r}, "
            f"not configured target {config.target!r}"
        )
    if candidate.config_hash != config.hash:
        raise ModelError(
            f"active policy {candidate.id} does not match the current configuration hash"
        )
    manual = candidate.engine == "manual"
    if manual:
        _validate_manual_candidate_runtime(candidate, config)
    elif _claims_manual_provenance(candidate):
        raise ModelError(f"active policy {candidate.id} has incompatible manual provenance")
    elif candidate.engine != config.learner.engine:
        raise ModelError(
            f"active policy {candidate.id} uses engine {candidate.engine!r}, not "
            f"{config.learner.engine!r}"
        )
    descriptor = config.resolved_pack
    validate_policy_pack_contract(
        descriptor,
        candidate.metadata,
        {literal.predicate for clause in candidate.rules.clauses for literal in clause.body},
        schema_version=config.schema_version,
        evidence_protocol_hash=config.evidence_protocol_hash,
        subject=f"active policy {candidate.id}",
    )
    if candidate.metadata.get("repository_id") != config.protocol.repository_id:
        raise ModelError(f"active policy {candidate.id} belongs to a different repository")
    reviewer = candidate.review.get("reviewer")
    reviewed_at = candidate.review.get("reviewed_at")
    note = candidate.review.get("note")
    override = candidate.review.get("override")
    unmet_gates = candidate.review.get("unmet_gates")
    if (
        not isinstance(reviewer, str)
        or not reviewer.strip()
        or not isinstance(reviewed_at, str)
        or not isinstance(note, str)
        or not isinstance(override, bool)
        or not isinstance(unmet_gates, list)
        or not all(isinstance(item, str) for item in unmet_gates)
        or (override and not note.strip())
    ):
        raise ModelError(f"active policy {candidate.id} lacks valid human review provenance")
    parse_timestamp(reviewed_at)


def _prediction_ledger_key(root: Path, path: Path, evidence_protocol_hash: str) -> str:
    if len(evidence_protocol_hash) != 64:
        raise ModelError("prediction evidence protocol hash must contain 64 characters")
    return content_hash(
        {
            "evidence_protocol_hash": evidence_protocol_hash,
            "log_path": _prediction_log_relative(root, path),
        }
    )[:24]


def _prediction_attestation_path(root: Path, ledger_key: str, prediction_id: str) -> Path:
    validate_subject(ledger_key)
    validate_subject(prediction_id)
    return (
        _trusted_project_dir(root)
        / "prediction-ledgers"
        / ledger_key
        / "records"
        / f"{prediction_id}.json"
    )


def _prediction_transaction_path(root: Path, ledger_key: str) -> Path:
    validate_subject(ledger_key)
    return _trusted_project_dir(root) / "prediction-ledgers" / ledger_key / "transaction.json"


def _prediction_attestation_value(
    prediction: Prediction,
    *,
    sequence: int,
    previous: str,
    recorded_at: datetime | None = None,
) -> JsonObject:
    instant = recorded_at or datetime.now(UTC)
    if instant.tzinfo is None:
        raise ModelError("prediction recorded_at must include a timezone")
    predicted_at = parse_timestamp(prediction.predicted_at)
    lag = (instant - predicted_at).total_seconds()
    if lag < 0 or lag > _MAX_PREDICTION_RECORDING_LAG_SECONDS:
        raise ModelError(
            "prediction must be recorded between 0 and "
            f"{_MAX_PREDICTION_RECORDING_LAG_SECONDS} seconds after predicted_at"
        )
    if isinstance(sequence, bool) or sequence < 1:
        raise ModelError("prediction attestation sequence must be an integer >= 1")
    if len(previous) != 64:
        raise ModelError("prediction attestation previous hash must contain 64 characters")
    artifact_hash = content_hash(prediction.to_dict())
    chain_head = content_hash(
        {
            "previous": previous,
            "sequence": sequence,
            "prediction_id": prediction.id,
            "artifact_hash": artifact_hash,
        }
    )
    return {
        "schema_version": 1,
        "prediction_id": prediction.id,
        "artifact_hash": artifact_hash,
        "predicted_at": prediction.predicted_at,
        "recorded_at": instant.isoformat().replace("+00:00", "Z"),
        "sequence": sequence,
        "previous": previous,
        "chain_head": chain_head,
    }


def record_prediction_attestation(
    root: Path,
    prediction: Prediction,
    *,
    ledger_key: str,
    sequence: int,
    previous: str,
    recorded_at: datetime | None = None,
) -> Path:
    """Bind one prediction to the local wall-clock time at which it was appended."""
    path = _prediction_attestation_path(root, ledger_key, prediction.id)
    value = _prediction_attestation_value(
        prediction,
        sequence=sequence,
        previous=previous,
        recorded_at=recorded_at,
    )
    if path.exists():
        persisted = read_json(path)
        if persisted != value:
            raise ModelError(f"trusted prediction attestation conflicts with {prediction.id}")
        return path
    write_json(path, value)
    return path


def _verify_prediction_attestation(
    root: Path,
    prediction: Prediction,
    *,
    ledger_key: str,
    sequence: int,
    previous: str,
) -> str:
    path = _prediction_attestation_path(root, ledger_key, prediction.id)
    try:
        value = read_json(path)
    except ModelError as exc:
        raise ModelError(
            f"prediction {prediction.id} lacks a trusted local recording attestation"
        ) from exc
    recorded_at = value.get("recorded_at")
    if (
        set(value)
        != {
            "schema_version",
            "prediction_id",
            "artifact_hash",
            "predicted_at",
            "recorded_at",
            "sequence",
            "previous",
            "chain_head",
        }
        or value.get("schema_version") != 1
        or value.get("prediction_id") != prediction.id
        or value.get("artifact_hash") != content_hash(prediction.to_dict())
        or value.get("predicted_at") != prediction.predicted_at
        or value.get("sequence") != sequence
        or value.get("previous") != previous
        or not isinstance(recorded_at, str)
    ):
        raise ModelError(f"prediction {prediction.id} does not match its trusted local attestation")
    lag = (parse_timestamp(recorded_at) - parse_timestamp(prediction.predicted_at)).total_seconds()
    if lag < 0 or lag > _MAX_PREDICTION_RECORDING_LAG_SECONDS:
        raise ModelError(f"prediction {prediction.id} has an invalid trusted recording time")
    expected_head = content_hash(
        {
            "previous": previous,
            "sequence": sequence,
            "prediction_id": prediction.id,
            "artifact_hash": content_hash(prediction.to_dict()),
        }
    )
    if value.get("chain_head") != expected_head:
        raise ModelError(f"prediction {prediction.id} breaks the trusted append chain")
    return expected_head


def load_predictions(path: Path) -> list[Prediction]:
    if not path.exists():
        return []
    if path.stat().st_size > _MAX_JSONL_BYTES:
        raise ModelError(f"prediction log exceeds {_MAX_JSONL_BYTES} bytes: {path}")
    predictions: list[Prediction] = []
    seen: set[str] = set()
    for line_number, line in enumerate(_jsonl_lines(path.read_text(encoding="utf-8")), 1):
        if line_number > _MAX_PREDICTION_RECORDS:
            raise ModelError(f"prediction log exceeds {_MAX_PREDICTION_RECORDS} records: {path}")
        if len(line.encode("utf-8")) > _MAX_JSONL_LINE_BYTES:
            raise ModelError(f"prediction record is too large at {path}:{line_number}")
        if not line.strip():
            continue
        try:
            raw = strict_json_loads(line, f"{path}:{line_number}")
        except json.JSONDecodeError as exc:
            raise ModelError(f"invalid JSON at {path}:{line_number}: {exc}") from exc
        if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
            raise ModelError(f"expected an object at {path}:{line_number}")
        prediction = Prediction.from_dict(raw)
        prediction.validate_identity()
        if prediction.id in seen:
            raise ModelError(f"duplicate prediction id {prediction.id!r} in {path}")
        seen.add(prediction.id)
        predictions.append(prediction)
    return predictions


def load_trusted_predictions(root: Path, config: RuleLoomConfig) -> list[Prediction]:
    """Load only predictions whose original local append time is independently attested."""
    path = predictions_path(root, config)
    ledger_key = _prediction_ledger_key(root, path, config.evidence_protocol_hash)
    with _file_lock(path):
        _recover_prediction_transaction(root, path, ledger_key)
        predictions = load_predictions(path)
        _verify_prediction_prefix(root, predictions, ledger_key=ledger_key)
        return predictions


def _verify_prediction_prefix(
    root: Path,
    predictions: list[Prediction],
    *,
    ledger_key: str,
    pending_id: str | None = None,
) -> str:
    artifact_ids = {prediction.id for prediction in predictions}
    attested_ids = _local_attestation_ids(root, "prediction-ledgers", ledger_key, "records")
    allowed_attestations = artifact_ids | ({pending_id} if pending_id is not None else set())
    if not artifact_ids.issubset(attested_ids) or not attested_ids.issubset(allowed_attestations):
        raise ModelError(
            "prediction log and trusted local records differ; missing log records: "
            + ", ".join(sorted(attested_ids - artifact_ids))
            + "; untrusted log records: "
            + ", ".join(sorted(artifact_ids - attested_ids))
        )
    previous = "0" * 64
    for sequence, prediction in enumerate(predictions, 1):
        previous = _verify_prediction_attestation(
            root,
            prediction,
            ledger_key=ledger_key,
            sequence=sequence,
            previous=previous,
        )
    return previous


def _prediction_log_relative(root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError as exc:
        raise ModelError("prediction log must remain inside the initialized project") from exc


def _write_prediction_transaction(
    root: Path,
    path: Path,
    prediction: Prediction,
    attestation: JsonObject,
    ledger_key: str,
) -> Path:
    transaction_path = _prediction_transaction_path(root, ledger_key)
    value: JsonObject = {
        "schema_version": 1,
        "log_path": _prediction_log_relative(root, path),
        "prediction": prediction.to_dict(),
        "attestation": attestation,
    }
    if transaction_path.exists():
        if read_json(transaction_path) != value:
            raise ModelError(
                "an unfinished prediction transaction must be recovered before a new append"
            )
        return transaction_path
    write_json(transaction_path, value)
    return transaction_path


def _recover_prediction_transaction(root: Path, path: Path, ledger_key: str) -> None:
    """Idempotently finish the single write-ahead prediction transaction, if present."""
    transaction_path = _prediction_transaction_path(root, ledger_key)
    if not transaction_path.exists():
        return
    transaction = read_json(transaction_path)
    if (
        set(transaction)
        != {
            "schema_version",
            "log_path",
            "prediction",
            "attestation",
        }
        or transaction.get("schema_version") != 1
    ):
        raise ModelError("trusted prediction transaction has an invalid schema")
    if transaction.get("log_path") != _prediction_log_relative(root, path):
        raise ModelError("trusted prediction transaction targets a different log")
    raw_prediction = transaction.get("prediction")
    raw_attestation = transaction.get("attestation")
    if not isinstance(raw_prediction, dict) or not all(
        isinstance(key, str) for key in raw_prediction
    ):
        raise ModelError("trusted prediction transaction has an invalid prediction")
    if not isinstance(raw_attestation, dict) or not all(
        isinstance(key, str) for key in raw_attestation
    ):
        raise ModelError("trusted prediction transaction has an invalid attestation")
    prediction = Prediction.from_dict(raw_prediction)
    prediction.validate_identity()
    sequence = raw_attestation.get("sequence")
    previous = raw_attestation.get("previous")
    recorded_at = raw_attestation.get("recorded_at")
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or not isinstance(previous, str)
        or not isinstance(recorded_at, str)
    ):
        raise ModelError("trusted prediction transaction has invalid chain fields")
    expected_attestation = _prediction_attestation_value(
        prediction,
        sequence=sequence,
        previous=previous,
        recorded_at=parse_timestamp(recorded_at),
    )
    if raw_attestation != expected_attestation:
        raise ModelError("trusted prediction transaction payload does not match its attestation")

    existing = load_predictions(path)
    if len(existing) == sequence:
        if not existing or existing[-1] != prediction:
            raise ModelError("prediction transaction does not match the current log suffix")
        prefix = existing[:-1]
        already_logged = True
    elif len(existing) == sequence - 1:
        if any(item.id == prediction.id for item in existing):
            raise ModelError("prediction transaction id already exists outside its sequence")
        prefix = existing
        already_logged = False
    else:
        raise ModelError("prediction transaction sequence does not match the current log")
    verified_previous = _verify_prediction_prefix(
        root, prefix, ledger_key=ledger_key, pending_id=prediction.id
    )
    if verified_previous != previous:
        raise ModelError("prediction transaction does not extend the trusted chain head")
    if not already_logged:
        if len(existing) >= _MAX_PREDICTION_RECORDS:
            raise ModelError(
                f"prediction log reached its {_MAX_PREDICTION_RECORDS}-record safety cap"
            )
        content = "".join(canonical_json(item.to_dict()) + "\n" for item in [*existing, prediction])
        _validate_jsonl_content(path, content, "prediction")
        _atomic_write(path, content)
    record_prediction_attestation(
        root,
        prediction,
        ledger_key=ledger_key,
        sequence=sequence,
        previous=previous,
        recorded_at=parse_timestamp(recorded_at),
    )
    recovered = load_predictions(path)
    _verify_prediction_prefix(root, recovered, ledger_key=ledger_key)
    transaction_path.unlink()


def append_prediction(
    path: Path,
    prediction: Prediction,
    *,
    root: Path | None = None,
    recorded_at: datetime | None = None,
) -> None:
    prediction.validate_identity()
    evidence_protocol_hash = prediction.protocol.get("evidence_protocol_hash")
    if not isinstance(evidence_protocol_hash, str):
        raise ModelError("prediction lacks an evidence protocol hash")
    ledger_key = (
        _prediction_ledger_key(root, path, evidence_protocol_hash) if root is not None else ""
    )
    with _file_lock(path):
        if root is not None:
            _recover_prediction_transaction(root, path, ledger_key)
        existing = load_predictions(path)
        if root is not None:
            previous = _verify_prediction_prefix(root, existing, ledger_key=ledger_key)
        if any(item.id == prediction.id for item in existing):
            raise ModelError(f"prediction id already exists: {prediction.id}")
        if len(existing) >= _MAX_PREDICTION_RECORDS:
            raise ModelError(
                f"prediction log reached its {_MAX_PREDICTION_RECORDS}-record safety cap"
            )
        content = "".join(canonical_json(item.to_dict()) + "\n" for item in [*existing, prediction])
        _validate_jsonl_content(path, content, "prediction")
        if root is not None:
            attestation = _prediction_attestation_value(
                prediction,
                sequence=len(existing) + 1,
                previous=previous,
                recorded_at=recorded_at,
            )
            transaction_path = _write_prediction_transaction(
                root,
                path,
                prediction,
                attestation,
                ledger_key,
            )
        _atomic_write(path, content)
        if root is not None:
            recorded_at_value = cast(str, attestation["recorded_at"])
            record_prediction_attestation(
                root,
                prediction,
                ledger_key=ledger_key,
                sequence=len(existing) + 1,
                previous=previous,
                recorded_at=parse_timestamp(recorded_at_value),
            )
            _verify_prediction_prefix(root, [*existing, prediction], ledger_key=ledger_key)
            transaction_path.unlink()
