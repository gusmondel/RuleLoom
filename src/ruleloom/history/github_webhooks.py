"""Point-in-time GitHub delivery capture without executing repository code.

This adapter is intentionally separate from the mutable GitHub archive adapter.
It consumes an already captured webhook body or ``GITHUB_EVENT_PATH`` file,
normalizes only a small allow-list of structured fields, and emits immutable
``HistoricalEvent`` records.  It never calls GitHub, checks out a pull request,
or evaluates free-form provider text.

The webhook transport authenticates the raw body with GitHub's
``X-Hub-Signature-256`` HMAC.  The GitHub Actions transport cannot re-verify a
provider webhook signature and is therefore identified honestly as trusted
runner context.  Both transports require a separate local envelope key so a
persisted normalized bundle can be authenticated before replay or ingestion.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from ruleloom.history.models import ChangeUnit, HistoricalEvent, validate_git_sha
from ruleloom.history.outcomes import ATOMIC_OUTCOME_TARGETS
from ruleloom.history.storage import (
    change_units_path,
    events_path,
    load_history_snapshot,
    upsert_history_batch,
)
from ruleloom.history.units import assemble_change_units
from ruleloom.models import (
    JsonObject,
    JsonValue,
    ModelError,
    canonical_json,
    content_hash,
    parse_timestamp,
    strict_json_loads,
    validate_predicate,
    validate_subject,
)

GITHUB_WEBHOOK_ADAPTER_VERSION = "ruleloom-github-webhook/1"
GITHUB_CAPTURE_SCHEMA_VERSION = 1
MAX_GITHUB_DELIVERY_BYTES = 2 * 1024 * 1024
MAX_GITHUB_CAPTURE_BYTES = 4 * 1024 * 1024
MAX_GITHUB_LABEL_POLICY_BYTES = 64 * 1024
MAX_GITHUB_CAPTURE_BUNDLES = 10_000
MAX_GITHUB_CAPTURE_BATCH_BYTES = 64 * 1024 * 1024
MAX_GITHUB_CAPTURE_BATCH_EVENTS = 50_000

CaptureTransport = Literal["github_webhook_hmac", "github_actions_event_file"]
OutcomeValue = Literal["positive", "negative"]

_CAPTURE_TRANSPORTS = frozenset({"github_webhook_hmac", "github_actions_event_file"})
_OUTCOME_VALUES = frozenset({"positive", "negative"})
_WEBHOOK_EVENTS = frozenset({"pull_request", "pull_request_review", "check_run", "label"})
_PULL_ACTIONS = frozenset({"opened", "reopened", "synchronize", "closed", "labeled", "unlabeled"})
_REVIEW_ACTIONS = frozenset({"submitted", "edited", "dismissed"})
_LABEL_ACTIONS = frozenset({"created", "edited", "deleted"})
_CHECK_CONCLUSIONS = frozenset(
    {
        "action_required",
        "cancelled",
        "failure",
        "neutral",
        "skipped",
        "stale",
        "startup_failure",
        "success",
        "timed_out",
    }
)
_DELIVERY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_BUNDLE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,239}\.json$")
_FULL_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}$")
_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
_SIGNATURE_RE = re.compile(r"^sha256=([0-9a-f]{64})$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

_CAPTURE_MAC_DOMAIN = b"ruleloom-github-capture-envelope-v1\x00"
_IDENTITY_DOMAIN = b"ruleloom-github-identity-v1\x00"
_POLICY_HASH_DOMAIN = b"ruleloom-github-label-policy-v1\x00"


class GitHubWebhookCaptureError(ModelError):
    """Raised when a delivery cannot be authenticated or normalized safely."""


def _integer(value: JsonValue, name: str, *, positive: bool = False) -> int:
    minimum = 1 if positive else 0
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > 2**63 - 1
    ):
        qualifier = "positive " if positive else "non-negative "
        raise GitHubWebhookCaptureError(f"GitHub {name} must be a {qualifier}integer")
    return value


def _string(value: JsonValue, name: str, *, maximum_bytes: int = 1024) -> str:
    if not isinstance(value, str) or not value or _CONTROL_RE.search(value):
        raise GitHubWebhookCaptureError(
            f"GitHub {name} must be a non-empty string without control characters"
        )
    if len(value.encode("utf-8")) > maximum_bytes:
        raise GitHubWebhookCaptureError(f"GitHub {name} exceeds {maximum_bytes} bytes")
    return value


def _object(value: JsonValue, name: str) -> JsonObject:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise GitHubWebhookCaptureError(f"GitHub {name} must be an object")
    return value


def _array(value: JsonValue, name: str, *, maximum: int) -> list[JsonValue]:
    if not isinstance(value, list):
        raise GitHubWebhookCaptureError(f"GitHub {name} must be an array")
    if len(value) > maximum:
        raise GitHubWebhookCaptureError(f"GitHub {name} exceeds {maximum} items")
    return value


def _normalize_timestamp(value: str | datetime, name: str) -> str:
    try:
        parsed = value if isinstance(value, datetime) else parse_timestamp(value)
    except (ModelError, ValueError) as exc:
        raise GitHubWebhookCaptureError(
            f"{name} must be a timezone-aware ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise GitHubWebhookCaptureError(f"{name} must include a timezone")
    normalized = parsed.astimezone(UTC)
    timespec = "microseconds" if normalized.microsecond else "seconds"
    return normalized.isoformat(timespec=timespec).replace("+00:00", "Z")


def _provider_timestamp(value: JsonValue, name: str, received_at: str) -> str:
    timestamp = _normalize_timestamp(_string(value, name), f"GitHub {name}")
    if parse_timestamp(timestamp) > parse_timestamp(received_at):
        raise GitHubWebhookCaptureError(f"GitHub {name} cannot postdate capture time")
    return timestamp


def _validate_key(value: bytes, name: str) -> bytes:
    if not isinstance(value, bytes) or len(value) < 16:
        raise GitHubWebhookCaptureError(f"{name} must contain at least 16 bytes")
    return value


def _provider_key(namespace: str, value: str | int) -> str:
    digest = hashlib.sha256(f"github\x00{namespace}\x00{value}".encode()).hexdigest()[:20]
    return f"github.{namespace}.{digest}"


def _pseudonymous_key(identity_key: bytes, namespace: str, value: str | int) -> str:
    digest = hmac.new(
        identity_key,
        _IDENTITY_DOMAIN + f"{namespace}\x00{value}".encode(),
        hashlib.sha256,
    ).hexdigest()[:24]
    return f"github.{namespace}.{digest}"


def _capture_mac(envelope_key: bytes, payload: JsonValue) -> str:
    return hmac.new(
        envelope_key,
        _CAPTURE_MAC_DOMAIN + canonical_json(payload).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class GitHubLabelOutcome:
    """One exact point-in-time label assertion supplied to capture.

    ``authorized_actor_ids`` are provider numeric IDs used only at capture time.
    They are covered by the policy hash but never copied into normalized events.
    Strong evidence is eligible only when ingestion independently pins that hash.
    """

    name: str
    target: str
    value: OutcomeValue
    evidence_complete: bool
    authorized_actor_ids: frozenset[int]

    def __post_init__(self) -> None:
        _string(self.name, "label policy name", maximum_bytes=256)
        validate_predicate(self.target, field_name="GitHub label outcome target")
        if self.target not in ATOMIC_OUTCOME_TARGETS:
            raise GitHubWebhookCaptureError(
                f"unsupported GitHub label outcome target: {self.target!r}"
            )
        if self.value not in _OUTCOME_VALUES:
            raise GitHubWebhookCaptureError(
                "GitHub label outcome value must be positive or negative"
            )
        if self.evidence_complete is not True:
            raise GitHubWebhookCaptureError("GitHub label outcomes require evidence_complete=true")
        if not self.authorized_actor_ids:
            raise GitHubWebhookCaptureError(
                "GitHub label outcomes require at least one authorized actor id"
            )
        for actor_id in self.authorized_actor_ids:
            _integer(actor_id, "authorized actor id", positive=True)

    def to_dict(self) -> JsonObject:
        return {
            "name": self.name,
            "target": self.target,
            "value": self.value,
            "evidence_complete": self.evidence_complete,
            "authorized_actor_ids": cast(JsonValue, sorted(self.authorized_actor_ids)),
        }

    @classmethod
    def from_dict(cls, value: JsonObject) -> GitHubLabelOutcome:
        expected = {
            "name",
            "target",
            "value",
            "evidence_complete",
            "authorized_actor_ids",
        }
        unknown = set(value).difference(expected)
        missing = expected.difference(value)
        if unknown or missing:
            detail = sorted(unknown or missing)
            qualifier = "unknown" if unknown else "missing"
            raise GitHubWebhookCaptureError(
                f"{qualifier} GitHub label policy fields: {', '.join(detail)}"
            )
        raw_actors = value["authorized_actor_ids"]
        if not isinstance(raw_actors, list):
            raise GitHubWebhookCaptureError(
                "GitHub label policy authorized_actor_ids must be an array"
            )
        actors = frozenset(
            _integer(item, "authorized actor id", positive=True) for item in raw_actors
        )
        if len(actors) != len(raw_actors):
            raise GitHubWebhookCaptureError(
                "GitHub label policy authorized_actor_ids cannot contain duplicates"
            )
        raw_complete = value["evidence_complete"]
        if not isinstance(raw_complete, bool):
            raise GitHubWebhookCaptureError(
                "GitHub label policy evidence_complete must be a boolean"
            )
        return cls(
            name=_string(value["name"], "label policy name", maximum_bytes=256),
            target=_string(value["target"], "label policy target", maximum_bytes=128),
            value=cast(
                OutcomeValue,
                _string(value["value"], "label policy value", maximum_bytes=16),
            ),
            evidence_complete=raw_complete,
            authorized_actor_ids=actors,
        )


def parse_github_label_policy(content: str) -> tuple[GitHubLabelOutcome, ...]:
    """Parse one strict, bounded label policy without changing project state."""

    if not isinstance(content, str):
        raise GitHubWebhookCaptureError("GitHub label policy must be UTF-8 JSON text")
    if len(content.encode("utf-8")) > MAX_GITHUB_LABEL_POLICY_BYTES:
        raise GitHubWebhookCaptureError(
            f"GitHub label policy exceeds {MAX_GITHUB_LABEL_POLICY_BYTES} bytes"
        )
    try:
        raw = strict_json_loads(content, "GitHub label policy")
    except (json.JSONDecodeError, ModelError) as exc:
        raise GitHubWebhookCaptureError(f"invalid GitHub label policy: {exc}") from exc
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "labels"}:
        raise GitHubWebhookCaptureError(
            "GitHub label policy must contain only schema_version and labels"
        )
    raw_schema = raw.get("schema_version")
    if isinstance(raw_schema, bool) or not isinstance(raw_schema, int) or raw_schema != 1:
        raise GitHubWebhookCaptureError("unsupported GitHub label policy schema version")
    labels = raw.get("labels")
    if not isinstance(labels, list) or len(labels) > 1_000:
        raise GitHubWebhookCaptureError(
            "GitHub label policy labels must be an array of at most 1000 entries"
        )
    result = tuple(
        GitHubLabelOutcome.from_dict(_object(item, f"label policy labels[{index}]"))
        for index, item in enumerate(labels)
    )
    names = [item.name for item in result]
    if len(names) != len(set(names)):
        raise GitHubWebhookCaptureError("GitHub label policy names must be unique")
    return result


def github_label_policy_hash(
    label_policy: Iterable[GitHubLabelOutcome],
    identity_key: bytes,
) -> str:
    """Return the stable keyed pin for a reviewed exact-label policy.

    Compute and freeze this value before the first eligible delivery.  Capture
    ingestion requires the independently supplied pin; a bundle is never
    allowed to declare its own policy as pre-registered.
    """

    identity_secret = _validate_key(identity_key, "GitHub identity key")
    policy = tuple(label_policy)
    if len(policy) > 1_000 or any(not isinstance(item, GitHubLabelOutcome) for item in policy):
        raise GitHubWebhookCaptureError(
            "GitHub label policy must contain at most 1000 GitHubLabelOutcome entries"
        )
    names = [item.name for item in policy]
    if len(names) != len(set(names)):
        raise GitHubWebhookCaptureError("GitHub label policy names must be unique")
    payload = cast(JsonValue, [item.to_dict() for item in policy])
    return hmac.new(
        identity_secret,
        _POLICY_HASH_DOMAIN + canonical_json(payload).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class GitHubWebhookCapture:
    """A normalized delivery bundle with a content hash and local HMAC."""

    transport: CaptureTransport
    delivery_id: str
    event_name: str
    received_at: str
    repository_id: str
    provider_repository_id: int
    provider_repository_key: str
    repository_full_name_at_delivery: str
    payload_sha256: str
    signature_verified: bool
    label_policy_hash: str
    events: tuple[HistoricalEvent, ...]
    envelope_sha256: str
    envelope_mac_sha256: str
    schema_version: int = GITHUB_CAPTURE_SCHEMA_VERSION
    adapter_version: str = GITHUB_WEBHOOK_ADAPTER_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != GITHUB_CAPTURE_SCHEMA_VERSION:
            raise GitHubWebhookCaptureError("unsupported GitHub capture schema version")
        if self.adapter_version != GITHUB_WEBHOOK_ADAPTER_VERSION:
            raise GitHubWebhookCaptureError("unsupported GitHub webhook adapter version")
        if self.transport not in _CAPTURE_TRANSPORTS:
            raise GitHubWebhookCaptureError("unsupported GitHub capture transport")
        _validate_delivery_id(self.delivery_id)
        if self.event_name not in _WEBHOOK_EVENTS:
            raise GitHubWebhookCaptureError("unsupported GitHub capture event name")
        _normalize_timestamp(self.received_at, "GitHub capture received_at")
        validate_subject(self.repository_id)
        _integer(self.provider_repository_id, "repository.id", positive=True)
        validate_subject(self.provider_repository_key)
        if self.provider_repository_key != _provider_key(
            "github.com.repo", self.provider_repository_id
        ):
            raise GitHubWebhookCaptureError(
                "GitHub provider repository key does not match numeric repository identity"
            )
        _validate_full_name(self.repository_full_name_at_delivery)
        for value, name in (
            (self.payload_sha256, "payload_sha256"),
            (self.label_policy_hash, "label_policy_hash"),
            (self.envelope_sha256, "envelope_sha256"),
            (self.envelope_mac_sha256, "envelope_mac_sha256"),
        ):
            if not _HEX_64_RE.fullmatch(value):
                raise GitHubWebhookCaptureError(f"GitHub capture {name} must be lowercase SHA-256")
        if self.signature_verified is not (self.transport == "github_webhook_hmac"):
            raise GitHubWebhookCaptureError(
                "GitHub signature_verified must be true only for webhook HMAC capture"
            )
        event_ids = [event.id for event in self.events]
        if not self.events or len(event_ids) != len(set(event_ids)):
            raise GitHubWebhookCaptureError(
                "GitHub capture requires unique events including its delivery event"
            )
        delivery_events = [event for event in self.events if event.kind == "provider_delivery"]
        if len(delivery_events) != 1:
            raise GitHubWebhookCaptureError(
                "GitHub capture requires exactly one provider delivery event"
            )
        delivery_key = hashlib.sha256(self.delivery_id.encode("utf-8")).hexdigest()[:20]
        expected_event_prefix = f"event.{self.provider_repository_key}.webhook.{delivery_key}."
        expected_source_prefix = f"github-webhook:{self.provider_repository_key}:"
        expected_change_prefix = f"change.{self.provider_repository_key}.pull."
        delivery_manifest: JsonObject = {
            "schema_version": self.schema_version,
            "adapter_version": self.adapter_version,
            "transport": self.transport,
            "delivery_id": self.delivery_id,
            "event_name": self.event_name,
            "received_at": self.received_at,
            "repository_id": self.repository_id,
            "provider_repository_id": self.provider_repository_id,
            "provider_repository_key": self.provider_repository_key,
            "repository_full_name_at_delivery": self.repository_full_name_at_delivery,
            "payload_sha256": self.payload_sha256,
            "signature_verified": self.signature_verified,
            "label_policy_hash": self.label_policy_hash,
        }
        expected_capture_provenance: JsonObject = {
            "transport": self.transport,
            "delivery_id": self.delivery_id,
            "event_name": self.event_name,
            "received_at": self.received_at,
            "payload_sha256": self.payload_sha256,
            "signature_verified": self.signature_verified,
            "label_policy_hash": self.label_policy_hash,
            "delivery_envelope_sha256": content_hash(delivery_manifest),
        }
        for event in self.events:
            if event.repository_id != self.repository_id or event.provider != "github":
                raise GitHubWebhookCaptureError(
                    "GitHub capture event repository/provider boundary mismatch"
                )
            if not event.id.startswith(expected_event_prefix):
                raise GitHubWebhookCaptureError(
                    f"GitHub capture event {event.id!r} has an invalid delivery identity"
                )
            if not event.source_ref.startswith(expected_source_prefix):
                raise GitHubWebhookCaptureError(
                    f"GitHub capture event {event.id!r} has an invalid repository source"
                )
            if event.change_id is not None and not event.change_id.startswith(
                expected_change_prefix
            ):
                raise GitHubWebhookCaptureError(
                    f"GitHub capture event {event.id!r} has an invalid change identity"
                )
            if event.available_at != self.received_at:
                raise GitHubWebhookCaptureError(
                    f"GitHub capture event {event.id!r} has invalid availability time"
                )
            if (
                event.data.get("adapter") != GITHUB_WEBHOOK_ADAPTER_VERSION
                or event.data.get("provider_repository_id") != self.provider_repository_id
                or event.data.get("repository_full_name_at_delivery")
                != self.repository_full_name_at_delivery
            ):
                raise GitHubWebhookCaptureError(
                    f"GitHub capture event {event.id!r} has inconsistent provider identity"
                )
            capture = event.data.get("capture")
            if not isinstance(capture, dict):
                raise GitHubWebhookCaptureError(
                    f"GitHub capture event {event.id!r} lacks capture provenance"
                )
            if any(
                capture.get(key) != expected
                for key, expected in expected_capture_provenance.items()
            ):
                raise GitHubWebhookCaptureError(
                    f"GitHub capture event {event.id!r} has inconsistent provenance"
                )
        if content_hash(self.envelope_payload()) != self.envelope_sha256:
            raise GitHubWebhookCaptureError("GitHub capture envelope hash does not match")

    def envelope_payload(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "adapter_version": self.adapter_version,
            "transport": self.transport,
            "delivery_id": self.delivery_id,
            "event_name": self.event_name,
            "received_at": self.received_at,
            "repository_id": self.repository_id,
            "provider_repository_id": self.provider_repository_id,
            "provider_repository_key": self.provider_repository_key,
            "repository_full_name_at_delivery": self.repository_full_name_at_delivery,
            "payload_sha256": self.payload_sha256,
            "signature_verified": self.signature_verified,
            "label_policy_hash": self.label_policy_hash,
            "events": [event.to_dict() for event in self.events],
        }

    def verify(self, envelope_key: bytes) -> None:
        """Fail unless both the envelope content hash and local HMAC are valid."""

        key = _validate_key(envelope_key, "GitHub capture envelope key")
        expected_hash = content_hash(self.envelope_payload())
        if not hmac.compare_digest(expected_hash, self.envelope_sha256):
            raise GitHubWebhookCaptureError("GitHub capture envelope hash does not match")
        expected_mac = _capture_mac(key, self.envelope_payload())
        if not hmac.compare_digest(expected_mac, self.envelope_mac_sha256):
            raise GitHubWebhookCaptureError("GitHub capture envelope MAC does not match")

    def to_dict(self) -> JsonObject:
        if content_hash(self.envelope_payload()) != self.envelope_sha256:
            raise GitHubWebhookCaptureError("GitHub capture changed after creation")
        return {
            **self.envelope_payload(),
            "envelope_sha256": self.envelope_sha256,
            "envelope_mac_sha256": self.envelope_mac_sha256,
        }

    @classmethod
    def from_dict(cls, value: JsonObject) -> GitHubWebhookCapture:
        expected = {
            "schema_version",
            "adapter_version",
            "transport",
            "delivery_id",
            "event_name",
            "received_at",
            "repository_id",
            "provider_repository_id",
            "provider_repository_key",
            "repository_full_name_at_delivery",
            "payload_sha256",
            "signature_verified",
            "label_policy_hash",
            "events",
            "envelope_sha256",
            "envelope_mac_sha256",
        }
        if set(value) != expected:
            raise GitHubWebhookCaptureError("GitHub capture bundle has an invalid field set")
        raw_events = value["events"]
        if not isinstance(raw_events, list):
            raise GitHubWebhookCaptureError("GitHub capture events must be an array")
        raw_signature = value["signature_verified"]
        if not isinstance(raw_signature, bool):
            raise GitHubWebhookCaptureError("GitHub signature_verified must be a boolean")
        raw_schema = value["schema_version"]
        if isinstance(raw_schema, bool) or not isinstance(raw_schema, int):
            raise GitHubWebhookCaptureError("GitHub capture schema_version must be an integer")
        return cls(
            schema_version=raw_schema,
            adapter_version=_string(
                value["adapter_version"], "capture adapter_version", maximum_bytes=128
            ),
            transport=cast(
                CaptureTransport,
                _string(value["transport"], "capture transport", maximum_bytes=64),
            ),
            delivery_id=_string(value["delivery_id"], "capture delivery_id", maximum_bytes=128),
            event_name=_string(value["event_name"], "capture event_name", maximum_bytes=64),
            received_at=_string(value["received_at"], "capture received_at", maximum_bytes=64),
            repository_id=_string(
                value["repository_id"], "capture repository_id", maximum_bytes=256
            ),
            provider_repository_id=_integer(
                value["provider_repository_id"], "capture provider_repository_id", positive=True
            ),
            provider_repository_key=_string(
                value["provider_repository_key"],
                "capture provider_repository_key",
                maximum_bytes=256,
            ),
            repository_full_name_at_delivery=_string(
                value["repository_full_name_at_delivery"],
                "capture repository_full_name_at_delivery",
                maximum_bytes=256,
            ),
            payload_sha256=_string(
                value["payload_sha256"], "capture payload_sha256", maximum_bytes=64
            ),
            signature_verified=raw_signature,
            label_policy_hash=_string(
                value["label_policy_hash"], "capture label_policy_hash", maximum_bytes=64
            ),
            events=tuple(
                HistoricalEvent.from_dict(_object(item, f"capture events[{index}]"))
                for index, item in enumerate(raw_events)
            ),
            envelope_sha256=_string(
                value["envelope_sha256"], "capture envelope_sha256", maximum_bytes=64
            ),
            envelope_mac_sha256=_string(
                value["envelope_mac_sha256"],
                "capture envelope_mac_sha256",
                maximum_bytes=64,
            ),
        )


def _validate_delivery_id(value: str) -> str:
    if not _DELIVERY_RE.fullmatch(value):
        raise GitHubWebhookCaptureError(
            "GitHub delivery id must be 1-128 safe ASCII identifier characters"
        )
    return value


def _validate_full_name(value: str) -> str:
    if not _FULL_NAME_RE.fullmatch(value):
        raise GitHubWebhookCaptureError("GitHub repository.full_name is invalid")
    return value


def _headers(headers: Mapping[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in headers.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise GitHubWebhookCaptureError("GitHub webhook headers must be strings")
        lowered = key.casefold()
        if lowered in normalized:
            raise GitHubWebhookCaptureError(f"duplicate GitHub webhook header: {key}")
        if (
            not key
            or _CONTROL_RE.search(key)
            or _CONTROL_RE.search(value)
            or len(key.encode("utf-8")) > 256
            or len(value.encode("utf-8")) > 4096
        ):
            raise GitHubWebhookCaptureError("GitHub webhook header is malformed or oversized")
        normalized[lowered] = value
    return normalized


def verify_github_webhook_signature(
    payload: bytes,
    signature: str,
    webhook_secret: bytes,
) -> None:
    """Verify GitHub's SHA-256 HMAC over the exact raw request body."""

    if not isinstance(payload, bytes):
        raise GitHubWebhookCaptureError("GitHub webhook payload must be raw bytes")
    key = _validate_key(webhook_secret, "GitHub webhook secret")
    match = _SIGNATURE_RE.fullmatch(signature)
    if match is None:
        raise GitHubWebhookCaptureError("GitHub webhook signature is malformed")
    expected = hmac.new(key, payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, match.group(1)):
        raise GitHubWebhookCaptureError("GitHub webhook signature verification failed")


@dataclass(frozen=True, slots=True)
class _CaptureContext:
    transport: CaptureTransport
    delivery_id: str
    event_name: str
    received_at: str
    repository_id: str
    provider_repository_id: int
    provider_repository_key: str
    repository_full_name: str
    payload_sha256: str
    signature_verified: bool
    identity_key: bytes
    label_policy: tuple[GitHubLabelOutcome, ...]
    label_policy_hash: str
    delivery_envelope_sha256: str


def _capture_data(context: _CaptureContext) -> JsonObject:
    return {
        "transport": context.transport,
        "delivery_id": context.delivery_id,
        "event_name": context.event_name,
        "received_at": context.received_at,
        "payload_sha256": context.payload_sha256,
        "signature_verified": context.signature_verified,
        "label_policy_hash": context.label_policy_hash,
        "delivery_envelope_sha256": context.delivery_envelope_sha256,
    }


def _base_data(context: _CaptureContext, actor_key: str) -> JsonObject:
    return {
        "adapter": GITHUB_WEBHOOK_ADAPTER_VERSION,
        "capture": _capture_data(context),
        "provider_repository_id": context.provider_repository_id,
        "repository_full_name_at_delivery": context.repository_full_name,
        "actor_key": actor_key,
    }


def _event_id(context: _CaptureContext, suffix: str) -> str:
    delivery_key = hashlib.sha256(context.delivery_id.encode("utf-8")).hexdigest()[:20]
    identifier = f"event.{context.provider_repository_key}.webhook.{delivery_key}.{suffix}"
    validate_subject(identifier)
    return identifier


def _pull_source_ref(context: _CaptureContext, number: int) -> str:
    delivery_key = hashlib.sha256(context.delivery_id.encode("utf-8")).hexdigest()[:20]
    return f"github-webhook:{context.provider_repository_key}:pull:{number}:delivery:{delivery_key}"


def _label_source_ref(context: _CaptureContext, label_id: int) -> str:
    delivery_key = hashlib.sha256(context.delivery_id.encode("utf-8")).hexdigest()[:20]
    return (
        f"github-webhook:{context.provider_repository_key}:label:{label_id}:delivery:{delivery_key}"
    )


def _change_id(context: _CaptureContext, number: int) -> str:
    return f"change.{context.provider_repository_key}.pull.{number}"


def _sender(payload: JsonObject, context: _CaptureContext) -> tuple[int, str]:
    sender = _object(payload.get("sender"), "sender")
    sender_id = _integer(sender.get("id"), "sender.id", positive=True)
    return sender_id, _pseudonymous_key(context.identity_key, "user", sender_id)


def _delivery_event(
    context: _CaptureContext,
    actor_key: str,
    *,
    source_ref: str,
    change_id: str | None,
    action: str,
) -> HistoricalEvent:
    data = _base_data(context, actor_key)
    data["action"] = action
    data["point_in_time"] = True
    return HistoricalEvent(
        id=_event_id(context, "delivery"),
        repository_id=context.repository_id,
        kind="provider_delivery",
        occurred_at=context.received_at,
        available_at=context.received_at,
        provider="github",
        source_ref=source_ref,
        change_id=change_id,
        independent_group=actor_key,
        data=data,
    )


def _pull_events(payload: JsonObject, context: _CaptureContext) -> tuple[HistoricalEvent, ...]:
    action = _string(payload.get("action"), "pull_request action", maximum_bytes=32).lower()
    if action not in _PULL_ACTIONS:
        raise GitHubWebhookCaptureError(f"unsupported GitHub pull_request action: {action!r}")
    pull = _object(payload.get("pull_request"), "pull_request")
    number = _integer(pull.get("number"), "pull_request.number", positive=True)
    author = _object(pull.get("user"), "pull_request.user")
    author_id = _integer(author.get("id"), "pull_request.user.id", positive=True)
    sender_id, actor_key = _sender(payload, context)
    author_key = _pseudonymous_key(context.identity_key, "user", author_id)
    created_at = _provider_timestamp(
        pull.get("created_at"), "pull_request.created_at", context.received_at
    )
    base = _object(pull.get("base"), "pull_request.base")
    head = _object(pull.get("head"), "pull_request.head")
    base_sha = validate_git_sha(
        _string(base.get("sha"), "pull_request.base.sha", maximum_bytes=64),
        field_name="GitHub pull_request.base.sha",
    )
    head_sha = validate_git_sha(
        _string(head.get("sha"), "pull_request.head.sha", maximum_bytes=64),
        field_name="GitHub pull_request.head.sha",
    )
    change_id = _change_id(context, number)
    source_ref = _pull_source_ref(context, number)
    events = [
        _delivery_event(
            context,
            actor_key,
            source_ref=source_ref,
            change_id=change_id,
            action=action,
        )
    ]

    if action in {"opened", "reopened", "synchronize"}:
        data = _base_data(context, actor_key)
        data.update(
            {
                "base_sha": base_sha,
                "head_sha": head_sha,
                "commits": [head_sha],
                "point_in_time": True,
                "provider_created_at": created_at,
                "code_changed": action == "synchronize",
                "actor_key": actor_key,
                "author_key": author_key,
                "independent": sender_id != author_id,
            }
        )
        events.append(
            HistoricalEvent(
                id=_event_id(context, "snapshot"),
                repository_id=context.repository_id,
                kind="change_snapshot",
                occurred_at=context.received_at,
                available_at=context.received_at,
                provider="github",
                source_ref=source_ref,
                change_id=change_id,
                independent_group=change_id,
                data=data,
            )
        )
    elif action == "closed":
        raw_merged = pull.get("merged")
        if not isinstance(raw_merged, bool):
            raise GitHubWebhookCaptureError("GitHub pull_request.merged must be a boolean")
        merged = raw_merged
        time_field = "merged_at" if merged else "closed_at"
        finalized_at = _provider_timestamp(
            pull.get(time_field), f"pull_request.{time_field}", context.received_at
        )
        data = _base_data(context, actor_key)
        data.update(
            {
                "base_sha": base_sha,
                "head_sha": head_sha,
                "final_sha": head_sha,
                "commits": [head_sha],
                "point_in_time": True,
                "actor_key": actor_key,
                "author_key": author_key,
                "independent": sender_id != author_id,
            }
        )
        merge_sha = pull.get("merge_commit_sha")
        if merged and isinstance(merge_sha, str) and merge_sha:
            data["merge_sha"] = validate_git_sha(
                merge_sha,
                field_name="GitHub pull_request.merge_commit_sha",
            )
        events.append(
            HistoricalEvent(
                id=_event_id(context, "final"),
                repository_id=context.repository_id,
                kind="change_merged" if merged else "change_closed",
                occurred_at=finalized_at,
                available_at=context.received_at,
                provider="github",
                source_ref=source_ref,
                change_id=change_id,
                independent_group=change_id,
                data=data,
            )
        )
    else:
        label = _object(payload.get("label"), "pull_request label")
        label_id = _integer(label.get("id"), "pull_request label.id", positive=True)
        label_name = _string(label.get("name"), "pull_request label.name", maximum_bytes=256)
        data = _base_data(context, actor_key)
        data.update(
            {
                "label_id": label_id,
                "label_name_at_delivery": label_name,
                "label_action": action,
                "point_in_time_label_name": True,
                "actor_key": actor_key,
                "author_key": author_key,
                "independent": sender_id != author_id,
            }
        )
        events.append(
            HistoricalEvent(
                id=_event_id(context, "label-applied" if action == "labeled" else "label-removed"),
                repository_id=context.repository_id,
                kind="provider_label_applied" if action == "labeled" else "provider_label_removed",
                occurred_at=context.received_at,
                available_at=context.received_at,
                provider="github",
                source_ref=source_ref,
                change_id=change_id,
                independent_group=actor_key,
                data=data,
            )
        )
        declaration = next(
            (item for item in context.label_policy if item.name == label_name),
            None,
        )
        if (
            action == "labeled"
            and declaration is not None
            and sender_id in declaration.authorized_actor_ids
            and sender_id != author_id
        ):
            outcome_data = dict(data)
            outcome_data.update(
                {
                    "target": declaration.target,
                    "value": declaration.value,
                    "evidence_complete": True,
                    "strength": "strong",
                    "confidence": 1.0,
                    "reason": ("authorized independent point-in-time GitHub label assertion"),
                    "authorization": "registered_provider_actor_allowlist",
                }
            )
            events.append(
                HistoricalEvent(
                    id=_event_id(context, "label-outcome"),
                    repository_id=context.repository_id,
                    kind="change_finalized",
                    occurred_at=context.received_at,
                    available_at=context.received_at,
                    provider="github",
                    source_ref=source_ref,
                    change_id=change_id,
                    independent_group=actor_key,
                    data=outcome_data,
                )
            )
    return tuple(events)


def _review_events(payload: JsonObject, context: _CaptureContext) -> tuple[HistoricalEvent, ...]:
    action = _string(payload.get("action"), "review action", maximum_bytes=32).lower()
    if action not in _REVIEW_ACTIONS:
        raise GitHubWebhookCaptureError(f"unsupported GitHub review action: {action!r}")
    pull = _object(payload.get("pull_request"), "pull_request")
    number = _integer(pull.get("number"), "pull_request.number", positive=True)
    author = _object(pull.get("user"), "pull_request.user")
    author_id = _integer(author.get("id"), "pull_request.user.id", positive=True)
    _provider_timestamp(pull.get("created_at"), "pull_request.created_at", context.received_at)
    review = _object(payload.get("review"), "review")
    review_id = _integer(review.get("id"), "review.id", positive=True)
    reviewer = _object(review.get("user"), "review.user")
    reviewer_id = _integer(reviewer.get("id"), "review.user.id", positive=True)
    sender_id, actor_key = _sender(payload, context)
    author_key = _pseudonymous_key(context.identity_key, "user", author_id)
    reviewer_key = _pseudonymous_key(context.identity_key, "user", reviewer_id)
    submitted_at = _provider_timestamp(
        review.get("submitted_at"), "review.submitted_at", context.received_at
    )
    state = _string(review.get("state"), "review.state", maximum_bytes=64).lower()
    decision = {
        "approved": "approved",
        "changes_requested": "changes_requested",
        "commented": "commented",
        "dismissed": "dismissed",
        "pending": "pending",
    }.get(state, "unspecified")
    change_id = _change_id(context, number)
    source_ref = _pull_source_ref(context, number)
    delivery = _delivery_event(
        context,
        actor_key,
        source_ref=source_ref,
        change_id=change_id,
        action=action,
    )
    data = _base_data(context, actor_key)
    data.update(
        {
            "review_id": review_id,
            "review_action": action,
            "decision": decision,
            "category": "unspecified",
            "reviewer_key": reviewer_key,
            "actor_key": actor_key,
            "author_key": author_key,
            "independent": reviewer_id != author_id,
            "actor_is_reviewer": sender_id == reviewer_id,
            "provider_submitted_at": submitted_at,
            "point_in_time": True,
        }
    )
    commit_id = review.get("commit_id")
    if isinstance(commit_id, str) and commit_id:
        data["commit_sha"] = validate_git_sha(
            commit_id,
            field_name="GitHub review.commit_id",
        )
    event = HistoricalEvent(
        id=_event_id(context, f"review-{review_id}"),
        repository_id=context.repository_id,
        kind="review" if action == "submitted" else "provider_review_changed",
        occurred_at=submitted_at if action == "submitted" else context.received_at,
        available_at=context.received_at,
        provider="github",
        source_ref=source_ref,
        change_id=change_id,
        independent_group=reviewer_key,
        data=data,
    )
    return delivery, event


def _check_events(payload: JsonObject, context: _CaptureContext) -> tuple[HistoricalEvent, ...]:
    action = _string(payload.get("action"), "check_run action", maximum_bytes=32).lower()
    if action != "completed":
        raise GitHubWebhookCaptureError(f"unsupported GitHub check_run action: {action!r}")
    check = _object(payload.get("check_run"), "check_run")
    check_id = _integer(check.get("id"), "check_run.id", positive=True)
    status = _string(check.get("status"), "check_run.status", maximum_bytes=32).lower()
    if status != "completed":
        raise GitHubWebhookCaptureError(
            "GitHub completed check_run action has non-completed status"
        )
    conclusion = _string(check.get("conclusion"), "check_run.conclusion", maximum_bytes=64).lower()
    if conclusion not in _CHECK_CONCLUSIONS:
        raise GitHubWebhookCaptureError(f"unsupported GitHub check_run conclusion: {conclusion!r}")
    completed_at = _provider_timestamp(
        check.get("completed_at"), "check_run.completed_at", context.received_at
    )
    head_sha = validate_git_sha(
        _string(check.get("head_sha"), "check_run.head_sha", maximum_bytes=64),
        field_name="GitHub check_run.head_sha",
    )
    name = _string(check.get("name"), "check_run.name", maximum_bytes=512)
    app = _object(check.get("app"), "check_run.app")
    app_id = _integer(app.get("id"), "check_run.app.id", positive=True)
    _sender_id, actor_key = _sender(payload, context)
    check_key = _pseudonymous_key(context.identity_key, "check", f"{app_id}:{name}")
    pulls = _array(check.get("pull_requests"), "check_run.pull_requests", maximum=100)
    if not pulls:
        source_ref = (
            f"github-webhook:{context.provider_repository_key}:check:{check_id}:"
            f"delivery:{hashlib.sha256(context.delivery_id.encode()).hexdigest()[:20]}"
        )
        return (
            _delivery_event(
                context,
                actor_key,
                source_ref=source_ref,
                change_id=None,
                action=action,
            ),
        )
    events: list[HistoricalEvent] = []
    for index, raw_pull in enumerate(pulls):
        pull = _object(raw_pull, f"check_run.pull_requests[{index}]")
        number = _integer(
            pull.get("number"), f"check_run.pull_requests[{index}].number", positive=True
        )
        change_id = _change_id(context, number)
        source_ref = _pull_source_ref(context, number)
        if index == 0:
            events.append(
                _delivery_event(
                    context,
                    actor_key,
                    source_ref=source_ref,
                    change_id=change_id,
                    action=action,
                )
            )
        data = _base_data(context, actor_key)
        data.update(
            {
                "check_id": check_key,
                "provider_check_run_id": check_id,
                "conclusion": conclusion,
                "head_sha": head_sha,
                "attributable_to_change": False,
                "attribution": "unattributed_provider_result",
                "evidence_grade": "provider_event",
                "provider_completed_at": completed_at,
                "point_in_time": True,
            }
        )
        events.append(
            HistoricalEvent(
                id=_event_id(context, f"check-{check_id}-pull-{number}"),
                repository_id=context.repository_id,
                kind="ci_run",
                occurred_at=completed_at,
                available_at=context.received_at,
                provider="github",
                source_ref=source_ref,
                change_id=change_id,
                independent_group=check_key,
                data=data,
            )
        )
    return tuple(events)


def _repository_label_events(
    payload: JsonObject,
    context: _CaptureContext,
) -> tuple[HistoricalEvent, ...]:
    action = _string(payload.get("action"), "label action", maximum_bytes=32).lower()
    if action not in _LABEL_ACTIONS:
        raise GitHubWebhookCaptureError(f"unsupported GitHub label action: {action!r}")
    label = _object(payload.get("label"), "label")
    label_id = _integer(label.get("id"), "label.id", positive=True)
    label_name = _string(label.get("name"), "label.name", maximum_bytes=256)
    _sender_id, actor_key = _sender(payload, context)
    source_ref = _label_source_ref(context, label_id)
    delivery = _delivery_event(
        context,
        actor_key,
        source_ref=source_ref,
        change_id=None,
        action=action,
    )
    data = _base_data(context, actor_key)
    data.update(
        {
            "label_id": label_id,
            "label_name_at_delivery": label_name,
            "label_action": action,
            "point_in_time_label_name": True,
        }
    )
    changes = payload.get("changes")
    if action == "edited" and isinstance(changes, dict):
        name_change = changes.get("name")
        if isinstance(name_change, dict) and "from" in name_change:
            data["previous_name_at_delivery"] = _string(
                name_change.get("from"), "label changes.name.from", maximum_bytes=256
            )
    event = HistoricalEvent(
        id=_event_id(context, f"repository-label-{label_id}"),
        repository_id=context.repository_id,
        kind="provider_label_definition",
        occurred_at=context.received_at,
        available_at=context.received_at,
        provider="github",
        source_ref=source_ref,
        change_id=None,
        independent_group=actor_key,
        data=data,
    )
    return delivery, event


def _decode_payload(payload: bytes) -> JsonObject:
    if not isinstance(payload, bytes):
        raise GitHubWebhookCaptureError("GitHub delivery payload must be raw bytes")
    if not payload:
        raise GitHubWebhookCaptureError("GitHub delivery payload cannot be empty")
    if len(payload) > MAX_GITHUB_DELIVERY_BYTES:
        raise GitHubWebhookCaptureError(
            f"GitHub delivery payload exceeds {MAX_GITHUB_DELIVERY_BYTES} bytes"
        )
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GitHubWebhookCaptureError("GitHub delivery payload must be UTF-8") from exc
    try:
        raw = strict_json_loads(text, "GitHub delivery payload")
    except (json.JSONDecodeError, ModelError) as exc:
        raise GitHubWebhookCaptureError(f"invalid GitHub delivery payload: {exc}") from exc
    return _object(raw, "delivery payload")


def _capture_delivery(
    payload_bytes: bytes,
    *,
    transport: CaptureTransport,
    delivery_id: str,
    event_name: str,
    received_at: str | datetime,
    repository_id: str,
    expected_provider_repository_id: int,
    signature_verified: bool,
    identity_key: bytes,
    envelope_key: bytes,
    label_policy: Iterable[GitHubLabelOutcome],
) -> GitHubWebhookCapture:
    identity_secret = _validate_key(identity_key, "GitHub identity key")
    envelope_secret = _validate_key(envelope_key, "GitHub capture envelope key")
    delivery = _validate_delivery_id(delivery_id)
    normalized_event = event_name.casefold()
    if normalized_event not in _WEBHOOK_EVENTS:
        raise GitHubWebhookCaptureError(f"unsupported GitHub webhook event: {event_name!r}")
    normalized_received_at = _normalize_timestamp(received_at, "GitHub capture received_at")
    validate_subject(repository_id)
    expected_id = _integer(
        expected_provider_repository_id,
        "expected repository id",
        positive=True,
    )
    policy = tuple(label_policy)
    label_policy_hash = github_label_policy_hash(policy, identity_secret)
    payload = _decode_payload(payload_bytes)
    repository = _object(payload.get("repository"), "repository")
    provider_repository_id = _integer(repository.get("id"), "repository.id", positive=True)
    if provider_repository_id != expected_id:
        raise GitHubWebhookCaptureError(
            "GitHub payload repository id does not match the configured provider repository"
        )
    full_name = _validate_full_name(
        _string(repository.get("full_name"), "repository.full_name", maximum_bytes=256)
    )
    provider_repository_key = _provider_key("github.com.repo", provider_repository_id)
    payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()
    delivery_manifest: JsonObject = {
        "schema_version": GITHUB_CAPTURE_SCHEMA_VERSION,
        "adapter_version": GITHUB_WEBHOOK_ADAPTER_VERSION,
        "transport": transport,
        "delivery_id": delivery,
        "event_name": normalized_event,
        "received_at": normalized_received_at,
        "repository_id": repository_id,
        "provider_repository_id": provider_repository_id,
        "provider_repository_key": provider_repository_key,
        "repository_full_name_at_delivery": full_name,
        "payload_sha256": payload_sha256,
        "signature_verified": signature_verified,
        "label_policy_hash": label_policy_hash,
    }
    context = _CaptureContext(
        transport=transport,
        delivery_id=delivery,
        event_name=normalized_event,
        received_at=normalized_received_at,
        repository_id=repository_id,
        provider_repository_id=provider_repository_id,
        provider_repository_key=provider_repository_key,
        repository_full_name=full_name,
        payload_sha256=payload_sha256,
        signature_verified=signature_verified,
        identity_key=identity_secret,
        label_policy=policy,
        label_policy_hash=label_policy_hash,
        delivery_envelope_sha256=content_hash(delivery_manifest),
    )
    normalizers = {
        "pull_request": _pull_events,
        "pull_request_review": _review_events,
        "check_run": _check_events,
        "label": _repository_label_events,
    }
    events = normalizers[normalized_event](payload, context)
    envelope_payload: JsonObject = {
        **delivery_manifest,
        "events": [event.to_dict() for event in events],
    }
    envelope_sha256 = content_hash(envelope_payload)
    envelope_mac_sha256 = _capture_mac(envelope_secret, envelope_payload)
    capture = GitHubWebhookCapture(
        transport=transport,
        delivery_id=delivery,
        event_name=normalized_event,
        received_at=normalized_received_at,
        repository_id=repository_id,
        provider_repository_id=provider_repository_id,
        provider_repository_key=provider_repository_key,
        repository_full_name_at_delivery=full_name,
        payload_sha256=payload_sha256,
        signature_verified=signature_verified,
        label_policy_hash=label_policy_hash,
        events=events,
        envelope_sha256=envelope_sha256,
        envelope_mac_sha256=envelope_mac_sha256,
    )
    capture.verify(envelope_secret)
    return capture


def capture_github_webhook(
    payload: bytes,
    headers: Mapping[str, str],
    *,
    received_at: str | datetime,
    repository_id: str,
    expected_provider_repository_id: int,
    webhook_secret: bytes,
    identity_key: bytes,
    envelope_key: bytes,
    label_policy: Iterable[GitHubLabelOutcome] = (),
) -> GitHubWebhookCapture:
    """Authenticate and normalize one raw public-GitHub webhook delivery."""

    if not isinstance(payload, bytes) or len(payload) > MAX_GITHUB_DELIVERY_BYTES:
        raise GitHubWebhookCaptureError(
            f"GitHub delivery payload must be raw bytes no larger than {MAX_GITHUB_DELIVERY_BYTES}"
        )
    normalized_headers = _headers(headers)
    try:
        event_name = normalized_headers["x-github-event"]
        delivery_id = normalized_headers["x-github-delivery"]
        signature = normalized_headers["x-hub-signature-256"]
    except KeyError as exc:
        raise GitHubWebhookCaptureError(
            f"missing required GitHub webhook header: {exc.args[0]}"
        ) from exc
    verify_github_webhook_signature(payload, signature, webhook_secret)
    return _capture_delivery(
        payload,
        transport="github_webhook_hmac",
        delivery_id=delivery_id,
        event_name=event_name,
        received_at=received_at,
        repository_id=repository_id,
        expected_provider_repository_id=expected_provider_repository_id,
        signature_verified=True,
        identity_key=identity_key,
        envelope_key=envelope_key,
        label_policy=label_policy,
    )


def capture_github_actions_event(
    payload: bytes,
    *,
    event_name: str,
    run_id: int,
    received_at: str | datetime,
    repository_id: str,
    expected_provider_repository_id: int,
    identity_key: bytes,
    envelope_key: bytes,
    label_policy: Iterable[GitHubLabelOutcome] = (),
) -> GitHubWebhookCapture:
    """Normalize a GitHub Actions event file under an explicit runner trust boundary.

    This transport does not claim provider-HMAC verification.  Call it before
    checking out or executing pull-request code, and persist its MAC-protected
    bundle in storage controlled by the receiving team.
    """

    normalized_run_id = _integer(run_id, "Actions run id", positive=True)
    return _capture_delivery(
        payload,
        transport="github_actions_event_file",
        delivery_id=f"actions-{normalized_run_id}",
        event_name=event_name,
        received_at=received_at,
        repository_id=repository_id,
        expected_provider_repository_id=expected_provider_repository_id,
        signature_verified=False,
        identity_key=identity_key,
        envelope_key=envelope_key,
        label_policy=label_policy,
    )


def _read_bounded_regular_file(path: Path, *, maximum: int, name: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise GitHubWebhookCaptureError(
            f"{name} must be a readable regular, non-symlink file: {path}: {exc}"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
            raise GitHubWebhookCaptureError(
                f"{name} must be a regular file no larger than {maximum} bytes: {path}"
            )
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            content = handle.read(maximum + 1)
        if len(content) > maximum:
            raise GitHubWebhookCaptureError(f"{name} exceeds {maximum} bytes: {path}")
        return content
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def capture_github_actions_event_file(
    path: Path,
    *,
    event_name: str,
    run_id: int,
    received_at: str | datetime,
    repository_id: str,
    expected_provider_repository_id: int,
    identity_key: bytes,
    envelope_key: bytes,
    label_policy: Iterable[GitHubLabelOutcome] = (),
) -> GitHubWebhookCapture:
    """Safely read and normalize an existing ``GITHUB_EVENT_PATH`` file."""

    payload = _read_bounded_regular_file(
        path,
        maximum=MAX_GITHUB_DELIVERY_BYTES,
        name="GitHub Actions event payload",
    )
    return capture_github_actions_event(
        payload,
        event_name=event_name,
        run_id=run_id,
        received_at=received_at,
        repository_id=repository_id,
        expected_provider_repository_id=expected_provider_repository_id,
        identity_key=identity_key,
        envelope_key=envelope_key,
        label_policy=label_policy,
    )


def _validate_output_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise GitHubWebhookCaptureError(
            f"GitHub capture output directory must already exist: {path}: {exc}"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise GitHubWebhookCaptureError(
            f"GitHub capture output directory must be a non-symlink directory: {path}"
        )
    if metadata.st_uid != os.getuid():
        raise GitHubWebhookCaptureError(
            f"GitHub capture output directory must be owned by the current user: {path}"
        )
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise GitHubWebhookCaptureError(
            f"GitHub capture output directory must not be group/world writable: {path}"
        )


def write_github_capture_bundle(
    path: Path,
    capture: GitHubWebhookCapture,
    *,
    envelope_key: bytes,
) -> bool:
    """Create and authenticate a create-once bundle; return false for an exact replay."""

    capture.verify(envelope_key)
    capture_content = (canonical_json(capture.to_dict()) + "\n").encode("utf-8")
    if len(capture_content) > MAX_GITHUB_CAPTURE_BYTES:
        raise GitHubWebhookCaptureError(
            f"GitHub capture bundle exceeds {MAX_GITHUB_CAPTURE_BYTES} bytes"
        )
    _validate_output_directory(path.parent)
    try:
        existing = _read_bounded_regular_file(
            path,
            maximum=MAX_GITHUB_CAPTURE_BYTES,
            name="GitHub capture bundle",
        )
    except GitHubWebhookCaptureError:
        if path.exists() or path.is_symlink():
            raise
    else:
        if hmac.compare_digest(existing, capture_content):
            return False
        raise GitHubWebhookCaptureError(
            f"refusing to overwrite conflicting GitHub capture bundle: {path}"
        )

    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(capture_content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary_path, path, follow_symlinks=False)
        except FileExistsError:
            existing = _read_bounded_regular_file(
                path,
                maximum=MAX_GITHUB_CAPTURE_BYTES,
                name="GitHub capture bundle",
            )
            if hmac.compare_digest(existing, capture_content):
                return False
            raise GitHubWebhookCaptureError(
                f"refusing to overwrite conflicting GitHub capture bundle: {path}"
            ) from None
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return True
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)


def load_github_capture_bundle(path: Path, *, envelope_key: bytes) -> GitHubWebhookCapture:
    """Read a bounded non-symlink bundle and verify its hash and local HMAC."""

    content = _read_bounded_regular_file(
        path,
        maximum=MAX_GITHUB_CAPTURE_BYTES,
        name="GitHub capture bundle",
    )
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GitHubWebhookCaptureError("GitHub capture bundle must be UTF-8") from exc
    if not text.endswith("\n") or "\n" in text[:-1]:
        raise GitHubWebhookCaptureError("GitHub capture bundle must contain one JSON line")
    try:
        raw = strict_json_loads(text[:-1], "GitHub capture bundle")
    except (json.JSONDecodeError, ModelError) as exc:
        raise GitHubWebhookCaptureError(f"invalid GitHub capture bundle: {exc}") from exc
    capture = GitHubWebhookCapture.from_dict(_object(raw, "capture bundle"))
    capture.verify(envelope_key)
    return capture


@dataclass(frozen=True, slots=True)
class GitHubCaptureDirectoryReport:
    """Deterministic counts for one atomic bounded inbox ingestion."""

    processed_bundles: tuple[str, ...]
    unique_deliveries: int
    duplicate_replays: int
    events_inserted: int
    events_unchanged: int
    units_inserted: int
    units_unchanged: int

    def __post_init__(self) -> None:
        if tuple(sorted(self.processed_bundles)) != self.processed_bundles:
            raise GitHubWebhookCaptureError("processed capture bundles must be sorted")
        values = (
            self.unique_deliveries,
            self.duplicate_replays,
            self.events_inserted,
            self.events_unchanged,
            self.units_inserted,
            self.units_unchanged,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values
        ):
            raise GitHubWebhookCaptureError("capture directory report counts must be non-negative")
        if self.unique_deliveries + self.duplicate_replays != len(self.processed_bundles):
            raise GitHubWebhookCaptureError(
                "capture directory report delivery counts do not add up"
            )

    def to_dict(self) -> JsonObject:
        return {
            "processed_bundles": cast(JsonValue, list(self.processed_bundles)),
            "bundles_examined": len(self.processed_bundles),
            "unique_deliveries": self.unique_deliveries,
            "duplicate_replays": self.duplicate_replays,
            "events_inserted": self.events_inserted,
            "events_unchanged": self.events_unchanged,
            "units_inserted": self.units_inserted,
            "units_unchanged": self.units_unchanged,
        }


def _merge_event_preview(
    existing: Sequence[HistoricalEvent],
    incoming: Iterable[HistoricalEvent],
) -> tuple[HistoricalEvent, ...]:
    by_id = {event.id: event for event in existing}
    for event in incoming:
        previous = by_id.get(event.id)
        if previous is not None and previous != event:
            raise GitHubWebhookCaptureError(
                f"conflicting immutable GitHub historical event id {event.id!r}"
            )
        by_id[event.id] = event
    return tuple(by_id.values())


def _complete_github_webhook_units(
    events: Sequence[HistoricalEvent],
    existing_units: Sequence[ChangeUnit],
    eligible_change_ids: frozenset[str],
) -> tuple[ChangeUnit, ...]:
    if not eligible_change_ids:
        return ()
    existing_by_id = {unit.id: unit for unit in existing_units}
    conflicting_archive = next(
        (
            unit
            for unit in existing_units
            if unit.id in eligible_change_ids and unit.kind == "github_archive_change"
        ),
        None,
    )
    if conflicting_archive is not None:
        raise GitHubWebhookCaptureError(
            f"point-in-time capture cannot upgrade existing github_archive_change "
            f"{conflicting_archive.id!r}; start a new experiment before capturing "
            "this pull request"
        )
    archive_change_ids = {
        event.change_id
        for event in events
        if event.change_id is not None and event.data.get("adapter") == "ruleloom-github/1"
    }
    mixed = sorted(eligible_change_ids.intersection(archive_change_ids))
    if mixed:
        raise GitHubWebhookCaptureError(
            "point-in-time capture cannot upgrade GitHub archive evidence for "
            f"{mixed[0]!r}; start a new experiment before capturing this pull request"
        )

    candidates = tuple(
        unit
        for unit in assemble_change_units(events)
        if unit.id in eligible_change_ids
        and unit.confirmatory
        and unit.evidence_quality == "rich"
        and unit.final_sha is not None
        and unit.finalized_at is not None
        and unit.source_ref.startswith("github-webhook:")
    )
    for candidate in candidates:
        previous = existing_by_id.get(candidate.id)
        if previous is None or previous == candidate:
            continue
        raise GitHubWebhookCaptureError(
            f"point-in-time capture conflicts with immutable change unit {candidate.id!r}"
        )
    return candidates


def _validate_expected_repository_id(value: str) -> str:
    try:
        return validate_subject(value)
    except ModelError as exc:
        raise GitHubWebhookCaptureError(
            f"invalid expected GitHub capture repository id: {exc}"
        ) from exc


def _validate_expected_label_policy_hash(value: str) -> str:
    if not isinstance(value, str) or _HEX_64_RE.fullmatch(value) is None:
        raise GitHubWebhookCaptureError(
            "expected GitHub label policy hash must be a lowercase SHA-256 digest"
        )
    return value


def _validate_capture_pins(
    capture: GitHubWebhookCapture,
    *,
    expected_repository_id: str,
    expected_label_policy_hash: str,
) -> None:
    if capture.repository_id != expected_repository_id:
        raise GitHubWebhookCaptureError(
            f"GitHub capture targets repository {capture.repository_id!r}, not configured "
            f"repository {expected_repository_id!r}"
        )
    if not hmac.compare_digest(capture.label_policy_hash, expected_label_policy_hash):
        raise GitHubWebhookCaptureError(
            "GitHub capture label policy does not match the independently frozen "
            "experiment policy hash"
        )


def _validate_history_capture_pins(
    events: Sequence[HistoricalEvent],
    units: Sequence[ChangeUnit],
    *,
    expected_repository_id: str,
    expected_label_policy_hash: str,
) -> None:
    repository_ids = {
        *(event.repository_id for event in events),
        *(unit.repository_id for unit in units),
    }
    if repository_ids and repository_ids != {expected_repository_id}:
        raise GitHubWebhookCaptureError(
            "GitHub capture repository id does not match existing history"
        )
    for event in events:
        if event.data.get("adapter") != GITHUB_WEBHOOK_ADAPTER_VERSION:
            continue
        capture = event.data.get("capture")
        policy_hash = capture.get("label_policy_hash") if isinstance(capture, dict) else None
        if not isinstance(policy_hash, str) or _HEX_64_RE.fullmatch(policy_hash) is None:
            raise GitHubWebhookCaptureError(
                f"GitHub webhook event {event.id!r} lacks a valid label-policy pin"
            )
        if not hmac.compare_digest(policy_hash, expected_label_policy_hash):
            raise GitHubWebhookCaptureError(
                f"GitHub webhook event {event.id!r} does not match the independently frozen "
                "experiment policy hash"
            )


def ingest_github_capture(
    root: Path,
    capture: GitHubWebhookCapture,
    *,
    expected_repository_id: str,
    expected_label_policy_hash: str,
    envelope_key: bytes,
) -> tuple[tuple[int, int], tuple[int, int]]:
    """Append events and finalize any now-complete point-in-time change unit.

    The capture must match caller-supplied repository and label-policy pins; it
    cannot establish either boundary from its own envelope.  A unit is
    persisted only after both a confirmatory point-in-time snapshot and a
    structural finalization event exist.  This prevents an immutable open unit
    from later needing an in-place upgrade.  The return value is
    ``(event_counts, unit_counts)`` where each pair is ``(inserted, unchanged)``.
    """

    repository_pin = _validate_expected_repository_id(expected_repository_id)
    policy_pin = _validate_expected_label_policy_hash(expected_label_policy_hash)
    envelope_secret = _validate_key(envelope_key, "GitHub capture envelope key")
    capture.verify(envelope_secret)
    _validate_capture_pins(
        capture,
        expected_repository_id=repository_pin,
        expected_label_policy_hash=policy_pin,
    )
    change_ids = frozenset(
        event.change_id for event in capture.events if event.change_id is not None
    )
    try:
        event_path = events_path(root)
        unit_path = change_units_path(root)
        existing_events, existing_units = load_history_snapshot(event_path, unit_path)
        _validate_history_capture_pins(
            existing_events,
            existing_units,
            expected_repository_id=repository_pin,
            expected_label_policy_hash=policy_pin,
        )
        combined_events = _merge_event_preview(existing_events, capture.events)
        candidate_units = _complete_github_webhook_units(
            combined_events,
            existing_units,
            change_ids,
        )
        event_counts, unit_counts = upsert_history_batch(
            event_path,
            capture.events,
            unit_path,
            candidate_units,
        )
    except ModelError as exc:
        if isinstance(exc, GitHubWebhookCaptureError):
            raise
        raise GitHubWebhookCaptureError(f"cannot ingest GitHub capture: {exc}") from exc
    return event_counts, unit_counts


def finalize_github_capture_units(
    root: Path,
    *,
    expected_repository_id: str,
    expected_label_policy_hash: str,
    change_ids: Iterable[str] | None = None,
) -> tuple[int, int]:
    """Persist complete rich webhook units and safely retry convergence.

    Callers may run this after a batch or periodically.  Every eligible stored
    event must still match independently supplied repository and label-policy
    pins.  The helper never creates a final-only/open unit and never replaces an
    archive or another immutable unit.  Repeated calls are idempotent.
    """

    repository_pin = _validate_expected_repository_id(expected_repository_id)
    policy_pin = _validate_expected_label_policy_hash(expected_label_policy_hash)
    requested = None if change_ids is None else frozenset(change_ids)
    if requested is not None:
        for change_id in requested:
            validate_subject(change_id)
    try:
        event_path = events_path(root)
        unit_path = change_units_path(root)
        events, units = load_history_snapshot(event_path, unit_path)
        _validate_history_capture_pins(
            events,
            units,
            expected_repository_id=repository_pin,
            expected_label_policy_hash=policy_pin,
        )
        webhook_change_ids = frozenset(
            event.change_id
            for event in events
            if event.change_id is not None
            and event.data.get("adapter") == GITHUB_WEBHOOK_ADAPTER_VERSION
        )
        eligible_change_ids = webhook_change_ids
        if requested is not None:
            eligible_change_ids = frozenset(webhook_change_ids.intersection(requested))
        if not eligible_change_ids:
            return (0, 0)
        candidates = _complete_github_webhook_units(events, units, eligible_change_ids)
        if not candidates:
            return (0, 0)
        _event_counts, unit_counts = upsert_history_batch(
            event_path,
            (),
            unit_path,
            candidates,
        )
        return unit_counts
    except ModelError as exc:
        if isinstance(exc, GitHubWebhookCaptureError):
            raise
        raise GitHubWebhookCaptureError(f"cannot finalize GitHub capture units: {exc}") from exc


def _capture_inbox_paths(inbox: Path, *, max_bundles: int) -> tuple[Path, ...]:
    if (
        isinstance(max_bundles, bool)
        or not isinstance(max_bundles, int)
        or not 1 <= max_bundles <= MAX_GITHUB_CAPTURE_BUNDLES
    ):
        raise GitHubWebhookCaptureError(
            f"max_bundles must be between 1 and {MAX_GITHUB_CAPTURE_BUNDLES}"
        )
    _validate_output_directory(inbox)
    paths: list[Path] = []
    total_bytes = 0
    try:
        with os.scandir(inbox) as entries:
            for entry in entries:
                if len(paths) >= max_bundles:
                    raise GitHubWebhookCaptureError(
                        f"GitHub capture inbox exceeds max_bundles={max_bundles}"
                    )
                if not _BUNDLE_NAME_RE.fullmatch(entry.name):
                    raise GitHubWebhookCaptureError(
                        f"GitHub capture inbox contains unsafe/non-bundle entry {entry.name!r}"
                    )
                if entry.is_symlink():
                    raise GitHubWebhookCaptureError(
                        f"GitHub capture inbox bundle must not be a symlink: {entry.name!r}"
                    )
                metadata = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(metadata.st_mode):
                    raise GitHubWebhookCaptureError(
                        f"GitHub capture inbox entry must be a regular file: {entry.name!r}"
                    )
                total_bytes += metadata.st_size
                if total_bytes > MAX_GITHUB_CAPTURE_BATCH_BYTES:
                    raise GitHubWebhookCaptureError(
                        "GitHub capture inbox exceeds cumulative byte limit "
                        f"{MAX_GITHUB_CAPTURE_BATCH_BYTES} at {entry.name!r}"
                    )
                paths.append(inbox / entry.name)
    except OSError as exc:
        raise GitHubWebhookCaptureError(f"cannot scan GitHub capture inbox {inbox}: {exc}") from exc
    return tuple(sorted(paths, key=lambda path: path.name))


def ingest_github_capture_directory(
    root: Path,
    inbox: Path,
    *,
    expected_repository_id: str,
    expected_label_policy_hash: str,
    envelope_key: bytes,
    max_bundles: int = 1_000,
) -> GitHubCaptureDirectoryReport:
    """Verify and atomically ingest one bounded immutable bundle directory.

    Every file is loaded and MAC-verified, pinned to the caller's configured
    RuleLoom repository ID and independently frozen label-policy hash, and all
    delivery/event/unit conflicts are preflighted before the single history
    transaction.  Files are never deleted, renamed, or moved.  A failing
    filename is included in the raised error and no prefix is reported as
    successful.
    """

    repository_pin = _validate_expected_repository_id(expected_repository_id)
    policy_pin = _validate_expected_label_policy_hash(expected_label_policy_hash)
    envelope_secret = _validate_key(envelope_key, "GitHub capture envelope key")
    paths = _capture_inbox_paths(inbox, max_bundles=max_bundles)
    if not paths:
        return GitHubCaptureDirectoryReport(
            processed_bundles=(),
            unique_deliveries=0,
            duplicate_replays=0,
            events_inserted=0,
            events_unchanged=0,
            units_inserted=0,
            units_unchanged=0,
        )

    unique_by_delivery: dict[tuple[str, int, str], GitHubWebhookCapture] = {}
    duplicate_replays = 0
    for path in paths:
        try:
            capture = load_github_capture_bundle(path, envelope_key=envelope_secret)
        except (GitHubWebhookCaptureError, OSError) as exc:
            raise GitHubWebhookCaptureError(
                f"GitHub capture bundle {path.name!r} failed verification before ingestion; "
                f"no changes were written: {exc}"
            ) from exc
        try:
            _validate_capture_pins(
                capture,
                expected_repository_id=repository_pin,
                expected_label_policy_hash=policy_pin,
            )
        except GitHubWebhookCaptureError as exc:
            raise GitHubWebhookCaptureError(
                f"GitHub capture bundle {path.name!r} failed experiment pinning; "
                f"no changes were written: {exc}"
            ) from exc
        identity = (
            capture.transport,
            capture.provider_repository_id,
            capture.delivery_id,
        )
        previous = unique_by_delivery.get(identity)
        if previous is None:
            unique_by_delivery[identity] = capture
        elif previous.envelope_sha256 == capture.envelope_sha256:
            duplicate_replays += 1
        else:
            raise GitHubWebhookCaptureError(
                f"GitHub capture bundle {path.name!r} conflicts with an earlier bundle for "
                f"delivery {capture.delivery_id!r}; no changes were written"
            )

    captures = tuple(unique_by_delivery.values())
    boundaries = {
        (
            capture.repository_id,
            capture.provider_repository_id,
            capture.provider_repository_key,
        )
        for capture in captures
    }
    if len(boundaries) != 1:
        raise GitHubWebhookCaptureError(
            "GitHub capture inbox crosses repository identities; no changes were written"
        )
    incoming_events = tuple(event for capture in captures for event in capture.events)
    if len(incoming_events) > MAX_GITHUB_CAPTURE_BATCH_EVENTS:
        raise GitHubWebhookCaptureError(
            "GitHub capture inbox exceeds cumulative normalized event limit "
            f"{MAX_GITHUB_CAPTURE_BATCH_EVENTS}; no changes were written"
        )
    change_ids = frozenset(
        event.change_id for event in incoming_events if event.change_id is not None
    )
    try:
        event_path = events_path(root)
        unit_path = change_units_path(root)
        existing_events, existing_units = load_history_snapshot(event_path, unit_path)
        _validate_history_capture_pins(
            existing_events,
            existing_units,
            expected_repository_id=repository_pin,
            expected_label_policy_hash=policy_pin,
        )
        combined_events = _merge_event_preview(existing_events, incoming_events)
        candidate_units = _complete_github_webhook_units(
            combined_events,
            existing_units,
            change_ids,
        )
        event_counts, unit_counts = upsert_history_batch(
            event_path,
            incoming_events,
            unit_path,
            candidate_units,
        )
    except ModelError as exc:
        if isinstance(exc, GitHubWebhookCaptureError):
            raise
        raise GitHubWebhookCaptureError(
            f"GitHub capture inbox failed atomic ingestion; no success was reported: {exc}"
        ) from exc

    return GitHubCaptureDirectoryReport(
        processed_bundles=tuple(path.name for path in paths),
        unique_deliveries=len(captures),
        duplicate_replays=duplicate_replays,
        events_inserted=event_counts[0],
        events_unchanged=event_counts[1],
        units_inserted=unit_counts[0],
        units_unchanged=unit_counts[1],
    )
