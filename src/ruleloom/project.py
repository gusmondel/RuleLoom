"""Project initialization and dataset validation."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ruleloom.agents import SyncResult, sync_agents
from ruleloom.config import CONFIG_PATH, RuleLoomConfig, default_config
from ruleloom.gitfacts import GitFactsError, repository_identity
from ruleloom.history.materialize import resolve_git_window, validate_materialized_outcome
from ruleloom.history.models import HistoricalEvent, validate_git_sha
from ruleloom.history.storage import (
    change_units_path,
    events_path,
    load_history_snapshot,
)
from ruleloom.history.units import (
    validate_change_unit_event_links,
    validate_change_unit_evidence,
    validate_unique_event_ownership,
)
from ruleloom.lifecycle import Readiness, readiness
from ruleloom.models import ModelError, Observation, parse_timestamp
from ruleloom.packs import (
    ConfiguredPathsConfig,
    matches_pack_version,
    validate_persisted_extraction,
)
from ruleloom.storage import (
    dataset_path,
    load_approved,
    load_candidates,
    load_observations,
    load_shadow,
    load_trusted_predictions,
    predictions_path,
    project_path,
    trusted_state_path,
    write_json,
    write_text,
)


@dataclass(frozen=True, slots=True)
class InitResult:
    root: Path
    config: RuleLoomConfig
    agent_files: tuple[SyncResult, ...]


def initialize_project(
    root: Path,
    project: str | None = None,
    *,
    target: str = "needs_extra_validation",
    outcome_definition: str | None = None,
    pack: str | None = None,
    pack_version: int | None = None,
    pack_config: ConfiguredPathsConfig | None = None,
    schema_version: int = 5,
    agents: Sequence[str] = (),
    git_window_days: int | None = None,
) -> InitResult:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    config_path = project_path(root, CONFIG_PATH)
    try:
        repository_id = repository_identity(root)
    except GitFactsError as exc:
        raise ModelError(f"RuleLoom init requires an initialized Git repository: {exc}") from exc
    config = default_config(
        project if project is not None else root.name,
        repository_id=repository_id,
        target=target,
        outcome_definition=outcome_definition,
        pack=pack,
        pack_version=pack_version,
        pack_config=pack_config,
        schema_version=schema_version,
        test_start_at=(
            datetime.now(UTC).isoformat().replace("+00:00", "Z") if schema_version >= 4 else None
        ),
        git_window_days=git_window_days,
    )
    managed_paths = [
        config_path,
        dataset_path(root, config),
        predictions_path(root, config),
        project_path(root, ".ruleloom/README.md"),
        project_path(root, config.candidates_dir),
        project_path(root, config.shadow_dir),
        project_path(root, config.approved_dir),
        project_path(root, config.deprecated_dir),
        project_path(root, ".ruleloom/signal-probes"),
        events_path(root),
        change_units_path(root),
    ]
    agent_relative_paths = {
        "codex": (
            ".agents/skills/ruleloom/SKILL.md",
            ".agents/skills/ruleloom/references/approved-rules.md",
        ),
        "claude": (
            ".claude/skills/ruleloom/SKILL.md",
            ".claude/skills/ruleloom/references/approved-rules.md",
        ),
    }
    for agent in agents:
        if agent not in agent_relative_paths:
            raise ModelError(f"unsupported agent: {agent}")
        managed_paths.extend(
            project_path(root, relative) for relative in agent_relative_paths[agent]
        )
    try:
        trusted_path = trusted_state_path(root)
    except ModelError:
        trusted_path = None
    if trusted_path is not None:
        managed_paths.append(trusted_path)
    conflicts = [path for path in managed_paths if path.exists()]
    if conflicts:
        rendered = ", ".join(str(path) for path in conflicts)
        raise ModelError(f"refusing to overwrite existing RuleLoom artifacts: {rendered}")
    write_json(config_path, config.to_dict())
    write_text(dataset_path(root, config), "")
    write_text(predictions_path(root, config), "")
    write_text(events_path(root), "")
    write_text(change_units_path(root), "")
    for directory in (
        config.candidates_dir,
        config.shadow_dir,
        config.approved_dir,
        config.deprecated_dir,
        ".ruleloom/signal-probes",
    ):
        project_path(root, directory).mkdir(parents=True, exist_ok=True)
    write_text(
        project_path(root, ".ruleloom/README.md"),
        """# RuleLoom project data

Commit `config.json`, `observations.jsonl`, normalized `history/` records, candidate manifests,
and reviewed shadow/approved policies when repository policy permits. They form the
reproducibility and provenance trail.

Do not use CI or review outcomes as prediction-time facts. Record them later as labels with
`label_evidence.available_at` provenance. Git-only and final-state historical units are
exploratory; only independently sourced point-in-time units can be confirmatory. Generated
caches and external learner checkouts are ignored.
""",
    )
    agent_files = tuple(sync_agents(root, config, agents=agents)) if agents else ()
    return InitResult(root=root, config=config, agent_files=agent_files)


def validate_observations(
    observations: list[Observation],
    config: RuleLoomConfig,
    *,
    as_of: datetime | None = None,
) -> Readiness:
    """Run cross-record checks after per-record schema validation."""
    identifiers = [item.id for item in observations]
    if len(identifiers) != len(set(identifiers)):
        raise ModelError("observation ids must be unique")
    descriptor = config.resolved_pack
    expected_protocol_hash = config.evidence_protocol_hash
    for item in observations:
        if item.protocol_hash != expected_protocol_hash:
            raise ModelError(
                f"observation {item.id!r} belongs to a different evidence protocol; "
                "start a new experiment dataset instead of reinterpreting its labels"
            )
        kind = item.source.get("kind")
        if kind not in {"git_commit", "git_range", "git_worktree", "historical_change"}:
            raise ModelError(f"observation {item.id!r} lacks a supported source.kind")
        if item.source.get("repository") != config.protocol.repository_id:
            raise ModelError(f"observation {item.id!r} belongs to a different repository")
        if item.source.get("pack") != config.pack:
            raise ModelError(f"observation {item.id!r} uses a different fact pack")
        source_pack_version = item.source.get("pack_version")
        if (
            config.schema_version >= 2 or source_pack_version is not None
        ) and not matches_pack_version(source_pack_version, config.pack_version):
            raise ModelError(f"observation {item.id!r} uses a different fact pack version")
        extractor = item.source.get("extractor")
        if extractor != descriptor.extractor:
            raise ModelError(
                f"observation {item.id!r} extractor provenance {extractor!r} does not match "
                f"configured extractor {descriptor.extractor!r}"
            )
        source_configuration = item.source.get("pack_config_hash")
        if (
            descriptor.configuration_hash is not None
            and source_configuration != descriptor.configuration_hash
        ):
            raise ModelError(f"observation {item.id!r} uses a different pack configuration")
        if descriptor.configuration_hash is None and source_configuration is not None:
            raise ModelError(
                f"observation {item.id!r} has unexpected pack-configuration provenance"
            )
        validate_persisted_extraction(
            descriptor,
            item.facts,
            item.fact_evidence,
            subject=f"observation {item.id!r}",
            metadata=item.metadata,
        )
        evidence = item.label_evidence.get(config.target)
        if evidence is not None and parse_timestamp(evidence.available_at) <= parse_timestamp(
            item.observed_at
        ):
            raise ModelError(
                f"label for {item.id} must become available after observation time: "
                f"{evidence.available_at} <= {item.observed_at}"
            )
    return readiness(observations, config.target, as_of=as_of)


def validate_project(root: Path, config: RuleLoomConfig) -> Readiness:
    as_of = datetime.now(UTC)
    try:
        actual_repository = repository_identity(root)
    except GitFactsError as exc:
        raise ModelError(f"cannot verify configured Git repository: {exc}") from exc
    if actual_repository != config.protocol.repository_id:
        raise ModelError(
            f"configured repository id {config.protocol.repository_id!r} does not match "
            f"this checkout {actual_repository!r}"
        )
    observations = load_observations(dataset_path(root, config))
    report = validate_observations(observations, config, as_of=as_of)
    events, units = load_history_snapshot(events_path(root), change_units_path(root))
    for event in events:
        if event.repository_id != config.protocol.repository_id:
            raise ModelError(f"historical event {event.id!r} belongs to a different repository")
    events_by_id = {event.id: event for event in events}
    events_by_change: dict[tuple[str, str], list[HistoricalEvent]] = defaultdict(list)
    for event in events:
        if event.change_id is not None:
            events_by_change[(event.repository_id, event.change_id)].append(event)
    units_by_id = {unit.id: unit for unit in units}
    git_window = resolve_git_window(config, events)
    validate_unique_event_ownership(units)
    for unit in units:
        if unit.repository_id != config.protocol.repository_id:
            raise ModelError(f"change unit {unit.id!r} belongs to a different repository")
        validate_change_unit_event_links(unit, events_by_id)
        linked = {
            event.id: event for event in events_by_change.get((unit.repository_id, unit.id), ())
        }
        for event_id in unit.event_ids:
            attached_event = events_by_id[event_id]
            if attached_event.change_id is None:
                linked[attached_event.id] = attached_event
        validate_change_unit_evidence(unit, list(linked.values()))
    for observation in observations:
        if observation.source.get("kind") != "historical_change":
            continue
        change_id = observation.source.get("change_id")
        if not isinstance(change_id, str) or change_id not in units_by_id:
            raise ModelError(
                f"historical observation {observation.id!r} lacks a persisted change unit"
            )
        unit = units_by_id[change_id]
        source_base = observation.source.get("base")
        provider_base = observation.source.get("provider_base")
        diff_base_kind = observation.source.get("diff_base_kind")
        merge_base_matches = False
        if (
            diff_base_kind == "merge_base"
            and provider_base == unit.base_sha
            and isinstance(source_base, str)
        ):
            try:
                validate_git_sha(source_base, field_name="historical observation merge base")
            except ModelError:
                pass
            else:
                merge_base_matches = True
        direct_base_matches = (
            source_base == unit.base_sha and provider_base is None and diff_base_kind is None
        )
        if (
            observation.id != f"history.{unit.id}"
            or observation.observed_at != unit.prediction_at
            or not (direct_base_matches or merge_base_matches)
            or observation.source.get("head") != unit.prediction_sha
            or observation.source.get("unit_kind") != unit.kind
            or observation.source.get("provider") != unit.provider
            or observation.source.get("source_ref") != unit.source_ref
            or observation.source.get("evidence_quality") != unit.evidence_quality
        ):
            raise ModelError(
                f"historical observation {observation.id!r} conflicts with its change unit"
            )
        source_confirmatory = observation.source.get("confirmatory")
        if source_confirmatory is True and not unit.confirmatory:
            raise ModelError(
                f"historical observation {observation.id!r} overstates confirmatory evidence"
            )
        linked = {
            event.id: event for event in events_by_change.get((unit.repository_id, unit.id), ())
        }
        for event_id in unit.event_ids:
            attached_event = events_by_id[event_id]
            if attached_event.change_id is None:
                linked[attached_event.id] = attached_event
        validate_materialized_outcome(
            config,
            observation,
            unit,
            list(linked.values()),
            git_window=git_window,
        )
    load_candidates(root, config)
    load_shadow(root, config)
    load_approved(root, config)
    load_trusted_predictions(root, config)
    return report
