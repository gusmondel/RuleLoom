"""Outcome-blind diagnostics for a frozen predicate vocabulary."""

from __future__ import annotations

import math
from collections.abc import Sequence
from itertools import combinations
from typing import cast

from ruleloom.models import (
    JsonObject,
    JsonValue,
    ModelError,
    Observation,
    content_hash,
    parse_timestamp,
)

_PATH_PREFIX = "path:"
_MISSING_MARKER = ";missing:"
_MAX_PATH_EXAMPLES = 8
_MIN_RELATION_SUPPORT = 2
_WARMUP_WARNING_FRACTION = 0.2


def _threshold(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ModelError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise ModelError(f"{name} must be between 0 and 1")
    return result


def _ordered(observations: Sequence[Observation]) -> tuple[list[Observation], str, list[str]]:
    items = list(observations)
    warnings: list[str] = []
    topology: list[tuple[str, int]] = []
    for item in items:
        repository = item.source.get("repository")
        position = item.metadata.get("topological_index")
        if (
            isinstance(repository, str)
            and repository
            and isinstance(position, int)
            and not isinstance(position, bool)
            and position >= 1
        ):
            topology.append((repository, position))
    if (
        items
        and len(topology) == len(items)
        and len({repository for repository, _ in topology}) == 1
    ):
        items.sort(key=lambda item: (cast(int, item.metadata["topological_index"]), item.id))
        if len({position for _, position in topology}) != len(topology):
            warnings.append(
                "duplicate first-parent positions detected; equal positions use a stable "
                "id tie-break"
            )
        return items, "first_parent_topology", warnings
    items.sort(key=lambda item: (parse_timestamp(item.observed_at), item.id))
    if topology:
        warnings.append(
            "incomplete or mixed Git topology metadata; chronology falls back to observed_at"
        )
    return items, "observed_at", warnings


def _rate(count: int, total: int) -> float | None:
    return count / total if total else None


def _path_examples(observations: Sequence[Observation], predicate: str) -> list[str]:
    examples = {
        evidence[len(_PATH_PREFIX) :].split(";", 1)[0]
        for item in observations
        if (fact_evidence := item.fact_evidence.get(predicate)) is not None
        for evidence in fact_evidence.evidence
        if evidence.startswith(_PATH_PREFIX) and len(evidence) > len(_PATH_PREFIX)
    }
    return sorted(example for example in examples if example)[:_MAX_PATH_EXAMPLES]


def _partner_examples(observations: Sequence[Observation], predicate: str) -> list[JsonValue]:
    """Surface which usual partner was missing, aggregated across observations."""
    counts: dict[tuple[str, str], int] = {}
    for item in observations:
        fact_evidence = item.fact_evidence.get(predicate)
        if fact_evidence is None:
            continue
        for evidence in fact_evidence.evidence:
            if not evidence.startswith(_PATH_PREFIX) or _MISSING_MARKER not in evidence:
                continue
            path, remainder = evidence[len(_PATH_PREFIX) :].split(_MISSING_MARKER, 1)
            partner = remainder.split(";", 1)[0]
            if path and partner:
                counts[(path, partner)] = counts.get((path, partner), 0) + 1
    ordered = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    return [
        {"path": path, "missing_partner": partner, "observations": count}
        for (path, partner), count in ordered[:_MAX_PATH_EXAMPLES]
    ]


def _history_windows(ordered: Sequence[Observation]) -> tuple[JsonObject, list[str]]:
    """Compare the time-window feature horizons with the observed history span."""
    if not ordered:
        return {"status": "unavailable"}, []
    instants = [parse_timestamp(item.observed_at) for item in ordered]
    earliest = min(instants)
    span_days = (max(instants) - earliest).total_seconds() / 86_400
    windows: dict[str, int] = {}
    for item in ordered:
        context = item.metadata.get("historical_context")
        if not isinstance(context, dict):
            continue
        for key, predicate in (
            ("hotspot_window_days", "touches_recent_change_hotspot"),
            ("dormant_days", "touches_dormant_area"),
        ):
            value = context.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                windows.setdefault(predicate, value)
    if not windows:
        return {"status": "not_applicable", "observed_span_days": span_days}, []
    rows: dict[str, JsonValue] = {}
    warnings: list[str] = []
    for predicate, window_days in sorted(windows.items()):
        warmup = sum(
            (instant - earliest).total_seconds() / 86_400 < window_days for instant in instants
        )
        warmup_fraction = warmup / len(instants)
        exceeds = span_days < window_days
        rows[predicate] = {
            "window_days": window_days,
            "left_censored_warmup_observations": warmup,
            "left_censored_warmup_fraction": warmup_fraction,
            "window_exceeds_observed_history": exceeds,
        }
        if exceeds:
            warnings.append(
                f"{predicate}: its {window_days}-day window exceeds the observed history span "
                f"of {span_days:.1f} days, so it cannot fire and is structurally constant here"
            )
        elif warmup_fraction >= _WARMUP_WARNING_FRACTION:
            warnings.append(
                f"{predicate}: {warmup_fraction:.0%} of observations fall inside the first "
                f"{window_days} days of history where prior-window facts are left-censored; "
                "early/late prevalence drift can be an artifact of warm-up"
            )
    return {"status": "available", "observed_span_days": span_days, "predicates": rows}, warnings


def _matched_path_count(observations: Sequence[Observation], predicate: str) -> int | None:
    found = False
    total = 0
    for item in observations:
        raw_counts = item.metadata.get("configured_path_match_counts")
        if not isinstance(raw_counts, dict):
            continue
        raw_count = raw_counts.get(predicate)
        if isinstance(raw_count, int) and not isinstance(raw_count, bool) and raw_count >= 0:
            found = True
            total += raw_count
    return total if found else None


def _observation_manifest(observations: Sequence[Observation]) -> str:
    """Hash exactly the outcome-blind observation fields consumed by this audit."""
    records: list[JsonValue] = []
    for item in observations:
        records.append(
            {
                "schema_version": item.schema_version,
                "id": item.id,
                "observed_at": item.observed_at,
                "protocol_hash": item.protocol_hash,
                "facts": cast(JsonValue, sorted(item.facts)),
                "fact_evidence": {
                    predicate: item.fact_evidence[predicate].to_dict()
                    for predicate in sorted(item.fact_evidence)
                },
                "repository": item.source.get("repository"),
                "topological_index": item.metadata.get("topological_index"),
                "configured_path_match_counts": item.metadata.get("configured_path_match_counts"),
            }
        )
    return content_hash(records)


def audit_predicates(
    observations: Sequence[Observation],
    predicates: Sequence[str],
    *,
    configured_predicates: Sequence[str] = (),
    rare_threshold: float = 0.01,
    saturated_threshold: float = 0.99,
    drift_threshold: float = 0.20,
    overlap_threshold: float = 0.90,
) -> JsonObject:
    """Describe predicate coverage without reading outcomes or label evidence."""

    rare = _threshold(rare_threshold, "rare_threshold")
    saturated = _threshold(saturated_threshold, "saturated_threshold")
    drift = _threshold(drift_threshold, "drift_threshold")
    overlap = _threshold(overlap_threshold, "overlap_threshold")
    if rare > saturated:
        raise ModelError("rare_threshold must not exceed saturated_threshold")

    names = tuple(sorted(set(predicates)))
    configured = frozenset(configured_predicates)
    unknown_configured = sorted(configured.difference(names))
    if unknown_configured:
        raise ModelError(
            "configured predicates are absent from the audited vocabulary: "
            + ", ".join(unknown_configured)
        )

    ordered, ordering, warnings = _ordered(observations)
    total = len(ordered)
    implication_min_support = max(_MIN_RELATION_SUPPORT, math.ceil(rare * total))
    boundary = total // 2
    early = ordered[:boundary] if total >= 2 else []
    late = ordered[boundary:] if total >= 2 else []
    if total < 2:
        warnings.append("at least two observations are required for a drift comparison")

    truth_sets = {
        predicate: frozenset(index for index, item in enumerate(ordered) if predicate in item.facts)
        for predicate in names
    }
    predicate_rows: list[JsonValue] = []
    for predicate in names:
        matching = truth_sets[predicate]
        count = len(matching)
        prevalence = _rate(count, total)
        early_count = sum(predicate in item.facts for item in early)
        late_count = sum(predicate in item.facts for item in late)
        early_prevalence = _rate(early_count, len(early))
        late_prevalence = _rate(late_count, len(late))
        shift = (
            None
            if early_prevalence is None or late_prevalence is None
            else abs(late_prevalence - early_prevalence)
        )
        flags: list[str] = []
        if total and count == 0:
            flags.append("never_true")
        elif total and count == total:
            flags.append("always_true")
        elif prevalence is not None and prevalence <= rare:
            flags.append("rare")
        elif prevalence is not None and prevalence >= saturated:
            flags.append("saturated")
        if shift is not None and shift >= drift:
            flags.append("prevalence_drift")
        row: JsonObject = {
            "predicate": predicate,
            "configured": predicate in configured,
            "observation_count": count,
            "prevalence": prevalence,
            "early_count": early_count,
            "early_prevalence": early_prevalence,
            "late_count": late_count,
            "late_prevalence": late_prevalence,
            "absolute_prevalence_shift": shift,
            "flags": cast(JsonValue, flags),
        }
        if predicate in configured:
            row["matched_path_count"] = _matched_path_count(ordered, predicate)
        path_examples = _path_examples(ordered, predicate)
        if predicate in configured or path_examples:
            row["path_examples"] = cast(JsonValue, path_examples)
        partner_examples = _partner_examples(ordered, predicate)
        if partner_examples:
            row["partner_examples"] = partner_examples
        predicate_rows.append(row)

    relations: list[JsonValue] = []
    for left, right in combinations(names, 2):
        left_true = truth_sets[left]
        right_true = truth_sets[right]
        intersection = left_true & right_true
        union = left_true | right_true
        jaccard = len(intersection) / len(union) if union else None
        equivalent = len(union) >= _MIN_RELATION_SUPPORT and left_true == right_true
        complementary = total >= _MIN_RELATION_SUPPORT and not intersection and len(union) == total
        left_implies_right = (
            not equivalent and len(left_true) >= implication_min_support and left_true < right_true
        )
        right_implies_left = (
            not equivalent and len(right_true) >= implication_min_support and right_true < left_true
        )
        high_overlap = (
            len(union) >= _MIN_RELATION_SUPPORT and jaccard is not None and jaccard >= overlap
        )
        if not (
            equivalent or complementary or left_implies_right or right_implies_left or high_overlap
        ):
            continue
        relations.append(
            {
                "left": left,
                "right": right,
                "intersection_count": len(intersection),
                "union_count": len(union),
                "jaccard": jaccard,
                "equivalent": equivalent,
                "complementary": complementary,
                "left_implies_right": left_implies_right,
                "right_implies_left": right_implies_left,
            }
        )

    configured_covered = (
        frozenset().union(*(truth_sets[predicate] for predicate in configured))
        if configured
        else frozenset()
    )
    configured_uncovered = total - len(configured_covered)
    history_windows, window_warnings = _history_windows(ordered)
    warnings.extend(window_warnings)
    return {
        "schema_version": 1,
        "outcome_blind": True,
        "observation_count": total,
        "observation_manifest_hash": _observation_manifest(ordered),
        "ordering": ordering,
        "windows": {
            "early_observations": len(early),
            "late_observations": len(late),
        },
        "history_windows": history_windows,
        "thresholds": {
            "rare": rare,
            "saturated": saturated,
            "drift": drift,
            "overlap": overlap,
            "relation_min_support": _MIN_RELATION_SUPPORT,
            "implication_min_antecedent_support": implication_min_support,
        },
        "configured_coverage": {
            "covered_observations": len(configured_covered),
            "uncovered_observations": configured_uncovered,
            "coverage": _rate(len(configured_covered), total),
        },
        "predicates": predicate_rows,
        "relations": relations,
        "warnings": cast(JsonValue, warnings),
    }
