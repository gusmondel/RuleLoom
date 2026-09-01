"""Train-only, time-aware signal diagnostics that protect a frozen holdout."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from statistics import NormalDist
from typing import cast

from ruleloom.config import RuleLoomConfig
from ruleloom.evaluation import fit_boolean_logistic_baseline, label_is_mature
from ruleloom.models import (
    JsonObject,
    JsonValue,
    LabelValue,
    Metrics,
    ModelError,
    Observation,
    content_hash,
    parse_timestamp,
)

SIGNAL_PROBE_VERSION = "ruleloom-signal-probe/1"


def wilson_interval(successes: int, trials: int, confidence: float) -> tuple[float, float]:
    """Return a two-sided Wilson score interval for one binomial proportion."""

    if trials <= 0:
        return 0.0, 1.0
    if not 0 <= successes <= trials:
        raise ModelError("Wilson successes must be between zero and trials")
    if not 0.5 < confidence < 1:
        raise ModelError("Wilson confidence must be strictly between 0.5 and 1")
    z = NormalDist().inv_cdf(0.5 + confidence / 2)
    proportion = successes / trials
    denominator = 1 + z * z / trials
    centre = proportion + z * z / (2 * trials)
    radius = z * math.sqrt(proportion * (1 - proportion) / trials + z * z / (4 * trials * trials))
    return max(0.0, (centre - radius) / denominator), min(1.0, (centre + radius) / denominator)


def conservative_lift_diagnostic(metrics: Metrics, confidence: float) -> JsonObject:
    """Compare alert precision with cohort prevalence using conservative Wilson endpoints.

    This is deliberately called a diagnostic rather than a formal confidence interval for
    lift. The alerted subset is selected by a fitted model and the temporally ordered outcomes
    need not be independent Bernoulli trials.
    """

    alerted = metrics.true_positive + metrics.false_positive
    total = alerted + metrics.true_negative + metrics.false_negative
    positive = metrics.true_positive + metrics.false_negative
    precision_lower, precision_upper = wilson_interval(metrics.true_positive, alerted, confidence)
    prevalence_lower, prevalence_upper = wilson_interval(positive, total, confidence)
    point_lift = metrics.precision / metrics.prevalence if metrics.prevalence else 0.0
    lower = precision_lower / prevalence_upper if prevalence_upper else 0.0
    return {
        "confidence_level": confidence,
        "precision_wilson_lower": precision_lower,
        "precision_wilson_upper": precision_upper,
        "prevalence_wilson_lower": prevalence_lower,
        "prevalence_wilson_upper": prevalence_upper,
        "point_lift": point_lift,
        "conservative_lift_lower": lower,
        "interpretation": "descriptive_not_a_formal_post_selection_lift_interval",
    }


def _metrics(labels: list[bool], predictions: list[bool]) -> Metrics:
    tp = fp = tn = fn = 0
    for actual, predicted in zip(labels, predictions, strict=True):
        if actual and predicted:
            tp += 1
        elif not actual and predicted:
            fp += 1
        elif not actual and not predicted:
            tn += 1
        else:
            fn += 1
    return Metrics.from_counts(tp, fp, tn, fn)


def _average_precision(labels: list[bool], scores: list[float]) -> float:
    positives = sum(labels)
    if not positives:
        return 0.0
    ranked = sorted(
        enumerate(zip(scores, labels, strict=True)),
        key=lambda item: (-item[1][0], item[0]),
    )
    found = 0
    total = 0.0
    for rank, (_, (_, actual)) in enumerate(ranked, 1):
        if actual:
            found += 1
            total += found / rank
    return total / positives


def _threshold(scores: list[float], labels: list[bool]) -> float:
    values = sorted(set(scores))
    thresholds = [0.0, *values, 1.000000000001]
    return max(
        thresholds,
        key=lambda value: (
            (
                metrics := _metrics(labels, [score >= value for score in scores])
            ).matthews_correlation,
            metrics.f1,
            metrics.precision,
            -metrics.predicted_positive_rate,
            value,
        ),
    )


@dataclass(frozen=True, slots=True)
class _TreeNode:
    probability: float
    predicate: str | None = None
    absent: _TreeNode | None = None
    present: _TreeNode | None = None

    def score(self, facts: frozenset[str]) -> float:
        if self.predicate is None:
            return self.probability
        child = self.present if self.predicate in facts else self.absent
        return self.probability if child is None else child.score(facts)


@dataclass(frozen=True, slots=True)
class _BooleanTreeModel:
    root: _TreeNode
    threshold: float

    def score(self, facts: frozenset[str]) -> float:
        return self.root.score(facts)

    def predicts(self, facts: frozenset[str]) -> bool:
        return self.score(facts) >= self.threshold


def _weighted_probability(labels: list[bool], weights: list[float], indices: list[int]) -> float:
    denominator = sum(weights[index] for index in indices)
    if denominator <= 0:
        return 0.0
    return sum(weights[index] for index in indices if labels[index]) / denominator


def _gini(labels: list[bool], weights: list[float], indices: list[int]) -> float:
    probability = _weighted_probability(labels, weights, indices)
    return 2 * probability * (1 - probability)


def _fit_boolean_tree(
    observations: list[Observation],
    target: str,
    *,
    max_depth: int,
    max_predicates: int,
) -> _BooleanTreeModel:
    labels = [item.labels[target] is LabelValue.POSITIVE for item in observations]
    positive = sum(labels)
    negative = len(labels) - positive
    if not positive or not negative:
        raise ModelError("Boolean tree signal model requires both mature classes")
    weights = [
        len(labels) / (2 * positive) if actual else len(labels) / (2 * negative)
        for actual in labels
    ]
    predicates = sorted({fact for item in observations for fact in item.facts})

    def rate_gap(predicate: str) -> tuple[float, str]:
        positive_rate = (
            sum(
                predicate in item.facts and actual
                for item, actual in zip(observations, labels, strict=True)
            )
            / positive
        )
        negative_rate = (
            sum(
                predicate in item.facts and not actual
                for item, actual in zip(observations, labels, strict=True)
            )
            / negative
        )
        return -abs(positive_rate - negative_rate), predicate

    predicates = sorted(predicates, key=rate_gap)[:max_predicates]

    def build(indices: list[int], available: tuple[str, ...], depth: int) -> _TreeNode:
        probability = _weighted_probability(labels, weights, indices)
        if depth >= max_depth or not available or len({labels[index] for index in indices}) <= 1:
            return _TreeNode(probability=probability)
        candidates: list[tuple[float, str, list[int], list[int]]] = []
        total_weight = sum(weights[index] for index in indices)
        for predicate in available:
            absent = [index for index in indices if predicate not in observations[index].facts]
            present = [index for index in indices if predicate in observations[index].facts]
            if not absent or not present:
                continue
            impurity = (
                sum(weights[index] for index in absent) * _gini(labels, weights, absent)
                + sum(weights[index] for index in present) * _gini(labels, weights, present)
            ) / total_weight
            candidates.append((impurity, predicate, absent, present))
        if not candidates:
            return _TreeNode(probability=probability)
        _, predicate, absent, present = min(candidates, key=lambda item: (item[0], item[1]))
        remaining = tuple(item for item in available if item != predicate)
        return _TreeNode(
            probability=probability,
            predicate=predicate,
            absent=build(absent, remaining, depth + 1),
            present=build(present, remaining, depth + 1),
        )

    root = build(list(range(len(observations))), tuple(predicates), 0)
    train_scores = [root.score(item.facts) for item in observations]
    return _BooleanTreeModel(root=root, threshold=_threshold(train_scores, labels))


@dataclass(frozen=True, slots=True)
class SignalModelReport:
    family: str
    folds: int
    observations: int
    metrics: Metrics
    average_precision: float
    selective_risk: float
    lift: JsonObject
    gate_passed: bool
    gate_reasons: tuple[str, ...]

    def to_dict(self) -> JsonObject:
        return {
            "family": self.family,
            "folds": self.folds,
            "observations": self.observations,
            "metrics": self.metrics.to_dict(),
            "average_precision": self.average_precision,
            "selective_risk": self.selective_risk,
            "lift": self.lift,
            "gate_passed": self.gate_passed,
            "gate_reasons": list(self.gate_reasons),
        }


@dataclass(frozen=True, slots=True)
class SignalProbeReport:
    id: str
    created_at: str
    status: str
    version: str
    dataset_hash: str
    config_hash: str
    holdout_start_at: str
    training_observations: int
    models: tuple[SignalModelReport, ...]
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.status not in {"pass", "fail", "inconclusive"}:
            raise ModelError("unsupported signal probe status")

    def identity_payload(self) -> JsonObject:
        return {
            "version": self.version,
            "created_at": self.created_at,
            "status": self.status,
            "dataset_hash": self.dataset_hash,
            "config_hash": self.config_hash,
            "holdout_start_at": self.holdout_start_at,
            "training_observations": self.training_observations,
            "models": [item.to_dict() for item in self.models],
            "warnings": list(self.warnings),
        }

    @property
    def expected_id(self) -> str:
        return f"probe-{content_hash(self.identity_payload())[:16]}"

    def with_identity(self) -> SignalProbeReport:
        return replace(self, id=self.expected_id)

    def to_dict(self) -> JsonObject:
        return {
            "schema_version": 1,
            "id": self.id,
            **self.identity_payload(),
            "methodology": {
                "scope": "strictly_pre_holdout_mature_labels",
                "validation": "expanding_window_rolling_origin",
                "label_availability_enforced_at_each_fold": True,
                "holdout_consulted": False,
                "model_families_predeclared": [
                    "logistic_regression_boolean_facts",
                    "shallow_boolean_tree",
                ],
                "interpretation": "signal_availability_probe_not_a_theoretical_ceiling",
            },
        }


def _dataset_hash(observations: list[Observation]) -> str:
    return content_hash(
        cast(
            JsonValue,
            [item.to_dict() for item in sorted(observations, key=lambda item: item.id)],
        )
    )


def _ordered_pre_holdout(
    observations: list[Observation], target: str, boundary: datetime, cutoff: datetime
) -> list[Observation]:
    examples = [
        item
        for item in observations
        if parse_timestamp(item.observed_at) < boundary
        and label_is_mature(item, target, as_of=cutoff)
    ]
    topology = [item.metadata.get("topological_index") for item in examples]
    repositories = {item.source.get("repository") for item in examples}
    if (
        examples
        and len(repositories) == 1
        and all(isinstance(value, int) and not isinstance(value, bool) for value in topology)
    ):
        examples.sort(key=lambda item: (cast(int, item.metadata["topological_index"]), item.id))
    else:
        examples.sort(key=lambda item: (parse_timestamp(item.observed_at), item.id))
    return examples


def _fold_ranges(total: int, folds: int, min_train: int, min_validation: int) -> list[range]:
    fold_count = min(folds, (total - min_train) // min_validation)
    if fold_count < 2:
        return []
    validation_total = total - min_train
    base, remainder = divmod(validation_total, fold_count)
    ranges: list[range] = []
    start = min_train
    for index in range(fold_count):
        size = base + (1 if index < remainder else 0)
        ranges.append(range(start, start + size))
        start += size
    return ranges


def run_signal_probe(
    observations: list[Observation],
    config: RuleLoomConfig,
    *,
    as_of: datetime | None = None,
) -> SignalProbeReport:
    """Estimate whether the frozen vocabulary contains deployable train-only signal."""

    options = config.signal_probe
    if not options.enabled:
        raise ModelError("signal probing is disabled in the frozen config")
    if config.evaluation.test_start_at is None:
        raise ModelError("signal probing requires a frozen evaluation.test_start_at")
    cutoff = as_of or datetime.now(UTC)
    if cutoff.tzinfo is None:
        raise ModelError("as_of must include a timezone")
    boundary = parse_timestamp(config.evaluation.test_start_at)
    examples = _ordered_pre_holdout(observations, config.target, boundary, cutoff)
    ranges = _fold_ranges(
        len(examples),
        options.folds,
        options.min_train_examples,
        options.min_validation_examples,
    )
    warnings: list[str] = []
    if not ranges:
        warnings.append(
            "insufficient pre-holdout mature labels for at least two rolling-origin folds"
        )
    outputs: dict[str, tuple[list[bool], list[bool], list[float], int]] = {
        "logistic_regression_boolean_facts": ([], [], [], 0),
        "shallow_boolean_tree": ([], [], [], 0),
    }
    for fold_range in ranges:
        validation = examples[fold_range.start : fold_range.stop]
        validation_start = min(parse_timestamp(item.observed_at) for item in validation)
        train = [
            item
            for item in examples[: fold_range.start]
            if (evidence := item.label_evidence.get(config.target)) is not None
            and parse_timestamp(evidence.available_at) <= validation_start
        ]
        train_labels = [item.labels[config.target] is LabelValue.POSITIVE for item in train]
        if len(train) < options.min_train_examples or len(set(train_labels)) < 2:
            warnings.append(
                f"skipped fold starting {validation[0].observed_at}: insufficient "
                "temporally available examples or only one class"
            )
            continue
        logistic = fit_boolean_logistic_baseline(
            train,
            config.target,
            as_of=validation_start,
            max_predicates=options.max_predicates,
        )
        tree = _fit_boolean_tree(
            train,
            config.target,
            max_depth=options.tree_max_depth,
            max_predicates=options.max_predicates,
        )
        actual = [item.labels[config.target] is LabelValue.POSITIVE for item in validation]
        for family, scorer, predictor in (
            (
                "logistic_regression_boolean_facts",
                logistic.score,
                logistic.predicts,
            ),
            ("shallow_boolean_tree", tree.score, tree.predicts),
        ):
            labels, predictions, scores, completed = outputs[family]
            labels.extend(actual)
            predictions.extend(predictor(item.facts) for item in validation)
            scores.extend(scorer(item.facts) for item in validation)
            outputs[family] = (labels, predictions, scores, completed + 1)

    model_reports: list[SignalModelReport] = []
    for family, (labels, predictions, scores, completed) in outputs.items():
        if completed < 2 or not labels or len(set(labels)) < 2:
            continue
        metrics = _metrics(labels, predictions)
        lift = conservative_lift_diagnostic(metrics, options.confidence_level)
        lift_lower = cast(float, lift["conservative_lift_lower"])
        gate_reasons: list[str] = []
        if metrics.predicted_positive_rate < options.min_alert_rate:
            gate_reasons.append("alert_rate_below_minimum")
        if (
            metrics.matthews_correlation < options.min_mcc
            and lift_lower < options.min_lift_lower_bound
        ):
            gate_reasons.append("neither_mcc_nor_conservative_lift_met_threshold")
        gate_passed = not gate_reasons
        model_reports.append(
            SignalModelReport(
                family=family,
                folds=completed,
                observations=len(labels),
                metrics=metrics,
                average_precision=_average_precision(labels, scores),
                selective_risk=1 - metrics.precision if metrics.predicted_positive_rate else 1.0,
                lift=lift,
                gate_passed=gate_passed,
                gate_reasons=tuple(gate_reasons),
            )
        )
    status = (
        "inconclusive"
        if not model_reports
        else "pass"
        if any(item.gate_passed for item in model_reports)
        else "fail"
    )
    created_at = cutoff.isoformat().replace("+00:00", "Z")
    return SignalProbeReport(
        id="probe-pending",
        created_at=created_at,
        status=status,
        version=SIGNAL_PROBE_VERSION,
        dataset_hash=_dataset_hash(examples),
        config_hash=config.hash,
        holdout_start_at=config.evaluation.test_start_at,
        training_observations=len(examples),
        models=tuple(model_reports),
        warnings=tuple(dict.fromkeys(warnings)),
    ).with_identity()
