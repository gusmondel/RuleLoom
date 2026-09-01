"""Assemble provider-neutral logical changes from normalized history events."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping, Sequence

from ruleloom.history.models import (
    ChangeUnit,
    EvidenceQuality,
    HistoricalEvent,
    validate_git_sha,
)
from ruleloom.models import JsonValue, ModelError, parse_timestamp

_PREDICTION_EVENTS = frozenset({"change_opened", "change_snapshot"})
_FINAL_EVENTS = frozenset({"change_finalized", "change_merged", "change_closed"})
_VERIFIABLE_FINAL_EVENTS = _FINAL_EVENTS | {"git_merge"}
_BUILTIN_GITHUB_ADAPTER = "ruleloom-github/1"
_BUILTIN_GITHUB_UNIT_SOURCE_RE = re.compile(
    r"^github:(github\.github\.com\.repo\.[0-9a-f]{20}):pull:[0-9]+(?:$|:)"
)
_BUILTIN_GITHUB_EVENT_SOURCE_RE = re.compile(
    r"^github:(github\.github\.com\.repo\.[0-9a-f]{20}):"
    r"(?:pull:[0-9]+|commit:[0-9a-f]{40}|commit:[0-9a-f]{64})(?:$|:)"
)


def _string(data: dict[str, JsonValue], key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) and value else None


def _sha(data: dict[str, JsonValue], *keys: str) -> str | None:
    for key in keys:
        value = _string(data, key)
        if value is not None:
            return validate_git_sha(value, field_name=f"historical event data.{key}")
    return None


def _commits(events: Sequence[HistoricalEvent]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for event in events:
        raw = event.data.get("commits", [])
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            if "commits" in event.data:
                raise ModelError(f"historical event {event.id!r} data.commits must be strings")
            continue
        for value in raw:
            assert isinstance(value, str)
            commit = value
            validate_git_sha(commit, field_name=f"historical event {event.id!r} commit")
            if commit not in seen:
                result.append(commit)
                seen.add(commit)
    return tuple(result)


def _is_semantic_outcome_event(event: HistoricalEvent) -> bool:
    """Keep explicit outcome records out of structural finalization evidence."""
    return event.kind == "change_finalized" and "target" in event.data


def validate_change_unit_evidence(
    unit: ChangeUnit,
    events: Sequence[HistoricalEvent],
) -> None:
    """Require persisted point-in-time and structural finalization evidence."""
    by_id = {event.id: event for event in events}
    if len(by_id) != len(events):
        raise ModelError("historical events must have unique ids")
    validate_change_unit_event_links(unit, by_id)
    if unit.confirmatory:
        supporting: list[str] = []
        for event_id in unit.event_ids:
            event = by_id.get(event_id)
            if event is None or event.kind not in _PREDICTION_EVENTS:
                continue
            if (
                event.repository_id != unit.repository_id
                or event.change_id != unit.id
                or event.provider != unit.provider
                or event.source_ref != unit.source_ref
                or event.data.get("point_in_time") is not True
                or _sha(event.data, "base_sha") != unit.base_sha
                or _sha(event.data, "prediction_sha", "head_sha") != unit.prediction_sha
                or parse_timestamp(event.occurred_at) != parse_timestamp(unit.prediction_at)
                or parse_timestamp(event.available_at) > parse_timestamp(unit.prediction_at)
            ):
                continue
            supporting.append(event.id)
        if not supporting:
            raise ModelError(
                f"confirmatory change unit {unit.id!r} lacks a matching persisted "
                "point-in-time snapshot event"
            )
    if unit.final_sha is None:
        return
    assert unit.finalized_at is not None
    final_support = []
    for event_id in unit.event_ids:
        event = by_id.get(event_id)
        if (
            event is None
            or event.kind not in _VERIFIABLE_FINAL_EVENTS
            or _is_semantic_outcome_event(event)
        ):
            continue
        if (
            event.repository_id != unit.repository_id
            or event.change_id != unit.id
            or event.provider != unit.provider
            or _sha(event.data, "final_sha", "merge_sha", "head_sha", "sha") != unit.final_sha
            or parse_timestamp(event.occurred_at) != parse_timestamp(unit.finalized_at)
        ):
            continue
        final_support.append(event.id)
    if not final_support:
        raise ModelError(f"change unit {unit.id!r} lacks a matching persisted finalization event")


def validate_change_unit_event_links(
    unit: ChangeUnit,
    events_by_id: Mapping[str, HistoricalEvent],
) -> None:
    """Require every attached event to exist and stay within the unit boundary."""
    for event_id in unit.event_ids:
        event = events_by_id.get(event_id)
        if event is None:
            raise ModelError(
                f"change unit {unit.id!r} references missing historical event {event_id!r}"
            )
        if event.repository_id != unit.repository_id or event.change_id not in {None, unit.id}:
            raise ModelError(
                f"historical event {event_id!r} is not owned by change unit {unit.id!r}"
            )


def validate_unique_event_ownership(units: Sequence[ChangeUnit]) -> None:
    """Prevent one attached event from supporting multiple logical changes."""
    owners: dict[str, str] = {}
    for unit in units:
        for event_id in unit.event_ids:
            previous = owners.setdefault(event_id, unit.id)
            if previous != unit.id:
                raise ModelError(
                    f"historical event {event_id!r} is attached to multiple change units: "
                    f"{previous!r}, {unit.id!r}"
                )


def validate_history_snapshot(
    events: Sequence[HistoricalEvent],
    units: Sequence[ChangeUnit],
) -> None:
    """Validate all cross-log ownership, links, and evidence as one snapshot."""

    events_by_id = {event.id: event for event in events}
    if len(events_by_id) != len(events):
        raise ModelError("historical events must have unique ids")
    validate_unique_event_ownership(units)
    github_keys_by_repository: dict[str, set[str]] = defaultdict(set)
    for unit in units:
        if unit.kind != "github_archive_change":
            continue
        match = _BUILTIN_GITHUB_UNIT_SOURCE_RE.match(unit.source_ref)
        if unit.provider != "github" or match is None:
            raise ModelError(
                f"built-in GitHub change unit {unit.id!r} has invalid provider provenance"
            )
        github_keys_by_repository[unit.repository_id].add(match.group(1))
    for event in events:
        if event.data.get("adapter") != _BUILTIN_GITHUB_ADAPTER:
            continue
        match = _BUILTIN_GITHUB_EVENT_SOURCE_RE.match(event.source_ref)
        if event.provider != "github" or match is None:
            raise ModelError(f"built-in GitHub event {event.id!r} has invalid provider provenance")
        github_keys_by_repository[event.repository_id].add(match.group(1))
    for repository_id, keys in github_keys_by_repository.items():
        if len(keys) > 1:
            raise ModelError(
                f"repository {repository_id!r} contains multiple built-in GitHub "
                "repository identities; start a new experiment for a different provider repo"
            )
    events_by_change: dict[tuple[str, str], list[HistoricalEvent]] = defaultdict(list)
    for event in events:
        if event.change_id is not None:
            events_by_change[(event.repository_id, event.change_id)].append(event)
    for unit in units:
        validate_change_unit_event_links(unit, events_by_id)
        linked = {
            event.id: event for event in events_by_change.get((unit.repository_id, unit.id), ())
        }
        for event_id in unit.event_ids:
            attached_event = events_by_id[event_id]
            if attached_event.change_id is None:
                linked[attached_event.id] = attached_event
        validate_change_unit_evidence(unit, tuple(linked.values()))


def assemble_change_units(events: Sequence[HistoricalEvent]) -> tuple[ChangeUnit, ...]:
    """Build one immutable unit per normalized provider ``change_id``.

    A confirmatory unit requires a persisted ``change_opened`` or
    ``change_snapshot`` event whose data contains ``base_sha``, ``head_sha`` (or
    ``prediction_sha``), and ``point_in_time: true``. If only a final event is
    available, the reconstructed unit is retained as ``final_only`` and cannot
    support policy approval.
    """

    grouped: dict[tuple[str, str], list[HistoricalEvent]] = defaultdict(list)
    for event in events:
        if event.change_id is not None:
            grouped[(event.repository_id, event.change_id)].append(event)

    units: list[ChangeUnit] = []
    for (repository_id, change_id), group in sorted(grouped.items()):
        ordered = sorted(
            group,
            key=lambda item: (
                parse_timestamp(item.occurred_at),
                parse_timestamp(item.available_at),
                item.id,
            ),
        )
        prediction_event: HistoricalEvent | None = None
        base_sha: str | None = None
        prediction_sha: str | None = None
        for event in ordered:
            if event.kind not in _PREDICTION_EVENTS:
                continue
            candidate_base = _sha(event.data, "base_sha")
            candidate_head = _sha(event.data, "prediction_sha", "head_sha")
            if candidate_base is not None and candidate_head is not None:
                prediction_event = event
                base_sha = candidate_base
                prediction_sha = candidate_head
                break

        final_event: HistoricalEvent | None = None
        final_sha: str | None = None
        for candidate in reversed(ordered):
            if candidate.kind not in _FINAL_EVENTS or _is_semantic_outcome_event(candidate):
                continue
            candidate_sha = _sha(candidate.data, "final_sha", "merge_sha", "head_sha")
            if candidate_sha is not None:
                final_event = candidate
                final_sha = candidate_sha
                break

        point_in_time = (
            prediction_event is not None
            and prediction_event.data.get("point_in_time") is True
            and parse_timestamp(prediction_event.available_at)
            <= parse_timestamp(prediction_event.occurred_at)
        )
        quality: EvidenceQuality = "rich"
        confirmatory = point_in_time
        if prediction_event is None:
            if final_event is None:
                continue
            base_sha = _sha(final_event.data, "base_sha")
            prediction_sha = final_sha
            if base_sha is None or prediction_sha is None:
                continue
            prediction_event = final_event
            quality = "final_only"
            confirmatory = False

        assert prediction_event is not None
        assert base_sha is not None
        assert prediction_sha is not None
        if final_event is not None and parse_timestamp(final_event.occurred_at) < parse_timestamp(
            prediction_event.occurred_at
        ):
            raise ModelError(f"change {change_id!r} final event predates its prediction snapshot")

        commits = _commits(ordered)
        if not commits:
            commits = (
                (prediction_sha,)
                if final_sha is None
                else tuple(dict.fromkeys((prediction_sha, final_sha)))
            )
        units.append(
            ChangeUnit(
                id=change_id,
                repository_id=repository_id,
                kind="provider_change" if quality == "rich" else "final_change",
                base_sha=base_sha,
                prediction_sha=prediction_sha,
                prediction_at=prediction_event.occurred_at,
                final_sha=final_sha,
                finalized_at=final_event.occurred_at if final_event is not None else None,
                commits=commits,
                event_ids=tuple(
                    dict.fromkeys(
                        (
                            prediction_event.id,
                            *(() if final_event is None else (final_event.id,)),
                        )
                    )
                ),
                provider=prediction_event.provider,
                source_ref=prediction_event.source_ref,
                evidence_quality=quality,
                confirmatory=confirmatory,
            )
        )
    return tuple(units)
