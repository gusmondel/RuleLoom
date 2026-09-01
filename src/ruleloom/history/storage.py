"""Bounded, immutable JSONL persistence for historical bootstrap records."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol, TypeVar

from ruleloom.history.models import ChangeUnit, HistoricalEvent
from ruleloom.models import (
    JsonObject,
    ModelError,
    canonical_json,
    parse_timestamp,
    strict_json_loads,
)
from ruleloom.storage import project_path

HISTORY_DIRECTORY = Path(".ruleloom/history")
EVENTS_RELATIVE_PATH = HISTORY_DIRECTORY / "events.jsonl"
CHANGE_UNITS_RELATIVE_PATH = HISTORY_DIRECTORY / "change-units.jsonl"

HISTORY_JSONL_MAX_BYTES = 64 * 1024 * 1024
HISTORY_JSONL_MAX_RECORDS = 250_000
HISTORY_JSONL_MAX_LINE_BYTES = 1024 * 1024


class _HistoricalRecord(Protocol):
    @property
    def id(self) -> str: ...

    def to_dict(self) -> JsonObject: ...


RecordT = TypeVar("RecordT", bound=_HistoricalRecord)


def events_path(root: Path) -> Path:
    return project_path(root, EVENTS_RELATIVE_PATH)


def change_units_path(root: Path) -> Path:
    return project_path(root, CHANGE_UNITS_RELATIVE_PATH)


def _reject_unsafe_file(path: Path) -> None:
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        raise ModelError(f"historical storage path must be a regular file: {path}")


@contextmanager
def _file_lock(path: Path, *, timeout_seconds: float = 10.0) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    flags = os.O_CREAT | os.O_RDWR
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise ModelError(f"cannot safely open historical storage lock {lock_path}: {exc}") from exc
    lock_stat = os.fstat(descriptor)
    if not stat.S_ISREG(lock_stat.st_mode):
        os.close(descriptor)
        raise ModelError(f"historical storage lock must be a regular file: {lock_path}")
    if lock_stat.st_uid != os.getuid():
        os.close(descriptor)
        raise ModelError(f"historical storage lock must be owned by the current user: {lock_path}")
    if stat.S_IMODE(lock_stat.st_mode) & 0o077:
        os.close(descriptor)
        raise ModelError(f"historical storage lock permissions are too broad: {lock_path}")
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(descriptor)
                raise ModelError(
                    f"timed out waiting for historical storage lock: {lock_path}"
                ) from None
            time.sleep(0.05)
    try:
        _reject_unsafe_file(path)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_unsafe_file(path)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _jsonl_lines(content: str) -> list[str]:
    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _validate_content(path: Path, content: str, kind: str) -> None:
    if len(content.encode("utf-8")) > HISTORY_JSONL_MAX_BYTES:
        raise ModelError(f"{kind} log exceeds {HISTORY_JSONL_MAX_BYTES} bytes: {path}")
    lines = _jsonl_lines(content)
    if len(lines) > HISTORY_JSONL_MAX_RECORDS:
        raise ModelError(f"{kind} log exceeds {HISTORY_JSONL_MAX_RECORDS} records: {path}")
    for line_number, line in enumerate(lines, 1):
        if len(line.encode("utf-8")) > HISTORY_JSONL_MAX_LINE_BYTES:
            raise ModelError(f"{kind} record is too large at {path}:{line_number}")


def _read_records(
    path: Path,
    parser: Callable[[JsonObject], RecordT],
    kind: str,
) -> list[RecordT]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise ModelError(
            f"{kind} log must be a readable regular file and not a symlink: {path}: {exc}"
        ) from exc
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ModelError(f"historical storage path must be a regular file: {path}")
        if file_stat.st_size > HISTORY_JSONL_MAX_BYTES:
            raise ModelError(f"{kind} log exceeds {HISTORY_JSONL_MAX_BYTES} bytes: {path}")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            content_bytes = handle.read(HISTORY_JSONL_MAX_BYTES + 1)
        if len(content_bytes) > HISTORY_JSONL_MAX_BYTES:
            raise ModelError(f"{kind} log exceeds {HISTORY_JSONL_MAX_BYTES} bytes: {path}")
        content = content_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ModelError(f"{kind} log is not valid UTF-8: {path}") from exc
    except OSError as exc:
        raise ModelError(f"cannot read {kind} log {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    records: list[RecordT] = []
    seen: set[str] = set()
    for line_number, line in enumerate(_jsonl_lines(content), 1):
        if line_number > HISTORY_JSONL_MAX_RECORDS:
            raise ModelError(f"{kind} log exceeds {HISTORY_JSONL_MAX_RECORDS} records: {path}")
        if len(line.encode("utf-8")) > HISTORY_JSONL_MAX_LINE_BYTES:
            raise ModelError(f"{kind} record is too large at {path}:{line_number}")
        if not line.strip():
            raise ModelError(f"blank {kind} record at {path}:{line_number}")
        try:
            raw = strict_json_loads(line, f"{path}:{line_number}")
        except json.JSONDecodeError as exc:
            raise ModelError(f"invalid JSON at {path}:{line_number}: {exc}") from exc
        if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
            raise ModelError(f"expected an object at {path}:{line_number}")
        record = parser(raw)
        if record.id in seen:
            raise ModelError(f"duplicate {kind} id {record.id!r} in {path}")
        seen.add(record.id)
        records.append(record)
    return records


def _canonical_content(
    path: Path,
    records: Iterable[RecordT],
    kind: str,
    order_key: Callable[[RecordT], tuple[object, ...]],
) -> str:
    materialized = list(records)
    by_id: dict[str, RecordT] = {}
    for record in materialized:
        if record.id in by_id:
            raise ModelError(f"cannot persist duplicate {kind} id {record.id!r}")
        by_id[record.id] = record
    materialized.sort(key=order_key)
    content = "".join(canonical_json(record.to_dict()) + "\n" for record in materialized)
    _validate_content(path, content, kind)
    return content


def _save_records(
    path: Path,
    records: Iterable[RecordT],
    parser: Callable[[JsonObject], RecordT],
    kind: str,
    order_key: Callable[[RecordT], tuple[object, ...]],
) -> None:
    content = _canonical_content(path, records, kind, order_key)
    with _file_lock(path):
        if path.exists():
            persisted = _read_records(path, parser, kind)
            existing_content = _canonical_content(path, persisted, kind, order_key)
            if existing_content != content:
                raise ModelError(f"refusing to overwrite immutable {kind} log: {path}")
            return
        _atomic_write(path, content)


def _upsert_records(
    path: Path,
    incoming: Iterable[RecordT],
    parser: Callable[[JsonObject], RecordT],
    kind: str,
    order_key: Callable[[RecordT], tuple[object, ...]],
) -> tuple[int, int]:
    candidates: dict[str, RecordT] = {}
    for record in incoming:
        previous = candidates.get(record.id)
        if previous is not None and previous != record:
            raise ModelError(f"conflicting incoming {kind} id {record.id!r}")
        candidates[record.id] = record
    with _file_lock(path):
        existing = {record.id: record for record in _read_records(path, parser, kind)}
        inserted = 0
        unchanged = 0
        for identifier, record in candidates.items():
            previous = existing.get(identifier)
            if previous is None:
                existing[identifier] = record
                inserted += 1
            elif previous != record:
                raise ModelError(f"refusing to overwrite immutable {kind} {identifier!r}")
            else:
                unchanged += 1
        content = _canonical_content(path, existing.values(), kind, order_key)
        if inserted or not path.exists():
            _atomic_write(path, content)
    return inserted, unchanged


def _event_order(event: HistoricalEvent) -> tuple[object, ...]:
    return (parse_timestamp(event.available_at), parse_timestamp(event.occurred_at), event.id)


def _change_unit_order(unit: ChangeUnit) -> tuple[object, ...]:
    return (parse_timestamp(unit.prediction_at), unit.id)


def load_events(path: Path) -> list[HistoricalEvent]:
    return _read_records(path, HistoricalEvent.from_dict, "historical event")


def save_events(path: Path, events: Iterable[HistoricalEvent]) -> None:
    _save_records(path, events, HistoricalEvent.from_dict, "historical event", _event_order)


def upsert_events(path: Path, events: Iterable[HistoricalEvent]) -> tuple[int, int]:
    """Insert immutable events, returning ``(inserted, unchanged)`` counts."""
    return _upsert_records(
        path, events, HistoricalEvent.from_dict, "historical event", _event_order
    )


def load_change_units(path: Path) -> list[ChangeUnit]:
    return _read_records(path, ChangeUnit.from_dict, "change unit")


def save_change_units(path: Path, units: Iterable[ChangeUnit]) -> None:
    _save_records(path, units, ChangeUnit.from_dict, "change unit", _change_unit_order)


def upsert_change_units(path: Path, units: Iterable[ChangeUnit]) -> tuple[int, int]:
    """Insert immutable change units, returning ``(inserted, unchanged)`` counts."""
    return _upsert_records(path, units, ChangeUnit.from_dict, "change unit", _change_unit_order)
