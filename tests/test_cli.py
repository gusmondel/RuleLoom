from __future__ import annotations

import csv
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from ruleloom import cli
from ruleloom.config import LearnerConfig, RuleLoomConfig
from ruleloom.gitfacts import collect_snapshot
from ruleloom.learners.popper import PopperDoctorReport
from ruleloom.models import LabelValue, ModelError
from ruleloom.storage import dataset_path, load_observations, load_predictions


def _git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


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


def _create_flutter_history(repo: Path, count: int = 12) -> list[str]:
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "ruleloom@example.test")
    _git(repo, "config", "user.name", "RuleLoom Test")
    commits: list[str] = []
    for index in range(count):
        path = repo / f"lib/change_{index:02d}.dart"
        path.parent.mkdir(parents=True, exist_ok=True)
        if index % 2 == 0:
            content = f"""Future<void> operation{index}() async {{
  await Future<void>.value();
}}
"""
        else:
            content = f"""int value{index}() {{
  return {index};
}}
"""
        path.write_text(content, encoding="utf-8")
        commits.append(
            _commit(
                repo,
                f"Change {index:02d}",
                f"2026-01-{index + 1:02d}T10:00:00+00:00",
            )
        )
    return commits


def _run_cli(arguments: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    exit_code = cli.main(arguments)
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


def _label_history(
    repo: Path,
    commits: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    first_id = f"commit.{commits[0]}"
    exit_code, stdout, stderr = _run_cli(
        [
            "label",
            "--root",
            str(repo),
            first_id,
            "positive",
            "--kind",
            "ci",
            "--source",
            "ci/run-0",
            "--available-at",
            "2026-01-01T11:00:00Z",
            "--reason",
            "regression reproduced",
        ],
        capsys,
    )
    assert exit_code == 0
    assert stdout == f"Labeled {first_id} as positive\n"
    assert stderr == ""

    labels_path = repo / "labels.csv"
    with labels_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["id", "value", "available_at", "kind", "source", "reason"],
        )
        writer.writeheader()
        for index, commit in enumerate(commits[1:], 1):
            writer.writerow(
                {
                    "id": f"commit.{commit}",
                    "value": "positive" if index % 2 == 0 else "negative",
                    "available_at": f"2026-01-{index + 1:02d}T11:00:00Z",
                    "kind": "imported",
                    "source": "pilot/outcomes.csv",
                    "reason": "mature pilot outcome",
                }
            )

    exit_code, stdout, stderr = _run_cli(
        ["import-labels", "--root", str(repo), str(labels_path)], capsys
    )
    assert exit_code == 0
    assert stdout == f"Imported {len(commits) - 1} labels from {labels_path}\n"
    assert stderr == ""


def test_cli_defaults_to_generic_pack_and_lists_versioned_packs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "technology_neutral"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "ruleloom@example.test")
    _git(repo, "config", "user.name", "RuleLoom Test")
    (repo / "service.py").write_text("def run():\n    return True\n", encoding="utf-8")
    _commit(repo, "Initial", "2026-01-01T10:00:00Z")

    exit_code, _, stderr = _run_cli(["init", str(repo)], capsys)
    assert exit_code == 0
    assert stderr == ""
    config = RuleLoomConfig.load(repo)
    assert config.schema_version == 2
    assert (config.pack, config.pack_version) == ("generic_changes", 1)

    exit_code, stdout, stderr = _run_cli(["packs", "list", "--json"], capsys)
    assert exit_code == 0
    assert stderr == ""
    packs = json.loads(stdout)
    assert {(item["name"], item["version"]) for item in packs} == {
        ("configured_paths", 1),
        ("generic_changes", 1),
        ("flutter_testing", 1),
        ("flutter_testing", 2),
    }
    assert (
        next(item for item in packs if item["name"] == "configured_paths")["configurable"] is True
    )
    assert (
        next(item for item in packs if item["name"] == "flutter_testing" and item["latest"])[
            "version"
        ]
        == 2
    )


def test_cli_initializes_and_collects_with_configured_paths(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "configured_monorepo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "ruleloom@example.test")
    _git(repo, "config", "user.name", "RuleLoom Test")
    web = repo / "apps/web/main.ts"
    contract = repo / "packages/shared/schema.json"
    web.parent.mkdir(parents=True)
    contract.parent.mkdir(parents=True)
    web.write_text("export const ready = true;\n", encoding="utf-8")
    contract.write_text('{"version": 1}\n', encoding="utf-8")
    commit = _commit(repo, "Initial surfaces", "2026-01-01T10:00:00Z")

    exit_code, stdout, stderr = _run_cli(
        [
            "init",
            str(repo),
            "--pack",
            "configured_paths",
            "--path-predicate",
            "touches_surface_web=apps/web/**",
            "--path-predicate",
            "touches_shared_contract=packages/shared/**",
            "--path-exclude",
            "touches_surface_web=apps/web/generated/**",
        ],
        capsys,
    )
    assert exit_code == 0
    assert "Initialized RuleLoom" in stdout
    assert stderr == ""
    config = RuleLoomConfig.load(repo)
    assert config.schema_version == 3
    assert config.pack_config is not None
    assert config.pack_config.predicates == (
        "touches_shared_contract",
        "touches_surface_web",
    )

    exit_code, stdout, stderr = _run_cli(
        ["collect", "--root", str(repo), "git", "--last", "1"],
        capsys,
    )
    assert exit_code == 0
    assert stderr == ""
    assert json.loads(stdout)["ids"] == [f"commit.{commit}"]
    observations = load_observations(dataset_path(repo, config))
    assert len(observations) == 1
    assert {
        "touches_surface_web",
        "touches_shared_contract",
    } <= observations[0].facts
    assert observations[0].source["pack_config_hash"] == config.pack_config_hash


def test_cli_rejects_configured_path_flags_for_static_packs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "static_pack"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "ruleloom@example.test")
    _git(repo, "config", "user.name", "RuleLoom Test")
    (repo / "README.md").write_text("# Static\n", encoding="utf-8")
    _commit(repo, "Initial", "2026-01-01T10:00:00Z")

    exit_code, stdout, stderr = _run_cli(
        [
            "init",
            str(repo),
            "--path-predicate",
            "touches_surface_web=apps/web/**",
        ],
        capsys,
    )
    assert exit_code == 2
    assert stdout == ""
    assert "require --pack configured_paths" in stderr


def test_cli_rejects_an_explicit_empty_init_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code, stdout, stderr = _run_cli(["init", ""], capsys)

    assert exit_code == 2
    assert stdout == ""
    assert "init path must not be empty" in stderr


def test_explicit_root_must_name_the_initialized_project_exactly(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "project"
    _create_flutter_history(repo, count=1)
    exit_code, _, stderr = _run_cli(["init", str(repo)], capsys)
    assert exit_code == 0
    assert stderr == ""
    child = repo / "nested"
    child.mkdir()

    for wrong_root, message in (
        (repo / "typo", "not an existing directory"),
        (child, "not an initialized RuleLoom project"),
    ):
        exit_code, stdout, stderr = _run_cli(
            ["readiness", "--root", str(wrong_root)],
            capsys,
        )
        assert exit_code == 2
        assert stdout == ""
        assert message in stderr


def test_collection_merge_rejects_conflicting_pack_version(tmp_path: Path) -> None:
    repo = tmp_path / "conflicting_pack_version"
    commits = _create_flutter_history(repo, count=2)
    collected = collect_snapshot(
        repo,
        commits[0],
        commits[1],
        protocol_hash="e" * 64,
    )
    prior = replace(collected, source={**collected.source, "pack_version": 2})

    with pytest.raises(ModelError, match="conflicting provenance"):
        cli._merge_collected_observation([prior], collected)


def test_collect_git_rejects_mode_arguments_that_would_be_ignored(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "collect_modes"
    commits = _create_flutter_history(repo, count=2)
    exit_code, _, stderr = _run_cli(["init", str(repo), "--agents", "none"], capsys)
    assert exit_code == 0
    assert stderr == ""

    cases = (
        (["--last", "1", "--head", "HEAD"], "--head is valid only with --base"),
        (["--working-tree", "--head", "HEAD"], "--head is valid only with --base"),
        (
            ["--base", commits[0], "--ref", "HEAD"],
            "--ref is valid only with --last or --working-tree",
        ),
        (["--last", "1", "--ref", ""], "unsafe or empty Git revision"),
        (["--base", commits[0], "--head", ""], "unsafe or empty Git revision"),
    )
    for arguments, message in cases:
        exit_code, stdout, stderr = _run_cli(
            ["collect", "--root", str(repo), "git", *arguments],
            capsys,
        )
        assert exit_code == 2
        assert stdout == ""
        assert message in stderr

    exit_code, stdout, stderr = _run_cli(
        [
            "collect",
            "--root",
            str(repo),
            "git",
            "--base",
            commits[0],
            "--head",
            commits[1],
        ],
        capsys,
    )
    assert exit_code == 0
    assert stderr == ""
    payload = json.loads(stdout)
    assert (payload["examined"], payload["eligible"], payload["skipped"]) == (1, 1, 0)


def test_cli_runs_evidence_to_reviewed_policy_workflow(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "example_project"
    commits = _create_flutter_history(repo)

    exit_code, stdout, stderr = _run_cli(
        [
            "init",
            str(repo),
            "--project",
            "example_project",
            "--pack",
            "flutter_testing",
            "--agents",
            "all",
        ],
        capsys,
    )
    assert exit_code == 0
    assert f"Initialized RuleLoom in {repo}" in stdout
    assert "Codex + Claude" in stdout
    assert stderr == ""
    assert (repo / ".ruleloom/config.json").is_file()
    assert (repo / ".agents/skills/ruleloom/SKILL.md").is_file()
    assert (repo / ".claude/skills/ruleloom/SKILL.md").is_file()
    initial_rule_cards = (repo / ".agents/skills/ruleloom/references/approved-rules.md").read_text(
        encoding="utf-8"
    )
    assert "No rule is approved" in initial_rule_cards

    exit_code, _, stderr = _run_cli(["init", str(repo)], capsys)
    assert exit_code == 2
    assert "refusing to overwrite" in stderr

    exit_code, stdout, stderr = _run_cli(
        ["collect", "--root", str(repo), "git", "--last", str(len(commits))],
        capsys,
    )
    assert exit_code == 0
    assert stderr == ""
    collection = json.loads(stdout)
    assert collection["collected"] == len(commits)
    assert collection["inserted"] == len(commits)
    assert collection["updated"] == 0
    assert collection["ids"] == [f"commit.{commit}" for commit in commits]
    assert collection["examined"] == len(commits)
    assert collection["eligible"] == len(commits)
    assert collection["skipped"] == 0
    assert collection["skipped_by_reason"] == {
        "mixed_scope": 0,
        "no_in_scope_files": 0,
    }
    assert collection["skipped_preview"] == []
    assert collection["skipped_preview_truncated"] == 0
    assert len(collection["skipped_manifest_hash"]) == 64

    exit_code, stdout, stderr = _run_cli(["readiness", "--root", str(repo)], capsys)
    assert exit_code == 0
    assert stderr == ""
    initial_readiness = json.loads(stdout)
    assert initial_readiness == {
        "distinct_predicates": 2,
        "fact_evidence_coverage": 1.0,
        "label_evidence_coverage": 0.0,
        "labeled": 0,
        "negative": 0,
        "observations": 12,
        "positive": 0,
        "stage": "collection",
        "unknown": 12,
        "warnings": [
            "fewer than 20 positive outcomes: learn only exploratory rules",
            "12 outcomes remain unknown or censored",
        ],
    }

    exit_code, stdout, stderr = _run_cli(
        ["learn", "--root", str(repo), "--engine", "horn", "--json"],
        capsys,
    )
    assert exit_code == 2
    assert stdout == ""
    assert "no mature labels are available for learning" in stderr
    assert list((repo / ".ruleloom/candidates").glob("*.json")) == []

    _label_history(repo, commits, capsys)
    config = RuleLoomConfig.load(repo)
    observations = load_observations(dataset_path(repo, config))
    assert len(observations) == 12
    assert sum(item.labels[config.target] is LabelValue.POSITIVE for item in observations) == 6
    assert sum(item.labels[config.target] is LabelValue.NEGATIVE for item in observations) == 6
    assert all(config.target in item.label_evidence for item in observations)

    exit_code, stdout, stderr = _run_cli(["readiness", "--root", str(repo)], capsys)
    assert exit_code == 0
    assert stderr == ""
    mature_readiness = json.loads(stdout)
    assert mature_readiness["labeled"] == 12
    assert mature_readiness["positive"] == 6
    assert mature_readiness["negative"] == 6
    assert mature_readiness["unknown"] == 0
    assert mature_readiness["label_evidence_coverage"] == 1.0
    assert mature_readiness["stage"] == "collection"

    exit_code, stdout, stderr = _run_cli(["validate", "--root", str(repo)], capsys)
    assert exit_code == 0
    assert "mature labels: 12" in stdout
    assert stderr == ""

    exit_code, stdout, stderr = _run_cli(
        ["learn", "--root", str(repo), "--engine", "horn", "--json"], capsys
    )
    assert exit_code == 0
    assert stderr == ""
    candidate = json.loads(stdout)
    candidate_id = candidate["id"]
    assert candidate["status"] == "candidate"
    assert candidate["engine"] == "horn"
    assert candidate["train_ids"] == [f"commit.{commit}" for commit in commits[:8]]
    assert candidate["test_ids"] == [f"commit.{commit}" for commit in commits[8:]]
    assert candidate["metrics"]["test"]["precision"] == 1.0
    assert candidate["metrics"]["test"]["recall"] == 1.0
    assert [rule["body"] for rule in candidate["rules"]["clauses"]] == [
        [{"negated": False, "predicate": "uses_async"}]
    ]

    exit_code, stdout, stderr = _run_cli(["candidate", "--root", str(repo), "list"], capsys)
    assert exit_code == 0
    assert stderr == ""
    listing = json.loads(stdout)
    assert listing == [
        {
            "created_at": candidate["created_at"],
            "engine": "horn",
            "id": candidate_id,
            "rules": 1,
            "test_mcc": 1.0,
        }
    ]

    exit_code, stdout, stderr = _run_cli(
        ["candidate", "--root", str(repo), "show", candidate_id], capsys
    )
    assert exit_code == 0
    assert stderr == ""
    assert json.loads(stdout) == candidate

    approve = [
        "promote",
        "--root",
        str(repo),
        candidate_id,
        "--to",
        "approved",
        "--reviewer",
        "test-reviewer",
    ]
    exit_code, stdout, stderr = _run_cli(approve, capsys)
    assert exit_code == 2
    assert stdout == ""
    assert "non-overridable promotion gates failed" in stderr
    assert "recorded shadow transition" in stderr
    assert "prospective shadow evidence" in stderr

    shadow = [
        "promote",
        "--root",
        str(repo),
        candidate_id,
        "--to",
        "shadow",
        "--reviewer",
        "test-reviewer",
    ]
    exit_code, _, stderr = _run_cli(shadow, capsys)
    assert exit_code == 2
    assert "positive outcomes 6 < required 20" in stderr
    exit_code, _, stderr = _run_cli([*shadow, "--override"], capsys)
    assert exit_code == 2
    assert "override requires a non-empty note" in stderr
    exit_code, stdout, stderr = _run_cli(
        [
            *shadow,
            "--override",
            "--note",
            "Pilot shadow-only; never enforce",
        ],
        capsys,
    )
    assert exit_code == 0
    assert f"Promoted {candidate_id} to shadow" in stdout
    assert "Human override recorded" in stdout
    assert stderr == ""
    assert (repo / f".ruleloom/shadow/{candidate_id}.json").is_file()

    exit_code, stdout, stderr = _run_cli(["sync-agents", "--root", str(repo), "--check"], capsys)
    assert exit_code == 0
    assert stdout.count("ok:") == 4
    assert stderr == ""
    for agent_root in (".agents", ".claude"):
        rule_cards = (repo / agent_root / "skills/ruleloom/references/approved-rules.md").read_text(
            encoding="utf-8"
        )
        assert candidate_id not in rule_cards
        assert "No rule is approved" in rule_cards

    pending_change = repo / "lib/change_11.dart"
    pending_change.write_text(
        pending_change.read_text(encoding="utf-8")
        + "\nFuture<void> pending() async => Future<void>.value();\n",
        encoding="utf-8",
    )
    exit_code, stdout, stderr = _run_cli(
        [
            "assess",
            "--root",
            str(repo),
            "--base",
            "HEAD",
            "--change-id",
            "example-e2e-change",
            "--include-shadow",
            "--json",
        ],
        capsys,
    )
    assert exit_code == 2
    assert stdout == ""
    assert "must use --blind" in stderr

    exit_code, stdout, stderr = _run_cli(
        [
            "collect",
            "--root",
            str(repo),
            "git",
            "--working-tree",
            "--ref",
            "HEAD",
        ],
        capsys,
    )
    assert exit_code == 0
    assert stderr == ""
    worktree_collection = json.loads(stdout)
    assert worktree_collection["collected"] == 1
    assert (
        worktree_collection["examined"],
        worktree_collection["eligible"],
        worktree_collection["skipped"],
    ) == (1, 1, 0)

    before_invalid_assessment = load_observations(repo / config.dataset)
    exit_code, stdout, stderr = _run_cli(
        [
            "assess",
            "--root",
            str(repo),
            "--base",
            "HEAD",
            "--change-id",
            "Bad/ID",
            "--include-shadow",
            "--blind",
        ],
        capsys,
    )
    assert exit_code == 2
    assert stdout == ""
    assert "lowercase letters" in stderr
    assert load_observations(repo / config.dataset) == before_invalid_assessment

    exit_code, stdout, stderr = _run_cli(
        [
            "assess",
            "--root",
            str(repo),
            "--base",
            "HEAD",
            "--change-id",
            "example-e2e-change",
            "--include-shadow",
            "--blind",
            "--json",
        ],
        capsys,
    )
    assert exit_code == 0
    assert stderr == ""
    blind = json.loads(stdout)
    assert set(blind) == {
        "blind",
        "observation_id",
        "predicted_at",
        "prediction_id",
        "protocol_hash",
        "recorded",
        "unit_id",
    }
    assert blind["blind"] is True
    assert blind["unit_id"] == "example-e2e-change"
    predictions = load_predictions(repo / config.predictions)
    assert len(predictions) == 1
    assert not predictions[0].abstained
    assert predictions[0].unit_id == "example-e2e-change"
    assert predictions[0].matches[0]["candidate_id"] == candidate_id

    exit_code, stdout, stderr = _run_cli(["report", "--root", str(repo)], capsys)
    assert exit_code == 0
    assert stderr == ""
    grouped = json.loads(stdout)
    assert grouped["readiness"]["observations"] == len(commits) + 1
    assert len(grouped["policy_sets"]) == 1
    report = next(iter(grouped["policy_sets"].values()))
    assert report["predictions"] == 1
    assert report["unique_observations"] == 1
    assert report["still_unknown"] == 1
    assert report["mature_after_prediction"] == 0


def test_doctor_reports_required_checks_without_requiring_popper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(project),
            "-c",
            "user.name=RuleLoom Tests",
            "-c",
            "user.email=tests@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "initial",
        ],
        check=True,
        capture_output=True,
    )
    exit_code, _, stderr = _run_cli(["init", str(project)], capsys)
    assert exit_code == 0
    assert stderr == ""

    monkeypatch.setattr(
        cli.shutil,
        "which",
        lambda command, path=None: "/tools/git" if command == "git" else None,
    )
    exit_code, stdout, stderr = _run_cli(["doctor", "--root", str(project)], capsys)
    assert exit_code == 0
    assert stderr == ""
    checks = json.loads(stdout)
    assert checks["python"]["ok"] is True
    assert checks["git"] == {"ok": True, "path": "/tools/git"}
    assert checks["project"]["ok"] is True
    assert checks["project"]["root"] == str(project)
    assert checks["popper_optional"]["ok"] is False
    assert checks["popper_optional"]["runtime_probe"] is False
    assert checks["popper_optional"]["required_for"] == "learner.engine=popper only"

    config = RuleLoomConfig.load(project)
    unsafe = replace(
        config,
        learner=LearnerConfig(
            engine="popper",
            max_rules=1,
            bootstrap_runs=0,
            popper_dir=str(project / "vendor" / "Popper"),
        ),
    )
    (project / ".ruleloom/config.json").write_text(json.dumps(unsafe.to_dict()), encoding="utf-8")
    exit_code, stdout, stderr = _run_cli(
        ["doctor", "--root", str(project), "--probe-popper-runtime"], capsys
    )
    assert exit_code == 2
    assert stdout == ""
    assert "inside the repository" in stderr

    external_checkout = tmp_path / "external-popper"
    external_checkout.mkdir()
    safe = replace(
        unsafe,
        learner=replace(unsafe.learner, popper_dir=str(external_checkout)),
    )
    (project / ".ruleloom/config.json").write_text(json.dumps(safe.to_dict()), encoding="utf-8")
    probed: dict[str, object] = {}

    def ready_popper(
        popper_dir: str | Path | None,
        *,
        probe_runtime: bool,
    ) -> PopperDoctorReport:
        probed["popper_dir"] = popper_dir
        probed["probe_runtime"] = probe_runtime
        return PopperDoctorReport(requirements=())

    monkeypatch.setattr("ruleloom.learners.popper.doctor_popper", ready_popper)
    exit_code, stdout, stderr = _run_cli(
        ["doctor", "--root", str(project), "--probe-popper-runtime"], capsys
    )
    assert exit_code == 0
    assert stderr == ""
    checks = json.loads(stdout)
    assert checks["popper_optional"]["ok"] is True
    assert checks["popper_optional"]["required"] is True
    assert checks["popper_optional"]["runtime_probe"] is True
    assert probed == {
        "popper_dir": external_checkout.resolve(),
        "probe_runtime": True,
    }

    missing = tmp_path / "not-initialized"
    missing.mkdir()
    exit_code, stdout, stderr = _run_cli(["doctor", "--root", str(missing)], capsys)
    assert exit_code == 1
    assert stderr == ""
    checks = json.loads(stdout)
    assert checks["project"]["ok"] is False
    assert "not an initialized RuleLoom project" in checks["project"]["detail"]

    exit_code, stdout, stderr = _run_cli(
        ["doctor", "--root", str(missing), "--probe-popper-runtime"], capsys
    )
    assert exit_code == 2
    assert stdout == ""
    assert "requires an initialized project" in stderr
