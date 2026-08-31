"""Core, provider-neutral RuleLoom data model."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import TypeAlias, cast

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]

SCHEMA_VERSION = 1
_PREDICATE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SUBJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_CONTENT_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class ModelError(ValueError):
    """Raised when persisted evidence violates the public schema."""


class LabelValue(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class LabelEvidence:
    """Provenance for an outcome label and the time it became observable."""

    kind: str
    available_at: str
    source: str
    reason: str = ""
    confidence: float | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"ci", "review", "incident", "human", "imported", "synthetic"}:
            raise ModelError(f"unsupported label evidence kind: {self.kind!r}")
        validate_timestamp(self.available_at)
        if not self.source:
            raise ModelError("label evidence source cannot be empty")
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ModelError("label evidence confidence must be between 0 and 1")

    def to_dict(self) -> JsonObject:
        result: JsonObject = {
            "kind": self.kind,
            "available_at": self.available_at,
            "source": self.source,
            "reason": self.reason,
        }
        if self.confidence is not None:
            result["confidence"] = self.confidence
        return result

    @classmethod
    def from_dict(cls, value: JsonObject) -> LabelEvidence:
        _reject_unknown_fields(
            value,
            {"kind", "available_at", "source", "reason", "confidence"},
            "label evidence",
        )
        raw_confidence = value.get("confidence")
        return cls(
            kind=_expect_string(value.get("kind"), "label evidence kind"),
            available_at=_expect_string(value.get("available_at"), "label evidence available_at"),
            source=_expect_string(value.get("source"), "label evidence source"),
            reason=_expect_string(value.get("reason", ""), "label evidence reason"),
            confidence=(
                None
                if raw_confidence is None
                else _expect_number(raw_confidence, "label evidence confidence")
            ),
        )


def validate_predicate(value: str, *, field_name: str = "predicate") -> str:
    if not _PREDICATE_RE.fullmatch(value):
        raise ModelError(
            f"{field_name} must start with a lowercase letter and contain only "
            "lowercase letters, numbers, and underscores: "
            f"{value!r}"
        )
    return value


def validate_subject(value: str) -> str:
    if not _SUBJECT_RE.fullmatch(value):
        raise ModelError(
            "observation id must contain only lowercase letters, numbers, dots, "
            f"underscores, and hyphens: {value!r}"
        )
    return value


def validate_timestamp(value: str) -> str:
    parse_timestamp(value)
    return value


def parse_timestamp(value: str) -> datetime:
    """Parse an aware ISO-8601 timestamp so offsets compare by actual instant."""
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ModelError(f"observed_at must be an ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ModelError("observed_at must include a timezone")
    return parsed


def canonical_json(value: JsonValue) -> str:
    validate_json_value(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def validate_json_value(value: object, field_name: str = "JSON value") -> None:
    """Reject non-JSON Python objects and non-finite numbers recursively."""
    if value is None or isinstance(value, str | bool | int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ModelError(f"{field_name} must not contain NaN or Infinity")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            validate_json_value(item, f"{field_name}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ModelError(f"{field_name} object keys must be strings")
            validate_json_value(item, f"{field_name}.{key}")
        return
    raise ModelError(f"{field_name} contains unsupported value type {type(value).__name__}")


def strict_json_loads(content: str, field_name: str = "JSON") -> JsonValue:
    """Decode standards-compliant JSON while rejecting constants and duplicate keys."""

    def reject_constant(value: str) -> None:
        raise ModelError(f"{field_name} contains invalid numeric constant {value}")

    def unique_object(pairs: list[tuple[str, JsonValue]]) -> JsonObject:
        result: JsonObject = {}
        for key, value in pairs:
            if key in result:
                raise ModelError(f"{field_name} contains duplicate object key {key!r}")
            result[key] = value
        return result

    try:
        value = cast(
            JsonValue,
            json.loads(
                content,
                parse_constant=reject_constant,
                object_pairs_hook=unique_object,
            ),
        )
    except RecursionError as exc:
        raise ModelError(f"{field_name} is nested too deeply") from exc
    validate_json_value(value, field_name)
    return value


def content_hash(value: JsonValue) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _expect_object(value: JsonValue, field_name: str) -> JsonObject:
    if not isinstance(value, dict):
        raise ModelError(f"{field_name} must be an object")
    return value


def _expect_string(value: JsonValue, field_name: str) -> str:
    if not isinstance(value, str):
        raise ModelError(f"{field_name} must be a string")
    return value


def _expect_number(value: JsonValue, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ModelError(f"{field_name} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ModelError(f"{field_name} must be finite")
    return result


def _reject_unknown_fields(value: JsonObject, allowed: set[str], field_name: str) -> None:
    unknown = set(value).difference(allowed)
    if unknown:
        raise ModelError(f"unknown {field_name} fields: {', '.join(sorted(unknown))}")


@dataclass(frozen=True, slots=True)
class FactEvidence:
    kind: str
    extractor: str
    evidence: tuple[str, ...] = ()
    confidence: float | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"deterministic", "agent", "human", "imported"}:
            raise ModelError(f"unsupported evidence kind: {self.kind!r}")
        if not self.extractor:
            raise ModelError("evidence extractor cannot be empty")
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ModelError("evidence confidence must be between 0 and 1")

    def to_dict(self) -> JsonObject:
        result: JsonObject = {
            "kind": self.kind,
            "extractor": self.extractor,
            "evidence": list(self.evidence),
        }
        if self.confidence is not None:
            result["confidence"] = self.confidence
        return result

    @classmethod
    def from_dict(cls, value: JsonObject) -> FactEvidence:
        _reject_unknown_fields(
            value,
            {"kind", "extractor", "evidence", "confidence"},
            "fact evidence",
        )
        raw_evidence = value.get("evidence", [])
        if not isinstance(raw_evidence, list) or not all(
            isinstance(item, str) for item in raw_evidence
        ):
            raise ModelError("fact evidence must be an array of strings")
        raw_confidence = value.get("confidence")
        confidence = (
            None
            if raw_confidence is None
            else _expect_number(raw_confidence, "fact evidence confidence")
        )
        return cls(
            kind=_expect_string(value.get("kind"), "fact evidence kind"),
            extractor=_expect_string(value.get("extractor"), "fact evidence extractor"),
            evidence=tuple(cast(list[str], raw_evidence)),
            confidence=confidence,
        )


@dataclass(frozen=True, slots=True)
class Observation:
    id: str
    observed_at: str
    protocol_hash: str
    facts: frozenset[str]
    labels: dict[str, LabelValue] = field(default_factory=dict)
    label_evidence: dict[str, LabelEvidence] = field(default_factory=dict)
    fact_evidence: dict[str, FactEvidence] = field(default_factory=dict)
    source: JsonObject = field(default_factory=dict)
    metadata: JsonObject = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ModelError(
                f"unsupported observation schema version {self.schema_version}; "
                f"expected {SCHEMA_VERSION}"
            )
        validate_subject(self.id)
        validate_timestamp(self.observed_at)
        if not _CONTENT_HASH_RE.fullmatch(self.protocol_hash):
            raise ModelError("observation protocol_hash must be a lowercase SHA-256 digest")
        for predicate in self.facts:
            validate_predicate(predicate, field_name="fact")
        for target in self.labels:
            validate_predicate(target, field_name="label target")
        unknown_label_evidence = set(self.label_evidence).difference(self.labels)
        if unknown_label_evidence:
            unknown = ", ".join(sorted(unknown_label_evidence))
            raise ModelError(f"label_evidence references absent labels: {unknown}")
        missing_mature_evidence = {
            target
            for target, label in self.labels.items()
            if label is not LabelValue.UNKNOWN and target not in self.label_evidence
        }
        if missing_mature_evidence:
            missing = ", ".join(sorted(missing_mature_evidence))
            raise ModelError(f"mature labels require label_evidence: {missing}")
        observed_at = parse_timestamp(self.observed_at)
        for target, evidence in self.label_evidence.items():
            if parse_timestamp(evidence.available_at) < observed_at:
                raise ModelError(f"label evidence for {target!r} cannot predate observation time")
        unknown_evidence = set(self.fact_evidence).difference(self.facts)
        if unknown_evidence:
            unknown = ", ".join(sorted(unknown_evidence))
            raise ModelError(f"fact_evidence references absent facts: {unknown}")
        validate_json_value(self.source, "observation source")
        validate_json_value(self.metadata, "observation metadata")

    def with_label(
        self, target: str, value: LabelValue, evidence: LabelEvidence | None = None
    ) -> Observation:
        validate_predicate(target, field_name="label target")
        labels = dict(self.labels)
        labels[target] = value
        label_evidence = dict(self.label_evidence)
        if evidence is None:
            label_evidence.pop(target, None)
        else:
            label_evidence[target] = evidence
        return replace(self, labels=labels, label_evidence=label_evidence)

    def to_dict(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "observed_at": self.observed_at,
            "protocol_hash": self.protocol_hash,
            "facts": cast(JsonValue, sorted(self.facts)),
            "labels": {key: self.labels[key].value for key in sorted(self.labels)},
            "label_evidence": {
                key: self.label_evidence[key].to_dict() for key in sorted(self.label_evidence)
            },
            "fact_evidence": {
                key: self.fact_evidence[key].to_dict() for key in sorted(self.fact_evidence)
            },
            "source": self.source,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, value: JsonObject) -> Observation:
        _reject_unknown_fields(
            value,
            {
                "schema_version",
                "id",
                "observed_at",
                "protocol_hash",
                "facts",
                "labels",
                "label_evidence",
                "fact_evidence",
                "source",
                "metadata",
            },
            "observation",
        )
        raw_facts = value.get("facts")
        if not isinstance(raw_facts, list) or not all(isinstance(item, str) for item in raw_facts):
            raise ModelError("observation facts must be an array of strings")

        raw_labels = _expect_object(value.get("labels", {}), "observation labels")
        labels: dict[str, LabelValue] = {}
        for target, raw_label in raw_labels.items():
            label_text = _expect_string(raw_label, f"label {target}")
            try:
                labels[target] = LabelValue(label_text)
            except ValueError as exc:
                raise ModelError(f"unsupported label value for {target}: {label_text!r}") from exc

        raw_evidence = _expect_object(value.get("fact_evidence", {}), "observation fact_evidence")
        fact_evidence = {
            predicate: FactEvidence.from_dict(_expect_object(item, f"evidence for {predicate}"))
            for predicate, item in raw_evidence.items()
        }

        raw_label_evidence = _expect_object(
            value.get("label_evidence", {}), "observation label_evidence"
        )
        label_evidence = {
            target: LabelEvidence.from_dict(_expect_object(item, f"label evidence for {target}"))
            for target, item in raw_label_evidence.items()
        }

        raw_version = value.get("schema_version")
        if isinstance(raw_version, bool) or not isinstance(raw_version, int):
            raise ModelError("observation schema_version must be an integer")
        return cls(
            schema_version=raw_version,
            id=_expect_string(value.get("id"), "observation id"),
            observed_at=_expect_string(value.get("observed_at"), "observation observed_at"),
            protocol_hash=_expect_string(value.get("protocol_hash"), "observation protocol_hash"),
            facts=frozenset(cast(list[str], raw_facts)),
            labels=labels,
            label_evidence=label_evidence,
            fact_evidence=fact_evidence,
            source=_expect_object(value.get("source", {}), "observation source"),
            metadata=_expect_object(value.get("metadata", {}), "observation metadata"),
        )


@dataclass(frozen=True, slots=True, order=True)
class RuleLiteral:
    predicate: str
    negated: bool = False

    def __post_init__(self) -> None:
        validate_predicate(self.predicate)

    @property
    def name(self) -> str:
        return f"not_{self.predicate}" if self.negated else self.predicate

    def matches(self, facts: frozenset[str]) -> bool:
        present = self.predicate in facts
        return not present if self.negated else present

    def to_dict(self) -> JsonObject:
        return {"predicate": self.predicate, "negated": self.negated}

    @classmethod
    def from_dict(cls, value: JsonObject) -> RuleLiteral:
        _reject_unknown_fields(value, {"predicate", "negated"}, "literal")
        raw_negated = value.get("negated", False)
        if not isinstance(raw_negated, bool):
            raise ModelError("literal negated must be a boolean")
        return cls(
            predicate=_expect_string(value.get("predicate"), "literal predicate"),
            negated=raw_negated,
        )


@dataclass(frozen=True, slots=True)
class HornClause:
    target: str
    body: tuple[RuleLiteral, ...]

    def __post_init__(self) -> None:
        validate_predicate(self.target, field_name="rule target")
        if not self.body:
            raise ModelError("a learned rule must contain at least one body literal")
        if len(set(self.body)) != len(self.body):
            raise ModelError("a learned rule cannot contain duplicate literals")
        predicates = {literal.predicate for literal in self.body}
        if len(predicates) != len(self.body):
            raise ModelError("a learned rule cannot contain both forms of the same predicate")

    @property
    def signature(self) -> str:
        body = ",".join(literal.name for literal in self.body)
        return f"{self.target}:-{body}"

    def matches(self, facts: frozenset[str]) -> bool:
        return all(literal.matches(facts) for literal in self.body)

    def to_prolog(self, variable: str = "A") -> str:
        body = ", ".join(f"{literal.name}({variable})" for literal in self.body)
        return f"{self.target}({variable}) :- {body}."

    def to_dict(self) -> JsonObject:
        return {"target": self.target, "body": [literal.to_dict() for literal in self.body]}

    @classmethod
    def from_dict(cls, value: JsonObject) -> HornClause:
        _reject_unknown_fields(value, {"target", "body"}, "rule")
        raw_body = value.get("body")
        if not isinstance(raw_body, list):
            raise ModelError("rule body must be an array")
        return cls(
            target=_expect_string(value.get("target"), "rule target"),
            body=tuple(
                RuleLiteral.from_dict(_expect_object(item, "rule literal")) for item in raw_body
            ),
        )


@dataclass(frozen=True, slots=True)
class RuleSet:
    target: str
    clauses: tuple[HornClause, ...]

    def __post_init__(self) -> None:
        validate_predicate(self.target, field_name="rule-set target")
        if any(clause.target != self.target for clause in self.clauses):
            raise ModelError("all clauses in a rule set must share its target")

    @property
    def signatures(self) -> frozenset[str]:
        return frozenset(clause.signature for clause in self.clauses)

    def predicts(self, facts: frozenset[str]) -> bool:
        return any(clause.matches(facts) for clause in self.clauses)

    def to_dict(self) -> JsonObject:
        return {"target": self.target, "clauses": [clause.to_dict() for clause in self.clauses]}

    @classmethod
    def from_dict(cls, value: JsonObject) -> RuleSet:
        _reject_unknown_fields(value, {"target", "clauses"}, "rule set")
        raw_clauses = value.get("clauses")
        if not isinstance(raw_clauses, list):
            raise ModelError("rule-set clauses must be an array")
        return cls(
            target=_expect_string(value.get("target"), "rule-set target"),
            clauses=tuple(
                HornClause.from_dict(_expect_object(item, "rule-set clause"))
                for item in raw_clauses
            ),
        )


@dataclass(frozen=True, slots=True)
class Metrics:
    true_positive: int
    false_positive: int
    true_negative: int
    false_negative: int
    precision: float
    recall: float
    f1: float
    accuracy: float
    balanced_accuracy: float
    matthews_correlation: float
    prevalence: float
    predicted_positive_rate: float

    def __post_init__(self) -> None:
        counts = {
            "true_positive": self.true_positive,
            "false_positive": self.false_positive,
            "true_negative": self.true_negative,
            "false_negative": self.false_negative,
        }
        for name, value in counts.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ModelError(f"metric {name} must be a non-negative integer")

        tp, fp, tn, fn = (
            self.true_positive,
            self.false_positive,
            self.true_negative,
            self.false_negative,
        )
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        specificity = tn / (tn + fp) if tn + fp else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        total = tp + fp + tn + fn
        denominator = math.sqrt((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn))
        expected = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "accuracy": (tp + tn) / total if total else 0.0,
            "balanced_accuracy": (recall + specificity) / 2,
            "matthews_correlation": (tp * tn - fp * fn) / denominator if denominator else 0.0,
            "prevalence": (tp + fn) / total if total else 0.0,
            "predicted_positive_rate": (tp + fp) / total if total else 0.0,
        }
        for name, expected_value in expected.items():
            actual = getattr(self, name)
            if not math.isfinite(actual) or not math.isclose(
                actual, expected_value, rel_tol=1e-12, abs_tol=1e-12
            ):
                raise ModelError(
                    f"metric {name}={actual!r} is inconsistent with confusion counts; "
                    f"expected {expected_value!r}"
                )

    @classmethod
    def from_counts(cls, tp: int, fp: int, tn: int, fn: int) -> Metrics:
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        specificity = tn / (tn + fp) if tn + fp else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        total = tp + fp + tn + fn
        mcc_denominator = math.sqrt((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn))
        return cls(
            true_positive=tp,
            false_positive=fp,
            true_negative=tn,
            false_negative=fn,
            precision=precision,
            recall=recall,
            f1=f1,
            accuracy=(tp + tn) / total if total else 0.0,
            balanced_accuracy=(recall + specificity) / 2,
            matthews_correlation=(tp * tn - fp * fn) / mcc_denominator if mcc_denominator else 0.0,
            prevalence=(tp + fn) / total if total else 0.0,
            predicted_positive_rate=(tp + fp) / total if total else 0.0,
        )

    def to_dict(self) -> JsonObject:
        return {
            "true_positive": self.true_positive,
            "false_positive": self.false_positive,
            "true_negative": self.true_negative,
            "false_negative": self.false_negative,
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
            "accuracy": self.accuracy,
            "balanced_accuracy": self.balanced_accuracy,
            "matthews_correlation": self.matthews_correlation,
            "prevalence": self.prevalence,
            "predicted_positive_rate": self.predicted_positive_rate,
        }

    @classmethod
    def from_dict(cls, value: JsonObject) -> Metrics:
        _reject_unknown_fields(
            value,
            {
                "true_positive",
                "false_positive",
                "true_negative",
                "false_negative",
                "precision",
                "recall",
                "f1",
                "accuracy",
                "balanced_accuracy",
                "matthews_correlation",
                "prevalence",
                "predicted_positive_rate",
            },
            "metric",
        )

        def integer(name: str) -> int:
            raw = value.get(name)
            if isinstance(raw, bool) or not isinstance(raw, int):
                raise ModelError(f"metric {name} must be an integer")
            return raw

        return cls(
            true_positive=integer("true_positive"),
            false_positive=integer("false_positive"),
            true_negative=integer("true_negative"),
            false_negative=integer("false_negative"),
            precision=_expect_number(value.get("precision"), "metric precision"),
            recall=_expect_number(value.get("recall"), "metric recall"),
            f1=_expect_number(value.get("f1"), "metric f1"),
            accuracy=_expect_number(value.get("accuracy"), "metric accuracy"),
            balanced_accuracy=_expect_number(
                value.get("balanced_accuracy"), "metric balanced_accuracy"
            ),
            matthews_correlation=_expect_number(
                value.get("matthews_correlation"), "metric matthews_correlation"
            ),
            prevalence=_expect_number(value.get("prevalence"), "metric prevalence"),
            predicted_positive_rate=_expect_number(
                value.get("predicted_positive_rate"), "metric predicted_positive_rate"
            ),
        )


@dataclass(frozen=True, slots=True)
class Candidate:
    id: str
    created_at: str
    engine: str
    engine_version: str
    dataset_hash: str
    config_hash: str
    rules: RuleSet
    metrics: dict[str, Metrics]
    baselines: dict[str, Metrics]
    stability: float
    train_ids: tuple[str, ...]
    test_ids: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    metadata: JsonObject = field(default_factory=dict)
    review: JsonObject = field(default_factory=dict)
    status: str = "candidate"
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ModelError("unsupported candidate schema version")
        if self.status not in {"candidate", "shadow", "approved", "rejected", "deprecated"}:
            raise ModelError(f"unsupported candidate status: {self.status!r}")
        if not 0 <= self.stability <= 1:
            raise ModelError("candidate stability must be between 0 and 1")
        validate_subject(self.id)
        validate_timestamp(self.created_at)
        if self.engine not in {"horn", "popper"}:
            raise ModelError(f"unsupported candidate engine: {self.engine!r}")
        if not self.engine_version:
            raise ModelError("candidate engine_version cannot be empty")
        if not _CONTENT_HASH_RE.fullmatch(self.dataset_hash):
            raise ModelError("candidate dataset_hash must be a lowercase SHA-256 digest")
        if not _CONTENT_HASH_RE.fullmatch(self.config_hash):
            raise ModelError("candidate config_hash must be a lowercase SHA-256 digest")
        for item_id in (*self.train_ids, *self.test_ids):
            validate_subject(item_id)
        if len(set(self.train_ids)) != len(self.train_ids):
            raise ModelError("candidate train_ids cannot contain duplicates")
        if len(set(self.test_ids)) != len(self.test_ids):
            raise ModelError("candidate test_ids cannot contain duplicates")
        if set(self.train_ids) & set(self.test_ids):
            raise ModelError("candidate train_ids and test_ids must be disjoint")
        validate_json_value(self.metadata, "candidate metadata")
        validate_json_value(self.review, "candidate review")

    def identity_payload(self) -> JsonObject:
        """Return every immutable semantic field bound by the candidate id."""
        return {
            "schema_version": self.schema_version,
            "engine": self.engine,
            "engine_version": self.engine_version,
            "dataset_hash": self.dataset_hash,
            "config_hash": self.config_hash,
            "rules": self.rules.to_dict(),
            "metrics": {key: self.metrics[key].to_dict() for key in sorted(self.metrics)},
            "baselines": {key: self.baselines[key].to_dict() for key in sorted(self.baselines)},
            "stability": self.stability,
            "train_ids": list(self.train_ids),
            "test_ids": list(self.test_ids),
            "warnings": list(self.warnings),
            "metadata": self.metadata,
        }

    @property
    def expected_id(self) -> str:
        return f"cand-{content_hash(self.identity_payload())[:16]}"

    def with_identity(self) -> Candidate:
        return replace(self, id=self.expected_id)

    def validate_identity(self) -> None:
        if self.id != self.expected_id:
            raise ModelError(
                f"candidate id {self.id!r} does not match content identity {self.expected_id!r}"
            )

    def to_dict(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "created_at": self.created_at,
            "status": self.status,
            "engine": self.engine,
            "engine_version": self.engine_version,
            "dataset_hash": self.dataset_hash,
            "config_hash": self.config_hash,
            "rules": self.rules.to_dict(),
            "metrics": {key: self.metrics[key].to_dict() for key in sorted(self.metrics)},
            "baselines": {key: self.baselines[key].to_dict() for key in sorted(self.baselines)},
            "stability": self.stability,
            "train_ids": list(self.train_ids),
            "test_ids": list(self.test_ids),
            "warnings": list(self.warnings),
            "metadata": self.metadata,
            "review": self.review,
        }

    @classmethod
    def from_dict(cls, value: JsonObject) -> Candidate:
        _reject_unknown_fields(
            value,
            {
                "schema_version",
                "id",
                "created_at",
                "status",
                "engine",
                "engine_version",
                "dataset_hash",
                "config_hash",
                "rules",
                "metrics",
                "baselines",
                "stability",
                "train_ids",
                "test_ids",
                "warnings",
                "metadata",
                "review",
            },
            "candidate",
        )
        raw_metrics = _expect_object(value.get("metrics"), "candidate metrics")
        metrics = {
            key: Metrics.from_dict(_expect_object(item, f"candidate metric {key}"))
            for key, item in raw_metrics.items()
        }
        raw_baselines = _expect_object(value.get("baselines", {}), "candidate baselines")
        baselines = {
            key: Metrics.from_dict(_expect_object(item, f"candidate baseline {key}"))
            for key, item in raw_baselines.items()
        }

        def string_tuple(name: str) -> tuple[str, ...]:
            raw = value.get(name, [])
            if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
                raise ModelError(f"candidate {name} must be an array of strings")
            return tuple(cast(list[str], raw))

        raw_version = value.get("schema_version")
        if isinstance(raw_version, bool) or not isinstance(raw_version, int):
            raise ModelError("candidate schema_version must be an integer")
        return cls(
            schema_version=raw_version,
            id=_expect_string(value.get("id"), "candidate id"),
            created_at=_expect_string(value.get("created_at"), "candidate created_at"),
            status=_expect_string(value.get("status"), "candidate status"),
            engine=_expect_string(value.get("engine"), "candidate engine"),
            engine_version=_expect_string(value.get("engine_version"), "candidate engine_version"),
            dataset_hash=_expect_string(value.get("dataset_hash"), "candidate dataset_hash"),
            config_hash=_expect_string(value.get("config_hash"), "candidate config_hash"),
            rules=RuleSet.from_dict(_expect_object(value.get("rules"), "candidate rules")),
            metrics=metrics,
            baselines=baselines,
            stability=_expect_number(value.get("stability"), "candidate stability"),
            train_ids=string_tuple("train_ids"),
            test_ids=string_tuple("test_ids"),
            warnings=string_tuple("warnings"),
            metadata=_expect_object(value.get("metadata", {}), "candidate metadata"),
            review=_expect_object(value.get("review", {}), "candidate review"),
        )


@dataclass(frozen=True, slots=True)
class Prediction:
    """An immutable shadow/advisory decision captured before its later outcome."""

    id: str
    predicted_at: str
    observation: Observation
    target: str
    unit_id: str
    protocol_hash: str
    protocol: JsonObject
    policy_set_hash: str
    policies: tuple[JsonObject, ...]
    matches: tuple[JsonObject, ...]
    abstained: bool
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ModelError("unsupported prediction schema version")
        validate_subject(self.id)
        validate_timestamp(self.predicted_at)
        validate_predicate(self.target, field_name="prediction target")
        if self.target not in self.observation.labels:
            raise ModelError(f"prediction target {self.target!r} is absent from observation labels")
        if self.observation.labels[self.target] is not LabelValue.UNKNOWN:
            raise ModelError("prediction snapshots must not contain a mature target outcome")
        if self.target in self.observation.label_evidence:
            raise ModelError("prediction snapshots must not contain target label evidence")
        if parse_timestamp(self.predicted_at) < parse_timestamp(self.observation.observed_at):
            raise ModelError("prediction time cannot precede observation time")
        validate_subject(self.unit_id)
        if self.observation.source.get("change_id") != self.unit_id:
            raise ModelError("prediction unit_id does not match observation source change_id")
        _reject_unknown_fields(
            self.protocol,
            {
                "experiment_id",
                "repository_id",
                "observation_unit",
                "outcome_definition",
                "target",
                "pack",
                "extractor",
                "config_hash",
                "evidence_protocol_hash",
            },
            "prediction protocol",
        )
        if set(self.protocol) != {
            "experiment_id",
            "repository_id",
            "observation_unit",
            "outcome_definition",
            "target",
            "pack",
            "extractor",
            "config_hash",
            "evidence_protocol_hash",
        }:
            raise ModelError("prediction protocol snapshot is incomplete")
        validate_subject(
            _expect_string(self.protocol.get("experiment_id"), "protocol experiment_id")
        )
        repository_id = validate_subject(
            _expect_string(self.protocol.get("repository_id"), "protocol repository_id")
        )
        if self.observation.source.get("repository") != repository_id:
            raise ModelError("prediction protocol repository does not match its observation source")
        observation_unit = _expect_string(
            self.protocol.get("observation_unit"), "protocol observation_unit"
        )
        if observation_unit not in {"git_commit", "git_range", "git_worktree"}:
            raise ModelError(f"unsupported prediction observation unit: {observation_unit!r}")
        if self.observation.source.get("kind") != observation_unit:
            raise ModelError("prediction protocol unit does not match its observation source")
        if _expect_string(self.protocol.get("target"), "protocol target") != self.target:
            raise ModelError("prediction protocol target does not match prediction target")
        if self.observation.source.get("pack") != _expect_string(
            self.protocol.get("pack"), "protocol pack"
        ):
            raise ModelError("prediction protocol pack does not match its observation source")
        if self.observation.source.get("extractor") != _expect_string(
            self.protocol.get("extractor"), "protocol extractor"
        ):
            raise ModelError("prediction protocol extractor does not match its observation source")
        if not _expect_string(
            self.protocol.get("outcome_definition"), "protocol outcome_definition"
        ).strip():
            raise ModelError("protocol outcome_definition cannot be blank")
        config_hash = _expect_string(self.protocol.get("config_hash"), "protocol config_hash")
        if len(config_hash) != 64:
            raise ModelError("protocol config_hash must contain 64 characters")
        evidence_protocol_hash = _expect_string(
            self.protocol.get("evidence_protocol_hash"), "protocol evidence_protocol_hash"
        )
        if evidence_protocol_hash != self.observation.protocol_hash:
            raise ModelError("prediction evidence protocol does not match its observation protocol")
        expected_protocol_hash = content_hash(self.protocol)
        if self.protocol_hash != expected_protocol_hash:
            raise ModelError("prediction protocol_hash does not match its protocol snapshot")
        policy_statuses: dict[str, str] = {}
        policy_signatures: dict[str, frozenset[str]] = {}
        validate_json_value(list(self.policies), "prediction policies")
        validate_json_value(list(self.matches), "prediction matches")
        for policy in self.policies:
            _reject_unknown_fields(
                policy,
                {"candidate_id", "status", "target", "manifest_hash", "rule_signatures"},
                "prediction policy",
            )
            candidate_id = validate_subject(
                _expect_string(policy.get("candidate_id"), "policy candidate_id")
            )
            status = _expect_string(policy.get("status"), "policy status")
            if status not in {"shadow", "approved"}:
                raise ModelError(f"unsupported prediction policy status: {status!r}")
            policy_target = _expect_string(policy.get("target"), "policy target")
            if policy_target != self.target:
                raise ModelError(f"prediction policy target {policy_target!r} != {self.target!r}")
            if not _expect_string(policy.get("manifest_hash"), "policy manifest_hash"):
                raise ModelError("policy manifest_hash cannot be empty")
            raw_signatures = policy.get("rule_signatures")
            if not isinstance(raw_signatures, list) or not all(
                isinstance(item, str) and item for item in raw_signatures
            ):
                raise ModelError("policy rule_signatures must be an array of strings")
            signatures = cast(list[str], raw_signatures)
            if len(signatures) != len(set(signatures)):
                raise ModelError("policy rule_signatures cannot contain duplicates")
            if candidate_id in policy_statuses:
                raise ModelError(f"duplicate prediction policy candidate id: {candidate_id}")
            policy_statuses[candidate_id] = status
            policy_signatures[candidate_id] = frozenset(signatures)
        expected_policy_hash = content_hash(
            {
                "protocol_hash": self.protocol_hash,
                "target": self.target,
                "policies": cast(JsonValue, list(self.policies)),
            }
        )
        if self.policy_set_hash != expected_policy_hash:
            raise ModelError("prediction policy_set_hash does not match its policy snapshot")
        if self.abstained != (len(self.matches) == 0):
            raise ModelError("prediction abstained must agree with its matches")
        match_keys: set[tuple[str, str]] = set()
        for match in self.matches:
            _reject_unknown_fields(
                match,
                {"candidate_id", "status", "rule", "prolog"},
                "prediction match",
            )
            candidate_id = validate_subject(
                _expect_string(match.get("candidate_id"), "match candidate_id")
            )
            status = _expect_string(match.get("status"), "match status")
            if status not in {"shadow", "approved"}:
                raise ModelError(f"unsupported prediction match status: {status!r}")
            clause = HornClause.from_dict(_expect_object(match.get("rule"), "match rule"))
            if clause.target != self.target:
                raise ModelError(f"prediction match target {clause.target!r} != {self.target!r}")
            if policy_statuses.get(candidate_id) != status:
                raise ModelError(
                    f"prediction match {candidate_id} is absent from its policy snapshot"
                )
            if clause.signature not in policy_signatures[candidate_id]:
                raise ModelError("prediction match rule is absent from its policy snapshot")
            if not clause.matches(self.observation.facts):
                raise ModelError("prediction match rule does not match observation facts")
            match_key = (candidate_id, clause.signature)
            if match_key in match_keys:
                raise ModelError("prediction contains a duplicate candidate/rule match")
            match_keys.add(match_key)
            prolog = _expect_string(match.get("prolog"), "match prolog")
            if prolog != clause.to_prolog():
                raise ModelError("prediction match prolog does not match its structured rule")

    def identity_payload(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "predicted_at": self.predicted_at,
            "observation": self.observation.to_dict(),
            "target": self.target,
            "unit_id": self.unit_id,
            "protocol_hash": self.protocol_hash,
            "protocol": self.protocol,
            "policy_set_hash": self.policy_set_hash,
            "policies": list(self.policies),
            "matches": list(self.matches),
            "abstained": self.abstained,
        }

    @property
    def expected_id(self) -> str:
        return f"prediction.{content_hash(self.identity_payload())[:20]}"

    def with_identity(self) -> Prediction:
        return replace(self, id=self.expected_id)

    def validate_identity(self) -> None:
        if self.id != self.expected_id:
            raise ModelError(
                f"prediction id {self.id!r} does not match content identity {self.expected_id!r}"
            )

    def to_dict(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "predicted_at": self.predicted_at,
            "observation": self.observation.to_dict(),
            "target": self.target,
            "unit_id": self.unit_id,
            "protocol_hash": self.protocol_hash,
            "protocol": self.protocol,
            "policy_set_hash": self.policy_set_hash,
            "policies": list(self.policies),
            "matches": list(self.matches),
            "abstained": self.abstained,
        }

    @classmethod
    def from_dict(cls, value: JsonObject) -> Prediction:
        _reject_unknown_fields(
            value,
            {
                "schema_version",
                "id",
                "predicted_at",
                "observation",
                "target",
                "unit_id",
                "protocol_hash",
                "protocol",
                "policy_set_hash",
                "policies",
                "matches",
                "abstained",
            },
            "prediction",
        )
        raw_matches = value.get("matches")
        if not isinstance(raw_matches, list):
            raise ModelError("prediction matches must be an array")
        matches = tuple(_expect_object(item, "prediction match") for item in raw_matches)
        raw_policies = value.get("policies")
        if not isinstance(raw_policies, list):
            raise ModelError("prediction policies must be an array")
        policies = tuple(_expect_object(item, "prediction policy") for item in raw_policies)
        raw_abstained = value.get("abstained")
        if not isinstance(raw_abstained, bool):
            raise ModelError("prediction abstained must be a boolean")
        raw_version = value.get("schema_version")
        if isinstance(raw_version, bool) or not isinstance(raw_version, int):
            raise ModelError("prediction schema_version must be an integer")
        return cls(
            schema_version=raw_version,
            id=_expect_string(value.get("id"), "prediction id"),
            predicted_at=_expect_string(value.get("predicted_at"), "prediction predicted_at"),
            observation=Observation.from_dict(
                _expect_object(value.get("observation"), "prediction observation")
            ),
            target=_expect_string(value.get("target"), "prediction target"),
            unit_id=_expect_string(value.get("unit_id"), "prediction unit_id"),
            protocol_hash=_expect_string(value.get("protocol_hash"), "prediction protocol_hash"),
            protocol=_expect_object(value.get("protocol"), "prediction protocol"),
            policy_set_hash=_expect_string(
                value.get("policy_set_hash"), "prediction policy_set_hash"
            ),
            policies=policies,
            matches=matches,
            abstained=raw_abstained,
        )


def validate_prediction_cohort(
    predictions: Sequence[Prediction],
    *,
    expected_protocol_hash: str | None = None,
    expected_observation_unit: str | None = None,
    expected_repository_id: str | None = None,
    require_one_policy_set: bool = True,
) -> None:
    """Reject pooling across policy, experiment, repository, or observation-unit contracts."""
    protocol_hashes = {item.protocol_hash for item in predictions}
    policy_sets = {item.policy_set_hash for item in predictions}
    units = {cast(str, item.protocol["observation_unit"]) for item in predictions}
    repositories = {cast(str, item.protocol["repository_id"]) for item in predictions}
    if len(protocol_hashes) > 1:
        raise ModelError("prediction cohort mixes prospective protocol hashes")
    if require_one_policy_set and len(policy_sets) > 1:
        raise ModelError("prediction cohort mixes policy sets")
    if len(units) > 1:
        raise ModelError("prediction cohort mixes observation units: " + ", ".join(sorted(units)))
    if len(repositories) > 1:
        raise ModelError("prediction cohort mixes repositories")
    if expected_protocol_hash is not None and protocol_hashes not in (
        set(),
        {expected_protocol_hash},
    ):
        raise ModelError("prediction cohort does not match the configured prospective protocol")
    if expected_observation_unit is not None and units not in (
        set(),
        {expected_observation_unit},
    ):
        raise ModelError("prediction cohort does not match the configured observation unit")
    if expected_repository_id is not None and repositories not in (
        set(),
        {expected_repository_id},
    ):
        raise ModelError("prediction cohort does not match the configured repository")
