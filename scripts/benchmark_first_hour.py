#!/usr/bin/env python3
"""Reproducible, read-only benchmark for first-hour Git diff batching."""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from ruleloom import __version__
from ruleloom.first_hour import (
    FIRST_HOUR_REPORT_ENGINE_VERSION,
    FirstHourReport,
    RepositoryAuditLimits,
    audit_repository,
)

_MAX_COMMITS = 10_000
_MAX_BATCH_SIZE = 512
_MAX_REPEATS = 20


@dataclass(frozen=True, slots=True)
class _Sample:
    batch_size: int
    seconds: float
    report: FirstHourReport


def _bounded_int(raw: str, *, name: str, minimum: int, maximum: int) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise argparse.ArgumentTypeError(f"{name} must be between {minimum} and {maximum}")
    return value


def _commit_count(raw: str) -> int:
    return _bounded_int(raw, name="max commits", minimum=1, maximum=_MAX_COMMITS)


def _batch_size(raw: str) -> int:
    # Batch size 1 is the benchmark baseline and must remain a distinct bucket.
    return _bounded_int(raw, name="batch size", minimum=2, maximum=_MAX_BATCH_SIZE)


def _repeat_count(raw: str) -> int:
    # One run cannot alternate execution order and is especially cache-sensitive.
    return _bounded_int(raw, name="repeats", minimum=2, maximum=_MAX_REPEATS)


def _measure(root: Path, *, max_commits: int, batch_size: int) -> _Sample:
    started = time.perf_counter()
    report = audit_repository(
        root,
        limits=RepositoryAuditLimits(
            max_commits=max_commits,
            diff_batch_size=batch_size,
        ),
    )
    return _Sample(batch_size, time.perf_counter() - started, report)


def _git_version() -> str:
    completed = subprocess.run(
        ("git", "--version"),
        check=True,
        capture_output=True,
        text=True,
        shell=False,
    )
    return completed.stdout.strip()


def _normalized_structural_report(report: FirstHourReport) -> str:
    """Remove transport-only fields before checking complete report parity."""
    payload = dict(report.to_dict())
    payload.pop("manifest_hash", None)
    raw_limits = payload.get("limits")
    if not isinstance(raw_limits, dict):
        raise RuntimeError("first-hour report contains malformed limits")
    normalized_limits = dict(raw_limits)
    normalized_limits.pop("diff_batch_size", None)
    payload["limits"] = normalized_limits
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compare one diff-tree process per commit with RuleLoom's bounded batch transport. "
            "The target checkout is never modified."
        )
    )
    parser.add_argument("repository", type=Path)
    parser.add_argument("--max-commits", type=_commit_count, default=500)
    parser.add_argument("--batch-size", type=_batch_size, default=128)
    parser.add_argument("--repeats", type=_repeat_count, default=3)
    args = parser.parse_args()

    samples: dict[int, list[_Sample]] = {1: [], args.batch_size: []}
    for index in range(args.repeats):
        order = (1, args.batch_size) if index % 2 == 0 else (args.batch_size, 1)
        for batch_size in order:
            samples[batch_size].append(
                _measure(
                    args.repository,
                    max_commits=args.max_commits,
                    batch_size=batch_size,
                )
            )

    baseline = samples[1]
    batched = samples[args.batch_size]
    reports = [item.report for values in samples.values() for item in values]
    evidence_hashes = {item.evidence_manifest_hash for item in reports}
    volumes = {json.dumps(item.volume, sort_keys=True) for item in reports}
    structural_reports = {_normalized_structural_report(item) for item in reports}
    baseline_median = statistics.median(item.seconds for item in baseline)
    batched_median = statistics.median(item.seconds for item in batched)
    exemplar = reports[0]
    result = {
        "schema_version": 1,
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "git": _git_version(),
            "ruleloom": __version__,
            "first_hour_engine": FIRST_HOUR_REPORT_ENGINE_VERSION,
        },
        "selection": {
            "requested_max_commits": args.max_commits,
            "examined_commits": exemplar.topology["commit_count"],
            "resolved_ref": exemplar.topology["resolved_ref"],
        },
        "baseline": {
            "diff_batch_size": 1,
            "seconds": [round(item.seconds, 6) for item in baseline],
            "median_seconds": round(baseline_median, 6),
        },
        "batched": {
            "diff_batch_size": args.batch_size,
            "seconds": [round(item.seconds, 6) for item in batched],
            "median_seconds": round(batched_median, 6),
        },
        "speedup": round(baseline_median / batched_median, 6),
        "equivalence": {
            "evidence_manifest_hash_equal": len(evidence_hashes) == 1,
            "volume_equal": len(volumes) == 1,
            "normalized_structural_report_equal": len(structural_reports) == 1,
            "evidence_manifest_hash": sorted(evidence_hashes),
        },
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    equivalent = len(evidence_hashes) == 1 and len(volumes) == 1 and len(structural_reports) == 1
    return 0 if equivalent else 2


if __name__ == "__main__":
    raise SystemExit(main())
