"""Contracts and shared helpers for deterministic evidence packs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

from ruleloom.models import FactEvidence, JsonObject, JsonValue

EVIDENCE_LIMIT = 12
EVIDENCE_JSON_BYTES = 8 * 1024
METADATA_PATH_BYTES = 32 * 1024
INTERNAL_PREFIXES = (
    ".ruleloom/",
    ".agents/skills/ruleloom/",
    ".claude/skills/ruleloom/",
)


@dataclass(frozen=True, slots=True)
class FileChange:
    """Line-level churn reported by Git for one repository-relative path."""

    path: str
    additions: int
    deletions: int

    @property
    def churn(self) -> int:
        return self.additions + self.deletions


@dataclass(frozen=True, slots=True)
class DiffEvidence:
    """Pack-neutral, normalized evidence from one Git diff."""

    changes: tuple[FileChange, ...]
    content_patch: str = ""
    excluded_paths: tuple[str, ...] = ()
    scope_total_files: int | None = None
    scope_outside_files: int = 0
    scope_excluded_files: int = 0


@dataclass(frozen=True, slots=True)
class PackOptions:
    """Settings shared by all evidence packs and bound into the protocol hash."""

    large_change_churn: int
    multi_file_count: int
    metadata_file_limit: int


@dataclass(frozen=True, slots=True)
class PackExtraction:
    facts: frozenset[str]
    provenance: Mapping[str, FactEvidence]
    metadata: JsonObject


@dataclass(frozen=True, slots=True)
class EvidencePack:
    """A versioned, deterministic projection from Git evidence to Boolean facts."""

    name: str
    version: int
    extractor: str
    description: str
    predicates: tuple[str, ...]
    content_path: Callable[[str], bool]
    extract: Callable[[DiffEvidence, PackOptions], PackExtraction]
    configurable: bool = False
    configuration_hash: str | None = None

    def run(self, evidence: DiffEvidence, options: PackOptions) -> PackExtraction:
        if self.configurable and self.configuration_hash is None:
            raise ValueError(
                f"evidence pack {self.name}@{self.version} must be resolved with pack_config "
                "before extraction"
            )
        result = self.extract(evidence, options)
        if set(result.facts) != set(result.provenance):
            raise ValueError(
                f"evidence pack {self.name!r} returned facts without matching provenance"
            )
        undeclared = set(result.facts).difference(self.predicates)
        if undeclared:
            raise ValueError(
                f"evidence pack {self.name!r} returned undeclared predicates: "
                + ", ".join(sorted(undeclared))
            )
        for fact, item in result.provenance.items():
            if item.kind != "deterministic" or item.extractor != self.extractor:
                raise ValueError(
                    f"evidence pack {self.name!r} fact {fact!r} must have deterministic "
                    f"provenance from {self.extractor!r}"
                )
        return result


def is_internal_path(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in INTERNAL_PREFIXES)


def changed_payload(patch: str) -> tuple[str, str]:
    """Return all changed lines and only added lines from a zero-context patch."""

    changed: list[str] = []
    added: list[str] = []
    for line in patch.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            payload = line[1:]
            changed.append(payload)
            added.append(payload)
        elif line.startswith("-"):
            changed.append(line[1:])
    return "\n".join(changed), "\n".join(added)


def _entropy(churn_by_file: list[int]) -> tuple[float, float]:
    import math

    total = sum(churn_by_file)
    if total <= 0:
        return 0.0, 0.0
    entropy = -sum(
        (churn / total) * math.log2(churn / total) for churn in churn_by_file if churn > 0
    )
    nonzero_files = sum(churn > 0 for churn in churn_by_file)
    normalized = entropy / math.log2(nonzero_files) if nonzero_files > 1 else 0.0
    return round(entropy, 6), round(normalized, 6)


def _is_test_path(path: str) -> bool:
    lowered = path.lower()
    pure = PurePosixPath(lowered)
    parts = set(pure.parts)
    name = pure.name
    stem = pure.stem
    return bool(
        parts.intersection({"test", "tests", "__tests__", "integration_test"})
        or stem.startswith("test_")
        or stem.endswith("_test")
        or ".test." in name
        or ".spec." in name
    )


def record_common_path_facts(
    paths: list[str],
    record: Callable[[str, str], None],
) -> None:
    """Record language-neutral facts shared by built-in packs."""

    dependency_names = {
        "build.gradle",
        "build.gradle.kts",
        "cargo.lock",
        "cargo.toml",
        "composer.json",
        "composer.lock",
        "go.mod",
        "go.sum",
        "package-lock.json",
        "package.json",
        "pnpm-lock.yaml",
        "poetry.lock",
        "pom.xml",
        "pubspec.lock",
        "pubspec.yaml",
        "pyproject.toml",
        "requirements.txt",
        "uv.lock",
        "yarn.lock",
    }
    for path in paths:
        lowered = path.lower()
        pure = PurePosixPath(lowered)
        if _is_test_path(path):
            record("touches_test", f"path:{path}")
        if (
            pure.name.startswith("readme")
            or "docs" in pure.parts
            or pure.suffix in {".md", ".mdx", ".rst", ".adoc"}
        ):
            record("touches_docs", f"path:{path}")
        if (
            lowered.startswith(".github/workflows/")
            or lowered.startswith(".circleci/")
            or pure.name in {".gitlab-ci.yml", "azure-pipelines.yml", "jenkinsfile"}
        ):
            record("touches_ci", f"path:{path}")
        if pure.name in dependency_names or pure.name.startswith("requirements-"):
            record("touches_dependencies", f"path:{path}")


def finalize_extraction(
    evidence: DiffEvidence,
    reasons: Mapping[str, set[str]],
    *,
    extractor: str,
    options: PackOptions,
) -> PackExtraction:
    """Add shared scale facts and bounded, integrity-preserving metadata."""

    visible = tuple(change for change in evidence.changes if not is_internal_path(change.path))
    paths = [change.path for change in visible]
    additions = sum(change.additions for change in visible)
    deletions = sum(change.deletions for change in visible)
    churn = additions + deletions
    mutable_reasons = {fact: set(items) for fact, items in reasons.items()}

    def record(fact: str, reason: str) -> None:
        mutable_reasons.setdefault(fact, set()).add(reason)

    record_common_path_facts(paths, record)
    if churn >= options.large_change_churn:
        record(
            "large_change",
            f"churn:{churn}>={options.large_change_churn}",
        )
    if len(visible) >= options.multi_file_count:
        record(
            "multi_file_change",
            f"files:{len(visible)}>={options.multi_file_count}",
        )

    entropy, normalized_entropy = _entropy([change.churn for change in visible])
    ordered = sorted(visible, key=lambda item: item.path)
    sample: list[FileChange] = []
    sampled_path_bytes = 0
    for change in ordered:
        encoded_path_bytes = len(change.path.encode("utf-8"))
        if (
            len(sample) >= options.metadata_file_limit
            or sampled_path_bytes + encoded_path_bytes > METADATA_PATH_BYTES
        ):
            break
        sample.append(change)
        sampled_path_bytes += encoded_path_bytes
    manifest = hashlib.sha256()
    for change in ordered:
        manifest.update(
            json.dumps(
                [change.path, change.additions, change.deletions],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        manifest.update(b"\n")
    manifest_hash = manifest.hexdigest()
    internal_paths = sorted(
        {
            *evidence.excluded_paths,
            *(change.path for change in evidence.changes if is_internal_path(change.path)),
        }
    )
    internal_sample: list[str] = []
    internal_path_bytes = 0
    for path in internal_paths:
        encoded_path_bytes = len(path.encode("utf-8"))
        if (
            len(internal_sample) >= options.metadata_file_limit
            or internal_path_bytes + encoded_path_bytes > METADATA_PATH_BYTES
        ):
            break
        internal_sample.append(path)
        internal_path_bytes += encoded_path_bytes
    metadata: JsonObject = {
        "additions": additions,
        "deletions": deletions,
        "churn": churn,
        "files_changed": len(visible),
        "change_entropy": entropy,
        "normalized_change_entropy": normalized_entropy,
        "change_manifest_hash": manifest_hash,
        "changed_files": [change.path for change in sample],
        "file_churn": cast_json({change.path: change.churn for change in sample}),
        "metadata_files_truncated": len(ordered) - len(sample),
        "metadata_path_bytes": sampled_path_bytes,
        "excluded_internal_files": len(internal_paths),
        "excluded_internal_paths": cast_json(internal_sample),
        "excluded_internal_paths_truncated": len(internal_paths) - len(internal_sample),
    }
    if evidence.scope_total_files is not None:
        metadata.update(
            {
                "scope_total_files": evidence.scope_total_files,
                "scope_included_files": len(visible),
                "scope_outside_files": evidence.scope_outside_files,
                "scope_excluded_files": evidence.scope_excluded_files,
            }
        )
    provenance = {
        fact: FactEvidence(
            kind="deterministic",
            extractor=extractor,
            evidence=_bounded_reasons(fact_reasons),
        )
        for fact, fact_reasons in mutable_reasons.items()
    }
    return PackExtraction(
        facts=frozenset(mutable_reasons),
        provenance=provenance,
        metadata=metadata,
    )


def _bounded_reasons(reasons: set[str]) -> tuple[str, ...]:
    selected: list[str] = []
    used = 0
    for reason in sorted(reasons):
        if len(selected) >= EVIDENCE_LIMIT:
            break
        encoded = len(json.dumps(reason, ensure_ascii=True).encode("utf-8")) + 1
        if used + encoded > EVIDENCE_JSON_BYTES:
            digest = hashlib.sha256(reason.encode("utf-8")).hexdigest()
            marker = f"evidence-truncated-sha256:{digest}"
            if not selected:
                selected.append(marker)
            break
        selected.append(reason)
        used += encoded
    return tuple(selected)


def cast_json(value: object) -> JsonValue:
    """Keep JSON-compatible construction explicit for strict static typing."""

    from typing import cast

    return cast(JsonValue, value)
