"""Command-line interface for the end-to-end RuleLoom workflow."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sys
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from ruleloom import __version__
from ruleloom.agents import sync_agents
from ruleloom.config import CONFIG_PATH, RuleLoomConfig, discover_root
from ruleloom.first_hour import (
    FirstHourAuditError,
    RepositoryAuditLimits,
    audit_repository,
)
from ruleloom.gitfacts import (
    BackfillReport,
    GitFactsError,
    backfill_commits_detailed,
    collect_snapshot,
    collect_worktree,
    missing_commit_objects,
    repository_identity,
    repository_origin_url,
)
from ruleloom.history.git import GitHistoryError, collect_git_history
from ruleloom.history.github import (
    GhApiClient,
    GitHubHistoryError,
    collect_github_history,
    github_repository_from_origin,
)
from ruleloom.history.github_webhooks import ingest_github_capture_directory
from ruleloom.history.importing import import_change_units, import_events
from ruleloom.history.materialize import materialize_history
from ruleloom.history.models import ChangeUnit, HistoricalEvent
from ruleloom.history.outcomes import ATOMIC_OUTCOME_TARGETS
from ruleloom.history.storage import (
    change_units_path,
    events_path,
    load_history_snapshot,
    upsert_history_batch,
)
from ruleloom.history.units import assemble_change_units
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
from ruleloom.manual_rules import (
    audit_manual_rule,
    declare_manual_rule,
    load_manual_rule_manifest,
    manual_candidate_from_audit,
)
from ruleloom.models import (
    JsonObject,
    LabelEvidence,
    LabelValue,
    ModelError,
    Observation,
    content_hash,
    parse_timestamp,
    validate_subject,
)
from ruleloom.onboarding import diagnose_onboarding
from ruleloom.packs import (
    ConfiguredPathsConfig,
    PathPredicateConfig,
    available_packs,
    latest_pack_version,
)
from ruleloom.predicate_audit import audit_predicates
from ruleloom.project import initialize_project, validate_observations, validate_project
from ruleloom.reporting import build_pilot_report, build_pilot_reports
from ruleloom.repository_assertions import (
    audit_repository_assertions,
    declare_repository_assertions,
    load_repository_assertion_declaration,
    load_repository_assertion_manifest,
)
from ruleloom.storage import (
    _file_lock,
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
    project_path,
    save_candidate,
    write_json,
)

_REPOSITORY_ASSERTIONS_PATH = Path(".ruleloom/repository-assertions.json")


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


def _cmd_audit(args: argparse.Namespace) -> int:
    """Produce useful, outcome-blind repository evidence before initialization."""

    if args.path == "":
        raise ModelError("audit path must not be empty")
    root = Path(args.path if args.path is not None else ".").resolve()
    if not root.is_dir():
        raise ModelError(f"audit path is not an existing directory: {root}")
    report = audit_repository(
        root,
        ref=args.ref,
        limits=RepositoryAuditLimits(
            max_commits=args.max_commits,
            max_hotspots=args.max_hotspots,
            max_cochanges=args.max_cochanges,
            min_cochange_count=args.min_cochange_count,
            max_cochange_paths_per_commit=args.max_cochange_paths_per_commit,
            max_pair_updates=args.max_pair_updates,
            max_total_path_entries=args.max_path_entries,
            diff_batch_size=args.diff_batch_size,
        ),
    )
    if args.json:
        _json(report.to_dict())
    else:
        print(report.render_text(), end="")
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


def _require_recorded_git_boundary(
    root: Path,
    repository_id: str,
    boundary: str,
) -> None:
    """Require an exact Git cursor already bound to this canonical ledger."""
    existing_events, _existing_units = load_history_snapshot(
        events_path(root), change_units_path(root)
    )
    recorded = any(
        event.repository_id == repository_id
        and event.provider == "git"
        and event.kind in {"git_commit", "git_merge"}
        and event.source_ref == boundary
        and event.data.get("sha") == boundary
        for event in existing_events
    )
    if not recorded:
        raise ModelError(
            "--after must exactly match the source_ref of an already-recorded Git commit "
            "or merge in this repository's canonical history ledger"
        )


def _cmd_history_bootstrap_git(args: argparse.Namespace) -> int:
    root, config = _project(args)
    _ensure_repository_boundary(root, config)
    if args.after is not None:
        _require_recorded_git_boundary(
            root,
            config.protocol.repository_id,
            args.after,
        )
    maximum = args.max_commits
    if args.all or maximum is None:
        maximum = None
    report = collect_git_history(
        root,
        ref=args.ref,
        max_commits=maximum,
        since=args.since,
        after=args.after,
        repository_id=config.protocol.repository_id,
    )
    event_counts, unit_counts = upsert_history_batch(
        events_path(root),
        report.events,
        change_units_path(root),
        report.units,
    )
    events_inserted, events_unchanged = event_counts
    units_inserted, units_unchanged = unit_counts
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

    existing_events, _existing_units = load_history_snapshot(
        events_path(root), change_units_path(root)
    )
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
    counts = _persist_history_import(root, config, incoming_events, incoming_units)
    _json(
        {
            "events_imported": len(incoming_events),
            "events_inserted": counts["events_inserted"],
            "events_unchanged": counts["events_unchanged"],
            "units_imported": len(imported_units),
            "units_assembled": len(assembled),
            "units_inserted": counts["units_inserted"],
            "units_unchanged": counts["units_unchanged"],
        }
    )
    return 0


def _cmd_history_ingest_github_captures(args: argparse.Namespace) -> int:
    root, config = _project(args)
    _ensure_repository_boundary(root, config)
    variable = args.envelope_key_env
    if (
        not isinstance(variable, str)
        or not variable
        or len(variable) > 128
        or "=" in variable
        or any(ord(character) < 33 or ord(character) > 126 for character in variable)
    ):
        raise ModelError("--envelope-key-env must name one printable environment variable")
    secret = os.environ.get(variable)
    if secret is None or not secret:
        raise ModelError(f"GitHub capture envelope key environment variable is not set: {variable}")
    try:
        envelope_key = secret.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ModelError("GitHub capture envelope key must be valid UTF-8") from exc
    report = ingest_github_capture_directory(
        root,
        Path(args.inbox),
        expected_repository_id=config.protocol.repository_id,
        expected_label_policy_hash=args.expected_label_policy_hash,
        envelope_key=envelope_key,
        max_bundles=args.max_bundles,
    )
    _json(report.to_dict())
    return 0


def _persist_history_import(
    root: Path,
    config: RuleLoomConfig,
    incoming_events: Sequence[HistoricalEvent],
    incoming_units: Sequence[ChangeUnit],
) -> dict[str, int]:
    """Validate one immutable history batch completely before writing either log."""

    _ensure_history_repository(incoming_units, config.protocol.repository_id)
    _ensure_history_repository(incoming_events, config.protocol.repository_id)

    event_counts, unit_counts = upsert_history_batch(
        events_path(root),
        incoming_events,
        change_units_path(root),
        incoming_units,
    )
    return {
        "events_inserted": event_counts[0],
        "events_unchanged": event_counts[1],
        "units_inserted": unit_counts[0],
        "units_unchanged": unit_counts[1],
    }


def _cmd_history_import_github(args: argparse.Namespace) -> int:
    root, config = _project(args)
    _ensure_repository_boundary(root, config)
    try:
        origin = repository_origin_url(root)
    except GitFactsError as exc:
        raise ModelError(f"cannot inspect remote.origin.url: {exc}") from exc
    origin_repository = github_repository_from_origin(origin)
    repository_matches = (
        origin_repository is not None and origin_repository.casefold() == args.repository.casefold()
    )
    if not repository_matches and not args.allow_unverified_repository:
        raise ModelError(
            "--repository is not verifiably equal to this checkout's public-GitHub "
            "remote.origin.url; correct the repository/origin or explicitly pass "
            "--allow-unverified-repository for a reviewed mirror/import"
        )
    repository_binding = "verified_origin" if repository_matches else "explicit_unverified_override"
    report = collect_github_history(
        GhApiClient(),
        args.repository,
        config.protocol.repository_id,
        since=args.since,
        until=args.until,
        max_pull_requests=args.max_pull_requests,
        max_commits_per_pull=args.max_commits_per_pull,
        max_reviews_per_pull=args.max_reviews_per_pull,
        max_checks_per_commit=args.max_checks_per_commit,
        max_repository_commits=args.max_repository_commits,
        max_api_requests=args.max_api_requests,
        max_provider_records=args.max_provider_records,
        repository_binding=repository_binding,
    )
    required_objects = tuple(
        sorted(
            {
                object_id
                for unit in report.units
                for object_id in (unit.base_sha, unit.prediction_sha)
            }
        )
    )
    try:
        missing_objects = missing_commit_objects(root, required_objects)
    except GitFactsError as exc:
        raise ModelError(f"cannot preflight local Git objects: {exc}") from exc
    missing_set = set(missing_objects)
    affected_units = sum(
        unit.base_sha in missing_set or unit.prediction_sha in missing_set for unit in report.units
    )
    local_git_preflight: JsonObject = {
        "required_commit_objects": len(required_objects),
        "available_commit_objects": len(required_objects) - len(missing_objects),
        "missing_commit_objects": len(missing_objects),
        "affected_change_units": affected_units,
        "missing_preview": list(missing_objects[:20]),
        "preview_truncated": len(missing_objects) > 20,
    }
    counts = _persist_history_import(root, config, report.events, report.units)
    _json(
        {
            **report.to_dict(),
            **counts,
            "local_git_preflight": local_git_preflight,
            "note": (
                "Archived GitHub PRs are grouped exploratory units, not exact opening "
                "snapshots. Reviews and checks remain unattributed unless structured "
                "evidence says otherwise. Missing local Git objects must be fetched "
                "explicitly before their units can be materialized. Cutoffs filter this "
                "collection and never delete evidence already present in the append-only "
                "ledger."
            ),
        }
    )
    return 0


def _cmd_history_materialize(args: argparse.Namespace) -> int:
    root, config = _project(args)
    _ensure_repository_boundary(root, config)
    events, units = load_history_snapshot(events_path(root), change_units_path(root))
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
    events, units = load_history_snapshot(events_path(root), change_units_path(root))
    observations = load_observations(dataset_path(root, config))
    historical = [item for item in observations if item.source.get("kind") == "historical_change"]
    qualities: dict[str, int] = {}
    for unit in units:
        qualities[unit.evidence_quality] = qualities.get(unit.evidence_quality, 0) + 1
    event_grades: dict[str, int] = {}
    for event in events:
        grade = event.data.get("evidence_grade")
        if isinstance(grade, str):
            event_grades[grade] = event_grades.get(grade, 0) + 1
    labels = {item.value: 0 for item in LabelValue}
    for observation in historical:
        labels[observation.labels.get(config.target, LabelValue.UNKNOWN).value] += 1
    _json(
        {
            "events": len(events),
            "change_units": len(units),
            "evidence_quality": qualities,
            "event_evidence_grade": event_grades,
            "confirmatory_units": sum(item.confirmatory for item in units),
            "historical_observations": len(historical),
            "labels": labels,
            "language_neutral_core": True,
        }
    )
    return 0


def _cmd_diagnose(args: argparse.Namespace) -> int:
    """Explain the next safe step without mutating evidence or relaxing gates."""

    root, config = _project(args)
    validate_project(root, config)
    events, units = load_history_snapshot(events_path(root), change_units_path(root))
    observations = load_observations(dataset_path(root, config))
    status = readiness(observations, config.target, as_of=datetime.now(UTC))
    qualities: dict[str, int] = {}
    for unit in units:
        qualities[unit.evidence_quality] = qualities.get(unit.evidence_quality, 0) + 1
    configured = (
        tuple(item.predicate for item in config.pack_config.path_predicates)
        if config.pack_config is not None
        else ()
    )
    predicate_report = audit_predicates(
        observations,
        config.resolved_pack.predicates,
        configured_predicates=configured,
    )
    diagnosis = diagnose_onboarding(
        status,
        history_status={
            "events": len(events),
            "change_units": len(units),
            "confirmatory_units": sum(item.confirmatory for item in units),
            "evidence_quality": qualities,
        },
        predicate_audit=predicate_report,
        min_positive_for_shadow=config.promotion.min_positive_for_shadow,
        min_positive_for_approval=config.promotion.min_positive_for_approval,
    )
    if args.json:
        _json(diagnosis.to_dict())
    else:
        print(diagnosis.render_text(), end="")
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


def _cmd_rules_import(args: argparse.Namespace) -> int:
    root, config = _project(args)
    validate_project(root, config)
    manifest = load_manual_rule_manifest(Path(args.manifest))
    instant = datetime.now(UTC)
    declaration = declare_manual_rule(root, config, manifest, declared_at=instant)
    observations = load_observations(dataset_path(root, config))
    validate_observations(observations, config, as_of=instant)
    audit = audit_manual_rule(
        root,
        config,
        declaration,
        observations,
        as_of=instant,
    )
    candidate = manual_candidate_from_audit(declaration, audit, config)
    path = candidate_path(root, config, candidate.id)
    save_candidate(path, candidate)
    if args.json:
        _json(
            {
                "candidate": candidate.to_dict(),
                "declaration": declaration.to_dict(),
                "audit": audit.to_dict(),
                "manifest_path": str(path),
            }
        )
    else:
        print(f"Manual candidate: {candidate.id}")
        print(
            f"Historical coverage: {audit.matched_observations}/{audit.observations} "
            f"({audit.match_rate:.1%})"
        )
        print(
            f"Mature outcomes: {audit.mature_labels} "
            f"({audit.positive} positive, {audit.negative} negative)"
        )
        print(f"Manifest: {path}")
        print(
            "Historical metrics are post-hoc diagnostics. After human review, this rule may "
            "enter shadow; approval requires prospective evidence."
        )
        for warning in audit.warnings:
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
        "github_optional": {
            "ok": shutil.which("gh") is not None,
            "path": shutil.which("gh"),
            "required": False,
            "required_for": "history import-github only",
        },
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


def _cmd_predicates_audit(args: argparse.Namespace) -> int:
    root, config = _project(args)
    observations = load_observations(dataset_path(root, config))
    validate_observations(observations, config, as_of=datetime.now(UTC))
    configured = (
        tuple(item.predicate for item in config.pack_config.path_predicates)
        if config.pack_config is not None
        else ()
    )
    report = audit_predicates(
        observations,
        config.resolved_pack.predicates,
        configured_predicates=configured,
        rare_threshold=args.rare_threshold,
        saturated_threshold=args.saturated_threshold,
        drift_threshold=args.drift_threshold,
        overlap_threshold=args.overlap_threshold,
    )
    payload: JsonObject = {
        "experiment_id": config.protocol.experiment_id,
        "repository_id": config.protocol.repository_id,
        "target": config.target,
        "config_hash": config.hash,
        "evidence_protocol_hash": config.evidence_protocol_hash,
        "pack": config.pack,
        "pack_version": config.pack_version,
        "pack_config_hash": config.pack_config_hash,
        **report,
    }
    payload["audit_manifest_hash"] = content_hash(payload)
    _json(payload)
    return 0


def _cmd_assertions_declare(args: argparse.Namespace) -> int:
    root, config = _project(args)
    _ensure_repository_boundary(root, config)
    manifest = load_repository_assertion_manifest(Path(args.manifest))
    declared_at = None if args.declared_at is None else parse_timestamp(args.declared_at)
    declaration = declare_repository_assertions(
        root,
        manifest,
        repository_id=config.protocol.repository_id,
        protocol_hash=config.evidence_protocol_hash,
        predicate_vocabulary=tuple(sorted(config.resolved_pack.predicates)),
        declared_at=declared_at,
    )
    destination = project_path(root, _REPOSITORY_ASSERTIONS_PATH)
    with _file_lock(destination):
        if destination.exists() or destination.is_symlink():
            raise ModelError(
                f"refusing to overwrite repository assertion declaration: {destination}"
            )
        write_json(destination, declaration.to_dict())
    _json(
        {
            "declaration_id": declaration.id,
            "manifest_hash": declaration.manifest_hash,
            "assertions": len(declaration.manifest.assertions),
            "destination": str(_REPOSITORY_ASSERTIONS_PATH),
            "outcome_blind": True,
        }
    )
    return 0


def _cmd_assertions_audit(args: argparse.Namespace) -> int:
    root, config = _project(args)
    _ensure_repository_boundary(root, config)
    declaration = load_repository_assertion_declaration(
        project_path(root, _REPOSITORY_ASSERTIONS_PATH)
    )
    observations = load_observations(dataset_path(root, config))
    validate_observations(observations, config, as_of=datetime.now(UTC))
    report = audit_repository_assertions(root, declaration, observations)
    if args.json:
        _json(report.to_dict())
    else:
        print(report.render_text(), end="")
    return 0


def _cmd_mcp_serve(args: argparse.Namespace) -> int:
    """Run the optional official-SDK MCP server over local stdio only."""

    root = _root(args)
    from ruleloom.mcp_sdk import serve_sdk_stdio

    serve_sdk_stdio(root)
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

    audit = subparsers.add_parser(
        "audit",
        help="get an outcome-blind repository structure report without initializing RuleLoom",
    )
    audit.add_argument("path", nargs="?", default=".")
    audit.add_argument("--ref", default="HEAD")
    audit.add_argument("--max-commits", type=int, default=500)
    audit.add_argument("--max-hotspots", type=int, default=25)
    audit.add_argument("--max-cochanges", type=int, default=50)
    audit.add_argument("--min-cochange-count", type=int, default=2)
    audit.add_argument("--max-cochange-paths-per-commit", type=int, default=200)
    audit.add_argument("--max-pair-updates", type=int, default=2_000_000)
    audit.add_argument("--max-path-entries", type=int, default=1_000_000)
    audit.add_argument(
        "--diff-batch-size",
        type=int,
        default=128,
        help="commits per native Git diff batch (lower this for unusually large changes)",
    )
    audit.add_argument("--json", action="store_true")
    audit.set_defaults(handler=_cmd_audit)

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

    predicates = subparsers.add_parser(
        "predicates", help="audit the frozen predicate vocabulary without reading outcomes"
    )
    _add_root(predicates)
    predicate_commands = predicates.add_subparsers(dest="predicates_command", required=True)
    predicate_audit = predicate_commands.add_parser(
        "audit", help="report prevalence, path examples, overlap, and temporal drift"
    )
    predicate_audit.add_argument("--rare-threshold", type=float, default=0.01)
    predicate_audit.add_argument("--saturated-threshold", type=float, default=0.99)
    predicate_audit.add_argument("--drift-threshold", type=float, default=0.20)
    predicate_audit.add_argument("--overlap-threshold", type=float, default=0.90)
    predicate_audit.set_defaults(handler=_cmd_predicates_audit)

    assertions = subparsers.add_parser(
        "assertions",
        help="freeze and audit explicit repository conventions without interpreting prose",
    )
    _add_root(assertions)
    assertion_commands = assertions.add_subparsers(dest="assertions_command", required=True)
    assertions_declare = assertion_commands.add_parser(
        "declare",
        help="bind a strict assertion manifest to repository sources and predicate vocabulary",
    )
    assertions_declare.add_argument("manifest")
    assertions_declare.add_argument(
        "--declared-at",
        help="aware ISO-8601 declaration time for reproducible automation",
    )
    assertions_declare.set_defaults(handler=_cmd_assertions_declare)
    assertions_audit = assertion_commands.add_parser(
        "audit",
        help="report structural adherence and exceptions without consulting outcomes",
    )
    assertions_audit.add_argument("--json", action="store_true")
    assertions_audit.set_defaults(handler=_cmd_assertions_audit)

    mcp = subparsers.add_parser(
        "mcp",
        help="serve approved-only agent guidance through the optional official MCP SDK",
    )
    mcp_commands = mcp.add_subparsers(dest="mcp_command", required=True)
    mcp_serve = mcp_commands.add_parser(
        "serve",
        help="serve the initialized repository over local stdio",
    )
    _add_root(mcp_serve)
    mcp_serve.set_defaults(handler=_cmd_mcp_serve)

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
    bootstrap_window = bootstrap_git.add_mutually_exclusive_group()
    bootstrap_window.add_argument(
        "--since",
        help="optional aware ISO-8601 lower timestamp bound (not valid with --after)",
    )
    bootstrap_window.add_argument(
        "--after",
        help=(
            "ingest the complete range after this exact already-recorded Git source_ref; "
            "divergence or truncation fails closed"
        ),
    )
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

    github_capture_ingest = history_commands.add_parser(
        "ingest-github-captures",
        help="verify and atomically ingest a bounded point-in-time capture inbox",
    )
    github_capture_ingest.add_argument("inbox", help="owner-only bundle directory")
    github_capture_ingest.add_argument(
        "--envelope-key-env",
        default="RULELOOM_GITHUB_ENVELOPE_KEY",
        help=(
            "environment variable containing the capture HMAC key "
            "(default: RULELOOM_GITHUB_ENVELOPE_KEY)"
        ),
    )
    github_capture_ingest.add_argument(
        "--expected-label-policy-hash",
        required=True,
        help=("reviewed lowercase SHA-256 policy pin supplied independently of capture bundles"),
    )
    github_capture_ingest.add_argument("--max-bundles", type=int, default=1_000)
    github_capture_ingest.set_defaults(handler=_cmd_history_ingest_github_captures)

    github_import = history_commands.add_parser(
        "import-github",
        help="collect bounded PR, review, check, and revert archive evidence via gh",
    )
    github_import.add_argument(
        "--repository",
        required=True,
        metavar="OWNER/NAME",
        help=(
            "GitHub repository to import; normalized records are bound to this initialized "
            "checkout's verified RuleLoom repository identity"
        ),
    )
    github_import.add_argument("--since", help="optional aware ISO-8601 PR lower bound")
    github_import.add_argument("--until", help="optional aware ISO-8601 upper bound")
    github_import.add_argument(
        "--allow-unverified-repository",
        action="store_true",
        help=(
            "allow an explicitly reviewed repository whose OWNER/NAME cannot be verified "
            "against public-GitHub remote.origin.url; recorded in the manifest"
        ),
    )
    github_import.add_argument("--max-pull-requests", type=int, default=1000)
    github_import.add_argument("--max-commits-per-pull", type=int, default=1000)
    github_import.add_argument("--max-reviews-per-pull", type=int, default=1000)
    github_import.add_argument("--max-checks-per-commit", type=int, default=1000)
    github_import.add_argument("--max-repository-commits", type=int, default=10000)
    github_import.add_argument(
        "--max-api-requests",
        type=int,
        default=20000,
        help="global request budget; exhaustion aborts without persisting any records",
    )
    github_import.add_argument(
        "--max-provider-records",
        type=int,
        default=250000,
        help="global top-level provider-record budget; exhaustion aborts without persistence",
    )
    github_import.set_defaults(handler=_cmd_history_import_github)

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

    diagnose = subparsers.add_parser(
        "diagnose",
        help="explain evidence readiness and the next safe onboarding actions",
    )
    _add_root(diagnose)
    diagnose.add_argument("--json", action="store_true")
    diagnose.set_defaults(handler=_cmd_diagnose)

    readiness = subparsers.add_parser("readiness", help="measure data readiness")
    _add_root(readiness)
    readiness.set_defaults(handler=_cmd_readiness)

    validate = subparsers.add_parser("validate", help="validate config, schema, and time order")
    _add_root(validate)
    validate.set_defaults(handler=_cmd_validate)

    rules = subparsers.add_parser(
        "rules",
        help="declare and audit explicit hand-authored Horn rules",
    )
    _add_root(rules)
    rule_commands = rules.add_subparsers(dest="rules_command", required=True)
    rules_import = rule_commands.add_parser(
        "import",
        help="freeze a JSON rule manifest and audit its historical coverage",
    )
    rules_import.add_argument("manifest")
    rules_import.add_argument("--json", action="store_true")
    rules_import.set_defaults(handler=_cmd_rules_import)

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
    except (
        ModelError,
        GitFactsError,
        GitHistoryError,
        GitHubHistoryError,
        FirstHourAuditError,
        PopperError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
