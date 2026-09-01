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
        if not 1 <= self.max_predicates <= 32:
            raise ModelError("max_predicates must be between 1 and 32")
        if not 1 <= self.popper_timeout_seconds <= 3600:
            raise ModelError("popper_timeout_seconds must be between 1 and 3600")
        hypotheses = self.hypothesis_count()
        if hypotheses > 250_000 or hypotheses * (self.bootstrap_runs + 1) > 5_000_000:
            raise ModelError(
                "learner search budget is too large; reduce max_body, max_predicates, "
                "or bootstrap_runs"
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
            if self.popper_dir is None or not Path(self.popper_dir).is_absolute():
                raise ModelError(
                    "learner.popper_dir must be an explicit absolute path when engine='popper'"
                )

    def hypothesis_count(self, predicate_count: int | None = None) -> int:
        """Return the bounded number of clause bodies considered by the Horn search."""
        predicates = self.max_predicates if predicate_count is None else predicate_count
        if isinstance(predicates, bool) or not 0 <= predicates <= self.max_predicates:
            raise ModelError("predicate_count must be between 0 and learner.max_predicates")
        literal_variants = 2 if self.allow_negation else 1
        return sum(
            comb(predicates, size) * literal_variants**size
            for size in range(1, min(self.max_body, predicates) + 1)
        )

    def to_dict(self) -> JsonObject:
        return {
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

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> LearnerConfig:
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
        )


@dataclass(frozen=True, slots=True)
class EvaluationConfig:
    test_fraction: float = 0.25
    min_train_examples: int = 8
    min_test_examples: int = 4
    seed: int = 17

    def __post_init__(self) -> None:
        if not math.isfinite(self.test_fraction) or not 0 < self.test_fraction < 1:
            raise ModelError("evaluation.test_fraction must be strictly between 0 and 1")
        if self.min_train_examples < 2 or self.min_test_examples < 1:
            raise ModelError("evaluation minimum sizes are invalid")

    def to_dict(self) -> JsonObject:
        return {
            "test_fraction": self.test_fraction,
            "min_train_examples": self.min_train_examples,
            "min_test_examples": self.min_test_examples,
            "seed": self.seed,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> EvaluationConfig:
        _reject_unknown(
            value,
            {"test_fraction", "min_train_examples", "min_test_examples", "seed"},
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
        if self.prediction_unit not in {"git_commit", "git_range", "git_worktree"}:
            raise ModelError(
                "protocol.prediction_unit must be git_commit, git_range, or git_worktree"
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
    pack: str = "flutter_testing"
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
    schema_version: int = 1
    pack_config: ConfiguredPathsConfig | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version not in {1, 2, 3}
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
            "learner": self.learner.to_dict(),
            "evaluation": self.evaluation.to_dict(),
            "promotion": self.promotion.to_dict(),
        }
        if self.schema_version >= 2:
            value["pack_version"] = self.pack_version
            value["evidence"] = self.evidence.to_dict()
        if self.schema_version >= 3:
            value["pack_config"] = self.pack_config_dict
        return value

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> RuleLoomConfig:
        raw_version = value.get("schema_version")
        if isinstance(raw_version, bool) or not isinstance(raw_version, int):
            raise ModelError("config.schema_version must be an integer")
        version_fields = {"pack_version", "evidence"} if raw_version >= 2 else set()
        if raw_version >= 3:
            version_fields.add("pack_config")
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
                },
                "schema-v2 learner",
            )
            _require_fields(
                raw_evaluation,
                {"test_fraction", "min_train_examples", "min_test_examples", "seed"},
                "schema-v2 evaluation",
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
            learner=LearnerConfig.from_dict(raw_learner),
            evaluation=EvaluationConfig.from_dict(raw_evaluation),
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
) -> RuleLoomConfig:
    selected_pack = (
        pack
        if pack is not None
        else ("flutter_testing" if schema_version == 1 else "generic_changes")
    )
    selected_version = (
        1
        if schema_version == 1 and pack_version is None
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
    )
