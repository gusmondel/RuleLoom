"""Leakage-resistant evaluation primitives for learned rules."""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from itertools import pairwise
from typing import cast

from ruleloom.models import (
    JsonObject,
    LabelValue,
    Metrics,
    ModelError,
    Observation,
    RuleSet,
    parse_timestamp,
)

Predictor = Callable[[frozenset[str]], bool]
Learner = Callable[[Sequence[Observation], str], RuleSet]


@dataclass(frozen=True, slots=True)
class TemporalSplit:
    train: tuple[Observation, ...]
    test: tuple[Observation, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BooleanLogisticModel:
    """Deterministic dependency-free logistic baseline over Boolean facts."""

    predicates: tuple[str, ...]
    weights: tuple[float, ...]
    intercept: float
    threshold: float
    iterations: int
    learning_rate: float
    l2: float
    class_balanced: bool = True

    def score(self, facts: frozenset[str]) -> float:
        logit = self.intercept + sum(
            weight
            for predicate, weight in zip(self.predicates, self.weights, strict=True)
            if predicate in facts
        )
        if logit >= 0:
            return 1.0 / (1.0 + math.exp(-min(logit, 35.0)))
        exponential = math.exp(max(logit, -35.0))
        return exponential / (1.0 + exponential)

    def predicts(self, facts: frozenset[str]) -> bool:
        return self.score(facts) >= self.threshold

    def to_dict(self) -> JsonObject:
        return {
            "predicates": list(self.predicates),
            "weights": list(self.weights),
            "intercept": self.intercept,
            "threshold": self.threshold,
            "iterations": self.iterations,
            "learning_rate": self.learning_rate,
            "l2": self.l2,
            "class_balanced": self.class_balanced,
        }


def _cutoff(as_of: datetime | None) -> datetime:
    value = as_of or datetime.now(UTC)
    if value.tzinfo is None:
        raise ModelError("as_of must include a timezone")
    return value


def label_is_mature(
    observation: Observation,
    target: str,
    *,
    as_of: datetime | None = None,
) -> bool:
    """Return whether a target outcome was genuinely observable by ``as_of``."""
    if observation.labels.get(target, LabelValue.UNKNOWN) is LabelValue.UNKNOWN:
        return False
    evidence = observation.label_evidence.get(target)
    if evidence is None:
        return False
    observed_at = parse_timestamp(observation.observed_at)
    available_at = parse_timestamp(evidence.available_at)
    if available_at <= observed_at:
        raise ModelError(
            f"label for {observation.id!r} must become available after observation time"
        )
    return available_at <= _cutoff(as_of)


def labeled(
    observations: Sequence[Observation],
    target: str,
    *,
    as_of: datetime | None = None,
) -> list[Observation]:
    return [item for item in observations if label_is_mature(item, target, as_of=as_of)]


def temporal_split(
    observations: Sequence[Observation],
    target: str,
    *,
    test_fraction: float,
    min_train: int,
    min_test: int,
    test_start_at: str | None = None,
    as_of: datetime | None = None,
) -> TemporalSplit:
    examples = labeled(observations, target, as_of=as_of)
    warnings: list[str] = []
    topological: list[tuple[str, int]] = []
    for item in examples:
        repository = item.source.get("repository")
        position = item.metadata.get("topological_index")
        if (
            isinstance(repository, str)
            and repository
            and isinstance(position, int)
            and not isinstance(position, bool)
            and position >= 1
        ):
            topological.append((repository, position))
    if (
        examples
        and len(topological) == len(examples)
        and len({item[0] for item in topological}) == 1
    ):
        examples.sort(
            key=lambda item: (
                cast(int, item.metadata["topological_index"]),
                item.id,
            )
        )
        positions = [position for _, position in topological]
        if len(positions) != len(set(positions)):
            warnings.append(
                "duplicate first-parent positions detected; equal positions use a stable "
                "id tie-break"
            )
        instants = [parse_timestamp(item.observed_at) for item in examples]
        if any(later <= earlier for earlier, later in pairwise(instants)):
            warnings.append(
                "commit timestamps are tied or non-monotonic; split uses first-parent topology"
            )
    else:
        examples.sort(key=lambda item: (parse_timestamp(item.observed_at), item.id))
        instants = [parse_timestamp(item.observed_at) for item in examples]
        if len(instants) != len(set(instants)):
            warnings.append("observed_at ties detected; equal instants use a stable id tie-break")
        if topological:
            warnings.append(
                "incomplete or mixed Git topology metadata; split falls back to observed_at"
            )
    if test_start_at is not None:
        boundary_at = parse_timestamp(test_start_at)
        train = tuple(item for item in examples if parse_timestamp(item.observed_at) < boundary_at)
        test = tuple(item for item in examples if parse_timestamp(item.observed_at) >= boundary_at)
        if len(train) < min_train or len(test) < min_test:
            warnings.append(
                f"fixed temporal boundary produced {len(train)} train and {len(test)} test "
                f"examples; configured minima are {min_train} and {min_test}"
            )
        return TemporalSplit(train, test, tuple(warnings))
    if len(examples) < min_train + min_test:
        warnings.append(
            f"only {len(examples)} mature labels; need {min_train + min_test} for the "
            "configured temporal holdout"
        )
        if len(examples) < 2:
            return TemporalSplit(tuple(examples), (), tuple(warnings))
        test_size = max(1, min(len(examples) - 1, math.ceil(len(examples) * test_fraction)))
    else:
        test_size = max(min_test, math.ceil(len(examples) * test_fraction))
        test_size = min(test_size, len(examples) - min_train)
    boundary = len(examples) - test_size
    return TemporalSplit(tuple(examples[:boundary]), tuple(examples[boundary:]), tuple(warnings))


def evaluate(
    observations: Sequence[Observation],
    target: str,
    predictor: Predictor,
    *,
    as_of: datetime | None = None,
) -> Metrics:
    tp = fp = tn = fn = 0
    for item in labeled(observations, target, as_of=as_of):
        actual = item.labels[target] is LabelValue.POSITIVE
        predicted = predictor(item.facts)
        if actual and predicted:
            tp += 1
        elif not actual and predicted:
            fp += 1
        elif not actual and not predicted:
            tn += 1
        else:
            fn += 1
    return Metrics.from_counts(tp, fp, tn, fn)


def majority_baseline(
    observations: Sequence[Observation],
    target: str,
    *,
    as_of: datetime | None = None,
) -> tuple[bool, Metrics]:
    examples = labeled(observations, target, as_of=as_of)
    positives = sum(item.labels[target] is LabelValue.POSITIVE for item in examples)
    prediction = positives > len(examples) - positives
    return prediction, evaluate(examples, target, lambda _facts: prediction, as_of=as_of)


def best_literal_baseline(
    train: Sequence[Observation],
    test: Sequence[Observation],
    target: str,
    *,
    as_of: datetime | None = None,
) -> tuple[str | None, Metrics]:
    predicates = sorted({fact for item in train for fact in item.facts})
    variants: list[tuple[str, Predictor]] = []
    for predicate in predicates:

        def present(facts: frozenset[str], predicate: str = predicate) -> bool:
            return predicate in facts

        def absent(facts: frozenset[str], predicate: str = predicate) -> bool:
            return predicate not in facts

        variants.append((predicate, present))
        variants.append((f"not_{predicate}", absent))
    if not variants:
        return None, Metrics.from_counts(0, 0, 0, 0)
    name, predictor = max(
        variants,
        key=lambda pair: (
            evaluate(train, target, pair[1], as_of=as_of).matthews_correlation,
            evaluate(train, target, pair[1], as_of=as_of).f1,
            evaluate(train, target, pair[1], as_of=as_of).precision,
            pair[0],
        ),
    )
    return name, evaluate(test, target, predictor, as_of=as_of)


def _score_metrics(scores: Sequence[float], labels: Sequence[bool], threshold: float) -> Metrics:
    tp = fp = tn = fn = 0
    for score, actual in zip(scores, labels, strict=True):
        predicted = score >= threshold
        if actual and predicted:
            tp += 1
        elif not actual and predicted:
            fp += 1
        elif not actual and not predicted:
            tn += 1
        else:
            fn += 1
    return Metrics.from_counts(tp, fp, tn, fn)


def fit_boolean_logistic_baseline(
    observations: Sequence[Observation],
    target: str,
    *,
    as_of: datetime | None = None,
    iterations: int = 400,
    learning_rate: float = 0.1,
    l2: float = 1.0,
    max_predicates: int = 256,
) -> BooleanLogisticModel:
    """Fit a class-balanced logistic baseline using training data only."""

    examples = sorted(labeled(observations, target, as_of=as_of), key=lambda item: item.id)
    if not examples:
        raise ModelError("logistic baseline requires at least one mature training label")
    if not 1 <= iterations <= 10_000:
        raise ModelError("logistic baseline iterations must be between 1 and 10000")
    if not 0 < learning_rate <= 1 or not math.isfinite(learning_rate):
        raise ModelError("logistic baseline learning_rate must be finite and in (0, 1]")
    if l2 < 0 or not math.isfinite(l2):
        raise ModelError("logistic baseline l2 must be finite and non-negative")
    predicates = tuple(sorted({fact for item in examples for fact in item.facts}))
    if len(predicates) > max_predicates:
        raise ModelError(
            f"logistic baseline has {len(predicates)} predicates; limit is {max_predicates}"
        )
    labels = [item.labels[target] is LabelValue.POSITIVE for item in examples]
    positive_count = sum(labels)
    negative_count = len(labels) - positive_count
    weights = [0.0] * len(predicates)
    intercept = 0.0
    positive_weight = len(labels) / (2 * positive_count) if positive_count else 1.0
    negative_weight = len(labels) / (2 * negative_count) if negative_count else 1.0
    active_features = [
        tuple(index for index, predicate in enumerate(predicates) if predicate in item.facts)
        for item in examples
    ]
    for _ in range(iterations):
        gradient = [0.0] * len(weights)
        intercept_gradient = 0.0
        for features, actual in zip(active_features, labels, strict=True):
            logit = intercept + sum(weights[index] for index in features)
            if logit >= 0:
                probability = 1.0 / (1.0 + math.exp(-min(logit, 35.0)))
            else:
                exponential = math.exp(max(logit, -35.0))
                probability = exponential / (1.0 + exponential)
            sample_weight = positive_weight if actual else negative_weight
            error = sample_weight * (probability - float(actual))
            intercept_gradient += error
            for index in features:
                gradient[index] += error
        denominator = float(len(examples))
        intercept -= learning_rate * intercept_gradient / denominator
        for index, value in enumerate(weights):
            weights[index] -= learning_rate * (gradient[index] + l2 * value) / denominator
    provisional = BooleanLogisticModel(
        predicates=predicates,
        weights=tuple(weights),
        intercept=intercept,
        threshold=0.5,
        iterations=iterations,
        learning_rate=learning_rate,
        l2=l2,
    )
    scores = [provisional.score(item.facts) for item in examples]
    unique_thresholds = sorted(set(scores))
    if len(unique_thresholds) > 1024:
        unique_thresholds = [
            unique_thresholds[index * (len(unique_thresholds) - 1) // 1023] for index in range(1024)
        ]
    thresholds = [0.0, *unique_thresholds, 1.000000000001]
    threshold = max(
        thresholds,
        key=lambda value: (
            (metrics := _score_metrics(scores, labels, value)).matthews_correlation,
            metrics.f1,
            metrics.precision,
            -metrics.predicted_positive_rate,
            value,
        ),
    )
    return replace(provisional, threshold=threshold)


def bootstrap_stability(
    observations: Sequence[Observation],
    target: str,
    reference: RuleSet,
    learner: Learner,
    *,
    runs: int,
    seed: int,
) -> float:
    if runs == 0 or not observations:
        return 0.0
    generator = random.Random(seed)
    scores: list[float] = []
    for run_index in range(runs):
        sample = [
            replace(
                generator.choice(observations),
                id=f"bootstrap.{run_index}.{sample_index}",
            )
            for sample_index in range(len(observations))
        ]
        learned = learner(sample, target)
        union = reference.signatures | learned.signatures
        score = 1.0 if not union else len(reference.signatures & learned.signatures) / len(union)
        scores.append(score)
    return sum(scores) / len(scores)
