"""Frozen Flutter pack used only to read or reproduce schema-v1 experiments."""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from typing import cast

from ruleloom.models import FactEvidence, JsonObject, JsonValue
from ruleloom.packs.base import (
    EVIDENCE_LIMIT,
    DiffEvidence,
    PackExtraction,
    PackOptions,
    changed_payload,
    is_internal_path,
)

NAME = "flutter_testing"
VERSION = 1
EXTRACTOR = "ruleloom.flutter_testing.git.v1"
LARGE_CHANGE_CHURN = 200
MULTI_FILE_COUNT = 3

_CONTENT_PATTERNS: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
    "touches_widget": (
        (
            "widget superclass",
            re.compile(r"\bextends\s+(?:StatelessWidget|StatefulWidget|ConsumerWidget)\b"),
        ),
        ("widget build method", re.compile(r"\bWidget\s+build\s*\(")),
    ),
    "user_input": (
        (
            "input widget",
            re.compile(
                r"\b(?:TextField|TextFormField|Form|GestureDetector|InkWell|"
                r"ElevatedButton|TextButton|IconButton)\s*\("
            ),
        ),
        ("input callback", re.compile(r"\b(?:onTap|onPressed|onChanged|onSubmitted)\s*:")),
    ),
    "mutates_state": (
        ("setState", re.compile(r"\bsetState\s*\(")),
        ("notifier mutation", re.compile(r"\b(?:notifyListeners|emit)\s*\(")),
        ("provider state assignment", re.compile(r"\.state\s*=")),
    ),
    "uses_async": (
        ("async keyword", re.compile(r"\basync\b")),
        ("await keyword", re.compile(r"\bawait\b")),
        ("asynchronous type", re.compile(r"\b(?:Future|Stream)\s*<")),
    ),
    "navigation": (
        ("Navigator API", re.compile(r"\bNavigator\s*\.")),
        ("router API", re.compile(r"\b(?:GoRouter|AutoRouter|MaterialPageRoute)\b")),
        ("context navigation", re.compile(r"\bcontext\s*\.\s*(?:go|push|pop)\s*\(")),
    ),
    "backend_contract": (
        (
            "network or database API",
            re.compile(
                r"\b(?:Dio|GraphQLClient|FirebaseFirestore|SupabaseClient)\b|"
                r"\bhttp\s*\.\s*(?:get|post|put|patch|delete)\s*\("
            ),
        ),
        ("JSON boundary", re.compile(r"\b(?:fromJson|toJson)\s*\(")),
    ),
    "auth": (
        ("authentication provider", re.compile(r"\b(?:FirebaseAuth|OAuth|Auth0)\b", re.I)),
        (
            "authentication operation",
            re.compile(r"\b(?:signIn|signOut|logIn|logOut|login|logout|accessToken|idToken)\b"),
        ),
    ),
    "payment": (
        (
            "payment integration",
            re.compile(r"\b(?:Stripe|RevenueCat|payment|checkout|purchase|subscription)\b", re.I),
        ),
    ),
}

_PATH_PATTERNS: dict[str, re.Pattern[str]] = {
    "navigation": re.compile(r"(?:^|/)(?:routes?|router|navigation)(?:[./_]|$)", re.I),
    "backend_contract": re.compile(
        r"(?:^|/)(?:api|clients?|repositories|services?|models?)(?:/|[._])", re.I
    ),
    "auth": re.compile(r"(?:^|/)(?:auth|authentication)(?:/|[._])", re.I),
    "payment": re.compile(r"(?:^|/)(?:payments?|checkout|billing)(?:/|[._])", re.I),
}


def wants_dart_content(path: str) -> bool:
    return path.lower().endswith(".dart")


def _entropy(churn_by_file: Sequence[int]) -> tuple[float, float]:
    total = sum(churn_by_file)
    if total <= 0:
        return 0.0, 0.0
    entropy = -sum(
        (churn / total) * math.log2(churn / total) for churn in churn_by_file if churn > 0
    )
    nonzero_files = sum(churn > 0 for churn in churn_by_file)
    normalized = entropy / math.log2(nonzero_files) if nonzero_files > 1 else 0.0
    return round(entropy, 6), round(normalized, 6)


def extract_flutter_testing_v1_facts(
    evidence: DiffEvidence,
    _options: PackOptions,
) -> PackExtraction:
    """Preserve every v1 fact and metadata rule exactly; do not extend this function."""

    reasons: dict[str, set[str]] = {}

    def record(fact: str, reason: str) -> None:
        reasons.setdefault(fact, set()).add(reason)

    visible = tuple(change for change in evidence.changes if not is_internal_path(change.path))
    internal_paths = sorted(
        {
            *evidence.excluded_paths,
            *(change.path for change in evidence.changes if is_internal_path(change.path)),
        }
    )
    paths = [change.path for change in visible]
    for path in paths:
        lowered = path.lower()
        is_dart = lowered.endswith(".dart")
        if is_dart:
            record("changes_dart", f"path:{path}")
        parts = lowered.split("/")
        if lowered.endswith("_test.dart") or "test" in parts or "integration_test" in parts:
            record("touches_test", f"path:{path}")
        if is_dart:
            for fact, pattern in _PATH_PATTERNS.items():
                if pattern.search(path):
                    record(fact, f"path:{path}")

    changed, added = changed_payload(evidence.content_patch)
    for fact, patterns in _CONTENT_PATTERNS.items():
        for marker, pattern in patterns:
            if pattern.search(changed):
                record(fact, f"diff-pattern:{marker}")
    if re.search(r"\btestWidgets\s*\(", added):
        record("adds_widget_test", "added-pattern:testWidgets")

    additions = sum(change.additions for change in visible)
    deletions = sum(change.deletions for change in visible)
    churn = additions + deletions
    if churn >= LARGE_CHANGE_CHURN:
        record("large_change", f"churn:{churn}>={LARGE_CHANGE_CHURN}")
    if len(visible) >= MULTI_FILE_COUNT:
        record("multi_file_change", f"files:{len(visible)}>={MULTI_FILE_COUNT}")

    entropy, normalized_entropy = _entropy([change.churn for change in visible])
    metadata: JsonObject = {
        "additions": additions,
        "deletions": deletions,
        "churn": churn,
        "files_changed": len(visible),
        "change_entropy": entropy,
        "normalized_change_entropy": normalized_entropy,
        "changed_files": cast(JsonValue, paths),
        "file_churn": cast(JsonValue, {change.path: change.churn for change in visible}),
        "excluded_internal_files": len(internal_paths),
        "excluded_internal_paths": cast(JsonValue, internal_paths),
    }
    provenance = {
        fact: FactEvidence(
            kind="deterministic",
            extractor=EXTRACTOR,
            evidence=tuple(sorted(fact_reasons)[:EVIDENCE_LIMIT]),
        )
        for fact, fact_reasons in reasons.items()
    }
    return PackExtraction(frozenset(reasons), provenance, metadata)
