"""Leakage-resistant evaluation primitives for learned rules."""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from itertools import pairwise
from typing import cast

from ruleloom.models import LabelValue, Metrics, ModelError, Observation, RuleSet, parse_timestamp

Predictor = Callable[[frozenset[str]], bool]
Learner = Callable[[Sequence[Observation], str], RuleSet]


@dataclass(frozen=True, slots=True)
class TemporalSplit:
    train: tuple[Observation, ...]
    test: tuple[Observation, ...]
    warnings: tuple[str, ...] = ()


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
