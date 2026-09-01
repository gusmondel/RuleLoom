"""Optional adapter for Popper's noisy, MDL-based learner.

RuleLoom deliberately does not vendor, download, or install Popper.  This
module exports the three files expected by Popper, checks an explicitly
configured Popper checkout, invokes its current command-line entry point, and
translates the supported unary Horn fragment back into RuleLoom models.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ruleloom.learners.horn import rank_predicates
from ruleloom.models import (
    HornClause,
    LabelValue,
    ModelError,
    Observation,
    RuleLiteral,
    RuleSet,
    validate_predicate,
)

_POPPER_ENV = "POPPER_HOME"
_PROCESS_TIMEOUT_GRACE_SECONDS = 5
_RULE_RE = re.compile(
    r"^\s*(?P<head>[a-z][a-z0-9_]*)\s*\(\s*"
    r"(?P<head_var>[A-Z_][A-Za-z0-9_]*)\s*\)\s*:-\s*"
    r"(?P<body>.+?)\s*\.\s*$"
)
_LITERAL_RE = re.compile(
    r"^\s*(?P<predicate>[a-z][a-z0-9_]*)\s*\(\s*"
    r"(?P<variable>[A-Z_][A-Za-z0-9_]*)\s*\)\s*$"
)
_SOLUTION_MARKER_RE = re.compile(r"\*+\s+SOLUTION\s+\*+", re.IGNORECASE)
_TIMEOUT_RE = re.compile(r"TIMEOUT OF .* SECONDS EXCEEDED", re.IGNORECASE)
_ELAPSED_PREFIX_RE = re.compile(r"^\s*\d+(?:\.\d+)?s\s+")
POPPER_ADAPTER_VERSION = "ruleloom-popper-adapter/0.1"


class PopperError(RuntimeError):
    """Base class for actionable Popper adapter failures."""


class PopperConfigurationError(PopperError):
    """Raised when no usable Popper checkout was configured."""


class PopperDependencyError(PopperError):
    """Raised when Popper's external runtime requirements are unavailable."""


class PopperExportError(PopperError):
    """Raised when observations cannot be represented safely for Popper."""


class PopperParseError(PopperError):
    """Raised when Popper emits a hypothesis outside RuleLoom's supported fragment."""


class PopperRunError(PopperError):
    """Raised when the external Popper process fails or times out."""


@dataclass(frozen=True, slots=True)
class PopperProblem:
    """A materialized Popper problem and the examples used to build it."""

    directory: Path
    target: str
    predicates: tuple[str, ...]
    positive_ids: tuple[str, ...]
    negative_ids: tuple[str, ...]

    @property
    def examples_path(self) -> Path:
        return self.directory / "exs.pl"

    @property
    def background_path(self) -> Path:
        return self.directory / "bk.pl"

    @property
    def bias_path(self) -> Path:
        return self.directory / "bias.pl"


@dataclass(frozen=True, slots=True)
class PopperRequirement:
    """One read-only Popper environment check."""

    name: str
    available: bool
    path: Path | None
    detail: str


@dataclass(frozen=True, slots=True)
class PopperDoctorReport:
    """Result of checking Popper without changing the host machine."""

    requirements: tuple[PopperRequirement, ...]
    runtime_fingerprint: str | None = None

    @property
    def ready(self) -> bool:
        return all(item.available for item in self.requirements)

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.requirements if not item.available)

    def require_path(self, name: str) -> Path:
        for item in self.requirements:
            if item.name == name and item.path is not None:
                return item.path
        raise PopperDependencyError("Popper is not ready; missing " + ", ".join(self.missing))

    def require_ready(self) -> None:
        if not self.ready:
            raise PopperDependencyError(
                "Popper is not ready; missing "
                + ", ".join(self.missing)
                + ". Configure a checkout with learner.popper_dir or POPPER_HOME; "
                "RuleLoom will not install dependencies automatically."
            )


@dataclass(frozen=True, slots=True)
class PopperRun:
    """A completed Popper invocation and its parsed hypothesis."""

    problem: PopperProblem
    rules: RuleSet
    command: tuple[str, ...]
    stdout: str
    stderr: str
    engine_version: str


def _effective_environment(environ: Mapping[str, str] | None) -> dict[str, str]:
    result = dict(os.environ)
    if environ is not None:
        result.update(environ)
    return result


def _positive_integer(value: int, name: str) -> int:
    if isinstance(value, bool) or value < 1:
        raise PopperExportError(f"{name} must be an integer >= 1")
    return value


def _prolog_atom(value: str) -> str:
    """Quote a model-validated observation id as a portable Prolog atom."""
    return "'" + value.replace("'", "''") + "'"


def export_popper_problem(
    observations: Sequence[Observation],
    target: str,
    directory: Path,
    *,
    max_body: int = 3,
    max_rules: int = 3,
    max_predicates: int = 24,
    allow_negation: bool = True,
) -> PopperProblem:
    """Export labelled observations as ``exs.pl``, ``bk.pl``, and ``bias.pl``.

    Every observed predicate is unary over the observation id.  Closed-world
    negation is made explicit: when ``p`` is absent, ``not_p`` is present.  The
    ``not_`` namespace is therefore reserved for this representation. Constant
    predicates and duplicate truth columns are excluded from the bounded
    hypothesis space using only these labelled training observations.
    Unknown-labelled observations are excluded from the learning problem.
    """
    try:
        validate_predicate(target, field_name="target")
    except ModelError as exc:
        raise PopperExportError(str(exc)) from exc
    _positive_integer(max_body, "max_body")
    _positive_integer(max_rules, "max_rules")
    _positive_integer(max_predicates, "max_predicates")
    if target.startswith("not_"):
        raise PopperExportError("the 'not_' target prefix is reserved for closed-world negation")

    labelled = tuple(
        item
        for item in observations
        if item.labels.get(target, LabelValue.UNKNOWN) is not LabelValue.UNKNOWN
    )
    if not labelled:
        raise PopperExportError(f"no positive or negative examples are labelled for {target!r}")
    seen_ids: set[str] = set()
    duplicate_ids_set: set[str] = set()
    for item in labelled:
        if item.id in seen_ids:
            duplicate_ids_set.add(item.id)
        seen_ids.add(item.id)
    duplicate_ids = tuple(sorted(duplicate_ids_set))
    if duplicate_ids:
        raise PopperExportError(
            "labelled observations must have unique ids: " + ", ".join(duplicate_ids)
        )

    all_predicates = tuple(sorted({fact for item in labelled for fact in item.facts}))
    if not all_predicates:
        raise PopperExportError("labelled observations contain no facts to learn from")
    if target in all_predicates:
        raise PopperExportError(
            f"fact {target!r} duplicates the target and would leak the training label"
        )
    reserved = tuple(predicate for predicate in all_predicates if predicate.startswith("not_"))
    if reserved:
        raise PopperExportError(
            "fact predicates starting with 'not_' are reserved for closed-world negation: "
            + ", ".join(reserved)
        )
    positive_count = sum(item.labels[target] is LabelValue.POSITIVE for item in labelled)
    negative_count = sum(item.labels[target] is LabelValue.NEGATIVE for item in labelled)
    if not positive_count or not negative_count:
        raise PopperExportError(
            "predicate ranking requires at least one positive and one negative example"
        )
    predicates = tuple(
        rank_predicates(
            labelled,
            target,
            allow_negation=allow_negation,
        )[:max_predicates]
    )
    if not predicates:
        raise PopperExportError("labelled observations contain no non-constant facts to learn from")

    positive_ids = tuple(item.id for item in labelled if item.labels[target] is LabelValue.POSITIVE)
    negative_ids = tuple(item.id for item in labelled if item.labels[target] is LabelValue.NEGATIVE)
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)

    example_lines = [
        "% Generated by RuleLoom; edit observations, not this file.",
        *(f"pos({target}({_prolog_atom(item_id)}))." for item_id in positive_ids),
        *(f"neg({target}({_prolog_atom(item_id)}))." for item_id in negative_ids),
    ]
    background_lines = ["% Explicit unary facts with closed-world complements."]
    for item in labelled:
        subject = _prolog_atom(item.id)
        for predicate in predicates:
            exported_predicate = predicate if predicate in item.facts else f"not_{predicate}"
            background_lines.append(f"{exported_predicate}({subject}).")

    body_predicates = list(predicates)
    if allow_negation:
        body_predicates.extend(f"not_{predicate}" for predicate in predicates)
    bias_lines = [
        "% Bounded, typed, non-recursive unary hypothesis space.",
        "% RuleLoom validates the final clause count because current non-recursive",
        "% Popper versions do not use max_clauses as a generation bound.",
        f"max_body({max_body}).",
        "max_vars(1).",
        f"max_clauses({max_rules}).",
        "",
        f"head_pred({target},1).",
        *(f"body_pred({predicate},1)." for predicate in body_predicates),
        "",
        f"type({target},(observation,)).",
        *(f"type({predicate},(observation,))." for predicate in body_predicates),
        "",
        f"direction({target},(in,)).",
        *(f"direction({predicate},(in,))." for predicate in body_predicates),
        "",
        ":- clause(C), #count{P,A,Vars : body_literal(C,P,A,Vars)} == 0.",
    ]

    (directory / "exs.pl").write_text("\n".join(example_lines) + "\n", encoding="utf-8")
    (directory / "bk.pl").write_text("\n".join(background_lines) + "\n", encoding="utf-8")
    (directory / "bias.pl").write_text("\n".join(bias_lines) + "\n", encoding="utf-8")
    return PopperProblem(
        directory=directory,
        target=target,
        predicates=predicates,
        positive_ids=positive_ids,
        negative_ids=negative_ids,
    )


def locate_popper(
    popper_dir: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve ``popper.py`` from an explicit directory or ``POPPER_HOME``."""
    effective_env = _effective_environment(environ)
    configured: str | Path | None = popper_dir
    if configured is None:
        configured = effective_env.get(_POPPER_ENV)
    if configured is None or not str(configured).strip():
        raise PopperConfigurationError(
            "Popper checkout is not configured; set learner.popper_dir or POPPER_HOME"
        )

    configured_path = Path(configured).expanduser().resolve()
    script = (
        configured_path if configured_path.name == "popper.py" else configured_path / "popper.py"
    )
    if not script.is_file():
        raise PopperConfigurationError(f"Popper entry point not found: {script}")
    return script


def fingerprint_popper(
    popper_dir: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Fingerprint the configured checkout, including local source modifications."""
    script = locate_popper(popper_dir, environ=environ)
    checkout = script.parent
    digest = hashlib.sha256()
    included = 0
    for path in sorted(checkout.rglob("*")):
        relative = path.relative_to(checkout)
        if any(part in {".git", ".venv", "__pycache__"} for part in relative.parts):
            continue
        if not path.is_file() or path.suffix not in {".py", ".pl", ".lp", ".toml", ".lock"}:
            continue
        digest.update(relative.as_posix().encode())
        digest.update(b"\x00")
        digest.update(path.read_bytes())
        included += 1
    if not included:
        raise PopperConfigurationError(f"Popper checkout contains no source files: {checkout}")

    revision = "no-git-revision"
    try:
        completed = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        pass
    else:
        if completed.returncode == 0 and completed.stdout.strip():
            revision = completed.stdout.strip()
    return f"popper/git:{revision}/tree-sha256:{digest.hexdigest()}"


def _popper_python_candidates(script: Path, environment: Mapping[str, str]) -> list[Path]:
    candidates = [
        script.parent / ".venv" / "bin" / "python",
        script.parent / ".venv" / "Scripts" / "python.exe",
    ]
    virtual_env = environment.get("VIRTUAL_ENV")
    if virtual_env:
        candidates.extend(
            [Path(virtual_env) / "bin" / "python", Path(virtual_env) / "Scripts" / "python.exe"]
        )
    result: list[Path] = []
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            resolved = candidate.resolve()
            if resolved not in result:
                result.append(resolved)
    return result


def doctor_popper(
    popper_dir: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
    probe_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    probe_runtime: bool = False,
) -> PopperDoctorReport:
    """Check Popper without executing its Python runtime unless explicitly requested."""
    effective_env = _effective_environment(environ)

    def find_command(name: str) -> str | None:
        if which is not None:
            return which(name)
        return shutil.which(name, path=effective_env.get("PATH"))

    requirements: list[PopperRequirement] = []
    for name in ("swipl", "timeout"):
        raw_path = find_command(name)
        path = Path(raw_path).resolve() if raw_path else None
        requirements.append(
            PopperRequirement(
                name=name,
                available=path is not None,
                path=path,
                detail=f"found at {path}" if path else f"{name} is not on PATH",
            )
        )

    runtime_fingerprint: str | None = None
    try:
        script = locate_popper(popper_dir, environ=effective_env)
    except PopperConfigurationError as exc:
        requirements.append(
            PopperRequirement(name="popper.py", available=False, path=None, detail=str(exc))
        )
    else:
        requirements.append(
            PopperRequirement(
                name="popper.py",
                available=True,
                path=script,
                detail=f"found at {script}",
            )
        )
        if not probe_runtime:
            detail = "runtime not executed; explicitly request a trusted Popper probe"
            requirements.extend(
                (
                    PopperRequirement(
                        name="popper-python", available=False, path=None, detail=detail
                    ),
                    PopperRequirement(
                        name="popper-smoke", available=False, path=None, detail=detail
                    ),
                )
            )
            return PopperDoctorReport(requirements=tuple(requirements))
        candidates = _popper_python_candidates(script, effective_env)
        invoke = probe_runner or subprocess.run
        selected: Path | None = None
        environment_manifest = ""
        smoke_detail = "no provisioned environment passed the compatibility smoke check"
        for python in candidates:
            try:
                completed = invoke(
                    (
                        str(python),
                        "-c",
                        "import importlib,importlib.metadata as m,json,sys; "
                        "assert sys.version_info >= (3,14); "
                        "importlib.import_module('clingo'); "
                        "importlib.import_module('popper.loop'); "
                        "print(json.dumps({'python':list(sys.version_info[:3]),"
                        "'packages':sorted((d.metadata.get('Name',''),d.version) "
                        "for d in m.distributions())},sort_keys=True,separators=(',',':')))",
                    ),
                    cwd=script.parent,
                    env=effective_env,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                smoke_detail = f"provisioned environment smoke check failed: {exc}"
                continue
            if completed.returncode == 0 and completed.stdout.strip():
                selected = python
                environment_manifest = completed.stdout.strip()
                break
            detail = (completed.stderr or completed.stdout or "").strip()
            smoke_detail = "provisioned environment is incompatible: " + detail[-500:]
        requirements.append(
            PopperRequirement(
                name="popper-python",
                available=selected is not None,
                path=selected,
                detail=(
                    f"found compatible provisioned environment at {selected}"
                    if selected
                    else "no compatible provisioned Popper .venv (or VIRTUAL_ENV) was found"
                ),
            )
        )
        requirements.append(
            PopperRequirement(
                name="popper-smoke",
                available=selected is not None,
                path=selected,
                detail=(
                    "Python >=3.14 and Popper/clingo imports succeeded"
                    if selected
                    else smoke_detail
                ),
            )
        )
        if selected is not None:
            environment_hash = hashlib.sha256(environment_manifest.encode()).hexdigest()
            runtime_fingerprint = (
                f"{fingerprint_popper(script, environ=effective_env)}/env-sha256:{environment_hash}"
            )
    return PopperDoctorReport(
        requirements=tuple(requirements), runtime_fingerprint=runtime_fingerprint
    )


def _parse_clause(line: str, target: str) -> HornClause | None:
    match = _RULE_RE.fullmatch(line)
    if match is None:
        return None
    head = match.group("head")
    if head != target:
        raise PopperParseError(
            f"unsupported learned predicate {head!r}; expected only target {target!r}"
        )
    head_variable = match.group("head_var")
    literal_texts = match.group("body").split(",")
    literals: list[RuleLiteral] = []
    for literal_text in literal_texts:
        literal_match = _LITERAL_RE.fullmatch(literal_text)
        if literal_match is None:
            raise PopperParseError(f"unsupported body literal in Popper output: {literal_text!r}")
        if literal_match.group("variable") != head_variable:
            raise PopperParseError(
                "RuleLoom supports only unary clauses whose literals share the head variable"
            )
        exported_predicate = literal_match.group("predicate")
        negated = exported_predicate.startswith("not_")
        predicate = exported_predicate.removeprefix("not_") if negated else exported_predicate
        if not predicate:
            raise PopperParseError("closed-world literal has an empty predicate")
        if predicate == target:
            raise PopperParseError("recursive Popper hypotheses are not supported")
        try:
            literals.append(RuleLiteral(predicate=predicate, negated=negated))
        except ModelError as exc:
            raise PopperParseError(str(exc)) from exc

    try:
        return HornClause(
            target=target,
            body=tuple(sorted(literals, key=lambda item: (item.predicate, item.negated))),
        )
    except ModelError as exc:
        raise PopperParseError(str(exc)) from exc


def parse_popper_rules(output: str, target: str) -> RuleSet:
    """Parse Popper's printed solution into the unary RuleLoom Horn fragment."""
    try:
        validate_predicate(target, field_name="target")
    except ModelError as exc:
        raise PopperParseError(str(exc)) from exc

    if _TIMEOUT_RE.search(output):
        raise PopperParseError("Popper timed out before proving a final solution")

    raw_lines = [_ELAPSED_PREFIX_RE.sub("", line, count=1) for line in output.splitlines()]
    solution_markers = [
        index for index, line in enumerate(raw_lines) if _SOLUTION_MARKER_RE.search(line)
    ]
    if solution_markers:
        start = solution_markers[-1] + 1
        selected_lines: list[str] = []
        for line in raw_lines[start:]:
            stripped = line.strip()
            if len(stripped) >= 3 and set(stripped) == {"*"}:
                break
            selected_lines.append(line)
    elif any("New best hypothesis:" in line for line in raw_lines):
        raise PopperParseError(
            "Popper output contained only intermediate hypotheses and no final SOLUTION block"
        )
    else:
        selected_lines = raw_lines

    clauses: list[HornClause] = []
    signatures: set[str] = set()
    suspicious_target_line: str | None = None
    target_start = re.compile(rf"^\s*{re.escape(target)}\s*\(")
    for raw_line in selected_lines:
        line = raw_line.split("%", maxsplit=1)[0].strip()
        if not line:
            continue
        clause = _parse_clause(line, target)
        if clause is not None:
            if clause.signature not in signatures:
                clauses.append(clause)
                signatures.add(clause.signature)
            continue
        if target_start.match(line):
            suspicious_target_line = line

    if suspicious_target_line is not None:
        raise PopperParseError(
            "unsupported or malformed target clause in Popper output: " + suspicious_target_line
        )
    if clauses:
        return RuleSet(target=target, clauses=tuple(clauses))
    if "NO SOLUTION" in output.upper():
        return RuleSet(target=target, clauses=())
    raise PopperParseError("Popper output contained neither a supported rule nor NO SOLUTION")


def _process_error_tail(stdout: str, stderr: str) -> str:
    combined = "\n".join(part.strip() for part in (stdout, stderr) if part.strip())
    if not combined:
        return "no process output"
    lines = combined.splitlines()
    return "\n".join(lines[-12:])


def run_popper(
    problem: PopperProblem,
    *,
    popper_dir: str | Path | None = None,
    timeout_seconds: int = 120,
    max_body: int = 3,
    max_rules: int = 1,
    allow_negation: bool = True,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
    doctor_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> PopperRun:
    """Run Popper in noisy/MaxSynth mode with internal and process timeouts."""
    _positive_integer(timeout_seconds, "timeout_seconds")
    _positive_integer(max_body, "max_body")
    _positive_integer(max_rules, "max_rules")
    if max_rules != 1:
        raise PopperConfigurationError(
            "the supported non-recursive Popper adapter requires max_rules=1; "
            "use the built-in Horn learner for multi-clause hypotheses"
        )
    report = doctor_popper(
        popper_dir,
        environ=environ,
        which=which,
        probe_runner=doctor_runner,
        probe_runtime=True,
    )
    report.require_ready()
    if report.runtime_fingerprint is None:
        raise PopperDependencyError("Popper runtime fingerprint could not be established")
    python = report.require_path("popper-python")
    script = report.require_path("popper.py")
    command = (
        str(python),
        script.name,
        str(problem.directory),
        "--noisy",
        "--timeout",
        str(timeout_seconds),
        "--max-body",
        str(max_body),
        "--max-vars",
        "1",
    )
    invoke = runner or subprocess.run
    try:
        completed = invoke(
            command,
            cwd=script.parent,
            env=_effective_environment(environ),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds + _PROCESS_TIMEOUT_GRACE_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise PopperRunError(
            f"Popper exceeded the {timeout_seconds}-second learning timeout"
        ) from exc
    except OSError as exc:
        raise PopperRunError(f"could not start Popper: {exc}") from exc

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    if completed.returncode != 0:
        raise PopperRunError(
            f"Popper exited with status {completed.returncode}:\n"
            + _process_error_tail(stdout, stderr)
        )
    combined_output = "\n".join((stdout, stderr))
    if _TIMEOUT_RE.search(combined_output):
        raise PopperRunError(
            f"Popper reached its {timeout_seconds}-second internal timeout; "
            "the printed hypothesis is incomplete and was discarded"
        )
    rules = parse_popper_rules(combined_output, problem.target)
    if len(rules.clauses) > max_rules:
        raise PopperRunError(
            f"Popper returned {len(rules.clauses)} clauses, exceeding max_rules={max_rules}; "
            "the hypothesis was discarded"
        )
    allowed_predicates = set(problem.predicates)
    for clause in rules.clauses:
        if len(clause.body) > max_body:
            raise PopperRunError(
                f"Popper returned a clause with {len(clause.body)} literals, "
                f"exceeding max_body={max_body}; the hypothesis was discarded"
            )
        for literal in clause.body:
            if literal.predicate not in allowed_predicates:
                raise PopperRunError(
                    f"Popper returned predicate {literal.predicate!r} outside the exported bias; "
                    "the hypothesis was discarded"
                )
            if literal.negated and not allow_negation:
                raise PopperRunError(
                    "Popper returned a closed-world negated literal while allow_negation=false; "
                    "the hypothesis was discarded"
                )
    return PopperRun(
        problem=problem,
        rules=rules,
        command=command,
        stdout=stdout,
        stderr=stderr,
        engine_version=f"{POPPER_ADAPTER_VERSION};runtime={report.runtime_fingerprint}",
    )


def learn_popper(
    observations: Sequence[Observation],
    target: str,
    problem_dir: Path,
    *,
    popper_dir: str | Path | None = None,
    max_body: int = 3,
    max_rules: int = 1,
    max_predicates: int = 24,
    allow_negation: bool = True,
    timeout_seconds: int = 120,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
    doctor_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> PopperRun:
    """Export observations and run the optional Popper backend."""
    if max_rules != 1:
        raise PopperConfigurationError(
            "the supported non-recursive Popper adapter requires max_rules=1; "
            "use the built-in Horn learner for multi-clause hypotheses"
        )
    problem = export_popper_problem(
        observations,
        target,
        problem_dir,
        max_body=max_body,
        max_rules=max_rules,
        max_predicates=max_predicates,
        allow_negation=allow_negation,
    )
    return run_popper(
        problem,
        popper_dir=popper_dir,
        timeout_seconds=timeout_seconds,
        max_body=max_body,
        max_rules=max_rules,
        allow_negation=allow_negation,
        environ=environ,
        which=which,
        doctor_runner=doctor_runner,
        runner=runner,
    )


__all__ = [
    "POPPER_ADAPTER_VERSION",
    "PopperConfigurationError",
    "PopperDependencyError",
    "PopperDoctorReport",
    "PopperError",
    "PopperExportError",
    "PopperParseError",
    "PopperProblem",
    "PopperRequirement",
    "PopperRun",
    "PopperRunError",
    "doctor_popper",
    "export_popper_problem",
    "fingerprint_popper",
    "learn_popper",
    "locate_popper",
    "parse_popper_rules",
    "run_popper",
]
