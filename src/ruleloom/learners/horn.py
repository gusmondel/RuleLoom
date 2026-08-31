"""A small, deterministic separate-and-conquer Horn learner.

This engine is intentionally bounded and dependency-free. It is the portable
baseline; the Popper adapter is the preferred option when noisy labels warrant
MDL-based learning.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from itertools import combinations, product

from ruleloom.models import (
    HornClause,
    LabelValue,
    ModelError,
    Observation,
    RuleLiteral,
    RuleSet,
)


@dataclass(frozen=True, slots=True)
class HornSettings:
    max_body: int = 3
    max_rules: int = 3
    allow_negation: bool = True
    min_precision: float = 0.7
    min_support: int = 2
    false_positive_cost: float = 1.5
    max_predicates: int = 12


@dataclass(slots=True)
class HornBudget:
    """Hard aggregate cap on literal checks across initial and bootstrap searches."""

    limit: int
    consumed: int = 0

    def matches(self, clause: HornClause, observation: Observation) -> bool:
        self.consumed += len(clause.body)
        if self.consumed > self.limit:
            raise ModelError(
                "Horn search exceeded the hard literal-check budget; reduce observations, "
                "max_body, max_rules, max_predicates, or bootstrap_runs"
            )
        return clause.matches(observation.facts)


@dataclass(frozen=True, slots=True)
class _ScoredClause:
    clause: HornClause
    covered_positive_ids: frozenset[str]
    true_positive: int
    false_positive: int
    precision: float
    utility: float


def _literal_bodies(
    predicates: Sequence[str], max_body: int, allow_negation: bool
) -> Iterator[tuple[RuleLiteral, ...]]:
    for length in range(1, min(max_body, len(predicates)) + 1):
        for selected in combinations(predicates, length):
            signs: Iterable[tuple[bool, ...]]
            signs = product((False, True), repeat=length) if allow_negation else [(False,) * length]
            for negations in signs:
                yield tuple(
                    RuleLiteral(predicate=predicate, negated=negated)
                    for predicate, negated in zip(selected, negations, strict=True)
                )


def rank_predicates(examples: Sequence[Observation], target: str) -> list[str]:
    """Rank predicates deterministically by class-presence discrimination."""
    facts = sorted({fact for item in examples for fact in item.facts})

    def discrimination(predicate: str) -> tuple[float, int, str]:
        positive_present = sum(
            predicate in item.facts and item.labels[target] is LabelValue.POSITIVE
            for item in examples
        )
        negative_present = sum(
            predicate in item.facts and item.labels[target] is LabelValue.NEGATIVE
            for item in examples
        )
        return (
            abs(positive_present - negative_present),
            positive_present + negative_present,
            predicate,
        )

    return sorted(facts, key=discrimination, reverse=True)


def _score_clause(
    clause: HornClause,
    positives: Sequence[Observation],
    negatives: Sequence[Observation],
    uncovered: frozenset[str],
    false_positive_cost: float,
    budget: HornBudget,
) -> _ScoredClause:
    covered_positive_ids = frozenset(item.id for item in positives if budget.matches(clause, item))
    new_true_positive = len(covered_positive_ids & uncovered)
    false_positive = sum(budget.matches(clause, item) for item in negatives)
    total_positive = len(covered_positive_ids)
    precision = (
        total_positive / (total_positive + false_positive) if total_positive + false_positive else 0
    )
    complexity_cost = 0.05 * len(clause.body)
    utility = new_true_positive - false_positive_cost * false_positive - complexity_cost
    return _ScoredClause(
        clause=clause,
        covered_positive_ids=covered_positive_ids,
        true_positive=new_true_positive,
        false_positive=false_positive,
        precision=precision,
        utility=utility,
    )


def _combined_precision(
    clauses: Sequence[HornClause],
    positives: Sequence[Observation],
    negatives: Sequence[Observation],
    budget: HornBudget,
) -> float:
    true_positive = sum(
        any(budget.matches(clause, item) for clause in clauses) for item in positives
    )
    false_positive = sum(
        any(budget.matches(clause, item) for clause in clauses) for item in negatives
    )
    return (
        true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
    )


def learn_horn(
    observations: Sequence[Observation],
    target: str,
    settings: HornSettings | None = None,
    *,
    budget: HornBudget | None = None,
) -> RuleSet:
    """Learn a disjunction of bounded Horn clauses from positive/negative examples."""
    options = settings or HornSettings()
    active_budget = budget or HornBudget(50_000_000)
    examples = [
        item
        for item in observations
        if item.labels.get(target, LabelValue.UNKNOWN) is not LabelValue.UNKNOWN
    ]
    all_facts = {fact for item in examples for fact in item.facts}
    if target in all_facts:
        raise ModelError(
            f"target {target!r} appears as a prediction-time fact; this would leak the label"
        )
    reserved = sorted(fact for fact in all_facts if fact.startswith("not_"))
    if reserved:
        raise ModelError(
            "fact predicates starting with 'not_' are reserved for closed-world negation: "
            + ", ".join(reserved)
        )
    positives = [item for item in examples if item.labels[target] is LabelValue.POSITIVE]
    negatives = [item for item in examples if item.labels[target] is LabelValue.NEGATIVE]
    predicates = rank_predicates(examples, target)[: options.max_predicates]
    uncovered = frozenset(item.id for item in positives)
    selected: list[HornClause] = []

    for _ in range(options.max_rules):
        candidates: list[_ScoredClause] = []
        for body in _literal_bodies(predicates, options.max_body, options.allow_negation):
            scored = _score_clause(
                HornClause(target=target, body=body),
                positives,
                negatives,
                uncovered,
                options.false_positive_cost,
                active_budget,
            )
            if (
                scored.true_positive >= options.min_support
                and scored.precision >= options.min_precision
                and scored.utility > 0
                and _combined_precision(
                    [*selected, scored.clause], positives, negatives, active_budget
                )
                >= options.min_precision
            ):
                candidates.append(scored)
        if not candidates:
            break
        best = max(
            candidates,
            key=lambda item: (
                item.utility,
                item.precision,
                item.true_positive,
                -item.false_positive,
                -len(item.clause.body),
                item.clause.signature,
            ),
        )
        selected.append(best.clause)
        uncovered -= best.covered_positive_ids
        if not uncovered:
            break
    return RuleSet(target=target, clauses=tuple(selected))
