"""Strict, outcome-independent declarations for hand-authored risk rules.

This module deliberately does not interpret prose from ``AGENTS.md``,
``CLAUDE.md``, or any other repository document.  A human must translate an
existing assertion into the same explicit ``RuleSet`` representation used by
RuleLoom's learners.  Repository documents may be attached as hashed source
references so the translation remains auditable.

Manual-rule history audits are always post-hoc and exploratory.  They can
describe trigger coverage and association with mature outcomes, but they are
not prospective evidence and must not satisfy approval gates.
"""

from __future__ import annotations

import hashlib
import math
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import cast

from ruleloom.config import RuleLoomConfig
from ruleloom.evaluation import evaluate, labeled
from ruleloom.models import (
    Candidate,
    HornClause,
    JsonObject,
    JsonValue,
    LabelValue,
    Metrics,
    ModelError,
    Observation,
    RuleSet,
    canonical_json,
    content_hash,
    parse_timestamp,
    validate_json_value,
    validate_subject,
    validate_timestamp,
)
from ruleloom.packs import (
    matches_pack_version,
    validate_persisted_extraction,
    validate_policy_pack_contract,
)
from ruleloom.storage import project_path, read_json

MANUAL_RULE_SCHEMA_VERSION = 1
MANUAL_RULE_ENGINE_VERSION = "ruleloom-manual-audit/0.1"
MANUAL_RULE_CLAIM_KIND = "risk_trigger"
MANUAL_RULE_EVALUATION_MODE = "retrospective_post_hoc_exploratory"

_MAX_POLICY_SUMMARY_CHARS = 500
_MAX_SOURCE_PATH_CHARS = 512
_MAX_SOURCE_REFS = 16
_MAX_SOURCE_BYTES = 1024 * 1024
_MAX_SOURCE_LINES = 100_000
_MAX_SOURCE_SPAN_LINES = 500
_MAX_EXAMPLE_IDS = 20
_MAX_MANUAL_RULES = 10
_MAX_MANUAL_BODY = 4
_HASH_RE = frozenset("0123456789abcdef")
_GENERATED_SOURCE_PREFIXES = (
    ".agents/skills/ruleloom/",
    ".claude/skills/ruleloom/",
    ".ruleloom/",
)


def _reject_unknown(value: JsonObject, allowed: set[str], name: str) -> None:
    unknown = set(value).difference(allowed)
    if unknown:
        raise ModelError(f"unknown {name} fields: {', '.join(sorted(unknown))}")


def _string(value: JsonValue, name: str) -> str:
    if not isinstance(value, str):
        raise ModelError(f"{name} must be a string")
    return value


def _integer(value: JsonValue, name: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ModelError(f"{name} must be an integer >= {minimum}")
    return value


def _object(value: JsonValue, name: str) -> JsonObject:
    if not isinstance(value, dict):
        raise ModelError(f"{name} must be an object")
    return value


def _sha256(value: str, name: str) -> str:
    if len(value) != 64 or any(character not in _HASH_RE for character in value):
        raise ModelError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _single_line(value: str, name: str, *, maximum: int) -> str:
    if (
        not value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ModelError(
            f"{name} must be a non-empty single-line string of at most {maximum} characters"
        )
    return value


def _source_path(value: str) -> str:
    if (
        not value
        or len(value) > _MAX_SOURCE_PATH_CHARS
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ModelError("manual rule source path is invalid")
    pure = PurePosixPath(value)
    folded_value = value.casefold()
    if (
        pure.is_absolute()
        or value != pure.as_posix()
        or value in {".", ".."}
        or ".." in pure.parts
        or pure.parts[0].casefold() == ".git"
        or any(folded_value.startswith(prefix.casefold()) for prefix in _GENERATED_SOURCE_PREFIXES)
    ):
        raise ModelError(
            "manual rule source path must be a normalized repository-relative, non-generated path"
        )
    return value


def _canonical_rules(rules: RuleSet) -> RuleSet:
    clauses = tuple(
        sorted(
            (HornClause(clause.target, tuple(sorted(clause.body))) for clause in rules.clauses),
            key=lambda clause: clause.signature,
        )
    )
    return RuleSet(rules.target, clauses)


@dataclass(frozen=True, slots=True)
class ManualRuleSourceRef:
    """A bounded line span in a repository document; its prose is never parsed."""

    path: str
    start_line: int | None = None
    end_line: int | None = None

    def __post_init__(self) -> None:
        _source_path(self.path)
        if (self.start_line is None) != (self.end_line is None):
            raise ModelError("manual rule source line range requires both start_line and end_line")
        if self.start_line is None:
            return
        start = self.start_line
        end = cast(int, self.end_line)
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or start < 1
            or end < start
            or end - start + 1 > _MAX_SOURCE_SPAN_LINES
        ):
            raise ModelError(
                "manual rule source line range must be positive, ordered, and at most "
                f"{_MAX_SOURCE_SPAN_LINES} lines"
            )

    def to_dict(self) -> JsonObject:
        value: JsonObject = {"path": self.path}
        if self.start_line is not None:
            value["start_line"] = self.start_line
            value["end_line"] = self.end_line
        return value

    @classmethod
    def from_dict(cls, value: JsonObject) -> ManualRuleSourceRef:
        _reject_unknown(value, {"path", "start_line", "end_line"}, "manual rule source")
        raw_start = value.get("start_line")
        raw_end = value.get("end_line")
        return cls(
            path=_string(value.get("path"), "manual rule source path"),
            start_line=(
                None if raw_start is None else _integer(raw_start, "manual rule source start_line")
            ),
            end_line=(
                None if raw_end is None else _integer(raw_end, "manual rule source end_line")
            ),
        )


@dataclass(frozen=True, slots=True)
class ManualRuleManifest:
    """Explicit human translation of one existing assertion into Horn clauses."""

    policy_id: str
    revision: int
    summary: str
    rules: RuleSet
    sources: tuple[ManualRuleSourceRef, ...] = ()
    claim_kind: str = MANUAL_RULE_CLAIM_KIND
    schema_version: int = MANUAL_RULE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != MANUAL_RULE_SCHEMA_VERSION:
            raise ModelError("unsupported manual rule manifest schema version")
        validate_subject(self.policy_id)
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 1
        ):
            raise ModelError("manual rule revision must be an integer >= 1")
        _single_line(self.summary, "manual rule summary", maximum=_MAX_POLICY_SUMMARY_CHARS)
        if self.claim_kind != MANUAL_RULE_CLAIM_KIND:
            raise ModelError(
                "manual rule claim_kind must be 'risk_trigger'; prescriptive actions and "
                "causal claims are not supported"
            )
        canonical_rules = _canonical_rules(self.rules)
        if not canonical_rules.clauses:
            raise ModelError("manual rule manifest must contain at least one clause")
        if len(canonical_rules.clauses) > _MAX_MANUAL_RULES:
            raise ModelError(f"manual rule manifest exceeds {_MAX_MANUAL_RULES} clauses")
        if any(len(clause.body) > _MAX_MANUAL_BODY for clause in canonical_rules.clauses):
            raise ModelError(f"manual rule clause exceeds {_MAX_MANUAL_BODY} literals")
        signatures = [clause.signature for clause in canonical_rules.clauses]
        if len(signatures) != len(set(signatures)):
            raise ModelError("manual rule manifest contains duplicate clauses")
        if len(self.sources) > _MAX_SOURCE_REFS:
            raise ModelError(f"manual rule manifest exceeds {_MAX_SOURCE_REFS} source references")
        canonical_sources = tuple(
            sorted(
                self.sources,
                key=lambda item: (
                    item.path,
                    item.start_line is not None,
                    item.start_line or 0,
                    item.end_line or 0,
                ),
            )
        )
        if len(canonical_sources) != len(set(canonical_sources)):
            raise ModelError("manual rule manifest contains duplicate source references")
        object.__setattr__(self, "rules", canonical_rules)
        object.__setattr__(self, "sources", canonical_sources)

    def to_dict(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "revision": self.revision,
            "claim_kind": self.claim_kind,
            "summary": self.summary,
            "rules": self.rules.to_dict(),
            "sources": [item.to_dict() for item in self.sources],
        }

    @classmethod
    def from_dict(cls, value: JsonObject) -> ManualRuleManifest:
        _reject_unknown(
            value,
            {
                "schema_version",
                "policy_id",
                "revision",
                "claim_kind",
                "summary",
                "rules",
                "sources",
            },
            "manual rule manifest",
        )
        raw_sources = value.get("sources", [])
        if not isinstance(raw_sources, list):
            raise ModelError("manual rule sources must be an array")
        sources = tuple(
            ManualRuleSourceRef.from_dict(_object(item, "manual rule source"))
            for item in raw_sources
        )
        return cls(
            schema_version=_integer(
                value.get("schema_version"),
                "manual rule schema_version",
            ),
            policy_id=_string(value.get("policy_id"), "manual rule policy_id"),
            revision=_integer(value.get("revision"), "manual rule revision"),
            claim_kind=_string(value.get("claim_kind"), "manual rule claim_kind"),
            summary=_string(value.get("summary"), "manual rule summary"),
            rules=RuleSet.from_dict(_object(value.get("rules"), "manual rule rules")),
            sources=sources,
        )


@dataclass(frozen=True, slots=True, order=True)
class ManualRuleSourceSnapshot:
    ref: ManualRuleSourceRef
    document_sha256: str
    excerpt_sha256: str
    size_bytes: int
    line_count: int

    def __post_init__(self) -> None:
        _sha256(self.document_sha256, "manual rule source document_sha256")
        _sha256(self.excerpt_sha256, "manual rule source excerpt_sha256")
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or not 0 <= self.size_bytes <= _MAX_SOURCE_BYTES
        ):
            raise ModelError("manual rule source size_bytes is invalid")
        if (
            isinstance(self.line_count, bool)
            or not isinstance(self.line_count, int)
            or not 0 <= self.line_count <= _MAX_SOURCE_LINES
        ):
            raise ModelError("manual rule source line_count is invalid")

    def to_dict(self) -> JsonObject:
        return {
            "ref": self.ref.to_dict(),
            "document_sha256": self.document_sha256,
            "excerpt_sha256": self.excerpt_sha256,
            "size_bytes": self.size_bytes,
            "line_count": self.line_count,
        }

    @classmethod
    def from_dict(cls, value: JsonObject) -> ManualRuleSourceSnapshot:
        _reject_unknown(
            value,
            {"ref", "document_sha256", "excerpt_sha256", "size_bytes", "line_count"},
            "manual rule source snapshot",
        )
        return cls(
            ref=ManualRuleSourceRef.from_dict(
                _object(value.get("ref"), "manual rule source snapshot ref")
            ),
            document_sha256=_string(
                value.get("document_sha256"),
                "manual rule source document_sha256",
            ),
            excerpt_sha256=_string(
                value.get("excerpt_sha256"),
                "manual rule source excerpt_sha256",
            ),
            size_bytes=_integer(
                value.get("size_bytes"),
                "manual rule source size_bytes",
                minimum=0,
            ),
            line_count=_integer(
                value.get("line_count"),
                "manual rule source line_count",
                minimum=0,
            ),
        )


def _snapshot_source(root: Path, ref: ManualRuleSourceRef) -> ManualRuleSourceSnapshot:
    path = project_path(root, ref.path)
    try:
        source_stat = path.stat()
    except OSError as exc:
        raise ModelError(f"cannot read manual rule source {ref.path}: {exc}") from exc
    if not stat.S_ISREG(source_stat.st_mode):
        raise ModelError(f"manual rule source must be a regular file: {ref.path}")
    if source_stat.st_size > _MAX_SOURCE_BYTES:
        raise ModelError(f"manual rule source exceeds {_MAX_SOURCE_BYTES} bytes: {ref.path}")
    try:
        raw = path.read_bytes()
        raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ModelError(f"manual rule source must be readable UTF-8: {ref.path}: {exc}") from exc
    if len(raw) > _MAX_SOURCE_BYTES:
        raise ModelError(f"manual rule source exceeds {_MAX_SOURCE_BYTES} bytes: {ref.path}")
    lines = raw.split(b"\n") if raw else []
    if raw.endswith(b"\n"):
        lines.pop()
    if len(lines) > _MAX_SOURCE_LINES:
        raise ModelError(f"manual rule source exceeds {_MAX_SOURCE_LINES} lines: {ref.path}")
    if ref.start_line is None:
        excerpt = raw
    else:
        end = cast(int, ref.end_line)
        if end > len(lines):
            raise ModelError(
                f"manual rule source range {ref.start_line}:{end} exceeds "
                f"{len(lines)} lines in {ref.path}"
            )
        excerpt = b"\n".join(lines[ref.start_line - 1 : end])
    return ManualRuleSourceSnapshot(
        ref=ref,
        document_sha256=hashlib.sha256(raw).hexdigest(),
        excerpt_sha256=hashlib.sha256(excerpt).hexdigest(),
        size_bytes=len(raw),
        line_count=len(lines),
    )


def snapshot_manual_rule_sources(
    root: Path, manifest: ManualRuleManifest
) -> tuple[ManualRuleSourceSnapshot, ...]:
    """Hash explicit source documents without interpreting or executing their prose."""

    return tuple(_snapshot_source(root, ref) for ref in manifest.sources)


@dataclass(frozen=True, slots=True)
class ManualRuleDeclaration:
    """Immutable rule declaration bound to one repository evidence protocol."""

    id: str
    declared_at: str
    manifest: ManualRuleManifest
    sources: tuple[ManualRuleSourceSnapshot, ...]
    config_hash: str
    evidence_protocol_hash: str
    repository_id: str
    pack: str
    pack_version: int
    extractor: str
    pack_config_hash: str | None = None
    schema_version: int = MANUAL_RULE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != MANUAL_RULE_SCHEMA_VERSION:
            raise ModelError("unsupported manual rule declaration schema version")
        validate_subject(self.id)
        validate_timestamp(self.declared_at)
        _sha256(self.config_hash, "manual rule declaration config_hash")
        _sha256(
            self.evidence_protocol_hash,
            "manual rule declaration evidence_protocol_hash",
        )
        if self.pack_config_hash is not None:
            _sha256(self.pack_config_hash, "manual rule declaration pack_config_hash")
        if not self.repository_id or not self.pack or not self.extractor:
            raise ModelError("manual rule declaration provenance cannot be empty")
        if isinstance(self.pack_version, bool) or not isinstance(self.pack_version, int):
            raise ModelError("manual rule declaration pack_version must be an integer")
        expected_refs = tuple(item.ref for item in self.sources)
        if expected_refs != self.manifest.sources:
            raise ModelError("manual rule declaration source snapshots do not match its manifest")
        if self.id != self.expected_id:
            raise ModelError(
                f"manual rule declaration id {self.id!r} does not match {self.expected_id!r}"
            )

    def identity_payload(self) -> JsonObject:
        value: JsonObject = {
            "schema_version": self.schema_version,
            "declared_at": self.declared_at,
            "manifest": self.manifest.to_dict(),
            "sources": [item.to_dict() for item in self.sources],
            "config_hash": self.config_hash,
            "evidence_protocol_hash": self.evidence_protocol_hash,
            "repository_id": self.repository_id,
            "pack": self.pack,
            "pack_version": self.pack_version,
            "extractor": self.extractor,
            "pack_config_hash": self.pack_config_hash,
        }
        return value

    @property
    def expected_id(self) -> str:
        return f"manual-{content_hash(self.identity_payload())[:24]}"

    def to_dict(self) -> JsonObject:
        return {"id": self.id, **self.identity_payload()}

    @classmethod
    def from_dict(cls, value: JsonObject) -> ManualRuleDeclaration:
        _reject_unknown(
            value,
            {
                "id",
                "schema_version",
                "declared_at",
                "manifest",
                "sources",
                "config_hash",
                "evidence_protocol_hash",
                "repository_id",
                "pack",
                "pack_version",
                "extractor",
                "pack_config_hash",
            },
            "manual rule declaration",
        )
        raw_sources = value.get("sources")
        if not isinstance(raw_sources, list):
            raise ModelError("manual rule declaration sources must be an array")
        raw_pack_config_hash = value.get("pack_config_hash")
        if raw_pack_config_hash is not None and not isinstance(raw_pack_config_hash, str):
            raise ModelError("manual rule declaration pack_config_hash must be a string or null")
        return cls(
            id=_string(value.get("id"), "manual rule declaration id"),
            schema_version=_integer(
                value.get("schema_version"),
                "manual rule declaration schema_version",
            ),
            declared_at=_string(
                value.get("declared_at"),
                "manual rule declaration declared_at",
            ),
            manifest=ManualRuleManifest.from_dict(
                _object(value.get("manifest"), "manual rule declaration manifest")
            ),
            sources=tuple(
                ManualRuleSourceSnapshot.from_dict(_object(item, "manual rule declaration source"))
                for item in raw_sources
            ),
            config_hash=_string(
                value.get("config_hash"),
                "manual rule declaration config_hash",
            ),
            evidence_protocol_hash=_string(
                value.get("evidence_protocol_hash"),
                "manual rule declaration evidence_protocol_hash",
            ),
            repository_id=_string(
                value.get("repository_id"),
                "manual rule declaration repository_id",
            ),
            pack=_string(value.get("pack"), "manual rule declaration pack"),
            pack_version=_integer(
                value.get("pack_version"),
                "manual rule declaration pack_version",
            ),
            extractor=_string(
                value.get("extractor"),
                "manual rule declaration extractor",
            ),
            pack_config_hash=raw_pack_config_hash,
        )


def declare_manual_rule(
    root: Path,
    config: RuleLoomConfig,
    manifest: ManualRuleManifest,
    *,
    declared_at: datetime | None = None,
) -> ManualRuleDeclaration:
    """Freeze a hand-authored rule without loading observations or outcome labels."""

    instant = declared_at or datetime.now(UTC)
    if instant.tzinfo is None:
        raise ModelError("manual rule declared_at must include a timezone")
    if manifest.rules.target != config.target:
        raise ModelError(
            f"manual rule target {manifest.rules.target!r} does not match configured target "
            f"{config.target!r}"
        )
    if len(manifest.rules.clauses) > config.learner.max_rules:
        raise ModelError("manual rule exceeds the configured maximum clause count")
    if any(len(clause.body) > config.learner.max_body for clause in manifest.rules.clauses):
        raise ModelError("manual rule exceeds the configured maximum body size")
    if not config.learner.allow_negation and any(
        literal.negated for clause in manifest.rules.clauses for literal in clause.body
    ):
        raise ModelError("manual rule uses negation but learner.allow_negation is false")
    descriptor = config.resolved_pack
    metadata: JsonObject = {
        "pack": config.pack,
        "pack_version": config.pack_version,
        "extractors": [descriptor.extractor],
        "evidence_protocol_hash": config.evidence_protocol_hash,
    }
    if descriptor.configuration_hash is not None:
        metadata["pack_config_hash"] = descriptor.configuration_hash
    validate_policy_pack_contract(
        descriptor,
        metadata,
        {literal.predicate for clause in manifest.rules.clauses for literal in clause.body},
        schema_version=config.schema_version,
        evidence_protocol_hash=config.evidence_protocol_hash,
        subject=f"manual rule {manifest.policy_id}",
    )
    declared_text = instant.isoformat().replace("+00:00", "Z")
    sources = snapshot_manual_rule_sources(root, manifest)
    identity_payload: JsonObject = {
        "schema_version": MANUAL_RULE_SCHEMA_VERSION,
        "declared_at": declared_text,
        "manifest": manifest.to_dict(),
        "sources": [item.to_dict() for item in sources],
        "config_hash": config.hash,
        "evidence_protocol_hash": config.evidence_protocol_hash,
        "repository_id": config.protocol.repository_id,
        "pack": config.pack,
        "pack_version": config.pack_version,
        "extractor": descriptor.extractor,
        "pack_config_hash": descriptor.configuration_hash,
    }
    return ManualRuleDeclaration(
        id=f"manual-{content_hash(identity_payload)[:24]}",
        declared_at=declared_text,
        manifest=manifest,
        sources=sources,
        config_hash=config.hash,
        evidence_protocol_hash=config.evidence_protocol_hash,
        repository_id=config.protocol.repository_id,
        pack=config.pack,
        pack_version=config.pack_version,
        extractor=descriptor.extractor,
        pack_config_hash=descriptor.configuration_hash,
    )


def load_manual_rule_manifest(path: Path) -> ManualRuleManifest:
    """Load a strict explicit manifest; repository prose is never interpreted."""

    if path.is_symlink():
        raise ModelError(f"manual rule manifest must not be a symlink: {path}")
    return ManualRuleManifest.from_dict(read_json(path))


@dataclass(frozen=True, slots=True)
class ManualRuleSourceStatus:
    ref: ManualRuleSourceRef
    status: str
    current_document_sha256: str | None = None
    current_excerpt_sha256: str | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if self.status not in {"unchanged", "changed", "unavailable"}:
            raise ModelError(f"unsupported manual rule source status: {self.status!r}")
        if self.current_document_sha256 is not None:
            _sha256(self.current_document_sha256, "current source document_sha256")
        if self.current_excerpt_sha256 is not None:
            _sha256(self.current_excerpt_sha256, "current source excerpt_sha256")
        hashes_available = (
            self.current_document_sha256 is not None and self.current_excerpt_sha256 is not None
        )
        hashes_absent = self.current_document_sha256 is None and self.current_excerpt_sha256 is None
        if not hashes_available and not hashes_absent:
            raise ModelError(
                "manual rule source status must provide both current hashes or neither"
            )
        if self.status in {"unchanged", "changed"} and not hashes_available:
            raise ModelError("available manual rule source status requires current hashes")
        if self.status == "unavailable" and (not hashes_absent or not self.reason):
            raise ModelError(
                "unavailable manual rule source status requires a reason and no hashes"
            )

    def to_dict(self) -> JsonObject:
        return {
            "ref": self.ref.to_dict(),
            "status": self.status,
            "current_document_sha256": self.current_document_sha256,
            "current_excerpt_sha256": self.current_excerpt_sha256,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, value: JsonObject) -> ManualRuleSourceStatus:
        _reject_unknown(
            value,
            {
                "ref",
                "status",
                "current_document_sha256",
                "current_excerpt_sha256",
                "reason",
            },
            "manual rule source status",
        )
        raw_document_hash = value.get("current_document_sha256")
        raw_excerpt_hash = value.get("current_excerpt_sha256")
        if raw_document_hash is not None and not isinstance(raw_document_hash, str):
            raise ModelError("current source document_sha256 must be a string or null")
        if raw_excerpt_hash is not None and not isinstance(raw_excerpt_hash, str):
            raise ModelError("current source excerpt_sha256 must be a string or null")
        return cls(
            ref=ManualRuleSourceRef.from_dict(
                _object(value.get("ref"), "manual rule source status ref")
            ),
            status=_string(value.get("status"), "manual rule source status"),
            current_document_sha256=raw_document_hash,
            current_excerpt_sha256=raw_excerpt_hash,
            reason=_string(value.get("reason"), "manual rule source status reason"),
        )


def verify_manual_rule_sources(
    root: Path, declaration: ManualRuleDeclaration
) -> tuple[ManualRuleSourceStatus, ...]:
    """Report source drift while preserving the declaration's original snapshot."""

    statuses: list[ManualRuleSourceStatus] = []
    for expected in declaration.sources:
        try:
            current = _snapshot_source(root, expected.ref)
        except ModelError as exc:
            statuses.append(
                ManualRuleSourceStatus(
                    ref=expected.ref,
                    status="unavailable",
                    reason=str(exc),
                )
            )
            continue
        unchanged = (
            current.document_sha256 == expected.document_sha256
            and current.excerpt_sha256 == expected.excerpt_sha256
        )
        statuses.append(
            ManualRuleSourceStatus(
                ref=expected.ref,
                status="unchanged" if unchanged else "changed",
                current_document_sha256=current.document_sha256,
                current_excerpt_sha256=current.excerpt_sha256,
            )
        )
    return tuple(statuses)


@dataclass(frozen=True, slots=True)
class ManualClauseAudit:
    signature: str
    matched_observations: int
    match_rate: float
    mature_matches: int
    metrics: Metrics
    example_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _single_line(self.signature, "manual clause audit signature", maximum=1_000)
        for name, value in (
            ("matched_observations", self.matched_observations),
            ("mature_matches", self.mature_matches),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ModelError(f"manual clause audit {name} must be a non-negative integer")
        if self.mature_matches > self.matched_observations:
            raise ModelError("manual clause audit mature_matches exceeds matched_observations")
        if not 0 <= self.match_rate <= 1 or not math.isfinite(self.match_rate):
            raise ModelError("manual clause audit match_rate must be between 0 and 1")
        if len(self.example_ids) > _MAX_EXAMPLE_IDS:
            raise ModelError(f"manual clause audit exceeds {_MAX_EXAMPLE_IDS} example ids")
        if len(set(self.example_ids)) != len(self.example_ids):
            raise ModelError("manual clause audit example ids cannot contain duplicates")
        for example_id in self.example_ids:
            validate_subject(example_id)

    def to_dict(self) -> JsonObject:
        return {
            "signature": self.signature,
            "matched_observations": self.matched_observations,
            "match_rate": self.match_rate,
            "mature_matches": self.mature_matches,
            "metrics": self.metrics.to_dict(),
            "example_ids": list(self.example_ids),
        }

    @classmethod
    def from_dict(cls, value: JsonObject) -> ManualClauseAudit:
        _reject_unknown(
            value,
            {
                "signature",
                "matched_observations",
                "match_rate",
                "mature_matches",
                "metrics",
                "example_ids",
            },
            "manual clause audit",
        )
        raw_rate = value.get("match_rate")
        if isinstance(raw_rate, bool) or not isinstance(raw_rate, (int, float)):
            raise ModelError("manual clause audit match_rate must be a number")
        raw_examples = value.get("example_ids")
        if not isinstance(raw_examples, list) or not all(
            isinstance(item, str) for item in raw_examples
        ):
            raise ModelError("manual clause audit example_ids must be an array of strings")
        return cls(
            signature=_string(value.get("signature"), "manual clause audit signature"),
            matched_observations=_integer(
                value.get("matched_observations"),
                "manual clause audit matched_observations",
                minimum=0,
            ),
            match_rate=float(raw_rate),
            mature_matches=_integer(
                value.get("mature_matches"),
                "manual clause audit mature_matches",
                minimum=0,
            ),
            metrics=Metrics.from_dict(_object(value.get("metrics"), "manual clause audit metrics")),
            example_ids=tuple(cast(list[str], raw_examples)),
        )


@dataclass(frozen=True, slots=True)
class ManualRuleAudit:
    declaration_id: str
    audited_at: str
    dataset_hash: str
    observations: int
    matched_observations: int
    match_rate: float
    mature_labels: int
    positive: int
    negative: int
    unknown_or_censored: int
    metrics: Metrics
    baselines: dict[str, Metrics]
    clauses: tuple[ManualClauseAudit, ...]
    source_statuses: tuple[ManualRuleSourceStatus, ...]
    warnings: tuple[str, ...]
    evaluation_mode: str = MANUAL_RULE_EVALUATION_MODE
    confirmatory: bool = False
    engine_version: str = MANUAL_RULE_ENGINE_VERSION
    schema_version: int = MANUAL_RULE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != MANUAL_RULE_SCHEMA_VERSION:
            raise ModelError("unsupported manual rule audit schema version")
        validate_subject(self.declaration_id)
        validate_timestamp(self.audited_at)
        _sha256(self.dataset_hash, "manual rule audit dataset_hash")
        if self.evaluation_mode != MANUAL_RULE_EVALUATION_MODE or self.confirmatory:
            raise ModelError("manual rule history audits must remain post-hoc and non-confirmatory")
        if self.engine_version != MANUAL_RULE_ENGINE_VERSION:
            raise ModelError("unsupported manual rule audit engine version")
        counts = {
            "observations": self.observations,
            "matched_observations": self.matched_observations,
            "mature_labels": self.mature_labels,
            "positive": self.positive,
            "negative": self.negative,
            "unknown_or_censored": self.unknown_or_censored,
        }
        for name, value in counts.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ModelError(f"manual rule audit {name} must be a non-negative integer")
        if self.matched_observations > self.observations:
            raise ModelError("manual rule audit matched_observations exceeds observations")
        if self.mature_labels != self.positive + self.negative:
            raise ModelError("manual rule audit mature label counts are inconsistent")
        if self.observations != self.mature_labels + self.unknown_or_censored:
            raise ModelError("manual rule audit observation counts are inconsistent")
        if not 0 <= self.match_rate <= 1 or not math.isfinite(self.match_rate):
            raise ModelError("manual rule audit match_rate must be between 0 and 1")
        expected_rate = self.matched_observations / self.observations if self.observations else 0.0
        if not math.isclose(self.match_rate, expected_rate, rel_tol=1e-12, abs_tol=1e-12):
            raise ModelError("manual rule audit match_rate is inconsistent with its counts")
        total = (
            self.metrics.true_positive
            + self.metrics.false_positive
            + self.metrics.true_negative
            + self.metrics.false_negative
        )
        if total != self.mature_labels:
            raise ModelError("manual rule audit metrics do not cover the mature labels")
        if self.metrics.true_positive + self.metrics.false_negative != self.positive:
            raise ModelError("manual rule audit positive metrics are inconsistent")
        if self.metrics.true_negative + self.metrics.false_positive != self.negative:
            raise ModelError("manual rule audit negative metrics are inconsistent")
        if self.metrics.true_positive + self.metrics.false_positive > self.matched_observations:
            raise ModelError("manual rule audit metrics exceed matched observations")
        expected_baselines = {
            "never_alert": Metrics.from_counts(0, 0, self.negative, self.positive),
            "always_alert": Metrics.from_counts(self.positive, self.negative, 0, 0),
        }
        if self.baselines != expected_baselines:
            raise ModelError("manual rule audit baselines are inconsistent with its outcomes")
        signatures = [item.signature for item in self.clauses]
        if len(signatures) != len(set(signatures)):
            raise ModelError("manual rule audit contains duplicate clause signatures")
        for clause in self.clauses:
            clause_total = (
                clause.metrics.true_positive
                + clause.metrics.false_positive
                + clause.metrics.true_negative
                + clause.metrics.false_negative
            )
            if clause_total != self.mature_labels:
                raise ModelError("manual clause audit metrics do not cover the mature labels")
            if (
                clause.metrics.true_positive + clause.metrics.false_positive
                != clause.mature_matches
            ):
                raise ModelError("manual clause audit mature match count is inconsistent")
            expected_clause_rate = (
                clause.matched_observations / self.observations if self.observations else 0.0
            )
            if not math.isclose(
                clause.match_rate,
                expected_clause_rate,
                rel_tol=1e-12,
                abs_tol=1e-12,
            ):
                raise ModelError("manual clause audit match_rate is inconsistent with its counts")
        status_refs = [item.ref for item in self.source_statuses]
        if len(status_refs) != len(set(status_refs)):
            raise ModelError("manual rule audit contains duplicate source statuses")
        if not all(isinstance(item, str) and item for item in self.warnings):
            raise ModelError("manual rule audit warnings must be non-empty strings")

    def payload(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "engine_version": self.engine_version,
            "declaration_id": self.declaration_id,
            "audited_at": self.audited_at,
            "dataset_hash": self.dataset_hash,
            "evaluation_mode": self.evaluation_mode,
            "confirmatory": self.confirmatory,
            "observations": self.observations,
            "matched_observations": self.matched_observations,
            "match_rate": self.match_rate,
            "mature_labels": self.mature_labels,
            "positive": self.positive,
            "negative": self.negative,
            "unknown_or_censored": self.unknown_or_censored,
            "metrics": self.metrics.to_dict(),
            "baselines": {key: self.baselines[key].to_dict() for key in sorted(self.baselines)},
            "clauses": [item.to_dict() for item in self.clauses],
            "source_statuses": [item.to_dict() for item in self.source_statuses],
            "warnings": list(self.warnings),
        }

    @property
    def manifest_hash(self) -> str:
        return content_hash(self.payload())

    def to_dict(self) -> JsonObject:
        return {**self.payload(), "manifest_hash": self.manifest_hash}

    @classmethod
    def from_dict(cls, value: JsonObject) -> ManualRuleAudit:
        _reject_unknown(
            value,
            {
                "schema_version",
                "engine_version",
                "declaration_id",
                "audited_at",
                "dataset_hash",
                "evaluation_mode",
                "confirmatory",
                "observations",
                "matched_observations",
                "match_rate",
                "mature_labels",
                "positive",
                "negative",
                "unknown_or_censored",
                "metrics",
                "baselines",
                "clauses",
                "source_statuses",
                "warnings",
                "manifest_hash",
            },
            "manual rule audit",
        )
        raw_rate = value.get("match_rate")
        if isinstance(raw_rate, bool) or not isinstance(raw_rate, (int, float)):
            raise ModelError("manual rule audit match_rate must be a number")
        raw_confirmatory = value.get("confirmatory")
        if not isinstance(raw_confirmatory, bool):
            raise ModelError("manual rule audit confirmatory must be a boolean")
        raw_baselines = _object(value.get("baselines"), "manual rule audit baselines")
        raw_clauses = value.get("clauses")
        raw_statuses = value.get("source_statuses")
        raw_warnings = value.get("warnings")
        if not isinstance(raw_clauses, list):
            raise ModelError("manual rule audit clauses must be an array")
        if not isinstance(raw_statuses, list):
            raise ModelError("manual rule audit source_statuses must be an array")
        if not isinstance(raw_warnings, list) or not all(
            isinstance(item, str) for item in raw_warnings
        ):
            raise ModelError("manual rule audit warnings must be an array of strings")
        audit = cls(
            schema_version=_integer(
                value.get("schema_version"),
                "manual rule audit schema_version",
            ),
            engine_version=_string(
                value.get("engine_version"),
                "manual rule audit engine_version",
            ),
            declaration_id=_string(
                value.get("declaration_id"),
                "manual rule audit declaration_id",
            ),
            audited_at=_string(value.get("audited_at"), "manual rule audit audited_at"),
            dataset_hash=_string(
                value.get("dataset_hash"),
                "manual rule audit dataset_hash",
            ),
            evaluation_mode=_string(
                value.get("evaluation_mode"),
                "manual rule audit evaluation_mode",
            ),
            confirmatory=raw_confirmatory,
            observations=_integer(
                value.get("observations"),
                "manual rule audit observations",
                minimum=0,
            ),
            matched_observations=_integer(
                value.get("matched_observations"),
                "manual rule audit matched_observations",
                minimum=0,
            ),
            match_rate=float(raw_rate),
            mature_labels=_integer(
                value.get("mature_labels"),
                "manual rule audit mature_labels",
                minimum=0,
            ),
            positive=_integer(value.get("positive"), "manual rule audit positive", minimum=0),
            negative=_integer(value.get("negative"), "manual rule audit negative", minimum=0),
            unknown_or_censored=_integer(
                value.get("unknown_or_censored"),
                "manual rule audit unknown_or_censored",
                minimum=0,
            ),
            metrics=Metrics.from_dict(_object(value.get("metrics"), "manual rule audit metrics")),
            baselines={
                key: Metrics.from_dict(_object(item, f"manual rule audit baseline {key}"))
                for key, item in raw_baselines.items()
            },
            clauses=tuple(
                ManualClauseAudit.from_dict(_object(item, "manual clause audit"))
                for item in raw_clauses
            ),
            source_statuses=tuple(
                ManualRuleSourceStatus.from_dict(_object(item, "manual rule source status"))
                for item in raw_statuses
            ),
            warnings=tuple(cast(list[str], raw_warnings)),
        )
        raw_manifest_hash = _string(
            value.get("manifest_hash"),
            "manual rule audit manifest_hash",
        )
        _sha256(raw_manifest_hash, "manual rule audit manifest_hash")
        if raw_manifest_hash != audit.manifest_hash:
            raise ModelError("manual rule audit manifest_hash does not match its payload")
        return audit


def _dataset_hash(observations: list[Observation]) -> str:
    values = [item.to_dict() for item in sorted(observations, key=lambda item: item.id)]
    validate_json_value(values, "manual rule audit observations")
    return hashlib.sha256(canonical_json(cast(JsonValue, values)).encode()).hexdigest()


def _validate_declaration_config(
    declaration: ManualRuleDeclaration, config: RuleLoomConfig
) -> None:
    descriptor = config.resolved_pack
    if (
        declaration.config_hash != config.hash
        or declaration.evidence_protocol_hash != config.evidence_protocol_hash
        or declaration.repository_id != config.protocol.repository_id
        or declaration.pack != config.pack
        or declaration.pack_version != config.pack_version
        or declaration.extractor != descriptor.extractor
        or declaration.pack_config_hash != descriptor.configuration_hash
        or declaration.manifest.rules.target != config.target
    ):
        raise ModelError("manual rule declaration does not match the current configuration")
    if len(declaration.manifest.rules.clauses) > config.learner.max_rules:
        raise ModelError("manual rule declaration exceeds the configured maximum clause count")
    if any(
        len(clause.body) > config.learner.max_body for clause in declaration.manifest.rules.clauses
    ):
        raise ModelError("manual rule declaration exceeds the configured maximum body size")
    if not config.learner.allow_negation and any(
        literal.negated for clause in declaration.manifest.rules.clauses for literal in clause.body
    ):
        raise ModelError("manual rule declaration uses disabled negation")
    validate_policy_pack_contract(
        descriptor,
        {
            "pack": declaration.pack,
            "pack_version": declaration.pack_version,
            "extractors": [declaration.extractor],
            "evidence_protocol_hash": declaration.evidence_protocol_hash,
            "pack_config_hash": declaration.pack_config_hash,
        },
        {
            literal.predicate
            for clause in declaration.manifest.rules.clauses
            for literal in clause.body
        },
        schema_version=config.schema_version,
        evidence_protocol_hash=config.evidence_protocol_hash,
        subject=f"manual rule declaration {declaration.id}",
    )


def manual_candidate_from_audit(
    declaration: ManualRuleDeclaration,
    audit: ManualRuleAudit,
    config: RuleLoomConfig,
) -> Candidate:
    """Build the one canonical candidate representation for a manual declaration."""

    _validate_declaration_config(declaration, config)
    if audit.declaration_id != declaration.id:
        raise ModelError("manual rule audit does not match its declaration")
    if parse_timestamp(audit.audited_at) < parse_timestamp(declaration.declared_at):
        raise ModelError("manual rule audit predates its declaration")
    if tuple(item.ref for item in audit.source_statuses) != declaration.manifest.sources:
        raise ModelError("manual rule audit source statuses do not match its declaration")
    for status, snapshot in zip(audit.source_statuses, declaration.sources, strict=True):
        same_hashes = (
            status.current_document_sha256 == snapshot.document_sha256
            and status.current_excerpt_sha256 == snapshot.excerpt_sha256
        )
        if status.status == "unchanged" and not same_hashes:
            raise ModelError("manual rule audit unchanged source hashes do not match declaration")
        if status.status == "changed" and same_hashes:
            raise ModelError("manual rule audit changed source hashes still match declaration")
    clause_signatures = tuple(item.signature for item in audit.clauses)
    expected_signatures = tuple(clause.signature for clause in declaration.manifest.rules.clauses)
    if clause_signatures != expected_signatures:
        raise ModelError("manual rule audit clauses do not match its declaration")
    descriptor = config.resolved_pack
    metadata: JsonObject = {
        "candidate_origin": "manual_declaration",
        "pack": config.pack,
        "pack_version": config.pack_version,
        "repository_id": config.protocol.repository_id,
        "evidence_protocol_hash": config.evidence_protocol_hash,
        "extractors": [descriptor.extractor],
        "manual_declaration": declaration.to_dict(),
        "manual_audit": audit.to_dict(),
        "evaluation": {
            "method": MANUAL_RULE_EVALUATION_MODE,
            "confirmatory": False,
            "approval_basis": "prospective_shadow_only",
        },
    }
    if descriptor.configuration_hash is not None:
        metadata["pack_config_hash"] = descriptor.configuration_hash
    candidate = Candidate(
        id="cand-pending",
        created_at=audit.audited_at,
        engine="manual",
        engine_version=MANUAL_RULE_ENGINE_VERSION,
        dataset_hash=audit.dataset_hash,
        config_hash=config.hash,
        rules=declaration.manifest.rules,
        metrics={"historical": audit.metrics},
        baselines=dict(audit.baselines),
        stability=0.0,
        train_ids=(),
        test_ids=(),
        warnings=tuple(
            dict.fromkeys(
                (
                    *audit.warnings,
                    "manual rule: historical metrics cannot satisfy approval gates; "
                    "approval requires attributable prospective shadow evidence",
                )
            )
        ),
        metadata=metadata,
    )
    return candidate.with_identity()


def validate_manual_candidate(
    candidate: Candidate,
    config: RuleLoomConfig,
) -> tuple[ManualRuleDeclaration, ManualRuleAudit]:
    """Parse and verify a persisted manual candidate as a closed runtime contract.

    Review state is intentionally excluded: the candidate identity and local transition
    attestation already bind it separately. Everything that claims to describe how the
    manual policy was declared and audited is reconstructed and compared exactly.
    """

    if candidate.engine != "manual":
        raise ModelError("manual candidate validator requires engine 'manual'")
    candidate.validate_identity()
    if candidate.metadata.get("candidate_origin") != "manual_declaration":
        raise ModelError("manual candidate lacks declared-rule provenance")
    raw_declaration = candidate.metadata.get("manual_declaration")
    if not isinstance(raw_declaration, dict) or not all(
        isinstance(key, str) for key in raw_declaration
    ):
        raise ModelError("manual candidate lacks its immutable declaration")
    raw_audit = candidate.metadata.get("manual_audit")
    if not isinstance(raw_audit, dict) or not all(isinstance(key, str) for key in raw_audit):
        raise ModelError("manual candidate lacks its immutable audit")
    declaration = ManualRuleDeclaration.from_dict(raw_declaration)
    audit = ManualRuleAudit.from_dict(raw_audit)
    if candidate.created_at != audit.audited_at:
        raise ModelError("manual candidate timestamp does not match its audit")
    if candidate.rules != declaration.manifest.rules:
        raise ModelError("manual candidate rules diverge from its declaration")
    if (
        set(candidate.metrics) != {"historical"}
        or candidate.metrics.get("historical") != audit.metrics
    ):
        raise ModelError("manual candidate metrics do not match its historical audit")
    if candidate.baselines != audit.baselines:
        raise ModelError("manual candidate baselines do not match its historical audit")
    if candidate.stability != 0.0 or candidate.train_ids or candidate.test_ids:
        raise ModelError("manual candidate cannot claim learned train, test, or stability evidence")
    expected_evaluation: JsonObject = {
        "method": MANUAL_RULE_EVALUATION_MODE,
        "confirmatory": False,
        "approval_basis": "prospective_shadow_only",
    }
    if candidate.metadata.get("evaluation") != expected_evaluation:
        raise ModelError("manual candidate evaluation must remain prospective-shadow-only")
    expected = manual_candidate_from_audit(declaration, audit, config)
    if candidate.identity_payload() != expected.identity_payload():
        raise ModelError("manual candidate does not match its canonical declaration and audit")
    return declaration, audit


def _validate_audit_observations(observations: list[Observation], config: RuleLoomConfig) -> None:
    identifiers = [item.id for item in observations]
    if len(identifiers) != len(set(identifiers)):
        raise ModelError("manual rule audit observation ids must be unique")
    descriptor = config.resolved_pack
    for item in observations:
        if (
            item.protocol_hash != config.evidence_protocol_hash
            or item.source.get("repository") != config.protocol.repository_id
            or item.source.get("pack") != config.pack
            or not matches_pack_version(item.source.get("pack_version"), config.pack_version)
            or item.source.get("extractor") != descriptor.extractor
            or item.source.get("pack_config_hash") != descriptor.configuration_hash
        ):
            raise ModelError(
                f"manual rule audit observation {item.id!r} does not match the current "
                "evidence protocol"
            )
        validate_persisted_extraction(
            descriptor,
            item.facts,
            item.fact_evidence,
            subject=f"manual rule audit observation {item.id!r}",
            metadata=item.metadata,
        )


def audit_manual_rule(
    root: Path,
    config: RuleLoomConfig,
    declaration: ManualRuleDeclaration,
    observations: list[Observation],
    *,
    as_of: datetime | None = None,
) -> ManualRuleAudit:
    """Audit fixed rules retrospectively without presenting the result as a holdout."""

    cutoff = as_of or datetime.now(UTC)
    if cutoff.tzinfo is None:
        raise ModelError("manual rule audit as_of must include a timezone")
    _validate_declaration_config(declaration, config)
    _validate_audit_observations(observations, config)
    rules = declaration.manifest.rules
    matched = [item for item in observations if rules.predicts(item.facts)]
    mature = labeled(observations, config.target, as_of=cutoff)
    mature_ids = {item.id for item in mature}
    positives = sum(item.labels[config.target] is LabelValue.POSITIVE for item in mature)
    negatives = len(mature) - positives
    metrics = evaluate(observations, config.target, rules.predicts, as_of=cutoff)
    clauses: list[ManualClauseAudit] = []
    for clause in rules.clauses:
        clause_matches = [item for item in observations if clause.matches(item.facts)]
        mature_clause_matches = sum(item.id in mature_ids for item in clause_matches)
        clauses.append(
            ManualClauseAudit(
                signature=clause.signature,
                matched_observations=len(clause_matches),
                match_rate=(len(clause_matches) / len(observations) if observations else 0.0),
                mature_matches=mature_clause_matches,
                metrics=evaluate(observations, config.target, clause.matches, as_of=cutoff),
                example_ids=tuple(
                    item.id for item in sorted(clause_matches, key=lambda item: item.id)
                )[:_MAX_EXAMPLE_IDS],
            )
        )
    warnings = [
        "retrospective manual-rule metrics are post-hoc exploratory and cannot satisfy "
        "prospective approval gates"
    ]
    if not observations:
        warnings.append("no observations are available; only the declaration was validated")
    elif not matched:
        warnings.append("the manual rule did not match any historical observation")
    if not mature:
        warnings.append("no mature labels are available; coverage does not establish rule validity")
    elif positives == 0 or negatives == 0:
        warnings.append("only one mature outcome class is present")
    unknown = len(observations) - len(mature)
    if unknown:
        warnings.append(f"{unknown} outcomes remain unknown or censored")
    source_statuses = verify_manual_rule_sources(root, declaration)
    changed_sources = sum(item.status != "unchanged" for item in source_statuses)
    if changed_sources:
        warnings.append(
            f"{changed_sources} manual rule source document(s) changed or became unavailable"
        )
    return ManualRuleAudit(
        declaration_id=declaration.id,
        audited_at=cutoff.isoformat().replace("+00:00", "Z"),
        dataset_hash=_dataset_hash(observations),
        observations=len(observations),
        matched_observations=len(matched),
        match_rate=(len(matched) / len(observations) if observations else 0.0),
        mature_labels=len(mature),
        positive=positives,
        negative=negatives,
        unknown_or_censored=unknown,
        metrics=metrics,
        baselines={
            "never_alert": evaluate(
                observations,
                config.target,
                lambda _facts: False,
                as_of=cutoff,
            ),
            "always_alert": evaluate(
                observations,
                config.target,
                lambda _facts: True,
                as_of=cutoff,
            ),
        },
        clauses=tuple(clauses),
        source_statuses=source_statuses,
        warnings=tuple(warnings),
    )


__all__ = [
    "MANUAL_RULE_CLAIM_KIND",
    "MANUAL_RULE_ENGINE_VERSION",
    "MANUAL_RULE_EVALUATION_MODE",
    "ManualClauseAudit",
    "ManualRuleAudit",
    "ManualRuleDeclaration",
    "ManualRuleManifest",
    "ManualRuleSourceRef",
    "ManualRuleSourceSnapshot",
    "ManualRuleSourceStatus",
    "audit_manual_rule",
    "declare_manual_rule",
    "load_manual_rule_manifest",
    "manual_candidate_from_audit",
    "snapshot_manual_rule_sources",
    "validate_manual_candidate",
    "verify_manual_rule_sources",
]
