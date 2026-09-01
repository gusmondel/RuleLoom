from __future__ import annotations

import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import ruleloom.first_hour as first_hour
from ruleloom.first_hour import (
    FirstHourAuditError,
    RepositoryAuditLimits,
    audit_repository,
    build_first_hour_report,
)
from ruleloom.history.git import collect_git_history
from ruleloom.history.models import ChangeUnit
from ruleloom.models import LabelEvidence, LabelValue, Observation, RuleLiteral
from ruleloom.repository_assertions import (
    RepositoryAssertion,
    RepositoryAssertionManifest,
    RepositoryAssertionSourceRef,
    declare_repository_assertions,
)

PROTOCOL_HASH = "a" * 64
TARGET = "validation_rework_required"


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str, day: int) -> None:
    timestamp = f"2026-01-{day:02d}T12:00:00+00:00"
    env = {
        "PATH": __import__("os").environ["PATH"],
        "GIT_AUTHOR_NAME": "RuleLoom Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "RuleLoom Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "GIT_AUTHOR_DATE": timestamp,
        "GIT_COMMITTER_DATE": timestamp,
    }
    _git(repo, "add", ".", env=env)
    _git(repo, "commit", "-m", message, env=env)


def _empty_commit(repo: Path, message: str, day: int) -> None:
    timestamp = f"2026-01-{day:02d}T12:00:00+00:00"
    env = {
        "PATH": __import__("os").environ["PATH"],
        "GIT_AUTHOR_NAME": "RuleLoom Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "RuleLoom Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "GIT_AUTHOR_DATE": timestamp,
        "GIT_COMMITTER_DATE": timestamp,
    }
    _git(repo, "commit", "--allow-empty", "-m", message, env=env)


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.txt").write_text("a\nb\nc\n", encoding="utf-8")
    (repo / "b.txt").write_text("one\n", encoding="utf-8")
    _commit(repo, "first", 1)
    (repo / "a.txt").write_text("a\nb\nc\nd\ne\n", encoding="utf-8")
    (repo / "b.txt").write_text("one\ntwo\n", encoding="utf-8")
    _commit(repo, "second", 2)
    (repo / "a.txt").write_text("a\nb\nc\nd\ne\nf\n", encoding="utf-8")
    _commit(repo, "third", 3)
    return repo


def _observation(index: int, facts: set[str]) -> Observation:
    return Observation(
        id=f"audit-observation-{index}",
        observed_at=datetime(2026, 2, index, tzinfo=UTC).isoformat(),
        protocol_hash=PROTOCOL_HASH,
        facts=frozenset(facts),
        labels={TARGET: LabelValue.UNKNOWN},
        source={"repository": "example.repository"},
        metadata={"topological_index": index},
    )


def test_zero_config_audit_is_read_only_deterministic_and_useful(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    before = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")

    first = build_first_hour_report(
        repo,
        limits=RepositoryAuditLimits(max_commits=20, min_cochange_count=2),
    )
    second = build_first_hour_report(
        repo,
        limits=RepositoryAuditLimits(max_commits=20, min_cochange_count=2),
    )

    assert first.to_dict() == second.to_dict()
    assert first.outcome_blind is True
    assert first.read_only is True
    assert first.topology["commit_count"] == 3
    assert first.topology["root_commits"] == 1
    assert first.volume["total_churn"] == 8
    assert first.volume["churn_per_commit"] == {
        "count": 3,
        "minimum": 1,
        "p25": 1,
        "median": 3,
        "p75": 4,
        "p90": 4,
        "p95": 4,
        "maximum": 4,
        "method": "nearest_rank",
    }
    first_hotspot = first.hotspots[0]
    first_cochange = first.cochanges[0]
    assert isinstance(first_hotspot, dict)
    assert isinstance(first_cochange, dict)
    assert first_hotspot["path"] == "a.txt"
    assert first_hotspot["change_count"] == 3
    assert first_cochange["left_path"] == "a.txt"
    assert first_cochange["right_path"] == "b.txt"
    assert first_cochange["cochange_count"] == 2
    assert first.predicate_quality["status"] == "not_supplied"
    assert first.structural_test_expectations["status"] == "not_configured"
    assert len(first.manifest_hash) == 64
    assert len(first.evidence_manifest_hash) == 64
    assert (
        audit_repository(
            repo,
            limits=RepositoryAuditLimits(max_commits=20, min_cochange_count=2),
        ).to_dict()
        == first.to_dict()
    )
    rendered = first.render_text()
    assert rendered.startswith("RuleLoom repository structure audit\n")
    assert 'Most frequently changed paths\n- "a.txt": 3 changes' in rendered
    assert rendered.endswith(f"Manifest: {first.manifest_hash}\n")
    assert not (repo / ".ruleloom").exists()
    assert _git(repo, "status", "--porcelain=v1", "--untracked-files=all") == before


def test_audit_reports_truncation_and_cochange_coverage_budgets(tmp_path: Path) -> None:
    repo = _repository(tmp_path)

    report = build_first_hour_report(
        repo,
        limits=RepositoryAuditLimits(
            max_commits=2,
            min_cochange_count=1,
            max_cochange_paths_per_commit=1,
        ),
    )

    assert report.topology["commit_count"] == 2
    assert report.topology["truncated"] is True
    cochange = report.coverage["cochange"]
    assert isinstance(cochange, dict)
    assert cochange["skipped_large_commits"] == 1
    assert any("co-change excluded" in warning for warning in report.warnings)
    rendered = report.render_text()
    assert "Co-change coverage\n- Eligible commits: 1; skipped above path budget: 1" in rendered
    warning_block = rendered.split("Warnings\n", 1)[1].split("\n\nPredicate quality", 1)[0]
    assert report.warnings
    for warning in report.warnings:
        assert f"- {warning}" in warning_block


@pytest.mark.parametrize(("budget", "expected_batch_calls"), ((1, 1), (2, 2)))
def test_path_entry_budget_fails_before_collecting_later_batches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    budget: int,
    expected_batch_calls: int,
) -> None:
    repo = _repository(tmp_path)
    original_batch = first_hour._collect_numstat_batch
    original_single = first_hour._collect_numstat_single
    batch_calls: list[tuple[str, ...]] = []
    root_calls: list[str] = []

    def record_batch(
        root: Path,
        units: tuple[ChangeUnit, ...],
        *,
        max_path_entries: int | None = None,
    ) -> tuple[tuple[first_hour._PathChange, ...], ...]:
        batch_calls.append(tuple(item.prediction_sha for item in units))
        return original_batch(root, units, max_path_entries=max_path_entries)

    def record_root(
        root: Path,
        unit: ChangeUnit,
        *,
        max_path_entries: int | None = None,
    ) -> tuple[first_hour._PathChange, ...]:
        root_calls.append(unit.prediction_sha)
        return original_single(root, unit, max_path_entries=max_path_entries)

    monkeypatch.setattr(first_hour, "_collect_numstat_batch", record_batch)
    monkeypatch.setattr(first_hour, "_collect_numstat_single", record_root)

    with pytest.raises(FirstHourAuditError, match="max_total_path_entries"):
        audit_repository(
            repo,
            limits=RepositoryAuditLimits(
                max_commits=20,
                diff_batch_size=1,
                max_total_path_entries=budget,
            ),
        )

    assert len(batch_calls) == expected_batch_calls
    assert root_calls == []


def test_batch_parser_aborts_before_materializing_records_above_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = "1" * 40
    head = "2" * 40
    unit = ChangeUnit(
        id="bounded-parser",
        repository_id="example.repository",
        kind="git_only",
        base_sha=base,
        prediction_sha=head,
        prediction_at="2026-01-01T00:00:00Z",
        commits=(head,),
        event_ids=(),
        provider="git",
        source_ref=head,
        evidence_quality="git_only",
        confirmatory=False,
    )
    stdout = b"\x00".join(
        (
            head.encode("ascii"),
            *(f"1\t0\t.ruleloom/internal-{index}.json".encode() for index in range(100)),
            b"",
        )
    )

    def synthetic_diff(
        root: Path,
        arguments: tuple[str, ...],
        *,
        input_bytes: bytes | None = None,
    ) -> tuple[bytes, bytes, int]:
        del root, arguments, input_bytes
        return stdout, b"", 0

    original_parse = first_hour._parse_numstat_record
    parsed_records: list[bytes] = []

    def record_parse(raw_record: bytes, *, commit: str) -> first_hour._PathChange:
        parsed_records.append(raw_record)
        return original_parse(raw_record, commit=commit)

    monkeypatch.setattr(first_hour, "_run_git_capped", synthetic_diff)
    monkeypatch.setattr(first_hour, "_parse_numstat_record", record_parse)

    with pytest.raises(FirstHourAuditError, match="max_total_path_entries"):
        first_hour._collect_numstat_batch(
            Path("."),
            (unit,),
            max_path_entries=3,
        )

    # Internal paths are excluded from the report, but still consume parser
    # memory and therefore count against this safety budget.
    assert len(parsed_records) == 3


def test_batch_diff_preserves_individual_numstat_evidence_and_bounds_processes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repository(tmp_path)
    unusual_path = repo / "odd\tname\nrow.txt"
    unusual_path.write_text("one\ntwo\n", encoding="utf-8")
    _commit(repo, "unusual path", 4)
    _empty_commit(repo, "empty commit", 5)
    history = collect_git_history(repo, max_commits=None)
    root_predictions = {
        event.source_ref
        for event in history.events
        if isinstance(event.data.get("parents"), list) and not event.data["parents"]
    }
    ordinary = tuple(unit for unit in history.units if unit.prediction_sha not in root_predictions)

    batched = first_hour._collect_numstat_batch(repo, ordinary)
    individual = tuple(first_hour._collect_numstat_single(repo, unit) for unit in ordinary)

    assert batched == individual
    assert any(change.path == unusual_path.name for changes in batched for change in changes)

    original = first_hour._run_git_capped  # type: ignore[attr-defined]
    diff_commands: list[tuple[str, ...]] = []

    def record_diff_commands(
        target: Path,
        arguments: tuple[str, ...],
        *,
        input_bytes: bytes | None = None,
    ) -> tuple[bytes, bytes, int]:
        diff_commands.append(arguments)
        return original(target, arguments, input_bytes=input_bytes)

    monkeypatch.setattr(first_hour, "_run_git_capped", record_diff_commands)
    report = audit_repository(
        repo,
        limits=RepositoryAuditLimits(max_commits=20, diff_batch_size=128),
    )

    assert report.topology["commit_count"] == 5
    assert [arguments[0] for arguments in diff_commands] == ["diff-tree", "diff"]

    single_process_report = audit_repository(
        repo,
        limits=RepositoryAuditLimits(max_commits=20, diff_batch_size=1),
    )
    assert single_process_report.evidence_manifest_hash == report.evidence_manifest_hash
    assert single_process_report.volume == report.volume


def test_text_report_escapes_untrusted_git_paths_to_one_line(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    injected = repo / "line\nFAKE REPORT LINE\n\x1b[31mred.txt"
    injected.write_text("payload\n", encoding="utf-8")
    (repo / "partner.txt").write_text("partner\n", encoding="utf-8")
    _commit(repo, "untrusted path bytes", 4)

    rendered = audit_repository(
        repo,
        limits=RepositoryAuditLimits(
            max_commits=20,
            min_cochange_count=1,
        ),
    ).render_text()

    assert "\x1b" not in rendered
    assert "\nFAKE REPORT LINE\n" not in rendered
    escaped = r'"line\nFAKE REPORT LINE\n\u001b[31mred.txt"'
    assert rendered.count(escaped) >= 2


def test_optional_vocabulary_and_assertions_enrich_without_reading_labels(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    (repo / "ENGINEERING.md").write_text(
        "Repository expectations\nSource changes require test changes.\n",
        encoding="utf-8",
    )
    manifest = RepositoryAssertionManifest(
        (
            RepositoryAssertion(
                assertion_id="source_requires_tests",
                revision=1,
                summary="Source contact expects test contact in the same change.",
                category="test_structure",
                antecedent=(RuleLiteral("touches_source"),),
                expectation=(RuleLiteral("touches_tests"),),
                sources=(RepositoryAssertionSourceRef("ENGINEERING.md", 2, 2),),
            ),
        )
    )
    declaration = declare_repository_assertions(
        repo,
        manifest,
        repository_id="example.repository",
        protocol_hash=PROTOCOL_HASH,
        predicate_vocabulary=("touches_source", "touches_tests"),
        declared_at=datetime(2026, 2, 10, tzinfo=UTC),
    )
    observations = [
        _observation(1, {"touches_source", "touches_tests"}),
        _observation(2, {"touches_source"}),
        _observation(3, {"touches_tests"}),
    ]
    labelled = [
        replace(
            item,
            labels={
                TARGET: LabelValue.POSITIVE if index % 2 == 0 else LabelValue.NEGATIVE,
            },
            label_evidence={
                TARGET: LabelEvidence(
                    kind="synthetic",
                    available_at=(
                        datetime.fromisoformat(item.observed_at) + timedelta(days=1)
                    ).isoformat(),
                    source="ignored-by-structural-audit",
                )
            },
        )
        for index, item in enumerate(observations)
    ]
    limits = RepositoryAuditLimits(max_commits=20)
    unknown_report = build_first_hour_report(
        repo,
        observations=observations,
        limits=limits,
        predicate_vocabulary=("touches_source", "touches_tests"),
        configured_predicates=("touches_source", "touches_tests"),
        assertion_declaration=declaration,
    )
    labelled_report = build_first_hour_report(
        repo,
        observations=labelled,
        limits=limits,
        predicate_vocabulary=("touches_source", "touches_tests"),
        configured_predicates=("touches_source", "touches_tests"),
        assertion_declaration=declaration,
    )

    assert unknown_report.to_dict() == labelled_report.to_dict()
    assert unknown_report.predicate_quality["status"] == "available"
    structural = unknown_report.structural_test_expectations
    assert structural["status"] == "available"
    assert structural["eligible_assertion_observation_pairs"] == 2
    assert structural["expectation_absent_pairs"] == 1
    assert unknown_report.assertion_adherence["status"] == "available"
