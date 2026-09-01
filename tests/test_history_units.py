from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from ruleloom.history.importing import import_change_units, import_events
from ruleloom.history.models import ChangeUnit, HistoricalEvent
from ruleloom.history.units import (
    assemble_change_units,
    validate_change_unit_evidence,
    validate_unique_event_ownership,
)
from ruleloom.models import ModelError

BASE = "1" * 40
HEAD = "2" * 40
FINAL = "3" * 40


def _event(
    identifier: str,
    kind: str,
    occurred_at: str,
    data: dict[str, object],
    *,
    change_id: str | None = "pr-7",
) -> HistoricalEvent:
    return HistoricalEvent(
        id=identifier,
        repository_id="repo.example",
        kind=kind,
        occurred_at=occurred_at,
        available_at=occurred_at,
        provider="forge",
        source_ref=f"forge/{identifier}",
        change_id=change_id,
        independent_group=change_id or identifier,
        data=data,  # type: ignore[arg-type]
    )


def test_assembles_confirmatory_point_in_time_change() -> None:
    opened = _event(
        "event.opened",
        "change_opened",
        "2025-01-01T00:00:00Z",
        {
            "base_sha": BASE,
            "head_sha": HEAD,
            "point_in_time": True,
            "commits": [HEAD],
        },
    )
    finalized = _event(
        "event.merged",
        "change_merged",
        "2025-01-03T00:00:00Z",
        {"merge_sha": FINAL, "commits": [HEAD, FINAL]},
    )

    unit = ChangeUnit(
        id="pr-7",
        repository_id="repo.example",
        kind="provider_change",
        base_sha=BASE,
        prediction_sha=HEAD,
        prediction_at="2025-01-01T00:00:00Z",
        final_sha=FINAL,
        finalized_at="2025-01-03T00:00:00Z",
        commits=(HEAD, FINAL),
        event_ids=("event.opened", "event.merged"),
        provider="forge",
        source_ref="forge/event.opened",
        evidence_quality="rich",
        confirmatory=True,
    )
    assert assemble_change_units([finalized, opened]) == (unit,)
    validate_change_unit_evidence(unit, [opened, finalized])
    with pytest.raises(ModelError, match="matching persisted finalization"):
        validate_change_unit_evidence(
            unit,
            [opened, replace(finalized, data={"merge_sha": HEAD})],
        )


def test_event_provenance_cannot_be_attached_to_multiple_changes() -> None:
    first = ChangeUnit(
        id="pr-7",
        repository_id="repo.example",
        kind="provider_change",
        base_sha=BASE,
        prediction_sha=HEAD,
        prediction_at="2025-01-01T00:00:00Z",
        commits=(HEAD,),
        event_ids=("event.shared",),
        provider="forge",
        source_ref="forge/event.shared",
        evidence_quality="rich",
        confirmatory=False,
    )
    second = replace(first, id="pr-8")

    with pytest.raises(ModelError, match="attached to multiple change units"):
        validate_unique_event_ownership([first, second])


def test_final_state_only_is_retained_but_not_confirmatory() -> None:
    finalized = _event(
        "event.closed",
        "change_finalized",
        "2025-01-03T00:00:00Z",
        {"base_sha": BASE, "final_sha": FINAL},
    )

    unit = assemble_change_units([finalized])[0]
    assert unit.evidence_quality == "final_only"
    assert unit.confirmatory is False
    assert unit.prediction_sha == FINAL


def test_explicit_outcome_does_not_eclipse_structural_final_event_even_with_sha() -> None:
    opened = _event(
        "event.opened",
        "change_snapshot",
        "2025-01-01T00:00:00Z",
        {"base_sha": BASE, "head_sha": HEAD, "point_in_time": True},
    )
    merged = _event(
        "event.merged",
        "change_merged",
        "2025-01-02T00:00:00Z",
        {"merge_sha": FINAL},
    )
    outcome = _event(
        "event.outcome",
        "change_finalized",
        "2025-01-03T00:00:00Z",
        {
            "target": "validation_rework_required",
            "value": "negative",
            "evidence_complete": True,
            "head_sha": HEAD,
        },
    )

    unit = assemble_change_units([opened, merged, outcome])[0]

    assert unit.final_sha == FINAL
    assert unit.finalized_at == merged.occurred_at
    assert unit.event_ids == (opened.id, merged.id)

    forged = replace(
        unit,
        final_sha=HEAD,
        finalized_at=outcome.occurred_at,
        event_ids=(opened.id, outcome.id),
    )
    with pytest.raises(ModelError, match="matching persisted finalization"):
        validate_change_unit_evidence(forged, [opened, outcome])

    structural_with_confidence = replace(
        merged,
        id="event.merged.confident",
        kind="change_finalized",
        data={"final_sha": FINAL, "confidence": 1.0, "strength": "strong"},
    )
    confident_unit = assemble_change_units([opened, structural_with_confidence])[0]
    assert confident_unit.final_sha == FINAL
    assert confident_unit.finalized_at == structural_with_confidence.occurred_at
    validate_change_unit_evidence(confident_unit, [opened, structural_with_confidence])


def test_assembly_allows_independent_sources_and_ignores_unlinked() -> None:
    opened = _event(
        "event.opened",
        "change_snapshot",
        "2025-01-01T00:00:00Z",
        {"base_sha": BASE, "prediction_sha": HEAD, "point_in_time": True},
    )
    review = replace(
        opened,
        id="event.review",
        kind="review",
        provider="review-system",
        independent_group="reviewer-1",
        data={"decision": "approved"},
    )
    unit = assemble_change_units([opened, review])[0]
    assert unit.provider == "forge"
    assert unit.event_ids == (opened.id,)
    assert assemble_change_units([replace(opened, change_id=None)]) == ()


def test_confirmatory_unit_requires_matching_persisted_snapshot() -> None:
    opened = _event(
        "event.opened",
        "change_snapshot",
        "2025-01-01T00:00:00Z",
        {"base_sha": BASE, "head_sha": HEAD, "point_in_time": True},
    )
    unit = assemble_change_units([opened])[0]

    validate_change_unit_evidence(unit, [opened])
    for invalid_event in (
        replace(opened, data={**opened.data, "point_in_time": False}),
        replace(opened, source_ref="forge/other"),
    ):
        with pytest.raises(ModelError, match="matching persisted point-in-time"):
            validate_change_unit_evidence(unit, [invalid_event])
    with pytest.raises(ModelError, match="not owned by change unit"):
        validate_change_unit_evidence(unit, [replace(opened, change_id="pr-other")])
    with pytest.raises(ModelError, match="missing historical event"):
        validate_change_unit_evidence(
            replace(unit, event_ids=(opened.id, "event.missing")),
            [opened],
        )
    with pytest.raises(ModelError, match="matching persisted point-in-time"):
        validate_change_unit_evidence(replace(unit, event_ids=()), [opened])


def test_late_available_snapshot_is_not_confirmatory() -> None:
    opened = replace(
        _event(
            "event.opened",
            "change_snapshot",
            "2025-01-01T00:00:00Z",
            {"base_sha": BASE, "head_sha": HEAD, "point_in_time": True},
        ),
        available_at="2025-01-03T00:00:00Z",
    )

    unit = assemble_change_units([opened])[0]

    assert unit.evidence_quality == "rich"
    assert unit.confirmatory is False
    with pytest.raises(ModelError, match="matching persisted point-in-time"):
        validate_change_unit_evidence(replace(unit, confirmatory=True), [opened])


def test_explicit_unscoped_event_link_stays_within_repository() -> None:
    opened = _event(
        "event.opened",
        "change_snapshot",
        "2025-01-01T00:00:00Z",
        {"base_sha": BASE, "head_sha": HEAD, "point_in_time": True},
    )
    review = _event(
        "event.review",
        "review",
        "2025-01-02T00:00:00Z",
        {"decision": "changes_requested"},
        change_id=None,
    )
    unit = replace(
        assemble_change_units([opened])[0],
        event_ids=(opened.id, review.id),
    )

    validate_change_unit_evidence(unit, [opened, review])
    with pytest.raises(ModelError, match="not owned by change unit"):
        validate_change_unit_evidence(
            unit,
            [opened, replace(review, repository_id="repo.other")],
        )


def test_exploratory_unit_does_not_require_snapshot_attestation() -> None:
    finalized = _event(
        "event.closed",
        "change_finalized",
        "2025-01-03T00:00:00Z",
        {"base_sha": BASE, "final_sha": FINAL},
    )
    unit = assemble_change_units([finalized])[0]

    validate_change_unit_evidence(unit, [finalized])


def test_strict_normalized_import_round_trip_and_duplicate_rejection(tmp_path: Path) -> None:
    event = _event(
        "event.opened",
        "change_opened",
        "2025-01-01T00:00:00Z",
        {"base_sha": BASE, "head_sha": HEAD, "point_in_time": True},
    )
    unit = assemble_change_units([event])[0]
    events_file = tmp_path / "events.jsonl"
    events_file.write_text(json.dumps(event.to_dict()) + "\n", encoding="utf-8")
    units_file = tmp_path / "units.jsonl"
    units_file.write_text(json.dumps(unit.to_dict()) + "\n", encoding="utf-8")

    assert import_events(events_file) == (event,)
    assert import_change_units(units_file) == (unit,)
    events_file.write_text(
        json.dumps(event.to_dict()) + "\n" + json.dumps(event.to_dict()) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ModelError, match="duplicate historical event"):
        import_events(events_file)


def test_import_rejects_symlinks_and_blank_lines(tmp_path: Path) -> None:
    event = _event(
        "event.opened",
        "change_opened",
        "2025-01-01T00:00:00Z",
        {"base_sha": BASE, "head_sha": HEAD, "point_in_time": True},
    )
    target = tmp_path / "events.jsonl"
    target.write_text(json.dumps(event.to_dict()) + "\n\n", encoding="utf-8")
    with pytest.raises(ModelError, match="blank historical event"):
        import_events(target)
    link = tmp_path / "link.jsonl"
    link.symlink_to(target)
    with pytest.raises(ModelError, match="non-symlink"):
        import_events(link)
