"""Command-line interface for the end-to-end RuleLoom workflow."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ruleloom import __version__
from ruleloom.agents import sync_agents
from ruleloom.config import CONFIG_PATH, RuleLoomConfig, discover_root
from ruleloom.gitfacts import (
    GitFactsError,
    backfill_commits,
    collect_snapshot,
    collect_worktree,
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
    return discover_root(Path(configured) if configured else None)


def _project(args: argparse.Namespace) -> tuple[Path, RuleLoomConfig]:
    root = _root(args)
    return root, RuleLoomConfig.load(root)


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
    selected = {
        "none": (),
        "all": ("codex", "claude"),
        "codex": ("codex",),
        "claude": ("claude",),
    }[args.agents]
    result = initialize_project(Path(args.path), args.project, agents=selected)
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


def _cmd_collect_git(args: argparse.Namespace) -> int:
    root, config = _project(args)
    if args.last is not None:
        observations = backfill_commits(
            root,
            args.last,
            protocol_hash=config.evidence_protocol_hash,
            target=config.target,
            ref=args.ref,
            pack=config.pack,
            repository_id=config.protocol.repository_id,
        )
    elif args.working_tree:
        observations = [
            collect_worktree(
                root,
                args.ref,
                protocol_hash=config.evidence_protocol_hash,
                target=config.target,
                pack=config.pack,
                repository_id=config.protocol.repository_id,
            )
        ]
    else:
        if args.base is None:
            raise ModelError("collect git requires --last or --base")
        observations = [
            collect_snapshot(
                root,
                args.base,
                args.head,
                protocol_hash=config.evidence_protocol_hash,
                target=config.target,
                pack=config.pack,
                repository_id=config.protocol.repository_id,
            )
        ]
    inserted, updated, observations = _persist_collected(dataset_path(root, config), observations)
    _json(
        {
            "collected": len(observations),
            "inserted": inserted,
            "updated": updated,
            "ids": [item.id for item in observations],
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
            target=args.target or config.target,
            value=LabelValue(args.value),
            kind=args.kind,
            source=args.source,
            available_at=args.available_at or utc_now(as_of),
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
    if config.learner.engine == "popper":
        _external_popper_checkout(root, config)
    observations = load_observations(dataset_path(root, config))
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
    source_keys = {"kind", "repository", "base", "head", "pack", "extractor"}
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
        merged = [_merge_collected_observation(existing, item) for item in collected]
        by_id = {item.id: item for item in existing}
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
        root, config = _project(args)
    except ModelError as exc:
        checks["project"] = {"ok": False, "detail": str(exc)}
        config = None
    else:
        checks["project"] = {"ok": True, "root": str(root), "config": str(root / CONFIG_PATH)}
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
    init.add_argument("path", nargs="?", default=".")
    init.add_argument("--project")
    init.add_argument(
        "--agents",
        choices=["none", "all", "codex", "claude"],
        default="none",
        help="install agent skills now (default none for shadow-mode isolation)",
    )
    init.set_defaults(handler=_cmd_init)

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
    git.add_argument("--head", default="HEAD")
    git.add_argument("--ref", default="HEAD", help="history ref used with --last")
    git.set_defaults(handler=_cmd_collect_git)

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
    except (ModelError, GitFactsError, PopperError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
