"""Human-readable onboarding guidance derived from existing RuleLoom reports.

This module is deliberately read-only.  It does not collect evidence, reinterpret
labels, relax gates, or mutate an experiment.  The CLI can use it to turn the
existing readiness, history-status, and outcome-blind predicate-audit payloads
into concise next actions without duplicating their scientific logic.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from ruleloom.lifecycle import Readiness
from ruleloom.models import JsonObject, JsonValue, ModelError


@dataclass(frozen=True, slots=True)
class OnboardingAction:
    """One ordered, non-executing action suggested by the onboarding diagnosis."""

    code: str
    priority: int
    title: str
    detail: str
    command: str | None = None

    def __post_init__(self) -> None:
        if not self.code or not self.title or not self.detail:
            raise ModelError("onboarding actions require a code, title, and detail")
        if isinstance(self.priority, bool) or not 1 <= self.priority <= 3:
            raise ModelError("onboarding action priority must be between 1 and 3")

    def to_dict(self) -> JsonObject:
        return {
            "code": self.code,
            "priority": self.priority,
            "title": self.title,
            "detail": self.detail,
            "command": self.command,
        }


@dataclass(frozen=True, slots=True)
class OnboardingDiagnosis:
    """A stable machine payload plus a compact plain-text rendering."""

    stage: str
    headline: str
    evidence: tuple[str, ...]
    actions: tuple[OnboardingAction, ...]
    gate_gaps: Mapping[str, int]

    def to_dict(self) -> JsonObject:
        return {
            "stage": self.stage,
            "headline": self.headline,
            "evidence": cast(JsonValue, list(self.evidence)),
            "actions": cast(JsonValue, [action.to_dict() for action in self.actions]),
            "gate_gaps": cast(
                JsonValue,
                {key: self.gate_gaps[key] for key in sorted(self.gate_gaps)},
            ),
        }

    def render_text(self) -> str:
        """Render without ANSI escapes so output remains readable in logs and CI."""

        lines = [self.headline, "", "Evidence"]
        lines.extend(f"- {item}" for item in self.evidence)
        if self.actions:
            lines.extend(("", "Next actions"))
            for index, action in enumerate(self.actions, 1):
                lines.append(f"{index}. {action.title}")
                lines.append(f"   {action.detail}")
                if action.command is not None:
                    lines.append(f"   Command: {action.command}")
        return "\n".join(lines) + "\n"


@dataclass(frozen=True, slots=True)
class _AuditSummary:
    predicates: int = 0
    constants: int = 0
    rare: int = 0
    saturated: int = 0
    drifted: int = 0
    equivalent_relations: int = 0
    configured_coverage: float | None = None


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _ratio(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    result = float(value)
    return result if 0 <= result <= 1 else None


def _history_count(history: Mapping[str, object] | None, key: str) -> int | None:
    if history is None:
        return None
    return _nonnegative_int(history.get(key))


def _audit_summary(audit: Mapping[str, object] | None) -> _AuditSummary | None:
    if audit is None:
        return None
    predicate_rows = audit.get("predicates")
    flags = {
        name: 0 for name in ("never_true", "always_true", "rare", "saturated", "prevalence_drift")
    }
    predicate_count = 0
    if isinstance(predicate_rows, list):
        for row in predicate_rows:
            if not isinstance(row, dict):
                continue
            predicate_count += 1
            raw_flags = row.get("flags")
            if not isinstance(raw_flags, list):
                continue
            present = {item for item in raw_flags if isinstance(item, str)}
            for name in flags:
                flags[name] += name in present

    equivalent = 0
    relations = audit.get("relations")
    if isinstance(relations, list):
        for relation in relations:
            if not isinstance(relation, dict):
                continue
            equivalent += relation.get("equivalent") is True

    coverage: float | None = None
    configured = audit.get("configured_coverage")
    if isinstance(configured, dict):
        coverage = _ratio(configured.get("coverage"))
    return _AuditSummary(
        predicates=predicate_count,
        constants=flags["never_true"] + flags["always_true"],
        rare=flags["rare"],
        saturated=flags["saturated"],
        drifted=flags["prevalence_drift"],
        equivalent_relations=equivalent,
        configured_coverage=coverage,
    )


def _validate_floor(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ModelError(f"{name} must be an integer >= 1")
    return value


def _stage(readiness: Readiness) -> tuple[str, str]:
    if readiness.observations == 0:
        return "bootstrap", "No observations are available yet."
    if readiness.labeled == 0:
        return (
            "collect_outcomes",
            "Independent outcome evidence is the bottleneck for the available observations.",
        )
    if readiness.positive == 0 or readiness.negative == 0:
        return "balance_outcomes", "Mature outcomes currently contain only one class."
    if readiness.stage == "collection":
        return (
            "exploratory_learning",
            "Both outcome classes exist, but the evidence is still exploratory.",
        )
    if readiness.stage == "shadow":
        return "shadow", "The retrospective positive floor supports a shadow-only candidate."
    return (
        "preliminary_evaluation",
        "The retrospective positive floor is met; all later promotion gates still apply.",
    )


def diagnose_onboarding(
    readiness: Readiness,
    *,
    history_status: Mapping[str, object] | None = None,
    predicate_audit: Mapping[str, object] | None = None,
    min_positive_for_shadow: int = 20,
    min_positive_for_approval: int = 50,
) -> OnboardingDiagnosis:
    """Explain current evidence readiness without inspecting individual outcomes.

    ``history_status`` is the payload produced by ``ruleloom history status`` and
    ``predicate_audit`` is the outcome-blind payload produced by
    ``ruleloom predicates audit``.  Missing optional payloads become explicit
    collection actions rather than guessed evidence.
    """

    shadow_floor = _validate_floor(min_positive_for_shadow, "min_positive_for_shadow")
    approval_floor = _validate_floor(min_positive_for_approval, "min_positive_for_approval")
    if approval_floor < shadow_floor:
        raise ModelError("min_positive_for_approval must be >= min_positive_for_shadow")

    stage, headline = _stage(readiness)
    gate_gaps = {
        "positive_for_shadow": max(0, shadow_floor - readiness.positive),
        "positive_for_approval": max(0, approval_floor - readiness.positive),
        "positive_class": int(readiness.positive == 0),
        "negative_class": int(readiness.negative == 0),
    }
    evidence = [
        (
            f"{readiness.observations} observations; {readiness.labeled} mature outcomes "
            f"({readiness.positive} positive, {readiness.negative} negative); "
            f"{readiness.unknown} unknown or censored."
        ),
        (
            f"{readiness.distinct_predicates} observed predicates; deterministic fact "
            f"provenance coverage {readiness.fact_evidence_coverage:.1%}."
        ),
        (
            f"Positive-outcome floor gaps: {gate_gaps['positive_for_shadow']} for shadow and "
            f"{gate_gaps['positive_for_approval']} for approval. These counts alone never "
            "authorize promotion."
        ),
    ]
    evidence.extend(f"Readiness warning: {warning}" for warning in readiness.warnings)
    actions: list[OnboardingAction] = []

    units = _history_count(history_status, "change_units")
    events = _history_count(history_status, "events")
    confirmatory = _history_count(history_status, "confirmatory_units")
    if history_status is None:
        actions.append(
            OnboardingAction(
                code="inspect_history",
                priority=1,
                title="Inspect normalized history",
                detail=(
                    "History coverage was not supplied, so no evidence-grade conclusion "
                    "is possible."
                ),
                command="ruleloom history status",
            )
        )
    else:
        quality = history_status.get("evidence_quality")
        quality_counts = (
            {
                key: count
                for key, value in quality.items()
                if isinstance(key, str) and (count := _nonnegative_int(value)) is not None
            }
            if isinstance(quality, dict)
            else {}
        )
        rendered_quality = (
            "; evidence grades "
            + ", ".join(f"{key}={quality_counts[key]}" for key in sorted(quality_counts))
            if quality_counts
            else ""
        )
        evidence.append(
            f"Normalized history contains {events if events is not None else 'unknown'} events "
            f"and {units if units is not None else 'unknown'} change units; "
            f"{confirmatory if confirmatory is not None else 'unknown'} units are "
            f"confirmatory{rendered_quality}."
        )
        if None in {units, events, confirmatory}:
            actions.append(
                OnboardingAction(
                    code="inspect_history",
                    priority=1,
                    title="Inspect normalized history",
                    detail="The supplied history summary is incomplete or malformed.",
                    command="ruleloom history status",
                )
            )
        elif units == 0:
            actions.append(
                OnboardingAction(
                    code="bootstrap_git_history",
                    priority=1,
                    title="Bootstrap the repository history",
                    detail=(
                        "Collect Git topology now. It is useful exploratory evidence, but it "
                        "does not create independent outcomes."
                    ),
                    command="ruleloom history bootstrap-git --all",
                )
            )

    if readiness.observations == 0:
        actions.append(
            OnboardingAction(
                code="materialize_history",
                priority=1,
                title="Materialize prediction-time facts",
                detail=(
                    "After collecting or importing change units, materialize them before "
                    "auditing or learning."
                ),
                command="ruleloom history materialize",
            )
        )

    audit = _audit_summary(predicate_audit)
    if audit is None:
        actions.append(
            OnboardingAction(
                code="audit_predicates",
                priority=1 if readiness.observations else 2,
                title="Audit the frozen vocabulary",
                detail="Run the outcome-blind audit before importing target outcomes.",
                command="ruleloom predicates audit",
            )
        )
    else:
        audit_line = (
            f"Outcome-blind audit: {audit.predicates} predicates, {audit.constants} constant, "
            f"{audit.rare} rare, {audit.saturated} saturated, {audit.drifted} drifted, and "
            f"{audit.equivalent_relations} observed equivalent pairs."
        )
        if audit.configured_coverage is not None:
            audit_line += f" Configured-path coverage is {audit.configured_coverage:.1%}."
        evidence.append(audit_line)
        if audit.constants or audit.equivalent_relations:
            actions.append(
                OnboardingAction(
                    code="review_vocabulary_redundancy",
                    priority=1,
                    title="Review mechanically weak predicates",
                    detail=(
                        f"The audit found {audit.constants} constant predicates and "
                        f"{audit.equivalent_relations} empirically equivalent pairs. Review "
                        "them outcome-blind and start a new experiment for semantic changes."
                    ),
                )
            )
        if audit.rare or audit.saturated or audit.drifted:
            actions.append(
                OnboardingAction(
                    code="review_vocabulary_distribution",
                    priority=3,
                    title="Inspect sparse, saturated, or drifting predicates",
                    detail=(
                        f"Observed flags: {audit.rare} rare, {audit.saturated} saturated, and "
                        f"{audit.drifted} drifting. These are diagnostics, not proof that a "
                        "predicate is irrelevant."
                    ),
                )
            )

    only_exploratory_history = units is not None and units > 0 and confirmatory == 0
    needs_outcome_evidence = readiness.labeled == 0 or only_exploratory_history
    if needs_outcome_evidence:
        import_priority = 2 if readiness.observations == 0 or audit is None else 1
        actions.append(
            OnboardingAction(
                code="import_outcome_evidence",
                priority=import_priority,
                title="Import point-in-time outcome evidence",
                detail=(
                    "Capture authorized outcomes at event time through a webhook, exporter, "
                    "or immutable ledger, then import the normalized JSONL. The built-in "
                    "GitHub archive is exploratory and does not supply strong outcomes; never "
                    "infer negative outcomes from absence."
                ),
                command=("ruleloom history import --events /absolute/path/to/outcome-events.jsonl"),
            )
        )
        if units:
            actions.append(
                OnboardingAction(
                    code="rematerialize_outcomes",
                    priority=2,
                    title="Rematerialize delayed outcomes",
                    detail="Recompute labels after importing immutable normalized events.",
                    command="ruleloom history materialize",
                )
            )

    if readiness.labeled and (readiness.positive == 0 or readiness.negative == 0):
        missing = "positive" if readiness.positive == 0 else "negative"
        actions.append(
            OnboardingAction(
                code="collect_missing_class",
                priority=1,
                title=f"Collect mature {missing} outcomes",
                detail=(
                    "Both classes are required for discrimination. Preserve unknowns and do "
                    "not manufacture the missing class from event absence."
                ),
            )
        )
    elif readiness.positive and readiness.negative:
        actions.append(
            OnboardingAction(
                code="try_exploratory_learning",
                priority=2,
                title="Try chronological exploratory learning",
                detail=(
                    "Both classes exist. The learner will still enforce maturity, temporal "
                    "split, evidence-grade, baseline, and sample-size checks."
                ),
                command="ruleloom learn --json",
            )
        )

    ordered_actions = tuple(sorted(actions, key=lambda item: item.priority))
    return OnboardingDiagnosis(
        stage=stage,
        headline=headline,
        evidence=tuple(evidence),
        actions=ordered_actions,
        gate_gaps=gate_gaps,
    )


__all__ = ["OnboardingAction", "OnboardingDiagnosis", "diagnose_onboarding"]
