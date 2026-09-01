"""Explicit, outcome-blind repository assertions and historical adherence audits.

Repository prose is never parsed. A human or an external tool must encode each
assertion as a bounded conjunction of antecedent literals and a bounded
conjunction of expected literals. The declaration binds that translation to a
frozen predicate vocabulary, evidence protocol, repository, and hashed source
spans.

The historical audit describes structural adherence only. It does not read
outcomes and must not be interpreted as evidence of risk, quality, or causality.
"""

from __future__ import annotations

import hashlib
import math
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import cast

from ruleloom.models import (
    JsonObject,
    JsonValue,
    ModelError,
    Observation,
    RuleLiteral,
    content_hash,
    parse_timestamp,
    validate_predicate,
    validate_subject,
    validate_timestamp,
)
from ruleloom.storage import project_path, read_json

REPOSITORY_ASSERTION_SCHEMA_VERSION = 1
REPOSITORY_ASSERTION_ENGINE_VERSION = "ruleloom-repository-assertions/0.1"
REPOSITORY_ASSERTION_SEMANTICS = "antecedent_implies_expectation"
REPOSITORY_ASSERTION_EVALUATION_MODE = "retrospective_structural_adherence"

_ASSERTION_CATEGORIES = frozenset({"structural", "test_structure"})
_MAX_ASSERTIONS = 64
_MAX_LITERALS = 8
_MAX_SUMMARY_CHARS = 500
_MAX_SOURCE_REFS = 16
_MAX_SOURCE_PATH_CHARS = 512
_MAX_SOURCE_BYTES = 1024 * 1024
_MAX_TOTAL_SOURCE_BYTES = 8 * 1024 * 1024
_MAX_SOURCE_LINES = 100_000
_MAX_SOURCE_SPAN_LINES = 500
_MAX_EXAMPLE_IDS = 20
_HASH_CHARS = frozenset("0123456789abcdef")
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


def _integer(value: JsonValue, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ModelError(f"{name} must be an integer >= {minimum}")
    return value


def _object(value: JsonValue, name: str) -> JsonObject:
    if not isinstance(value, dict):
        raise ModelError(f"{name} must be an object")
    return value


def _sha256(value: str, name: str) -> str:
    if len(value) != 64 or any(character not in _HASH_CHARS for character in value):
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
        raise ModelError("repository assertion source path is invalid")
    pure = PurePosixPath(value)
    folded = value.casefold()
    if (
        pure.is_absolute()
        or value != pure.as_posix()
        or value in {".", ".."}
        or ".." in pure.parts
        or pure.parts[0].casefold() == ".git"
        or any(folded.startswith(prefix.casefold()) for prefix in _GENERATED_SOURCE_PREFIXES)
    ):
        raise ModelError(
            "repository assertion source must be a normalized repository-relative, "
            "non-generated path"
        )
    return value


def _canonical_literals(literals: tuple[RuleLiteral, ...], name: str) -> tuple[RuleLiteral, ...]:
    if not literals:
        raise ModelError(f"repository assertion {name} must contain at least one literal")
    if len(literals) > _MAX_LITERALS:
        raise ModelError(f"repository assertion {name} supports at most {_MAX_LITERALS} literals")
    if not all(isinstance(item, RuleLiteral) for item in literals):
        raise ModelError(f"repository assertion {name} contains an invalid literal")
    predicates = [item.predicate for item in literals]
    if len(predicates) != len(set(predicates)):
        raise ModelError(
            f"repository assertion {name} cannot repeat or negate both forms of a predicate"
        )
    return tuple(sorted(literals, key=lambda item: (item.predicate, item.negated)))


@dataclass(frozen=True, slots=True)
class RepositoryAssertionSourceRef:
    """A bounded source span whose prose is attached, but never interpreted."""

    path: str
    start_line: int | None = None
    end_line: int | None = None

    def __post_init__(self) -> None:
        _source_path(self.path)
        if (self.start_line is None) != (self.end_line is None):
            raise ModelError("repository assertion source range requires start_line and end_line")
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
                "repository assertion source range must be positive, ordered, and at most "
                f"{_MAX_SOURCE_SPAN_LINES} lines"
            )

    def to_dict(self) -> JsonObject:
        result: JsonObject = {"path": self.path}
        if self.start_line is not None:
            result["start_line"] = self.start_line
            result["end_line"] = self.end_line
        return result

    @classmethod
    def from_dict(cls, value: JsonObject) -> RepositoryAssertionSourceRef:
        _reject_unknown(
            value,
            {"path", "start_line", "end_line"},
            "repository assertion source",
        )
        raw_start = value.get("start_line")
        raw_end = value.get("end_line")
        return cls(
            path=_string(value.get("path"), "repository assertion source path"),
            start_line=(
                None
                if raw_start is None
                else _integer(raw_start, "repository assertion source start_line", minimum=1)
            ),
            end_line=(
                None
                if raw_end is None
                else _integer(raw_end, "repository assertion source end_line", minimum=1)
            ),
        )


def _source_ref_key(ref: RepositoryAssertionSourceRef) -> tuple[str, bool, int, int]:
    return (
        ref.path,
        ref.start_line is not None,
        ref.start_line or 0,
        ref.end_line or 0,
    )


@dataclass(frozen=True, slots=True)
class RepositoryAssertion:
    """One explicit structural expectation over a frozen predicate vocabulary."""

    assertion_id: str
    revision: int
    summary: str
    antecedent: tuple[RuleLiteral, ...]
    expectation: tuple[RuleLiteral, ...]
    sources: tuple[RepositoryAssertionSourceRef, ...] = ()
    category: str = "structural"
    semantics: str = REPOSITORY_ASSERTION_SEMANTICS

    def __post_init__(self) -> None:
        validate_subject(self.assertion_id)
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 1
        ):
            raise ModelError("repository assertion revision must be an integer >= 1")
        _single_line(
            self.summary,
            "repository assertion summary",
            maximum=_MAX_SUMMARY_CHARS,
        )
        if self.category not in _ASSERTION_CATEGORIES:
            raise ModelError(
                "repository assertion category must be one of: "
                + ", ".join(sorted(_ASSERTION_CATEGORIES))
            )
        if self.semantics != REPOSITORY_ASSERTION_SEMANTICS:
            raise ModelError(
                "repository assertions support antecedent_implies_expectation semantics only"
            )
        antecedent = _canonical_literals(self.antecedent, "antecedent")
        expectation = _canonical_literals(self.expectation, "expectation")
        overlap = {item.predicate for item in antecedent}.intersection(
            item.predicate for item in expectation
        )
        if overlap:
            raise ModelError(
                "repository assertion antecedent and expectation must use distinct predicates: "
                + ", ".join(sorted(overlap))
            )
        if not self.sources:
            raise ModelError("repository assertion requires at least one hashed source reference")
        if len(self.sources) > _MAX_SOURCE_REFS:
            raise ModelError(
                f"repository assertion supports at most {_MAX_SOURCE_REFS} source references"
            )
        canonical_sources = tuple(sorted(self.sources, key=_source_ref_key))
        if len(canonical_sources) != len(set(canonical_sources)):
            raise ModelError("repository assertion contains duplicate source references")
        object.__setattr__(self, "antecedent", antecedent)
        object.__setattr__(self, "expectation", expectation)
        object.__setattr__(self, "sources", canonical_sources)

    def antecedent_matches(self, facts: frozenset[str]) -> bool:
        return all(item.matches(facts) for item in self.antecedent)

    def expectation_matches(self, facts: frozenset[str]) -> bool:
        return all(item.matches(facts) for item in self.expectation)

    def to_dict(self) -> JsonObject:
        return {
            "assertion_id": self.assertion_id,
            "revision": self.revision,
            "summary": self.summary,
            "category": self.category,
            "semantics": self.semantics,
            "antecedent": [item.to_dict() for item in self.antecedent],
            "expectation": [item.to_dict() for item in self.expectation],
            "sources": [item.to_dict() for item in self.sources],
        }

    @classmethod
    def from_dict(cls, value: JsonObject) -> RepositoryAssertion:
        _reject_unknown(
            value,
            {
                "assertion_id",
                "revision",
                "summary",
                "category",
                "semantics",
                "antecedent",
                "expectation",
                "sources",
            },
            "repository assertion",
        )
        raw_antecedent = value.get("antecedent")
        raw_expectation = value.get("expectation")
        raw_sources = value.get("sources", [])
        if not isinstance(raw_antecedent, list):
            raise ModelError("repository assertion antecedent must be an array")
        if not isinstance(raw_expectation, list):
            raise ModelError("repository assertion expectation must be an array")
        if not isinstance(raw_sources, list):
            raise ModelError("repository assertion sources must be an array")
        return cls(
            assertion_id=_string(value.get("assertion_id"), "repository assertion id"),
            revision=_integer(value.get("revision"), "repository assertion revision", minimum=1),
            summary=_string(value.get("summary"), "repository assertion summary"),
            category=_string(value.get("category"), "repository assertion category"),
            semantics=_string(value.get("semantics"), "repository assertion semantics"),
            antecedent=tuple(
                RuleLiteral.from_dict(_object(item, "repository assertion antecedent literal"))
                for item in raw_antecedent
            ),
            expectation=tuple(
                RuleLiteral.from_dict(_object(item, "repository assertion expectation literal"))
                for item in raw_expectation
            ),
            sources=tuple(
                RepositoryAssertionSourceRef.from_dict(_object(item, "repository assertion source"))
                for item in raw_sources
            ),
        )


@dataclass(frozen=True, slots=True)
class RepositoryAssertionManifest:
    """Strict collection of explicit assertions; no source prose is interpreted."""

    assertions: tuple[RepositoryAssertion, ...]
    schema_version: int = REPOSITORY_ASSERTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != REPOSITORY_ASSERTION_SCHEMA_VERSION:
            raise ModelError("unsupported repository assertion manifest schema version")
        if not self.assertions:
            raise ModelError("repository assertion manifest must contain at least one assertion")
        if len(self.assertions) > _MAX_ASSERTIONS:
            raise ModelError(
                f"repository assertion manifest supports at most {_MAX_ASSERTIONS} assertions"
            )
        if not all(isinstance(item, RepositoryAssertion) for item in self.assertions):
            raise ModelError("repository assertion manifest contains an invalid assertion")
        canonical = tuple(
            sorted(self.assertions, key=lambda item: (item.assertion_id, item.revision))
        )
        identities = [(item.assertion_id, item.revision) for item in canonical]
        if len(identities) != len(set(identities)):
            raise ModelError("repository assertion manifest contains duplicate identities")
        duplicate_ids = [
            assertion_id
            for assertion_id in {item.assertion_id for item in canonical}
            if sum(item.assertion_id == assertion_id for item in canonical) > 1
        ]
        if duplicate_ids:
            raise ModelError(
                "repository assertion manifest cannot mix revisions of the same assertion: "
                + ", ".join(sorted(duplicate_ids))
            )
        object.__setattr__(self, "assertions", canonical)

    def to_dict(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "assertions": [item.to_dict() for item in self.assertions],
        }

    @classmethod
    def from_dict(cls, value: JsonObject) -> RepositoryAssertionManifest:
        _reject_unknown(value, {"schema_version", "assertions"}, "repository assertion manifest")
        raw_assertions = value.get("assertions")
        if not isinstance(raw_assertions, list):
            raise ModelError("repository assertion manifest assertions must be an array")
        return cls(
            schema_version=_integer(
                value.get("schema_version"),
                "repository assertion manifest schema_version",
                minimum=1,
            ),
            assertions=tuple(
                RepositoryAssertion.from_dict(_object(item, "repository assertion"))
                for item in raw_assertions
            ),
        )


@dataclass(frozen=True, slots=True)
class RepositoryAssertionSourceSnapshot:
    assertion_id: str
    ref: RepositoryAssertionSourceRef
    document_sha256: str
    excerpt_sha256: str
    size_bytes: int
    line_count: int

    def __post_init__(self) -> None:
        validate_subject(self.assertion_id)
        _sha256(self.document_sha256, "repository assertion source document_sha256")
        _sha256(self.excerpt_sha256, "repository assertion source excerpt_sha256")
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or not 0 <= self.size_bytes <= _MAX_SOURCE_BYTES
        ):
            raise ModelError("repository assertion source size_bytes is invalid")
        if (
            isinstance(self.line_count, bool)
            or not isinstance(self.line_count, int)
            or not 0 <= self.line_count <= _MAX_SOURCE_LINES
        ):
            raise ModelError("repository assertion source line_count is invalid")

    def to_dict(self) -> JsonObject:
        return {
            "assertion_id": self.assertion_id,
            "ref": self.ref.to_dict(),
            "document_sha256": self.document_sha256,
            "excerpt_sha256": self.excerpt_sha256,
            "size_bytes": self.size_bytes,
            "line_count": self.line_count,
        }

    @classmethod
    def from_dict(cls, value: JsonObject) -> RepositoryAssertionSourceSnapshot:
        _reject_unknown(
            value,
            {
                "assertion_id",
                "ref",
                "document_sha256",
                "excerpt_sha256",
                "size_bytes",
                "line_count",
            },
            "repository assertion source snapshot",
        )
        return cls(
            assertion_id=_string(
                value.get("assertion_id"), "repository assertion source snapshot assertion_id"
            ),
            ref=RepositoryAssertionSourceRef.from_dict(
                _object(value.get("ref"), "repository assertion source snapshot ref")
            ),
            document_sha256=_string(
                value.get("document_sha256"),
                "repository assertion source snapshot document_sha256",
            ),
            excerpt_sha256=_string(
                value.get("excerpt_sha256"),
                "repository assertion source snapshot excerpt_sha256",
            ),
            size_bytes=_integer(
                value.get("size_bytes"),
                "repository assertion source snapshot size_bytes",
            ),
            line_count=_integer(
                value.get("line_count"),
                "repository assertion source snapshot line_count",
            ),
        )


def _source_snapshot_key(
    snapshot: RepositoryAssertionSourceSnapshot,
) -> tuple[str, str, bool, int, int]:
    return (snapshot.assertion_id, *_source_ref_key(snapshot.ref))


@dataclass(frozen=True, slots=True)
class _RepositoryAssertionSourceDocument:
    raw: bytes
    lines: tuple[bytes, ...]


def _load_source_document(root: Path, path_text: str) -> _RepositoryAssertionSourceDocument:
    path = project_path(root, path_text)
    try:
        source_stat = path.stat()
    except OSError as exc:
        raise ModelError(f"cannot read repository assertion source {path_text}: {exc}") from exc
    if not stat.S_ISREG(source_stat.st_mode):
        raise ModelError(f"repository assertion source must be a regular file: {path_text}")
    if source_stat.st_size > _MAX_SOURCE_BYTES:
        raise ModelError(
            f"repository assertion source exceeds {_MAX_SOURCE_BYTES} bytes: {path_text}"
        )
    try:
        raw = path.read_bytes()
        raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ModelError(
            f"repository assertion source must be readable UTF-8: {path_text}: {exc}"
        ) from exc
    if len(raw) > _MAX_SOURCE_BYTES:
        raise ModelError(
            f"repository assertion source exceeds {_MAX_SOURCE_BYTES} bytes: {path_text}"
        )
    lines = raw.split(b"\n") if raw else []
    if raw.endswith(b"\n"):
        lines.pop()
    if len(lines) > _MAX_SOURCE_LINES:
        raise ModelError(
            f"repository assertion source exceeds {_MAX_SOURCE_LINES} lines: {path_text}"
        )
    return _RepositoryAssertionSourceDocument(raw=raw, lines=tuple(lines))


def _snapshot_source(
    root: Path,
    assertion_id: str,
    ref: RepositoryAssertionSourceRef,
    *,
    document: _RepositoryAssertionSourceDocument | None = None,
) -> RepositoryAssertionSourceSnapshot:
    source = document or _load_source_document(root, ref.path)
    raw = source.raw
    lines = source.lines
    if ref.start_line is None:
        excerpt = raw
    else:
        end = cast(int, ref.end_line)
        if end > len(lines):
            raise ModelError(
                f"repository assertion source range {ref.start_line}:{end} exceeds "
                f"{len(lines)} lines in {ref.path}"
            )
        excerpt = b"\n".join(lines[ref.start_line - 1 : end])
    return RepositoryAssertionSourceSnapshot(
        assertion_id=assertion_id,
        ref=ref,
        document_sha256=hashlib.sha256(raw).hexdigest(),
        excerpt_sha256=hashlib.sha256(excerpt).hexdigest(),
        size_bytes=len(raw),
        line_count=len(lines),
    )


@dataclass(frozen=True, slots=True)
class RepositoryAssertionDeclaration:
    """Assertions bound to one repository and one deterministic evidence vocabulary."""

    id: str
    declared_at: str
    repository_id: str
    protocol_hash: str
    predicate_vocabulary: tuple[str, ...]
    manifest: RepositoryAssertionManifest
    sources: tuple[RepositoryAssertionSourceSnapshot, ...]
    schema_version: int = REPOSITORY_ASSERTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != REPOSITORY_ASSERTION_SCHEMA_VERSION:
            raise ModelError("unsupported repository assertion declaration schema version")
        validate_subject(self.id)
        validate_timestamp(self.declared_at)
        validate_subject(self.repository_id)
        _sha256(self.protocol_hash, "repository assertion declaration protocol_hash")
        vocabulary = tuple(sorted(set(self.predicate_vocabulary)))
        if not vocabulary or vocabulary != self.predicate_vocabulary:
            raise ModelError(
                "repository assertion predicate_vocabulary must be non-empty, sorted, and unique"
            )
        for predicate in vocabulary:
            validate_predicate(predicate, field_name="repository assertion vocabulary predicate")
        used = {
            literal.predicate
            for assertion in self.manifest.assertions
            for literal in (*assertion.antecedent, *assertion.expectation)
        }
        unknown = used.difference(vocabulary)
        if unknown:
            raise ModelError(
                "repository assertions reference predicates outside the frozen vocabulary: "
                + ", ".join(sorted(unknown))
            )
        expected_source_keys = tuple(
            sorted(
                (
                    (assertion.assertion_id, ref)
                    for assertion in self.manifest.assertions
                    for ref in assertion.sources
                ),
                key=lambda item: (item[0], *_source_ref_key(item[1])),
            )
        )
        actual_source_keys = tuple((item.assertion_id, item.ref) for item in self.sources)
        if expected_source_keys != actual_source_keys:
            raise ModelError(
                "repository assertion source snapshots do not match the manifest sources"
            )
        documents: dict[str, tuple[str, int, int]] = {}
        for source in self.sources:
            identity = (source.document_sha256, source.size_bytes, source.line_count)
            prior = documents.setdefault(source.ref.path, identity)
            if prior != identity:
                raise ModelError(
                    "repository assertion snapshots for one source path must bind the same document"
                )
        if sum(size for _, size, _ in documents.values()) > _MAX_TOTAL_SOURCE_BYTES:
            raise ModelError(
                "repository assertion declaration exceeds the total source byte budget"
            )
        if self.id != self.expected_id:
            raise ModelError(
                f"repository assertion declaration id {self.id!r} does not match "
                f"{self.expected_id!r}"
            )

    def identity_payload(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "declared_at": self.declared_at,
            "repository_id": self.repository_id,
            "protocol_hash": self.protocol_hash,
            "predicate_vocabulary": list(self.predicate_vocabulary),
            "manifest": self.manifest.to_dict(),
            "sources": [item.to_dict() for item in self.sources],
        }

    @property
    def expected_id(self) -> str:
        return f"assertions-{content_hash(self.identity_payload())[:24]}"

    @property
    def manifest_hash(self) -> str:
        return content_hash(self.identity_payload())

    def to_dict(self) -> JsonObject:
        return {
            "id": self.id,
            **self.identity_payload(),
            "manifest_hash": self.manifest_hash,
        }

    @classmethod
    def from_dict(cls, value: JsonObject) -> RepositoryAssertionDeclaration:
        _reject_unknown(
            value,
            {
                "id",
                "schema_version",
                "declared_at",
                "repository_id",
                "protocol_hash",
                "predicate_vocabulary",
                "manifest",
                "sources",
                "manifest_hash",
            },
            "repository assertion declaration",
        )
        raw_vocabulary = value.get("predicate_vocabulary")
        raw_sources = value.get("sources")
        if not isinstance(raw_vocabulary, list) or not all(
            isinstance(item, str) for item in raw_vocabulary
        ):
            raise ModelError(
                "repository assertion declaration predicate_vocabulary must be an array of strings"
            )
        if not isinstance(raw_sources, list):
            raise ModelError("repository assertion declaration sources must be an array")
        declaration = cls(
            id=_string(value.get("id"), "repository assertion declaration id"),
            schema_version=_integer(
                value.get("schema_version"),
                "repository assertion declaration schema_version",
                minimum=1,
            ),
            declared_at=_string(
                value.get("declared_at"), "repository assertion declaration declared_at"
            ),
            repository_id=_string(
                value.get("repository_id"), "repository assertion declaration repository_id"
            ),
            protocol_hash=_string(
                value.get("protocol_hash"), "repository assertion declaration protocol_hash"
            ),
            predicate_vocabulary=tuple(cast(list[str], raw_vocabulary)),
            manifest=RepositoryAssertionManifest.from_dict(
                _object(value.get("manifest"), "repository assertion declaration manifest")
            ),
            sources=tuple(
                RepositoryAssertionSourceSnapshot.from_dict(
                    _object(item, "repository assertion declaration source")
                )
                for item in raw_sources
            ),
        )
        manifest_hash = _string(
            value.get("manifest_hash"), "repository assertion declaration manifest_hash"
        )
        _sha256(manifest_hash, "repository assertion declaration manifest_hash")
        if manifest_hash != declaration.manifest_hash:
            raise ModelError(
                "repository assertion declaration manifest_hash does not match its payload"
            )
        return declaration


def declare_repository_assertions(
    root: Path,
    manifest: RepositoryAssertionManifest,
    *,
    repository_id: str,
    protocol_hash: str,
    predicate_vocabulary: tuple[str, ...],
    declared_at: datetime | None = None,
) -> RepositoryAssertionDeclaration:
    """Freeze explicit assertions and source hashes without reading observations."""

    instant = declared_at or datetime.now(UTC)
    if instant.tzinfo is None:
        raise ModelError("repository assertion declared_at must include a timezone")
    declared_text = instant.isoformat().replace("+00:00", "Z")
    vocabulary = tuple(sorted(set(predicate_vocabulary)))
    documents: dict[str, _RepositoryAssertionSourceDocument] = {}
    total_source_bytes = 0
    for assertion in manifest.assertions:
        for ref in assertion.sources:
            if ref.path in documents:
                continue
            document = _load_source_document(root, ref.path)
            total_source_bytes += len(document.raw)
            if total_source_bytes > _MAX_TOTAL_SOURCE_BYTES:
                raise ModelError(
                    "repository assertion declaration exceeds the total source byte budget"
                )
            documents[ref.path] = document
    snapshots = tuple(
        sorted(
            (
                _snapshot_source(
                    root,
                    assertion.assertion_id,
                    ref,
                    document=documents[ref.path],
                )
                for assertion in manifest.assertions
                for ref in assertion.sources
            ),
            key=_source_snapshot_key,
        )
    )
    identity: JsonObject = {
        "schema_version": REPOSITORY_ASSERTION_SCHEMA_VERSION,
        "declared_at": declared_text,
        "repository_id": repository_id,
        "protocol_hash": protocol_hash,
        "predicate_vocabulary": list(vocabulary),
        "manifest": manifest.to_dict(),
        "sources": [item.to_dict() for item in snapshots],
    }
    return RepositoryAssertionDeclaration(
        id=f"assertions-{content_hash(identity)[:24]}",
        declared_at=declared_text,
        repository_id=repository_id,
        protocol_hash=protocol_hash,
        predicate_vocabulary=vocabulary,
        manifest=manifest,
        sources=snapshots,
    )


def load_repository_assertion_manifest(path: Path) -> RepositoryAssertionManifest:
    """Load a strict JSON manifest; never interpret a repository prose document."""

    if path.is_symlink():
        raise ModelError(f"repository assertion manifest must not be a symlink: {path}")
    return RepositoryAssertionManifest.from_dict(read_json(path))


def load_repository_assertion_declaration(path: Path) -> RepositoryAssertionDeclaration:
    """Load and verify a frozen assertion declaration and all of its hashes."""

    if path.is_symlink():
        raise ModelError(f"repository assertion declaration must not be a symlink: {path}")
    return RepositoryAssertionDeclaration.from_dict(read_json(path))


@dataclass(frozen=True, slots=True)
class RepositoryAssertionSourceStatus:
    assertion_id: str
    ref: RepositoryAssertionSourceRef
    status: str
    current_document_sha256: str | None = None
    current_excerpt_sha256: str | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        validate_subject(self.assertion_id)
        if self.status not in {"unchanged", "changed", "unavailable"}:
            raise ModelError(f"unsupported repository assertion source status: {self.status!r}")
        hashes = self.current_document_sha256, self.current_excerpt_sha256
        if any(item is not None for item in hashes) and not all(
            item is not None for item in hashes
        ):
            raise ModelError("repository assertion source status requires both hashes or neither")
        if self.status == "unavailable":
            if any(item is not None for item in hashes) or not self.reason:
                raise ModelError(
                    "unavailable repository assertion source requires a reason and no hashes"
                )
        else:
            if not all(item is not None for item in hashes):
                raise ModelError("available repository assertion source status requires hashes")
            _sha256(cast(str, hashes[0]), "current source document_sha256")
            _sha256(cast(str, hashes[1]), "current source excerpt_sha256")

    def to_dict(self) -> JsonObject:
        return {
            "assertion_id": self.assertion_id,
            "ref": self.ref.to_dict(),
            "status": self.status,
            "current_document_sha256": self.current_document_sha256,
            "current_excerpt_sha256": self.current_excerpt_sha256,
            "reason": self.reason,
        }


def verify_repository_assertion_sources(
    root: Path,
    declaration: RepositoryAssertionDeclaration,
) -> tuple[RepositoryAssertionSourceStatus, ...]:
    """Compare current sources with the immutable declaration snapshots."""

    statuses: list[RepositoryAssertionSourceStatus] = []
    documents: dict[str, _RepositoryAssertionSourceDocument] = {}
    failures: dict[str, str] = {}
    total_source_bytes = 0
    source_budget_exhausted = False
    for expected in declaration.sources:
        try:
            if expected.ref.path in failures:
                raise ModelError(failures[expected.ref.path])
            document = documents.get(expected.ref.path)
            if document is None:
                if source_budget_exhausted:
                    raise ModelError(
                        "repository assertion verification exceeds the total source byte budget"
                    )
                document = _load_source_document(root, expected.ref.path)
                if total_source_bytes + len(document.raw) > _MAX_TOTAL_SOURCE_BYTES:
                    source_budget_exhausted = True
                    raise ModelError(
                        "repository assertion verification exceeds the total source byte budget"
                    )
                total_source_bytes += len(document.raw)
                documents[expected.ref.path] = document
            current = _snapshot_source(
                root,
                expected.assertion_id,
                expected.ref,
                document=document,
            )
        except ModelError as exc:
            failures.setdefault(expected.ref.path, str(exc))
            statuses.append(
                RepositoryAssertionSourceStatus(
                    assertion_id=expected.assertion_id,
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
            RepositoryAssertionSourceStatus(
                assertion_id=expected.assertion_id,
                ref=expected.ref,
                status="unchanged" if unchanged else "changed",
                current_document_sha256=current.document_sha256,
                current_excerpt_sha256=current.excerpt_sha256,
            )
        )
    return tuple(statuses)


@dataclass(frozen=True, slots=True)
class RepositoryAssertionAuditRow:
    assertion_id: str
    category: str
    eligible_observations: int
    expectation_met: int
    expectation_absent: int
    eligible_rate: float
    adherence_rate: float | None
    absent_example_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_subject(self.assertion_id)
        if self.category not in _ASSERTION_CATEGORIES:
            raise ModelError("repository assertion audit row has an invalid category")
        for name, value in (
            ("eligible_observations", self.eligible_observations),
            ("expectation_met", self.expectation_met),
            ("expectation_absent", self.expectation_absent),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ModelError(f"repository assertion audit {name} must be non-negative")
        if self.eligible_observations != self.expectation_met + self.expectation_absent:
            raise ModelError("repository assertion audit row counts are inconsistent")
        if not math.isfinite(self.eligible_rate) or not 0 <= self.eligible_rate <= 1:
            raise ModelError("repository assertion audit eligible_rate must be between 0 and 1")
        if self.adherence_rate is not None and (
            not math.isfinite(self.adherence_rate) or not 0 <= self.adherence_rate <= 1
        ):
            raise ModelError("repository assertion audit adherence_rate must be null or [0, 1]")
        if (self.adherence_rate is None) != (self.eligible_observations == 0):
            raise ModelError(
                "repository assertion audit adherence_rate must be null exactly when no "
                "observations are eligible"
            )
        if len(self.absent_example_ids) > _MAX_EXAMPLE_IDS:
            raise ModelError(
                f"repository assertion audit supports at most {_MAX_EXAMPLE_IDS} examples"
            )
        if len(self.absent_example_ids) != len(set(self.absent_example_ids)):
            raise ModelError("repository assertion audit examples must be unique")
        for item in self.absent_example_ids:
            validate_subject(item)

    def to_dict(self) -> JsonObject:
        return {
            "assertion_id": self.assertion_id,
            "category": self.category,
            "eligible_observations": self.eligible_observations,
            "expectation_met": self.expectation_met,
            "expectation_absent": self.expectation_absent,
            "eligible_rate": self.eligible_rate,
            "adherence_rate": self.adherence_rate,
            "absent_example_ids": list(self.absent_example_ids),
        }


@dataclass(frozen=True, slots=True)
class RepositoryAssertionAudit:
    declaration_id: str
    declaration_manifest_hash: str
    observation_manifest_hash: str
    observations: int
    rows: tuple[RepositoryAssertionAuditRow, ...]
    source_statuses: tuple[RepositoryAssertionSourceStatus, ...]
    ordering: str
    limitations: tuple[str, ...]
    schema_version: int = REPOSITORY_ASSERTION_SCHEMA_VERSION
    engine_version: str = REPOSITORY_ASSERTION_ENGINE_VERSION
    evaluation_mode: str = REPOSITORY_ASSERTION_EVALUATION_MODE
    outcome_blind: bool = True

    def __post_init__(self) -> None:
        if self.schema_version != REPOSITORY_ASSERTION_SCHEMA_VERSION:
            raise ModelError("unsupported repository assertion audit schema version")
        if self.engine_version != REPOSITORY_ASSERTION_ENGINE_VERSION:
            raise ModelError("unsupported repository assertion audit engine version")
        if self.evaluation_mode != REPOSITORY_ASSERTION_EVALUATION_MODE or not self.outcome_blind:
            raise ModelError("repository assertion audits must remain outcome-blind")
        validate_subject(self.declaration_id)
        _sha256(self.declaration_manifest_hash, "assertion declaration manifest hash")
        _sha256(self.observation_manifest_hash, "assertion observation manifest hash")
        if isinstance(self.observations, bool) or not isinstance(self.observations, int):
            raise ModelError("repository assertion audit observations must be an integer")
        if self.observations < 0:
            raise ModelError("repository assertion audit observations must be non-negative")
        if self.ordering not in {"first_parent_topology", "observed_at"}:
            raise ModelError("repository assertion audit ordering is invalid")
        row_ids = [item.assertion_id for item in self.rows]
        if len(row_ids) != len(set(row_ids)):
            raise ModelError("repository assertion audit rows must have unique assertion ids")
        if not self.limitations or not all(item for item in self.limitations):
            raise ModelError("repository assertion audit limitations cannot be empty")

    def payload(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "engine_version": self.engine_version,
            "evaluation_mode": self.evaluation_mode,
            "outcome_blind": self.outcome_blind,
            "declaration_id": self.declaration_id,
            "declaration_manifest_hash": self.declaration_manifest_hash,
            "observation_manifest_hash": self.observation_manifest_hash,
            "observations": self.observations,
            "ordering": self.ordering,
            "rows": [item.to_dict() for item in self.rows],
            "source_statuses": [item.to_dict() for item in self.source_statuses],
            "limitations": list(self.limitations),
        }

    @property
    def manifest_hash(self) -> str:
        return content_hash(self.payload())

    def to_dict(self) -> JsonObject:
        return {**self.payload(), "manifest_hash": self.manifest_hash}

    def render_text(self) -> str:
        """Render the adoption-facing summary while keeping full evidence in JSON."""

        source_counts = {
            status: sum(item.status == status for item in self.source_statuses)
            for status in ("unchanged", "changed", "unavailable")
        }
        lines = [
            "RuleLoom repository assertion audit",
            "",
            f"Declaration: {self.declaration_id}",
            f"Observations: {self.observations} ({self.ordering})",
            (
                "Source evidence: "
                f"{source_counts['unchanged']} unchanged, "
                f"{source_counts['changed']} changed, "
                f"{source_counts['unavailable']} unavailable"
            ),
            "",
            "Structural adherence",
        ]
        for row in self.rows:
            rate = "not observed" if row.adherence_rate is None else f"{row.adherence_rate:.1%}"
            lines.append(
                f"- {row.assertion_id}: {rate}; {row.eligible_observations} eligible, "
                f"{row.expectation_absent} exceptions"
            )
        lines.extend(
            (
                "",
                "Limits of interpretation",
                *(f"- {item}" for item in self.limitations),
                "",
                f"Manifest: {self.manifest_hash}",
            )
        )
        return "\n".join(lines) + "\n"


def _ordered_observations(
    observations: tuple[Observation, ...],
) -> tuple[tuple[Observation, ...], str]:
    topology: list[tuple[str, int]] = []
    for item in observations:
        repository = item.source.get("repository")
        position = item.metadata.get("topological_index")
        if (
            isinstance(repository, str)
            and isinstance(position, int)
            and not isinstance(position, bool)
            and position >= 1
        ):
            topology.append((repository, position))
    if (
        observations
        and len(topology) == len(observations)
        and len({repository for repository, _ in topology}) == 1
    ):
        return (
            tuple(
                sorted(
                    observations,
                    key=lambda item: (cast(int, item.metadata["topological_index"]), item.id),
                )
            ),
            "first_parent_topology",
        )
    return (
        tuple(sorted(observations, key=lambda item: (parse_timestamp(item.observed_at), item.id))),
        "observed_at",
    )


def _observation_manifest(observations: tuple[Observation, ...]) -> str:
    return content_hash(
        [
            {
                "schema_version": item.schema_version,
                "id": item.id,
                "observed_at": item.observed_at,
                "protocol_hash": item.protocol_hash,
                "facts": cast(JsonValue, sorted(item.facts)),
                "repository": item.source.get("repository"),
                "topological_index": item.metadata.get("topological_index"),
            }
            for item in observations
        ]
    )


def audit_repository_assertions(
    root: Path,
    declaration: RepositoryAssertionDeclaration,
    observations: tuple[Observation, ...] | list[Observation],
) -> RepositoryAssertionAudit:
    """Describe historical structural adherence without reading any outcomes."""

    values = tuple(observations)
    identifiers = [item.id for item in values]
    if len(identifiers) != len(set(identifiers)):
        raise ModelError("repository assertion audit observation ids must be unique")
    for item in values:
        if (
            item.protocol_hash != declaration.protocol_hash
            or item.source.get("repository") != declaration.repository_id
        ):
            raise ModelError(
                f"repository assertion audit observation {item.id!r} does not match the "
                "declaration evidence binding"
            )
    ordered, ordering = _ordered_observations(values)
    rows: list[RepositoryAssertionAuditRow] = []
    for assertion in declaration.manifest.assertions:
        eligible_count = 0
        met_count = 0
        absent_examples: list[str] = []
        for item in ordered:
            if not assertion.antecedent_matches(item.facts):
                continue
            eligible_count += 1
            if assertion.expectation_matches(item.facts):
                met_count += 1
            elif len(absent_examples) < _MAX_EXAMPLE_IDS:
                absent_examples.append(item.id)
        absent_count = eligible_count - met_count
        rows.append(
            RepositoryAssertionAuditRow(
                assertion_id=assertion.assertion_id,
                category=assertion.category,
                eligible_observations=eligible_count,
                expectation_met=met_count,
                expectation_absent=absent_count,
                eligible_rate=eligible_count / len(ordered) if ordered else 0.0,
                adherence_rate=met_count / eligible_count if eligible_count else None,
                absent_example_ids=tuple(absent_examples),
            )
        )
    return RepositoryAssertionAudit(
        declaration_id=declaration.id,
        declaration_manifest_hash=declaration.manifest_hash,
        observation_manifest_hash=_observation_manifest(ordered),
        observations=len(ordered),
        rows=tuple(rows),
        source_statuses=verify_repository_assertion_sources(root, declaration),
        ordering=ordering,
        limitations=(
            "Historical adherence is a structural co-occurrence description, not a risk score.",
            "Historical adherence does not establish causality or the correctness of an assertion.",
            "Only explicit predicates are evaluated; repository prose is never interpreted.",
            "An absent predicate means the frozen extractor did not assert it; it is not proof "
            "that the underlying repository property was absent.",
        ),
    )


__all__ = [
    "REPOSITORY_ASSERTION_ENGINE_VERSION",
    "REPOSITORY_ASSERTION_EVALUATION_MODE",
    "REPOSITORY_ASSERTION_SCHEMA_VERSION",
    "REPOSITORY_ASSERTION_SEMANTICS",
    "RepositoryAssertion",
    "RepositoryAssertionAudit",
    "RepositoryAssertionAuditRow",
    "RepositoryAssertionDeclaration",
    "RepositoryAssertionManifest",
    "RepositoryAssertionSourceRef",
    "RepositoryAssertionSourceSnapshot",
    "RepositoryAssertionSourceStatus",
    "audit_repository_assertions",
    "declare_repository_assertions",
    "load_repository_assertion_declaration",
    "load_repository_assertion_manifest",
    "verify_repository_assertion_sources",
]
