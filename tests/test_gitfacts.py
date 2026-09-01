from __future__ import annotations

import json
import math
import os
import subprocess
from pathlib import Path

import pytest

import ruleloom.gitfacts as gitfacts_module
from ruleloom.config import EvidenceConfig, ProtocolConfig, RuleLoomConfig
from ruleloom.gitfacts import (
    EXTRACTOR,
    DiffEvidence,
    FileChange,
    GitFactsError,
    backfill_commits,
    backfill_commits_detailed,
    collect_snapshot,
    collect_worktree,
    extract_flutter_testing_facts,
    extract_generic_change_facts,
    missing_commit_objects,
    repository_identity,
    repository_origin_url,
)
from ruleloom.models import LabelValue, ModelError, Observation, canonical_json
from ruleloom.packs import ConfiguredPathsConfig, PathPredicateConfig
from ruleloom.packs.flutter_testing import EXTRACTOR as FLUTTER_EXTRACTOR
from ruleloom.project import validate_observations

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
        repo,
        base,
        head,
        protocol_hash=PROTOCOL_HASH,
        target="needs_extra_validation",
        pack="flutter_testing",
        pack_version=2,
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
    assert all(item.extractor == FLUTTER_EXTRACTOR for item in observation.fact_evidence.values())
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


def test_collect_snapshot_accepts_only_canonical_empty_tree_as_noncommit_base(
    flutter_repo: tuple[Path, str, str],
) -> None:
    repo, root_commit, head = flutter_repo
    empty_tree = (
        subprocess.run(
            ["git", "-C", str(repo), "hash-object", "-t", "tree", "--stdin"],
            check=True,
            capture_output=True,
            input=b"",
        )
        .stdout.decode("ascii")
        .strip()
    )

    observation = collect_snapshot(
        repo,
        empty_tree,
        root_commit,
        protocol_hash=PROTOCOL_HASH,
        pack="generic_changes",
        pack_version=1,
    )

    assert observation.source["base"] == empty_tree
    assert observation.source["head"] == root_commit
    assert observation.metadata["changed_files"] == ["README.md"]
    assert observation.facts == {"touches_docs"}

    ordinary_tree = _git(repo, "rev-parse", f"{head}^{{tree}}")
    with pytest.raises(GitFactsError, match="expected commit type"):
        collect_snapshot(
            repo,
            ordinary_tree,
            head,
            protocol_hash=PROTOCOL_HASH,
            pack="generic_changes",
            pack_version=1,
        )
    with pytest.raises(GitFactsError, match="expected commit type"):
        collect_snapshot(
            repo,
            root_commit,
            ordinary_tree,
            protocol_hash=PROTOCOL_HASH,
            pack="generic_changes",
            pack_version=1,
        )


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

    first = collect_snapshot(repo, base, head, protocol_hash=PROTOCOL_HASH)
    second = collect_snapshot(repo, base, head, protocol_hash=PROTOCOL_HASH)

    assert first == second
    assert first.source["pack"] == "flutter_testing"
    assert "pack_version" not in first.source
    assert first.source["extractor"] == "ruleloom.flutter_testing.git.v1"


def test_legacy_config_and_bare_collector_defaults_remain_compatible(
    flutter_repo: tuple[Path, str, str],
) -> None:
    repo, base, head = flutter_repo
    config = RuleLoomConfig(
        schema_version=1,
        project="LegacyApi",
        pack="flutter_testing",
        pack_version=1,
        protocol=ProtocolConfig(repository_id=repository_identity(repo)),
    )
    observation = collect_snapshot(
        repo,
        base,
        head,
        protocol_hash=config.evidence_protocol_hash,
        repository_id=config.protocol.repository_id,
    )

    validate_observations([observation], config)


def test_flutter_v1_recollection_preserves_legacy_observation_shape(tmp_path: Path) -> None:
    repo = tmp_path / "legacy"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "ruleloom@example.test")
    _git(repo, "config", "user.name", "RuleLoom Test")
    _write(repo / "README.md", "# Legacy\n")
    base = _commit(repo, "Initial", "2026-01-01T10:00:00Z")
    _write(repo / "lib/controller.dart", "void update() { ref.state = 1; }\n")
    message = "Legacy subject\n\nLegacy body"
    head = _commit(repo, message, "2026-01-02T10:00:00Z")

    observation = collect_snapshot(
        repo,
        base,
        head,
        protocol_hash=PROTOCOL_HASH,
        pack="flutter_testing",
        pack_version=1,
    )

    assert observation.metadata["commit_message"] == message
    assert "commit_message_hash" not in observation.metadata
    assert "scope_include" not in observation.metadata
    assert observation.source == {
        "kind": "git_range",
        "repository": repository_identity(repo),
        "base": base,
        "head": head,
        "pack": "flutter_testing",
        "extractor": "ruleloom.flutter_testing.git.v1",
    }
    assert observation.source["extractor"] == EXTRACTOR


def test_flutter_v1_rejects_configurable_evidence_collection(
    flutter_repo: tuple[Path, str, str],
) -> None:
    repo, base, head = flutter_repo
    profile = EvidenceConfig(include_paths=("lib/**",))
    expected = "flutter_testing@1 only supports the default evidence configuration"

    with pytest.raises(GitFactsError, match=expected):
        collect_snapshot(
            repo,
            base,
            head,
            protocol_hash=PROTOCOL_HASH,
            pack="flutter_testing",
            pack_version=1,
            evidence_config=profile,
        )
    with pytest.raises(GitFactsError, match=expected):
        collect_worktree(
            repo,
            head,
            protocol_hash=PROTOCOL_HASH,
            pack="flutter_testing",
            pack_version=1,
            evidence_config=profile,
        )
    with pytest.raises(GitFactsError, match=expected):
        backfill_commits(
            repo,
            1,
            protocol_hash=PROTOCOL_HASH,
            pack="flutter_testing",
            pack_version=1,
            evidence_config=profile,
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


def test_repository_origin_and_commit_object_preflight(
    flutter_repo: tuple[Path, str, str],
) -> None:
    repo, _base, head = flutter_repo
    assert repository_origin_url(repo) is None
    _git(repo, "remote", "add", "origin", "https://github.com/acme/widgets.git")
    assert repository_origin_url(repo) == "https://github.com/acme/widgets.git"

    tree = _git(repo, "rev-parse", f"{head}^{{tree}}")
    absent = "f" * 40
    assert set(missing_commit_objects(repo, [head, tree, absent])) == {tree, absent}
    with pytest.raises(GitFactsError, match="full SHA"):
        missing_commit_objects(repo, ["HEAD"])


def test_commit_object_preflight_streams_large_valid_batches_without_pipe_deadlock(
    flutter_repo: tuple[Path, str, str],
) -> None:
    repo, _base, head = flutter_repo
    absent = [f"{index:040x}" for index in range(5_000)]

    result = missing_commit_objects(repo, [head, *absent])

    assert len(result) == len(absent)
    assert set(result) == set(absent)


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

    first = collect_worktree(
        repo,
        head,
        protocol_hash=PROTOCOL_HASH,
        pack="flutter_testing",
        pack_version=2,
    )
    second = collect_worktree(
        repo,
        head,
        protocol_hash=PROTOCOL_HASH,
        pack="flutter_testing",
        pack_version=2,
    )

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


def test_collect_worktree_rejects_lossy_untracked_content_decode(
    flutter_repo: tuple[Path, str, str],
) -> None:
    repo, _, head = flutter_repo
    invalid = repo / "lib/invalid.dart"
    invalid.write_bytes(b"void update() {\n  state = \xff;\n}\n")

    with pytest.raises(GitFactsError, match="content file is not valid UTF-8"):
        collect_worktree(
            repo,
            head,
            protocol_hash=PROTOCOL_HASH,
            pack="flutter_testing",
            pack_version=2,
        )


@pytest.mark.parametrize(("pack_version", "expected_internal"), [(1, 1), (2, 0)])
def test_worktree_reports_untracked_internal_files_without_using_them_as_evidence(
    flutter_repo: tuple[Path, str, str],
    pack_version: int,
    expected_internal: int,
) -> None:
    repo, _, head = flutter_repo
    _write(repo / "lib/screens/checkout.dart", "void update() { state = 1; }\n")
    _write(repo / ".ruleloom/local.json", "{}\n")

    observation = collect_worktree(
        repo,
        head,
        protocol_hash=PROTOCOL_HASH,
        pack="flutter_testing",
        pack_version=pack_version,
    )

    assert observation.metadata["excluded_internal_files"] == expected_internal
    assert observation.metadata["excluded_internal_paths"] == (
        [".ruleloom/local.json"] if pack_version == 1 else []
    )
    assert observation.metadata["files_changed"] == 1


def test_current_worktree_identity_ignores_ruleloom_internal_churn(
    flutter_repo: tuple[Path, str, str],
) -> None:
    repo, _, head = flutter_repo
    _write(repo / "lib/screens/checkout.dart", "void update() { state = 1; }\n")
    before = collect_worktree(
        repo,
        head,
        protocol_hash=PROTOCOL_HASH,
        pack="generic_changes",
        pack_version=1,
    )
    _write(repo / ".ruleloom/observations.jsonl", '{"self":"generated"}\n')
    _write(repo / ".ruleloom/.observations.jsonl.lock", "lock\n")
    after = collect_worktree(
        repo,
        head,
        protocol_hash=PROTOCOL_HASH,
        pack="generic_changes",
        pack_version=1,
    )

    assert before.id == after.id
    assert before.facts == after.facts
    assert before.fact_evidence == after.fact_evidence
    assert before.metadata == after.metadata
    assert after.metadata["excluded_internal_files"] == 0


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
        content_patch="""@@ -0,0 +1,2 @@
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
            content_patch="",
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
            content_patch="+final item = Item.fromJson(data);",
        )
    )

    assert "multi_file_change" not in facts
    assert metadata["files_changed"] == 1
    assert metadata["churn"] == 1
    assert metadata["excluded_internal_files"] == 2


def test_generic_pack_is_language_neutral() -> None:
    python = DiffEvidence(
        changes=(FileChange("tests/test_service.py", additions=250, deletions=0),)
    )
    typescript = DiffEvidence(
        changes=(FileChange("tests/service.test.ts", additions=250, deletions=0),)
    )

    python_facts, _, python_metadata = extract_generic_change_facts(python)
    typescript_facts, _, typescript_metadata = extract_generic_change_facts(typescript)

    assert python_facts == typescript_facts == frozenset({"touches_test", "large_change"})
    assert python_metadata["churn"] == typescript_metadata["churn"] == 250


def test_flutter_v2_detects_bare_riverpod_state_without_local_variable_false_positive() -> None:
    bare_assignment = DiffEvidence(
        changes=(FileChange("lib/controller.dart", additions=1, deletions=0),),
        content_patch="+state = const AsyncLoading();",
    )
    local_variable = DiffEvidence(
        changes=(FileChange("lib/controller.dart", additions=1, deletions=0),),
        content_patch="+final state = const AsyncLoading();",
    )
    comparisons = DiffEvidence(
        changes=(FileChange("lib/controller.dart", additions=2, deletions=0),),
        content_patch="+if (state == value) {}\n+if (ref.state == value) {}",
    )

    bare_facts, bare_provenance, _ = extract_flutter_testing_facts(bare_assignment)
    local_facts, _, _ = extract_flutter_testing_facts(local_variable)
    comparison_facts, _, _ = extract_flutter_testing_facts(comparisons)

    assert "mutates_state" in bare_facts
    assert bare_provenance["mutates_state"].extractor == "ruleloom.flutter_testing.git.v2"
    assert "mutates_state" not in local_facts
    assert "mutates_state" not in comparison_facts


def test_collection_scope_rejects_mixed_units_and_backfill_skips_ineligible(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "monorepo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "ruleloom@example.test")
    _git(repo, "config", "user.name", "RuleLoom Test")
    _write(repo / "README.md", "# Monorepo\n")
    base = _commit(repo, "Initial", "2026-01-01T10:00:00Z")
    _write(repo / "apps/mobile/lib/controller.dart", "void update() {\n  state = 1;\n}\n")
    _write(repo / "apps/mobile/test/controller_test.dart", "void main() {}\n")
    _write(repo / "apps/web/lib/payment.dart", "void pay() { Stripe.checkout(); }\n")
    head = _commit(repo, "Cross component", "2026-01-02T10:00:00Z")

    profile = EvidenceConfig(
        include_paths=("apps/mobile/**",),
        large_change_churn=100,
        multi_file_count=3,
    )
    with pytest.raises(GitFactsError, match="mixes files inside and outside"):
        collect_snapshot(
            repo,
            base,
            head,
            protocol_hash=PROTOCOL_HASH,
            pack="flutter_testing",
            pack_version=2,
            evidence_config=profile,
        )

    _write(repo / "apps/mobile/lib/controller.dart", "void update() {\n  state = 2;\n}\n")
    _write(
        repo / "apps/mobile/test/controller_test.dart",
        "void main() { testWidgets('x', (_) {}); }\n",
    )
    pure_mobile = _commit(repo, "Mobile only", "2026-01-03T10:00:00Z")
    observation = collect_snapshot(
        repo,
        head,
        pure_mobile,
        protocol_hash=PROTOCOL_HASH,
        pack="flutter_testing",
        pack_version=2,
        evidence_config=profile,
    )

    _write(repo / "apps/web/lib/payment.dart", "void pay() { Stripe.refund(); }\n")
    web_only = _commit(repo, "Web only", "2026-01-04T10:00:00Z")
    report = backfill_commits_detailed(
        repo,
        4,
        protocol_hash=PROTOCOL_HASH,
        pack="flutter_testing",
        pack_version=2,
        evidence_config=profile,
    )
    eligible = list(report.observations)

    assert observation.metadata["files_changed"] == 2
    assert observation.metadata["scope_include"] == ["apps/mobile/**"]
    assert observation.metadata["scope_outside_files"] == 0
    assert {"changes_dart", "touches_test", "mutates_state"} <= observation.facts
    assert "payment" not in observation.facts
    assert "multi_file_change" not in observation.facts
    assert [item.source["head"] for item in eligible] == [pure_mobile]
    assert report.examined == 4
    assert report.eligible == 1
    assert report.skipped == 3
    assert report.skipped_no_in_scope_files == 2
    assert report.skipped_mixed_scope == 1
    assert report.skipped_preview == (
        (base, "no_in_scope_files"),
        (head, "mixed_scope"),
        (web_only, "no_in_scope_files"),
    )
    assert len(report.skipped_manifest_hash) == 64
    assert (
        backfill_commits(
            repo,
            4,
            protocol_hash=PROTOCOL_HASH,
            pack="flutter_testing",
            pack_version=2,
            evidence_config=profile,
        )
        == eligible
    )


def test_worktree_uses_git_glob_semantics_for_tracked_and_untracked_paths(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "glob-scope"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "ruleloom@example.test")
    _git(repo, "config", "user.name", "RuleLoom Test")
    _write(repo / "apps/mobile/lib/excluded_tracked.dart", "int value = 1;\n")
    _write(repo / "apps/mobile/lib/deeper/included_tracked.dart", "int value = 1;\n")
    base = _commit(repo, "Initial", "2026-01-01T10:00:00Z")
    _write(repo / "apps/mobile/lib/excluded_tracked.dart", "int value = 2;\n")
    _write(repo / "apps/mobile/lib/deeper/included_tracked.dart", "int value = 2;\n")
    _write(repo / "apps/mobile/lib/excluded_untracked.dart", "int value = 3;\n")
    _write(repo / "apps/mobile/lib/deeper/included_untracked.dart", "int value = 3;\n")

    observation = collect_worktree(
        repo,
        base,
        protocol_hash=PROTOCOL_HASH,
        pack="generic_changes",
        pack_version=1,
        evidence_config=EvidenceConfig(
            include_paths=("apps/**",),
            exclude_paths=("apps/mobile/lib/*",),
        ),
    )

    assert observation.metadata["changed_files"] == [
        "apps/mobile/lib/deeper/included_tracked.dart",
        "apps/mobile/lib/deeper/included_untracked.dart",
    ]
    assert observation.metadata["scope_outside_files"] == 0
    assert observation.metadata["scope_excluded_files"] == 2

    (repo / "apps/mobile/lib/excluded_untracked.dart").unlink()
    changed_scope_metadata = collect_worktree(
        repo,
        base,
        protocol_hash=PROTOCOL_HASH,
        pack="generic_changes",
        pack_version=1,
        evidence_config=EvidenceConfig(
            include_paths=("apps/**",),
            exclude_paths=("apps/mobile/lib/*",),
        ),
    )
    assert changed_scope_metadata.metadata["scope_excluded_files"] == 1
    assert changed_scope_metadata.id != observation.id


def test_v2_preserves_posix_backslash_filename_for_literal_content_selection(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "backslash-path"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "ruleloom@example.test")
    _git(repo, "config", "user.name", "RuleLoom Test")
    _write(repo / "README.md", "# Example\n")
    base = _commit(repo, "Initial", "2026-01-01T10:00:00Z")
    unusual = "lib/.ruleloom\\controller.dart"
    _write(repo / unusual, "void update() {\n  state = 1;\n}\n")
    head = _commit(repo, "Unusual path", "2026-01-02T10:00:00Z")

    observation = collect_snapshot(
        repo,
        base,
        head,
        protocol_hash=PROTOCOL_HASH,
        pack="flutter_testing",
        pack_version=2,
    )

    assert observation.metadata["changed_files"] == [unusual]
    assert {"changes_dart", "mutates_state"} <= observation.facts


def test_v2_metadata_is_bounded_for_megachanges() -> None:
    changes = tuple(
        FileChange(
            f"packages/component_{index:05d}/lib/very_long_generated_feature_name_{index:05d}.dart",
            additions=200,
            deletions=20,
        )
        for index in range(6_700)
    )
    evidence = DiffEvidence(changes=changes)

    facts, _, metadata = extract_flutter_testing_facts(evidence)
    encoded = json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    assert {"large_change", "multi_file_change", "changes_dart"} <= facts
    assert metadata["files_changed"] == 6_700
    assert metadata["churn"] == 1_474_000
    assert metadata["metadata_files_truncated"] > 0
    assert len(metadata["change_manifest_hash"]) == 64
    assert len(encoded) < 256 * 1024


def test_complete_adversarial_megachange_observation_fits_jsonl_record() -> None:
    control_run = "\x01" * 180
    changes = tuple(
        FileChange(
            f"tests/control_{index:05d}_{control_run}.test.py",
            additions=200,
            deletions=20,
        )
        for index in range(6_700)
    )
    evidence = DiffEvidence(
        changes=changes,
        excluded_paths=tuple(
            f".ruleloom/generated_{index:04d}_{control_run}.json" for index in range(1_000)
        ),
    )
    facts, provenance, metadata = extract_generic_change_facts(
        evidence,
        EvidenceConfig(metadata_file_limit=10_000),
    )
    metadata.update(
        {
            "commit_timestamp": "2026-01-02T10:00:00Z",
            "commit_message": "\x01" * 4_096,
            "commit_message_hash": "a" * 64,
            "commit_message_truncated": True,
            "scope_include": ["**"],
            "scope_exclude": [],
        }
    )
    observation = Observation(
        id="commit." + "a" * 40,
        observed_at="2026-01-02T10:00:00Z",
        protocol_hash=PROTOCOL_HASH,
        facts=facts,
        labels={"needs_extra_validation": LabelValue.UNKNOWN},
        fact_evidence=provenance,
        source={
            "kind": "git_commit",
            "repository": "repo.example",
            "base": "b" * 40,
            "head": "a" * 40,
            "pack": "generic_changes",
            "pack_version": 1,
            "extractor": "ruleloom.generic_changes.git.v1",
        },
        metadata=metadata,
    )

    encoded = (canonical_json(observation.to_dict()) + "\n").encode("utf-8")

    assert len(encoded) < 1024 * 1024
    assert observation.metadata["metadata_files_truncated"] > 0


def test_content_patch_has_global_batch_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, ...]] = []

    def fake_git(*args: object, **_kwargs: object) -> str:
        calls.append(args)
        return ""

    monkeypatch.setattr(gitfacts_module, "_git", fake_git)
    monkeypatch.setattr(gitfacts_module, "_PATCH_PATH_BATCH", 1)
    monkeypatch.setattr(gitfacts_module, "_MAX_CONTENT_PATCH_BATCHES", 2)

    with pytest.raises(GitFactsError, match="more than 2 Git batches"):
        gitfacts_module._content_patch(
            Path("/unused"),
            ("base", "head"),
            ["one.dart", "two.dart", "three.dart"],
        )

    assert len(calls) == 2


def test_content_path_batches_bound_argument_bytes() -> None:
    paths = [f"lib/{index:04d}_" + "x" * 4_000 + ".dart" for index in range(100)]

    batches = gitfacts_module._content_path_batches(paths)

    assert len(batches) > 1
    assert all(len(batch) <= gitfacts_module._PATCH_PATH_BATCH for batch in batches)
    assert all(
        sum(len(f":(literal){path}".encode()) + 1 for path in batch)
        <= gitfacts_module._MAX_PATCH_PATHSPEC_BYTES
        for batch in batches
    )


def test_collection_rejects_non_utf8_git_output_without_lossy_decoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        gitfacts_module,
        "_run_git_capped",
        lambda *_args, **_kwargs: (b"invalid_\xff.dart\x00", b"", 0),
    )

    with pytest.raises(GitFactsError, match="non-UTF-8"):
        gitfacts_module._git(Path("/unused"), "status")


def test_configured_paths_has_commit_range_worktree_and_backfill_parity(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "configured-parity"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "ruleloom@example.test")
    _git(repo, "config", "user.name", "RuleLoom Test")
    _write(repo / "README.md", "# Example\n")
    base = _commit(repo, "Initial", "2026-01-01T10:00:00Z")
    _write(repo / "apps/web/main.ts", "export const ready = true;\n")
    _write(repo / "packages/shared/schema.json", '{"version": 1}\n')
    _write(repo / "apps/web/tests/main.test.ts", "test('ready', () => {});\n")
    pack_config = ConfiguredPathsConfig(
        (
            PathPredicateConfig("touches_surface_web", ("apps/web/**",)),
            PathPredicateConfig("touches_shared_contract", ("packages/shared/**",)),
            PathPredicateConfig("touches_web_tests", ("apps/web/**/tests/**",)),
        )
    )
    profile = EvidenceConfig(large_change_churn=10_000, multi_file_count=100)

    worktree = collect_worktree(
        repo,
        base,
        protocol_hash=PROTOCOL_HASH,
        pack="configured_paths",
        pack_version=1,
        pack_config=pack_config,
        evidence_config=profile,
    )
    head = _commit(repo, "Cross-surface change", "2026-01-02T10:00:00Z")
    snapshot = collect_snapshot(
        repo,
        base,
        head,
        protocol_hash=PROTOCOL_HASH,
        pack="configured_paths",
        pack_version=1,
        pack_config=pack_config,
        evidence_config=profile,
    )
    backfilled = backfill_commits(
        repo,
        1,
        protocol_hash=PROTOCOL_HASH,
        pack="configured_paths",
        pack_version=1,
        pack_config=pack_config,
        evidence_config=profile,
    )[0]

    expected = {
        "touches_surface_web",
        "touches_shared_contract",
        "touches_web_tests",
        "touches_test",
    }
    assert worktree.facts == snapshot.facts == backfilled.facts == expected
    assert worktree.fact_evidence == snapshot.fact_evidence == backfilled.fact_evidence
    assert (
        worktree.metadata["configured_match_manifest_hash"]
        == snapshot.metadata["configured_match_manifest_hash"]
        == backfilled.metadata["configured_match_manifest_hash"]
    )
    assert worktree.source["pack_config_hash"] == pack_config.hash
    assert snapshot.source["pack_config_hash"] == pack_config.hash


def test_configured_paths_counts_deleted_and_binary_paths_without_content_patches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "configured-binary"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "ruleloom@example.test")
    _git(repo, "config", "user.name", "RuleLoom Test")
    _write(repo / "apps/native/obsolete.txt", "remove me\n")
    base = _commit(repo, "Initial", "2026-01-01T10:00:00Z")
    (repo / "apps/native/obsolete.txt").unlink()
    binary = repo / "apps/native/image.bin"
    binary.write_bytes(bytes(range(256)))
    head = _commit(repo, "Native assets", "2026-01-02T10:00:00Z")
    pack_config = ConfiguredPathsConfig(
        (PathPredicateConfig("touches_native_host", ("apps/native/**",)),)
    )
    original_content_patch = gitfacts_module._content_patch

    def assert_no_content_paths(
        path: Path,
        common: tuple[str, ...],
        paths: list[str],
    ) -> str:
        assert paths == []
        return original_content_patch(path, common, paths)

    monkeypatch.setattr(gitfacts_module, "_content_patch", assert_no_content_paths)
    observation = collect_snapshot(
        repo,
        base,
        head,
        protocol_hash=PROTOCOL_HASH,
        pack="configured_paths",
        pack_version=1,
        pack_config=pack_config,
    )

    assert "touches_native_host" in observation.facts
    assert observation.metadata["files_changed"] == 2
    assert observation.metadata["configured_path_match_counts"] == {"touches_native_host": 2}


def test_configured_observation_rejects_same_pack_with_different_configuration(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "configured-provenance"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "ruleloom@example.test")
    _git(repo, "config", "user.name", "RuleLoom Test")
    _write(repo / "README.md", "# Example\n")
    base = _commit(repo, "Initial", "2026-01-01T10:00:00Z")
    _write(repo / "apps/web/main.ts", "export const ready = true;\n")
    head = _commit(repo, "Web change", "2026-01-02T10:00:00Z")
    repository_id = repository_identity(repo)
    first_pack_config = ConfiguredPathsConfig(
        (PathPredicateConfig("touches_surface_web", ("apps/web/**",)),)
    )
    first_config = RuleLoomConfig(
        schema_version=3,
        project="ConfiguredProvenance",
        pack="configured_paths",
        pack_version=1,
        pack_config=first_pack_config,
        protocol=ProtocolConfig(repository_id=repository_id, prediction_unit="git_range"),
    )
    observation = collect_snapshot(
        repo,
        base,
        head,
        protocol_hash=first_config.evidence_protocol_hash,
        pack=first_config.pack,
        pack_version=first_config.pack_version,
        pack_config=first_config.pack_config,
        evidence_config=first_config.evidence,
        repository_id=repository_id,
    )
    validate_observations([observation], first_config)

    second_config = RuleLoomConfig(
        schema_version=3,
        project="ConfiguredProvenance",
        pack="configured_paths",
        pack_version=1,
        pack_config=ConfiguredPathsConfig(
            (PathPredicateConfig("touches_surface_web", ("web/**",)),)
        ),
        protocol=ProtocolConfig(repository_id=repository_id, prediction_unit="git_range"),
    )
    forged_protocol = Observation.from_dict(
        {**observation.to_dict(), "protocol_hash": second_config.evidence_protocol_hash}
    )
    with pytest.raises(ModelError, match="different pack configuration"):
        validate_observations([forged_protocol], second_config)


def test_rejects_invalid_requests(tmp_path: Path) -> None:
    with pytest.raises(GitFactsError, match="does not exist"):
        collect_snapshot(tmp_path / "missing", "HEAD~1", "HEAD", protocol_hash=PROTOCOL_HASH)
    for invalid in (0, True, 1.0, "1"):
        with pytest.raises(GitFactsError, match="integer >= 1"):
            backfill_commits(
                tmp_path,
                invalid,  # type: ignore[arg-type]
                protocol_hash=PROTOCOL_HASH,
            )
