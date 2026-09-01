"""Provider- and language-neutral records for historical bootstrap evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, cast

from ruleloom.models import (
    JsonObject,
    JsonValue,
    ModelError,
    parse_timestamp,
    validate_json_value,
    validate_subject,
)

HISTORY_SCHEMA_VERSION = 1

EvidenceQuality = Literal["rich", "git_only", "final_only"]

_GIT_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_EVIDENCE_QUALITIES = frozenset({"rich", "git_only", "final_only"})


def _expect_object(value: JsonValue, field_name: str) -> JsonObject:
    if not isinstance(value, dict):
        raise ModelError(f"{field_name} must be an object")
    return value


def _expect_string(value: JsonValue, field_name: str) -> str:
    if not isinstance(value, str):
        raise ModelError(f"{field_name} must be a string")
    return value


def _expect_optional_string(value: JsonValue, field_name: str) -> str | None:
    if value is None:
        return None
    return _expect_string(value, field_name)


def _expect_string_tuple(value: JsonValue, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ModelError(f"{field_name} must be an array of strings")
    return tuple(cast(list[str], value))


def _expect_schema_version(value: JsonValue, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ModelError(f"{field_name} must be an integer")
    return value


def _reject_unknown_fields(value: JsonObject, allowed: frozenset[str], field_name: str) -> None:
    unknown = set(value).difference(allowed)
    if unknown:
        raise ModelError(f"unknown {field_name} fields: {', '.join(sorted(unknown))}")


def _validate_identifier(value: str, field_name: str) -> str:
    try:
        return validate_subject(value)
    except ModelError as exc:
        raise ModelError(f"invalid {field_name}: {value!r}") from exc


def _validate_reference(value: str, field_name: str) -> str:
    validate_json_value(value, field_name)
    if not value.strip():
        raise ModelError(f"{field_name} cannot be empty")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ModelError(f"{field_name} cannot contain control characters")
    return value


def validate_git_sha(value: str, *, field_name: str = "Git SHA") -> str:
    """Validate SHA-1 and SHA-256 object identifiers emitted by Git."""
    if not _GIT_SHA_RE.fullmatch(value):
        raise ModelError(f"{field_name} must be a lowercase 40- or 64-character Git SHA")
    return value


def _validate_unique(values: tuple[str, ...], field_name: str) -> None:
    if len(values) != len(set(values)):
        raise ModelError(f"{field_name} cannot contain duplicate values")


@dataclass(frozen=True, slots=True)
class HistoricalEvent:
    """An immutable event and the time at which a learner could observe it."""

    id: str
    repository_id: str
    kind: str
    occurred_at: str
    available_at: str
    provider: str
    source_ref: str
    independent_group: str
    data: JsonObject = field(default_factory=dict)
    change_id: str | None = None
    schema_version: int = HISTORY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != HISTORY_SCHEMA_VERSION:
            raise ModelError(
                f"unsupported historical-event schema version {self.schema_version}; "
                f"expected {HISTORY_SCHEMA_VERSION}"
            )
        _validate_identifier(self.id, "historical event id")
        _validate_identifier(self.repository_id, "historical event repository_id")
        _validate_identifier(self.kind, "historical event kind")
        _validate_identifier(self.provider, "historical event provider")
        _validate_reference(self.source_ref, "historical event source_ref")
        _validate_identifier(self.independent_group, "historical event independent_group")
        if self.change_id is not None:
            _validate_identifier(self.change_id, "historical event change_id")
        occurred_at = parse_timestamp(self.occurred_at)
        available_at = parse_timestamp(self.available_at)
        if available_at < occurred_at:
            raise ModelError("historical event available_at cannot predate occurred_at")
        if not isinstance(self.data, dict):
            raise ModelError("historical event data must be an object")
        validate_json_value(self.data, "historical event data")

    def to_dict(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "repository_id": self.repository_id,
            "kind": self.kind,
            "occurred_at": self.occurred_at,
            "available_at": self.available_at,
            "provider": self.provider,
            "source_ref": self.source_ref,
            "change_id": self.change_id,
            "independent_group": self.independent_group,
            "data": self.data,
        }

    @classmethod
    def from_dict(cls, value: JsonObject) -> HistoricalEvent:
        _reject_unknown_fields(
            value,
            frozenset(
                {
                    "schema_version",
                    "id",
                    "repository_id",
                    "kind",
                    "occurred_at",
                    "available_at",
                    "provider",
                    "source_ref",
                    "change_id",
                    "independent_group",
                    "data",
                }
            ),
            "historical event",
        )
        return cls(
            schema_version=_expect_schema_version(
                value.get("schema_version"), "historical event schema_version"
            ),
            id=_expect_string(value.get("id"), "historical event id"),
            repository_id=_expect_string(
                value.get("repository_id"), "historical event repository_id"
            ),
            kind=_expect_string(value.get("kind"), "historical event kind"),
            occurred_at=_expect_string(value.get("occurred_at"), "historical event occurred_at"),
            available_at=_expect_string(value.get("available_at"), "historical event available_at"),
            provider=_expect_string(value.get("provider"), "historical event provider"),
            source_ref=_expect_string(value.get("source_ref"), "historical event source_ref"),
            change_id=_expect_optional_string(value.get("change_id"), "historical event change_id"),
            independent_group=_expect_string(
                value.get("independent_group"), "historical event independent_group"
            ),
            data=_expect_object(value.get("data"), "historical event data"),
        )


@dataclass(frozen=True, slots=True)
class ChangeUnit:
    """A grouped change described only by evidence available at prediction time."""

    id: str
    repository_id: str
    kind: str
    base_sha: str
    prediction_sha: str
    prediction_at: str
    commits: tuple[str, ...]
    event_ids: tuple[str, ...]
    provider: str
    source_ref: str
    evidence_quality: EvidenceQuality
    confirmatory: bool
    final_sha: str | None = None
    finalized_at: str | None = None
    schema_version: int = HISTORY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != HISTORY_SCHEMA_VERSION:
            raise ModelError(
                f"unsupported change-unit schema version {self.schema_version}; "
                f"expected {HISTORY_SCHEMA_VERSION}"
            )
        _validate_identifier(self.id, "change unit id")
        _validate_identifier(self.repository_id, "change unit repository_id")
        _validate_identifier(self.kind, "change unit kind")
        _validate_identifier(self.provider, "change unit provider")
        _validate_reference(self.source_ref, "change unit source_ref")
        validate_git_sha(self.base_sha, field_name="change unit base_sha")
        validate_git_sha(self.prediction_sha, field_name="change unit prediction_sha")
        prediction_at = parse_timestamp(self.prediction_at)
        if (self.final_sha is None) != (self.finalized_at is None):
            raise ModelError("change unit final_sha and finalized_at must be set together")
        if self.final_sha is not None:
            validate_git_sha(self.final_sha, field_name="change unit final_sha")
            assert self.finalized_at is not None
            if parse_timestamp(self.finalized_at) < prediction_at:
                raise ModelError("change unit finalized_at cannot predate prediction_at")
        for index, commit in enumerate(self.commits):
            validate_git_sha(commit, field_name=f"change unit commits[{index}]")
        for event_id in self.event_ids:
            _validate_identifier(event_id, "change unit event id")
        _validate_unique(self.commits, "change unit commits")
        _validate_unique(self.event_ids, "change unit event_ids")
        if self.evidence_quality not in _EVIDENCE_QUALITIES:
            raise ModelError(
                "change unit evidence_quality must be one of: "
                + ", ".join(sorted(_EVIDENCE_QUALITIES))
            )
        if not isinstance(self.confirmatory, bool):
            raise ModelError("change unit confirmatory must be a boolean")
        if self.confirmatory and self.evidence_quality != "rich":
            raise ModelError("only rich change units may be confirmatory")

    def to_dict(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "repository_id": self.repository_id,
            "kind": self.kind,
            "base_sha": self.base_sha,
            "prediction_sha": self.prediction_sha,
            "prediction_at": self.prediction_at,
            "final_sha": self.final_sha,
            "finalized_at": self.finalized_at,
            "commits": list(self.commits),
            "event_ids": list(self.event_ids),
            "provider": self.provider,
            "source_ref": self.source_ref,
            "evidence_quality": self.evidence_quality,
            "confirmatory": self.confirmatory,
        }

    @classmethod
    def from_dict(cls, value: JsonObject) -> ChangeUnit:
        _reject_unknown_fields(
            value,
            frozenset(
                {
                    "schema_version",
                    "id",
                    "repository_id",
                    "kind",
                    "base_sha",
                    "prediction_sha",
                    "prediction_at",
                    "final_sha",
                    "finalized_at",
                    "commits",
                    "event_ids",
                    "provider",
                    "source_ref",
                    "evidence_quality",
                    "confirmatory",
                }
            ),
            "change unit",
        )
        evidence_quality = _expect_string(
            value.get("evidence_quality"), "change unit evidence_quality"
        )
        if evidence_quality not in _EVIDENCE_QUALITIES:
            raise ModelError(
                "change unit evidence_quality must be one of: "
                + ", ".join(sorted(_EVIDENCE_QUALITIES))
            )
        confirmatory = value.get("confirmatory")
        if not isinstance(confirmatory, bool):
            raise ModelError("change unit confirmatory must be a boolean")
        return cls(
            schema_version=_expect_schema_version(
                value.get("schema_version"), "change unit schema_version"
            ),
            id=_expect_string(value.get("id"), "change unit id"),
            repository_id=_expect_string(value.get("repository_id"), "change unit repository_id"),
            kind=_expect_string(value.get("kind"), "change unit kind"),
            base_sha=_expect_string(value.get("base_sha"), "change unit base_sha"),
            prediction_sha=_expect_string(
                value.get("prediction_sha"), "change unit prediction_sha"
            ),
            prediction_at=_expect_string(value.get("prediction_at"), "change unit prediction_at"),
            final_sha=_expect_optional_string(value.get("final_sha"), "change unit final_sha"),
            finalized_at=_expect_optional_string(
                value.get("finalized_at"), "change unit finalized_at"
            ),
            commits=_expect_string_tuple(value.get("commits"), "change unit commits"),
            event_ids=_expect_string_tuple(value.get("event_ids"), "change unit event_ids"),
            provider=_expect_string(value.get("provider"), "change unit provider"),
            source_ref=_expect_string(value.get("source_ref"), "change unit source_ref"),
            evidence_quality=cast(EvidenceQuality, evidence_quality),
            confirmatory=confirmatory,
        )
