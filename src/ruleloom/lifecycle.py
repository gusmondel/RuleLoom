"""Learning, readiness, promotion, and policy assessment workflows."""

from __future__ import annotations

import re
import subprocess
import tempfile
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from math import sqrt
from pathlib import Path
from typing import cast

from ruleloom.config import RuleLoomConfig
from ruleloom.evaluation import (
    BooleanLogisticModel,
    best_literal_baseline,
    bootstrap_stability,
    evaluate,
    fit_boolean_logistic_baseline,
    label_is_mature,
    labeled,
    majority_baseline,
    temporal_split,
)
from ruleloom.gitfacts import GitFactsError, repository_identity
from ruleloom.learners.horn import (
    HORN_ENGINE_VERSION,
    HornBudget,
    HornLearningResult,
    HornSettings,
    apply_predicate_order,
    learn_horn,
    learn_horn_diagnostics,
    select_train_predicates,
)
from ruleloom.manual_rules import (
    ManualRuleDeclaration,
    audit_manual_rule,
    manual_candidate_from_audit,
    validate_manual_candidate,
    verify_manual_rule_sources,
)
from ruleloom.models import (
    Candidate,
    HornClause,
    JsonObject,
    JsonValue,
    LabelValue,
    Metrics,
    ModelError,
    Observation,
    Prediction,
    RuleLiteral,
    RuleSet,
    content_hash,
    parse_timestamp,
    validate_prediction_cohort,
)
from ruleloom.packs import (
    EvidencePack,
    matches_pack_version,
    validate_persisted_extraction,
    validate_policy_pack_contract,
)
from ruleloom.signal_probe import SignalProbeReport, run_signal_probe, tree_seed_bodies
from ruleloom.storage import (
    approved_path,
    candidate_path,
    dataset_path,
    deprecated_path,
    load_approved,
    load_candidate,
    load_observations,
    load_reviewed_artifact_untrusted,
    load_shadow,
    load_trusted_predictions,
    record_transition_attestation,
    save_candidate,
    shadow_path,
)

_MAX_HORN_BITSET_WORK_UNITS = 150_000_000
_MAX_RANGE_COMMITS = 10_000
_COMMIT_ID_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")


def _wilson_lower(successes: int, trials: int, *, z: float = 1.959963984540054) -> float:
    """Lower endpoint of a two-sided 95% Wilson score interval."""
    if trials <= 0:
        return 0.0
    proportion = successes / trials
    denominator = 1 + z * z / trials
    centre = proportion + z * z / (2 * trials)
    radius = z * sqrt(proportion * (1 - proportion) / trials + z * z / (4 * trials * trials))
    return max(0.0, (centre - radius) / denominator)


@dataclass(frozen=True, slots=True)
class Readiness:
    observations: int
    labeled: int
    positive: int
    negative: int
    unknown: int
    fact_evidence_coverage: float
    label_evidence_coverage: float
    distinct_predicates: int
    stage: str
    warnings: tuple[str, ...]

    def to_dict(self) -> JsonObject:
        return {
            "observations": self.observations,
            "labeled": self.labeled,
            "positive": self.positive,
            "negative": self.negative,
            "unknown": self.unknown,
            "fact_evidence_coverage": self.fact_evidence_coverage,
            "label_evidence_coverage": self.label_evidence_coverage,
            "distinct_predicates": self.distinct_predicates,
            "stage": self.stage,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    allowed: bool
    unmet: tuple[str, ...]
    blocking: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ShadowEvidence:
    predictions: int
    unique_observations: int
    mature_outcomes: int
    elapsed_days: float
    metrics: Metrics
    rule_metrics: dict[str, Metrics]
    manifest_hash: str

    def to_dict(self) -> JsonObject:
        return {
            "predictions": self.predictions,
            "unique_observations": self.unique_observations,
            "mature_outcomes": self.mature_outcomes,
            "elapsed_days": self.elapsed_days,
            "metrics": self.metrics.to_dict(),
            "rule_metrics": {
                key: self.rule_metrics[key].to_dict() for key in sorted(self.rule_metrics)
            },
            "manifest_hash": self.manifest_hash,
        }


@dataclass(frozen=True, slots=True)
class RuleMatch:
    candidate_id: str
    status: str
    clause: HornClause

    def to_dict(self) -> JsonObject:
        return {
            "candidate_id": self.candidate_id,
            "status": self.status,
            "rule": self.clause.to_dict(),
            "prolog": self.clause.to_prolog(),
        }


def _as_of(value: datetime | None = None) -> datetime:
    instant = value or datetime.now(UTC)
    if instant.tzinfo is None:
        raise ModelError("as_of must include a timezone")
    return instant


def utc_now(value: datetime | None = None) -> str:
    return _as_of(value).isoformat().replace("+00:00", "Z")


def observations_hash(observations: Sequence[Observation]) -> str:
    """Hash a complete, order-independent evidence snapshot."""
    value = [item.to_dict() for item in sorted(observations, key=lambda item: item.id)]
    return content_hash(cast(JsonValue, value))


def _extractors(observations: Sequence[Observation]) -> list[str]:
    names = {
        evidence.extractor
        for observation in observations
        for evidence in observation.fact_evidence.values()
    }
    for observation in observations:
        source_extractor = observation.source.get("extractor")
        if isinstance(source_extractor, str) and source_extractor:
            names.add(source_extractor)
    return sorted(names)


def readiness(
    observations: Sequence[Observation],
    target: str,
    *,
    as_of: datetime | None = None,
) -> Readiness:
    cutoff = _as_of(as_of)
    mature_items = [item for item in observations if label_is_mature(item, target, as_of=cutoff)]
    positives = sum(item.labels[target] is LabelValue.POSITIVE for item in mature_items)
    negatives = sum(item.labels[target] is LabelValue.NEGATIVE for item in mature_items)
    unknown = len(observations) - positives - negatives
    total_facts = sum(len(item.facts) for item in observations)
    evidenced_facts = sum(len(item.fact_evidence) for item in observations)
    mature = positives + negatives
    evidenced_labels = len(mature_items)
    warnings: list[str] = []
    if positives < 20:
        stage = "collection"
        warnings.append("fewer than 20 positive outcomes: learn only exploratory rules")
    elif positives < 50:
        stage = "shadow"
        warnings.append("20-49 positive outcomes: keep rules in shadow mode")
    else:
        stage = "preliminary_evaluation"
    if mature and (positives == 0 or negatives == 0):
        warnings.append("only one mature class is present")
    if unknown:
        warnings.append(f"{unknown} outcomes remain unknown or censored")
    future_labels = sum(
        item.labels.get(target, LabelValue.UNKNOWN) is not LabelValue.UNKNOWN
        and (evidence := item.label_evidence.get(target)) is not None
        and parse_timestamp(evidence.available_at) > cutoff
        for item in observations
    )
    if future_labels:
        warnings.append(
            f"{future_labels} outcome label(s) have future availability and are censored"
        )
    return Readiness(
        observations=len(observations),
        labeled=mature,
        positive=positives,
        negative=negatives,
        unknown=unknown,
        fact_evidence_coverage=evidenced_facts / total_facts if total_facts else 0.0,
        label_evidence_coverage=evidenced_labels / mature if mature else 0.0,
        distinct_predicates=len({fact for item in observations for fact in item.facts}),
        stage=stage,
        warnings=tuple(warnings),
    )


def _horn_settings(config: RuleLoomConfig) -> HornSettings:
    learner = config.learner
    return HornSettings(
        max_body=learner.max_body,
        max_rules=learner.max_rules,
        allow_negation=learner.allow_negation,
        min_precision=learner.min_precision,
        min_support=learner.min_support,
        false_positive_cost=learner.false_positive_cost,
        max_predicates=learner.max_predicates,
        gate_mode=learner.gate_mode,
        min_lift_lower_bound=learner.min_lift_lower_bound,
        min_alert_rate=learner.min_alert_rate,
        confidence_level=learner.confidence_level,
        near_miss_limit=learner.near_miss_limit,
        search_strategy=learner.search_strategy,
        beam_width=learner.beam_width,
        beam_ranking=learner.beam_ranking,
        utility_cost_basis=learner.utility_cost_basis,
        precision_estimate=learner.precision_estimate,
        require_temporal_consistency=learner.require_temporal_consistency,
        prune_fraction=learner.prune_fraction,
        permutation_runs=learner.permutation_runs,
        seed=config.evaluation.seed,
    )


def _availability_filter(
    train: Sequence[Observation], test: Sequence[Observation], target: str
) -> tuple[list[Observation], list[str]]:
    if not test:
        return list(train), []
    test_start = min(parse_timestamp(item.observed_at) for item in test)
    eligible: list[Observation] = []
    excluded: list[str] = []
    for item in train:
        evidence = item.label_evidence.get(target)
        if evidence is not None and parse_timestamp(evidence.available_at) <= test_start:
            eligible.append(item)
        else:
            excluded.append(item.id)
    warnings = (
        [
            f"excluded {len(excluded)} training labels unavailable at holdout start "
            "to prevent temporal leakage"
        ]
        if excluded
        else []
    )
    return eligible, warnings


def _rule_cards(rules: RuleSet, examples: Sequence[Observation], target: str) -> list[JsonObject]:
    cards: list[JsonObject] = []
    for clause in rules.clauses:
        covered = [item for item in examples if clause.matches(item.facts)]
        positive_ids = [
            item.id for item in covered if item.labels.get(target) is LabelValue.POSITIVE
        ]
        counterexample_ids = [
            item.id for item in covered if item.labels.get(target) is LabelValue.NEGATIVE
        ]
        condition = " and ".join(
            ("not " if literal.negated else "") + literal.predicate for literal in clause.body
        )
        cards.append(
            {
                "signature": clause.signature,
                "plain_language": f"Recommend {target} when {condition}.",
                "positive_examples": cast(JsonValue, positive_ids),
                "counterexamples": cast(JsonValue, counterexample_ids),
                "support": len(positive_ids),
                "exceptions": len(counterexample_ids),
                "literal_count": len(clause.body),
            }
        )
    return cards


def _run_horn(
    observations: Sequence[Observation],
    target: str,
    config: RuleLoomConfig,
    *,
    budget: HornBudget,
    seed_bodies: Sequence[tuple[RuleLiteral, ...]] = (),
    predicate_order: Sequence[str] | None = None,
) -> RuleSet:
    """Bootstrap resamples reuse the train-only search controls without the null runs."""
    settings = replace(_horn_settings(config), permutation_runs=0)
    return learn_horn(
        observations,
        target,
        settings,
        budget=budget,
        seed_bodies=seed_bodies,
        predicate_order=predicate_order,
    )


def _train_predicate_order(
    config: RuleLoomConfig,
    logistic_model: BooleanLogisticModel,
) -> tuple[str, ...] | None:
    """Order predicates by the magnitude of their train-only logistic weight."""
    if config.learner.predicate_ranking != "logistic_weight":
        return None
    return tuple(
        predicate
        for predicate, _weight in sorted(
            zip(logistic_model.predicates, logistic_model.weights, strict=True),
            key=lambda pair: (-abs(pair[1]), pair[0]),
        )
    )


def _validate_observation_pack_contract(
    item: Observation,
    config: RuleLoomConfig,
    descriptor: EvidencePack,
    expected_protocol_hash: str,
    *,
    subject: str,
) -> None:
    if item.protocol_hash != expected_protocol_hash:
        raise ModelError(f"{subject} {item.id!r} belongs to a different evidence protocol")
    kind = item.source.get("kind")
    if not isinstance(kind, str) or kind not in {
        "git_commit",
        "git_range",
        "git_worktree",
        "historical_change",
    }:
        raise ModelError(f"{subject} {item.id!r} lacks a supported observation unit")
    repository = item.source.get("repository")
    if not isinstance(repository, str) or not repository:
        raise ModelError(f"{subject} {item.id!r} lacks repository provenance")
    if repository != config.protocol.repository_id:
        raise ModelError(f"{subject} {item.id!r} belongs to a different configured repository")
    if item.source.get("pack") != config.pack:
        raise ModelError(f"{subject} {item.id!r} uses a different fact pack")
    source_pack_version = item.source.get("pack_version")
    if (config.schema_version >= 2 or source_pack_version is not None) and not matches_pack_version(
        source_pack_version, config.pack_version
    ):
        raise ModelError(f"{subject} {item.id!r} uses a different fact pack version")
    if item.source.get("extractor") != descriptor.extractor:
        raise ModelError(f"{subject} {item.id!r} has incompatible extractor provenance")
    source_configuration = item.source.get("pack_config_hash")
    if (
        descriptor.configuration_hash is not None
        and source_configuration != descriptor.configuration_hash
    ):
        raise ModelError(f"{subject} {item.id!r} uses a different pack configuration")
    if descriptor.configuration_hash is None and source_configuration is not None:
        raise ModelError(f"{subject} {item.id!r} has unexpected pack configuration")
    validate_persisted_extraction(
        descriptor,
        item.facts,
        item.fact_evidence,
        subject=f"{subject} {item.id!r}",
        metadata=item.metadata,
    )


def learn_candidate(
    observations: Sequence[Observation],
    config: RuleLoomConfig,
    *,
    as_of: datetime | None = None,
) -> Candidate:
    """Fit on the past, evaluate on the future, and return an immutable candidate."""
    cutoff = _as_of(as_of)
    target = config.target
    expected_protocol_hash = config.evidence_protocol_hash
    mismatched_protocol = [
        item.id for item in observations if item.protocol_hash != expected_protocol_hash
    ]
    if mismatched_protocol:
        raise ModelError(
            "learning data contains observations from a different evidence protocol: "
            + ", ".join(sorted(mismatched_protocol)[:10])
        )
    descriptor = config.resolved_pack
    for item in observations:
        _validate_observation_pack_contract(
            item,
            config,
            descriptor,
            expected_protocol_hash,
            subject="learning observation",
        )
    mature_examples = labeled(observations, target, as_of=cutoff)
    if not mature_examples:
        raise ModelError(
            "no mature labels are available for learning at the requested cutoff; "
            "record independently evidenced outcomes whose available_at is not later "
            "than the cutoff"
        )
    units: set[str] = set()
    repositories: set[str] = set()
    for item in mature_examples:
        kind = item.source.get("kind")
        repository = item.source.get("repository")
        if not isinstance(kind, str) or kind not in {
            "git_commit",
            "git_range",
            "git_worktree",
            "historical_change",
        }:
            raise ModelError(f"learning observation {item.id!r} lacks a supported observation unit")
        if not isinstance(repository, str) or not repository:
            raise ModelError(f"learning observation {item.id!r} lacks repository provenance")
        units.add(kind)
        repositories.add(repository)
    if len(units) > 1:
        raise ModelError("learning data mixes observation units: " + ", ".join(sorted(units)))
    if units not in ({"git_commit"}, {"historical_change"}):
        raise ModelError(
            "the retrospective learner accepts canonical git_commit units only, or grouped "
            "historical_change units; git_range/worktree observations remain prospective "
            "pilot evidence"
        )
    change_ids: list[str] = []
    if units == {"historical_change"}:
        for item in mature_examples:
            change_id = item.source.get("change_id")
            if not isinstance(change_id, str) or not change_id:
                raise ModelError(f"historical observation {item.id!r} lacks a stable change_id")
            change_ids.append(change_id)
        duplicates = sorted(
            change_id for change_id, count in Counter(change_ids).items() if count > 1
        )
        if duplicates:
            raise ModelError(
                "historical learning data contains more than one labeled snapshot for a "
                "logical change; regroup before learning: " + ", ".join(duplicates[:10])
            )
    if repositories != {config.protocol.repository_id}:
        raise ModelError(
            "learning data must come from configured repository "
            f"{config.protocol.repository_id!r}; observed: " + ", ".join(sorted(repositories))
        )
    signal_report: SignalProbeReport | None = None
    if config.signal_probe.enabled:
        signal_report = run_signal_probe(list(observations), config, as_of=cutoff)
        if signal_report.status != "pass":
            raise ModelError(
                f"signal probe {signal_report.id} is {signal_report.status}; the frozen "
                "holdout was not evaluated. Enrich the prediction-time vocabulary or "
                "collect more pre-holdout evidence under a new preregistered experiment."
            )
    split = temporal_split(
        observations,
        target,
        test_fraction=config.evaluation.test_fraction,
        min_train=config.evaluation.min_train_examples,
        min_test=config.evaluation.min_test_examples,
        test_start_at=config.evaluation.test_start_at,
        as_of=cutoff,
    )
    train, availability_warnings = _availability_filter(split.train, split.test, target)
    if len(labeled(train, target, as_of=cutoff)) < 2:
        raise ModelError("at least two temporally eligible mature labels are required")
    predicate_selection = select_train_predicates(
        train,
        target,
        allow_negation=config.learner.allow_negation,
    )
    logistic_model = fit_boolean_logistic_baseline(train, target, as_of=cutoff)
    predicate_order = _train_predicate_order(config, logistic_model)
    search_predicates = apply_predicate_order(
        predicate_selection.ranked_predicates, predicate_order
    )[: config.learner.max_predicates]
    seed_bodies: tuple[tuple[RuleLiteral, ...], ...] = ()
    if (
        config.learner.engine == "horn"
        and config.learner.tree_seeds
        and predicate_selection.positive_observations
        and predicate_selection.negative_observations
    ):
        seed_bodies = tree_seed_bodies(
            train,
            target,
            max_depth=config.signal_probe.tree_max_depth,
            max_predicates=config.signal_probe.max_predicates,
        )

    if config.learner.engine == "horn":
        predicate_count = min(
            config.learner.max_predicates,
            len(predicate_selection.ranked_predicates),
        )
        # Permutation-null runs consume leftover budget and stop early on their own,
        # so the fail-closed estimate covers only the learning and bootstrap searches.
        estimated_checks = (
            config.learner.hypothesis_count(predicate_count)
            * max(1, (len(train) + 63) // 64)
            * config.learner.max_body
            * (config.learner.bootstrap_runs + 1)
            * config.learner.max_rules
        )
        if estimated_checks > _MAX_HORN_BITSET_WORK_UNITS:
            raise ModelError(
                "estimated Horn search work exceeds the safe budget; reduce observations, "
                "max_body, max_predicates, beam_width, or bootstrap_runs"
            )

    if config.learner.engine == "horn":
        horn_budget = HornBudget(_MAX_HORN_BITSET_WORK_UNITS)
        learned_horn = learn_horn_diagnostics(
            train,
            target,
            _horn_settings(config),
            budget=horn_budget,
            seed_bodies=seed_bodies,
            predicate_order=predicate_order,
        )
        horn_result: HornLearningResult | None = learned_horn
        rules = learned_horn.rules
        engine_version = HORN_ENGINE_VERSION
    else:
        horn_result = None
        from ruleloom.learners.popper import learn_popper

        with tempfile.TemporaryDirectory(prefix="ruleloom-popper-") as temporary:
            run = learn_popper(
                train,
                target,
                Path(temporary),
                popper_dir=config.learner.popper_dir,
                max_body=config.learner.max_body,
                max_rules=config.learner.max_rules,
                max_predicates=config.learner.max_predicates,
                allow_negation=config.learner.allow_negation,
                timeout_seconds=config.learner.popper_timeout_seconds,
            )
        rules = run.rules
        engine_version = run.engine_version

    train_metrics = evaluate(train, target, rules.predicts, as_of=cutoff)
    test_metrics = evaluate(split.test, target, rules.predicts, as_of=cutoff)
    majority_value, _ = majority_baseline(train, target, as_of=cutoff)
    _, literal_metrics = best_literal_baseline(train, split.test, target, as_of=cutoff)

    def size_only_predictor(facts: frozenset[str]) -> bool:
        return bool({"large_change", "multi_file_change"}.intersection(facts))

    baselines = {
        "never_alert": evaluate(split.test, target, lambda _facts: False, as_of=cutoff),
        "always_alert": evaluate(split.test, target, lambda _facts: True, as_of=cutoff),
        "train_majority": evaluate(split.test, target, lambda _facts: majority_value, as_of=cutoff),
        "best_single_literal": literal_metrics,
        "size_only": evaluate(
            split.test,
            target,
            size_only_predictor,
            as_of=cutoff,
        ),
        "logistic_regression_boolean_facts": evaluate(
            split.test,
            target,
            logistic_model.predicts,
            as_of=cutoff,
        ),
    }

    def bootstrap_learner(sample: Sequence[Observation], sample_target: str) -> RuleSet:
        if config.learner.engine != "horn":
            return rules
        return _run_horn(
            sample,
            sample_target,
            config,
            budget=horn_budget,
            seed_bodies=seed_bodies,
            predicate_order=predicate_order,
        )

    warnings = [*split.warnings, *availability_warnings]
    if predicate_selection.constant_predicates:
        warnings.append(
            "predicate preprocessing excluded "
            f"{len(predicate_selection.constant_predicates)} training-constant predicate(s)"
        )
    if predicate_selection.duplicate_predicates:
        warnings.append(
            "predicate preprocessing collapsed "
            f"{len(predicate_selection.duplicate_predicates)} duplicate training-column "
            "alias(es); representatives were chosen lexically without consulting holdout"
        )
    if config.learner.engine == "horn":
        stability = bootstrap_stability(
            train,
            target,
            rules,
            bootstrap_learner,
            runs=config.learner.bootstrap_runs,
            seed=config.evaluation.seed,
        )
    else:
        stability = 0.0
        warnings.append(
            "Popper bootstrap stability was not computed; promotion remains gated until "
            "stability is measured explicitly"
        )
    if not rules.clauses:
        warnings.append("the learner abstained: no rule met support and precision constraints")
    status = readiness(observations, target, as_of=cutoff)
    warnings.extend(status.warnings)
    dataset_hash = observations_hash(observations)
    best_literal_name, _ = best_literal_baseline(train, split.test, target, as_of=cutoff)
    historical_unit = next(iter(units))
    duplicate_groups: list[JsonValue] = [
        {
            "representative": group[0],
            "aliases": list(group[1:]),
        }
        for group in predicate_selection.duplicate_groups
    ]
    metadata: JsonObject = {
        "pack": config.pack,
        "pack_version": config.pack_version,
        "repository_id": config.protocol.repository_id,
        "evidence_protocol_hash": expected_protocol_hash,
        "historical_observation_unit": historical_unit,
        "extractors": cast(JsonValue, _extractors(observations)),
        "readiness": status.to_dict(),
        "best_single_literal": best_literal_name,
        "baseline_models": {
            "size_only": {
                "definition": "large_change OR multi_file_change",
                "training_selected": False,
            },
            "logistic_regression_boolean_facts": logistic_model.to_dict(),
        },
        "rule_cards": cast(JsonValue, _rule_cards(rules, train, target)),
        "rule_evaluation": cast(
            JsonValue,
            [
                {
                    "signature": clause.signature,
                    "train": evaluate(train, target, clause.matches, as_of=cutoff).to_dict(),
                    "test": evaluate(split.test, target, clause.matches, as_of=cutoff).to_dict(),
                }
                for clause in rules.clauses
            ],
        ),
        "evaluation": {
            "method": "temporal_holdout",
            "test_start": split.test[0].observed_at if split.test else None,
            "configured_test_start_at": config.evaluation.test_start_at,
            "label_availability_enforced": True,
        },
        "predicate_selection": {
            "scope": "temporally_eligible_train",
            "holdout_consulted": False,
            "labelled_observations": predicate_selection.labelled_observations,
            "positive_observations": predicate_selection.positive_observations,
            "negative_observations": predicate_selection.negative_observations,
            "observed_predicate_count": len(predicate_selection.observed_predicates),
            "eligible_representative_count": len(predicate_selection.ranked_predicates),
            "search_strategy": config.learner.search_strategy,
            "predicate_ranking": config.learner.predicate_ranking,
            "search_predicates": list(search_predicates),
            "constant_predicates": list(predicate_selection.constant_predicates),
            "duplicate_groups": duplicate_groups,
            "tree_seed_bodies": [[literal.to_dict() for literal in body] for body in seed_bodies],
        },
    }
    if signal_report is not None:
        metadata["signal_probe"] = signal_report.to_dict()
    if horn_result is not None:
        metadata["horn_diagnostics"] = horn_result.diagnostics_dict()
    if historical_unit == "historical_change":
        evidence_qualities = sorted(
            {
                value
                for item in mature_examples
                if isinstance((value := item.source.get("evidence_quality")), str)
            }
        )
        confirmatory_history = all(
            item.source.get("confirmatory") is True
            and item.source.get("evidence_quality") == "rich"
            for item in mature_examples
        )
        metadata["historical_evidence_qualities"] = cast(JsonValue, evidence_qualities)
        metadata["confirmatory_history"] = confirmatory_history
        metadata["independent_change_units"] = len(change_ids)
        if not confirmatory_history:
            warnings.append(
                "historical labels include exploratory or final-state evidence; this "
                "candidate may enter shadow review but cannot be approved"
            )
    if descriptor.configuration_hash is not None:
        metadata["pack_config_hash"] = descriptor.configuration_hash
    candidate = Candidate(
        id="cand-pending",
        created_at=utc_now(cutoff),
        engine=config.learner.engine,
        engine_version=engine_version,
        dataset_hash=dataset_hash,
        config_hash=config.hash,
        rules=rules,
        metrics={"train": train_metrics, "test": test_metrics},
        baselines=baselines,
        stability=stability,
        train_ids=tuple(item.id for item in train),
        test_ids=tuple(item.id for item in split.test),
        warnings=tuple(dict.fromkeys(warnings)),
        metadata=metadata,
    )
    return candidate.with_identity()


def save_learned_candidate(root: Path, config: RuleLoomConfig, candidate: Candidate) -> Path:
    path = candidate_path(root, config, candidate.id)
    if path.exists():
        persisted = load_candidate(path)
        if replace(candidate, created_at=persisted.created_at) == persisted:
            return path
    save_candidate(path, candidate)
    return path


def validate_non_overlapping_git_ranges(root: Path, predictions: Sequence[Prediction]) -> None:
    """Reject prospective Git ranges that count any commit more than once."""
    if not predictions or predictions[0].protocol.get("observation_unit") != "git_range":
        return
    seen: dict[str, str] = {}
    for prediction in predictions:
        base = prediction.observation.source.get("base")
        head = prediction.observation.source.get("head")
        if (
            not isinstance(base, str)
            or not isinstance(head, str)
            or not _COMMIT_ID_RE.fullmatch(base)
            or not _COMMIT_ID_RE.fullmatch(head)
        ):
            raise ModelError(f"git_range prediction {prediction.id} has invalid commit endpoints")
        try:
            completed = subprocess.run(
                [
                    "git",
                    "-C",
                    str(root.resolve()),
                    "rev-list",
                    f"--max-count={_MAX_RANGE_COMMITS + 1}",
                    head,
                    f"^{base}",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ModelError(f"cannot validate prospective Git range overlap: {exc}") from exc
        commits = [item for item in completed.stdout.splitlines() if item]
        if completed.returncode != 0:
            detail = completed.stderr.strip() or "git rev-list failed"
            raise ModelError(f"cannot validate prospective Git range {prediction.id}: {detail}")
        if len(commits) > _MAX_RANGE_COMMITS:
            raise ModelError(
                f"prospective Git range {prediction.id} exceeds {_MAX_RANGE_COMMITS} commits"
            )
        for commit in commits:
            prior = seen.get(commit)
            if prior is not None and prior != prediction.unit_id:
                raise ModelError(
                    "prospective Git ranges overlap at commit "
                    f"{commit}: units {prior!r} and {prediction.unit_id!r}"
                )
            seen[commit] = prediction.unit_id


def prospective_unit_outcome(
    observations: Sequence[Observation],
    prediction: Prediction,
    target: str,
    *,
    as_of: datetime,
) -> tuple[LabelValue, datetime | None]:
    """Resolve one consistent later outcome across every snapshot of a change unit."""
    current = next(
        (item for item in observations if item.id == prediction.observation.id),
        None,
    )
    if current is None:
        raise ModelError(
            f"prediction unit {prediction.unit_id!r} references missing observation "
            f"{prediction.observation.id!r}"
        )
    if replace(current, labels={}, label_evidence={}) != replace(
        prediction.observation, labels={}, label_evidence={}
    ):
        raise ModelError(
            f"current observation {current.id!r} does not match its prediction snapshot"
        )

    outcomes: list[tuple[LabelValue, datetime]] = []
    for item in observations:
        if item.source.get("change_id") != prediction.unit_id:
            continue
        if item.protocol_hash != prediction.observation.protocol_hash:
            raise ModelError(f"prediction unit {prediction.unit_id!r} mixes evidence protocols")
        if item.source.get("repository") != prediction.observation.source.get("repository"):
            raise ModelError(f"prediction unit {prediction.unit_id!r} mixes repositories")
        if item.source.get("kind") != prediction.protocol.get("observation_unit"):
            raise ModelError(f"prediction unit {prediction.unit_id!r} mixes observation units")
        if item.source.get("pack") != prediction.protocol.get("pack"):
            raise ModelError(f"prediction unit {prediction.unit_id!r} mixes fact packs")
        if item.source.get("extractor") != prediction.protocol.get("extractor"):
            raise ModelError(f"prediction unit {prediction.unit_id!r} mixes extractors")
        expected_pack_version = prediction.observation.source.get("pack_version")
        actual_pack_version = item.source.get("pack_version")
        if expected_pack_version is None:
            if actual_pack_version is not None:
                raise ModelError(f"prediction unit {prediction.unit_id!r} mixes fact pack versions")
        elif (
            isinstance(expected_pack_version, bool)
            or not isinstance(expected_pack_version, int)
            or not matches_pack_version(actual_pack_version, expected_pack_version)
        ):
            raise ModelError(f"prediction unit {prediction.unit_id!r} mixes fact pack versions")
        if item.source.get("pack_config_hash") != prediction.observation.source.get(
            "pack_config_hash"
        ):
            raise ModelError(
                f"prediction unit {prediction.unit_id!r} mixes fact pack configurations"
            )
        if target not in item.labels:
            raise ModelError(
                f"prediction unit {prediction.unit_id!r} contains an observation without target "
                f"{target!r}"
            )
        label = item.labels.get(target, LabelValue.UNKNOWN)
        evidence = item.label_evidence.get(target)
        if label is LabelValue.UNKNOWN or evidence is None:
            continue
        available = parse_timestamp(evidence.available_at)
        if available <= as_of:
            outcomes.append((label, available))
    values = {label for label, _ in outcomes}
    if len(values) > 1:
        raise ModelError(f"prediction unit {prediction.unit_id!r} has conflicting outcomes")
    if not outcomes:
        return LabelValue.UNKNOWN, None
    return outcomes[0][0], min(available for _, available in outcomes)


def shadow_evidence(
    root: Path,
    config: RuleLoomConfig,
    candidate: Candidate,
    *,
    as_of: datetime | None = None,
) -> ShadowEvidence:
    """Evaluate one exact shadow manifest against later, mature outcomes."""
    cutoff = _as_of(as_of)
    if candidate.status != "shadow":
        raise ModelError("prospective evidence requires an exact shadow candidate manifest")
    raw_reviewed_at = candidate.review.get("reviewed_at")
    if not isinstance(raw_reviewed_at, str):
        raise ModelError("shadow candidate is missing its reviewed_at transition timestamp")
    shadow_started_at = max(parse_timestamp(candidate.created_at), parse_timestamp(raw_reviewed_at))
    manifest_hash = content_hash(candidate.to_dict())
    expected_config_hash = config.hash
    expected_protocol_hash = config.evidence_protocol_hash
    predictions = [
        prediction
        for prediction in load_trusted_predictions(root, config)
        if prediction.target == config.target
        and parse_timestamp(prediction.predicted_at) >= shadow_started_at
        and parse_timestamp(prediction.predicted_at) <= cutoff
        and any(
            policy.get("candidate_id") == candidate.id
            and policy.get("status") == "shadow"
            and policy.get("manifest_hash") == manifest_hash
            for policy in prediction.policies
        )
    ]
    for prediction in predictions:
        if (
            prediction.protocol.get("experiment_id") != config.protocol.experiment_id
            or prediction.protocol.get("repository_id") != config.protocol.repository_id
            or prediction.protocol.get("observation_unit") != config.protocol.prediction_unit
            or prediction.protocol.get("outcome_definition") != config.protocol.outcome_definition
            or prediction.protocol.get("target") != config.target
            or prediction.protocol.get("pack") != config.pack
            or prediction.protocol.get("config_hash") != expected_config_hash
            or prediction.protocol.get("evidence_protocol_hash") != expected_protocol_hash
        ):
            raise ModelError(
                f"shadow prediction {prediction.id} does not match the configured protocol"
            )
    validate_prediction_cohort(
        predictions,
        expected_observation_unit=config.protocol.prediction_unit,
        expected_repository_id=config.protocol.repository_id,
    )
    validate_non_overlapping_git_ranges(root, predictions)
    earliest: dict[str, Prediction] = {}
    for prediction in sorted(
        predictions, key=lambda item: (parse_timestamp(item.predicted_at), item.id)
    ):
        earliest.setdefault(prediction.unit_id, prediction)

    observations = load_observations(dataset_path(root, config))
    descriptor = config.resolved_pack
    for item in observations:
        _validate_observation_pack_contract(
            item,
            config,
            descriptor,
            expected_protocol_hash,
            subject="shadow outcome observation",
        )
    tp = fp = tn = fn = mature = 0
    rule_counts = {signature: [0, 0, 0, 0] for signature in candidate.rules.signatures}
    for prediction in earliest.values():
        label, available = prospective_unit_outcome(
            observations,
            prediction,
            config.target,
            as_of=cutoff,
        )
        if (
            label is LabelValue.UNKNOWN
            or available is None
            or available <= parse_timestamp(prediction.predicted_at)
        ):
            continue
        mature += 1
        actual = label is LabelValue.POSITIVE
        matched_signatures = {
            HornClause.from_dict(cast(JsonObject, match["rule"])).signature
            for match in prediction.matches
            if match.get("candidate_id") == candidate.id and match.get("status") == "shadow"
        }
        predicted = any(
            match.get("candidate_id") == candidate.id and match.get("status") == "shadow"
            for match in prediction.matches
        )
        if actual and predicted:
            tp += 1
        elif not actual and predicted:
            fp += 1
        elif not actual and not predicted:
            tn += 1
        else:
            fn += 1
        for signature, counts in rule_counts.items():
            clause_predicted = signature in matched_signatures
            if actual and clause_predicted:
                counts[0] += 1
            elif not actual and clause_predicted:
                counts[1] += 1
            elif not actual and not clause_predicted:
                counts[2] += 1
            else:
                counts[3] += 1

    instants = [parse_timestamp(item.predicted_at) for item in earliest.values()]
    elapsed_days = (
        (max(instants) - min(instants)).total_seconds() / 86_400 if len(instants) >= 2 else 0.0
    )
    return ShadowEvidence(
        predictions=len(predictions),
        unique_observations=len(earliest),
        mature_outcomes=mature,
        elapsed_days=elapsed_days,
        metrics=Metrics.from_counts(tp, fp, tn, fn),
        rule_metrics={
            signature: Metrics.from_counts(*counts)
            for signature, counts in sorted(rule_counts.items())
        },
        manifest_hash=manifest_hash,
    )


def promotion_decision(
    candidate: Candidate,
    config: RuleLoomConfig,
    destination: str,
    *,
    shadow_recorded: bool = True,
    positive_count: int | None = None,
    prospective_shadow: ShadowEvidence | None = None,
) -> PromotionDecision:
    if destination not in {"shadow", "approved"}:
        raise ModelError("promotion destination must be shadow or approved")
    manual = candidate.engine == "manual"
    if manual:
        validate_manual_candidate(candidate, config)
    claims_manual_provenance = (
        candidate.metadata.get("candidate_origin") == "manual_declaration"
        or "manual_declaration" in candidate.metadata
        or "manual_audit" in candidate.metadata
    )
    unmet: list[str] = []
    blocking: list[str] = []

    def block(message: str) -> None:
        unmet.append(message)
        blocking.append(message)

    if candidate.status != "candidate":
        block(f"source status {candidate.status!r} is not 'candidate'")
    if candidate.rules.target != config.target:
        block(f"candidate target {candidate.rules.target!r} != configured target {config.target!r}")
    if manual and candidate.metadata.get("candidate_origin") != "manual_declaration":
        block("manual candidate lacks declared-rule provenance")
    if not manual and claims_manual_provenance:
        block("declared-rule provenance is incompatible with a learned candidate")
    if not manual and candidate.engine != config.learner.engine:
        block(
            f"candidate engine {candidate.engine!r} != configured engine {config.learner.engine!r}"
        )
    if candidate.config_hash != config.hash:
        block("candidate config hash does not match the current configuration")
    if candidate.metadata.get("pack") != config.pack:
        block(f"candidate fact pack does not match {config.pack!r}")
    if candidate.metadata.get("repository_id") != config.protocol.repository_id:
        block("candidate repository does not match the configured repository")
    if (
        destination == "approved"
        and candidate.metadata.get("historical_observation_unit") == "historical_change"
        and candidate.metadata.get("confirmatory_history") is not True
    ):
        block(
            "candidate was learned from non-confirmatory historical evidence; approval "
            "requires rich point-in-time change units"
        )
    readiness_value = candidate.metadata.get("readiness")
    positive = positive_count if positive_count is not None else 0
    if positive_count is None and isinstance(readiness_value, dict):
        raw_positive = readiness_value.get("positive", 0)
        if isinstance(raw_positive, int) and not isinstance(raw_positive, bool):
            positive = raw_positive
    required_positive = (
        config.promotion.min_positive_for_shadow
        if destination == "shadow"
        else config.promotion.min_positive_for_approval
    )
    if not manual and positive < required_positive:
        unmet.append(f"positive outcomes {positive} < required {required_positive}")
    if not candidate.rules.clauses:
        block(
            "candidate contains no declared rules"
            if manual
            else "candidate contains no learned rules"
        )
    if not manual and "train" not in candidate.metrics:
        block("train metrics are missing")
    if destination == "approved":
        if not manual and len(candidate.train_ids) < config.evaluation.min_train_examples:
            block(
                f"temporally eligible training examples {len(candidate.train_ids)} < required "
                f"{config.evaluation.min_train_examples}"
            )
        if not manual and len(candidate.test_ids) < config.evaluation.min_test_examples:
            block(
                f"temporal test examples {len(candidate.test_ids)} < required "
                f"{config.evaluation.min_test_examples}"
            )
        if not shadow_recorded:
            block("candidate has not completed a recorded shadow transition")
        if prospective_shadow is None:
            block("candidate has no attributable prospective shadow evidence")
        else:
            promotion = config.promotion
            if (
                prospective_shadow.unique_observations
                < promotion.min_shadow_predictions_for_approval
            ):
                block(
                    f"shadow predictions {prospective_shadow.unique_observations} < required "
                    f"{promotion.min_shadow_predictions_for_approval}"
                )
            if (
                prospective_shadow.mature_outcomes
                < promotion.min_shadow_mature_outcomes_for_approval
            ):
                block(
                    f"mature shadow outcomes {prospective_shadow.mature_outcomes} < required "
                    f"{promotion.min_shadow_mature_outcomes_for_approval}"
                )
            if prospective_shadow.elapsed_days < promotion.min_shadow_days_for_approval:
                block(
                    f"shadow window {prospective_shadow.elapsed_days:.2f} days < required "
                    f"{promotion.min_shadow_days_for_approval}"
                )
            shadow_precision_lower = _wilson_lower(
                prospective_shadow.metrics.true_positive,
                prospective_shadow.metrics.true_positive
                + prospective_shadow.metrics.false_positive,
            )
            shadow_recall_lower = _wilson_lower(
                prospective_shadow.metrics.true_positive,
                prospective_shadow.metrics.true_positive
                + prospective_shadow.metrics.false_negative,
            )
            if shadow_precision_lower < promotion.min_shadow_precision:
                block(
                    f"shadow precision 95% Wilson lower bound {shadow_precision_lower:.3f} < "
                    f"{promotion.min_shadow_precision:.3f}"
                )
            if shadow_recall_lower < promotion.min_shadow_recall:
                block(
                    f"shadow recall 95% Wilson lower bound {shadow_recall_lower:.3f} < "
                    f"{promotion.min_shadow_recall:.3f}"
                )
            if prospective_shadow.metrics.matthews_correlation < promotion.min_shadow_mcc:
                block(
                    f"shadow MCC {prospective_shadow.metrics.matthews_correlation:.3f} < "
                    f"{promotion.min_shadow_mcc:.3f}"
                )
            positive_outcomes = (
                prospective_shadow.metrics.true_positive + prospective_shadow.metrics.false_negative
            )
            negative_outcomes = (
                prospective_shadow.metrics.true_negative + prospective_shadow.metrics.false_positive
            )
            if positive_outcomes < promotion.min_shadow_positive_outcomes_for_approval:
                block(
                    f"positive shadow outcomes {positive_outcomes} < required "
                    f"{promotion.min_shadow_positive_outcomes_for_approval}"
                )
            if negative_outcomes < promotion.min_shadow_negative_outcomes_for_approval:
                block(
                    f"negative shadow outcomes {negative_outcomes} < required "
                    f"{promotion.min_shadow_negative_outcomes_for_approval}"
                )
            expected_rule_signatures = set(candidate.rules.signatures)
            reported_rule_signatures = set(prospective_shadow.rule_metrics)
            if reported_rule_signatures != expected_rule_signatures:
                missing = sorted(expected_rule_signatures - reported_rule_signatures)
                unexpected = sorted(reported_rule_signatures - expected_rule_signatures)
                details: list[str] = []
                if missing:
                    details.append("missing " + ", ".join(missing))
                if unexpected:
                    details.append("unexpected " + ", ".join(unexpected))
                block(
                    "shadow per-rule evidence does not match the candidate: " + "; ".join(details)
                )
            for signature in sorted(expected_rule_signatures & reported_rule_signatures):
                metrics = prospective_shadow.rule_metrics[signature]
                predicted_positive = metrics.true_positive + metrics.false_positive
                if predicted_positive < promotion.min_shadow_matches_per_rule_for_approval:
                    block(
                        f"shadow rule {signature} prospective matches {predicted_positive} < "
                        f"required {promotion.min_shadow_matches_per_rule_for_approval}"
                    )
                else:
                    rule_precision_lower = _wilson_lower(
                        metrics.true_positive,
                        predicted_positive,
                    )
                    if rule_precision_lower < promotion.min_shadow_precision:
                        block(
                            f"shadow rule {signature} precision 95% Wilson lower bound "
                            f"{rule_precision_lower:.3f} < {promotion.min_shadow_precision:.3f}"
                        )
        if manual:
            return PromotionDecision(not unmet, tuple(unmet), tuple(blocking))
        test = candidate.metrics.get("test")
        if config.promotion.require_test_set and not candidate.test_ids:
            block("a temporal test set is required")
        if test is None:
            block("test metrics are missing")
        else:
            if test.precision < config.promotion.min_test_precision:
                unmet.append(
                    f"test precision {test.precision:.3f} < "
                    f"{config.promotion.min_test_precision:.3f}"
                )
            if test.recall < config.promotion.min_test_recall:
                unmet.append(
                    f"test recall {test.recall:.3f} < {config.promotion.min_test_recall:.3f}"
                )
            if config.promotion.require_baseline_improvement:
                required_baselines = {
                    "never_alert",
                    "always_alert",
                    "train_majority",
                    "best_single_literal",
                }
                missing_baselines = required_baselines.difference(candidate.baselines)
                if missing_baselines:
                    block("baseline metrics are missing: " + ", ".join(sorted(missing_baselines)))
                else:
                    best_baseline = max(
                        metric.matthews_correlation for metric in candidate.baselines.values()
                    )
                    if test.matthews_correlation <= best_baseline:
                        unmet.append(
                            f"test MCC {test.matthews_correlation:.3f} does not exceed best "
                            f"baseline {best_baseline:.3f}"
                        )
        raw_rule_evaluation = candidate.metadata.get("rule_evaluation")
        evaluations: dict[str, Metrics] = {}
        if isinstance(raw_rule_evaluation, list):
            for raw_item in raw_rule_evaluation:
                if not isinstance(raw_item, dict):
                    continue
                raw_signature = raw_item.get("signature")
                raw_test = raw_item.get("test")
                if isinstance(raw_signature, str) and isinstance(raw_test, dict):
                    try:
                        evaluations[raw_signature] = Metrics.from_dict(raw_test)
                    except ModelError:
                        continue
        for clause in candidate.rules.clauses:
            clause_metrics = evaluations.get(clause.signature)
            if clause_metrics is None:
                block(f"test metrics are missing for rule {clause.signature}")
                continue
            predicted_positive = clause_metrics.true_positive + clause_metrics.false_positive
            if predicted_positive == 0:
                block(f"rule {clause.signature} has no temporal holdout matches")
            elif clause_metrics.precision < config.promotion.min_test_precision:
                block(
                    f"rule {clause.signature} test precision {clause_metrics.precision:.3f} < "
                    f"{config.promotion.min_test_precision:.3f}"
                )
        if candidate.stability < config.promotion.min_stability:
            unmet.append(
                f"stability {candidate.stability:.3f} < {config.promotion.min_stability:.3f}"
            )
    return PromotionDecision(not unmet, tuple(unmet), tuple(blocking))


def promote_candidate(
    root: Path,
    config: RuleLoomConfig,
    candidate_id: str,
    *,
    destination: str,
    reviewer: str,
    note: str,
    override: bool = False,
) -> tuple[Candidate, PromotionDecision, Path]:
    cutoff = _as_of()
    if not reviewer.strip():
        raise ModelError("reviewer cannot be empty")
    source = load_candidate(candidate_path(root, config, candidate_id))
    if source.id != candidate_id:
        raise ModelError(
            f"candidate filename/request id {candidate_id!r} does not match manifest "
            f"id {source.id!r}"
        )
    shadow_file = shadow_path(root, config, candidate_id)
    shadow_recorded = shadow_file.exists()
    observations = load_observations(dataset_path(root, config))
    declaration: ManualRuleDeclaration | None = None
    if source.engine == "manual":
        declaration, _audit = validate_manual_candidate(source, config)
        drifted = [
            item
            for item in verify_manual_rule_sources(root, declaration)
            if item.status != "unchanged"
        ]
        if drifted:
            raise ModelError(
                "manual rule source changed or became unavailable; declare a new revision "
                "instead of promoting stale policy text"
            )
    if destination == "shadow" or not shadow_recorded:
        current_hash = observations_hash(observations)
        if source.dataset_hash != current_hash:
            raise ModelError(
                "candidate dataset snapshot no longer matches current evidence; recreate the "
                "candidate before the first reviewed transition"
            )
        if declaration is None:
            reproduced = learn_candidate(observations, config, as_of=cutoff)
        else:
            audit = audit_manual_rule(
                root,
                config,
                declaration,
                observations,
                as_of=parse_timestamp(source.created_at),
            )
            reproduced = manual_candidate_from_audit(declaration, audit, config)
        if source.identity_payload() != reproduced.identity_payload():
            raise ModelError(
                "candidate manifest cannot be reproduced from the current evidence and config"
            )
    shadow_report: ShadowEvidence | None = None
    if destination == "approved" and shadow_recorded:
        shadow = next(
            (item for item in load_shadow(root, config) if item.id == candidate_id),
            None,
        )
        if shadow is None:
            raise ModelError(f"recorded shadow policy is not active: {candidate_id}")
        if replace(shadow, status="candidate", review={}) != source:
            raise ModelError("recorded shadow artifact does not match the candidate manifest")
        shadow_report = shadow_evidence(root, config, shadow, as_of=cutoff)
    decision = promotion_decision(
        source,
        config,
        destination,
        shadow_recorded=shadow_recorded,
        positive_count=(
            readiness(observations, config.target, as_of=cutoff).positive
            if destination == "shadow"
            else None
        ),
        prospective_shadow=shadow_report,
    )
    if decision.blocking:
        raise ModelError("non-overridable promotion gates failed: " + "; ".join(decision.blocking))
    if not decision.allowed and not override:
        raise ModelError("promotion gates failed: " + "; ".join(decision.unmet))
    if override and not note.strip():
        raise ModelError("an override requires a non-empty note")
    review: JsonObject = {
        "reviewer": reviewer,
        "reviewed_at": utc_now(cutoff),
        "note": note,
        "override": override,
        "unmet_gates": list(decision.unmet),
    }
    if shadow_report is not None:
        review["shadow_evidence"] = shadow_report.to_dict()
    promoted = replace(source, status=destination, review=review)
    destination_path = (
        shadow_path(root, config, candidate_id)
        if destination == "shadow"
        else approved_path(root, config, candidate_id)
    )
    save_candidate(destination_path, promoted)
    record_transition_attestation(root, promoted)
    return promoted, decision, destination_path


def deprecate_candidate(
    root: Path,
    config: RuleLoomConfig,
    candidate_id: str,
    *,
    reviewer: str,
    note: str,
) -> tuple[Candidate, Path]:
    """Write an immutable tombstone while preserving the reviewed artifact."""
    if not reviewer.strip() or not note.strip():
        raise ModelError("deprecation requires a non-empty reviewer and note")
    tombstone = deprecated_path(root, config, candidate_id)
    if tombstone.exists():
        raise ModelError(f"candidate is already deprecated: {candidate_id}")
    approved = {item.id: item for item in load_approved(root, config)}
    shadow = {item.id: item for item in load_shadow(root, config)}
    source = approved.get(candidate_id) or shadow.get(candidate_id)
    if source is None:
        raise ModelError(f"no active reviewed candidate found: {candidate_id}")
    review = dict(source.review)
    review["deprecation"] = {
        "reviewer": reviewer.strip(),
        "reviewed_at": utc_now(),
        "note": note.strip(),
    }
    deprecated = replace(source, status="deprecated", review=review)
    save_candidate(tombstone, deprecated)
    deprecation_review = cast(JsonObject, review["deprecation"])
    record_transition_attestation(
        root,
        deprecated,
        reviewer=reviewer.strip(),
        note=note.strip(),
        trusted_at=cast(str, deprecation_review["reviewed_at"]),
    )
    return deprecated, tombstone


def trust_reviewed_artifact(
    root: Path,
    config: RuleLoomConfig,
    candidate_id: str,
    *,
    status: str,
    reviewer: str,
    note: str,
) -> tuple[Candidate, Path]:
    """Explicitly trust a versioned reviewed artifact in this clone/worktree."""
    if not reviewer.strip() or not note.strip():
        raise ModelError("local trust requires a non-empty reviewer and note")
    try:
        actual_repository = repository_identity(root)
    except GitFactsError as exc:
        raise ModelError(f"cannot verify configured Git repository: {exc}") from exc
    if actual_repository != config.protocol.repository_id:
        raise ModelError("reviewed artifact belongs to a different Git repository")
    candidate = load_reviewed_artifact_untrusted(root, config, candidate_id, status)
    path = record_transition_attestation(
        root,
        candidate,
        reviewer=reviewer.strip(),
        note=note.strip(),
        trusted_at=utc_now(),
    )
    return candidate, path


def match_rules(facts: frozenset[str], candidates: Sequence[Candidate]) -> list[RuleMatch]:
    matches: list[RuleMatch] = []
    for candidate in candidates:
        for clause in candidate.rules.clauses:
            if clause.matches(facts):
                matches.append(
                    RuleMatch(
                        candidate_id=candidate.id,
                        status=candidate.status,
                        clause=clause,
                    )
                )
    return matches


def make_prediction(
    observation: Observation,
    candidates: Sequence[Candidate],
    config: RuleLoomConfig,
    *,
    predicted_at: datetime | None = None,
) -> Prediction:
    identifiers = [candidate.id for candidate in candidates]
    if len(identifiers) != len(set(identifiers)):
        raise ModelError("active policy set contains duplicate candidate ids")
    target = config.target
    expected_protocol_hash = config.evidence_protocol_hash
    expected_config_hash = config.hash
    if observation.protocol_hash != expected_protocol_hash:
        raise ModelError("observation belongs to a different evidence protocol")
    if target not in observation.labels:
        raise ModelError(f"observation lacks configured prediction target {target!r}")
    source_kind = observation.source.get("kind")
    repository_id = observation.source.get("repository")
    unit_id = observation.source.get("change_id")
    observation_pack = observation.source.get("pack")
    observation_pack_version = observation.source.get("pack_version")
    source_extractor = observation.source.get("extractor")
    descriptor = config.resolved_pack
    if source_kind != config.protocol.prediction_unit:
        raise ModelError(
            f"observation unit {source_kind!r} does not match configured prospective unit "
            f"{config.protocol.prediction_unit!r}"
        )
    if repository_id != config.protocol.repository_id:
        raise ModelError(
            f"observation repository {repository_id!r} does not match configured repository "
            f"{config.protocol.repository_id!r}"
        )
    if not isinstance(unit_id, str) or not unit_id:
        raise ModelError(
            "prospective observations require a stable source.change_id for independent-unit "
            "deduplication"
        )
    if observation_pack != config.pack:
        raise ModelError(
            f"observation fact pack {observation_pack!r} does not match configured pack "
            f"{config.pack!r}"
        )
    if (
        config.schema_version >= 2 or observation_pack_version is not None
    ) and not matches_pack_version(observation_pack_version, config.pack_version):
        raise ModelError(
            f"observation fact pack version {observation_pack_version!r} does not match "
            f"configured version {config.pack_version!r}"
        )
    if source_extractor != descriptor.extractor:
        raise ModelError(
            f"observation extractor provenance {source_extractor!r} does not match configured "
            f"extractor {descriptor.extractor!r}"
        )
    source_configuration = observation.source.get("pack_config_hash")
    if (
        descriptor.configuration_hash is not None
        and source_configuration != descriptor.configuration_hash
    ):
        raise ModelError("observation uses a different pack configuration")
    if descriptor.configuration_hash is None and source_configuration is not None:
        raise ModelError("observation has unexpected pack configuration")
    validate_persisted_extraction(
        descriptor,
        observation.facts,
        observation.fact_evidence,
        subject=f"prediction observation {observation.id!r}",
        metadata=observation.metadata,
    )
    targets = {candidate.rules.target for candidate in candidates}
    if len(targets) > 1:
        raise ModelError("active policy set contains multiple prediction targets")
    if targets and targets != {target}:
        raise ModelError("active policy target does not match configured prediction target")
    for candidate in candidates:
        candidate.validate_identity()
        if candidate.status not in {"shadow", "approved"}:
            raise ModelError(f"candidate {candidate.id} is not an active reviewed policy")
        if candidate.config_hash != expected_config_hash:
            raise ModelError(f"candidate {candidate.id} does not match the current configuration")
        if candidate.metadata.get("repository_id") != config.protocol.repository_id:
            raise ModelError(f"candidate {candidate.id} belongs to a different repository")
        candidate_pack = candidate.metadata.get("pack")
        if (
            isinstance(observation_pack, str)
            and isinstance(candidate_pack, str)
            and observation_pack != candidate_pack
        ):
            raise ModelError(
                f"observation fact pack {observation_pack!r} is incompatible with "
                f"candidate {candidate.id} pack {candidate_pack!r}"
            )
        validate_policy_pack_contract(
            descriptor,
            candidate.metadata,
            {literal.predicate for clause in candidate.rules.clauses for literal in clause.body},
            schema_version=config.schema_version,
            evidence_protocol_hash=expected_protocol_hash,
            subject=f"candidate {candidate.id}",
        )
    matches = match_rules(observation.facts, candidates)
    policies: list[JsonObject] = [
        {
            "candidate_id": item.id,
            "status": item.status,
            "target": item.rules.target,
            "manifest_hash": content_hash(item.to_dict()),
            "rule_signatures": cast(JsonValue, sorted(item.rules.signatures)),
        }
        for item in sorted(candidates, key=lambda candidate: candidate.id)
    ]
    protocol: JsonObject = {
        "experiment_id": config.protocol.experiment_id,
        "repository_id": config.protocol.repository_id,
        "observation_unit": config.protocol.prediction_unit,
        "outcome_definition": config.protocol.outcome_definition,
        "target": target,
        "pack": config.pack,
        "extractor": source_extractor,
        "config_hash": expected_config_hash,
        "evidence_protocol_hash": expected_protocol_hash,
    }
    protocol_hash = content_hash(protocol)
    policy_set_hash = content_hash(
        {
            "protocol_hash": protocol_hash,
            "target": target,
            "policies": cast(JsonValue, policies),
        }
    )
    prediction = Prediction(
        id="prediction.pending",
        predicted_at=utc_now(predicted_at),
        observation=observation,
        target=target,
        unit_id=unit_id,
        protocol_hash=protocol_hash,
        protocol=protocol,
        policy_set_hash=policy_set_hash,
        policies=tuple(policies),
        matches=tuple(item.to_dict() for item in matches),
        abstained=not matches,
    )
    return prediction.with_identity()
