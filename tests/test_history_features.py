from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import ruleloom.history_features as history_features
from ruleloom import gitfacts
from ruleloom.history.models import HistoricalEvent
from ruleloom.history_features import (
    EXPERIENCE_PREDICATES,
    OFF_HOURS_PREDICATE,
    REWORK_HISTORY_PREDICATE,
    enrich_history_features,
)
from ruleloom.models import FactEvidence, LabelValue, Observation

EXTRACTOR = "ruleloom.generic_changes.git.v2"
TARGET = "post_merge_defect"


def _observation(
    index: int,
    day: int,
    paths: tuple[str, ...],
    *,
    base: str | None = None,
    author: str | None = None,
    observed_at: str | None = None,
) -> Observation:
    fact = "churn_band_tiny"
    return Observation(
        id=f"commit.{index}",
        observed_at=(
            observed_at
            if observed_at is not None
            else (datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=day)).isoformat()
        ),
        protocol_hash="a" * 64,
        facts=frozenset({fact}),
        labels={TARGET: LabelValue.UNKNOWN},
        fact_evidence={
            fact: FactEvidence(
                kind="deterministic",
                extractor=EXTRACTOR,
                evidence=("synthetic",),
            )
        },
        source={
            "kind": "git_commit",
            "repository": "repository.example",
            "pack": "generic_changes",
            "pack_version": 2,
            "extractor": EXTRACTOR,
            **({"base": base} if base is not None else {}),
        },
        metadata={
            "topological_index": index,
            "files_changed": len(paths),
            "changed_files": list(paths),
            "metadata_files_truncated": 0,
            **({"historical_author_hash": author} if author is not None else {}),
        },
    )


def _rework_event(landed_day: int, files: list[str]) -> HistoricalEvent:
    landed = (datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=landed_day)).isoformat()
    return HistoricalEvent(
        schema_version=1,
        id=f"event.git_rework.later{landed_day}.earlier",
        repository_id="repository.example",
        kind="rework",
        occurred_at=landed,
        available_at=landed,
        provider="git",
        source_ref="later",
        independent_group="change.git_commit.later",
        change_id="change.git_commit.earlier",
        data={"files": files, "reworked_lines": 4, "link_kind": "git_line_content"},
    )


A, B, C, D = "a" * 64, "b" * 64, "c" * 64, "d" * 64


def _v4_priors() -> list[Observation]:
    """Author A touches pkg/x every 5 days; B and C touch the same file once each."""
    # Topological order must agree with time order, otherwise the time-window
    # features abstain on purpose (see the non-monotonic timestamp test).
    priors = [_observation(index, index * 5, ("pkg/x/a.go",), author=A) for index in range(1, 21)]
    priors.append(_observation(21, 101, ("pkg/x/a.go",), author=B))
    priors.append(_observation(22, 102, ("pkg/x/a.go", "pkg/x/b.go"), author=C))
    return priors


def test_v4_experience_and_ownership_facts_use_only_earlier_changes() -> None:
    priors = _v4_priors()
    newcomer = _observation(40, 120, ("pkg/x/a.go", "web/y.ts"), author=D)
    veteran = _observation(41, 121, ("pkg/x/a.go",), author=A)

    enriched = enrich_history_features(
        priors, [newcomer, veteran], extractor=EXTRACTOR, pack_version=4, rework_events=[]
    )

    first, second = enriched
    assert {
        "author_low_experience",
        "author_new_to_area",
        "touched_files_many_authors",
    } <= first.facts
    assert "author:prior_365d_changes:0<3" in first.fact_evidence["author_low_experience"].evidence
    assert first.metadata["historical_context"]["experience_status"] == "available"
    assert first.metadata["historical_context"]["version"] == "ruleloom-history-features/3"
    assert "author_low_experience" not in second.facts
    assert "author_new_to_area" not in second.facts
    assert "touched_files_many_authors" in second.facts
    assert second.metadata["historical_context"]["rework_history_status"] == "available"
    assert "touches_recently_reworked_file" not in second.facts


def test_v4_experience_facts_abstain_during_warmup_and_without_an_author_hash() -> None:
    priors = _v4_priors()[:5]
    early = _observation(40, 30, ("pkg/x/a.go",), author=D)
    anonymous = _observation(41, 120, ("pkg/x/a.go",))

    early_result, anonymous_result = enrich_history_features(
        priors, [early, anonymous], extractor=EXTRACTOR, pack_version=4
    )

    assert not {"author_low_experience", "author_new_to_area"} & early_result.facts
    assert early_result.metadata["historical_context"]["experience_status"] == "abstained_warmup"
    assert not {"author_low_experience", "author_new_to_area"} & anonymous_result.facts
    assert (
        anonymous_result.metadata["historical_context"]["experience_status"]
        == "abstained_no_author_hash"
    )
    assert (
        anonymous_result.metadata["historical_context"]["rework_history_status"]
        == "abstained_no_rework_events"
    )


def test_v4_rework_history_fact_uses_reworks_landed_strictly_before_the_change() -> None:
    priors = _v4_priors()
    events = [_rework_event(110, ["pkg/x/a.go", "pkg/x/b.go"])]
    before = _observation(40, 105, ("pkg/x/a.go",), author=A)
    inside = _observation(41, 120, ("pkg/x/b.go",), author=A)
    expired = _observation(42, 250, ("pkg/x/a.go",), author=A)

    results = enrich_history_features(
        priors, [before, inside, expired], extractor=EXTRACTOR, pack_version=4, rework_events=events
    )

    assert "touches_recently_reworked_file" not in results[0].facts
    assert "touches_recently_reworked_file" in results[1].facts
    assert any(
        reason.startswith("path:pkg/x/b.go;rework_landed_at:")
        for reason in results[1].fact_evidence["touches_recently_reworked_file"].evidence
    )
    assert "touches_recently_reworked_file" not in results[2].facts


def test_v4_off_hours_is_judged_in_the_timestamp_local_offset() -> None:
    priors = _v4_priors()
    saturday_night = _observation(
        40, 0, ("pkg/x/a.go",), author=A, observed_at="2024-05-04T23:30:00+02:00"
    )
    tuesday_morning = _observation(
        41, 0, ("pkg/x/a.go",), author=A, observed_at="2024-05-07T10:00:00+02:00"
    )
    # 21:30 UTC on a Tuesday is 23:30 in the committer's own +02:00 offset.
    late_by_offset = _observation(
        42, 0, ("pkg/x/a.go",), author=A, observed_at="2024-05-07T23:30:00+02:00"
    )

    results = enrich_history_features(
        priors,
        [saturday_night, tuesday_morning, late_by_offset],
        extractor=EXTRACTOR,
        pack_version=4,
        rework_events=[],
    )

    assert "authored_off_hours" in results[0].facts
    assert "authored_off_hours" not in results[1].facts
    assert "authored_off_hours" in results[2].facts
    assert results[2].fact_evidence["authored_off_hours"].evidence == (
        "local_weekday:1;local_hour:23",
    )


def test_v3_enrichment_ignores_author_hashes_and_never_emits_v4_facts() -> None:
    priors = _v4_priors()
    current = _observation(40, 120, ("pkg/x/a.go", "web/y.ts"), author=D)

    (result,) = enrich_history_features(priors, [current], extractor=EXTRACTOR, pack_version=3)

    assert not set(EXPERIENCE_PREDICATES) & result.facts
    assert REWORK_HISTORY_PREDICATE not in result.facts
    assert OFF_HOURS_PREDICATE not in result.facts
    assert "experience_status" not in result.metadata["historical_context"]
    assert result.metadata["historical_context"]["version"] == "ruleloom-history-features/2"


def test_history_features_find_hotspots_and_missing_usual_partner_without_future_data() -> None:
    priors = [_observation(index, index * 10, ("a", "b")) for index in range(1, 6)]
    current = _observation(6, 60, ("a",))
    future = _observation(7, 70, ("a",))

    enriched = enrich_history_features([*priors, future], [current], extractor=EXTRACTOR)[0]

    assert "touches_recent_change_hotspot" in enriched.facts
    assert "missing_usual_cochange_partner" in enriched.facts
    assert enriched.metadata["historical_context"]["eligible_prior_observations"] == 5
    evidence = enriched.fact_evidence["missing_usual_cochange_partner"]
    assert evidence.kind == "deterministic"
    assert evidence.extractor == EXTRACTOR
    assert any("missing:b" in reason for reason in evidence.evidence)


def test_history_features_identify_known_dormancy_but_abstain_on_truncated_paths() -> None:
    old = _observation(1, 0, ("dormant",))
    current = _observation(2, 400, ("dormant",))
    truncated = _observation(3, 401, ("dormant",))
    truncated = Observation.from_dict(
        {
            **truncated.to_dict(),
            "metadata": {
                **truncated.metadata,
                "metadata_files_truncated": 1,
            },
        }
    )

    dormant = enrich_history_features([old], [current], extractor=EXTRACTOR)[0]
    abstained = enrich_history_features([old, current], [truncated], extractor=EXTRACTOR)[0]

    assert "touches_dormant_area" in dormant.facts
    assert abstained.metadata["historical_context"]["status"] == "abstained"
    assert not {
        "crosses_codeowners_boundary",
        "touches_dormant_area",
        "touches_recent_change_hotspot",
        "missing_usual_cochange_partner",
    }.intersection(abstained.facts)


def test_time_window_features_abstain_after_non_monotonic_commit_timestamps() -> None:
    priors = [_observation(index, 10 + index, ("a",)) for index in range(1, 5)]
    current = _observation(5, 5, ("a",))

    enriched = enrich_history_features(priors, [current], extractor=EXTRACTOR)[0]

    assert "touches_recent_change_hotspot" not in enriched.facts
    assert "touches_dormant_area" not in enriched.facts
    assert (
        enriched.metadata["historical_context"]["time_features_status"]
        == "abstained_non_monotonic_timestamps"
    )


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def test_history_features_detect_codeowners_boundaries_from_the_prior_snapshot(
    tmp_path: Path,
) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "ruleloom@example.invalid")
    _git(tmp_path, "config", "user.name", "RuleLoom Test")
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "CODEOWNERS").write_text(
        "/api/** @api-team\n/web/** @web-team\n",
        encoding="utf-8",
    )
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "seed owners")
    base = _git(tmp_path, "rev-parse", "HEAD")
    current = _observation(1, 1, ("api/model.py", "web/view.ts"), base=base)

    enriched = enrich_history_features(
        [],
        [current],
        extractor=EXTRACTOR,
        root=tmp_path,
    )[0]

    assert "crosses_codeowners_boundary" in enriched.facts
    context = enriched.metadata["historical_context"]
    assert context["codeowners"]["status"] == "available"
    assert context["codeowners"]["owner_boundaries"] == 2
    assert "@api-team" not in str(enriched.to_dict())


def test_codeowners_reject_negation_and_ignore_inline_comment_tokens(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "ruleloom@example.invalid")
    _git(tmp_path, "config", "user.name", "RuleLoom Test")
    (tmp_path / "CODEOWNERS").write_text(
        "!src/** @ignored\nsrc/** @src # explanation\ntests/** @tests\n",
        encoding="utf-8",
    )
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "seed owners")
    base = _git(tmp_path, "rev-parse", "HEAD")
    current = _observation(1, 1, ("src/model.py", "tests/test_model.py"), base=base)

    enriched = enrich_history_features([], [current], extractor=EXTRACTOR, root=tmp_path)[0]

    assert "crosses_codeowners_boundary" in enriched.facts
    context = enriched.metadata["historical_context"]
    assert context["codeowners"]["unsupported_rules"] == 1
    assert context["codeowners"]["owner_boundaries"] == 2


def test_history_features_abstain_when_prior_codeowners_are_unavailable(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "ruleloom@example.invalid")
    _git(tmp_path, "config", "user.name", "RuleLoom Test")
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "seed without owners")
    base = _git(tmp_path, "rev-parse", "HEAD")
    current = _observation(1, 1, ("api/model.py", "web/view.ts"), base=base)

    enriched = enrich_history_features(
        [],
        [current],
        extractor=EXTRACTOR,
        root=tmp_path,
    )[0]

    assert "crosses_codeowners_boundary" not in enriched.facts
    codeowners = enriched.metadata["historical_context"]["codeowners"]
    assert codeowners == {"status": "abstained", "reason": "codeowners_not_found"}


def test_codeowners_snapshots_use_bounded_git_batches(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "ruleloom@example.invalid")
    _git(tmp_path, "config", "user.name", "RuleLoom Test")
    (tmp_path / "CODEOWNERS").write_text("api/** @api-team\nweb/** @web-team\n", encoding="utf-8")
    (tmp_path / "seed.txt").write_text("0\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "seed owners")
    bases: list[str] = []
    for index in range(1, 21):
        (tmp_path / "seed.txt").write_text(f"{index}\n", encoding="utf-8")
        _git(tmp_path, "add", "seed.txt")
        _git(tmp_path, "commit", "-qm", f"change {index}")
        bases.append(_git(tmp_path, "rev-parse", "HEAD"))
    calls = 0
    original = gitfacts._run_git_capped

    def counted(*args: Any, **kwargs: Any) -> tuple[bytes, bytes, int]:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(gitfacts, "_run_git_capped", counted)
    observations = [
        _observation(index, index, ("api/model.py", "web/view.ts"), base=base)
        for index, base in enumerate(bases, 1)
    ]

    enriched = enrich_history_features(
        [],
        observations,
        extractor=EXTRACTOR,
        root=tmp_path,
    )

    assert len(enriched) == 20
    assert all("crosses_codeowners_boundary" in item.facts for item in enriched)
    assert calls == 2


def test_codeowners_batch_reader_fails_closed_on_malformed_git_output(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    base = "a" * 40

    def raise_git_error(*_args: Any, **_kwargs: Any) -> tuple[bytes, bytes, int]:
        raise gitfacts.GitFactsError("unavailable")

    monkeypatch.setattr(gitfacts, "_run_git_capped", raise_git_error)
    objects, failures = history_features._codeowners_blob_index(tmp_path, (base,))
    assert not objects
    assert set(failures.values()) == {"git_codeowners_read_failed"}

    responses = (
        (b"", b"", 1),
        (b"\xff", b"", 0),
        (b"", b"", 0),
        (
            b"dead tree 0\ndead blob invalid\ndead blob 1048577\n",
            b"",
            0,
        ),
    )
    for response in responses:
        monkeypatch.setattr(
            gitfacts,
            "_run_git_capped",
            lambda *_a, _response=response, **_kw: _response,
        )
        objects, failures = history_features._codeowners_blob_index(tmp_path, (base,))
        assert not objects
        assert failures


def test_codeowners_content_reader_and_snapshot_decoder_fail_closed(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    object_id = "b" * 40
    base = "a" * 40

    def raise_git_error(*_args: Any, **_kwargs: Any) -> tuple[bytes, bytes, int]:
        raise gitfacts.GitFactsError("unavailable")

    monkeypatch.setattr(gitfacts, "_run_git_capped", raise_git_error)
    assert history_features._codeowners_blob_contents(tmp_path, (object_id,)) == {}

    for response in (
        (b"", b"", 1),
        (b"no-newline", b"", 0),
        (b"\xff\n", b"", 0),
        (b"wrong blob 1\nx\n", b"", 0),
        (f"{object_id} blob invalid\n".encode(), b"", 0),
        (f"{object_id} blob 2\nx\n".encode(), b"", 0),
    ):
        monkeypatch.setattr(
            gitfacts,
            "_run_git_capped",
            lambda *_a, _response=response, **_kw: _response,
        )
        assert history_features._codeowners_blob_contents(tmp_path, (object_id,)) == {}

    monkeypatch.setattr(
        history_features,
        "_codeowners_blob_index",
        lambda _root, _bases, *_locations: (
            {(base, "CODEOWNERS"): object_id},
            {(base, ".github/CODEOWNERS"): "codeowners_not_found"},
        ),
    )
    monkeypatch.setattr(history_features, "_codeowners_blob_contents", lambda *_a: {})
    assert history_features._read_codeowners_batch(tmp_path, {base})[base] == (
        None,
        "git_codeowners_read_failed",
    )
    monkeypatch.setattr(
        history_features,
        "_codeowners_blob_contents",
        lambda *_a: {object_id: b"\xff"},
    )
    assert history_features._read_codeowners_batch(tmp_path, {base})[base] == (
        None,
        "codeowners_is_not_utf8",
    )
    assert history_features._read_codeowners_batch(None, {"invalid"}) == {
        "invalid": (None, "base_commit_unavailable")
    }


def test_history_pair_budget_abstains_without_losing_other_facts(monkeypatch: Any) -> None:
    monkeypatch.setattr(history_features, "_MAX_PAIR_UPDATES", 0)
    first = _observation(1, 1, ("a", "b"))
    second = _observation(2, 2, ("a", "b"))

    enriched = enrich_history_features([first], [second], extractor=EXTRACTOR)[0]

    context = enriched.metadata["historical_context"]
    assert context["pair_budget_exhausted"] is True
    assert context["pair_updates"] == 0
    assert context["cochange_feature_status"] == "abstained_pair_budget_exhausted"
    assert "missing_usual_cochange_partner" not in enriched.facts


def test_codeowners_abstains_when_match_work_exceeds_budget(monkeypatch: Any) -> None:
    base = "a" * 40
    current = _observation(1, 1, ("src/a.py", "tests/a.py"), base=base)
    monkeypatch.setattr(history_features, "_MAX_CODEOWNERS_MATCH_WORK", 1)
    monkeypatch.setattr(
        history_features,
        "_read_codeowners_batch",
        lambda _root, _bases: {base: ("src/** @src\ntests/** @tests\n", "CODEOWNERS")},
    )

    enriched = enrich_history_features([], [current], extractor=EXTRACTOR)[0]

    assert "crosses_codeowners_boundary" not in enriched.facts
    codeowners = enriched.metadata["historical_context"]["codeowners"]
    assert codeowners["status"] == "abstained"
    assert codeowners["reason"] == "codeowners_match_work_limit_exceeded"
