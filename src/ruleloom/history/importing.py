"""Strict import of normalized, provider-neutral historical JSON Lines."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from ruleloom.history.models import ChangeUnit, HistoricalEvent
from ruleloom.history.storage import (
    HISTORY_JSONL_MAX_BYTES,
    HISTORY_JSONL_MAX_LINE_BYTES,
    HISTORY_JSONL_MAX_RECORDS,
)
from ruleloom.models import JsonObject, ModelError, strict_json_loads

RecordT = TypeVar("RecordT", HistoricalEvent, ChangeUnit)


def _load_jsonl(
    path: Path,
    parser: Callable[[JsonObject], RecordT],
    kind: str,
) -> tuple[RecordT, ...]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ModelError(
            f"{kind} import must be a readable regular, non-symlink file: {path}: {exc}"
        ) from exc
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ModelError(f"{kind} import must be a regular, non-symlink file: {path}")
        if file_stat.st_size > HISTORY_JSONL_MAX_BYTES:
            raise ModelError(f"{kind} import exceeds {HISTORY_JSONL_MAX_BYTES} bytes: {path}")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            content_bytes = handle.read(HISTORY_JSONL_MAX_BYTES + 1)
        if len(content_bytes) > HISTORY_JSONL_MAX_BYTES:
            raise ModelError(f"{kind} import exceeds {HISTORY_JSONL_MAX_BYTES} bytes: {path}")
        content = content_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ModelError(f"cannot read {kind} import {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if len(lines) > HISTORY_JSONL_MAX_RECORDS:
        raise ModelError(f"{kind} import exceeds {HISTORY_JSONL_MAX_RECORDS} records: {path}")
    records: list[RecordT] = []
    identifiers: set[str] = set()
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            raise ModelError(f"blank {kind} import record at {path}:{line_number}")
        if len(line.encode("utf-8")) > HISTORY_JSONL_MAX_LINE_BYTES:
            raise ModelError(f"{kind} import record is too large at {path}:{line_number}")
        try:
            raw = strict_json_loads(line, f"{path}:{line_number}")
        except json.JSONDecodeError as exc:
            raise ModelError(f"invalid JSON at {path}:{line_number}: {exc}") from exc
        if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
            raise ModelError(f"expected an object at {path}:{line_number}")
        record = parser(raw)
        if record.id in identifiers:
            raise ModelError(f"duplicate {kind} id {record.id!r} in {path}")
        identifiers.add(record.id)
        records.append(record)
    return tuple(records)


def import_events(path: Path) -> tuple[HistoricalEvent, ...]:
    """Parse normalized historical events without changing project state."""
    return _load_jsonl(path, HistoricalEvent.from_dict, "historical event")


def import_change_units(path: Path) -> tuple[ChangeUnit, ...]:
    """Parse normalized logical change units without changing project state."""
    return _load_jsonl(path, ChangeUnit.from_dict, "change unit")
