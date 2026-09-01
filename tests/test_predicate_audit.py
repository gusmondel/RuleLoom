from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from ruleloom.models import FactEvidence, LabelEvidence, LabelValue, ModelError, Observation
from ruleloom.predicate_audit import audit_predicates

PROTOCOL_HASH = "a" * 64
TARGET = "validation_rework_required"


def _observation(index: int, facts: set[str], path_counts: dict[str, int]) -> Observation:
    observed_at = datetime(2026, 1, index, tzinfo=UTC)
    return Observation(
        id=f"observation-{index}",
        observed_at=observed_at.isoformat(),
        protocol_hash=PROTOCOL_HASH,
        facts=frozenset(facts),
        labels={TARGET: LabelValue.UNKNOWN},
        fact_evidence={
            fact: FactEvidence(
                kind="deterministic",
                extractor="ruleloom.configured_paths.git.v1",
                evidence=(f"path:component/{fact}/{index}.txt",),
            )
            for fact in facts
        },
        source={
            "kind": "historical_change",
            "repository": "repository.example",
            "pack": "configured_paths",
            "pack_version": 1,
            "extractor": "ruleloom.configured_paths.git.v1",
        },
        metadata={
            "topological_index": index,
            "configured_path_match_counts": path_counts,
        },
    )


def _row(report: dict[str, object], predicate: str) -> dict[str, object]:
    rows = report["predicates"]
    assert isinstance(rows, list)
    return next(row for row in rows if isinstance(row, dict) and row["predicate"] == predicate)


def test_audit_reports_coverage_examples_relations_and_temporal_drift() -> None:
    observations = [
        _observation(
            1,
            {"touches_alpha", "touches_alpha_copy", "touches_all"},
            {"touches_alpha": 2},
        ),
        _observation(
            2,
            {"touches_alpha", "touches_alpha_copy", "touches_all"},
            {"touches_alpha": 1},
        ),
        _observation(3, {"touches_beta", "touches_all"}, {"touches_beta": 3}),
        _observation(4, {"touches_beta", "touches_all"}, {"touches_beta": 1}),
    ]

    report = audit_predicates(
        observations,
        (
            "touches_absent",
            "touches_all",
            "touches_alpha",
            "touches_alpha_copy",
            "touches_beta",
        ),
        configured_predicates=("touches_alpha", "touches_beta"),
    )

    assert report["outcome_blind"] is True
    assert isinstance(report["observation_manifest_hash"], str)
    assert len(report["observation_manifest_hash"]) == 64
    assert report["ordering"] == "first_parent_topology"
    assert report["configured_coverage"] == {
        "covered_observations": 4,
        "uncovered_observations": 0,
        "coverage": 1.0,
    }
    alpha = _row(report, "touches_alpha")
    assert alpha["observation_count"] == 2
    assert alpha["prevalence"] == 0.5
    assert alpha["early_prevalence"] == 1.0
    assert alpha["late_prevalence"] == 0.0
    assert alpha["flags"] == ["prevalence_drift"]
    assert alpha["matched_path_count"] == 3
    assert alpha["path_examples"] == [
        "component/touches_alpha/1.txt",
        "component/touches_alpha/2.txt",
    ]
    assert _row(report, "touches_absent")["flags"] == ["never_true"]
    assert _row(report, "touches_all")["flags"] == ["always_true"]
    relations = report["relations"]
    assert isinstance(relations, list)
    assert any(
        relation["left"] == "touches_alpha"
        and relation["right"] == "touches_alpha_copy"
        and relation["equivalent"] is True
        for relation in relations
        if isinstance(relation, dict)
    )
    assert any(
        relation["left"] == "touches_alpha"
        and relation["right"] == "touches_beta"
        and relation["complementary"] is True
        for relation in relations
        if isinstance(relation, dict)
    )


def test_audit_is_invariant_to_labels_and_label_evidence() -> None:
    observations = [
        _observation(1, {"touches_alpha"}, {"touches_alpha": 1}),
        _observation(2, set(), {"touches_alpha": 0}),
    ]
    labelled = [
        replace(
            item,
            labels={
                TARGET: LabelValue.POSITIVE if index == 0 else LabelValue.NEGATIVE,
            },
            label_evidence={
                TARGET: LabelEvidence(
                    kind="synthetic",
                    available_at=(
                        datetime.fromisoformat(item.observed_at) + timedelta(days=1)
                    ).isoformat(),
                    source="test-only",
                )
            },
        )
        for index, item in enumerate(observations)
    ]

    kwargs = {
        "configured_predicates": ("touches_alpha",),
        "rare_threshold": 0.1,
        "saturated_threshold": 0.9,
    }
    assert audit_predicates(observations, ("touches_alpha",), **kwargs) == audit_predicates(
        labelled,
        ("touches_alpha",),
        **kwargs,
    )


def test_audit_suppresses_relations_supported_by_only_one_observation() -> None:
    observations = [
        _observation(
            1,
            {"singleton", "singleton_alias"},
            {"singleton": 1, "singleton_alias": 1},
        ),
        _observation(
            2,
            {"stable", "stable_alias"},
            {"stable": 1, "stable_alias": 1},
        ),
        _observation(
            3,
            {"stable", "stable_alias"},
            {"stable": 1, "stable_alias": 1},
        ),
    ]

    report = audit_predicates(
        observations,
        ("singleton", "singleton_alias", "stable", "stable_alias"),
    )

    thresholds = report["thresholds"]
    assert isinstance(thresholds, dict)
    assert thresholds["relation_min_support"] == 2
    relations = report["relations"]
    assert isinstance(relations, list)
    assert not any(
        isinstance(relation, dict)
        and {relation["left"], relation["right"]} == {"singleton", "singleton_alias"}
        for relation in relations
    )
    assert any(
        isinstance(relation, dict)
        and relation["left"] == "stable"
        and relation["right"] == "stable_alias"
        and relation["equivalent"] is True
        for relation in relations
    )


def test_audit_keeps_rare_equivalence_but_suppresses_rare_implication() -> None:
    observations = [
        _observation(
            1,
            {"broad", "rare", "rare_alias"},
            {"broad": 1, "rare": 1, "rare_alias": 1},
        ),
        _observation(
            2,
            {"broad", "rare", "rare_alias"},
            {"broad": 1, "rare": 1, "rare_alias": 1},
        ),
        _observation(3, {"broad"}, {"broad": 1}),
        _observation(4, {"broad"}, {"broad": 1}),
        _observation(5, {"broad"}, {"broad": 1}),
        _observation(6, set(), {}),
    ]

    report = audit_predicates(
        observations,
        ("broad", "rare", "rare_alias"),
        rare_threshold=0.5,
    )

    thresholds = report["thresholds"]
    assert isinstance(thresholds, dict)
    assert thresholds["implication_min_antecedent_support"] == 3
    relations = report["relations"]
    assert isinstance(relations, list)
    rare_equivalence = next(
        relation
        for relation in relations
        if isinstance(relation, dict)
        and relation["left"] == "rare"
        and relation["right"] == "rare_alias"
    )
    assert rare_equivalence["equivalent"] is True
    assert rare_equivalence["left_implies_right"] is False
    assert rare_equivalence["right_implies_left"] is False
    assert not any(
        isinstance(relation, dict) and {relation["left"], relation["right"]} == {"broad", "rare"}
        for relation in relations
    )


def test_audit_rejects_invalid_thresholds_and_unknown_configured_predicates() -> None:
    with pytest.raises(ModelError, match="rare_threshold must not exceed"):
        audit_predicates([], (), rare_threshold=0.8, saturated_threshold=0.2)
    with pytest.raises(ModelError, match="absent from the audited vocabulary"):
        audit_predicates([], ("touches_alpha",), configured_predicates=("touches_beta",))
