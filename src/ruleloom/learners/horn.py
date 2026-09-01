"""A small, deterministic separate-and-conquer Horn learner.

This engine is intentionally bounded and dependency-free. It is the portable
baseline; the Popper adapter is the preferred option when noisy labels warrant
MDL-based learning.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from fractions import Fraction
from itertools import combinations, product

from ruleloom.models import (
    HornClause,
    LabelValue,
    ModelError,
    Observation,
    RuleLiteral,
    RuleSet,
)

HORN_ENGINE_VERSION = "ruleloom-horn/0.4"


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

    def consume(self, work_units: int) -> None:
        if work_units < 0:
            raise ModelError("Horn work units cannot be negative")
        self.consumed += work_units
        if self.consumed > self.limit:
            raise ModelError(
                "Horn search exceeded the hard bitset-work budget; reduce observations, "
                "max_body, max_rules, max_predicates, or bootstrap_runs"
            )

    def matches(self, clause: HornClause, observation: Observation) -> bool:
        self.consume(len(clause.body))
        return clause.matches(observation.facts)


@dataclass(frozen=True, slots=True)
class PredicateSelection:
    """Deterministic predicate preprocessing over one labelled training cohort."""

    labelled_observations: int
    positive_observations: int
    negative_observations: int
    observed_predicates: tuple[str, ...]
    constant_predicates: tuple[str, ...]
    duplicate_groups: tuple[tuple[str, ...], ...]
    ranked_predicates: tuple[str, ...]

    @property
    def duplicate_predicates(self) -> tuple[str, ...]:
        """Aliases removed after retaining each group's lexical representative."""
        return tuple(alias for group in self.duplicate_groups for alias in group[1:])


@dataclass(frozen=True, slots=True)
class _ScoredClause:
    clause: HornClause
    covered_positive_ids: frozenset[str]
    true_positive: int
    false_positive: int
    precision: float
    utility: float


@dataclass(frozen=True, slots=True)
class _BitScoredClause:
    clause: HornClause
    coverage: int
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


def select_train_predicates(
    examples: Sequence[Observation],
    target: str,
    *,
    allow_negation: bool = True,
) -> PredicateSelection:
    """Filter and rank predicates using only the supplied training observations.

    Unknown labels do not participate. Structurally constant columns are
    removed, and predicates with identical truth columns retain only their
    lexicographically first representative. This structural reduction does not
    inspect target values, although cohort membership is limited to labelled
    training observations. With negation available, representatives are ranked
    by their absolute class-conditional rate gap. Without negation, the signed
    positive-minus-negative rate gap prevents a negative-only signal from
    displacing an equally strong positive literal. If either class is absent,
    no gap is defined and the empty ranking forces the learner to abstain.
    """
    labelled = tuple(
        item
        for item in examples
        if item.labels.get(target, LabelValue.UNKNOWN) is not LabelValue.UNKNOWN
    )
    positives = tuple(item for item in labelled if item.labels[target] is LabelValue.POSITIVE)
    negatives = tuple(item for item in labelled if item.labels[target] is LabelValue.NEGATIVE)
    facts = tuple(sorted({fact for item in labelled for fact in item.facts}))
    columns = {
        predicate: tuple(predicate in item.facts for item in labelled) for predicate in facts
    }
    constant_predicates = tuple(
        predicate for predicate in facts if len(set(columns[predicate])) <= 1
    )
    column_groups: dict[tuple[bool, ...], list[str]] = {}
    for predicate in facts:
        column = columns[predicate]
        if len(set(column)) <= 1:
            continue
        column_groups.setdefault(column, []).append(predicate)
    duplicate_groups = tuple(
        tuple(group)
        for group in sorted(column_groups.values(), key=lambda item: item[0])
        if len(group) > 1
    )
    representatives = tuple(group[0] for group in column_groups.values())

    if not positives or not negatives:
        ranked_predicates: tuple[str, ...] = ()
    else:

        def ranking_key(predicate: str) -> tuple[Fraction, str]:
            positive_present = sum(predicate in item.facts for item in positives)
            negative_present = sum(predicate in item.facts for item in negatives)
            signed_rate_gap = Fraction(positive_present, len(positives)) - Fraction(
                negative_present, len(negatives)
            )
            rate_gap = abs(signed_rate_gap) if allow_negation else signed_rate_gap
            return -rate_gap, predicate

        ranked_predicates = tuple(sorted(representatives, key=ranking_key))

    return PredicateSelection(
        labelled_observations=len(labelled),
        positive_observations=len(positives),
        negative_observations=len(negatives),
        observed_predicates=facts,
        constant_predicates=constant_predicates,
        duplicate_groups=duplicate_groups,
        ranked_predicates=ranked_predicates,
    )


def rank_predicates(
    examples: Sequence[Observation],
    target: str,
    *,
    allow_negation: bool = True,
) -> list[str]:
    """Return structurally reduced predicates ranked on the supplied train cohort."""
    return list(
        select_train_predicates(
            examples,
            target,
            allow_negation=allow_negation,
        ).ranked_predicates
    )


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
    predicates = rank_predicates(
        examples,
        target,
        allow_negation=options.allow_negation,
    )[: options.max_predicates]
    all_mask = (1 << len(examples)) - 1
    positive_mask = sum(
        1 << index
        for index, item in enumerate(examples)
        if item.labels[target] is LabelValue.POSITIVE
    )
    negative_mask = all_mask ^ positive_mask
    present_masks = {
        predicate: sum(1 << index for index, item in enumerate(examples) if predicate in item.facts)
        for predicate in predicates
    }
    uncovered = positive_mask
    selected: list[HornClause] = []
    selected_coverage = 0
    word_count = max(1, (len(examples) + 63) // 64)

    for _ in range(options.max_rules):
        candidates: list[_BitScoredClause] = []
        for body in _literal_bodies(predicates, options.max_body, options.allow_negation):
            active_budget.consume(len(body) * word_count)
            coverage = all_mask
            for literal in body:
                present = present_masks[literal.predicate]
                coverage &= all_mask ^ present if literal.negated else present
            covered_positive = coverage & positive_mask
            true_positive = (covered_positive & uncovered).bit_count()
            total_positive = covered_positive.bit_count()
            false_positive = (coverage & negative_mask).bit_count()
            precision = (
                total_positive / (total_positive + false_positive)
                if total_positive + false_positive
                else 0.0
            )
            utility = (
                true_positive - options.false_positive_cost * false_positive - 0.05 * len(body)
            )
            combined = selected_coverage | coverage
            combined_true_positive = (combined & positive_mask).bit_count()
            combined_false_positive = (combined & negative_mask).bit_count()
            combined_precision = (
                combined_true_positive / (combined_true_positive + combined_false_positive)
                if combined_true_positive + combined_false_positive
                else 0.0
            )
            if (
                true_positive >= options.min_support
                and precision >= options.min_precision
                and utility > 0
                and combined_precision >= options.min_precision
            ):
                candidates.append(
                    _BitScoredClause(
                        clause=HornClause(target=target, body=body),
                        coverage=coverage,
                        true_positive=true_positive,
                        false_positive=false_positive,
                        precision=precision,
                        utility=utility,
                    )
                )
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
        selected_coverage |= best.coverage
        uncovered &= ~best.coverage
        if not uncovered:
            break
    return RuleSet(target=target, clauses=tuple(selected))
