"""Materialize leakage-aware observations from historical change units."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from ruleloom.config import RuleLoomConfig
from ruleloom.gitfacts import (
    GitFactsError,
    MissingPromisorObjectsError,
    SnapshotRepositoryContext,
    collect_snapshot,
    collect_snapshot_with_aggregate_stats,
    prepare_snapshot_repository,
)
from ruleloom.history.models import ChangeUnit, HistoricalEvent
from ruleloom.history.outcomes import (
    ATOMIC_OUTCOME_TARGETS,
    VALIDATION_REWORK_REQUIRED,
    GitWindow,
    OutcomeDerivation,
    aggregate_votes,
    derive_outcome,
    git_window_from_events,
)
from ruleloom.history.units import (
    validate_change_unit_event_links,
    validate_change_unit_evidence,
    validate_unique_event_ownership,
)
from ruleloom.models import (
    JsonObject,
    JsonValue,
    LabelValue,
    ModelError,
    Observation,
    content_hash,
    parse_timestamp,
)

_MAX_SKIP_PREVIEW = 50
_TARGET_ALIASES = {"needs_extra_validation": VALIDATION_REWORK_REQUIRED}
_EVENT_ARCHIVE_ADAPTER_V2 = "ruleloom-github-event-archive/2"

AggregateDiffStatistics = tuple[int, int, int, str]


@dataclass(frozen=True, slots=True)
class MaterializationReport:
    observations: tuple[Observation, ...]
    examined: int
    positive: int
    negative: int
    unknown: int
    confirmatory: int
    skipped: int
    skipped_by_reason: tuple[tuple[str, int], ...]
    skipped_preview: tuple[tuple[str, str], ...]
    outcome_target: str
    weak_evidence_enabled: bool
    manifest_hash: str
    eligible_positive: int
    eligible_negative: int
    eligible_unknown: int
    git_window: GitWindow | None = None
    git_window_negatives: int = 0

    @staticmethod
    def _retention(eligible: int, retained: int) -> JsonObject:
        return {
            "eligible": eligible,
            "retained": retained,
            "skipped": eligible - retained,
            "rate": None if eligible == 0 else retained / eligible,
        }

    def to_dict(self) -> JsonObject:
        return {
            "examined": self.examined,
            "materialized": len(self.observations),
            "positive": self.positive,
            "negative": self.negative,
            "unknown": self.unknown,
            "confirmatory": self.confirmatory,
            "exploratory": len(self.observations) - self.confirmatory,
            "skipped": self.skipped,
            "skipped_by_reason": dict(self.skipped_by_reason),
            "skipped_preview": [list(item) for item in self.skipped_preview],
            "outcome_target": self.outcome_target,
            "weak_evidence_enabled": self.weak_evidence_enabled,
            "manifest_hash": self.manifest_hash,
            "git_window": None if self.git_window is None else self.git_window.to_dict(),
            "git_window_negatives": self.git_window_negatives,
            "retention_by_outcome": {
                "positive": self._retention(self.eligible_positive, self.positive),
                "negative": self._retention(self.eligible_negative, self.negative),
                "unknown": self._retention(self.eligible_unknown, self.unknown),
                "interpretation": ("descriptive_missingness_diagnostic_before_git_materialization"),
            },
        }


def resolve_outcome_target(configured_target: str, requested: str | None = None) -> str:
    """Resolve only the atomic outcome registered by the frozen target."""
    registered = _TARGET_ALIASES.get(configured_target, configured_target)
    if registered not in ATOMIC_OUTCOME_TARGETS:
        raise ModelError(
            f"configured target {configured_target!r} has no registered historical outcome; "
            "start a separate experiment whose target is one of: "
            + ", ".join(ATOMIC_OUTCOME_TARGETS)
        )
    if requested is not None and requested != registered:
        raise ModelError(
            f"requested historical outcome {requested!r} does not match the frozen "
            f"configured target {configured_target!r} ({registered!r}); start a separate "
            "experiment instead of reinterpreting labels"
        )
    return registered


def _historical_observation(
    root: Path,
    config: RuleLoomConfig,
    unit: ChangeUnit,
    derivation: OutcomeDerivation,
    *,
    outcome_target: str,
    event_manifest_hash: str,
    weak_evidence_enabled: bool,
    aggregate_statistics: AggregateDiffStatistics | None,
    repository_context: SnapshotRepositoryContext,
    git_window: GitWindow | None,
) -> Observation:
    if aggregate_statistics is None:
        snapshot = collect_snapshot(
            root,
            unit.base_sha,
            unit.prediction_sha,
            protocol_hash=config.evidence_protocol_hash,
            target=config.target,
            pack=config.pack,
            pack_version=config.pack_version,
            pack_config=config.pack_config,
            evidence_config=config.evidence,
            repository_id=config.protocol.repository_id,
            include_topological_index=False,
            context=repository_context,
        )
    else:
        additions, deletions, files_changed, statistics_source = aggregate_statistics
        snapshot = collect_snapshot_with_aggregate_stats(
            root,
            unit.base_sha,
            unit.prediction_sha,
            additions=additions,
            deletions=deletions,
            files_changed=files_changed,
            statistics_source=statistics_source,
            observed_at=unit.prediction_at,
            protocol_hash=config.evidence_protocol_hash,
            target=config.target,
            pack=config.pack,
            pack_version=config.pack_version,
            pack_config=config.pack_config,
            evidence_config=config.evidence,
            repository_id=config.protocol.repository_id,
            context=repository_context,
        )
    evidence = derivation.evidence
    label = derivation.value
    warnings: list[str] = []
    if evidence is not None and parse_timestamp(evidence.available_at) <= parse_timestamp(
        unit.prediction_at
    ):
        warnings.append("derived outcome did not strictly postdate the prediction point")
        evidence = None
        label = LabelValue.UNKNOWN

    strong_only = aggregate_votes(outcome_target, derivation.votes, include_weak=False)
    weak_votes_contributed = (
        weak_evidence_enabled and label is not LabelValue.UNKNOWN and strong_only.value is not label
    )
    confirmatory = unit.confirmatory and not weak_votes_contributed
    source: JsonObject = {
        **snapshot.source,
        "kind": "historical_change",
        "change_id": unit.id,
        "unit_kind": unit.kind,
        "provider": unit.provider,
        "source_ref": unit.source_ref,
        "evidence_quality": unit.evidence_quality,
        "confirmatory": confirmatory,
        "historical_outcome_target": outcome_target,
    }
    snapshot_metadata = {
        key: value for key, value in snapshot.metadata.items() if key != "topological_index"
    }
    metadata: JsonObject = {
        **snapshot_metadata,
        "historical_prediction_at": unit.prediction_at,
        "historical_finalized_at": unit.finalized_at,
        "historical_event_manifest_hash": event_manifest_hash,
        "historical_event_ids": list(unit.event_ids),
        "historical_votes": [vote.to_dict() for vote in derivation.votes],
        "weak_evidence_enabled": weak_evidence_enabled,
        "historical_git_window": None if git_window is None else git_window.to_dict(),
        "history_warnings": cast(JsonValue, warnings),
    }
    return replace(
        snapshot,
        id=f"history.{unit.id}",
        observed_at=unit.prediction_at,
        labels={config.target: label},
        label_evidence={} if evidence is None else {config.target: evidence},
        source=source,
        metadata=metadata,
    )


def _aggregate_diff_statistics(
    unit: ChangeUnit,
    events: tuple[HistoricalEvent, ...],
) -> AggregateDiffStatistics | None:
    snapshots = [
        event
        for event in events
        if event.kind == "change_snapshot"
        and event.data.get("base_sha") == unit.base_sha
        and event.data.get("head_sha") == unit.prediction_sha
    ]
    if len(snapshots) != 1 or snapshots[0].data.get("adapter") != _EVENT_ARCHIVE_ADAPTER_V2:
        return None
    raw = snapshots[0].data.get("diff_statistics")
    if not isinstance(raw, dict):
        raise ModelError("event-archive v2 prediction snapshot lacks diff_statistics")
    expected = {"additions", "deletions", "files_changed", "complete", "source"}
    if set(raw) != expected:
        raise ModelError("event-archive v2 diff_statistics has an invalid schema")
    complete = raw.get("complete")
    if not isinstance(complete, bool):
        raise ModelError("event-archive v2 diff_statistics.complete must be a boolean")
    if not complete:
        return None
    values = (raw.get("additions"), raw.get("deletions"), raw.get("files_changed"))
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
        raise ModelError("event-archive v2 diff statistics must be non-negative integers")
    source = raw.get("source")
    if source != "github_event_archive_opened_event":
        raise ModelError("event-archive v2 diff statistics has an unexpected source")
    additions, deletions, files_changed = cast(tuple[int, int, int], values)
    return additions, deletions, files_changed, source


def _skip_reason_code(reason: str) -> str:
    if reason.startswith("missing local Git commit objects:"):
        return "missing_commit_objects"
    if reason.startswith("aggregate changed_files does not match"):
        return "aggregate_path_count_mismatch"
    if reason.startswith("aggregate diff statistics include files excluded"):
        return "aggregate_scope_exclusions"
    if "configured evidence scope" in reason:
        return "ineligible_evidence_scope"
    return "git_evidence_error"


def resolve_git_window(
    config: RuleLoomConfig,
    events: tuple[HistoricalEvent, ...] | list[HistoricalEvent],
) -> GitWindow | None:
    """Resolve the registered Git revert window against the persisted history horizon."""
    return git_window_from_events(
        events,
        window_days=config.outcomes.git_window_days,
        repository_id=config.protocol.repository_id,
    )


def validate_materialized_outcome(
    config: RuleLoomConfig,
    observation: Observation,
    unit: ChangeUnit,
    events: tuple[HistoricalEvent, ...] | list[HistoricalEvent],
    *,
    git_window: GitWindow | None = None,
) -> None:
    """Recompute a persisted historical label and its confirmatory status."""
    validate_change_unit_evidence(unit, events)
    recorded_target = observation.source.get("historical_outcome_target")
    if not isinstance(recorded_target, str):
        raise ModelError(
            f"historical observation {observation.id!r} lacks its registered outcome target"
        )
    selected_target = resolve_outcome_target(config.target, recorded_target)
    weak_evidence_enabled = observation.metadata.get("weak_evidence_enabled")
    if not isinstance(weak_evidence_enabled, bool):
        raise ModelError(
            f"historical observation {observation.id!r} lacks a weak-evidence decision"
        )
    derivation = derive_outcome(
        unit,
        events,
        selected_target,
        include_weak=weak_evidence_enabled,
        git_window=git_window,
    )
    strong_only = aggregate_votes(selected_target, derivation.votes, include_weak=False)
    weak_votes_contributed = (
        weak_evidence_enabled
        and derivation.value is not LabelValue.UNKNOWN
        and strong_only.value is not derivation.value
    )
    expected_confirmatory = unit.confirmatory and not weak_votes_contributed
    expected_window = None if git_window is None else git_window.to_dict()
    event_manifest_hash = content_hash(
        [event.to_dict() for event in sorted(events, key=lambda item: item.id)]
    )
    expected_evidence = derivation.evidence
    actual_evidence = observation.label_evidence.get(config.target)
    if (
        observation.labels.get(config.target, LabelValue.UNKNOWN) is not derivation.value
        or actual_evidence != expected_evidence
        or observation.source.get("confirmatory") is not expected_confirmatory
        or observation.metadata.get("historical_event_manifest_hash") != event_manifest_hash
        or observation.metadata.get("historical_votes")
        != [vote.to_dict() for vote in derivation.votes]
        or observation.metadata.get("historical_event_ids") != list(unit.event_ids)
        or observation.metadata.get("historical_prediction_at") != unit.prediction_at
        or observation.metadata.get("historical_finalized_at") != unit.finalized_at
        or observation.metadata.get("historical_git_window") != expected_window
    ):
        raise ModelError(
            f"historical observation {observation.id!r} does not match its recomputed "
            "outcome evidence"
        )


def materialize_history(
    root: Path,
    config: RuleLoomConfig,
    units: tuple[ChangeUnit, ...] | list[ChangeUnit],
    events: tuple[HistoricalEvent, ...] | list[HistoricalEvent],
    *,
    outcome_target: str | None = None,
    include_weak: bool = False,
) -> MaterializationReport:
    """Re-extract prediction-time facts and attach conservative delayed labels."""
    selected_target = resolve_outcome_target(config.target, outcome_target)
    event_values = tuple(events)
    unit_values = tuple(units)
    event_ids = [event.id for event in event_values]
    if len(event_ids) != len(set(event_ids)):
        raise ModelError("historical events must have unique ids")
    unit_ids = [unit.id for unit in unit_values]
    if len(unit_ids) != len(set(unit_ids)):
        raise ModelError("historical change units must have unique ids")
    validate_unique_event_ownership(unit_values)
    for event in event_values:
        if event.repository_id != config.protocol.repository_id:
            raise ModelError(
                f"historical event {event.id!r} belongs to repository "
                f"{event.repository_id!r}, not {config.protocol.repository_id!r}"
            )
    events_by_id = {event.id: event for event in event_values}
    events_by_change: dict[tuple[str, str], list[HistoricalEvent]] = defaultdict(list)
    for event in event_values:
        if event.change_id is not None:
            events_by_change[(event.repository_id, event.change_id)].append(event)
    for unit in unit_values:
        if unit.repository_id != config.protocol.repository_id:
            raise ModelError(
                f"change unit {unit.id!r} belongs to repository {unit.repository_id!r}, "
                f"not {config.protocol.repository_id!r}"
            )
        validate_change_unit_event_links(unit, events_by_id)
    complete_event_manifest_hash = content_hash(
        [event.to_dict() for event in sorted(event_values, key=lambda item: item.id)]
    )
    observations: list[Observation] = []
    skipped_preview: list[tuple[str, str]] = []
    skipped_by_reason: dict[str, int] = defaultdict(int)
    skipped_manifest = hashlib.sha256()
    counts = {LabelValue.POSITIVE: 0, LabelValue.NEGATIVE: 0, LabelValue.UNKNOWN: 0}
    eligible_counts = {
        LabelValue.POSITIVE: 0,
        LabelValue.NEGATIVE: 0,
        LabelValue.UNKNOWN: 0,
    }
    ordered_units = sorted(
        unit_values, key=lambda item: (parse_timestamp(item.prediction_at), item.id)
    )
    required_objects = tuple(
        sorted(
            {
                object_id
                for unit in ordered_units
                for object_id in (unit.base_sha, unit.prediction_sha)
            }
        )
    )
    repository_context = prepare_snapshot_repository(
        root,
        config.protocol.repository_id,
        required_objects,
    )
    missing_objects = repository_context.missing_object_ids
    git_window = resolve_git_window(config, event_values)
    git_window_negatives = 0
    for unit in ordered_units:
        linked = {
            event.id: event for event in events_by_change.get((unit.repository_id, unit.id), ())
        }
        for event_id in unit.event_ids:
            attached_event = events_by_id[event_id]
            if attached_event.change_id is None:
                linked[attached_event.id] = attached_event
        unit_events = tuple(linked.values())
        validate_change_unit_evidence(unit, unit_events)
        unit_event_manifest_hash = content_hash(
            [event.to_dict() for event in sorted(unit_events, key=lambda item: item.id)]
        )
        derivation = derive_outcome(
            unit,
            unit_events,
            selected_target,
            include_weak=include_weak,
            git_window=git_window,
        )
        eligible_counts[derivation.value] += 1
        unit_missing = tuple(
            object_id
            for object_id in (unit.base_sha, unit.prediction_sha)
            if object_id in missing_objects
        )
        if unit_missing:
            reason = "missing local Git commit objects: " + ",".join(unit_missing)
            skipped_by_reason[_skip_reason_code(reason)] += 1
            skipped_manifest.update(unit.id.encode())
            skipped_manifest.update(b"\x00")
            skipped_manifest.update(reason.encode())
            skipped_manifest.update(b"\n")
            if len(skipped_preview) < _MAX_SKIP_PREVIEW:
                skipped_preview.append((unit.id, reason))
            continue
        try:
            observation = _historical_observation(
                root,
                config,
                unit,
                derivation,
                outcome_target=selected_target,
                event_manifest_hash=unit_event_manifest_hash,
                weak_evidence_enabled=include_weak,
                aggregate_statistics=_aggregate_diff_statistics(unit, unit_events),
                repository_context=repository_context,
                git_window=git_window,
            )
        except MissingPromisorObjectsError:
            # This is a cohort-level environment problem, not unit-level missingness.
            # Failing the transaction prevents hundreds of one-object network fetches
            # and avoids retaining a path-dependent subset of the history.
            raise
        except GitFactsError as exc:
            reason = str(exc)
            skipped_by_reason[_skip_reason_code(reason)] += 1
            skipped_manifest.update(unit.id.encode())
            skipped_manifest.update(b"\x00")
            skipped_manifest.update(reason.encode())
            skipped_manifest.update(b"\n")
            if len(skipped_preview) < _MAX_SKIP_PREVIEW:
                skipped_preview.append((unit.id, reason))
            continue
        observations.append(observation)
        counts[observation.labels[config.target]] += 1
        evidence = observation.label_evidence.get(config.target)
        if (
            git_window is not None
            and observation.labels[config.target] is LabelValue.NEGATIVE
            and evidence is not None
            and evidence.source == "historical-events:" + git_window.horizon_event_id
        ):
            git_window_negatives += 1

    manifest: dict[str, JsonValue] = {
        "schema_version": 1,
        "repository_id": config.protocol.repository_id,
        "evidence_protocol_hash": config.evidence_protocol_hash,
        "outcome_target": selected_target,
        "weak_evidence_enabled": include_weak,
        "git_window": None if git_window is None else git_window.to_dict(),
        "event_manifest_hash": complete_event_manifest_hash,
        "unit_ids": [item.id for item in ordered_units],
        "observation_ids": [item.id for item in observations],
        "skipped_manifest_hash": skipped_manifest.hexdigest(),
        "eligible_outcomes": {
            value.value: eligible_counts[value]
            for value in (LabelValue.POSITIVE, LabelValue.NEGATIVE, LabelValue.UNKNOWN)
        },
    }
    return MaterializationReport(
        observations=tuple(observations),
        examined=len(unit_values),
        positive=counts[LabelValue.POSITIVE],
        negative=counts[LabelValue.NEGATIVE],
        unknown=counts[LabelValue.UNKNOWN],
        confirmatory=sum(item.source.get("confirmatory") is True for item in observations),
        skipped=len(unit_values) - len(observations),
        skipped_by_reason=tuple(sorted(skipped_by_reason.items())),
        skipped_preview=tuple(skipped_preview),
        outcome_target=selected_target,
        weak_evidence_enabled=include_weak,
        manifest_hash=content_hash(manifest),
        eligible_positive=eligible_counts[LabelValue.POSITIVE],
        eligible_negative=eligible_counts[LabelValue.NEGATIVE],
        eligible_unknown=eligible_counts[LabelValue.UNKNOWN],
        git_window=git_window,
        git_window_negatives=git_window_negatives,
    )
