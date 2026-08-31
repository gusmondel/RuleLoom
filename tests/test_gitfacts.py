from __future__ import annotations

import math
import os
import subprocess
from pathlib import Path

import pytest

from ruleloom.gitfacts import (
    EXTRACTOR,
    DiffEvidence,
    FileChange,
    GitFactsError,
    backfill_commits,
    collect_snapshot,
    collect_worktree,
    extract_flutter_testing_facts,
    repository_identity,
)
from ruleloom.models import LabelValue

PROTOCOL_HASH = "e" * 64


def _git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _commit(repo: Path, message: str, timestamp: str) -> str:
    _git(repo, "add", ".")
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", message],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_DATE": timestamp,
            "GIT_COMMITTER_DATE": timestamp,
        },
    )
    return _git(repo, "rev-parse", "HEAD")


def _empty_commit(repo: Path, message: str, timestamp: str) -> str:
    subprocess.run(
        ["git", "-C", str(repo), "commit", "--allow-empty", "-m", message],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_DATE": timestamp,
            "GIT_COMMITTER_DATE": timestamp,
        },
    )
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def flutter_repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "sample_flutter"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "ruleloom@example.test")
    _git(repo, "config", "user.name", "RuleLoom Test")
    _write(repo / "README.md", "# Sample\n")
    first = _commit(repo, "Initial project", "2026-01-01T10:00:00+00:00")

    _write(
        repo / "lib/screens/checkout.dart",
        """import 'package:flutter/material.dart';

class Checkout extends StatefulWidget {
  Future<void> pay() async {
    await Stripe.checkout();
    setState(() {});
    Navigator.push(context, MaterialPageRoute(builder: (_) => Form(child: TextField())));
  }
}
""",
    )
    _write(
        repo / "test/checkout_test.dart",
        """void main() {
  testWidgets('checkout works', (tester) async {
    await tester.pumpWidget(const Checkout());
  });
}
""",
    )
    second = _commit(repo, "Add checkout widget", "2026-01-02T11:30:00-03:00")
    return repo, first, second


def test_collect_snapshot_extracts_deterministic_flutter_evidence(
    flutter_repo: tuple[Path, str, str],
) -> None:
    repo, base, head = flutter_repo

    observation = collect_snapshot(
        repo, base, head, protocol_hash=PROTOCOL_HASH, target="needs_extra_validation"
    )

    assert observation.labels == {"needs_extra_validation": LabelValue.UNKNOWN}
    assert {
        "changes_dart",
        "touches_test",
        "adds_widget_test",
        "touches_widget",
        "user_input",
        "mutates_state",
        "uses_async",
        "navigation",
        "payment",
    } <= observation.facts
    assert "auth" not in observation.facts
    assert set(observation.fact_evidence) == set(observation.facts)
    assert all(item.kind == "deterministic" for item in observation.fact_evidence.values())
    assert all(item.extractor == EXTRACTOR for item in observation.fact_evidence.values())
    assert all(item.evidence for item in observation.fact_evidence.values())

    assert observation.metadata["files_changed"] == 2
    assert observation.metadata["additions"] == 14
    assert observation.metadata["deletions"] == 0
    assert observation.metadata["churn"] == 14
    assert observation.metadata["commit_timestamp"] == "2026-01-02T11:30:00-03:00"
    assert observation.metadata["commit_message"] == "Add checkout widget"
    file_churn = observation.metadata["file_churn"]
    assert isinstance(file_churn, dict)
    churn_values = [value for value in file_churn.values() if isinstance(value, int)]
    total = sum(churn_values)
    expected_entropy = -sum((value / total) * math.log2(value / total) for value in churn_values)
    assert observation.metadata["change_entropy"] == round(expected_entropy, 6)
    assert observation.source["base"] == base
    assert observation.source["head"] == head
    assert observation.source["kind"] == "git_range"


def test_backfill_is_chronological_and_never_infers_labels(
    flutter_repo: tuple[Path, str, str],
) -> None:
    repo, first, second = flutter_repo

    observations = backfill_commits(repo, 2, protocol_hash=PROTOCOL_HASH, target="regression_risk")

    assert [item.source["head"] for item in observations] == [first, second]
    assert [item.metadata["commit_message"] for item in observations] == [
        "Initial project",
        "Add checkout widget",
    ]
    assert all(item.labels == {"regression_risk": LabelValue.UNKNOWN} for item in observations)
    assert observations[0].id == f"commit.{first}"
    assert observations[1].id == f"commit.{second}"
    assert observations[0].observed_at < observations[1].observed_at
    assert [item.metadata["topological_index"] for item in observations] == [1, 2]


def test_collection_is_reproducible(flutter_repo: tuple[Path, str, str]) -> None:
    repo, base, head = flutter_repo

    assert collect_snapshot(repo, base, head, protocol_hash=PROTOCOL_HASH) == collect_snapshot(
        repo, base, head, protocol_hash=PROTOCOL_HASH
    )


def test_collection_verifies_configured_repository_identity(
    flutter_repo: tuple[Path, str, str],
) -> None:
    repo, base, head = flutter_repo
    identity = repository_identity(repo)

    assert (
        collect_snapshot(
            repo,
            base,
            head,
            protocol_hash=PROTOCOL_HASH,
            repository_id=identity,
        ).source["repository"]
        == identity
    )
    with pytest.raises(GitFactsError, match="does not match this Git repository"):
        collect_snapshot(
            repo,
            base,
            head,
            protocol_hash=PROTOCOL_HASH,
            repository_id="repo.other",
        )


def test_repository_identity_rejects_unanchored_empty_repository(tmp_path: Path) -> None:
    repo = tmp_path / "empty"
    repo.mkdir()
    _git(repo, "init")

    with pytest.raises(GitFactsError, match=r"remote\.origin\.url or at least one commit"):
        repository_identity(repo)


def test_collect_worktree_includes_tracked_and_untracked_flutter_changes(
    flutter_repo: tuple[Path, str, str],
) -> None:
    repo, _, head = flutter_repo
    _write(
        repo / "lib/screens/checkout.dart",
        """class Checkout extends ConsumerWidget {
  void update() => setState(() {});
}
""",
    )
    _write(
        repo / "test/new_flow_test.dart",
        """void main() {
  testWidgets('new flow', (tester) async {});
}
""",
    )

    first = collect_worktree(repo, head, protocol_hash=PROTOCOL_HASH)
    second = collect_worktree(repo, head, protocol_hash=PROTOCOL_HASH)

    assert first.id == second.id
    assert first.facts == second.facts
    assert {
        "changes_dart",
        "touches_widget",
        "mutates_state",
        "touches_test",
        "adds_widget_test",
    } <= first.facts
    assert first.source["kind"] == "git_worktree"
    assert first.source["head"] == "WORKTREE"
    assert first.metadata["files_changed"] == 2
    assert first.labels == {"needs_extra_validation": LabelValue.UNKNOWN}


def test_worktree_identity_includes_base_even_when_trees_are_identical(
    flutter_repo: tuple[Path, str, str],
) -> None:
    repo, _, first_base = flutter_repo
    second_base = _empty_commit(repo, "Metadata-only checkpoint", "2026-01-03T10:00:00Z")
    _write(repo / "lib/pending.dart", "Future<void> pending() async {}\n")

    first = collect_worktree(repo, first_base, protocol_hash=PROTOCOL_HASH)
    second = collect_worktree(repo, second_base, protocol_hash=PROTOCOL_HASH)

    assert first.facts == second.facts
    assert first.id != second.id
    assert first.metadata["base_commit"] == first_base
    assert second.metadata["base_commit"] == second_base


def test_pure_extractor_marks_threshold_and_contract_facts() -> None:
    evidence = DiffEvidence(
        changes=(
            FileChange("lib/auth/client.dart", additions=120, deletions=80),
            FileChange("lib/models/account.dart", additions=0, deletions=0),
            FileChange("assets/logo.png", additions=0, deletions=0),
        ),
        dart_patch="""@@ -0,0 +1,2 @@
+final auth = FirebaseAuth.instance;
+final account = Account.fromJson(payload);
""",
    )

    facts, provenance, metadata = extract_flutter_testing_facts(evidence)

    assert {
        "changes_dart",
        "auth",
        "backend_contract",
        "large_change",
        "multi_file_change",
    } <= facts
    assert metadata["churn"] == 200
    assert metadata["change_entropy"] == 0.0
    assert metadata["normalized_change_entropy"] == 0.0
    assert provenance["large_change"].evidence == ("churn:200>=200",)


def test_empty_or_binary_only_diff_has_zero_entropy() -> None:
    facts, _, metadata = extract_flutter_testing_facts(
        DiffEvidence(
            changes=(FileChange("assets/logo.png", additions=0, deletions=0),),
            dart_patch="",
        )
    )

    assert facts == frozenset()
    assert metadata["files_changed"] == 1
    assert metadata["change_entropy"] == 0.0


def test_excludes_ruleloom_generated_paths_from_change_facts() -> None:
    facts, _, metadata = extract_flutter_testing_facts(
        DiffEvidence(
            changes=(
                FileChange(".ruleloom/config.json", additions=100, deletions=0),
                FileChange(".agents/skills/ruleloom/SKILL.md", additions=100, deletions=0),
                FileChange("lib/item.dart", additions=1, deletions=0),
            ),
            dart_patch="+final item = Item.fromJson(data);",
        )
    )

    assert "multi_file_change" not in facts
    assert metadata["files_changed"] == 1
    assert metadata["churn"] == 1
    assert metadata["excluded_internal_files"] == 2


def test_rejects_invalid_requests(tmp_path: Path) -> None:
    with pytest.raises(GitFactsError, match="does not exist"):
        collect_snapshot(tmp_path / "missing", "HEAD~1", "HEAD", protocol_hash=PROTOCOL_HASH)
    with pytest.raises(GitFactsError, match="limit"):
        backfill_commits(tmp_path, 0, protocol_hash=PROTOCOL_HASH)
