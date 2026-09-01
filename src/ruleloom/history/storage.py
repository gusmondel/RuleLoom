"""Bounded, immutable JSONL persistence for historical bootstrap records."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Protocol, TypeVar

from ruleloom.history.models import ChangeUnit, HistoricalEvent
from ruleloom.history.units import validate_history_snapshot
from ruleloom.models import (
    JsonObject,
    ModelError,
    canonical_json,
    parse_timestamp,
    strict_json_loads,
)
from ruleloom.storage import project_path, trusted_state_path

HISTORY_DIRECTORY = Path(".ruleloom/history")
EVENTS_RELATIVE_PATH = HISTORY_DIRECTORY / "events.jsonl"
CHANGE_UNITS_RELATIVE_PATH = HISTORY_DIRECTORY / "change-units.jsonl"

_TRANSACTION_ANCHOR = ".ruleloom-history-transaction"
_TRANSACTION_JOURNAL = ".ruleloom-history-transaction.json"
_EVENTS_OLD_STAGE = ".ruleloom-history-events.old"
_EVENTS_NEW_STAGE = ".ruleloom-history-events.new"
_UNITS_OLD_STAGE = ".ruleloom-history-units.old"
_UNITS_NEW_STAGE = ".ruleloom-history-units.new"

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


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _transaction_state_directory(log_directory: Path) -> Path:
    if log_directory.name == HISTORY_DIRECTORY.name and log_directory.parent.name == ".ruleloom":
        root = log_directory.parent.parent
        return trusted_state_path(root) / "history-transaction"
    # Direct storage-unit tests may use canonical filenames outside a repository.
    return log_directory


def _transaction_files(state_directory: Path) -> dict[str, Path]:
    return {
        "journal": state_directory / _TRANSACTION_JOURNAL,
        "events_old": state_directory / _EVENTS_OLD_STAGE,
        "events_new": state_directory / _EVENTS_NEW_STAGE,
        "units_old": state_directory / _UNITS_OLD_STAGE,
        "units_new": state_directory / _UNITS_NEW_STAGE,
    }


def _read_bounded_text(path: Path, *, maximum: int, kind: str) -> str:
    _reject_unsafe_file(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ModelError(f"cannot read {kind} {path}: {exc}") from exc
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > maximum:
            raise ModelError(f"{kind} is not a bounded regular file: {path}")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            raw = handle.read(maximum + 1)
        if len(raw) > maximum:
            raise ModelError(f"{kind} exceeds {maximum} bytes: {path}")
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ModelError(f"{kind} is not valid UTF-8: {path}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _load_transaction(state_directory: Path) -> JsonObject | None:
    journal = _transaction_files(state_directory)["journal"]
    if not journal.exists():
        return None
    content = _read_bounded_text(journal, maximum=16 * 1024, kind="history transaction")
    try:
        value = strict_json_loads(content, str(journal))
    except json.JSONDecodeError as exc:
        raise ModelError(f"invalid history transaction journal {journal}: {exc}") from exc
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ModelError(f"history transaction journal must contain an object: {journal}")
    expected = {
        "schema_version",
        "events_existed",
        "units_existed",
        "events_old_sha256",
        "events_new_sha256",
        "units_old_sha256",
        "units_new_sha256",
    }
    digests = (
        value.get("events_old_sha256"),
        value.get("events_new_sha256"),
        value.get("units_old_sha256"),
        value.get("units_new_sha256"),
    )
    if (
        set(value) != expected
        or value.get("schema_version") != 1
        or not isinstance(value.get("events_existed"), bool)
        or not isinstance(value.get("units_existed"), bool)
        or any(
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            for digest in digests
        )
    ):
        raise ModelError(f"history transaction journal has an invalid schema: {journal}")
    return value


def _stage_content(path: Path, expected_sha256: object, kind: str) -> str:
    if not isinstance(expected_sha256, str):
        raise ModelError(f"history transaction {kind} digest is invalid")
    content = _read_bounded_text(path, maximum=HISTORY_JSONL_MAX_BYTES, kind=kind)
    if _content_sha256(content) != expected_sha256:
        raise ModelError(f"history transaction {kind} digest does not match: {path}")
    return content


def _cleanup_transaction_stages(state_directory: Path) -> None:
    files = _transaction_files(state_directory)
    for key in ("events_old", "events_new", "units_old", "units_new"):
        with suppress(OSError):
            files[key].unlink(missing_ok=True)


def _recover_history_transaction(log_directory: Path) -> None:
    """Roll back one interrupted two-log transaction before exposing either log."""

    state_directory = _transaction_state_directory(log_directory)
    transaction = _load_transaction(state_directory)
    if transaction is None:
        _cleanup_transaction_stages(state_directory)
        return
    files = _transaction_files(state_directory)
    events = log_directory / EVENTS_RELATIVE_PATH.name
    units = log_directory / CHANGE_UNITS_RELATIVE_PATH.name
    events_old = _stage_content(
        files["events_old"],
        transaction.get("events_old_sha256"),
        "old events stage",
    )
    units_old = _stage_content(
        files["units_old"],
        transaction.get("units_old_sha256"),
        "old units stage",
    )
    if transaction["events_existed"]:
        _atomic_write(events, events_old)
    else:
        events.unlink(missing_ok=True)
    if transaction["units_existed"]:
        _atomic_write(units, units_old)
    else:
        units.unlink(missing_ok=True)
    _fsync_directory(log_directory)
    files["journal"].unlink()
    _fsync_directory(state_directory)
    _cleanup_transaction_stages(state_directory)


@contextmanager
def _history_transaction_guard(log_directory: Path) -> Iterator[None]:
    log_directory.mkdir(parents=True, exist_ok=True)
    state_directory = _transaction_state_directory(log_directory)
    state_directory.mkdir(parents=True, exist_ok=True)
    with _file_lock(state_directory / _TRANSACTION_ANCHOR):
        _recover_history_transaction(log_directory)
        yield


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


def _merge_records(
    existing_records: Iterable[RecordT],
    incoming: Iterable[RecordT],
    kind: str,
) -> tuple[dict[str, RecordT], tuple[int, int]]:
    existing = {record.id: record for record in existing_records}
    candidates: dict[str, RecordT] = {}
    for record in incoming:
        previous = candidates.get(record.id)
        if previous is not None and previous != record:
            raise ModelError(f"conflicting incoming {kind} id {record.id!r}")
        candidates[record.id] = record
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
    return existing, (inserted, unchanged)


def _event_order(event: HistoricalEvent) -> tuple[object, ...]:
    return (parse_timestamp(event.available_at), parse_timestamp(event.occurred_at), event.id)


def _change_unit_order(unit: ChangeUnit) -> tuple[object, ...]:
    return (parse_timestamp(unit.prediction_at), unit.id)


def _validate_history_pair_paths(event_path: Path, unit_path: Path) -> None:
    if (
        event_path.parent.resolve() != unit_path.parent.resolve()
        or event_path.name != EVENTS_RELATIVE_PATH.name
        or unit_path.name != CHANGE_UNITS_RELATIVE_PATH.name
    ):
        raise ModelError("history paths must be sibling events.jsonl and change-units.jsonl files")


def load_history_snapshot(
    event_path: Path,
    unit_path: Path,
) -> tuple[list[HistoricalEvent], list[ChangeUnit]]:
    """Read and relationally validate both canonical logs under one guard."""

    _validate_history_pair_paths(event_path, unit_path)
    with _history_transaction_guard(event_path.parent):
        events = _read_records(event_path, HistoricalEvent.from_dict, "historical event")
        units = _read_records(unit_path, ChangeUnit.from_dict, "change unit")
        validate_history_snapshot(events, units)
        return events, units


def load_events(path: Path) -> list[HistoricalEvent]:
    with _history_transaction_guard(path.parent):
        return _read_records(path, HistoricalEvent.from_dict, "historical event")


def save_events(path: Path, events: Iterable[HistoricalEvent]) -> None:
    materialized = tuple(events)
    unit_path = path.parent / CHANGE_UNITS_RELATIVE_PATH.name
    _validate_history_pair_paths(path, unit_path)
    with _history_transaction_guard(path.parent):
        units = _read_records(
            unit_path,
            ChangeUnit.from_dict,
            "change unit",
        )
        validate_history_snapshot(materialized, units)
        _save_records(
            path,
            materialized,
            HistoricalEvent.from_dict,
            "historical event",
            _event_order,
        )


def upsert_events(path: Path, events: Iterable[HistoricalEvent]) -> tuple[int, int]:
    """Insert immutable events, returning ``(inserted, unchanged)`` counts."""
    event_counts, _unit_counts = upsert_history_batch(
        path,
        events,
        path.parent / CHANGE_UNITS_RELATIVE_PATH.name,
        (),
    )
    return event_counts


def load_change_units(path: Path) -> list[ChangeUnit]:
    with _history_transaction_guard(path.parent):
        return _read_records(path, ChangeUnit.from_dict, "change unit")


def save_change_units(path: Path, units: Iterable[ChangeUnit]) -> None:
    materialized = tuple(units)
    event_path = path.parent / EVENTS_RELATIVE_PATH.name
    _validate_history_pair_paths(event_path, path)
    with _history_transaction_guard(path.parent):
        events = _read_records(
            event_path,
            HistoricalEvent.from_dict,
            "historical event",
        )
        validate_history_snapshot(events, materialized)
        _save_records(path, materialized, ChangeUnit.from_dict, "change unit", _change_unit_order)


def upsert_change_units(path: Path, units: Iterable[ChangeUnit]) -> tuple[int, int]:
    """Insert immutable change units, returning ``(inserted, unchanged)`` counts."""
    _event_counts, unit_counts = upsert_history_batch(
        path.parent / EVENTS_RELATIVE_PATH.name,
        (),
        path,
        units,
    )
    return unit_counts


def upsert_history_batch(
    event_path: Path,
    incoming_events: Iterable[HistoricalEvent],
    unit_path: Path,
    incoming_units: Iterable[ChangeUnit],
) -> tuple[tuple[int, int], tuple[int, int]]:
    """Atomically insert one validated event/unit batch or restore both old logs.

    A small write-ahead journal and bounded old/new stages make an interrupted
    two-file update recoverable. Readers acquire the same directory guard and
    roll back any unfinished transaction before exposing either log.
    """

    _validate_history_pair_paths(event_path, unit_path)
    events_to_insert = tuple(incoming_events)
    units_to_insert = tuple(incoming_units)
    directory = event_path.parent
    with _history_transaction_guard(directory), _file_lock(event_path), _file_lock(unit_path):
        persisted_events = _read_records(
            event_path,
            HistoricalEvent.from_dict,
            "historical event",
        )
        persisted_units = _read_records(unit_path, ChangeUnit.from_dict, "change unit")
        merged_events, event_counts = _merge_records(
            persisted_events,
            events_to_insert,
            "historical event",
        )
        merged_units, unit_counts = _merge_records(
            persisted_units,
            units_to_insert,
            "change unit",
        )
        validate_history_snapshot(
            tuple(merged_events.values()),
            tuple(merged_units.values()),
        )
        events_old_content = _canonical_content(
            event_path,
            persisted_events,
            "historical event",
            _event_order,
        )
        units_old_content = _canonical_content(
            unit_path,
            persisted_units,
            "change unit",
            _change_unit_order,
        )
        events_new_content = _canonical_content(
            event_path,
            merged_events.values(),
            "historical event",
            _event_order,
        )
        units_new_content = _canonical_content(
            unit_path,
            merged_units.values(),
            "change unit",
            _change_unit_order,
        )
        if (
            events_old_content == events_new_content
            and units_old_content == units_new_content
            and event_path.exists()
            and unit_path.exists()
        ):
            return event_counts, unit_counts

        state_directory = _transaction_state_directory(directory)
        files = _transaction_files(state_directory)
        _cleanup_transaction_stages(state_directory)
        _atomic_write(files["events_old"], events_old_content)
        _atomic_write(files["events_new"], events_new_content)
        _atomic_write(files["units_old"], units_old_content)
        _atomic_write(files["units_new"], units_new_content)
        transaction: JsonObject = {
            "schema_version": 1,
            "events_existed": event_path.exists(),
            "units_existed": unit_path.exists(),
            "events_old_sha256": _content_sha256(events_old_content),
            "events_new_sha256": _content_sha256(events_new_content),
            "units_old_sha256": _content_sha256(units_old_content),
            "units_new_sha256": _content_sha256(units_new_content),
        }
        try:
            _atomic_write(files["journal"], canonical_json(transaction) + "\n")
            _atomic_write(event_path, events_new_content)
            _atomic_write(unit_path, units_new_content)
            _fsync_directory(directory)
            files["journal"].unlink()
            _fsync_directory(state_directory)
        except BaseException:
            _recover_history_transaction(directory)
            raise
        _cleanup_transaction_stages(state_directory)
        return event_counts, unit_counts
