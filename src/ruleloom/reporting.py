"""Prospective pilot metrics that keep prediction time separate from outcome time."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ruleloom.lifecycle import (
    Readiness,
    prospective_unit_outcome,
    readiness,
    validate_non_overlapping_git_ranges,
)
from ruleloom.models import (
    JsonObject,
    LabelValue,
    Metrics,
    ModelError,
    Observation,
    Prediction,
    parse_timestamp,
    validate_prediction_cohort,
)


@dataclass(frozen=True, slots=True)
class PilotReport:
    readiness: Readiness
    predictions: int
    unique_observations: int
    duplicate_predictions: int
    mature_after_prediction: int
    still_unknown: int
    excluded_preexisting_outcome: int
    matched: int
    abstained: int
    evaluated_matched: int
    evaluated_abstained: int
    metrics: Metrics

    def to_dict(self) -> JsonObject:
        return {
            "readiness": self.readiness.to_dict(),
            "predictions": self.predictions,
            "unique_observations": self.unique_observations,
            "duplicate_predictions": self.duplicate_predictions,
            "mature_after_prediction": self.mature_after_prediction,
            "still_unknown": self.still_unknown,
            "excluded_preexisting_outcome": self.excluded_preexisting_outcome,
            "matched": self.matched,
            "abstained": self.abstained,
            "coverage": self.matched / self.unique_observations
            if self.unique_observations
            else 0.0,
            "evaluated_matched": self.evaluated_matched,
            "evaluated_abstained": self.evaluated_abstained,
            "prospective_metrics": self.metrics.to_dict(),
            "interpretation": (
                "These are prospective association metrics, not a causal estimate of bugs "
                "prevented. Causal impact requires a randomized or staged advisory rollout."
            ),
        }


def build_pilot_report(
    observations: Sequence[Observation],
    predictions: Sequence[Prediction],
    target: str,
    *,
    as_of: datetime | None = None,
    root: Path | None = None,
) -> PilotReport:
    """Evaluate only the first prediction made before each outcome became available."""
    cutoff = as_of or datetime.now(UTC)
    if cutoff.tzinfo is None:
        raise ModelError("as_of must include a timezone")
    mismatched_targets = {item.target for item in predictions if item.target != target}
    if mismatched_targets:
        raise ModelError(
            f"pilot report target {target!r} cannot include predictions for "
            + ", ".join(repr(item) for item in sorted(mismatched_targets))
        )
    try:
        validate_prediction_cohort(predictions)
    except ModelError as exc:
        raise ModelError(f"pilot metrics cannot pool this prediction cohort: {exc}") from exc
    if predictions and predictions[0].protocol.get("observation_unit") == "git_range":
        if root is None:
            raise ModelError("pilot reporting for git_range units requires the repository root")
        validate_non_overlapping_git_ranges(root, predictions)
    future_predictions = [
        item.id for item in predictions if parse_timestamp(item.predicted_at) > cutoff
    ]
    if future_predictions:
        raise ModelError(
            "pilot report contains prediction timestamps later than as_of: "
            + ", ".join(future_predictions)
        )
    earliest: dict[str, Prediction] = {}
    for prediction in sorted(
        predictions, key=lambda item: (parse_timestamp(item.predicted_at), item.id)
    ):
        earliest.setdefault(prediction.unit_id, prediction)

    total_matched = sum(not item.abstained for item in earliest.values())
    total_abstained = len(earliest) - total_matched
    tp = fp = tn = fn = 0
    unknown = excluded = evaluated_matched = evaluated_abstained = 0
    for prediction in earliest.values():
        label, available = prospective_unit_outcome(
            observations,
            prediction,
            target,
            as_of=cutoff,
        )
        if label is LabelValue.UNKNOWN or available is None:
            unknown += 1
            continue
        if available <= parse_timestamp(prediction.predicted_at):
            excluded += 1
            continue
        actual = label is LabelValue.POSITIVE
        predicted = not prediction.abstained
        evaluated_matched += predicted
        evaluated_abstained += not predicted
        if actual and predicted:
            tp += 1
        elif not actual and predicted:
            fp += 1
        elif not actual and not predicted:
            tn += 1
        else:
            fn += 1
    return PilotReport(
        readiness=readiness(observations, target, as_of=cutoff),
        predictions=len(predictions),
        unique_observations=len(earliest),
        duplicate_predictions=len(predictions) - len(earliest),
        mature_after_prediction=tp + fp + tn + fn,
        still_unknown=unknown,
        excluded_preexisting_outcome=excluded,
        matched=total_matched,
        abstained=total_abstained,
        evaluated_matched=evaluated_matched,
        evaluated_abstained=evaluated_abstained,
        metrics=Metrics.from_counts(tp, fp, tn, fn),
    )


def build_pilot_reports(
    observations: Sequence[Observation],
    predictions: Sequence[Prediction],
    target: str,
    *,
    as_of: datetime | None = None,
    root: Path | None = None,
) -> dict[str, PilotReport]:
    """Build one report per immutable policy set so experiments are never pooled."""
    mismatched_targets = {item.target for item in predictions if item.target != target}
    if mismatched_targets:
        raise ModelError(
            f"pilot report target {target!r} cannot include predictions for "
            + ", ".join(repr(item) for item in sorted(mismatched_targets))
        )
    grouped: dict[str, list[Prediction]] = {}
    for prediction in predictions:
        grouped.setdefault(prediction.policy_set_hash, []).append(prediction)
    cutoff = as_of or datetime.now(UTC)
    return {
        policy_set_hash: build_pilot_report(
            observations,
            items,
            target,
            as_of=cutoff,
            root=root,
        )
        for policy_set_hash, items in sorted(grouped.items())
    }
