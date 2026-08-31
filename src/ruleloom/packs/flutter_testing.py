"""Flutter/Dart predicates layered on the shared Git evidence contract."""

from __future__ import annotations

import re

from ruleloom.packs.base import (
    DiffEvidence,
    PackExtraction,
    PackOptions,
    changed_payload,
    finalize_extraction,
    is_internal_path,
)

NAME = "flutter_testing"
EXTRACTOR = "ruleloom.flutter_testing.git.v2"

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
        (
            "provider state assignment",
            re.compile(r"\.state\s*=(?!=)|^\s*state\s*=(?!=)", re.MULTILINE),
        ),
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


def extract_flutter_testing_facts(
    evidence: DiffEvidence,
    options: PackOptions,
) -> PackExtraction:
    reasons: dict[str, set[str]] = {}

    def record(fact: str, reason: str) -> None:
        reasons.setdefault(fact, set()).add(reason)

    visible = tuple(change for change in evidence.changes if not is_internal_path(change.path))
    for change in visible:
        path = change.path
        if not wants_dart_content(path):
            continue
        record("changes_dart", f"path:{path}")
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

    return finalize_extraction(
        evidence,
        reasons,
        extractor=EXTRACTOR,
        options=options,
    )
