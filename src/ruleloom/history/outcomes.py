"""Conservative, language-neutral outcome derivation from historical events.

This module consumes normalized events; it never inspects source code, file
extensions, framework names, or free-form review/commit text.  Provider adapters
may emit the following semantic event taxonomy:

``review``
    ``decision``, ``category``, and ``independent``.  An independent
    ``changes_requested`` decision in category ``validation`` is strong evidence
    for validation rework.
``ci_run``
    ``check_id``, ``conclusion``, and (for failures)
    ``attributable_to_change``.  A strong CI outcome additionally requires a
    later code-changing snapshot and a later successful run of the same check.
``change_snapshot``
    Boolean ``code_changed`` and ``test_changed`` signals.  A test change alone
    is deliberately weak evidence.
``change_finalized``
    An optional explicit matured outcome with ``target``, ``value``,
    ``evidence_complete``, and optional ``strength`` and ``confidence``.  Mere
    finalization never implies a negative label.
``revert``
    ``linked_change_id`` and ``link_kind``.  Only ``link_kind=explicit`` is
    strong evidence.
``incident``
    ``category`` (``hotfix`` or ``defect``), ``linked_change_id``, and
    ``link_kind``.  ``explicit`` is strong; ``fix_keyword`` and ``szz`` are weak.

Malformed or incomplete semantic events abstain.  Absence of an event also
abstains: it is never converted into a negative label.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Literal, cast

from ruleloom.history.models import ChangeUnit, HistoricalEvent
from ruleloom.models import (
    JsonObject,
    LabelEvidence,
    LabelValue,
    ModelError,
    parse_timestamp,
    validate_predicate,
    validate_subject,
    validate_timestamp,
)

VALIDATION_REWORK_REQUIRED = "validation_rework_required"
CHANGE_ATTRIBUTABLE_CI_FAILURE = "change_attributable_ci_failure"
POST_MERGE_REVERT_OR_HOTFIX = "post_merge_revert_or_hotfix"
POST_MERGE_DEFECT = "post_merge_defect"

ATOMIC_OUTCOME_TARGETS = (
    VALIDATION_REWORK_REQUIRED,
    CHANGE_ATTRIBUTABLE_CI_FAILURE,
    POST_MERGE_REVERT_OR_HOTFIX,
    POST_MERGE_DEFECT,
)

VoteValue = Literal["positive", "negative", "abstain"]
VoteStrength = Literal["strong", "weak"]

_VOTE_VALUES = frozenset({"positive", "negative", "abstain"})
_VOTE_STRENGTHS = frozenset({"strong", "weak"})
_WEAK_LINK_KINDS = frozenset({"fix_keyword", "szz"})


@dataclass(frozen=True, slots=True)
class OutcomeVote:
    """One deterministic labeling-function vote with auditable provenance."""

    value: VoteValue
    strength: VoteStrength
    target: str
    available_at: str
    source_kind: str
    event_ids: tuple[str, ...]
    independent_group: str
    confidence: float
    reason: str

    def __post_init__(self) -> None:
        if self.value not in _VOTE_VALUES:
            raise ModelError("outcome vote value must be positive, negative, or abstain")
        if self.strength not in _VOTE_STRENGTHS:
            raise ModelError("outcome vote strength must be strong or weak")
        validate_predicate(self.target, field_name="outcome vote target")
        validate_timestamp(self.available_at)
        validate_subject(self.source_kind)
        validate_subject(self.independent_group)
        if len(self.event_ids) != len(set(self.event_ids)):
            raise ModelError("outcome vote event_ids cannot contain duplicates")
        for event_id in self.event_ids:
            validate_subject(event_id)
        if self.value != "abstain" and not self.event_ids:
            raise ModelError("non-abstaining outcome votes require event provenance")
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, int | float)
            or not math.isfinite(float(self.confidence))
            or not 0 <= self.confidence <= 1
        ):
            raise ModelError("outcome vote confidence must be between 0 and 1")
        if not self.reason.strip():
            raise ModelError("outcome vote reason cannot be empty")

    def to_dict(self) -> JsonObject:
        return {
            "value": self.value,
            "strength": self.strength,
            "target": self.target,
            "available_at": self.available_at,
            "source_kind": self.source_kind,
            "event_ids": list(self.event_ids),
            "independent_group": self.independent_group,
            "confidence": self.confidence,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class OutcomeDerivation:
    """A compatible RuleLoom label plus every vote considered to derive it."""

    target: str
    value: LabelValue
    evidence: LabelEvidence | None
    votes: tuple[OutcomeVote, ...]
    weak_evidence_enabled: bool

    def __post_init__(self) -> None:
        validate_predicate(self.target, field_name="outcome derivation target")
        if not isinstance(self.value, LabelValue):
            raise ModelError("outcome derivation value must be a LabelValue")
        if not isinstance(self.weak_evidence_enabled, bool):
            raise ModelError("weak_evidence_enabled must be a boolean")
        if any(vote.target != self.target for vote in self.votes):
            raise ModelError("outcome derivation votes must match its target")
        if self.value is LabelValue.UNKNOWN and self.evidence is not None:
            raise ModelError("unknown outcome derivations cannot have label evidence")
        if self.value is not LabelValue.UNKNOWN and self.evidence is None:
            raise ModelError("known outcome derivations require label evidence")

    @property
    def label(self) -> LabelValue:
        """Alias for callers that name the result as a label."""
        return self.value

    @property
    def label_evidence(self) -> LabelEvidence | None:
        """Alias matching :class:`ruleloom.models.Observation`."""
        return self.evidence

    def to_dict(self) -> JsonObject:
        return {
            "target": self.target,
            "value": self.value.value,
            "evidence": None if self.evidence is None else self.evidence.to_dict(),
            "votes": [vote.to_dict() for vote in self.votes],
            "weak_evidence_enabled": self.weak_evidence_enabled,
        }


def _event_sort_key(event: HistoricalEvent) -> tuple[object, ...]:
    return (
        parse_timestamp(event.occurred_at),
        parse_timestamp(event.available_at),
        event.id,
    )


def _vote_sort_key(vote: OutcomeVote) -> tuple[object, ...]:
    return (
        parse_timestamp(vote.available_at),
        vote.independent_group,
        vote.source_kind,
        vote.event_ids,
        vote.strength,
        vote.value,
    )


def _latest_timestamp(events: Sequence[HistoricalEvent]) -> str:
    return max(events, key=lambda event: parse_timestamp(event.available_at)).available_at


def _event_confidence(
    event: HistoricalEvent,
    *,
    default: float,
    maximum: float,
) -> float | None:
    raw = event.data.get("confidence")
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    confidence = float(raw)
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        return None
    return min(confidence, maximum)


def _scoped_events(
    change_unit: ChangeUnit,
    events: Iterable[HistoricalEvent],
) -> tuple[HistoricalEvent, ...]:
    """Keep only causally eligible events explicitly attached to this unit."""
    attached_ids = frozenset(change_unit.event_ids)
    prediction_at = parse_timestamp(change_unit.prediction_at)
    scoped: list[HistoricalEvent] = []
    for event in events:
        if event.repository_id != change_unit.repository_id:
            continue
        linked_by_change = event.change_id == change_unit.id
        linked_by_manifest = event.id in attached_ids and event.change_id is None
        if not (linked_by_change or linked_by_manifest):
            continue
        if parse_timestamp(event.occurred_at) <= prediction_at:
            continue
        scoped.append(event)
    return tuple(sorted(scoped, key=_event_sort_key))


def _explicit_outcome_votes(
    change_unit: ChangeUnit,
    target: str,
    events: Sequence[HistoricalEvent],
) -> tuple[OutcomeVote, ...]:
    votes: list[OutcomeVote] = []
    post_merge_target = target in {POST_MERGE_REVERT_OR_HOTFIX, POST_MERGE_DEFECT}
    finalized_at = (
        parse_timestamp(change_unit.finalized_at) if change_unit.finalized_at is not None else None
    )
    for event in events:
        if event.kind != "change_finalized":
            continue
        if post_merge_target and (
            finalized_at is None or parse_timestamp(event.occurred_at) <= finalized_at
        ):
            continue
        if event.data.get("target") != target or event.data.get("evidence_complete") is not True:
            continue
        raw_value = event.data.get("value")
        raw_strength = event.data.get("strength", "strong")
        if (
            not isinstance(raw_value, str)
            or raw_value not in _VOTE_VALUES
            or not isinstance(raw_strength, str)
            or raw_strength not in _VOTE_STRENGTHS
        ):
            continue
        maximum = 1.0 if raw_strength == "strong" else 0.7
        confidence = _event_confidence(event, default=maximum, maximum=maximum)
        if confidence is None:
            continue
        raw_reason = event.data.get("reason")
        reason = (
            raw_reason
            if isinstance(raw_reason, str) and raw_reason.strip()
            else "explicit matured outcome with complete evidence"
        )
        votes.append(
            OutcomeVote(
                value=cast(VoteValue, raw_value),
                strength=cast(VoteStrength, raw_strength),
                target=target,
                available_at=event.available_at,
                source_kind=event.kind,
                event_ids=(event.id,),
                independent_group=event.independent_group,
                confidence=confidence,
                reason=reason,
            )
        )
    return tuple(votes)


def _validation_rework_votes(
    change_unit: ChangeUnit,
    events: Sequence[HistoricalEvent],
) -> tuple[OutcomeVote, ...]:
    del change_unit
    votes: list[OutcomeVote] = []
    for event in events:
        if (
            event.kind == "review"
            and event.data.get("decision") == "changes_requested"
            and event.data.get("category") == "validation"
            and event.data.get("independent") is True
        ):
            confidence = _event_confidence(event, default=1.0, maximum=1.0)
            if confidence is not None:
                votes.append(
                    OutcomeVote(
                        value="positive",
                        strength="strong",
                        target=VALIDATION_REWORK_REQUIRED,
                        available_at=event.available_at,
                        source_kind=event.kind,
                        event_ids=(event.id,),
                        independent_group=event.independent_group,
                        confidence=confidence,
                        reason="independent review requested validation changes",
                    )
                )
        elif event.kind == "change_snapshot" and event.data.get("test_changed") is True:
            confidence = _event_confidence(event, default=0.55, maximum=0.7)
            if confidence is not None:
                votes.append(
                    OutcomeVote(
                        value="positive",
                        strength="weak",
                        target=VALIDATION_REWORK_REQUIRED,
                        available_at=event.available_at,
                        source_kind=event.kind,
                        event_ids=(event.id,),
                        independent_group=event.independent_group,
                        confidence=confidence,
                        reason="a later snapshot changed tests without an independent request",
                    )
                )
    return tuple(votes)


def _ci_failure_votes(
    change_unit: ChangeUnit,
    events: Sequence[HistoricalEvent],
) -> tuple[OutcomeVote, ...]:
    del change_unit
    votes: list[OutcomeVote] = []
    for failure_index, failure in enumerate(events):
        if (
            failure.kind != "ci_run"
            or failure.data.get("conclusion") != "failure"
            or failure.data.get("attributable_to_change") is not True
        ):
            continue
        check_id = failure.data.get("check_id")
        if not isinstance(check_id, str) or not check_id.strip():
            continue
        confidence = _event_confidence(failure, default=1.0, maximum=1.0)
        if confidence is None:
            continue
        for changed_index in range(failure_index + 1, len(events)):
            changed = events[changed_index]
            if changed.kind != "change_snapshot" or changed.data.get("code_changed") is not True:
                continue
            if parse_timestamp(changed.occurred_at) <= parse_timestamp(failure.occurred_at):
                continue
            success = next(
                (
                    event
                    for event in events[changed_index + 1 :]
                    if event.kind == "ci_run"
                    and event.provider == failure.provider
                    and event.data.get("check_id") == check_id
                    and event.data.get("conclusion") == "success"
                    and parse_timestamp(event.occurred_at) > parse_timestamp(changed.occurred_at)
                ),
                None,
            )
            if success is None:
                continue
            sequence = (failure, changed, success)
            votes.append(
                OutcomeVote(
                    value="positive",
                    strength="strong",
                    target=CHANGE_ATTRIBUTABLE_CI_FAILURE,
                    available_at=_latest_timestamp(sequence),
                    source_kind="ci_run",
                    event_ids=tuple(event.id for event in sequence),
                    independent_group=failure.independent_group,
                    confidence=confidence,
                    reason=f"attributable failure of {check_id!r} passed after code changed",
                )
            )
            break
    return tuple(votes)


def _is_linked(event: HistoricalEvent, change_unit: ChangeUnit, link_kind: str) -> bool:
    return (
        event.data.get("linked_change_id") == change_unit.id
        and event.data.get("link_kind") == link_kind
    )


def _post_merge_events(
    change_unit: ChangeUnit,
    events: Sequence[HistoricalEvent],
) -> tuple[HistoricalEvent, ...]:
    if change_unit.finalized_at is None:
        return ()
    finalized_at = parse_timestamp(change_unit.finalized_at)
    return tuple(event for event in events if parse_timestamp(event.occurred_at) > finalized_at)


def _revert_or_hotfix_votes(
    change_unit: ChangeUnit,
    events: Sequence[HistoricalEvent],
) -> tuple[OutcomeVote, ...]:
    votes: list[OutcomeVote] = []
    for event in _post_merge_events(change_unit, events):
        is_explicit_revert = event.kind == "revert" and _is_linked(event, change_unit, "explicit")
        is_explicit_hotfix = (
            event.kind == "incident"
            and event.data.get("category") == "hotfix"
            and _is_linked(event, change_unit, "explicit")
        )
        if not (is_explicit_revert or is_explicit_hotfix):
            continue
        confidence = _event_confidence(event, default=1.0, maximum=1.0)
        if confidence is None:
            continue
        votes.append(
            OutcomeVote(
                value="positive",
                strength="strong",
                target=POST_MERGE_REVERT_OR_HOTFIX,
                available_at=event.available_at,
                source_kind=event.kind,
                event_ids=(event.id,),
                independent_group=event.independent_group,
                confidence=confidence,
                reason="post-merge revert or hotfix explicitly linked to the change",
            )
        )
    return tuple(votes)


def _post_merge_defect_votes(
    change_unit: ChangeUnit,
    events: Sequence[HistoricalEvent],
) -> tuple[OutcomeVote, ...]:
    votes: list[OutcomeVote] = []
    for event in _post_merge_events(change_unit, events):
        if event.kind != "incident" or event.data.get("category") != "defect":
            continue
        link_kind = event.data.get("link_kind")
        if not isinstance(link_kind, str) or not _is_linked(event, change_unit, link_kind):
            continue
        if link_kind == "explicit":
            strength: VoteStrength = "strong"
            default_confidence = 1.0
        elif link_kind in _WEAK_LINK_KINDS:
            strength = "weak"
            default_confidence = 0.45 if link_kind == "fix_keyword" else 0.55
        else:
            continue
        confidence = _event_confidence(
            event,
            default=default_confidence,
            maximum=1.0 if strength == "strong" else 0.7,
        )
        if confidence is None:
            continue
        votes.append(
            OutcomeVote(
                value="positive",
                strength=strength,
                target=POST_MERGE_DEFECT,
                available_at=event.available_at,
                source_kind=event.kind,
                event_ids=(event.id,),
                independent_group=event.independent_group,
                confidence=confidence,
                reason=f"post-merge defect linked by {link_kind} evidence",
            )
        )
    return tuple(votes)


_LabelingFunction = Callable[
    [ChangeUnit, Sequence[HistoricalEvent]],
    tuple[OutcomeVote, ...],
]

_LABELING_FUNCTIONS: dict[str, _LabelingFunction] = {
    VALIDATION_REWORK_REQUIRED: _validation_rework_votes,
    CHANGE_ATTRIBUTABLE_CI_FAILURE: _ci_failure_votes,
    POST_MERGE_REVERT_OR_HOTFIX: _revert_or_hotfix_votes,
    POST_MERGE_DEFECT: _post_merge_defect_votes,
}


def _label_evidence_kind(votes: Sequence[OutcomeVote]) -> str:
    mappings = {
        "review": "review",
        "ci_run": "ci",
        "revert": "incident",
        "incident": "incident",
        "change_snapshot": "imported",
        "change_finalized": "imported",
    }
    kinds = {mappings.get(vote.source_kind, "imported") for vote in votes}
    return kinds.pop() if len(kinds) == 1 else "imported"


def aggregate_votes(
    target: str,
    votes: Iterable[OutcomeVote],
    *,
    include_weak: bool = False,
) -> OutcomeDerivation:
    """Aggregate votes without majority voting or implicit negative labels.

    Votes from one ``independent_group`` count as one evidence source.  Any
    positive/negative conflict within or across sources yields ``unknown``.
    Weak votes remain in provenance but do not affect the label unless explicitly
    enabled.
    """
    validate_predicate(target, field_name="outcome target")
    ordered = tuple(sorted(votes, key=_vote_sort_key))
    if any(vote.target != target for vote in ordered):
        raise ModelError("cannot aggregate outcome votes for different targets")
    applicable = tuple(
        vote
        for vote in ordered
        if vote.value != "abstain" and (vote.strength == "strong" or include_weak)
    )
    if not applicable:
        return OutcomeDerivation(
            target=target,
            value=LabelValue.UNKNOWN,
            evidence=None,
            votes=ordered,
            weak_evidence_enabled=include_weak,
        )

    grouped_values: dict[str, set[VoteValue]] = {}
    for vote in applicable:
        grouped_values.setdefault(vote.independent_group, set()).add(vote.value)
    if any(len(values) != 1 for values in grouped_values.values()):
        return OutcomeDerivation(
            target=target,
            value=LabelValue.UNKNOWN,
            evidence=None,
            votes=ordered,
            weak_evidence_enabled=include_weak,
        )
    resolved_values = {next(iter(values)) for values in grouped_values.values()}
    if len(resolved_values) != 1:
        return OutcomeDerivation(
            target=target,
            value=LabelValue.UNKNOWN,
            evidence=None,
            votes=ordered,
            weak_evidence_enabled=include_weak,
        )

    resolved = resolved_values.pop()
    label = LabelValue.POSITIVE if resolved == "positive" else LabelValue.NEGATIVE
    strong_applicable = tuple(vote for vote in applicable if vote.strength == "strong")
    evidence_candidates = strong_applicable or applicable
    earliest = min(evidence_candidates, key=_vote_sort_key)
    evidence = LabelEvidence(
        kind=_label_evidence_kind((earliest,)),
        available_at=earliest.available_at,
        source="historical-events:" + ",".join(earliest.event_ids),
        reason=earliest.reason,
        confidence=earliest.confidence,
    )
    return OutcomeDerivation(
        target=target,
        value=label,
        evidence=evidence,
        votes=ordered,
        weak_evidence_enabled=include_weak,
    )


def derive_outcome(
    change_unit: ChangeUnit,
    events: Iterable[HistoricalEvent],
    target: str,
    *,
    include_weak: bool = False,
) -> OutcomeDerivation:
    """Derive one atomic outcome from events attached to a change unit."""
    try:
        labeling_function = _LABELING_FUNCTIONS[target]
    except KeyError as exc:
        raise ModelError(f"unsupported historical outcome target: {target!r}") from exc
    scoped = _scoped_events(change_unit, events)
    votes = _explicit_outcome_votes(change_unit, target, scoped) + labeling_function(
        change_unit, scoped
    )
    return aggregate_votes(target, votes, include_weak=include_weak)


def derive_outcomes(
    change_unit: ChangeUnit,
    events: Iterable[HistoricalEvent],
    *,
    targets: Iterable[str] = ATOMIC_OUTCOME_TARGETS,
    include_weak: bool = False,
) -> dict[str, OutcomeDerivation]:
    """Derive multiple atomic targets without exhausting a one-shot event stream."""
    materialized_events = tuple(events)
    requested_targets = tuple(targets)
    if len(requested_targets) != len(set(requested_targets)):
        raise ModelError("historical outcome targets cannot contain duplicates")
    return {
        target: derive_outcome(
            change_unit,
            materialized_events,
            target,
            include_weak=include_weak,
        )
        for target in requested_targets
    }
