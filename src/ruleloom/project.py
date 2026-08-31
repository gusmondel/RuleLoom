"""Project initialization and dataset validation."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ruleloom.agents import SyncResult, sync_agents
from ruleloom.config import CONFIG_PATH, RuleLoomConfig, default_config
from ruleloom.gitfacts import GitFactsError, repository_identity
from ruleloom.lifecycle import Readiness, readiness
from ruleloom.models import ModelError, Observation, parse_timestamp
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
    agents: Sequence[str] = (),
) -> InitResult:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    config_path = project_path(root, CONFIG_PATH)
    try:
        repository_id = repository_identity(root)
    except GitFactsError as exc:
        raise ModelError(f"RuleLoom init requires an initialized Git repository: {exc}") from exc
    config = default_config(project or root.name, repository_id=repository_id)
    managed_paths = [
        config_path,
        dataset_path(root, config),
        predictions_path(root, config),
        project_path(root, ".ruleloom/README.md"),
        project_path(root, config.candidates_dir),
        project_path(root, config.shadow_dir),
        project_path(root, config.approved_dir),
        project_path(root, config.deprecated_dir),
    ]
    agent_paths = {
        "codex": (
            project_path(root, ".agents/skills/ruleloom/SKILL.md"),
            project_path(root, ".agents/skills/ruleloom/references/approved-rules.md"),
        ),
        "claude": (
            project_path(root, ".claude/skills/ruleloom/SKILL.md"),
            project_path(root, ".claude/skills/ruleloom/references/approved-rules.md"),
        ),
    }
    for agent in agents:
        if agent not in agent_paths:
            raise ModelError(f"unsupported agent: {agent}")
        managed_paths.extend(agent_paths[agent])
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
    for directory in (
        config.candidates_dir,
        config.shadow_dir,
        config.approved_dir,
        config.deprecated_dir,
    ):
        project_path(root, directory).mkdir(parents=True, exist_ok=True)
    write_text(
        project_path(root, ".ruleloom/README.md"),
        """# RuleLoom project data

Commit `config.json`, `observations.jsonl`, candidate manifests, and reviewed shadow/approved
policies when repository policy permits. They form the reproducibility and provenance trail.

Do not use CI or review outcomes as prediction-time facts. Record them later as labels with
`label_evidence.available_at` provenance. Generated caches and external learner checkouts are
ignored.
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
    for item in observations:
        if item.protocol_hash != config.evidence_protocol_hash:
            raise ModelError(
                f"observation {item.id!r} belongs to a different evidence protocol; "
                "start a new experiment dataset instead of reinterpreting its labels"
            )
        kind = item.source.get("kind")
        if kind not in {"git_commit", "git_range", "git_worktree"}:
            raise ModelError(f"observation {item.id!r} lacks a supported source.kind")
        if item.source.get("repository") != config.protocol.repository_id:
            raise ModelError(f"observation {item.id!r} belongs to a different repository")
        if item.source.get("pack") != config.pack:
            raise ModelError(f"observation {item.id!r} uses a different fact pack")
        extractor = item.source.get("extractor")
        if not isinstance(extractor, str) or not extractor:
            raise ModelError(f"observation {item.id!r} lacks extractor provenance")
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
    report = validate_observations(
        load_observations(dataset_path(root, config)), config, as_of=as_of
    )
    load_candidates(root, config)
    load_shadow(root, config)
    load_approved(root, config)
    load_trusted_predictions(root, config)
    return report
