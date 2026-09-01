"""Command-line interface for the end-to-end RuleLoom workflow."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sys
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from ruleloom import __version__
from ruleloom.agents import sync_agents
from ruleloom.config import CONFIG_PATH, RuleLoomConfig, discover_root
from ruleloom.gitfacts import (
    BackfillReport,
    GitFactsError,
    backfill_commits_detailed,
    collect_snapshot,
    collect_worktree,
    repository_identity,
)
from ruleloom.history.git import GitHistoryError, collect_git_history
from ruleloom.history.importing import import_change_units, import_events
from ruleloom.history.materialize import materialize_history
from ruleloom.history.models import ChangeUnit, HistoricalEvent
from ruleloom.history.outcomes import ATOMIC_OUTCOME_TARGETS
from ruleloom.history.storage import (
    change_units_path,
    events_path,
    load_change_units,
    load_events,
    upsert_change_units,
    upsert_events,
)
from ruleloom.history.units import (
    assemble_change_units,
    validate_change_unit_event_links,
    validate_change_unit_evidence,
    validate_unique_event_ownership,
)
from ruleloom.learners.popper import PopperError
from ruleloom.lifecycle import (
    deprecate_candidate,
    learn_candidate,
    make_prediction,
    promote_candidate,
    readiness,
    save_learned_candidate,
    trust_reviewed_artifact,
    utc_now,
)
from ruleloom.models import (
    LabelEvidence,
    LabelValue,
    ModelError,
    Observation,
    parse_timestamp,
    validate_subject,
)
from ruleloom.packs import (
    ConfiguredPathsConfig,
    PathPredicateConfig,
    available_packs,
    latest_pack_version,
)
from ruleloom.project import initialize_project, validate_observations, validate_project
from ruleloom.reporting import build_pilot_report, build_pilot_reports
from ruleloom.storage import (
    append_prediction,
    candidate_path,
    dataset_path,
    edit_observations,
    load_approved,
    load_candidate,
    load_candidates,
    load_observations,
    load_shadow,
    load_trusted_predictions,
    predictions_path,
)


def _json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _root(args: argparse.Namespace) -> Path:
    configured = getattr(args, "root", None)
    if configured == "":
        raise ModelError("--root must not be empty")
    if configured is None:
        return discover_root()
    root = Path(configured).resolve()
    if not root.is_dir():
        raise ModelError(f"--root is not an existing directory: {root}")
    if not (root / CONFIG_PATH).is_file():
        raise ModelError(f"--root is not an initialized RuleLoom project: {root}")
    return root


def _project(args: argparse.Namespace) -> tuple[Path, RuleLoomConfig]:
    root = _root(args)
    return root, RuleLoomConfig.load(root)


def _ensure_repository_boundary(root: Path, config: RuleLoomConfig) -> None:
    try:
        actual_repository = repository_identity(root)
    except GitFactsError as exc:
        raise ModelError(f"cannot verify configured Git repository: {exc}") from exc
    if actual_repository != config.protocol.repository_id:
        raise ModelError(
            f"configured repository id {config.protocol.repository_id!r} does not match "
            f"this checkout {actual_repository!r}"
        )


def _config_with_engine(config: RuleLoomConfig, engine: str | None) -> RuleLoomConfig:
    if engine is not None and engine != config.learner.engine:
        raise ModelError(
            "--engine cannot override the persisted learner profile; edit .ruleloom/config.json "
            "so learning and promotion bind the same complete configuration"
        )
    return config


def _external_popper_checkout(root: Path, config: RuleLoomConfig) -> Path:
    raw = config.learner.popper_dir
    if raw is None:
        raise ModelError("Popper execution requires learner.popper_dir in the reviewed config")
    configured = Path(raw)
    if not configured.is_absolute():
        raise ModelError("Popper execution requires an absolute learner.popper_dir")
    checkout = configured.resolve()
    try:
        checkout.relative_to(root.resolve())
    except ValueError:
        return checkout
    raise ModelError(
        "refusing to execute a Popper runtime stored inside the repository; provision a "
        "reviewed checkout outside the project"
    )


def _cmd_init(args: argparse.Namespace) -> int:
    if args.path == "":
        raise ModelError("init path must not be empty")
    selected = {
        "none": (),
        "all": ("codex", "claude"),
        "codex": ("codex",),
        "claude": ("claude",),
    }[args.agents]
    pack_config = _init_pack_config(args)
    result = initialize_project(
        Path(args.path if args.path is not None else "."),
        args.project,
        target=args.target,
        outcome_definition=args.outcome_definition,
        pack=args.pack,
        pack_version=args.pack_version,
        pack_config=pack_config,
        schema_version=3 if pack_config is not None else 2,
        agents=selected,
    )
    print(f"Initialized RuleLoom in {result.root}")
    print(f"Config: {result.root / CONFIG_PATH}")
    if selected:
        print(
            "Agent skills: "
            + " + ".join(item.title() for item in selected)
            + " (no approved rules)"
        )
    else:
        print("Agent skills: not installed (shadow-safe default)")
    return 0


def _init_pack_config(args: argparse.Namespace) -> ConfiguredPathsConfig | None:
    raw_includes: list[str] = args.path_predicate
    raw_excludes: list[str] = args.path_exclude
    if args.pack != "configured_paths":
        if raw_includes or raw_excludes:
            raise ModelError("--path-predicate and --path-exclude require --pack configured_paths")
        return None
    if not raw_includes:
        raise ModelError("configured_paths requires at least one --path-predicate PREDICATE=GLOB")

    def split(value: str, option: str) -> tuple[str, str]:
        predicate, separator, pattern = value.partition("=")
        if not separator or not predicate or not pattern:
            raise ModelError(f"{option} must use PREDICATE=GLOB")
        return predicate, pattern

    includes: dict[str, list[str]] = {}
    excludes: dict[str, list[str]] = {}
    for value in raw_includes:
        predicate, pattern = split(value, "--path-predicate")
        includes.setdefault(predicate, []).append(pattern)
    for value in raw_excludes:
        predicate, pattern = split(value, "--path-exclude")
        if predicate not in includes:
            raise ModelError(f"--path-exclude references undefined predicate {predicate!r}")
        excludes.setdefault(predicate, []).append(pattern)
    return ConfiguredPathsConfig(
        tuple(
            PathPredicateConfig(
                predicate=predicate,
                include_paths=tuple(patterns),
                exclude_paths=tuple(excludes.get(predicate, ())),
            )
            for predicate, patterns in includes.items()
        )
    )


def _cmd_collect_git(args: argparse.Namespace) -> int:
    root, config = _project(args)
    audit: BackfillReport | None = None
    if args.last is not None:
        if args.head is not None:
            raise ModelError("collect git --head is valid only with --base")
        audit = backfill_commits_detailed(
            root,
            args.last,
            protocol_hash=config.evidence_protocol_hash,
            target=config.target,
            ref=args.ref if args.ref is not None else "HEAD",
            pack=config.pack,
            pack_version=config.pack_version,
            pack_config=config.pack_config,
            evidence_config=config.evidence,
            repository_id=config.protocol.repository_id,
        )
        observations = list(audit.observations)
    elif args.working_tree:
        if args.head is not None:
            raise ModelError("collect git --head is valid only with --base")
        observations = [
            collect_worktree(
                root,
                args.ref if args.ref is not None else "HEAD",
                protocol_hash=config.evidence_protocol_hash,
                target=config.target,
                pack=config.pack,
                pack_version=config.pack_version,
                pack_config=config.pack_config,
                evidence_config=config.evidence,
                repository_id=config.protocol.repository_id,
            )
        ]
    else:
        if args.base is None:
            raise ModelError("collect git requires --last or --base")
        if args.ref is not None:
            raise ModelError("collect git --ref is valid only with --last or --working-tree")
        observations = [
            collect_snapshot(
                root,
                args.base,
                args.head if args.head is not None else "HEAD",
                protocol_hash=config.evidence_protocol_hash,
                target=config.target,
                pack=config.pack,
                pack_version=config.pack_version,
                pack_config=config.pack_config,
                evidence_config=config.evidence,
                repository_id=config.protocol.repository_id,
            )
        ]
    inserted, updated, observations = _persist_collected(dataset_path(root, config), observations)
    if audit is None:
        audit = BackfillReport(
            observations=tuple(observations),
            examined=1,
            skipped_no_in_scope_files=0,
            skipped_mixed_scope=0,
            skipped_preview=(),
            skipped_manifest_hash=hashlib.sha256().hexdigest(),
        )
    _json(
        {
            "collected": len(observations),
            "inserted": inserted,
            "updated": updated,
            "ids": [item.id for item in observations],
            **audit.to_dict(),
        }
    )
    return 0


class _HistoricalRecord(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def repository_id(self) -> str: ...


def _ensure_history_repository(records: Sequence[_HistoricalRecord], repository_id: str) -> None:
    mismatched = [item.id for item in records if item.repository_id != repository_id]
    if mismatched:
        raise ModelError(
            "historical import contains records for a different repository: "
            + ", ".join(str(item) for item in mismatched[:10])
        )


def _preflight_immutable_history(
    existing: Sequence[HistoricalEvent] | Sequence[ChangeUnit],
    incoming: Sequence[HistoricalEvent] | Sequence[ChangeUnit],
    *,
    kind: str,
) -> None:
    persisted = {item.id: item for item in existing}
    staged: dict[str, object] = {}
    for item in incoming:
        identifier = item.id
        previous = staged.get(identifier, persisted.get(identifier))
        if previous is not None and previous != item:
            raise ModelError(f"conflicting immutable {kind} id {identifier!r}")
        staged[identifier] = item


def _cmd_history_bootstrap_git(args: argparse.Namespace) -> int:
    root, config = _project(args)
    _ensure_repository_boundary(root, config)
    maximum = args.max_commits
    if args.all or maximum is None:
        maximum = None
    report = collect_git_history(
        root,
        ref=args.ref,
        max_commits=maximum,
        since=args.since,
        repository_id=config.protocol.repository_id,
    )
    events_inserted, events_unchanged = upsert_events(events_path(root), report.events)
    units_inserted, units_unchanged = upsert_change_units(change_units_path(root), report.units)
    _json(
        {
            **report.to_dict(),
            "events_inserted": events_inserted,
            "events_unchanged": events_unchanged,
            "units_inserted": units_inserted,
            "units_unchanged": units_unchanged,
            "evidence_grade": "exploratory_git_only",
            "note": (
                "Git topology is language-neutral but does not establish PR-time snapshots "
                "or independent outcomes. Import normalized provider events for confirmatory "
                "change units."
            ),
        }
    )
    return 0


def _cmd_history_import(args: argparse.Namespace) -> int:
    root, config = _project(args)
    _ensure_repository_boundary(root, config)
    incoming_events = import_events(Path(args.events)) if args.events is not None else ()
    imported_units = import_change_units(Path(args.units)) if args.units is not None else ()
    if not incoming_events and not imported_units:
        raise ModelError("history import requires --events and/or --units")
    _ensure_history_repository(incoming_events, config.protocol.repository_id)
    _ensure_history_repository(imported_units, config.protocol.repository_id)

    existing_events = load_events(events_path(root))
    existing_units = load_change_units(change_units_path(root))
    combined_events = {item.id: item for item in existing_events}
    for item in incoming_events:
        previous = combined_events.get(item.id)
        if previous is not None and previous != item:
            raise ModelError(f"conflicting immutable historical event id {item.id!r}")
        combined_events[item.id] = item
    assembled = (
        assemble_change_units(tuple(combined_events.values()))
        if incoming_events and not args.no_assemble
        else ()
    )
    incoming_units = (*imported_units, *assembled)
    _ensure_history_repository(incoming_units, config.protocol.repository_id)
    _preflight_immutable_history(existing_events, incoming_events, kind="historical event")
    _preflight_immutable_history(existing_units, incoming_units, kind="change unit")
    combined_event_values = tuple(combined_events.values())
    combined_events_by_change: dict[tuple[str, str], list[HistoricalEvent]] = defaultdict(list)
    for event in combined_event_values:
        if event.change_id is not None:
            combined_events_by_change[(event.repository_id, event.change_id)].append(event)
    combined_units = {item.id: item for item in existing_units}
    combined_units.update({item.id: item for item in incoming_units})
    validate_unique_event_ownership(tuple(combined_units.values()))
    for unit in combined_units.values():
        validate_change_unit_event_links(unit, combined_events)
        linked = {
            event.id: event
            for event in combined_events_by_change.get((unit.repository_id, unit.id), ())
        }
        for event_id in unit.event_ids:
            attached_event = combined_events[event_id]
            if attached_event.change_id is None:
                linked[attached_event.id] = attached_event
        validate_change_unit_evidence(unit, tuple(linked.values()))

    event_counts = upsert_events(events_path(root), incoming_events)
    unit_counts = upsert_change_units(change_units_path(root), incoming_units)
    _json(
        {
            "events_imported": len(incoming_events),
            "events_inserted": event_counts[0],
            "events_unchanged": event_counts[1],
            "units_imported": len(imported_units),
            "units_assembled": len(assembled),
            "units_inserted": unit_counts[0],
            "units_unchanged": unit_counts[1],
        }
    )
    return 0


def _cmd_history_materialize(args: argparse.Namespace) -> int:
    root, config = _project(args)
    _ensure_repository_boundary(root, config)
    events = load_events(events_path(root))
    units = load_change_units(change_units_path(root))
    if not units:
        raise ModelError("no historical change units are available; collect or import them first")
    report = materialize_history(
        root,
        config,
        units,
        events,
        outcome_target=args.outcome_target,
        include_weak=args.include_weak,
    )
    inserted, updated, _ = _persist_historical(
        dataset_path(root, config),
        list(report.observations),
        target=config.target,
    )
    validate_project(root, config)
    _json({**report.to_dict(), "inserted": inserted, "updated": updated})
    return 0


def _cmd_history_status(args: argparse.Namespace) -> int:
    root, config = _project(args)
    validate_project(root, config)
    events = load_events(events_path(root))
    units = load_change_units(change_units_path(root))
    observations = load_observations(dataset_path(root, config))
    historical = [item for item in observations if item.source.get("kind") == "historical_change"]
    qualities: dict[str, int] = {}
    for unit in units:
        qualities[unit.evidence_quality] = qualities.get(unit.evidence_quality, 0) + 1
    labels = {item.value: 0 for item in LabelValue}
    for observation in historical:
        labels[observation.labels.get(config.target, LabelValue.UNKNOWN).value] += 1
    _json(
        {
            "events": len(events),
            "change_units": len(units),
            "evidence_quality": qualities,
            "confirmatory_units": sum(item.confirmatory for item in units),
            "historical_observations": len(historical),
            "labels": labels,
            "language_neutral_core": True,
        }
    )
    return 0


def _label_one(
    observations: list[Observation],
    *,
    observation_id: str,
    target: str,
    value: LabelValue,
    kind: str,
    source: str,
    available_at: str,
    reason: str,
    confidence: float | None,
    as_of: datetime,
) -> None:
    for index, observation in enumerate(observations):
        if observation.id != observation_id:
            continue
        current = observation.labels.get(target, LabelValue.UNKNOWN)
        if observation.source.get("kind") == "historical_change":
            raise ModelError(
                "historical_change labels are derived from immutable events; import a "
                "normalized outcome event and run `ruleloom history materialize`"
            )
        if current is not LabelValue.UNKNOWN:
            raise ModelError(
                f"mature label {target!r} for {observation_id} is immutable; "
                "record a new corrected observation with explicit provenance"
            )
        evidence = (
            None
            if value is LabelValue.UNKNOWN
            else LabelEvidence(
                kind=kind,
                available_at=available_at,
                source=source,
                reason=reason,
                confidence=confidence,
            )
        )
        if evidence is not None:
            available = parse_timestamp(evidence.available_at)
            if available > as_of:
                raise ModelError(
                    f"label available_at cannot be in the future: {evidence.available_at}"
                )
            if available <= parse_timestamp(observation.observed_at):
                raise ModelError(f"label for {observation_id} must postdate observation time")
        observations[index] = observation.with_label(target, value, evidence)
        return
    raise ModelError(f"unknown observation id: {observation_id}")


def _cmd_label(args: argparse.Namespace) -> int:
    root, config = _project(args)
    as_of = datetime.now(UTC)
    with edit_observations(dataset_path(root, config)) as observations:
        _label_one(
            observations,
            observation_id=args.observation_id,
            target=args.target if args.target is not None else config.target,
            value=LabelValue(args.value),
            kind=args.kind,
            source=args.source,
            available_at=args.available_at if args.available_at is not None else utc_now(as_of),
            reason=args.reason,
            confidence=args.confidence,
            as_of=as_of,
        )
        validate_observations(observations, config, as_of=as_of)
    print(f"Labeled {args.observation_id} as {args.value}")
    return 0


def _cmd_import_labels(args: argparse.Namespace) -> int:
    root, config = _project(args)
    as_of = datetime.now(UTC)
    path = Path(args.file)
    try:
        handle = path.open(encoding="utf-8", newline="")
    except OSError as exc:
        raise ModelError(f"cannot read label CSV {path}: {exc}") from exc
    imported = 0
    with handle, edit_observations(dataset_path(root, config)) as observations:
        reader = csv.DictReader(handle)
        required = {"id", "value", "available_at", "kind", "source"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ModelError(
                "label CSV requires columns: id,value,available_at,kind,source; "
                "reason,confidence,target are optional"
            )
        for row in reader:
            raw_confidence = row.get("confidence", "").strip()
            confidence = float(raw_confidence) if raw_confidence else None
            try:
                value = LabelValue(row["value"].strip())
            except ValueError as exc:
                raise ModelError(f"unsupported label value in row {reader.line_num}") from exc
            _label_one(
                observations,
                observation_id=row["id"].strip(),
                target=row.get("target", "").strip() or config.target,
                value=value,
                kind=row["kind"].strip(),
                source=row["source"].strip(),
                available_at=row["available_at"].strip(),
                reason=row.get("reason", "").strip(),
                confidence=confidence,
                as_of=as_of,
            )
            imported += 1
        validate_observations(observations, config, as_of=as_of)
    print(f"Imported {imported} labels from {path}")
    return 0


def _cmd_readiness(args: argparse.Namespace) -> int:
    root, config = _project(args)
    report = validate_project(root, config)
    _json(report.to_dict())
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    root, config = _project(args)
    report = validate_project(root, config)
    print(f"Valid RuleLoom project: {root}")
    print(
        f"Observations: {report.observations}; mature labels: {report.labeled}; "
        f"stage: {report.stage}"
    )
    return 0


def _cmd_learn(args: argparse.Namespace) -> int:
    root, loaded_config = _project(args)
    config = _config_with_engine(loaded_config, args.engine)
    validate_project(root, config)
    if config.learner.engine == "popper":
        _external_popper_checkout(root, config)
    observations = load_observations(dataset_path(root, config))
    validate_observations(observations, config)
    candidate = learn_candidate(observations, config)
    path = save_learned_candidate(root, config, candidate)
    if args.json:
        _json(candidate.to_dict())
    else:
        print(f"Candidate: {candidate.id}")
        print(f"Rules: {len(candidate.rules.clauses)}; stability: {candidate.stability:.3f}")
        test = candidate.metrics["test"]
        print(
            f"Temporal test — precision {test.precision:.3f}, recall {test.recall:.3f}, "
            f"MCC {test.matthews_correlation:.3f}"
        )
        print(f"Manifest: {path}")
        for warning in candidate.warnings:
            print(f"Warning: {warning}")
    return 0


def _cmd_promote(args: argparse.Namespace) -> int:
    root, config = _project(args)
    validate_project(root, config)
    if config.learner.engine == "popper":
        _external_popper_checkout(root, config)
    promoted, decision, path = promote_candidate(
        root,
        config,
        args.candidate_id,
        destination=args.to,
        reviewer=args.reviewer,
        note=args.note,
        override=args.override,
    )
    print(f"Promoted {promoted.id} to {promoted.status}: {path}")
    if decision.unmet:
        print("Human override recorded for unmet gates:")
        for gate in decision.unmet:
            print(f"- {gate}")
    if promoted.status == "approved":
        print("Run `ruleloom sync-agents` to publish reviewed rule cards to agent skills.")
    return 0


def _cmd_deprecate(args: argparse.Namespace) -> int:
    root, config = _project(args)
    deprecated, path = deprecate_candidate(
        root,
        config,
        args.candidate_id,
        reviewer=args.reviewer,
        note=args.note,
    )
    print(f"Deprecated {deprecated.id}: {path}")
    print("Run `ruleloom sync-agents` to remove it from generated agent skills.")
    return 0


def _cmd_trust(args: argparse.Namespace) -> int:
    root, config = _project(args)
    candidate, path = trust_reviewed_artifact(
        root,
        config,
        args.candidate_id,
        status=args.status,
        reviewer=args.reviewer,
        note=args.note,
    )
    print(f"Trusted {candidate.status} artifact {candidate.id} in this worktree: {path}")
    return 0


def _cmd_sync_agents(args: argparse.Namespace) -> int:
    root, config = _project(args)
    selected = ("codex", "claude") if args.agent == "all" else (args.agent,)
    results = sync_agents(root, config, agents=selected, check=args.check)
    changed = [item for item in results if item.changed]
    for result in results:
        state = (
            "would change"
            if args.check and result.changed
            else "updated"
            if result.changed
            else "ok"
        )
        print(f"{state}: {result.path}")
    return 1 if args.check and changed else 0


def _merge_collected_observation(
    existing: list[Observation], collected: Observation
) -> Observation:
    prior = next((item for item in existing if item.id == collected.id), None)
    return _merge_collected_prior(prior, collected)


def _merge_collected_prior(
    prior: Observation | None,
    collected: Observation,
) -> Observation:
    if prior is None:
        return collected
    if (
        prior.facts != collected.facts
        or prior.fact_evidence != collected.fact_evidence
        or prior.protocol_hash != collected.protocol_hash
        or prior.metadata != collected.metadata
    ):
        raise ModelError(
            f"immutable Git snapshot {collected.id} produced different evidence or protocol; "
            "start a new experiment or check extractor/version provenance"
        )
    source_keys = {
        "kind",
        "repository",
        "base",
        "head",
        "pack",
        "pack_version",
        "pack_config_hash",
        "extractor",
    }
    if any(prior.source.get(key) != collected.source.get(key) for key in source_keys):
        raise ModelError(f"immutable Git snapshot {collected.id} has conflicting provenance")
    prior_change = prior.source.get("change_id")
    collected_change = collected.source.get("change_id")
    if (
        prior_change is not None
        and collected_change is not None
        and prior_change != collected_change
    ):
        raise ModelError(f"immutable Git snapshot {collected.id} has conflicting change_id")
    if prior_change is None and collected_change is not None:
        return replace(prior, source={**prior.source, "change_id": collected_change})
    return prior


def _persist_collected(
    path: Path, collected: list[Observation]
) -> tuple[int, int, list[Observation]]:
    with edit_observations(path) as existing:
        by_id = {item.id: item for item in existing}
        merged = [_merge_collected_prior(by_id.get(item.id), item) for item in collected]
        inserted = 0
        updated = 0
        for observation in merged:
            if observation.id in by_id:
                updated += by_id[observation.id] != observation
            else:
                inserted += 1
            by_id[observation.id] = observation
        existing[:] = by_id.values()
    return inserted, updated, merged


_HISTORICAL_DERIVATION_METADATA = frozenset(
    {
        "historical_event_manifest_hash",
        "historical_votes",
        "history_warnings",
    }
)


def _without_historical_derivation(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in metadata.items() if key not in _HISTORICAL_DERIVATION_METADATA
    }


def _merge_historical_observation(
    prior: Observation | None,
    collected: Observation,
    *,
    target: str,
) -> Observation:
    """Advance or refresh a derived label without changing its mature value."""
    if prior is None:
        return collected
    if prior.source.get("kind") != "historical_change":
        raise ModelError(
            f"historical observation {collected.id!r} conflicts with a non-historical record"
        )
    if (
        prior.observed_at != collected.observed_at
        or prior.protocol_hash != collected.protocol_hash
        or prior.facts != collected.facts
        or prior.fact_evidence != collected.fact_evidence
        or prior.source != collected.source
        or _without_historical_derivation(prior.metadata)
        != _without_historical_derivation(collected.metadata)
    ):
        raise ModelError(
            f"immutable historical snapshot {collected.id!r} changed its predictor, "
            "protocol, or materialization semantics"
        )

    previous_label = prior.labels.get(target, LabelValue.UNKNOWN)
    collected_label = collected.labels.get(target, LabelValue.UNKNOWN)
    if previous_label is not LabelValue.UNKNOWN:
        if collected_label is not previous_label:
            raise ModelError(
                f"mature historical label {target!r} for {collected.id!r} conflicts with "
                "newly imported evidence"
            )
        labels = dict(prior.labels)
        labels[target] = collected_label
        evidence = dict(prior.label_evidence)
        evidence[target] = collected.label_evidence[target]
        return replace(
            prior,
            labels=labels,
            label_evidence=evidence,
            metadata=collected.metadata,
        )

    labels = dict(prior.labels)
    labels[target] = collected_label
    evidence = dict(prior.label_evidence)
    if collected_label is LabelValue.UNKNOWN:
        evidence.pop(target, None)
    else:
        evidence[target] = collected.label_evidence[target]
    return replace(
        prior,
        labels=labels,
        label_evidence=evidence,
        metadata=collected.metadata,
    )


def _persist_historical(
    path: Path,
    collected: list[Observation],
    *,
    target: str,
) -> tuple[int, int, list[Observation]]:
    with edit_observations(path) as existing:
        by_id = {item.id: item for item in existing}
        merged = [
            _merge_historical_observation(by_id.get(item.id), item, target=target)
            for item in collected
        ]
        inserted = 0
        updated = 0
        for observation in merged:
            if observation.id in by_id:
                updated += by_id[observation.id] != observation
            else:
                inserted += 1
            by_id[observation.id] = observation
        existing[:] = by_id.values()
    return inserted, updated, merged


def _cmd_assess(args: argparse.Namespace) -> int:
    root, config = _project(args)
    validate_subject(args.change_id)
    if args.include_shadow and not args.blind:
        raise ModelError(
            "shadow assessment must use --blind so rule matches cannot influence the later outcome"
        )
    if args.blind and args.no_record:
        raise ModelError("--blind requires an immutable prediction record")
    observation = (
        collect_worktree(
            root,
            args.base,
            protocol_hash=config.evidence_protocol_hash,
            target=config.target,
            pack=config.pack,
            pack_version=config.pack_version,
            pack_config=config.pack_config,
            evidence_config=config.evidence,
            repository_id=config.protocol.repository_id,
        )
        if args.head == "WORKTREE"
        else collect_snapshot(
            root,
            args.base,
            args.head,
            protocol_hash=config.evidence_protocol_hash,
            target=config.target,
            pack=config.pack,
            pack_version=config.pack_version,
            pack_config=config.pack_config,
            evidence_config=config.evidence,
            repository_id=config.protocol.repository_id,
        )
    )
    observation = replace(
        observation,
        source={**observation.source, "change_id": args.change_id},
    )
    approved = load_approved(root, config)
    policies = approved
    if args.include_shadow:
        policies_by_id = {item.id: item for item in load_shadow(root, config)}
        policies_by_id.update({item.id: item for item in approved})
        policies = [policies_by_id[key] for key in sorted(policies_by_id)]
    prediction = make_prediction(observation, policies, config)
    if not args.no_record:
        _, _, persisted = _persist_collected(dataset_path(root, config), [observation])
        canonical = persisted[0]
        prediction_snapshot = replace(
            canonical,
            labels={config.target: LabelValue.UNKNOWN},
            label_evidence={},
        )
        prediction = make_prediction(prediction_snapshot, policies, config)
        append_prediction(predictions_path(root, config), prediction, root=root)
    if args.blind:
        blind_payload = {
            "prediction_id": prediction.id,
            "observation_id": prediction.observation.id,
            "unit_id": prediction.unit_id,
            "predicted_at": prediction.predicted_at,
            "protocol_hash": prediction.protocol_hash,
            "recorded": True,
            "blind": True,
        }
        if args.json:
            _json(blind_payload)
        else:
            print(f"Blind prediction recorded: {prediction.id}")
        return 0
    payload = prediction.to_dict()
    payload["recorded"] = not args.no_record
    if args.json:
        _json(payload)
    elif prediction.abstained:
        print("RuleLoom abstained: no reviewed rule matched this change.")
        print("Facts: " + (", ".join(sorted(observation.facts)) or "none"))
    else:
        print(f"Matched {len(prediction.matches)} reviewed rule(s):")
        print("Facts: " + (", ".join(sorted(observation.facts)) or "none"))
        for match in prediction.matches:
            print(f"- {match.get('candidate_id')}: {match.get('prolog')}")
        print("Advisory only: perform the requested extra validation and report its outcome.")
    if not args.no_record:
        print(f"Prediction record: {prediction.id}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    root, config = _project(args)
    as_of = datetime.now(UTC)
    observations = load_observations(dataset_path(root, config))
    validate_observations(observations, config, as_of=as_of)
    predictions = load_trusted_predictions(root, config)
    if args.policy_set:
        selected = [item for item in predictions if item.policy_set_hash == args.policy_set]
        if not selected:
            raise ModelError(f"unknown policy_set_hash: {args.policy_set}")
        _json(
            build_pilot_report(
                observations,
                selected,
                config.target,
                as_of=as_of,
                root=root,
            ).to_dict()
        )
    else:
        reports = build_pilot_reports(
            observations,
            predictions,
            config.target,
            as_of=as_of,
            root=root,
        )
        _json(
            {
                "target": config.target,
                "readiness": readiness(observations, config.target, as_of=as_of).to_dict(),
                "policy_sets": {key: value.to_dict() for key, value in reports.items()},
                "note": "Policy sets are reported separately and must not be pooled.",
            }
        )
    return 0


def _cmd_candidate_list(args: argparse.Namespace) -> int:
    root, config = _project(args)
    candidates = load_candidates(root, config)
    summaries: list[dict[str, object]] = []
    for item in candidates:
        test = item.metrics.get("test")
        summaries.append(
            {
                "id": item.id,
                "created_at": item.created_at,
                "engine": item.engine,
                "rules": len(item.rules.clauses),
                "test_mcc": test.matthews_correlation if test is not None else None,
            }
        )
    _json(summaries)
    return 0


def _cmd_candidate_show(args: argparse.Namespace) -> int:
    root, config = _project(args)
    _json(load_candidate(candidate_path(root, config, args.candidate_id)).to_dict())
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    checks: dict[str, Any] = {
        "python": {"ok": sys.version_info >= (3, 11), "version": sys.version.split()[0]},
        "git": {"ok": shutil.which("git") is not None, "path": shutil.which("git")},
    }
    try:
        root, loaded_config = _project(args)
    except ModelError as exc:
        checks["project"] = {"ok": False, "detail": str(exc)}
        config = None
    else:
        config = loaded_config
        checks["project"] = {
            "ok": True,
            "root": str(root),
            "config": str(root / CONFIG_PATH),
            "pack": loaded_config.pack,
            "pack_version": loaded_config.pack_version,
            "pack_config_hash": loaded_config.pack_config_hash,
            "predicates": list(loaded_config.resolved_pack.predicates),
        }
    from ruleloom.learners.popper import doctor_popper

    popper_dir: str | Path | None = config.learner.popper_dir if config else None
    if args.probe_popper_runtime:
        if config is None:
            raise ModelError("Popper runtime probing requires an initialized project")
        popper_dir = _external_popper_checkout(root, config)
    popper = doctor_popper(
        popper_dir,
        probe_runtime=args.probe_popper_runtime,
    )
    popper_required = config is not None and config.learner.engine == "popper"
    checks["popper_optional"] = {
        "ok": popper.ready,
        "required": popper_required,
        "required_for": "learner.engine=popper only",
        "runtime_probe": args.probe_popper_runtime,
        "requirements": [
            {"name": item.name, "ok": item.available, "detail": item.detail}
            for item in popper.requirements
        ],
    }
    _json(checks)
    required_ok = bool(
        checks["python"]["ok"]
        and checks["git"]["ok"]
        and checks["project"]["ok"]
        and (not popper_required or popper.ready)
    )
    return 0 if required_ok else 1


def _cmd_packs_list(args: argparse.Namespace) -> int:
    packs = [
        {
            "name": item.name,
            "version": item.version,
            "extractor": item.extractor,
            "description": item.description,
            "predicates": list(item.predicates),
            "configurable": item.configurable,
            "latest": item.version == latest_pack_version(item.name),
        }
        for item in available_packs()
    ]
    if args.json:
        _json(packs)
    else:
        for item in packs:
            suffix = " (latest)" if item["latest"] else " (frozen compatibility)"
            if item["configurable"]:
                suffix += " (configuration adds predicates)"
            print(f"{item['name']}@{item['version']}{suffix}: {item['description']}")
    return 0


def _add_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--root",
        help="initialized project root (defaults to discovery from the current directory)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ruleloom",
        description="Inductive logic programming for evidence-backed coding-agent policies.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="initialize a repository")
    init.add_argument("path", nargs="?")
    init.add_argument("--project")
    init.add_argument(
        "--target",
        default="needs_extra_validation",
        help="frozen outcome predicate for this experiment",
    )
    init.add_argument(
        "--outcome-definition",
        help="single-line operational definition bound into the evidence protocol",
    )
    pack_names = sorted({item.name for item in available_packs()})
    init.add_argument(
        "--pack",
        choices=pack_names,
        default="generic_changes",
        help="evidence pack for this experiment (default: generic_changes)",
    )
    init.add_argument(
        "--path-predicate",
        action="append",
        default=[],
        metavar="PREDICATE=GLOB",
        help="configured_paths include glob; repeat for predicates or additional includes",
    )
    init.add_argument(
        "--path-exclude",
        action="append",
        default=[],
        metavar="PREDICATE=GLOB",
        help="configured_paths exclusion glob for an already defined predicate",
    )
    init.add_argument(
        "--pack-version",
        type=int,
        help="explicit built-in pack version (default: latest registered version)",
    )
    init.add_argument(
        "--agents",
        choices=["none", "all", "codex", "claude"],
        default="none",
        help="install agent skills now (default none for shadow-mode isolation)",
    )
    init.set_defaults(handler=_cmd_init)

    packs = subparsers.add_parser("packs", help="inspect built-in evidence packs")
    pack_commands = packs.add_subparsers(dest="packs_command", required=True)
    packs_list = pack_commands.add_parser("list")
    packs_list.add_argument("--json", action="store_true")
    packs_list.set_defaults(handler=_cmd_packs_list)

    collect = subparsers.add_parser("collect", help="collect prediction-time facts")
    _add_root(collect)
    collect_types = collect.add_subparsers(dest="source", required=True)
    git = collect_types.add_parser("git", help="collect deterministic Git facts")
    mode = git.add_mutually_exclusive_group(required=True)
    mode.add_argument("--last", type=int, help="backfill the last N first-parent commits")
    mode.add_argument("--base", help="base commit for one range")
    mode.add_argument(
        "--working-tree",
        action="store_true",
        help="collect staged, unstaged, and untracked files against --ref",
    )
    git.add_argument("--head", help="range head used with --base (default: HEAD)")
    git.add_argument(
        "--ref",
        help="history ref for --last or worktree base for --working-tree (default: HEAD)",
    )
    git.set_defaults(handler=_cmd_collect_git)

    history = subparsers.add_parser(
        "history", help="bootstrap point-in-time evidence from existing repository history"
    )
    _add_root(history)
    history_commands = history.add_subparsers(dest="history_command", required=True)

    bootstrap_git = history_commands.add_parser(
        "bootstrap-git",
        help="ingest language-neutral Git topology (exploratory evidence)",
    )
    bootstrap_limit = bootstrap_git.add_mutually_exclusive_group()
    bootstrap_limit.add_argument(
        "--all",
        action="store_true",
        help=(
            "ingest the most recent reachable prefix up to 100000 commits and canonical "
            "storage limits (default)"
        ),
    )
    bootstrap_limit.add_argument(
        "--max-commits",
        type=int,
        help="limit ingestion to the most recent N commits, still bounded by canonical storage",
    )
    bootstrap_git.add_argument("--ref", default="HEAD")
    bootstrap_git.add_argument("--since", help="optional aware ISO-8601 lower timestamp bound")
    bootstrap_git.set_defaults(handler=_cmd_history_bootstrap_git)

    history_import = history_commands.add_parser(
        "import", help="import normalized provider events and/or change units"
    )
    history_import.add_argument("--events", help="historical-event JSONL file")
    history_import.add_argument("--units", help="change-unit JSONL file")
    history_import.add_argument(
        "--no-assemble",
        action="store_true",
        help="do not assemble change units from imported normalized events",
    )
    history_import.set_defaults(handler=_cmd_history_import)

    materialize = history_commands.add_parser(
        "materialize", help="extract prediction-time facts and conservative outcome labels"
    )
    materialize.add_argument(
        "--outcome-target",
        choices=ATOMIC_OUTCOME_TARGETS,
        help="assert the atomic target registered by config (cannot override it)",
    )
    materialize.add_argument(
        "--include-weak",
        action="store_true",
        help="opt into weak heuristic labels; resulting dependent cases cannot be approved",
    )
    materialize.set_defaults(handler=_cmd_history_materialize)

    history_status = history_commands.add_parser(
        "status", help="summarize historical evidence grades and labels"
    )
    history_status.set_defaults(handler=_cmd_history_status)

    label = subparsers.add_parser("label", help="record a mature outcome with provenance")
    _add_root(label)
    label.add_argument("observation_id")
    label.add_argument("value", choices=[item.value for item in LabelValue])
    label.add_argument("--target")
    label.add_argument(
        "--kind",
        default="human",
        choices=["ci", "review", "incident", "human", "imported", "synthetic"],
    )
    label.add_argument("--source", default="manual-cli")
    label.add_argument("--available-at")
    label.add_argument("--reason", default="")
    label.add_argument("--confidence", type=float)
    label.set_defaults(handler=_cmd_label)

    import_labels = subparsers.add_parser("import-labels", help="bulk-import outcome labels")
    _add_root(import_labels)
    import_labels.add_argument("file")
    import_labels.set_defaults(handler=_cmd_import_labels)

    readiness = subparsers.add_parser("readiness", help="measure data readiness")
    _add_root(readiness)
    readiness.set_defaults(handler=_cmd_readiness)

    validate = subparsers.add_parser("validate", help="validate config, schema, and time order")
    _add_root(validate)
    validate.set_defaults(handler=_cmd_validate)

    learn = subparsers.add_parser("learn", help="learn and temporally evaluate a candidate")
    _add_root(learn)
    learn.add_argument("--engine", choices=["horn", "popper"])
    learn.add_argument("--json", action="store_true")
    learn.set_defaults(handler=_cmd_learn)

    promote = subparsers.add_parser("promote", help="human-reviewed lifecycle transition")
    _add_root(promote)
    promote.add_argument("candidate_id")
    promote.add_argument("--to", required=True, choices=["shadow", "approved"])
    promote.add_argument("--reviewer", required=True)
    promote.add_argument("--note", default="")
    promote.add_argument("--override", action="store_true")
    promote.set_defaults(handler=_cmd_promote)

    deprecate = subparsers.add_parser(
        "deprecate", help="write a reviewed tombstone for an active policy"
    )
    _add_root(deprecate)
    deprecate.add_argument("candidate_id")
    deprecate.add_argument("--reviewer", required=True)
    deprecate.add_argument("--note", required=True)
    deprecate.set_defaults(handler=_cmd_deprecate)

    trust = subparsers.add_parser(
        "trust", help="locally attest a reviewed artifact after inspecting a clone"
    )
    _add_root(trust)
    trust.add_argument("candidate_id")
    trust.add_argument("--status", required=True, choices=["shadow", "approved", "deprecated"])
    trust.add_argument("--reviewer", required=True)
    trust.add_argument("--note", required=True)
    trust.set_defaults(handler=_cmd_trust)

    sync = subparsers.add_parser("sync-agents", help="sync approved rules to agent skills")
    _add_root(sync)
    sync.add_argument("--agent", choices=["all", "codex", "claude"], default="all")
    sync.add_argument("--check", action="store_true")
    sync.set_defaults(handler=_cmd_sync_agents)

    assess = subparsers.add_parser("assess", help="evaluate reviewed policies on a Git range")
    _add_root(assess)
    assess.add_argument("--base", required=True)
    assess.add_argument(
        "--change-id",
        required=True,
        help="stable PR/task/change identifier used as the independent pilot unit",
    )
    assess.add_argument(
        "--head",
        default="WORKTREE",
        help="committed head or WORKTREE (default) for uncommitted agent changes",
    )
    assess.add_argument("--include-shadow", action="store_true")
    assess.add_argument(
        "--blind",
        action="store_true",
        help="record without revealing facts or rule matches (required for recorded shadow)",
    )
    assess.add_argument("--no-record", action="store_true")
    assess.add_argument("--json", action="store_true")
    assess.set_defaults(handler=_cmd_assess)

    report = subparsers.add_parser("report", help="report leakage-safe prospective pilot metrics")
    _add_root(report)
    report.add_argument("--policy-set", help="report one exact policy_set_hash")
    report.set_defaults(handler=_cmd_report)

    candidate = subparsers.add_parser("candidate", help="inspect immutable candidates")
    _add_root(candidate)
    candidate_commands = candidate.add_subparsers(dest="candidate_command", required=True)
    candidate_list = candidate_commands.add_parser("list")
    candidate_list.set_defaults(handler=_cmd_candidate_list)
    candidate_show = candidate_commands.add_parser("show")
    candidate_show.add_argument("candidate_id")
    candidate_show.set_defaults(handler=_cmd_candidate_show)

    doctor = subparsers.add_parser("doctor", help="check local and optional Popper requirements")
    _add_root(doctor)
    doctor.add_argument(
        "--probe-popper-runtime",
        action="store_true",
        help="execute the explicitly configured external Popper Python for a compatibility probe",
    )
    doctor.set_defaults(handler=_cmd_doctor)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (ModelError, GitFactsError, GitHistoryError, PopperError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
