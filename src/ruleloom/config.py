"""Project configuration with strict, dependency-free validation."""

from __future__ import annotations

import json
import math
import unicodedata
from dataclasses import dataclass, field
from math import comb
from pathlib import Path
from typing import Any

from ruleloom.models import (
    JsonObject,
    ModelError,
    content_hash,
    parse_timestamp,
    strict_json_loads,
    validate_predicate,
    validate_subject,
)
from ruleloom.packs import (
    ConfiguredPathsConfig,
    EvidencePack,
    PackOptions,
    get_pack,
    latest_pack_version,
)

CONFIG_PATH = Path(".ruleloom/config.json")
_MAX_CONFIG_BYTES = 1024 * 1024


def _config_path(root: Path) -> Path:
    resolved_root = root.resolve()
    current = resolved_root
    for component in CONFIG_PATH.parts:
        current /= component
        if current.is_symlink():
            raise ModelError(f"refusing to follow RuleLoom config symlink: {current}")
    return resolved_root / CONFIG_PATH


def _object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ModelError(f"{name} must be an object")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ModelError(f"{name} must be a non-empty string")
    return value


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ModelError(f"{name} must be an integer >= {minimum}")
    return value


def _number(value: object, name: str, *, minimum: float = 0, maximum: float = 1) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ModelError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ModelError(f"{name} must be between {minimum} and {maximum}")
    return result


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ModelError(f"{name} must be a boolean")
    return value


def _reject_unknown(value: dict[str, object], allowed: set[str], name: str) -> None:
    unknown = set(value).difference(allowed)
    if unknown:
        raise ModelError(f"unknown {name} fields: {', '.join(sorted(unknown))}")


def _require_fields(value: dict[str, object], required: set[str], name: str) -> None:
    missing = required.difference(value)
    if missing:
        raise ModelError(f"{name} is missing required fields: {', '.join(sorted(missing))}")


SEARCH_STRATEGIES = ("exhaustive", "beam")
PREDICATE_RANKINGS = ("rate_gap", "logistic_weight")
PRECISION_ESTIMATES = ("point", "wilson_lower")
_MAX_EXHAUSTIVE_PREDICATES = 32
_MAX_BEAM_PREDICATES = 256
_LEGACY_SEARCH_CONTROLS: dict[str, object] = {
    "search_strategy": "exhaustive",
    "beam_width": 20,
    "predicate_ranking": "rate_gap",
    "precision_estimate": "point",
    "require_temporal_consistency": False,
    "prune_fraction": 0.0,
    "permutation_runs": 0,
    "tree_seeds": False,
}


@dataclass(frozen=True, slots=True)
class LearnerConfig:
    engine: str = "horn"
    max_body: int = 3
    max_rules: int = 3
    allow_negation: bool = True
    min_precision: float = 0.7
    min_support: int = 2
    false_positive_cost: float = 1.5
    bootstrap_runs: int = 30
    max_predicates: int = 12
    popper_dir: str | None = None
    popper_timeout_seconds: int = 120
    gate_mode: str = "absolute_precision"
    min_lift_lower_bound: float = 3.0
    min_alert_rate: float = 0.01
    confidence_level: float = 0.95
    near_miss_limit: int = 10
    search_strategy: str = "exhaustive"
    beam_width: int = 20
    predicate_ranking: str = "rate_gap"
    precision_estimate: str = "point"
    require_temporal_consistency: bool = False
    prune_fraction: float = 0.0
    permutation_runs: int = 0
    tree_seeds: bool = False

    def __post_init__(self) -> None:
        if self.engine not in {"horn", "popper"}:
            raise ModelError("learner.engine must be 'horn' or 'popper'")
        if not 1 <= self.max_body <= 4:
            raise ModelError("max_body must be between 1 and 4")
        if not 1 <= self.max_rules <= 10:
            raise ModelError("max_rules must be between 1 and 10")
        if not 0 <= self.min_precision <= 1:
            raise ModelError("min_precision must be between 0 and 1")
        if self.min_support < 1:
            raise ModelError("min_support must be >= 1")
        if not math.isfinite(self.false_positive_cost) or self.false_positive_cost < 0:
            raise ModelError("false_positive_cost must be >= 0")
        if not 0 <= self.bootstrap_runs <= 100:
            raise ModelError("bootstrap_runs must be between 0 and 100")
        if self.search_strategy not in SEARCH_STRATEGIES:
            raise ModelError(
                "learner.search_strategy must be one of: " + ", ".join(SEARCH_STRATEGIES)
            )
        predicate_cap = (
            _MAX_BEAM_PREDICATES if self.search_strategy == "beam" else _MAX_EXHAUSTIVE_PREDICATES
        )
        if not 1 <= self.max_predicates <= predicate_cap:
            raise ModelError(
                f"max_predicates must be between 1 and {predicate_cap} for "
                f"search_strategy={self.search_strategy!r}"
            )
        if not 1 <= self.beam_width <= 256:
            raise ModelError("learner.beam_width must be between 1 and 256")
        if self.predicate_ranking not in PREDICATE_RANKINGS:
            raise ModelError(
                "learner.predicate_ranking must be one of: " + ", ".join(PREDICATE_RANKINGS)
            )
        if self.precision_estimate not in PRECISION_ESTIMATES:
            raise ModelError(
                "learner.precision_estimate must be one of: " + ", ".join(PRECISION_ESTIMATES)
            )
        if not isinstance(self.require_temporal_consistency, bool):
            raise ModelError("learner.require_temporal_consistency must be a boolean")
        if not isinstance(self.tree_seeds, bool):
            raise ModelError("learner.tree_seeds must be a boolean")
        if (
            isinstance(self.prune_fraction, bool)
            or not isinstance(self.prune_fraction, int | float)
            or not math.isfinite(self.prune_fraction)
            or not 0 <= self.prune_fraction <= 0.5
        ):
            raise ModelError("learner.prune_fraction must be between 0 and 0.5")
        if (
            isinstance(self.permutation_runs, bool)
            or not isinstance(self.permutation_runs, int)
            or not 0 <= self.permutation_runs <= 1000
        ):
            raise ModelError("learner.permutation_runs must be between 0 and 1000")
        if not 1 <= self.popper_timeout_seconds <= 3600:
            raise ModelError("popper_timeout_seconds must be between 1 and 3600")
        if self.gate_mode not in {"absolute_precision", "relative_lift"}:
            raise ModelError("learner.gate_mode must be 'absolute_precision' or 'relative_lift'")
        if (
            not math.isfinite(self.min_lift_lower_bound)
            or self.min_lift_lower_bound < 1
            or self.min_lift_lower_bound > 1000
        ):
            raise ModelError("learner.min_lift_lower_bound must be between 1 and 1000")
        if not math.isfinite(self.min_alert_rate) or not 0 <= self.min_alert_rate <= 1:
            raise ModelError("learner.min_alert_rate must be between 0 and 1")
        if not math.isfinite(self.confidence_level) or not 0.5 < self.confidence_level < 1:
            raise ModelError("learner.confidence_level must be strictly between 0.5 and 1")
        if not 0 <= self.near_miss_limit <= 100:
            raise ModelError("learner.near_miss_limit must be between 0 and 100")
        hypotheses = self.hypothesis_count()
        if (
            hypotheses > 250_000
            or hypotheses * (self.bootstrap_runs + 1 + self.permutation_runs) > 5_000_000
        ):
            raise ModelError(
                "learner search budget is too large; reduce max_body, max_predicates, "
                "beam_width, bootstrap_runs, or permutation_runs"
            )
        if self.engine == "popper":
            if self.max_rules != 1:
                raise ModelError(
                    "learner.max_rules must be 1 for the supported non-recursive Popper adapter"
                )
            if self.bootstrap_runs != 0:
                raise ModelError(
                    "learner.bootstrap_runs must be 0 for Popper; RuleLoom does not rerun "
                    "external Popper during bootstrap"
                )
            horn_only = (
                self.min_precision != 0.7
                or self.min_support != 2
                or self.false_positive_cost != 1.5
            )
            if horn_only:
                raise ModelError(
                    "learner.min_precision, min_support, and false_positive_cost are "
                    "built-in Horn settings and must retain their defaults when engine='popper'"
                )
            if not self.search_controls_are_legacy:
                raise ModelError(
                    "learner search controls (beam search, pruning, permutation null, "
                    "tree seeds, precision estimate, temporal consistency) apply to the "
                    "built-in Horn engine only; keep their defaults when engine='popper'"
                )
            if self.popper_dir is None or not Path(self.popper_dir).is_absolute():
                raise ModelError(
                    "learner.popper_dir must be an explicit absolute path when engine='popper'"
                )

    @property
    def search_controls(self) -> dict[str, object]:
        return {
            "search_strategy": self.search_strategy,
            "beam_width": self.beam_width,
            "predicate_ranking": self.predicate_ranking,
            "precision_estimate": self.precision_estimate,
            "require_temporal_consistency": self.require_temporal_consistency,
            "prune_fraction": self.prune_fraction,
            "permutation_runs": self.permutation_runs,
            "tree_seeds": self.tree_seeds,
        }

    @property
    def search_controls_are_legacy(self) -> bool:
        """Whether the Horn 0.5 behaviour is reproduced exactly (schema v4 and older)."""
        return self.search_controls == _LEGACY_SEARCH_CONTROLS

    def hypothesis_count(self, predicate_count: int | None = None) -> int:
        """Return the bounded number of clause bodies considered by one Horn rule search."""
        predicates = self.max_predicates if predicate_count is None else predicate_count
        if isinstance(predicates, bool) or not 0 <= predicates <= self.max_predicates:
            raise ModelError("predicate_count must be between 0 and learner.max_predicates")
        literal_variants = 2 if self.allow_negation else 1
        if self.search_strategy == "beam":
            return literal_variants * predicates * (1 + self.beam_width * (self.max_body - 1))
        return sum(
            comb(predicates, size) * literal_variants**size
            for size in range(1, min(self.max_body, predicates) + 1)
        )

    def to_dict(
        self,
        *,
        include_signal_gates: bool = False,
        include_search_controls: bool = False,
    ) -> JsonObject:
        value: JsonObject = {
            "engine": self.engine,
            "max_body": self.max_body,
            "max_rules": self.max_rules,
            "allow_negation": self.allow_negation,
            "min_precision": self.min_precision,
            "min_support": self.min_support,
            "false_positive_cost": self.false_positive_cost,
            "bootstrap_runs": self.bootstrap_runs,
            "max_predicates": self.max_predicates,
            "popper_dir": self.popper_dir,
            "popper_timeout_seconds": self.popper_timeout_seconds,
        }
        if include_signal_gates:
            value.update(
                {
                    "gate_mode": self.gate_mode,
                    "min_lift_lower_bound": self.min_lift_lower_bound,
                    "min_alert_rate": self.min_alert_rate,
                    "confidence_level": self.confidence_level,
                    "near_miss_limit": self.near_miss_limit,
                }
            )
        if include_search_controls:
            value.update(
                {
                    "search_strategy": self.search_strategy,
                    "beam_width": self.beam_width,
                    "predicate_ranking": self.predicate_ranking,
                    "precision_estimate": self.precision_estimate,
                    "require_temporal_consistency": self.require_temporal_consistency,
                    "prune_fraction": self.prune_fraction,
                    "permutation_runs": self.permutation_runs,
                    "tree_seeds": self.tree_seeds,
                }
            )
        return value

    @classmethod
    def from_dict(
        cls,
        value: dict[str, object],
        *,
        include_signal_gates: bool = False,
        include_search_controls: bool = False,
    ) -> LearnerConfig:
        signal_fields = (
            {
                "gate_mode",
                "min_lift_lower_bound",
                "min_alert_rate",
                "confidence_level",
                "near_miss_limit",
            }
            if include_signal_gates
            else set()
        )
        search_fields = set(_LEGACY_SEARCH_CONTROLS) if include_search_controls else set()
        _reject_unknown(
            value,
            {
                "engine",
                "max_body",
                "max_rules",
                "allow_negation",
                "min_precision",
                "min_support",
                "false_positive_cost",
                "bootstrap_runs",
                "max_predicates",
                "popper_dir",
                "popper_timeout_seconds",
                *signal_fields,
                *search_fields,
            },
            "learner",
        )
        raw_popper_dir = value.get("popper_dir")
        if raw_popper_dir is not None and not isinstance(raw_popper_dir, str):
            raise ModelError("learner.popper_dir must be a string or null")
        return cls(
            engine=_string(value.get("engine", "horn"), "learner.engine"),
            max_body=_integer(value.get("max_body", 3), "learner.max_body", minimum=1),
            max_rules=_integer(value.get("max_rules", 3), "learner.max_rules", minimum=1),
            allow_negation=_boolean(value.get("allow_negation", True), "learner.allow_negation"),
            min_precision=_number(value.get("min_precision", 0.7), "learner.min_precision"),
            min_support=_integer(value.get("min_support", 2), "learner.min_support", minimum=1),
            false_positive_cost=_number(
                value.get("false_positive_cost", 1.5),
                "learner.false_positive_cost",
                maximum=float("inf"),
            ),
            bootstrap_runs=_integer(value.get("bootstrap_runs", 30), "learner.bootstrap_runs"),
            max_predicates=_integer(
                value.get("max_predicates", 12), "learner.max_predicates", minimum=1
            ),
            popper_dir=raw_popper_dir,
            popper_timeout_seconds=_integer(
                value.get("popper_timeout_seconds", 120),
                "learner.popper_timeout_seconds",
                minimum=1,
            ),
            gate_mode=_string(value.get("gate_mode", "absolute_precision"), "learner.gate_mode"),
            min_lift_lower_bound=_number(
                value.get("min_lift_lower_bound", 3.0),
                "learner.min_lift_lower_bound",
                minimum=1,
                maximum=1000,
            ),
            min_alert_rate=_number(value.get("min_alert_rate", 0.01), "learner.min_alert_rate"),
            confidence_level=_number(
                value.get("confidence_level", 0.95),
                "learner.confidence_level",
                minimum=0.500001,
                maximum=0.999999,
            ),
            near_miss_limit=_integer(value.get("near_miss_limit", 10), "learner.near_miss_limit"),
            search_strategy=_string(
                value.get("search_strategy", "exhaustive"), "learner.search_strategy"
            ),
            beam_width=_integer(value.get("beam_width", 20), "learner.beam_width", minimum=1),
            predicate_ranking=_string(
                value.get("predicate_ranking", "rate_gap"), "learner.predicate_ranking"
            ),
            precision_estimate=_string(
                value.get("precision_estimate", "point"), "learner.precision_estimate"
            ),
            require_temporal_consistency=_boolean(
                value.get("require_temporal_consistency", False),
                "learner.require_temporal_consistency",
            ),
            prune_fraction=_number(
                value.get("prune_fraction", 0.0),
                "learner.prune_fraction",
                maximum=0.5,
            ),
            permutation_runs=_integer(value.get("permutation_runs", 0), "learner.permutation_runs"),
            tree_seeds=_boolean(value.get("tree_seeds", False), "learner.tree_seeds"),
        )


@dataclass(frozen=True, slots=True)
class SignalProbeConfig:
    """Train-only signal-availability probe that protects the temporal holdout."""

    enabled: bool = False
    folds: int = 4
    min_train_examples: int = 20
    min_validation_examples: int = 5
    min_mcc: float = 0.25
    min_lift_lower_bound: float = 3.0
    min_alert_rate: float = 0.01
    confidence_level: float = 0.95
    tree_max_depth: int = 2
    max_predicates: int = 256

    def __post_init__(self) -> None:
        if not 2 <= self.folds <= 20:
            raise ModelError("signal_probe.folds must be between 2 and 20")
        if self.min_train_examples < 4:
            raise ModelError("signal_probe.min_train_examples must be >= 4")
        if self.min_validation_examples < 2:
            raise ModelError("signal_probe.min_validation_examples must be >= 2")
        if not math.isfinite(self.min_mcc) or not -1 <= self.min_mcc <= 1:
            raise ModelError("signal_probe.min_mcc must be between -1 and 1")
        if (
            not math.isfinite(self.min_lift_lower_bound)
            or not 1 <= self.min_lift_lower_bound <= 1000
        ):
            raise ModelError("signal_probe.min_lift_lower_bound must be between 1 and 1000")
        if not math.isfinite(self.min_alert_rate) or not 0 <= self.min_alert_rate <= 1:
            raise ModelError("signal_probe.min_alert_rate must be between 0 and 1")
        if not math.isfinite(self.confidence_level) or not 0.5 < self.confidence_level < 1:
            raise ModelError("signal_probe.confidence_level must be strictly between 0.5 and 1")
        if not 1 <= self.tree_max_depth <= 4:
            raise ModelError("signal_probe.tree_max_depth must be between 1 and 4")
        if not 1 <= self.max_predicates <= 1024:
            raise ModelError("signal_probe.max_predicates must be between 1 and 1024")

    def to_dict(self) -> JsonObject:
        return {
            "enabled": self.enabled,
            "folds": self.folds,
            "min_train_examples": self.min_train_examples,
            "min_validation_examples": self.min_validation_examples,
            "min_mcc": self.min_mcc,
            "min_lift_lower_bound": self.min_lift_lower_bound,
            "min_alert_rate": self.min_alert_rate,
            "confidence_level": self.confidence_level,
            "tree_max_depth": self.tree_max_depth,
            "max_predicates": self.max_predicates,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> SignalProbeConfig:
        _reject_unknown(
            value,
            {
                "enabled",
                "folds",
                "min_train_examples",
                "min_validation_examples",
                "min_mcc",
                "min_lift_lower_bound",
                "min_alert_rate",
                "confidence_level",
                "tree_max_depth",
                "max_predicates",
            },
            "signal_probe",
        )
        return cls(
            enabled=_boolean(value.get("enabled", False), "signal_probe.enabled"),
            folds=_integer(value.get("folds", 4), "signal_probe.folds", minimum=2),
            min_train_examples=_integer(
                value.get("min_train_examples", 20),
                "signal_probe.min_train_examples",
                minimum=4,
            ),
            min_validation_examples=_integer(
                value.get("min_validation_examples", 5),
                "signal_probe.min_validation_examples",
                minimum=2,
            ),
            min_mcc=_number(value.get("min_mcc", 0.25), "signal_probe.min_mcc", minimum=-1),
            min_lift_lower_bound=_number(
                value.get("min_lift_lower_bound", 3.0),
                "signal_probe.min_lift_lower_bound",
                minimum=1,
                maximum=1000,
            ),
            min_alert_rate=_number(
                value.get("min_alert_rate", 0.01), "signal_probe.min_alert_rate"
            ),
            confidence_level=_number(
                value.get("confidence_level", 0.95),
                "signal_probe.confidence_level",
                minimum=0.500001,
                maximum=0.999999,
            ),
            tree_max_depth=_integer(
                value.get("tree_max_depth", 2), "signal_probe.tree_max_depth", minimum=1
            ),
            max_predicates=_integer(
                value.get("max_predicates", 256), "signal_probe.max_predicates", minimum=1
            ),
        )


@dataclass(frozen=True, slots=True)
class EvaluationConfig:
    test_fraction: float = 0.25
    min_train_examples: int = 8
    min_test_examples: int = 4
    seed: int = 17
    test_start_at: str | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.test_fraction) or not 0 < self.test_fraction < 1:
            raise ModelError("evaluation.test_fraction must be strictly between 0 and 1")
        if self.min_train_examples < 2 or self.min_test_examples < 1:
            raise ModelError("evaluation minimum sizes are invalid")
        if self.test_start_at is not None:
            parse_timestamp(self.test_start_at)

    def to_dict(self) -> JsonObject:
        value: JsonObject = {
            "test_fraction": self.test_fraction,
            "min_train_examples": self.min_train_examples,
            "min_test_examples": self.min_test_examples,
            "seed": self.seed,
        }
        if self.test_start_at is not None:
            value["test_start_at"] = self.test_start_at
        return value

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> EvaluationConfig:
        _reject_unknown(
            value,
            {
                "test_fraction",
                "min_train_examples",
                "min_test_examples",
                "seed",
                "test_start_at",
            },
            "evaluation",
        )
        return cls(
            test_fraction=_number(
                value.get("test_fraction", 0.25),
                "evaluation.test_fraction",
                minimum=0.000001,
                maximum=0.999999,
            ),
            min_train_examples=_integer(
                value.get("min_train_examples", 8),
                "evaluation.min_train_examples",
                minimum=2,
            ),
            min_test_examples=_integer(
                value.get("min_test_examples", 4),
                "evaluation.min_test_examples",
                minimum=1,
            ),
            seed=_integer(value.get("seed", 17), "evaluation.seed"),
            test_start_at=(
                _string(value["test_start_at"], "evaluation.test_start_at")
                if "test_start_at" in value
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class ProtocolConfig:
    """Prospective evaluation contract used to prevent accidental evidence pooling."""

    experiment_id: str = "ruleloom-pilot-v1"
    repository_id: str = "repository.unspecified"
    prediction_unit: str = "git_worktree"
    outcome_definition: str = (
        "target label supported by timestamped evidence that became available after prediction"
    )

    def __post_init__(self) -> None:
        validate_subject(self.experiment_id)
        validate_subject(self.repository_id)
        if self.prediction_unit not in {
            "git_commit",
            "git_range",
            "git_worktree",
            "provider_change",
        }:
            raise ModelError(
                "protocol.prediction_unit must be git_commit, git_range, git_worktree, "
                "or provider_change"
            )
        if (
            not self.outcome_definition.strip()
            or len(self.outcome_definition) > 500
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in self.outcome_definition
            )
        ):
            raise ModelError(
                "protocol.outcome_definition must be a non-empty, single-line string of at "
                "most 500 characters"
            )

    def to_dict(self) -> JsonObject:
        return {
            "experiment_id": self.experiment_id,
            "repository_id": self.repository_id,
            "prediction_unit": self.prediction_unit,
            "outcome_definition": self.outcome_definition,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> ProtocolConfig:
        _reject_unknown(
            value,
            {"experiment_id", "repository_id", "prediction_unit", "outcome_definition"},
            "protocol",
        )
        return cls(
            experiment_id=_string(
                value.get("experiment_id", "ruleloom-pilot-v1"),
                "protocol.experiment_id",
            ),
            repository_id=_string(
                value.get("repository_id", "repository.unspecified"),
                "protocol.repository_id",
            ),
            prediction_unit=_string(
                value.get("prediction_unit", "git_worktree"),
                "protocol.prediction_unit",
            ),
            outcome_definition=_string(
                value.get(
                    "outcome_definition",
                    "target label supported by timestamped evidence that became available "
                    "after prediction",
                ),
                "protocol.outcome_definition",
            ),
        )


@dataclass(frozen=True, slots=True)
class PromotionConfig:
    min_test_precision: float = 0.75
    min_test_recall: float = 0.5
    min_stability: float = 0.4
    require_test_set: bool = True
    min_positive_for_shadow: int = 20
    min_positive_for_approval: int = 50
    require_baseline_improvement: bool = True
    min_shadow_predictions_for_approval: int = 30
    min_shadow_mature_outcomes_for_approval: int = 30
    min_shadow_days_for_approval: int = 7
    min_shadow_precision: float = 0.7
    min_shadow_recall: float = 0.5
    min_shadow_mcc: float = 0.1
    min_shadow_positive_outcomes_for_approval: int = 10
    min_shadow_negative_outcomes_for_approval: int = 10
    min_shadow_matches_per_rule_for_approval: int = 10

    def __post_init__(self) -> None:
        for name, value in {
            "min_test_precision": self.min_test_precision,
            "min_test_recall": self.min_test_recall,
            "min_stability": self.min_stability,
            "min_shadow_precision": self.min_shadow_precision,
            "min_shadow_recall": self.min_shadow_recall,
        }.items():
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ModelError(f"promotion.{name} must be between 0 and 1")
        if not math.isfinite(self.min_shadow_mcc) or not -1 <= self.min_shadow_mcc <= 1:
            raise ModelError("promotion.min_shadow_mcc must be between -1 and 1")
        for name, value, minimum in (
            ("min_positive_for_shadow", self.min_positive_for_shadow, 1),
            ("min_positive_for_approval", self.min_positive_for_approval, 1),
            (
                "min_shadow_predictions_for_approval",
                self.min_shadow_predictions_for_approval,
                1,
            ),
            (
                "min_shadow_mature_outcomes_for_approval",
                self.min_shadow_mature_outcomes_for_approval,
                1,
            ),
            ("min_shadow_days_for_approval", self.min_shadow_days_for_approval, 0),
            (
                "min_shadow_positive_outcomes_for_approval",
                self.min_shadow_positive_outcomes_for_approval,
                1,
            ),
            (
                "min_shadow_negative_outcomes_for_approval",
                self.min_shadow_negative_outcomes_for_approval,
                1,
            ),
            (
                "min_shadow_matches_per_rule_for_approval",
                self.min_shadow_matches_per_rule_for_approval,
                1,
            ),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ModelError(f"promotion.{name} must be an integer >= {minimum}")

    def to_dict(self) -> JsonObject:
        return {
            "min_test_precision": self.min_test_precision,
            "min_test_recall": self.min_test_recall,
            "min_stability": self.min_stability,
            "require_test_set": self.require_test_set,
            "min_positive_for_shadow": self.min_positive_for_shadow,
            "min_positive_for_approval": self.min_positive_for_approval,
            "require_baseline_improvement": self.require_baseline_improvement,
            "min_shadow_predictions_for_approval": self.min_shadow_predictions_for_approval,
            "min_shadow_mature_outcomes_for_approval": (
                self.min_shadow_mature_outcomes_for_approval
            ),
            "min_shadow_days_for_approval": self.min_shadow_days_for_approval,
            "min_shadow_precision": self.min_shadow_precision,
            "min_shadow_recall": self.min_shadow_recall,
            "min_shadow_mcc": self.min_shadow_mcc,
            "min_shadow_positive_outcomes_for_approval": (
                self.min_shadow_positive_outcomes_for_approval
            ),
            "min_shadow_negative_outcomes_for_approval": (
                self.min_shadow_negative_outcomes_for_approval
            ),
            "min_shadow_matches_per_rule_for_approval": (
                self.min_shadow_matches_per_rule_for_approval
            ),
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> PromotionConfig:
        _reject_unknown(
            value,
            {
                "min_test_precision",
                "min_test_recall",
                "min_stability",
                "require_test_set",
                "min_positive_for_shadow",
                "min_positive_for_approval",
                "require_baseline_improvement",
                "min_shadow_predictions_for_approval",
                "min_shadow_mature_outcomes_for_approval",
                "min_shadow_days_for_approval",
                "min_shadow_precision",
                "min_shadow_recall",
                "min_shadow_mcc",
                "min_shadow_positive_outcomes_for_approval",
                "min_shadow_negative_outcomes_for_approval",
                "min_shadow_matches_per_rule_for_approval",
            },
            "promotion",
        )
        return cls(
            min_test_precision=_number(
                value.get("min_test_precision", 0.75),
                "promotion.min_test_precision",
            ),
            min_test_recall=_number(value.get("min_test_recall", 0.5), "promotion.min_test_recall"),
            min_stability=_number(value.get("min_stability", 0.4), "promotion.min_stability"),
            require_test_set=_boolean(
                value.get("require_test_set", True), "promotion.require_test_set"
            ),
            min_positive_for_shadow=_integer(
                value.get("min_positive_for_shadow", 20),
                "promotion.min_positive_for_shadow",
                minimum=1,
            ),
            min_positive_for_approval=_integer(
                value.get("min_positive_for_approval", 50),
                "promotion.min_positive_for_approval",
                minimum=1,
            ),
            require_baseline_improvement=_boolean(
                value.get("require_baseline_improvement", True),
                "promotion.require_baseline_improvement",
            ),
            min_shadow_predictions_for_approval=_integer(
                value.get("min_shadow_predictions_for_approval", 30),
                "promotion.min_shadow_predictions_for_approval",
                minimum=1,
            ),
            min_shadow_mature_outcomes_for_approval=_integer(
                value.get("min_shadow_mature_outcomes_for_approval", 30),
                "promotion.min_shadow_mature_outcomes_for_approval",
                minimum=1,
            ),
            min_shadow_days_for_approval=_integer(
                value.get("min_shadow_days_for_approval", 7),
                "promotion.min_shadow_days_for_approval",
            ),
            min_shadow_precision=_number(
                value.get("min_shadow_precision", 0.7),
                "promotion.min_shadow_precision",
            ),
            min_shadow_recall=_number(
                value.get("min_shadow_recall", 0.5),
                "promotion.min_shadow_recall",
            ),
            min_shadow_mcc=_number(
                value.get("min_shadow_mcc", 0.1),
                "promotion.min_shadow_mcc",
                minimum=-1,
            ),
            min_shadow_positive_outcomes_for_approval=_integer(
                value.get("min_shadow_positive_outcomes_for_approval", 10),
                "promotion.min_shadow_positive_outcomes_for_approval",
                minimum=1,
            ),
            min_shadow_negative_outcomes_for_approval=_integer(
                value.get("min_shadow_negative_outcomes_for_approval", 10),
                "promotion.min_shadow_negative_outcomes_for_approval",
                minimum=1,
            ),
            min_shadow_matches_per_rule_for_approval=_integer(
                value.get("min_shadow_matches_per_rule_for_approval", 10),
                "promotion.min_shadow_matches_per_rule_for_approval",
                minimum=1,
            ),
        )


def _scope_pattern(value: object, name: str) -> str:
    pattern = _string(value, name)
    path = Path(pattern)
    if (
        len(pattern) > 256
        or any(ord(character) < 32 or ord(character) == 127 for character in pattern)
        or "\\" in pattern
        or path.is_absolute()
        or ".." in path.parts
        or pattern.startswith(":")
    ):
        raise ModelError(
            f"{name} must be a portable repository-relative glob without '..', backslashes, "
            "Git pathspec magic, or control characters"
        )
    return pattern


@dataclass(frozen=True, slots=True)
class EvidenceConfig:
    """Pack-neutral collection scope and change-shape thresholds."""

    include_paths: tuple[str, ...] = ("**",)
    exclude_paths: tuple[str, ...] = ()
    large_change_churn: int = 200
    multi_file_count: int = 3
    metadata_file_limit: int = 512

    def __post_init__(self) -> None:
        for value, name in (
            (self.include_paths, "evidence.include_paths"),
            (self.exclude_paths, "evidence.exclude_paths"),
        ):
            if isinstance(value, str) or not isinstance(value, tuple | list):
                raise ModelError(f"{name} must be an array of globs")
            if not all(isinstance(item, str) for item in value):
                raise ModelError(f"{name} items must be strings")
        if not self.include_paths:
            raise ModelError("evidence.include_paths must contain at least one glob")
        if len(self.include_paths) > 128 or len(self.exclude_paths) > 128:
            raise ModelError("evidence path scopes support at most 128 include and exclude globs")
        include = tuple(
            sorted(
                {_scope_pattern(item, "evidence.include_paths item") for item in self.include_paths}
            )
        )
        exclude = tuple(
            sorted(
                {_scope_pattern(item, "evidence.exclude_paths item") for item in self.exclude_paths}
            )
        )
        object.__setattr__(self, "include_paths", include)
        object.__setattr__(self, "exclude_paths", exclude)
        if (
            isinstance(self.large_change_churn, bool)
            or not isinstance(self.large_change_churn, int)
            or not 1 <= self.large_change_churn <= 10_000_000
        ):
            raise ModelError("evidence.large_change_churn must be between 1 and 10000000")
        if (
            isinstance(self.multi_file_count, bool)
            or not isinstance(self.multi_file_count, int)
            or not 1 <= self.multi_file_count <= 100_000
        ):
            raise ModelError("evidence.multi_file_count must be between 1 and 100000")
        if (
            isinstance(self.metadata_file_limit, bool)
            or not isinstance(self.metadata_file_limit, int)
            or not 1 <= self.metadata_file_limit <= 10_000
        ):
            raise ModelError("evidence.metadata_file_limit must be between 1 and 10000")

    @property
    def pack_options(self) -> PackOptions:
        return PackOptions(
            large_change_churn=self.large_change_churn,
            multi_file_count=self.multi_file_count,
            metadata_file_limit=self.metadata_file_limit,
        )

    def to_dict(self) -> JsonObject:
        return {
            "include_paths": list(self.include_paths),
            "exclude_paths": list(self.exclude_paths),
            "large_change_churn": self.large_change_churn,
            "multi_file_count": self.multi_file_count,
            "metadata_file_limit": self.metadata_file_limit,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> EvidenceConfig:
        _reject_unknown(
            value,
            {
                "include_paths",
                "exclude_paths",
                "large_change_churn",
                "multi_file_count",
                "metadata_file_limit",
            },
            "evidence",
        )

        def patterns(key: str, default: list[str]) -> tuple[str, ...]:
            raw = value.get(key, default)
            if not isinstance(raw, list):
                raise ModelError(f"evidence.{key} must be an array of path globs")
            return tuple(_scope_pattern(item, f"evidence.{key} item") for item in raw)

        return cls(
            include_paths=patterns("include_paths", ["**"]),
            exclude_paths=patterns("exclude_paths", []),
            large_change_churn=_integer(
                value.get("large_change_churn", 200),
                "evidence.large_change_churn",
                minimum=1,
            ),
            multi_file_count=_integer(
                value.get("multi_file_count", 3),
                "evidence.multi_file_count",
                minimum=1,
            ),
            metadata_file_limit=_integer(
                value.get("metadata_file_limit", 512),
                "evidence.metadata_file_limit",
                minimum=1,
            ),
        )


@dataclass(frozen=True, slots=True)
class RuleLoomConfig:
    project: str
    target: str = "needs_extra_validation"
    pack: str = "generic_changes"
    pack_version: int = 1
    dataset: str = ".ruleloom/observations.jsonl"
    candidates_dir: str = ".ruleloom/candidates"
    shadow_dir: str = ".ruleloom/shadow"
    approved_dir: str = ".ruleloom/approved"
    deprecated_dir: str = ".ruleloom/deprecated"
    predictions: str = ".ruleloom/predictions.jsonl"
    protocol: ProtocolConfig = field(default_factory=ProtocolConfig)
    evidence: EvidenceConfig = field(default_factory=EvidenceConfig)
    learner: LearnerConfig = field(default_factory=LearnerConfig)
    evaluation: EvaluationConfig = field(default_factory=EvaluationConfig)
    promotion: PromotionConfig = field(default_factory=PromotionConfig)
    schema_version: int = 2
    pack_config: ConfiguredPathsConfig | None = None
    signal_probe: SignalProbeConfig = field(default_factory=SignalProbeConfig)

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version not in {1, 2, 3, 4, 5}
        ):
            raise ModelError("unsupported config schema_version")
        if not self.project.strip():
            raise ModelError("project cannot be empty")
        if (
            len(self.project) > 80
            or any(ord(character) < 32 or ord(character) == 127 for character in self.project)
            or not self.project[0].isalnum()
        ):
            raise ModelError(
                "project must contain no control characters, be at most 80 characters, "
                "and start alphanumerically"
            )
        validate_predicate(self.target, field_name="target")
        if (
            isinstance(self.pack_version, bool)
            or not isinstance(self.pack_version, int)
            or self.pack_version < 1
        ):
            raise ModelError("pack_version must be an integer >= 1")
        if self.schema_version == 1:
            if self.pack != "flutter_testing" or self.pack_version != 1:
                raise ModelError("schema-v1 configs support only flutter_testing version 1")
            if self.evidence != EvidenceConfig():
                raise ModelError("schema-v1 configs cannot customize evidence collection")
            if self.pack_config is not None:
                raise ModelError("schema-v1 configs cannot define pack_config")
        elif self.pack == "flutter_testing" and self.pack_version == 1:
            raise ModelError(
                "flutter_testing version 1 is frozen for schema-v1 compatibility; "
                "schema-v2+ experiments must use flutter_testing version 2"
            )
        if self.schema_version == 2 and self.pack_config is not None:
            raise ModelError("schema-v2 configs cannot define pack_config")
        if self.schema_version < 3 and self.pack == "configured_paths":
            raise ModelError("configured_paths requires config schema_version 3")
        if self.schema_version < 4 and self.signal_probe != SignalProbeConfig():
            raise ModelError("signal_probe requires config schema_version 4")
        if self.schema_version < 5 and not self.learner.search_controls_are_legacy:
            raise ModelError(
                "learner search controls (search_strategy, beam_width, predicate_ranking, "
                "precision_estimate, require_temporal_consistency, prune_fraction, "
                "permutation_runs, tree_seeds) require config schema_version 5"
            )
        if (
            self.schema_version >= 4
            and self.signal_probe.enabled
            and self.evaluation.test_start_at is None
        ):
            raise ModelError(
                "schema-v4 signal probing requires evaluation.test_start_at to freeze the "
                "holdout before inspecting labels"
            )
        descriptor = get_pack(
            self.pack,
            self.pack_version,
            self.pack_config if self.schema_version >= 3 else None,
        )
        if self.pack_config is not None and self.target in descriptor.predicates:
            raise ModelError(f"target {self.target!r} collides with an evidence-pack predicate")
        managed = {
            "dataset": self.dataset,
            "candidates_dir": self.candidates_dir,
            "shadow_dir": self.shadow_dir,
            "approved_dir": self.approved_dir,
            "deprecated_dir": self.deprecated_dir,
            "predictions": self.predictions,
        }
        for name, value in managed.items():
            path = Path(value)
            if (
                not value.strip()
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
                or path == Path(".")
                or path.is_absolute()
                or ".." in path.parts
            ):
                raise ModelError(f"{name} must be a non-empty project-relative path without '..'")
            if not path.parts or path.parts[0] != ".ruleloom":
                raise ModelError(
                    f"{name} must remain under .ruleloom/ so evidence cannot contaminate "
                    "Git fact extraction"
                )
        layout = {
            **{name: Path(value) for name, value in managed.items()},
            "config": CONFIG_PATH,
            "readme": Path(".ruleloom/README.md"),
            "history": Path(".ruleloom/history"),
        }

        def portable_identity(path: Path) -> tuple[str, ...]:
            return tuple(unicodedata.normalize("NFC", part).casefold() for part in path.parts)

        items = [(name, path, portable_identity(path)) for name, path in layout.items()]
        for index, (left_name, left, left_identity) in enumerate(items):
            for right_name, right, right_identity in items[index + 1 :]:
                overlap = (
                    left_identity == right_identity
                    or left_identity == right_identity[: len(left_identity)]
                    or right_identity == left_identity[: len(right_identity)]
                )
                if overlap:
                    raise ModelError(
                        f"managed paths must not overlap: {left_name}={left} and "
                        f"{right_name}={right}"
                    )

    @property
    def hash(self) -> str:
        return content_hash(self.to_dict())

    @property
    def resolved_pack(self) -> EvidencePack:
        return get_pack(
            self.pack,
            self.pack_version,
            self.pack_config if self.schema_version >= 3 else None,
        )

    @property
    def pack_config_dict(self) -> JsonObject:
        return self.pack_config.to_dict() if self.pack_config is not None else {}

    @property
    def pack_config_hash(self) -> str | None:
        return self.resolved_pack.configuration_hash

    @property
    def evidence_protocol(self) -> JsonObject:
        """Fields that decide whether observations and outcomes may be pooled."""
        protocol: JsonObject = {
            "schema_version": self.schema_version,
            "experiment_id": self.protocol.experiment_id,
            "repository_id": self.protocol.repository_id,
            "prediction_unit": self.protocol.prediction_unit,
            "outcome_definition": self.protocol.outcome_definition,
            "target": self.target,
            "pack": self.pack,
        }
        if self.schema_version >= 2:
            protocol["pack_version"] = self.pack_version
            protocol["extractor"] = self.resolved_pack.extractor
            protocol["evidence"] = self.evidence.to_dict()
        if self.schema_version >= 3:
            protocol["pack_config"] = self.pack_config_dict
        return protocol

    @property
    def evidence_protocol_hash(self) -> str:
        return content_hash(self.evidence_protocol)

    def to_dict(self) -> JsonObject:
        value: JsonObject = {
            "schema_version": self.schema_version,
            "project": self.project,
            "target": self.target,
            "pack": self.pack,
            "dataset": self.dataset,
            "candidates_dir": self.candidates_dir,
            "shadow_dir": self.shadow_dir,
            "approved_dir": self.approved_dir,
            "deprecated_dir": self.deprecated_dir,
            "predictions": self.predictions,
            "protocol": self.protocol.to_dict(),
            "learner": self.learner.to_dict(
                include_signal_gates=self.schema_version >= 4,
                include_search_controls=self.schema_version >= 5,
            ),
            "evaluation": self.evaluation.to_dict(),
            "promotion": self.promotion.to_dict(),
        }
        if self.schema_version >= 2:
            value["pack_version"] = self.pack_version
            value["evidence"] = self.evidence.to_dict()
        if self.schema_version >= 3:
            value["pack_config"] = self.pack_config_dict
        if self.schema_version >= 4:
            value["signal_probe"] = self.signal_probe.to_dict()
        return value

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> RuleLoomConfig:
        raw_version = value.get("schema_version")
        if isinstance(raw_version, bool) or not isinstance(raw_version, int):
            raise ModelError("config.schema_version must be an integer")
        version_fields = {"pack_version", "evidence"} if raw_version >= 2 else set()
        if raw_version >= 3:
            version_fields.add("pack_config")
        if raw_version >= 4:
            version_fields.add("signal_probe")
        _reject_unknown(
            value,
            {
                "schema_version",
                "project",
                "target",
                "pack",
                *version_fields,
                "dataset",
                "candidates_dir",
                "shadow_dir",
                "approved_dir",
                "deprecated_dir",
                "predictions",
                "protocol",
                "learner",
                "evaluation",
                "promotion",
            },
            "config",
        )
        raw_protocol = _object(value.get("protocol", {}), "protocol")
        raw_evidence = _object(value.get("evidence", {}), "evidence")
        raw_pack_config = _object(value.get("pack_config", {}), "pack_config")
        raw_learner = _object(value.get("learner", {}), "learner")
        raw_evaluation = _object(value.get("evaluation", {}), "evaluation")
        raw_signal_probe = _object(value.get("signal_probe", {}), "signal_probe")
        raw_promotion = _object(value.get("promotion", {}), "promotion")
        if raw_version >= 2:
            _require_fields(
                value,
                {
                    "schema_version",
                    "project",
                    "target",
                    "pack",
                    "pack_version",
                    "dataset",
                    "candidates_dir",
                    "shadow_dir",
                    "approved_dir",
                    "deprecated_dir",
                    "predictions",
                    "protocol",
                    "evidence",
                    "learner",
                    "evaluation",
                    "promotion",
                    *({"pack_config"} if raw_version >= 3 else set()),
                    *({"signal_probe"} if raw_version >= 4 else set()),
                },
                f"schema-v{raw_version} config",
            )
            _require_fields(
                raw_protocol,
                {"experiment_id", "repository_id", "prediction_unit", "outcome_definition"},
                "schema-v2 protocol",
            )
            _require_fields(
                raw_evidence,
                {
                    "include_paths",
                    "exclude_paths",
                    "large_change_churn",
                    "multi_file_count",
                    "metadata_file_limit",
                },
                "schema-v2 evidence",
            )
            _require_fields(
                raw_learner,
                {
                    "engine",
                    "max_body",
                    "max_rules",
                    "allow_negation",
                    "min_precision",
                    "min_support",
                    "false_positive_cost",
                    "bootstrap_runs",
                    "max_predicates",
                    "popper_dir",
                    "popper_timeout_seconds",
                    *(
                        {
                            "gate_mode",
                            "min_lift_lower_bound",
                            "min_alert_rate",
                            "confidence_level",
                            "near_miss_limit",
                        }
                        if raw_version >= 4
                        else set()
                    ),
                    *(set(_LEGACY_SEARCH_CONTROLS) if raw_version >= 5 else set()),
                },
                "schema-v2 learner",
            )
            _require_fields(
                raw_evaluation,
                {"test_fraction", "min_train_examples", "min_test_examples", "seed"},
                "schema-v2 evaluation",
            )
            if raw_version >= 4:
                _require_fields(
                    raw_signal_probe,
                    {
                        "enabled",
                        "folds",
                        "min_train_examples",
                        "min_validation_examples",
                        "min_mcc",
                        "min_lift_lower_bound",
                        "min_alert_rate",
                        "confidence_level",
                        "tree_max_depth",
                        "max_predicates",
                    },
                    "schema-v4 signal_probe",
                )
            _require_fields(
                raw_promotion,
                {
                    "min_test_precision",
                    "min_test_recall",
                    "min_stability",
                    "require_test_set",
                    "min_positive_for_shadow",
                    "min_positive_for_approval",
                    "require_baseline_improvement",
                    "min_shadow_predictions_for_approval",
                    "min_shadow_mature_outcomes_for_approval",
                    "min_shadow_days_for_approval",
                    "min_shadow_precision",
                    "min_shadow_recall",
                    "min_shadow_mcc",
                    "min_shadow_positive_outcomes_for_approval",
                    "min_shadow_negative_outcomes_for_approval",
                    "min_shadow_matches_per_rule_for_approval",
                },
                "schema-v2 promotion",
            )
        default_pack = "flutter_testing" if raw_version == 1 else "generic_changes"
        raw_pack = _string(value.get("pack", default_pack), "pack")
        if raw_version >= 3 and raw_pack != "configured_paths" and raw_pack_config:
            raise ModelError(f"evidence pack {raw_pack!r} does not accept pack_config fields")
        raw_pack_version = value.get("pack_version", 1)
        if isinstance(raw_pack_version, bool) or not isinstance(raw_pack_version, int):
            raise ModelError("pack_version must be an integer")
        return cls(
            schema_version=raw_version,
            project=_string(value.get("project"), "project"),
            target=_string(value.get("target", "needs_extra_validation"), "target"),
            pack=raw_pack,
            pack_version=raw_pack_version,
            dataset=_string(value.get("dataset", ".ruleloom/observations.jsonl"), "dataset"),
            candidates_dir=_string(
                value.get("candidates_dir", ".ruleloom/candidates"), "candidates_dir"
            ),
            shadow_dir=_string(value.get("shadow_dir", ".ruleloom/shadow"), "shadow_dir"),
            approved_dir=_string(value.get("approved_dir", ".ruleloom/approved"), "approved_dir"),
            deprecated_dir=_string(
                value.get("deprecated_dir", ".ruleloom/deprecated"), "deprecated_dir"
            ),
            predictions=_string(
                value.get("predictions", ".ruleloom/predictions.jsonl"), "predictions"
            ),
            protocol=ProtocolConfig.from_dict(raw_protocol),
            evidence=(
                EvidenceConfig.from_dict(raw_evidence) if raw_version >= 2 else EvidenceConfig()
            ),
            pack_config=(
                ConfiguredPathsConfig.from_dict(raw_pack_config)
                if raw_version >= 3 and raw_pack == "configured_paths"
                else None
            ),
            learner=LearnerConfig.from_dict(
                raw_learner,
                include_signal_gates=raw_version >= 4,
                include_search_controls=raw_version >= 5,
            ),
            evaluation=EvaluationConfig.from_dict(raw_evaluation),
            signal_probe=(
                SignalProbeConfig.from_dict(raw_signal_probe)
                if raw_version >= 4
                else SignalProbeConfig()
            ),
            promotion=PromotionConfig.from_dict(raw_promotion),
        )

    @classmethod
    def load(cls, root: Path) -> RuleLoomConfig:
        path = _config_path(root)
        try:
            if path.stat().st_size > _MAX_CONFIG_BYTES:
                raise ModelError(f"config exceeds {_MAX_CONFIG_BYTES} bytes: {path}")
            raw: Any = strict_json_loads(path.read_text(encoding="utf-8"), str(path))
        except FileNotFoundError as exc:
            raise ModelError(f"RuleLoom is not initialized: missing {path}") from exc
        except json.JSONDecodeError as exc:
            raise ModelError(f"invalid JSON in {path}: {exc}") from exc
        except RecursionError as exc:
            raise ModelError(f"config JSON is nested too deeply: {path}") from exc
        return cls.from_dict(_object(raw, "config"))


def discover_root(start: Path | None = None) -> Path:
    """Find the nearest initialized RuleLoom project without crossing the filesystem root."""
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        if (directory / CONFIG_PATH).is_file():
            return directory
    raise ModelError(f"no {CONFIG_PATH} found from {current}")


def default_config(
    project: str,
    *,
    repository_id: str = "repository.unspecified",
    target: str = "needs_extra_validation",
    outcome_definition: str | None = None,
    pack: str | None = None,
    pack_version: int | None = None,
    pack_config: ConfiguredPathsConfig | None = None,
    schema_version: int = 2,
    test_start_at: str | None = None,
) -> RuleLoomConfig:
    selected_pack = (
        pack
        if pack is not None
        else ("flutter_testing" if schema_version == 1 else "generic_changes")
    )
    selected_version = (
        1
        if pack_version is None
        and (schema_version == 1 or (schema_version < 4 and selected_pack == "generic_changes"))
        else latest_pack_version(selected_pack)
        if pack_version is None
        else pack_version
    )
    return RuleLoomConfig(
        schema_version=schema_version,
        project=project,
        target=target,
        pack=selected_pack,
        pack_version=selected_version,
        pack_config=pack_config,
        protocol=(
            ProtocolConfig(repository_id=repository_id)
            if outcome_definition is None
            else ProtocolConfig(
                repository_id=repository_id,
                outcome_definition=outcome_definition,
            )
        ),
        learner=(
            LearnerConfig(
                gate_mode="relative_lift",
                search_strategy="beam",
                beam_width=20,
                max_predicates=64,
                predicate_ranking="logistic_weight",
                precision_estimate="wilson_lower",
                require_temporal_consistency=True,
                prune_fraction=0.2,
                permutation_runs=100,
                tree_seeds=True,
            )
            if schema_version >= 5
            else LearnerConfig(gate_mode="relative_lift")
            if schema_version >= 4
            else LearnerConfig()
        ),
        evaluation=EvaluationConfig(test_start_at=test_start_at),
        signal_probe=SignalProbeConfig(enabled=schema_version >= 4),
    )
