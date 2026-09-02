"""A small, deterministic separate-and-conquer Horn learner.

This engine is intentionally bounded and dependency-free. It is the portable
baseline; the Popper adapter is the preferred option when noisy labels warrant
MDL-based learning.

Version 0.6 adds four train-only controls that older configurations keep
disabled so their candidates and hashes remain reproducible:

- a beam search that refines bodies over every eligible predicate instead of
  enumerating conjunctions over a small marginal-ranked prefix;
- a Wilson lower-bound precision estimate for the absolute gate and the
  selection order, so a two-example clause cannot pass on point precision;
- a temporal-consistency gate requiring a clause to beat the base rate in both
  chronological halves of the training window;
- chronological grow/prune windows in the RIPPER style plus an optional
  within-block label-permutation null that calibrates the best train statistic.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from fractions import Fraction
from itertools import combinations, product
from typing import cast

from ruleloom.models import (
    HornClause,
    JsonObject,
    JsonValue,
    LabelValue,
    Metrics,
    ModelError,
    Observation,
    RuleLiteral,
    RuleSet,
    parse_timestamp,
)
from ruleloom.signal_probe import conservative_lift_diagnostic, wilson_interval

HORN_ENGINE_VERSION = "ruleloom-horn/0.6"
SEARCH_STRATEGIES = ("exhaustive", "beam")
PRECISION_ESTIMATES = ("point", "wilson_lower")
BEAM_RANKINGS = ("laplace", "wracc")
UTILITY_COST_BASES = ("absolute", "prior_odds")
MAX_PRUNE_FRACTION = 0.5
MAX_PERMUTATION_RUNS = 1000
PERMUTATION_BLOCKS = 4
_MAX_PRUNE_ATTEMPTS = 8
_COMPLEXITY_COST = 0.05


@dataclass(frozen=True, slots=True)
class HornSettings:
    max_body: int = 3
    max_rules: int = 3
    allow_negation: bool = True
    min_precision: float = 0.7
    min_support: int = 2
    false_positive_cost: float = 1.5
    max_predicates: int = 12
    gate_mode: str = "absolute_precision"
    min_lift_lower_bound: float = 3.0
    min_alert_rate: float = 0.01
    confidence_level: float = 0.95
    near_miss_limit: int = 10
    search_strategy: str = "exhaustive"
    beam_width: int = 20
    beam_ranking: str = "laplace"
    utility_cost_basis: str = "absolute"
    precision_estimate: str = "point"
    require_temporal_consistency: bool = False
    prune_fraction: float = 0.0
    permutation_runs: int = 0
    seed: int = 17

    def __post_init__(self) -> None:
        if self.search_strategy not in SEARCH_STRATEGIES:
            raise ModelError("search_strategy must be one of: " + ", ".join(SEARCH_STRATEGIES))
        if self.precision_estimate not in PRECISION_ESTIMATES:
            raise ModelError("precision_estimate must be one of: " + ", ".join(PRECISION_ESTIMATES))
        if self.beam_ranking not in BEAM_RANKINGS:
            raise ModelError("beam_ranking must be one of: " + ", ".join(BEAM_RANKINGS))
        if self.utility_cost_basis not in UTILITY_COST_BASES:
            raise ModelError("utility_cost_basis must be one of: " + ", ".join(UTILITY_COST_BASES))
        if isinstance(self.beam_width, bool) or not isinstance(self.beam_width, int):
            raise ModelError("beam_width must be an integer")
        if not 1 <= self.beam_width <= 256:
            raise ModelError("beam_width must be between 1 and 256")
        if (
            isinstance(self.prune_fraction, bool)
            or not isinstance(self.prune_fraction, int | float)
            or not math.isfinite(self.prune_fraction)
            or not 0 <= self.prune_fraction <= MAX_PRUNE_FRACTION
        ):
            raise ModelError(f"prune_fraction must be between 0 and {MAX_PRUNE_FRACTION}")
        if (
            isinstance(self.permutation_runs, bool)
            or not isinstance(self.permutation_runs, int)
            or not 0 <= self.permutation_runs <= MAX_PERMUTATION_RUNS
        ):
            raise ModelError(f"permutation_runs must be between 0 and {MAX_PERMUTATION_RUNS}")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            raise ModelError("seed must be a non-negative integer")


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
class ClauseDiagnostic:
    clause: HornClause
    true_positive: int
    false_positive: int
    true_negative: int
    false_negative: int
    utility: float
    lift: JsonObject
    rejection_reasons: tuple[str, ...]

    @property
    def metrics(self) -> Metrics:
        return Metrics.from_counts(
            self.true_positive,
            self.false_positive,
            self.true_negative,
            self.false_negative,
        )

    def to_dict(self) -> JsonObject:
        return {
            "signature": self.clause.signature,
            "rule": self.clause.to_dict(),
            "prolog": self.clause.to_prolog(),
            "metrics": self.metrics.to_dict(),
            "support": self.true_positive,
            "utility": self.utility,
            "lift": self.lift,
            "rejection_reasons": list(self.rejection_reasons),
            "selection_scope": "train_only_exploratory",
            "post_selection_inference": False,
        }


@dataclass(frozen=True, slots=True)
class HornLearningResult:
    rules: RuleSet
    near_misses: tuple[ClauseDiagnostic, ...]
    hypotheses_examined: int
    unique_hypotheses_examined: int
    search: JsonObject
    pruning: JsonObject
    permutation_null: JsonObject | None = None

    def diagnostics_dict(self) -> JsonObject:
        return {
            "near_misses": [item.to_dict() for item in self.near_misses],
            "hypotheses_examined": self.hypotheses_examined,
            "unique_hypotheses_examined": self.unique_hypotheses_examined,
            "search": self.search,
            "pruning": self.pruning,
            "permutation_null": self.permutation_null,
            "selection_scope": "train_only_exploratory",
            "multiple_testing_warning": (
                "near-misses were selected after searching many hypotheses and are not "
                "confirmatory evidence"
            ),
        }


@dataclass(frozen=True, slots=True)
class _Cohort:
    """Bitset view of a chronologically ordered labelled training cohort."""

    examples: tuple[Observation, ...]
    all_mask: int
    positive_mask: int
    present_masks: dict[str, int]
    word_count: int
    first_half: int
    second_half: int

    @property
    def negative_mask(self) -> int:
        return self.all_mask ^ self.positive_mask


@dataclass(frozen=True, slots=True)
class _Evaluated:
    clause: HornClause
    coverage: int
    new_true_positive: int
    true_positive: int
    false_positive: int
    true_negative: int
    false_negative: int
    precision: float
    precision_estimate: float
    utility: float
    lift: JsonObject
    reasons: tuple[str, ...]

    @property
    def metrics(self) -> Metrics:
        return Metrics.from_counts(
            self.true_positive,
            self.false_positive,
            self.true_negative,
            self.false_negative,
        )


@dataclass(slots=True)
class _SearchState:
    record: bool
    diagnostics: dict[str, ClauseDiagnostic]
    hypotheses_examined: int = 0
    unique_hypotheses: set[str] | None = None

    def __post_init__(self) -> None:
        if self.unique_hypotheses is None:
            self.unique_hypotheses = set()


def _gate_reasons(metrics: Metrics, precision_estimate: float, options: HornSettings) -> list[str]:
    reasons: list[str] = []
    if options.gate_mode == "absolute_precision":
        if precision_estimate < options.min_precision:
            reasons.append(
                "precision_below_absolute_minimum"
                if options.precision_estimate == "point"
                else "precision_lower_bound_below_minimum"
            )
        return reasons
    lift = conservative_lift_diagnostic(metrics, options.confidence_level)
    lower = lift["conservative_lift_lower"]
    if not isinstance(lower, int | float) or lower < options.min_lift_lower_bound:
        reasons.append("conservative_lift_below_minimum")
    if metrics.predicted_positive_rate < options.min_alert_rate:
        reasons.append("alert_rate_below_minimum")
    return reasons


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


def apply_predicate_order(
    ranked: Sequence[str], preferred_order: Sequence[str] | None
) -> tuple[str, ...]:
    """Reorder eligible predicates by a train-only preference without adding any."""
    if preferred_order is None:
        return tuple(ranked)
    eligible = set(ranked)
    ordered = [predicate for predicate in dict.fromkeys(preferred_order) if predicate in eligible]
    seen = set(ordered)
    ordered.extend(predicate for predicate in ranked if predicate not in seen)
    return tuple(ordered)


def _chronological(examples: Iterable[Observation]) -> tuple[Observation, ...]:
    return tuple(sorted(examples, key=lambda item: (parse_timestamp(item.observed_at), item.id)))


def _laplace(true_positive: int, false_positive: int) -> Fraction:
    return Fraction(true_positive + 1, true_positive + false_positive + 2)


def _false_positive_cost(options: HornSettings, *, positives: int, negatives: int) -> float:
    """Cost of one false alert in true-positive units.

    ``absolute`` charges ``false_positive_cost`` per false alert, so utility is
    positive only above the precision floor cost / (1 + cost) whatever the base
    rate (Elkan 2001). ``prior_odds`` scales that charge by the train prior odds
    positives / negatives, so utility is positive exactly when the clause's odds
    of being right exceed ``false_positive_cost`` times the prior odds; this keeps
    the utility gate consistent with a relative-lift gate at low prevalence.
    """
    if options.utility_cost_basis == "prior_odds" and negatives > 0 and positives > 0:
        return options.false_positive_cost * positives / negatives
    return options.false_positive_cost


def _temporal_reasons(
    coverage: int,
    cohort: _Cohort,
    positive_mask: int,
    scope: int,
) -> list[str]:
    for half in (cohort.first_half, cohort.second_half):
        part = half & scope
        total = part.bit_count()
        if total == 0:
            continue
        positives = (positive_mask & part).bit_count()
        covered = (coverage & part).bit_count()
        covered_positive = (coverage & positive_mask & part).bit_count()
        if covered_positive == 0 or covered == 0:
            return ["unstable_across_train_halves"]
        if Fraction(covered_positive, covered) <= Fraction(positives, total):
            return ["unstable_across_train_halves"]
    return []


def _evaluate(
    body: tuple[RuleLiteral, ...],
    *,
    target: str,
    cohort: _Cohort,
    scope: int,
    positive_mask: int,
    uncovered: int,
    selected_coverage: int,
    options: HornSettings,
    budget: HornBudget,
) -> _Evaluated:
    budget.consume(len(body) * cohort.word_count)
    coverage = cohort.all_mask
    for literal in body:
        present = cohort.present_masks[literal.predicate]
        coverage &= cohort.all_mask ^ present if literal.negated else present
    scoped_positive = positive_mask & scope
    scoped_negative = scope ^ scoped_positive
    covered_positive = coverage & scoped_positive
    true_positive = covered_positive.bit_count()
    new_true_positive = (covered_positive & uncovered).bit_count()
    false_positive = (coverage & scoped_negative).bit_count()
    alerted = true_positive + false_positive
    precision = true_positive / alerted if alerted else 0.0
    if options.precision_estimate == "point":
        estimate = precision
    else:
        estimate = wilson_interval(true_positive, alerted, options.confidence_level)[0]
    true_negative = scoped_negative.bit_count() - false_positive
    false_negative = scoped_positive.bit_count() - true_positive
    utility = (
        new_true_positive
        - _false_positive_cost(
            options,
            positives=scoped_positive.bit_count(),
            negatives=scoped_negative.bit_count(),
        )
        * false_positive
        - _COMPLEXITY_COST * len(body)
    )
    metrics = Metrics.from_counts(true_positive, false_positive, true_negative, false_negative)
    reasons = _gate_reasons(metrics, estimate, options)
    if new_true_positive < options.min_support:
        reasons.append("new_support_below_minimum")
    if utility <= 0:
        reasons.append("utility_not_positive")
    combined = (selected_coverage | coverage) & scope
    combined_true_positive = (combined & scoped_positive).bit_count()
    combined_false_positive = (combined & scoped_negative).bit_count()
    combined_metrics = Metrics.from_counts(
        combined_true_positive,
        combined_false_positive,
        scoped_negative.bit_count() - combined_false_positive,
        scoped_positive.bit_count() - combined_true_positive,
    )
    combined_alerted = combined_true_positive + combined_false_positive
    combined_precision = combined_true_positive / combined_alerted if combined_alerted else 0.0
    if options.precision_estimate == "point":
        combined_estimate = combined_precision
    else:
        combined_estimate = wilson_interval(
            combined_true_positive, combined_alerted, options.confidence_level
        )[0]
    reasons.extend(
        f"combined_{reason}"
        for reason in _gate_reasons(combined_metrics, combined_estimate, options)
    )
    if options.require_temporal_consistency:
        reasons.extend(_temporal_reasons(coverage, cohort, positive_mask, scope))
    return _Evaluated(
        clause=HornClause(target=target, body=body),
        coverage=coverage,
        new_true_positive=new_true_positive,
        true_positive=true_positive,
        false_positive=false_positive,
        true_negative=true_negative,
        false_negative=false_negative,
        precision=precision,
        precision_estimate=estimate,
        utility=utility,
        lift=conservative_lift_diagnostic(metrics, options.confidence_level),
        reasons=tuple(dict.fromkeys(reasons)),
    )


def _statistic(evaluated: _Evaluated, options: HornSettings) -> float:
    """Train statistic calibrated by the permutation null; zero below minimum support."""
    if evaluated.true_positive < options.min_support:
        return 0.0
    if options.gate_mode == "relative_lift":
        lower = evaluated.lift["conservative_lift_lower"]
        return float(lower) if isinstance(lower, int | float) else 0.0
    return evaluated.precision_estimate


def _selection_key(item: _Evaluated) -> tuple[float, float, int, int, int, str]:
    return (
        item.utility,
        item.precision_estimate,
        item.new_true_positive,
        -item.false_positive,
        -len(item.clause.body),
        item.clause.signature,
    )


def _weighted_relative_accuracy(item: _Evaluated, base_rate: Fraction) -> Fraction:
    """Excess new true positives over chance: coverage times (precision minus base rate).

    Weighted relative accuracy (Lavrac, Flach and Zupan 1999; CN2-SD) trades coverage
    against precision linearly, so a beam ordered by it keeps broad literals whose
    refinements can satisfy both a lift and an alert-rate gate, where a Laplace
    ordering fills the beam with tiny, pure clauses at low prevalence.
    """
    covered = item.new_true_positive + item.false_positive
    return Fraction(item.new_true_positive) - Fraction(covered) * base_rate


def _beam_key(
    item: _Evaluated, *, ranking: str, base_rate: Fraction
) -> tuple[Fraction, Fraction, int, int, int, str]:
    primary = -_weighted_relative_accuracy(item, base_rate) if ranking == "wracc" else Fraction(0)
    return (
        primary,
        -_laplace(item.new_true_positive, item.false_positive),
        -item.new_true_positive,
        item.false_positive,
        len(item.clause.body),
        item.clause.signature,
    )


def _select_beam(
    level: Sequence[_Evaluated], options: HornSettings, *, base_rate: Fraction
) -> list[_Evaluated]:
    eligible = [item for item in level if item.new_true_positive >= options.min_support]
    ordered = sorted(
        eligible,
        key=lambda item: _beam_key(item, ranking=options.beam_ranking, base_rate=base_rate),
    )
    return ordered[: options.beam_width]


def _search_iteration(
    *,
    target: str,
    cohort: _Cohort,
    predicates: Sequence[str],
    order: dict[str, int],
    scope: int,
    positive_mask: int,
    uncovered: int,
    selected_coverage: int,
    options: HornSettings,
    budget: HornBudget,
    seed_bodies: Sequence[tuple[RuleLiteral, ...]],
    state: _SearchState,
) -> tuple[list[_Evaluated], float]:
    """Return gate-passing candidates for one rule plus the best train statistic."""
    candidates: list[_Evaluated] = []
    seen: set[str] = set()
    best_statistic = 0.0

    def consider(body: tuple[RuleLiteral, ...]) -> _Evaluated | None:
        nonlocal best_statistic
        signature = HornClause(target=target, body=body).signature
        if signature in seen:
            return None
        seen.add(signature)
        evaluated = _evaluate(
            body,
            target=target,
            cohort=cohort,
            scope=scope,
            positive_mask=positive_mask,
            uncovered=uncovered,
            selected_coverage=selected_coverage,
            options=options,
            budget=budget,
        )
        state.hypotheses_examined += 1
        assert state.unique_hypotheses is not None
        state.unique_hypotheses.add(signature)
        best_statistic = max(best_statistic, _statistic(evaluated, options))
        if evaluated.reasons:
            if state.record and signature not in state.diagnostics:
                state.diagnostics[signature] = ClauseDiagnostic(
                    clause=evaluated.clause,
                    true_positive=evaluated.true_positive,
                    false_positive=evaluated.false_positive,
                    true_negative=evaluated.true_negative,
                    false_negative=evaluated.false_negative,
                    utility=evaluated.utility,
                    lift=evaluated.lift,
                    rejection_reasons=evaluated.reasons,
                )
        else:
            candidates.append(evaluated)
        return evaluated

    signs = (False, True) if options.allow_negation else (False,)
    if options.search_strategy == "exhaustive":
        for body in _literal_bodies(predicates, options.max_body, options.allow_negation):
            consider(body)
        for body in seed_bodies:
            consider(body)
        return candidates, best_statistic

    scoped_examples = scope.bit_count()
    base_rate = (
        Fraction((uncovered & scope).bit_count(), scoped_examples)
        if scoped_examples
        else Fraction(0)
    )
    level: list[_Evaluated] = []
    for predicate in predicates:
        for negated in signs:
            evaluated = consider((RuleLiteral(predicate=predicate, negated=negated),))
            if evaluated is not None:
                level.append(evaluated)
    for body in seed_bodies:
        evaluated = consider(body)
        if evaluated is not None:
            level.append(evaluated)
    beam = _select_beam(level, options, base_rate=base_rate)
    for depth in range(2, options.max_body + 1):
        next_level: list[_Evaluated] = []
        for item in beam:
            if len(item.clause.body) >= depth:
                continue
            used = {literal.predicate for literal in item.clause.body}
            for predicate in predicates:
                if predicate in used:
                    continue
                for negated in signs:
                    body = tuple(
                        sorted(
                            (*item.clause.body, RuleLiteral(predicate=predicate, negated=negated)),
                            key=lambda literal: order[literal.predicate],
                        )
                    )
                    evaluated = consider(body)
                    if evaluated is not None:
                        next_level.append(evaluated)
        if not next_level:
            break
        beam = _select_beam(next_level, options, base_rate=base_rate)
    return candidates, best_statistic


def _prune_body(
    body: tuple[RuleLiteral, ...],
    *,
    cohort: _Cohort,
    prune_scope: int,
    positive_mask: int,
    budget: HornBudget,
) -> tuple[tuple[RuleLiteral, ...], int]:
    """Delete literals while the RIPPER prune-window value does not decrease."""

    def value(candidate: tuple[RuleLiteral, ...]) -> Fraction:
        budget.consume(len(candidate) * cohort.word_count)
        coverage = cohort.all_mask
        for literal in candidate:
            present = cohort.present_masks[literal.predicate]
            coverage &= cohort.all_mask ^ present if literal.negated else present
        covered = coverage & prune_scope
        positive = (covered & positive_mask).bit_count()
        negative = covered.bit_count() - positive
        if positive + negative == 0:
            return Fraction(-1)
        return Fraction(positive - negative, positive + negative)

    current = body
    current_value = value(current)
    removed = 0
    while len(current) > 1:
        best_candidate: tuple[RuleLiteral, ...] | None = None
        best_value = current_value
        for index in range(len(current)):
            candidate = current[:index] + current[index + 1 :]
            candidate_value = value(candidate)
            if candidate_value >= best_value and (
                best_candidate is None or candidate_value > best_value
            ):
                best_candidate = candidate
                best_value = candidate_value
        if best_candidate is None:
            break
        current = best_candidate
        current_value = best_value
        removed += 1
    return current, removed


def _scope_metrics(coverage: int, *, scope: int, positive_mask: int) -> Metrics:
    scoped_positive = positive_mask & scope
    scoped_negative = scope ^ scoped_positive
    covered = coverage & scope
    true_positive = (covered & scoped_positive).bit_count()
    false_positive = (covered & scoped_negative).bit_count()
    return Metrics.from_counts(
        true_positive,
        false_positive,
        scoped_negative.bit_count() - false_positive,
        scoped_positive.bit_count() - true_positive,
    )


def _permuted_positive_mask(
    cohort: _Cohort, positive_mask: int, generator: random.Random, blocks: int
) -> int:
    """Shuffle labels within chronological blocks, preserving each block's prevalence."""
    total = len(cohort.examples)
    permuted = 0
    block_count = max(1, min(blocks, total))
    base, remainder = divmod(total, block_count)
    start = 0
    for index in range(block_count):
        size = base + (1 if index < remainder else 0)
        positions = list(range(start, start + size))
        labels = [(positive_mask >> position) & 1 for position in positions]
        generator.shuffle(labels)
        for position, label in zip(positions, labels, strict=True):
            if label:
                permuted |= 1 << position
        start += size
    return permuted


def _validated_seed_bodies(
    seed_bodies: Sequence[tuple[RuleLiteral, ...]],
    *,
    known_predicates: set[str],
    order: dict[str, int],
    options: HornSettings,
) -> tuple[tuple[RuleLiteral, ...], ...]:
    accepted: list[tuple[RuleLiteral, ...]] = []
    seen: set[tuple[tuple[str, bool], ...]] = set()
    for body in seed_bodies:
        literals = tuple(body)
        if not literals or len(literals) > options.max_body:
            continue
        predicates = [literal.predicate for literal in literals]
        if len(set(predicates)) != len(predicates):
            continue
        if any(predicate not in known_predicates for predicate in predicates):
            continue
        if not options.allow_negation and any(literal.negated for literal in literals):
            continue
        canonical = tuple(sorted(literals, key=lambda literal: order[literal.predicate]))
        key = tuple((literal.predicate, literal.negated) for literal in canonical)
        if key in seen:
            continue
        seen.add(key)
        accepted.append(canonical)
    return tuple(accepted)


def learn_horn_diagnostics(
    observations: Sequence[Observation],
    target: str,
    settings: HornSettings | None = None,
    *,
    budget: HornBudget | None = None,
    seed_bodies: Sequence[tuple[RuleLiteral, ...]] = (),
    predicate_order: Sequence[str] | None = None,
) -> HornLearningResult:
    """Learn a disjunction of bounded Horn clauses from positive/negative examples.

    ``seed_bodies`` are extra train-derived conjunctions evaluated alongside the
    search; ``predicate_order`` optionally reorders the structurally eligible
    predicates using a train-only preference. Neither adds predicates that the
    labelled cohort does not contain.
    """
    options = settings or HornSettings()
    active_budget = budget or HornBudget(50_000_000)
    examples = _chronological(
        item
        for item in observations
        if item.labels.get(target, LabelValue.UNKNOWN) is not LabelValue.UNKNOWN
    )
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
    selection = select_train_predicates(
        examples,
        target,
        allow_negation=options.allow_negation,
    )
    ranked = apply_predicate_order(selection.ranked_predicates, predicate_order)
    predicates = list(ranked[: options.max_predicates])
    order = {predicate: index for index, predicate in enumerate(predicates)}
    for predicate in sorted(all_facts):
        order.setdefault(predicate, len(order))
    seeds = _validated_seed_bodies(
        seed_bodies,
        known_predicates=set(ranked),
        order=order,
        options=options,
    )
    total = len(examples)
    all_mask = (1 << total) - 1
    positive_mask = sum(
        1 << index
        for index, item in enumerate(examples)
        if item.labels[target] is LabelValue.POSITIVE
    )
    needed_predicates = {*predicates, *(literal.predicate for body in seeds for literal in body)}
    present_masks = {
        predicate: sum(1 << index for index, item in enumerate(examples) if predicate in item.facts)
        for predicate in sorted(needed_predicates)
    }
    boundary = total // 2
    cohort = _Cohort(
        examples=examples,
        all_mask=all_mask,
        positive_mask=positive_mask,
        present_masks=present_masks,
        word_count=max(1, (total + 63) // 64),
        first_half=(1 << boundary) - 1,
        second_half=all_mask ^ ((1 << boundary) - 1),
    )
    negative_mask = cohort.negative_mask

    prune_count = math.floor(total * options.prune_fraction) if options.prune_fraction else 0
    grow_scope = all_mask
    prune_scope = 0
    pruning_status = "disabled"
    if prune_count > 0:
        grow_count = total - prune_count
        candidate_grow = (1 << grow_count) - 1
        candidate_prune = all_mask ^ candidate_grow
        has_both = all(
            (mask & positive_mask) and (mask & negative_mask)
            for mask in (candidate_grow, candidate_prune)
        )
        if has_both:
            grow_scope = candidate_grow
            prune_scope = candidate_prune
            pruning_status = "applied"
        else:
            prune_count = 0
            pruning_status = "skipped_insufficient_classes"

    state = _SearchState(record=True, diagnostics={})
    selected: list[_Evaluated] = []
    selected_coverage = 0
    uncovered = positive_mask
    observed_statistic: float | None = None
    consumed_before = active_budget.consumed
    search_cost = 0
    literals_removed = 0
    clauses_rejected_after_pruning = 0

    for rule_index in range(options.max_rules):
        candidates, statistic = _search_iteration(
            target=target,
            cohort=cohort,
            predicates=predicates,
            order=order,
            scope=grow_scope,
            positive_mask=positive_mask,
            uncovered=uncovered,
            selected_coverage=selected_coverage,
            options=options,
            budget=active_budget,
            seed_bodies=seeds,
            state=state,
        )
        if rule_index == 0:
            observed_statistic = statistic
            search_cost = active_budget.consumed - consumed_before
        if not candidates:
            break
        ordered = sorted(candidates, key=_selection_key, reverse=True)
        chosen: _Evaluated | None = None
        if prune_count == 0:
            chosen = ordered[0]
        else:
            for candidate in ordered[:_MAX_PRUNE_ATTEMPTS]:
                pruned_body, removed = _prune_body(
                    candidate.clause.body,
                    cohort=cohort,
                    prune_scope=prune_scope,
                    positive_mask=positive_mask,
                    budget=active_budget,
                )
                attempts = [pruned_body]
                if pruned_body != candidate.clause.body:
                    attempts.append(candidate.clause.body)
                for body in attempts:
                    full = _evaluate(
                        body,
                        target=target,
                        cohort=cohort,
                        scope=all_mask,
                        positive_mask=positive_mask,
                        uncovered=uncovered,
                        selected_coverage=selected_coverage,
                        options=options,
                        budget=active_budget,
                    )
                    if not full.reasons:
                        chosen = full
                        if body == pruned_body:
                            literals_removed += removed
                        break
                    if state.record and full.clause.signature not in state.diagnostics:
                        state.diagnostics[full.clause.signature] = ClauseDiagnostic(
                            clause=full.clause,
                            true_positive=full.true_positive,
                            false_positive=full.false_positive,
                            true_negative=full.true_negative,
                            false_negative=full.false_negative,
                            utility=full.utility,
                            lift=full.lift,
                            rejection_reasons=(
                                *full.reasons,
                                "rejected_on_complete_train_window_after_pruning",
                            ),
                        )
                if chosen is not None:
                    break
                clauses_rejected_after_pruning += 1
        if chosen is None:
            break
        selected.append(chosen)
        selected_coverage |= chosen.coverage
        uncovered &= ~chosen.coverage
        if not uncovered:
            break

    clauses_dropped = 0
    if prune_count > 0 and len(selected) > 1:
        retained = list(selected)
        index = len(retained) - 1
        while index >= 0 and len(retained) > 1:
            with_clause = 0
            for item in retained:
                with_clause |= item.coverage
            without_clause = 0
            for position, item in enumerate(retained):
                if position != index:
                    without_clause |= item.coverage
            keep = _scope_metrics(
                with_clause, scope=prune_scope, positive_mask=positive_mask
            ).matthews_correlation
            drop = _scope_metrics(
                without_clause, scope=prune_scope, positive_mask=positive_mask
            ).matthews_correlation
            if drop >= keep:
                del retained[index]
                clauses_dropped += 1
            index -= 1
        selected = retained

    permutation_null: JsonObject | None = None
    if options.permutation_runs > 0 and observed_statistic is not None and positive_mask:
        generator = random.Random(options.seed)
        null_statistics: list[float] = []
        budget_exhausted = False
        null_state = _SearchState(record=False, diagnostics={})
        for _ in range(options.permutation_runs):
            if active_budget.consumed + search_cost > active_budget.limit:
                budget_exhausted = True
                break
            permuted = _permuted_positive_mask(cohort, positive_mask, generator, PERMUTATION_BLOCKS)
            _, null_statistic = _search_iteration(
                target=target,
                cohort=cohort,
                predicates=predicates,
                order=order,
                scope=grow_scope,
                positive_mask=permuted,
                uncovered=permuted,
                selected_coverage=0,
                options=options,
                budget=active_budget,
                seed_bodies=seeds,
                state=null_state,
            )
            null_statistics.append(null_statistic)
        completed = len(null_statistics)
        exceeding = sum(value >= observed_statistic for value in null_statistics)
        ordered_null = sorted(null_statistics)

        def quantile(probability: float) -> float | None:
            if not ordered_null:
                return None
            rank = max(1, math.ceil(probability * len(ordered_null)))
            return ordered_null[rank - 1]

        permutation_null = {
            "statistic": (
                "conservative_lift_lower"
                if options.gate_mode == "relative_lift"
                else f"precision_{options.precision_estimate}"
            ),
            "requested_runs": options.permutation_runs,
            "completed_runs": completed,
            "budget_exhausted": budget_exhausted,
            "blocks": PERMUTATION_BLOCKS,
            "seed": options.seed,
            "observed_best": observed_statistic,
            "null_best_median": quantile(0.5),
            "null_best_p90": quantile(0.9),
            "null_best_p95": quantile(0.95),
            "null_best_maximum": quantile(1.0),
            "runs_at_or_above_observed": exceeding,
            "empirical_p_value": (1 + exceeding) / (completed + 1),
            "interpretation": (
                "empirical calibration of the best first-rule train statistic under "
                "within-block label permutation; not a formal hypothesis test"
            ),
        }

    ranked_near_misses = sorted(
        state.diagnostics.values(),
        key=lambda item: (
            -cast(float, item.lift["conservative_lift_lower"]),
            -item.metrics.precision,
            -item.true_positive,
            item.false_positive,
            len(item.clause.body),
            item.clause.signature,
        ),
    )[: options.near_miss_limit]
    assert state.unique_hypotheses is not None
    search: JsonObject = {
        "strategy": options.search_strategy,
        "beam_width": options.beam_width if options.search_strategy == "beam" else None,
        "beam_ranking": options.beam_ranking if options.search_strategy == "beam" else None,
        "utility_cost_basis": options.utility_cost_basis,
        "predicate_cap": options.max_predicates,
        "eligible_predicates": len(ranked),
        "searched_predicates": cast(JsonValue, list(predicates)),
        "precision_estimate": options.precision_estimate,
        "temporal_consistency_gate": options.require_temporal_consistency,
        "seed_bodies_evaluated": len(seeds),
        "chronological_examples": total,
    }
    pruning: JsonObject = {
        "status": pruning_status,
        "prune_fraction": options.prune_fraction,
        "grow_examples": total - prune_count,
        "prune_examples": prune_count,
        "literals_removed": literals_removed,
        "clauses_dropped": clauses_dropped,
        "candidates_rejected_on_complete_window": clauses_rejected_after_pruning,
    }
    return HornLearningResult(
        rules=RuleSet(target=target, clauses=tuple(item.clause for item in selected)),
        near_misses=tuple(ranked_near_misses),
        hypotheses_examined=state.hypotheses_examined,
        unique_hypotheses_examined=len(state.unique_hypotheses),
        search=search,
        pruning=pruning,
        permutation_null=permutation_null,
    )


def learn_horn(
    observations: Sequence[Observation],
    target: str,
    settings: HornSettings | None = None,
    *,
    budget: HornBudget | None = None,
    seed_bodies: Sequence[tuple[RuleLiteral, ...]] = (),
    predicate_order: Sequence[str] | None = None,
) -> RuleSet:
    """Compatibility wrapper returning only rules from the diagnostic learner."""

    return learn_horn_diagnostics(
        observations,
        target,
        settings,
        budget=budget,
        seed_bodies=seed_bodies,
        predicate_order=predicate_order,
    ).rules
