from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ruleloom.learners.popper import (
    PopperConfigurationError,
    PopperDependencyError,
    PopperExportError,
    PopperParseError,
    PopperRunError,
    doctor_popper,
    export_popper_problem,
    fingerprint_popper,
    learn_popper,
    locate_popper,
    parse_popper_rules,
    run_popper,
)
from ruleloom.models import LabelEvidence, LabelValue, Observation

TARGET = "needs_extra_validation"
PROTOCOL_HASH = "e" * 64
PROBE_MANIFEST = '{"packages":[["clingo","5.8.0"],["popper-ilp","0.1.0"]],"python":[3,14,0]}'


def observation(
    item_id: str,
    facts: set[str],
    label: LabelValue = LabelValue.UNKNOWN,
) -> Observation:
    label_evidence = (
        {}
        if label is LabelValue.UNKNOWN
        else {
            TARGET: LabelEvidence(
                kind="synthetic",
                available_at="2026-08-31T12:01:00+00:00",
                source="test",
            )
        }
    )
    return Observation(
        id=item_id,
        observed_at="2026-08-31T12:00:00+00:00",
        protocol_hash=PROTOCOL_HASH,
        facts=frozenset(facts),
        labels={TARGET: label},
        label_evidence=label_evidence,
    )


def fake_checkout(tmp_path: Path) -> Path:
    checkout = tmp_path / "Popper"
    checkout.mkdir(parents=True)
    (checkout / "popper.py").write_text("#!/usr/bin/env python\n", encoding="utf-8")
    python = checkout / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o755)
    return checkout


def available_command(name: str) -> str | None:
    paths = {"swipl": "/tools/swipl", "timeout": "/tools/timeout"}
    return paths.get(name)


def successful_probe(command: tuple[str, ...], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, 0, stdout=PROBE_MANIFEST + "\n", stderr="")


def test_export_popper_problem_writes_examples_closed_world_and_bias(tmp_path: Path) -> None:
    observations = [
        observation("commit-1", {"changes_dart", "uses_async"}, LabelValue.POSITIVE),
        observation("commit.2", {"changes_dart"}, LabelValue.NEGATIVE),
        observation("ignored", {"touches_auth"}),
    ]

    problem = export_popper_problem(
        observations,
        TARGET,
        tmp_path / "problem",
        max_body=2,
        max_rules=4,
    )

    assert problem.positive_ids == ("commit-1",)
    assert problem.negative_ids == ("commit.2",)
    assert problem.predicates == ("uses_async", "changes_dart")
    assert problem.examples_path.read_text(encoding="utf-8").splitlines()[1:] == [
        "pos(needs_extra_validation('commit-1')).",
        "neg(needs_extra_validation('commit.2')).",
    ]
    assert problem.background_path.read_text(encoding="utf-8").splitlines()[1:] == [
        "uses_async('commit-1').",
        "changes_dart('commit-1').",
        "not_uses_async('commit.2').",
        "changes_dart('commit.2').",
    ]
    bias = problem.bias_path.read_text(encoding="utf-8")
    assert "max_body(2)." in bias
    assert "max_vars(1)." in bias
    assert "max_clauses(4)." in bias
    assert "head_pred(needs_extra_validation,1)." in bias
    assert "body_pred(uses_async,1)." in bias
    assert "body_pred(not_uses_async,1)." in bias
    assert "type(not_uses_async,(observation,))." in bias
    assert "direction(not_uses_async,(in,))." in bias
    assert "#count{P,A,Vars : body_literal(C,P,A,Vars)} == 0" in bias
    assert "ignored" not in problem.background_path.read_text(encoding="utf-8")


def test_export_ranks_predicates_before_applying_the_limit(tmp_path: Path) -> None:
    problem = export_popper_problem(
        [
            observation("positive-1", {"high_signal", "common"}, LabelValue.POSITIVE),
            observation("positive-2", {"high_signal", "common"}, LabelValue.POSITIVE),
            observation("negative-1", {"common"}, LabelValue.NEGATIVE),
            observation("negative-2", {"common"}, LabelValue.NEGATIVE),
        ],
        TARGET,
        tmp_path / "problem",
        max_predicates=1,
    )

    assert problem.predicates == ("high_signal",)
    assert "body_pred(high_signal,1)." in problem.bias_path.read_text(encoding="utf-8")
    assert "common" not in problem.background_path.read_text(encoding="utf-8")


def test_export_can_hide_closed_world_predicates_from_hypothesis_space(tmp_path: Path) -> None:
    problem = export_popper_problem(
        [
            observation("positive", {"uses_async"}, LabelValue.POSITIVE),
            observation("negative", set(), LabelValue.NEGATIVE),
        ],
        TARGET,
        tmp_path / "problem",
        allow_negation=False,
    )

    assert "not_uses_async('negative')." in problem.background_path.read_text(encoding="utf-8")
    assert "body_pred(not_uses_async,1)." not in problem.bias_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("items", "target", "message"),
    [
        ([observation("unknown", {"fact"})], TARGET, "no positive or negative"),
        (
            [observation("labelled", set(), LabelValue.POSITIVE)],
            TARGET,
            "contain no facts",
        ),
        (
            [observation("labelled", {TARGET}, LabelValue.POSITIVE)],
            TARGET,
            "leak",
        ),
        (
            [observation("labelled", {"not_async"}, LabelValue.POSITIVE)],
            TARGET,
            "reserved",
        ),
        (
            [
                observation("duplicate", {"fact"}, LabelValue.POSITIVE),
                observation("duplicate", {"fact"}, LabelValue.NEGATIVE),
            ],
            TARGET,
            "unique ids",
        ),
    ],
)
def test_export_rejects_unsafe_or_unlearnable_data(
    tmp_path: Path,
    items: list[Observation],
    target: str,
    message: str,
) -> None:
    with pytest.raises(PopperExportError, match=message):
        export_popper_problem(items, target, tmp_path / "problem")


@pytest.mark.parametrize(
    ("option", "value"),
    [("max_body", 0), ("max_rules", True), ("max_predicates", -1)],
)
def test_export_rejects_non_positive_search_limits(
    tmp_path: Path,
    option: str,
    value: int,
) -> None:
    with pytest.raises(PopperExportError, match=rf"{option} must be an integer >= 1"):
        export_popper_problem(
            [observation("positive", {"fact"}, LabelValue.POSITIVE)],
            TARGET,
            tmp_path / "problem",
            **{option: value},
        )


@pytest.mark.parametrize(
    ("target", "message"),
    [("Bad Target", "target"), ("not_outcome", "reserved")],
)
def test_export_rejects_invalid_or_reserved_targets(
    tmp_path: Path,
    target: str,
    message: str,
) -> None:
    with pytest.raises(PopperExportError, match=message):
        export_popper_problem(
            [observation("positive", {"fact"}, LabelValue.POSITIVE)],
            target,
            tmp_path / "problem",
        )


def test_locate_popper_prefers_explicit_config_then_environment(tmp_path: Path) -> None:
    explicit = fake_checkout(tmp_path / "explicit")
    configured_by_env = fake_checkout(tmp_path / "environment")

    assert (
        locate_popper(explicit, environ={"POPPER_HOME": str(configured_by_env)})
        == (explicit / "popper.py").resolve()
    )
    assert (
        locate_popper(environ={"POPPER_HOME": str(configured_by_env)})
        == (configured_by_env / "popper.py").resolve()
    )
    assert locate_popper(explicit / "popper.py") == (explicit / "popper.py").resolve()


def test_locate_popper_reports_missing_configuration_and_script(tmp_path: Path) -> None:
    with pytest.raises(PopperConfigurationError, match="not configured"):
        locate_popper(environ={"POPPER_HOME": ""})
    with pytest.raises(PopperConfigurationError, match="not found"):
        locate_popper(tmp_path / "missing")


def test_doctor_checks_commands_provisioned_python_and_import_smoke(tmp_path: Path) -> None:
    checkout = fake_checkout(tmp_path)
    recorded: dict[str, Any] = {}

    def probe_runner(command: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        recorded["command"] = command
        recorded.update(kwargs)
        return successful_probe(command)

    report = doctor_popper(
        checkout,
        which=available_command,
        probe_runner=probe_runner,
        probe_runtime=True,
    )

    assert report.ready
    assert report.missing == ()
    assert [item.name for item in report.requirements] == [
        "swipl",
        "timeout",
        "popper.py",
        "popper-python",
        "popper-smoke",
    ]
    assert report.require_path("swipl") == Path("/tools/swipl")
    assert report.require_path("timeout") == Path("/tools/timeout")
    assert report.require_path("popper.py") == (checkout / "popper.py").resolve()
    assert report.require_path("popper-python") == (checkout / ".venv/bin/python").resolve()
    probe_command = recorded["command"]
    assert probe_command[:2] == (str((checkout / ".venv/bin/python").resolve()), "-c")
    assert "importlib.import_module('clingo')" in probe_command[2]
    assert "importlib.import_module('popper.loop')" in probe_command[2]
    assert "m.distributions()" in probe_command[2]
    assert recorded["cwd"] == checkout.resolve()
    assert recorded["timeout"] == 10
    expected_engine_version = (
        f"{fingerprint_popper(checkout)}/env-sha256:"
        f"{hashlib.sha256(PROBE_MANIFEST.encode()).hexdigest()}"
    )
    assert report.runtime_fingerprint == expected_engine_version

    missing = doctor_popper(
        checkout,
        which=lambda _name: None,
        probe_runner=successful_probe,
        probe_runtime=True,
    )
    assert not missing.ready
    assert missing.missing == ("swipl", "timeout")
    with pytest.raises(PopperDependencyError, match="swipl, timeout"):
        missing.require_ready()
    with pytest.raises(PopperDependencyError, match="swipl, timeout"):
        missing.require_path("not-present")


def test_doctor_is_static_until_runtime_probe_is_explicit(tmp_path: Path) -> None:
    checkout = fake_checkout(tmp_path)
    executed = False

    def forbidden_probe(
        command: tuple[str, ...], **_kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        nonlocal executed
        executed = True
        return successful_probe(command)

    report = doctor_popper(
        checkout,
        which=available_command,
        probe_runner=forbidden_probe,
    )

    assert not executed
    assert not report.ready
    assert report.missing == ("popper-python", "popper-smoke")
    assert all(
        "runtime not executed" in item.detail
        for item in report.requirements
        if item.name in {"popper-python", "popper-smoke"}
    )


def test_doctor_rejects_missing_or_incompatible_provisioned_python(tmp_path: Path) -> None:
    missing_checkout = fake_checkout(tmp_path / "missing")
    (missing_checkout / ".venv/bin/python").unlink()

    missing = doctor_popper(
        missing_checkout,
        environ={"VIRTUAL_ENV": ""},
        which=available_command,
        probe_runner=successful_probe,
        probe_runtime=True,
    )

    assert missing.missing == ("popper-python", "popper-smoke")
    assert missing.runtime_fingerprint is None

    incompatible_checkout = fake_checkout(tmp_path / "incompatible")

    def failed_probe(command: tuple[str, ...], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="ModuleNotFoundError: No module named 'clingo'",
        )

    incompatible = doctor_popper(
        incompatible_checkout,
        which=available_command,
        probe_runner=failed_probe,
        probe_runtime=True,
    )

    assert incompatible.missing == ("popper-python", "popper-smoke")
    smoke = next(item for item in incompatible.requirements if item.name == "popper-smoke")
    assert "No module named 'clingo'" in smoke.detail
    assert incompatible.runtime_fingerprint is None


def test_fingerprint_captures_checkout_source_changes(tmp_path: Path) -> None:
    checkout = fake_checkout(tmp_path)
    first = fingerprint_popper(checkout)
    (checkout / "popper/module.py").parent.mkdir()
    (checkout / "popper/module.py").write_text("VALUE = 1\n", encoding="utf-8")
    second = fingerprint_popper(checkout)

    assert first.startswith("popper/git:")
    assert first != second


def test_parse_popper_rules_translates_closed_world_literals_and_deduplicates() -> None:
    output = """
********** SOLUTION **********
Precision:0.88 Recall:0.70 TP:7 FN:3 TN:9 FP:1 Size:5 MDL:9
needs_extra_validation(V0):- uses_async(V0),not_adds_widget_test(V0).
needs_extra_validation(A) :- touches_navigation(A).
needs_extra_validation(V0):- uses_async(V0),not_adds_widget_test(V0).
******************************
"""

    rules = parse_popper_rules(output, TARGET)

    assert [clause.signature for clause in rules.clauses] == [
        "needs_extra_validation:-not_adds_widget_test,uses_async",
        "needs_extra_validation:-touches_navigation",
    ]
    first = rules.clauses[0]
    assert first.body[0].predicate == "adds_widget_test"
    assert first.body[0].negated
    assert not first.body[1].negated


def test_parser_rejects_partial_solution_with_an_unsupported_target_clause() -> None:
    output = """********** SOLUTION **********
needs_extra_validation(A) :- uses_async(A).
needs_extra_validation(A).
******************************
"""

    with pytest.raises(PopperParseError, match="unsupported or malformed"):
        parse_popper_rules(output, TARGET)


def test_parse_no_solution_returns_empty_rule_set() -> None:
    rules = parse_popper_rules("NO SOLUTION\n", TARGET)
    assert rules.target == TARGET
    assert rules.clauses == ()


def test_parser_rejects_invalid_target_timeout_and_duplicate_literals() -> None:
    with pytest.raises(PopperParseError, match="target"):
        parse_popper_rules("NO SOLUTION\n", "Bad Target")
    with pytest.raises(PopperParseError, match="timed out"):
        parse_popper_rules("TIMEOUT OF 30 SECONDS EXCEEDED", TARGET)
    with pytest.raises(PopperParseError, match="duplicate literals"):
        parse_popper_rules(
            "needs_extra_validation(A):- uses_async(A),uses_async(A).",
            TARGET,
        )


def test_parser_uses_only_final_solution_and_rejects_intermediate_only() -> None:
    output = """0.1s ********************
0.1s New best hypothesis:
0.1s needs_extra_validation(A):- payment(A).
0.1s ********************
0.2s ********** SOLUTION **********
0.2s Precision:1.00 Recall:1.00 TP:2 FN:0 TN:2 FP:0 Size:2 MDL:2
0.2s needs_extra_validation(A):- uses_async(A).
0.2s ******************************
"""

    rules = parse_popper_rules(output, TARGET)

    assert [clause.signature for clause in rules.clauses] == ["needs_extra_validation:-uses_async"]
    with pytest.raises(PopperParseError, match="intermediate hypotheses"):
        parse_popper_rules(
            "New best hypothesis:\nneeds_extra_validation(A):- payment(A).\n",
            TARGET,
        )


@pytest.mark.parametrize(
    ("output", "message"),
    [
        ("needs_extra_validation(A).", "malformed"),
        (
            "needs_extra_validation(A):- uses_async(B).",
            "share the head variable",
        ),
        (
            "needs_extra_validation(A):- needs_extra_validation(A).",
            "recursive",
        ),
        ("inv1(A):- uses_async(A).", "unsupported learned predicate"),
        ("some unrelated output", "neither a supported rule"),
    ],
)
def test_parse_rejects_output_outside_supported_fragment(output: str, message: str) -> None:
    with pytest.raises(PopperParseError, match=message):
        parse_popper_rules(output, TARGET)


def test_run_popper_uses_noisy_mode_and_two_timeouts(tmp_path: Path) -> None:
    checkout = fake_checkout(tmp_path / "checkout")
    problem = export_popper_problem(
        [
            observation("positive", {"uses_async"}, LabelValue.POSITIVE),
            observation("negative", set(), LabelValue.NEGATIVE),
        ],
        TARGET,
        tmp_path / "problem",
    )
    recorded: dict[str, Any] = {}

    def runner(command: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        recorded["command"] = command
        recorded.update(kwargs)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="needs_extra_validation(A):- uses_async(A).\n",
            stderr="",
        )

    result = run_popper(
        problem,
        popper_dir=checkout,
        timeout_seconds=37,
        max_body=2,
        which=available_command,
        doctor_runner=successful_probe,
        runner=runner,
    )

    assert result.rules.clauses[0].signature == "needs_extra_validation:-uses_async"
    command = recorded["command"]
    assert command[:2] == (str((checkout / ".venv/bin/python").resolve()), "popper.py")
    assert "--noisy" in command
    assert command[command.index("--timeout") + 1] == "37"
    assert command[command.index("--max-body") + 1] == "2"
    assert command[command.index("--max-vars") + 1] == "1"
    assert recorded["cwd"] == checkout.resolve()
    assert recorded["timeout"] == 42
    assert recorded["capture_output"] is True
    assert recorded["check"] is False
    assert result.engine_version == (
        f"{fingerprint_popper(checkout)}/env-sha256:"
        f"{hashlib.sha256(PROBE_MANIFEST.encode()).hexdigest()}"
    )


def test_run_popper_reports_dependencies_exit_and_process_timeout(tmp_path: Path) -> None:
    checkout = fake_checkout(tmp_path / "checkout")
    problem = export_popper_problem(
        [observation("positive", {"fact"}, LabelValue.POSITIVE)],
        TARGET,
        tmp_path / "problem",
    )

    with pytest.raises(PopperDependencyError, match="swipl"):
        run_popper(
            problem,
            popper_dir=checkout,
            which=lambda name: "/tools/timeout" if name == "timeout" else None,
            doctor_runner=successful_probe,
        )

    def failed(command: tuple[str, ...], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 7, stdout="", stderr="solver failed")

    with pytest.raises(PopperRunError, match=r"status 7:\nsolver failed"):
        run_popper(
            problem,
            popper_dir=checkout,
            which=available_command,
            doctor_runner=successful_probe,
            runner=failed,
        )

    def timed_out(command: tuple[str, ...], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, 125)

    with pytest.raises(PopperRunError, match="120-second"):
        run_popper(
            problem,
            popper_dir=checkout,
            which=available_command,
            doctor_runner=successful_probe,
            runner=timed_out,
        )

    def internal_timeout(
        command: tuple[str, ...], **_kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "TIMEOUT OF 120 SECONDS EXCEEDED\n"
                "********** SOLUTION **********\n"
                "needs_extra_validation(A):- fact(A).\n"
                "******************************\n"
            ),
            stderr="",
        )

    with pytest.raises(PopperRunError, match="internal timeout"):
        run_popper(
            problem,
            popper_dir=checkout,
            which=available_command,
            doctor_runner=successful_probe,
            runner=internal_timeout,
        )


def test_run_popper_enforces_final_clause_limit(tmp_path: Path) -> None:
    checkout = fake_checkout(tmp_path / "checkout")
    problem = export_popper_problem(
        [observation("positive", {"fact", "uses_async"}, LabelValue.POSITIVE)],
        TARGET,
        tmp_path / "problem",
    )

    def runner(command: tuple[str, ...], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "********** SOLUTION **********\n"
                "needs_extra_validation(A):- fact(A).\n"
                "needs_extra_validation(A):- uses_async(A).\n"
                "******************************\n"
            ),
            stderr="",
        )

    with pytest.raises(PopperRunError, match="exceeding max_rules=1"):
        run_popper(
            problem,
            popper_dir=checkout,
            max_rules=1,
            which=available_command,
            doctor_runner=successful_probe,
            runner=runner,
        )


def test_run_popper_rejects_process_and_hypothesis_boundary_violations(tmp_path: Path) -> None:
    checkout = fake_checkout(tmp_path / "checkout")
    problem = export_popper_problem(
        [observation("positive", {"fact", "uses_async"}, LabelValue.POSITIVE)],
        TARGET,
        tmp_path / "problem",
    )

    with pytest.raises(PopperConfigurationError, match="requires max_rules=1"):
        run_popper(problem, popper_dir=checkout, max_rules=2)

    def cannot_start(command: tuple[str, ...], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise OSError("permission denied")

    with pytest.raises(PopperRunError, match="could not start Popper: permission denied"):
        run_popper(
            problem,
            popper_dir=checkout,
            which=available_command,
            doctor_runner=successful_probe,
            runner=cannot_start,
        )

    def no_output(command: tuple[str, ...], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 3, stdout="", stderr="")

    with pytest.raises(PopperRunError, match="no process output"):
        run_popper(
            problem,
            popper_dir=checkout,
            which=available_command,
            doctor_runner=successful_probe,
            runner=no_output,
        )

    def hypothesis(output: str) -> Any:
        def runner(command: tuple[str, ...], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

        return runner

    with pytest.raises(PopperRunError, match="exceeding max_body=1"):
        run_popper(
            problem,
            popper_dir=checkout,
            max_body=1,
            which=available_command,
            doctor_runner=successful_probe,
            runner=hypothesis("needs_extra_validation(A):- fact(A),uses_async(A).\n"),
        )
    with pytest.raises(PopperRunError, match="outside the exported bias"):
        run_popper(
            problem,
            popper_dir=checkout,
            which=available_command,
            doctor_runner=successful_probe,
            runner=hypothesis("needs_extra_validation(A):- touches_auth(A).\n"),
        )
    with pytest.raises(PopperRunError, match="allow_negation=false"):
        run_popper(
            problem,
            popper_dir=checkout,
            allow_negation=False,
            which=available_command,
            doctor_runner=successful_probe,
            runner=hypothesis("needs_extra_validation(A):- not_fact(A).\n"),
        )


def test_learn_popper_rejects_unsupported_multi_clause_configuration(tmp_path: Path) -> None:
    with pytest.raises(PopperConfigurationError, match="requires max_rules=1"):
        learn_popper(
            [observation("positive", {"fact"}, LabelValue.POSITIVE)],
            TARGET,
            tmp_path / "problem",
            max_rules=2,
        )


def test_learn_popper_exports_then_runs(tmp_path: Path) -> None:
    checkout = fake_checkout(tmp_path / "checkout")

    def runner(command: tuple[str, ...], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout="NO SOLUTION\n", stderr="")

    result = learn_popper(
        [
            observation("positive", {"uses_async"}, LabelValue.POSITIVE),
            observation("negative", set(), LabelValue.NEGATIVE),
        ],
        TARGET,
        tmp_path / "problem",
        popper_dir=checkout,
        which=available_command,
        doctor_runner=successful_probe,
        runner=runner,
    )

    assert result.problem.examples_path.is_file()
    assert result.rules.clauses == ()
    assert result.engine_version.endswith(
        "env-sha256:" + hashlib.sha256(PROBE_MANIFEST.encode()).hexdigest()
    )
