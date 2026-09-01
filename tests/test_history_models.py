from __future__ import annotations

import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from ruleloom.history import (
    ChangeUnit,
    HistoricalEvent,
    change_units_path,
    events_path,
    load_change_units,
    load_events,
    load_history_snapshot,
    save_change_units,
    save_events,
    upsert_change_units,
    upsert_events,
    upsert_history_batch,
    validate_git_sha,
)
from ruleloom.history import storage as history_storage
from ruleloom.models import JsonValue, ModelError, canonical_json
from ruleloom.storage import trusted_state_path

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
SHA_256 = "d" * 64


def _event(
    identifier: str = "event.git.1",
    *,
    occurred_at: str = "2026-01-02T10:00:00+00:00",
    available_at: str = "2026-01-02T10:05:00+00:00",
) -> HistoricalEvent:
    return HistoricalEvent(
        id=identifier,
        repository_id="repo.example",
        kind="ci_check",
        occurred_at=occurred_at,
        available_at=available_at,
        provider="generic_ci",
        source_ref="ci://build/42#test",
        change_id="change.pr.42",
        independent_group="pr.42",
        data={"conclusion": "failed", "attempt": 1, "tags": ["unit", "linux"]},
    )


def _unit(
    identifier: str = "change.pr.42",
    *,
    prediction_at: str = "2026-01-02T09:00:00+00:00",
) -> ChangeUnit:
    return ChangeUnit(
        id=identifier,
        repository_id="repo.example",
        kind="pull_request",
        base_sha=SHA_A,
        prediction_sha=SHA_B,
        prediction_at=prediction_at,
        final_sha=SHA_C,
        finalized_at="2026-01-03T09:00:00+00:00",
        commits=(SHA_B,),
        event_ids=("event.git.1",),
        provider="forge",
        source_ref="forge://repo/pulls/42",
        evidence_quality="rich",
        confirmatory=False,
    )


def _batch_pair(suffix: str) -> tuple[HistoricalEvent, ChangeUnit]:
    change_id = f"change.{suffix}"
    event = replace(
        _event(f"event.{suffix}"),
        change_id=change_id,
    )
    unit = replace(
        _unit(change_id),
        final_sha=None,
        finalized_at=None,
        event_ids=(event.id,),
        evidence_quality="git_only",
    )
    return event, unit


def test_historical_event_round_trips_strict_json() -> None:
    event = _event()

    encoded = canonical_json(event.to_dict())
    decoded = json.loads(encoded)

    assert HistoricalEvent.from_dict(decoded) == event
    assert decoded["schema_version"] == 1
    assert decoded["data"]["conclusion"] == "failed"


def test_change_unit_round_trips_strict_json_and_sha256() -> None:
    unit = replace(
        _unit(),
        base_sha=SHA_256,
        prediction_sha="e" * 64,
        final_sha="f" * 64,
        commits=("e" * 64,),
    )

    encoded = canonical_json(unit.to_dict())
    decoded = json.loads(encoded)

    assert ChangeUnit.from_dict(decoded) == unit
    assert decoded["commits"] == ["e" * 64]


@pytest.mark.parametrize(
    "value",
    [
        "a" * 39,
        "a" * 41,
        "A" * 40,
        "g" * 40,
        "a" * 63,
        "a" * 65,
    ],
)
def test_git_sha_requires_lowercase_sha1_or_sha256(value: str) -> None:
    with pytest.raises(ModelError, match="40- or 64-character Git SHA"):
        validate_git_sha(value)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"id": "Event/1"}, "historical event id"),
        ({"repository_id": "Repo Example"}, "repository_id"),
        ({"kind": "CI Check"}, "kind"),
        ({"provider": "Git Hub"}, "provider"),
        ({"independent_group": "PR/42"}, "independent_group"),
        ({"change_id": "change/42"}, "change_id"),
        ({"source_ref": "\n"}, "source_ref"),
        ({"occurred_at": "2026-01-02T10:00:00"}, "timezone"),
        (
            {"available_at": "2026-01-02T09:59:59+00:00"},
            "cannot predate",
        ),
        ({"schema_version": 2}, "unsupported historical-event schema"),
        ({"data": {"invalid": float("nan")}}, "NaN or Infinity"),
    ],
)
def test_historical_event_rejects_invalid_evidence(
    changes: dict[str, JsonValue], message: str
) -> None:
    value = _event().to_dict()
    value.update(changes)
    with pytest.raises(ModelError, match=message):
        HistoricalEvent.from_dict(value)


def test_historical_event_from_dict_rejects_unknown_and_missing_fields() -> None:
    value = _event().to_dict()
    value["unexpected"] = True
    with pytest.raises(ModelError, match="unknown historical event fields"):
        HistoricalEvent.from_dict(value)

    value = _event().to_dict()
    del value["occurred_at"]
    with pytest.raises(ModelError, match="occurred_at must be a string"):
        HistoricalEvent.from_dict(value)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"id": "Change/1"}, "change unit id"),
        ({"prediction_at": "2026-01-02T09:00:00"}, "timezone"),
        ({"base_sha": "a" * 39}, "base_sha"),
        ({"final_sha": None}, "must be set together"),
        ({"finalized_at": None}, "must be set together"),
        ({"finalized_at": "2025-12-01T00:00:00Z"}, "cannot predate"),
        ({"commits": [SHA_B, SHA_B]}, "duplicate"),
        ({"event_ids": ["event.git.1", "event.git.1"]}, "duplicate"),
        ({"event_ids": ["event/bad"]}, "event id"),
        ({"evidence_quality": "unknown"}, "evidence_quality"),
        ({"confirmatory": 1}, "must be a boolean"),
        ({"schema_version": 2}, "unsupported change-unit schema"),
    ],
)
def test_change_unit_rejects_invalid_boundaries(
    changes: dict[str, JsonValue], message: str
) -> None:
    value = _unit().to_dict()
    value.update(changes)
    with pytest.raises(ModelError, match=message):
        ChangeUnit.from_dict(value)


def test_change_unit_accepts_open_nonconfirmatory_unit() -> None:
    unit = replace(_unit(), final_sha=None, finalized_at=None, evidence_quality="git_only")

    assert unit.final_sha is None
    assert ChangeUnit.from_dict(unit.to_dict()) == unit


def test_history_paths_are_fixed_and_reject_managed_symlinks(tmp_path: Path) -> None:
    assert events_path(tmp_path) == tmp_path / ".ruleloom/history/events.jsonl"
    assert change_units_path(tmp_path) == tmp_path / ".ruleloom/history/change-units.jsonl"

    outside = tmp_path / "outside"
    outside.mkdir()
    managed = tmp_path / ".ruleloom"
    managed.mkdir()
    (managed / "history").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ModelError, match="managed-path symlink"):
        events_path(tmp_path)


def test_event_storage_is_canonical_bounded_and_ordered_by_availability(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    later = _event("event.later", available_at="2026-01-03T10:00:00Z")
    earlier = _event(
        "event.earlier",
        occurred_at="2026-01-01T10:00:00-03:00",
        available_at="2026-01-01T11:00:00-03:00",
    )

    save_events(path, [later, earlier])

    assert load_events(path) == [earlier, later]
    assert path.read_text(encoding="utf-8") == (
        canonical_json(earlier.to_dict()) + "\n" + canonical_json(later.to_dict()) + "\n"
    )


def test_change_unit_storage_is_ordered_and_save_is_immutable(tmp_path: Path) -> None:
    event_path = tmp_path / "events.jsonl"
    unit_path = tmp_path / "change-units.jsonl"
    later_event, later = _batch_pair("later")
    earlier_event, earlier_unit = _batch_pair("earlier")
    earlier = replace(
        earlier_unit,
        prediction_at="2026-01-01T09:00:00Z",
    )
    save_events(event_path, [later_event, earlier_event])

    save_change_units(unit_path, [later, earlier])
    save_change_units(unit_path, [earlier, later])

    assert load_change_units(unit_path) == [earlier, later]
    with pytest.raises(ModelError, match="refusing to overwrite immutable change unit log"):
        save_change_units(unit_path, [earlier])
    assert load_change_units(unit_path) == [earlier, later]


def test_upserts_are_idempotent_and_reject_conflicting_overwrites(tmp_path: Path) -> None:
    event_path = tmp_path / "events.jsonl"
    unit_path = tmp_path / "change-units.jsonl"
    event, unit = _batch_pair("upsert")

    assert upsert_events(event_path, [event]) == (1, 0)
    assert upsert_events(event_path, [event]) == (0, 1)
    assert upsert_change_units(unit_path, [unit]) == (1, 0)
    assert upsert_change_units(unit_path, [unit]) == (0, 1)

    with pytest.raises(ModelError, match="refusing to overwrite immutable historical event"):
        upsert_events(event_path, [replace(event, data={"conclusion": "passed"})])
    with pytest.raises(ModelError, match="refusing to overwrite immutable change unit"):
        upsert_change_units(unit_path, [replace(unit, source_ref="forge://repo/pulls/other")])
    assert load_events(event_path) == [event]
    assert load_change_units(unit_path) == [unit]


def test_unit_only_upsert_cannot_create_dangling_history_snapshot(tmp_path: Path) -> None:
    unit_path = tmp_path / "change-units.jsonl"
    _event, unit = _batch_pair("dangling")

    with pytest.raises(ModelError, match="references missing historical event"):
        upsert_change_units(unit_path, [unit])

    assert load_change_units(unit_path) == []


def test_history_snapshot_pins_one_builtin_github_repository_identity(
    tmp_path: Path,
) -> None:
    event_path = tmp_path / "events.jsonl"
    unit_path = tmp_path / "change-units.jsonl"

    def github_pair(suffix: str, repository_key: str) -> tuple[HistoricalEvent, ChangeUnit]:
        event, unit = _batch_pair(suffix)
        source_ref = f"github:{repository_key}:pull:7"
        return (
            replace(
                event,
                provider="github",
                source_ref=f"{source_ref}:snapshot",
                data={**event.data, "adapter": "ruleloom-github/1"},
            ),
            replace(
                unit,
                kind="github_archive_change",
                provider="github",
                source_ref=source_ref,
            ),
        )

    first = github_pair("github-first", f"github.github.com.repo.{'a' * 20}")
    second = github_pair("github-second", f"github.github.com.repo.{'b' * 20}")
    upsert_history_batch(event_path, [first[0]], unit_path, [first[1]])

    with pytest.raises(ModelError, match="multiple built-in GitHub repository identities"):
        upsert_history_batch(event_path, [second[0]], unit_path, [second[1]])

    assert load_history_snapshot(event_path, unit_path) == ([first[0]], [first[1]])


def test_history_snapshot_reader_cannot_interleave_with_batch_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_path = tmp_path / "events.jsonl"
    unit_path = tmp_path / "change-units.jsonl"
    old_event, old_unit = _batch_pair("old-snapshot")
    new_event, new_unit = _batch_pair("new-snapshot")
    upsert_history_batch(event_path, [old_event], unit_path, [old_unit])

    event_read = threading.Event()
    release_reader = threading.Event()
    writer_started = threading.Event()
    original_read = history_storage._read_records

    def pause_after_events(*args: object, **kwargs: object) -> list[object]:
        records = original_read(*args, **kwargs)  # type: ignore[arg-type]
        if args[0] == event_path and not event_read.is_set():
            event_read.set()
            assert release_reader.wait(timeout=5)
        return records

    def write_batch() -> None:
        writer_started.set()
        upsert_history_batch(event_path, [new_event], unit_path, [new_unit])

    monkeypatch.setattr(history_storage, "_read_records", pause_after_events)
    with ThreadPoolExecutor(max_workers=2) as executor:
        reader = executor.submit(load_history_snapshot, event_path, unit_path)
        assert event_read.wait(timeout=5)
        writer = executor.submit(write_batch)
        assert writer_started.wait(timeout=5)
        assert not writer.done()
        release_reader.set()
        events, units = reader.result(timeout=5)
        writer.result(timeout=5)

    assert events == [old_event]
    assert units == [old_unit]
    final_events, final_units = load_history_snapshot(event_path, unit_path)
    assert {item.id for item in final_events} == {old_event.id, new_event.id}
    assert {item.id for item in final_units} == {old_unit.id, new_unit.id}


def test_history_batch_rolls_back_both_logs_when_second_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_path = tmp_path / "events.jsonl"
    unit_path = tmp_path / "change-units.jsonl"
    old_event, old_unit = _batch_pair("old")
    new_event, new_unit = _batch_pair("new")
    save_events(event_path, [old_event])
    save_change_units(unit_path, [old_unit])
    original_write = history_storage._atomic_write
    failed = False

    def fail_unit_once(path: Path, content: str) -> None:
        nonlocal failed
        if path == unit_path and not failed:
            failed = True
            raise OSError("injected unit write failure")
        original_write(path, content)

    monkeypatch.setattr(history_storage, "_atomic_write", fail_unit_once)

    with pytest.raises(OSError, match="injected unit write failure"):
        upsert_history_batch(event_path, [new_event], unit_path, [new_unit])

    assert load_events(event_path) == [old_event]
    assert load_change_units(unit_path) == [old_unit]
    assert not list(tmp_path.glob(".ruleloom-history-*"))


def test_history_reader_recovers_an_interrupted_batch_before_exposing_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_path = tmp_path / "events.jsonl"
    unit_path = tmp_path / "change-units.jsonl"
    old_event, old_unit = _batch_pair("old")
    new_event, new_unit = _batch_pair("new")
    save_events(event_path, [old_event])
    save_change_units(unit_path, [old_unit])
    original_write = history_storage._atomic_write
    original_recover = history_storage._recover_history_transaction
    failed = False

    def fail_unit_once(path: Path, content: str) -> None:
        nonlocal failed
        if path == unit_path and not failed:
            failed = True
            raise OSError("simulated process interruption")
        original_write(path, content)

    def interrupt_recovery(_directory: Path) -> None:
        raise RuntimeError("recovery deferred until next process")

    monkeypatch.setattr(history_storage, "_atomic_write", fail_unit_once)
    monkeypatch.setattr(history_storage, "_recover_history_transaction", interrupt_recovery)
    with pytest.raises(RuntimeError, match="recovery deferred"):
        upsert_history_batch(event_path, [new_event], unit_path, [new_unit])

    monkeypatch.setattr(history_storage, "_atomic_write", original_write)
    monkeypatch.setattr(history_storage, "_recover_history_transaction", original_recover)
    assert load_events(event_path) == [old_event]
    assert load_change_units(unit_path) == [old_unit]
    assert not list(tmp_path.glob(".ruleloom-history-*"))


def test_canonical_history_transaction_state_stays_outside_repository_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subprocess.run(
        ["git", "init", "--quiet", str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    event_path = events_path(tmp_path)
    unit_path = change_units_path(tmp_path)
    original_recover = history_storage._recover_history_transaction

    original_write = history_storage._atomic_write
    failed = False

    def leave_prepared_transaction(directory: Path) -> None:
        if failed:
            raise RuntimeError("simulate process exit before recovery")
        original_recover(directory)

    def fail_unit_once(path: Path, content: str) -> None:
        nonlocal failed
        if path == unit_path and not failed:
            failed = True
            raise OSError("simulated unit write failure")
        original_write(path, content)

    monkeypatch.setattr(history_storage, "_atomic_write", fail_unit_once)
    monkeypatch.setattr(history_storage, "_recover_history_transaction", leave_prepared_transaction)
    with pytest.raises(RuntimeError, match="simulate process exit"):
        event, unit = _batch_pair("private-state")
        upsert_history_batch(event_path, [event], unit_path, [unit])

    state_directory = trusted_state_path(tmp_path) / "history-transaction"
    assert (state_directory / ".ruleloom-history-transaction.json").is_file()
    assert not list((tmp_path / ".ruleloom" / "history").glob(".ruleloom-history-*"))

    monkeypatch.setattr(history_storage, "_atomic_write", original_write)
    monkeypatch.setattr(history_storage, "_recover_history_transaction", original_recover)
    assert load_events(event_path) == []
    assert load_change_units(unit_path) == []
    assert not (state_directory / ".ruleloom-history-transaction.json").exists()


def test_concurrent_history_batches_validate_merged_event_ownership_under_lock(
    tmp_path: Path,
) -> None:
    event_path = tmp_path / "events.jsonl"
    unit_path = tmp_path / "change-units.jsonl"
    shared = replace(_event("event.shared"), change_id=None)

    def import_owner(suffix: str) -> str:
        unit = replace(
            _unit(f"change.{suffix}"),
            final_sha=None,
            finalized_at=None,
            event_ids=(shared.id,),
            evidence_quality="git_only",
        )
        try:
            upsert_history_batch(event_path, [shared], unit_path, [unit])
        except ModelError as exc:
            return str(exc)
        return "inserted"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(import_owner, ("first", "second")))

    assert results.count("inserted") == 1
    errors = [result for result in results if result != "inserted"]
    assert len(errors) == 1
    assert "attached to multiple change units" in errors[0]
    assert len(load_change_units(unit_path)) == 1


def test_concurrent_upserts_do_not_lose_records(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    events = [_event(f"event.git.{index}") for index in range(20)]

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda event: upsert_events(path, [event]), events))

    assert sum(inserted for inserted, _ in results) == len(events)
    assert {event.id for event in load_events(path)} == {event.id for event in events}


def test_storage_rejects_duplicate_corrupt_and_symlinked_logs(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    line = canonical_json(_event().to_dict()) + "\n"
    path.write_text(line + line, encoding="utf-8")
    with pytest.raises(ModelError, match="duplicate historical event id"):
        load_events(path)

    path.write_text("{}\n\n", encoding="utf-8")
    with pytest.raises(ModelError):
        load_events(path)

    target = tmp_path / "target.jsonl"
    target.write_text("", encoding="utf-8")
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(ModelError, match="regular file"):
        load_events(path)
