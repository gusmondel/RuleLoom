from __future__ import annotations

from dataclasses import replace

import pytest

from ruleloom.history.models import ChangeUnit, HistoricalEvent
from ruleloom.history.outcomes import (
    ATOMIC_OUTCOME_TARGETS,
    CHANGE_ATTRIBUTABLE_CI_FAILURE,
    POST_MERGE_DEFECT,
    POST_MERGE_REVERT_OR_HOTFIX,
    VALIDATION_REWORK_REQUIRED,
    OutcomeVote,
    aggregate_votes,
    derive_outcome,
    derive_outcomes,
)
from ruleloom.models import LabelValue, ModelError

BASE_SHA = "a" * 40
PREDICTION_SHA = "b" * 40
FINAL_SHA = "c" * 40
PREDICTION_AT = "2026-01-01T10:00:00+00:00"
FINALIZED_AT = "2026-01-02T10:00:00+00:00"


def _change(*event_ids: str, finalized: bool = True) -> ChangeUnit:
    return ChangeUnit(
        id="github.pr.42",
        repository_id="repository.example",
        kind="pull_request",
        base_sha=BASE_SHA,
        prediction_sha=PREDICTION_SHA,
        prediction_at=PREDICTION_AT,
        final_sha=FINAL_SHA if finalized else None,
        finalized_at=FINALIZED_AT if finalized else None,
        commits=(PREDICTION_SHA,),
        event_ids=event_ids,
        provider="github",
        source_ref="github:pull/42",
        evidence_quality="rich",
        confirmatory=True,
    )


def _event(
    event_id: str,
    kind: str,
    occurred_at: str,
    data: dict[str, object],
    *,
    group: str | None = None,
    change_id: str | None = "github.pr.42",
    repository_id: str = "repository.example",
    available_at: str | None = None,
) -> HistoricalEvent:
    return HistoricalEvent(
        id=event_id,
        repository_id=repository_id,
        kind=kind,
        occurred_at=occurred_at,
        available_at=available_at or occurred_at,
        provider="github",
        source_ref=f"github:event/{event_id}",
        independent_group=group or event_id,
        data=data,
        change_id=change_id,
    )


def _vote(
    value: str,
    *,
    strength: str = "strong",
    group: str = "source.one",
    event_id: str = "event.one",
) -> OutcomeVote:
    return OutcomeVote(  # type: ignore[arg-type]
        value=value,
        strength=strength,
        target=VALIDATION_REWORK_REQUIRED,
        available_at="2026-01-03T10:00:00+00:00",
        source_kind="review",
        event_ids=(event_id,),
        independent_group=group,
        confidence=1.0 if strength == "strong" else 0.5,
        reason="synthetic deterministic vote",
    )


def test_independent_validation_request_is_a_strong_positive() -> None:
    review = _event(
        "review.validation",
        "review",
        "2026-01-01T11:00:00+00:00",
        {
            "decision": "changes_requested",
            "category": "validation",
            "independent": True,
        },
        group="reviewer.one",
    )

    result = derive_outcome(
        _change(review.id),
        [review],
        VALIDATION_REWORK_REQUIRED,
    )

    assert result.value is LabelValue.POSITIVE
    assert result.label is result.value
    assert result.evidence is not None
    assert result.evidence.kind == "review"
    assert result.evidence.available_at == review.available_at
    assert result.votes[0].strength == "strong"
    assert result.votes[0].event_ids == (review.id,)


def test_test_change_is_weak_and_requires_explicit_opt_in() -> None:
    snapshot = _event(
        "snapshot.tests",
        "change_snapshot",
        "2026-01-01T11:00:00+00:00",
        {"test_changed": True, "code_changed": True},
    )
    change = _change(snapshot.id)

    default = derive_outcome(change, [snapshot], VALIDATION_REWORK_REQUIRED)
    opted_in = derive_outcome(
        change,
        [snapshot],
        VALIDATION_REWORK_REQUIRED,
        include_weak=True,
    )

    assert default.value is LabelValue.UNKNOWN
    assert default.evidence is None
    assert default.votes[0].strength == "weak"
    assert opted_in.value is LabelValue.POSITIVE
    assert opted_in.evidence is not None
    assert opted_in.evidence.kind == "imported"


def test_agreeing_weak_vote_cannot_predate_strong_label_evidence() -> None:
    weak = replace(
        _vote("positive", strength="weak", group="heuristic", event_id="event.weak"),
        available_at="2026-01-02T10:00:00+00:00",
        source_kind="change_snapshot",
    )
    strong = replace(
        _vote("positive", group="reviewer", event_id="event.strong"),
        available_at="2026-01-10T10:00:00+00:00",
    )

    result = aggregate_votes(
        VALIDATION_REWORK_REQUIRED,
        [weak, strong],
        include_weak=True,
    )

    assert result.value is LabelValue.POSITIVE
    assert result.evidence is not None
    assert result.evidence.available_at == strong.available_at
    assert result.evidence.source == "historical-events:event.strong"
    assert result.evidence.kind == "review"


def test_ci_failure_requires_attribution_code_change_and_same_check_success() -> None:
    failure = _event(
        "ci.failure",
        "ci_run",
        "2026-01-01T11:00:00+00:00",
        {
            "check_id": "portable-test-suite",
            "conclusion": "failure",
            "attributable_to_change": True,
        },
        group="ci.portable",
    )
    changed = _event(
        "snapshot.code",
        "change_snapshot",
        "2026-01-01T12:00:00+00:00",
        {"code_changed": True},
    )
    other_success = _event(
        "ci.other.success",
        "ci_run",
        "2026-01-01T13:00:00+00:00",
        {"check_id": "another-check", "conclusion": "success"},
    )
    success = _event(
        "ci.success",
        "ci_run",
        "2026-01-01T14:00:00+00:00",
        {"check_id": "portable-test-suite", "conclusion": "success"},
        available_at="2026-01-01T14:05:00+00:00",
    )
    events = [success, other_success, changed, failure]

    result = derive_outcome(
        _change(*(event.id for event in events)),
        events,
        CHANGE_ATTRIBUTABLE_CI_FAILURE,
    )

    assert result.value is LabelValue.POSITIVE
    assert result.evidence is not None
    assert result.evidence.kind == "ci"
    assert result.evidence.available_at == success.available_at
    assert result.votes[0].event_ids == (failure.id, changed.id, success.id)


def test_unattributed_merge_failure_is_weak_and_requires_opt_in() -> None:
    failure = _event(
        "ci.merge.failure",
        "ci_run",
        "2026-01-03T11:00:00+00:00",
        {
            "check_id": "portable-test-suite",
            "conclusion": "failure",
            "attributable_to_change": False,
            "attribution": "unattributed_merge_result",
            "evidence_grade": "weak_heuristic",
        },
    )
    change = _change(failure.id)

    conservative = derive_outcome(change, [failure], CHANGE_ATTRIBUTABLE_CI_FAILURE)
    opted_in = derive_outcome(
        change,
        [failure],
        CHANGE_ATTRIBUTABLE_CI_FAILURE,
        include_weak=True,
    )

    assert conservative.value is LabelValue.UNKNOWN
    assert conservative.votes[0].strength == "weak"
    assert opted_in.value is LabelValue.POSITIVE
    assert opted_in.evidence is not None
    assert opted_in.evidence.confidence == 0.4


@pytest.mark.parametrize(
    ("events", "expected"),
    [
        (
            [
                _event(
                    "ci.failure.unattributed",
                    "ci_run",
                    "2026-01-01T11:00:00+00:00",
                    {
                        "check_id": "test",
                        "conclusion": "failure",
                        "attributable_to_change": False,
                    },
                ),
                _event(
                    "snapshot.after.failure",
                    "change_snapshot",
                    "2026-01-01T12:00:00+00:00",
                    {"code_changed": True},
                ),
                _event(
                    "ci.success.after.failure",
                    "ci_run",
                    "2026-01-01T13:00:00+00:00",
                    {"check_id": "test", "conclusion": "success"},
                ),
            ],
            LabelValue.UNKNOWN,
        ),
        (
            [
                _event(
                    "ci.failure.no.change",
                    "ci_run",
                    "2026-01-01T11:00:00+00:00",
                    {
                        "check_id": "test",
                        "conclusion": "failure",
                        "attributable_to_change": True,
                    },
                ),
                _event(
                    "ci.success.no.change",
                    "ci_run",
                    "2026-01-01T12:00:00+00:00",
                    {"check_id": "test", "conclusion": "success"},
                ),
            ],
            LabelValue.UNKNOWN,
        ),
    ],
)
def test_ci_reruns_without_complete_causal_sequence_abstain(
    events: list[HistoricalEvent],
    expected: LabelValue,
) -> None:
    result = derive_outcome(
        _change(*(event.id for event in events)),
        events,
        CHANGE_ATTRIBUTABLE_CI_FAILURE,
    )

    assert result.value is expected
    assert result.votes == ()


def test_ci_sequence_requires_strict_time_order_and_same_provider() -> None:
    failure = _event(
        "ci.failure.tie",
        "ci_run",
        "2026-01-01T11:00:00+00:00",
        {
            "check_id": "portable-test-suite",
            "conclusion": "failure",
            "attributable_to_change": True,
        },
    )
    changed = _event(
        "snapshot.tie",
        "change_snapshot",
        failure.occurred_at,
        {"code_changed": True},
    )
    success = _event(
        "ci.success.tie",
        "ci_run",
        "2026-01-01T12:00:00+00:00",
        {"check_id": "portable-test-suite", "conclusion": "success"},
    )
    change = _change(failure.id, changed.id, success.id)

    tied = derive_outcome(
        change,
        [failure, changed, success],
        CHANGE_ATTRIBUTABLE_CI_FAILURE,
    )
    other_provider = derive_outcome(
        change,
        [
            failure,
            replace(
                changed,
                occurred_at="2026-01-01T11:30:00+00:00",
                available_at="2026-01-01T11:30:00+00:00",
            ),
            replace(success, provider="another-ci"),
        ],
        CHANGE_ATTRIBUTABLE_CI_FAILURE,
    )

    assert tied.value is LabelValue.UNKNOWN
    assert tied.votes == ()
    assert other_provider.value is LabelValue.UNKNOWN
    assert other_provider.votes == ()


def test_explicit_post_merge_revert_hotfix_and_defect_are_separate_targets() -> None:
    revert = _event(
        "revert.explicit",
        "revert",
        "2026-01-03T10:00:00+00:00",
        {"linked_change_id": "github.pr.42", "link_kind": "explicit"},
    )
    hotfix = _event(
        "incident.hotfix",
        "incident",
        "2026-01-04T10:00:00+00:00",
        {
            "category": "hotfix",
            "linked_change_id": "github.pr.42",
            "link_kind": "explicit",
        },
    )
    defect = _event(
        "incident.defect",
        "incident",
        "2026-01-05T10:00:00+00:00",
        {
            "category": "defect",
            "linked_change_id": "github.pr.42",
            "link_kind": "explicit",
        },
    )
    events = [revert, hotfix, defect]
    change = _change(*(event.id for event in events))

    revert_result = derive_outcome(change, events, POST_MERGE_REVERT_OR_HOTFIX)
    defect_result = derive_outcome(change, events, POST_MERGE_DEFECT)

    assert revert_result.value is LabelValue.POSITIVE
    assert {vote.event_ids[0] for vote in revert_result.votes} == {revert.id, hotfix.id}
    assert defect_result.value is LabelValue.POSITIVE
    assert [vote.event_ids[0] for vote in defect_result.votes] == [defect.id]


def test_heuristic_revert_is_weak_and_requires_explicit_opt_in() -> None:
    revert = _event(
        "revert.heuristic",
        "revert",
        "2026-01-03T10:00:00+00:00",
        {
            "linked_change_id": "github.pr.42",
            "link_kind": "heuristic",
            "evidence_grade": "weak_heuristic",
            "heuristic_id": "git_revert_trailer@1",
        },
    )
    change = _change(revert.id)

    conservative = derive_outcome(change, [revert], POST_MERGE_REVERT_OR_HOTFIX)
    opted_in = derive_outcome(
        change,
        [revert],
        POST_MERGE_REVERT_OR_HOTFIX,
        include_weak=True,
    )

    assert conservative.value is LabelValue.UNKNOWN
    assert conservative.votes[0].strength == "weak"
    assert opted_in.value is LabelValue.POSITIVE
    assert opted_in.evidence is not None
    assert opted_in.evidence.confidence == 0.6


def test_heuristic_revert_without_weak_evidence_grade_abstains() -> None:
    revert = _event(
        "revert.ungraded",
        "revert",
        "2026-01-03T10:00:00+00:00",
        {
            "linked_change_id": "github.pr.42",
            "link_kind": "heuristic",
        },
    )

    result = derive_outcome(
        _change(revert.id),
        [revert],
        POST_MERGE_REVERT_OR_HOTFIX,
        include_weak=True,
    )

    assert result.value is LabelValue.UNKNOWN
    assert result.votes == ()


@pytest.mark.parametrize("link_kind", ["fix_keyword", "szz"])
def test_fix_keyword_and_szz_defect_links_are_weak(link_kind: str) -> None:
    incident = _event(
        f"incident.{link_kind}",
        "incident",
        "2026-01-03T10:00:00+00:00",
        {
            "category": "defect",
            "linked_change_id": "github.pr.42",
            "link_kind": link_kind,
        },
    )
    change = _change(incident.id)

    default = derive_outcome(change, [incident], POST_MERGE_DEFECT)
    opted_in = derive_outcome(change, [incident], POST_MERGE_DEFECT, include_weak=True)

    assert default.value is LabelValue.UNKNOWN
    assert default.votes[0].strength == "weak"
    assert opted_in.value is LabelValue.POSITIVE


def test_mere_finalization_or_absence_never_becomes_negative() -> None:
    ordinary_finalization = _event(
        "change.finalized",
        "change_finalized",
        FINALIZED_AT,
        {"final_sha": FINAL_SHA},
    )

    result = derive_outcome(
        _change(ordinary_finalization.id),
        [ordinary_finalization],
        VALIDATION_REWORK_REQUIRED,
    )

    assert result.value is LabelValue.UNKNOWN
    assert result.evidence is None
    assert result.votes == ()


def test_explicit_complete_matured_negative_is_supported() -> None:
    matured = _event(
        "outcome.matured",
        "change_finalized",
        "2026-01-10T10:00:00+00:00",
        {
            "target": VALIDATION_REWORK_REQUIRED,
            "value": "negative",
            "evidence_complete": True,
            "strength": "strong",
            "confidence": 0.95,
            "reason": "all configured gates passed in the maturity window",
        },
        group="maturity.window",
    )

    result = derive_outcome(
        _change(matured.id),
        [matured],
        VALIDATION_REWORK_REQUIRED,
    )

    assert result.value is LabelValue.NEGATIVE
    assert result.evidence is not None
    assert result.evidence.confidence == 0.95
    assert result.votes[0].event_ids == (matured.id,)


def test_explicit_post_merge_outcome_requires_strictly_later_finalization() -> None:
    at_merge = _event(
        "outcome.at.merge",
        "change_finalized",
        FINALIZED_AT,
        {
            "target": POST_MERGE_DEFECT,
            "value": "negative",
            "evidence_complete": True,
        },
    )

    at_boundary = derive_outcome(_change(at_merge.id), [at_merge], POST_MERGE_DEFECT)
    without_final = derive_outcome(
        _change(at_merge.id, finalized=False),
        [
            replace(
                at_merge,
                occurred_at="2026-01-03T10:00:00+00:00",
                available_at="2026-01-03T10:00:00+00:00",
            )
        ],
        POST_MERGE_DEFECT,
    )

    assert at_boundary.value is LabelValue.UNKNOWN
    assert at_boundary.votes == ()
    assert without_final.value is LabelValue.UNKNOWN
    assert without_final.votes == ()


def test_conflicting_independent_votes_resolve_to_unknown() -> None:
    votes = (
        _vote("positive", group="review.one", event_id="event.positive"),
        _vote("negative", group="maturity.one", event_id="event.negative"),
    )

    result = aggregate_votes(VALIDATION_REWORK_REQUIRED, votes)

    assert result.value is LabelValue.UNKNOWN
    assert result.evidence is None
    assert {vote.event_ids for vote in result.votes} == {
        ("event.positive",),
        ("event.negative",),
    }


def test_weak_conflict_is_ignored_by_default_but_honored_when_enabled() -> None:
    votes = (
        _vote("positive", group="review.one", event_id="event.positive"),
        _vote(
            "negative",
            strength="weak",
            group="heuristic.one",
            event_id="event.weak.negative",
        ),
    )

    default = aggregate_votes(VALIDATION_REWORK_REQUIRED, votes)
    opted_in = aggregate_votes(VALIDATION_REWORK_REQUIRED, votes, include_weak=True)

    assert default.value is LabelValue.POSITIVE
    assert opted_in.value is LabelValue.UNKNOWN


def test_events_must_be_attached_same_repository_and_strictly_after_prediction() -> None:
    at_prediction = _event(
        "review.at.prediction",
        "review",
        PREDICTION_AT,
        {
            "decision": "changes_requested",
            "category": "validation",
            "independent": True,
        },
    )
    other_repository = _event(
        "review.other.repository",
        "review",
        "2026-01-01T11:00:00+00:00",
        {
            "decision": "changes_requested",
            "category": "validation",
            "independent": True,
        },
        repository_id="repository.other",
    )
    unattached = _event(
        "review.unattached",
        "review",
        "2026-01-01T12:00:00+00:00",
        {
            "decision": "changes_requested",
            "category": "validation",
            "independent": True,
        },
        change_id=None,
    )
    change = _change(at_prediction.id, other_repository.id)

    result = derive_outcome(
        change,
        [at_prediction, other_repository, unattached],
        VALIDATION_REWORK_REQUIRED,
    )

    assert result.value is LabelValue.UNKNOWN
    assert result.votes == ()


def test_events_can_be_scoped_by_change_id_when_unit_has_no_event_index() -> None:
    review = _event(
        "review.scoped",
        "review",
        "2026-01-01T11:00:00+00:00",
        {
            "decision": "changes_requested",
            "category": "validation",
            "independent": True,
        },
    )
    unrelated = replace(review, id="review.unrelated", change_id="github.pr.99")

    result = derive_outcome(
        _change(),
        [review, unrelated],
        VALIDATION_REWORK_REQUIRED,
    )

    assert result.value is LabelValue.POSITIVE
    assert result.votes[0].event_ids == (review.id,)


def test_derive_outcomes_materializes_generator_and_returns_every_atomic_target() -> None:
    review = _event(
        "review.generator",
        "review",
        "2026-01-01T11:00:00+00:00",
        {
            "decision": "changes_requested",
            "category": "validation",
            "independent": True,
        },
    )

    results = derive_outcomes(_change(review.id), (event for event in [review]))

    assert tuple(results) == ATOMIC_OUTCOME_TARGETS
    assert results[VALIDATION_REWORK_REQUIRED].value is LabelValue.POSITIVE
    assert results[CHANGE_ATTRIBUTABLE_CI_FAILURE].value is LabelValue.UNKNOWN
    assert results[POST_MERGE_REVERT_OR_HOTFIX].value is LabelValue.UNKNOWN
    assert results[POST_MERGE_DEFECT].value is LabelValue.UNKNOWN


def test_unknown_target_and_duplicate_targets_are_rejected() -> None:
    with pytest.raises(ModelError, match="unsupported historical outcome target"):
        derive_outcome(_change(), [], "unknown_target")
    with pytest.raises(ModelError, match="cannot contain duplicates"):
        derive_outcomes(
            _change(),
            [],
            targets=(VALIDATION_REWORK_REQUIRED, VALIDATION_REWORK_REQUIRED),
        )


def test_vote_validation_and_serialization_are_strict() -> None:
    vote = _vote("positive")

    assert vote.to_dict()["event_ids"] == ["event.one"]
    result = aggregate_votes(VALIDATION_REWORK_REQUIRED, [vote])
    assert result.to_dict()["value"] == "positive"
    assert result.to_dict()["weak_evidence_enabled"] is False
    with pytest.raises(ModelError, match="require event provenance"):
        replace(vote, event_ids=())
    with pytest.raises(ModelError, match="different targets"):
        aggregate_votes(POST_MERGE_DEFECT, [vote])


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"value": "maybe"}, "value must be"),
        ({"strength": "medium"}, "strength must be"),
        ({"event_ids": ("event.one", "event.one")}, "cannot contain duplicates"),
        ({"confidence": True}, "confidence must be"),
        ({"confidence": float("nan")}, "confidence must be"),
        ({"reason": "  "}, "reason cannot be empty"),
    ],
)
def test_invalid_vote_shapes_are_rejected(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ModelError, match=message):
        replace(_vote("positive"), **changes)


def test_derivation_invariants_are_enforced() -> None:
    vote = _vote("positive")
    known = aggregate_votes(VALIDATION_REWORK_REQUIRED, [vote])
    assert known.label_evidence is known.evidence
    unknown = aggregate_votes(VALIDATION_REWORK_REQUIRED, [])

    for changes, message in (
        ({"value": "positive"}, "must be a LabelValue"),
        ({"weak_evidence_enabled": 1}, "must be a boolean"),
        ({"votes": (replace(vote, target=POST_MERGE_DEFECT),)}, "votes must match"),
    ):
        with pytest.raises(ModelError, match=message):
            replace(known, **changes)
    with pytest.raises(ModelError, match="unknown outcome"):
        replace(unknown, evidence=known.evidence)
    with pytest.raises(ModelError, match="known outcome"):
        replace(known, evidence=None)


def test_conflict_inside_one_independent_group_is_unknown() -> None:
    result = aggregate_votes(
        VALIDATION_REWORK_REQUIRED,
        [
            _vote("positive", group="same.group", event_id="event.positive.same"),
            _vote("negative", group="same.group", event_id="event.negative.same"),
        ],
    )

    assert result.value is LabelValue.UNKNOWN


def test_malformed_semantic_event_fields_abstain_instead_of_crashing() -> None:
    malformed = [
        _event(
            "outcome.bad.value",
            "change_finalized",
            "2026-01-03T10:00:00+00:00",
            {
                "target": VALIDATION_REWORK_REQUIRED,
                "value": ["positive"],
                "evidence_complete": True,
            },
        ),
        _event(
            "review.bad.confidence",
            "review",
            "2026-01-03T11:00:00+00:00",
            {
                "decision": "changes_requested",
                "category": "validation",
                "independent": True,
                "confidence": "certain",
            },
        ),
        _event(
            "snapshot.bad.confidence",
            "change_snapshot",
            "2026-01-03T12:00:00+00:00",
            {"test_changed": True, "confidence": 2.0},
        ),
    ]

    result = derive_outcome(
        _change(*(event.id for event in malformed)),
        malformed,
        VALIDATION_REWORK_REQUIRED,
        include_weak=True,
    )

    assert result.value is LabelValue.UNKNOWN
    assert result.votes == ()


def test_post_merge_signals_abstain_before_finalization() -> None:
    revert = _event(
        "revert.before.merge",
        "revert",
        "2026-01-03T10:00:00+00:00",
        {"linked_change_id": "github.pr.42", "link_kind": "explicit"},
    )

    result = derive_outcome(
        _change(revert.id, finalized=False),
        [revert],
        POST_MERGE_REVERT_OR_HOTFIX,
    )

    assert result.value is LabelValue.UNKNOWN
