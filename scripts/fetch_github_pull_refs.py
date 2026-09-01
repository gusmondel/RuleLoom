#!/usr/bin/env python3
"""Fetch public GitHub pull-request heads needed by imported RuleLoom history.

GitHub does not permit arbitrary fetches of every historical object ID. Pull
request head refs are public, however, and often retain an opening SHA as an
ancestor. This helper fetches only those refs, without checking out or running
repository code, and reports which exact prediction-time commit objects became
available. Missing objects remain missing and RuleLoom will abstain.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from ruleloom.gitfacts import GitFactsError, missing_commit_objects, repository_origin_url
from ruleloom.history.storage import load_change_units
from ruleloom.models import JsonObject, canonical_json

_SOURCE_REF_RE = re.compile(r"^github-event-archive:[^:]+:pull:([1-9][0-9]*)$")
_HTTPS_ORIGIN_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?$")
_SSH_ORIGIN_RE = re.compile(
    r"^(?:ssh://git@github\.com/|git@github\.com:)([^/]+)/([^/]+?)(?:\.git)?$"
)
_MAX_REFS = 50_000
_MAX_OUTPUT_BYTES = 4 * 1024 * 1024


class PullRefFetchError(RuntimeError):
    """Raised when public pull refs cannot be fetched safely."""


@dataclass(frozen=True, slots=True)
class PullRefFetchReport:
    repository: str
    units_examined: int
    refs_requested: int
    refs_fetched: int
    refs_unavailable: tuple[int, ...]
    required_objects: int
    objects_missing_before: int
    objects_recovered: int
    objects_missing_after: int
    units_recovered: int
    units_ready_after: int

    def to_dict(self) -> JsonObject:
        return {
            "repository": self.repository,
            "units_examined": self.units_examined,
            "refs_requested": self.refs_requested,
            "refs_fetched": self.refs_fetched,
            "refs_unavailable": list(self.refs_unavailable),
            "required_objects": self.required_objects,
            "objects_missing_before": self.objects_missing_before,
            "objects_recovered": self.objects_recovered,
            "objects_missing_after": self.objects_missing_after,
            "units_recovered": self.units_recovered,
            "units_ready_after": self.units_ready_after,
        }


def _repository(value: str) -> str:
    if value.count("/") != 1 or any(part in {"", ".", ".."} for part in value.split("/")):
        raise argparse.ArgumentTypeError("repository must be OWNER/NAME")
    if any(character in value for character in "\x00\r\n\\"):
        raise argparse.ArgumentTypeError("repository contains an invalid character")
    return value


def _origin_repository(origin: str) -> str | None:
    parsed = urlsplit(origin)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme != "https" or parsed.hostname != "github.com":
            return None
        match = _HTTPS_ORIGIN_RE.fullmatch(origin.rstrip("/"))
    else:
        match = _SSH_ORIGIN_RE.fullmatch(origin)
    if match is None:
        return None
    return f"{match.group(1)}/{match.group(2)}"


def _run_fetch(root: Path, numbers: tuple[int, ...], *, timeout: float) -> None:
    refs = [f"refs/pull/{number}/head" for number in numbers]
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "",
            "GCM_INTERACTIVE": "never",
        }
    )
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "fetch",
                "--no-tags",
                "--no-write-fetch-head",
                "--filter=blob:none",
                "origin",
                *refs,
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=timeout,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PullRefFetchError(f"git fetch failed before completion: {exc}") from exc
    if len(result.stdout) > _MAX_OUTPUT_BYTES or len(result.stderr) > _MAX_OUTPUT_BYTES:
        raise PullRefFetchError("git fetch output exceeded 4194304 bytes")
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise PullRefFetchError(detail or "git fetch returned a non-zero status")


def _fetch_resilient(
    root: Path,
    numbers: tuple[int, ...],
    *,
    timeout: float,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Bisect failed batches so one absent ref cannot hide available refs."""

    if not numbers:
        return (), ()
    try:
        _run_fetch(root, numbers, timeout=timeout)
    except PullRefFetchError:
        if len(numbers) == 1:
            return (), numbers
        middle = len(numbers) // 2
        left_ok, left_missing = _fetch_resilient(root, numbers[:middle], timeout=timeout)
        right_ok, right_missing = _fetch_resilient(root, numbers[middle:], timeout=timeout)
        return left_ok + right_ok, left_missing + right_missing
    return numbers, ()


def fetch_pull_refs(
    root: Path,
    repository: str,
    *,
    batch_size: int = 64,
    timeout: float = 180.0,
    max_refs: int = _MAX_REFS,
) -> PullRefFetchReport:
    resolved = root.resolve()
    origin = repository_origin_url(resolved)
    if origin is None or _origin_repository(origin) != repository:
        raise PullRefFetchError(
            f"origin must be the public GitHub repository {repository!r}; found {origin!r}"
        )
    units = load_change_units(resolved / ".ruleloom" / "history" / "change-units.jsonl")
    number_by_unit: dict[str, int] = {}
    for unit in units:
        match = _SOURCE_REF_RE.fullmatch(unit.source_ref)
        if match is None:
            raise PullRefFetchError(
                "all change units must come from the GitHub event-archive adapter"
            )
        number_by_unit[unit.id] = int(match.group(1))
    numbers = tuple(sorted(set(number_by_unit.values())))
    if len(numbers) > max_refs or len(numbers) > _MAX_REFS:
        raise PullRefFetchError(
            f"ref request contains {len(numbers)} pull requests; "
            f"limit is {min(max_refs, _MAX_REFS)}"
        )
    required = tuple(
        sorted({object_id for unit in units for object_id in (unit.base_sha, unit.prediction_sha)})
    )
    try:
        missing_before = frozenset(missing_commit_objects(resolved, required))
    except GitFactsError as exc:
        raise PullRefFetchError(str(exc)) from exc
    ready_before = {
        unit.id
        for unit in units
        if unit.base_sha not in missing_before and unit.prediction_sha not in missing_before
    }

    fetched: list[int] = []
    unavailable: list[int] = []
    for offset in range(0, len(numbers), batch_size):
        available_batch, unavailable_batch = _fetch_resilient(
            resolved,
            numbers[offset : offset + batch_size],
            timeout=timeout,
        )
        fetched.extend(available_batch)
        unavailable.extend(unavailable_batch)

    try:
        missing_after = frozenset(missing_commit_objects(resolved, required))
    except GitFactsError as exc:
        raise PullRefFetchError(str(exc)) from exc
    ready_after = {
        unit.id
        for unit in units
        if unit.base_sha not in missing_after and unit.prediction_sha not in missing_after
    }
    return PullRefFetchReport(
        repository=repository,
        units_examined=len(units),
        refs_requested=len(numbers),
        refs_fetched=len(fetched),
        refs_unavailable=tuple(sorted(unavailable)),
        required_objects=len(required),
        objects_missing_before=len(missing_before),
        objects_recovered=len(missing_before.difference(missing_after)),
        objects_missing_after=len(missing_after),
        units_recovered=len(ready_after.difference(ready_before)),
        units_ready_after=len(ready_after),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch bounded public GitHub pull refs needed to materialize exact "
            "event-archive snapshots. Repository code is never checked out or executed."
        )
    )
    parser.add_argument("repository", type=_repository, metavar="OWNER/NAME")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--max-refs", type=int, default=_MAX_REFS)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not 1 <= args.batch_size <= 256:
        raise PullRefFetchError("batch size must be between 1 and 256")
    if not 1 <= args.timeout <= 900:
        raise PullRefFetchError("timeout must be between 1 and 900 seconds")
    if not 1 <= args.max_refs <= _MAX_REFS:
        raise PullRefFetchError(f"max refs must be between 1 and {_MAX_REFS}")
    report = fetch_pull_refs(
        args.root,
        args.repository,
        batch_size=args.batch_size,
        timeout=args.timeout,
        max_refs=args.max_refs,
    )
    print(canonical_json(report.to_dict()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
