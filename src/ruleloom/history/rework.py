"""Line-content rework scan: later commits that deleted lines an earlier commit added.

This is the structural core of the SZZ family without its prose step: no commit
message, issue text, or "fix" keyword is read. A change is *reworked* when, in
the same file, a later non-merge commit deletes lines whose normalized content
the change had added. Lines that the later commit re-adds anywhere are treated
as moves, not rework; generated artifacts, dependency manifests, and trivial
lines are ignored. The result is a dense, change-level, weak outcome that any
Git checkout with blobs can supply, so a learner has something to learn from
before provider evidence arrives.

Every scan records how far it reached and which commits it skipped, so the
window negative of ``post_merge_rework`` is only derived where the scan was
complete. The scan never fetches missing blobs; a partial clone fails closed.
"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import cast

from ruleloom.config import RuleLoomConfig
from ruleloom.gitfacts import GitFactsError, _run_git_capped
from ruleloom.history.models import ChangeUnit, HistoricalEvent
from ruleloom.history.outcomes import (
    GIT_LINE_CONTENT_LINK_KIND,
    GIT_REWORK_SCAN_EVENT_KIND,
    MAX_REWORK_SKIPPED_SHAS,
)
from ruleloom.models import JsonObject, JsonValue, ModelError, parse_timestamp
from ruleloom.packs.base import DEPENDENCY_MANIFEST_NAMES, is_internal_path
from ruleloom.packs.generic_v3 import generated_path_marker

REWORK_SCAN_ADAPTER_VERSION = "ruleloom-git-rework/1"
MIN_LINE_CHARS = 12
MIN_ALNUM_CHARS = 4
MAX_LINES_PER_COMMIT = 5_000
MAX_INDEX_ENTRIES = 2_000_000
_MAX_FILES_PER_EVENT = 12
_BATCH_SIZE = 32
_PRUNE_EVERY = 256
_DIFF_HEADER = b"diff --git a/"


@dataclass(frozen=True, slots=True)
class ReworkScanReport:
    """Events produced by one scan plus the coverage facts a reader must know."""

    events: tuple[HistoricalEvent, ...]
    scan_event: HistoricalEvent
    commits_examined: int
    commits_scanned: int
    commits_skipped_large: int
    commits_skipped_binary_or_empty: int
    rework_events: int
    reworked_commits: int
    scanned_until: str
    window_days: int
    index_entries_peak: int
    warnings: tuple[str, ...]

    def to_dict(self) -> JsonObject:
        return {
            "adapter": REWORK_SCAN_ADAPTER_VERSION,
            "commits_examined": self.commits_examined,
            "commits_scanned": self.commits_scanned,
            "commits_skipped_large": self.commits_skipped_large,
            "commits_skipped_binary_or_empty": self.commits_skipped_binary_or_empty,
            "rework_events": self.rework_events,
            "reworked_commits": self.reworked_commits,
            "scanned_until": self.scanned_until,
            "window_days": self.window_days,
            "index_entries_peak": self.index_entries_peak,
            "scan_event_id": self.scan_event.id,
            "warnings": list(self.warnings),
            "evidence_grade": "weak_heuristic_exploratory",
            "note": (
                "rework is a change-level structural proxy matched by normalized line content; "
                "it is not a defect label and never makes a unit confirmatory"
            ),
        }


def normalize_line(payload: bytes) -> bytes | None:
    """Return the comparable form of a diff line, or ``None`` when it is too trivial."""
    stripped = b" ".join(payload.split())
    if len(stripped) < MIN_LINE_CHARS:
        return None
    if sum(1 for byte in stripped if chr(byte).isalnum()) < MIN_ALNUM_CHARS:
        return None
    return stripped


def _line_key(normalized: bytes) -> bytes:
    return hashlib.sha1(normalized).digest()[:10]


def _ignored_path(path: str) -> bool:
    if is_internal_path(path) or generated_path_marker(path) is not None:
        return True
    name = PurePosixPath(path).name.lower()
    return name in DEPENDENCY_MANIFEST_NAMES or name.startswith("requirements-")


def _chunks(values: Sequence[ChangeUnit], size: int) -> Iterator[Sequence[ChangeUnit]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _run(root: Path, arguments: tuple[str, ...], payload: bytes) -> bytes:
    try:
        stdout, stderr, returncode = _run_git_capped(
            root,
            arguments,
            input_bytes=payload,
            allow_lazy_fetch=False,
        )
    except GitFactsError as exc:
        raise ModelError(f"rework scan could not read Git diffs: {exc}") from exc
    if returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip() or "unknown Git error"
        raise ModelError(f"git {' '.join(arguments[:2])} failed during the rework scan: {detail}")
    return stdout


def _numstat_stdout(root: Path, batch: Sequence[ChangeUnit], roots: frozenset[str]) -> bytes:
    """Concatenate numstat output for a batch, reading root commits one at a time.

    ``diff-tree --stdin`` cannot take Git's empty tree as a parent, so a root
    commit is diffed with ``git diff`` and given the same ``<sha>`` boundary.
    """
    regular = [unit for unit in batch if unit.prediction_sha not in roots]
    outputs: dict[str, bytes] = {}
    if regular:
        payload = "".join(f"{unit.prediction_sha} {unit.base_sha}\n" for unit in regular).encode()
        stdout = _run(
            root,
            ("diff-tree", "--stdin", "--always", "-r", "--numstat", "-z", "--no-renames"),
            payload,
        )
        current: str | None = None
        for token in stdout.split(b"\x00"):
            if not token:
                continue
            if b"\t" not in token:
                current = token.decode("ascii", errors="replace")
                outputs[current] = token + b"\x00"
                continue
            if current is not None:
                outputs[current] += token + b"\x00"
    for unit in batch:
        if unit.prediction_sha in roots:
            stdout = _run(
                root,
                (
                    "diff",
                    "--numstat",
                    "-z",
                    "--no-renames",
                    unit.base_sha,
                    unit.prediction_sha,
                    "--",
                ),
                b"",
            )
            outputs[unit.prediction_sha] = unit.prediction_sha.encode("ascii") + b"\x00" + stdout
    return b"".join(
        outputs.get(unit.prediction_sha, unit.prediction_sha.encode("ascii") + b"\x00")
        for unit in batch
    )


def _patch_stdout(root: Path, batch: Sequence[ChangeUnit], roots: frozenset[str]) -> bytes:
    """Concatenate zero-context patches for a batch in batch order."""
    regular = [unit for unit in batch if unit.prediction_sha not in roots]
    outputs: dict[str, bytes] = {}
    if regular:
        payload = "".join(f"{unit.prediction_sha} {unit.base_sha}\n" for unit in regular).encode()
        stdout = _run(
            root,
            (
                "diff-tree",
                "--stdin",
                "--always",
                "-r",
                "-p",
                "-U0",
                "--no-renames",
                "--no-color",
                "--no-ext-diff",
            ),
            payload,
        )
        expected = {unit.prediction_sha.encode("ascii"): unit.prediction_sha for unit in regular}
        current = None
        for raw_line in stdout.split(b"\n"):
            if raw_line in expected:
                current = expected[raw_line]
                outputs[current] = raw_line + b"\n"
                continue
            if current is not None:
                outputs[current] += raw_line + b"\n"
    for unit in batch:
        if unit.prediction_sha in roots:
            stdout = _run(
                root,
                (
                    "diff",
                    "-p",
                    "-U0",
                    "--no-renames",
                    "--no-color",
                    "--no-ext-diff",
                    unit.base_sha,
                    unit.prediction_sha,
                    "--",
                ),
                b"",
            )
            outputs[unit.prediction_sha] = unit.prediction_sha.encode("ascii") + b"\n" + stdout
    return b"".join(
        outputs.get(unit.prediction_sha, unit.prediction_sha.encode("ascii") + b"\n")
        for unit in batch
    )


def _numstat_totals(
    root: Path, batch: Sequence[ChangeUnit], roots: frozenset[str]
) -> list[int | None]:
    """Changed-line totals per commit; ``None`` marks binary-only or empty diffs."""
    stdout = _numstat_stdout(root, batch, roots)
    totals: list[int | None] = []
    current = -1
    text_lines = 0
    saw_text = False
    for token in stdout.split(b"\x00"):
        if not token:
            continue
        if b"\t" not in token:
            if current >= 0:
                totals.append(text_lines if saw_text else None)
            current += 1
            text_lines = 0
            saw_text = False
            continue
        fields = token.split(b"\t", 2)
        if len(fields) != 3 or fields[0] == b"-" or fields[1] == b"-":
            continue
        try:
            text_lines += int(fields[0]) + int(fields[1])
        except ValueError:
            continue
        saw_text = True
    if current >= 0:
        totals.append(text_lines if saw_text else None)
    if len(totals) != len(batch):
        raise ModelError("rework scan received an incomplete numstat batch")
    return totals


@dataclass(slots=True)
class _CommitPatch:
    added: dict[str, list[bytes]]
    deleted: dict[str, list[bytes]]


def _parse_patch_batch(stdout: bytes, batch: Sequence[ChangeUnit]) -> list[_CommitPatch]:
    """Split a ``diff-tree --stdin -p`` stream into per-commit added/deleted line keys."""
    patches: list[_CommitPatch] = []
    expected = [unit.prediction_sha.encode("ascii") for unit in batch]
    current: _CommitPatch | None = None
    path: str | None = None
    for raw_line in stdout.split(b"\n"):
        if len(patches) < len(expected) and raw_line == expected[len(patches)]:
            current = _CommitPatch(defaultdict(list), defaultdict(list))
            patches.append(current)
            path = None
            continue
        if current is None:
            continue
        if raw_line.startswith(_DIFF_HEADER):
            remainder = raw_line[len(_DIFF_HEADER) :]
            marker = remainder.find(b" b/")
            path = None
            if marker > 0:
                candidate = remainder[marker + 3 :]
                try:
                    decoded = candidate.decode("utf-8")
                except UnicodeDecodeError:
                    decoded = ""
                if decoded and not decoded.startswith('"') and not _ignored_path(decoded):
                    path = decoded
            continue
        if path is None or not raw_line:
            continue
        head = raw_line[:1]
        if head == b"+" and not raw_line.startswith(b"+++ "):
            normalized = normalize_line(raw_line[1:])
            if normalized is not None:
                current.added[path].append(_line_key(normalized))
        elif head == b"-" and not raw_line.startswith(b"--- "):
            normalized = normalize_line(raw_line[1:])
            if normalized is not None:
                current.deleted[path].append(_line_key(normalized))
    if len(patches) != len(batch):
        raise ModelError("rework scan received an incomplete patch batch")
    return patches


def scan_rework(
    root: Path,
    config: RuleLoomConfig,
    units: Sequence[ChangeUnit],
    events: Sequence[HistoricalEvent],
) -> ReworkScanReport:
    """Scan Git-landed commits chronologically and emit ``rework`` and scan events."""

    window_days = config.outcomes.rework_window_days
    if window_days is None:
        raise ModelError(
            "outcomes.rework_window_days is not registered; initialize the experiment with "
            "--rework-window-days before scanning"
        )
    window = timedelta(days=window_days)
    repository_id = config.protocol.repository_id
    candidates = sorted(
        (
            unit
            for unit in units
            if unit.provider == "git"
            and unit.kind == "git_commit"
            and unit.repository_id == repository_id
        ),
        key=lambda unit: (parse_timestamp(unit.prediction_at), unit.id),
    )
    if not candidates:
        raise ModelError("no Git commit units are available; run history bootstrap-git first")
    author_by_sha: dict[str, str] = {}
    for event in events:
        if event.kind in {"git_commit", "git_merge"} and event.repository_id == repository_id:
            sha = event.data.get("sha")
            author = event.data.get("author_hash")
            if isinstance(sha, str) and isinstance(author, str):
                author_by_sha[sha] = author

    roots = frozenset(
        sha
        for event in events
        if event.kind == "git_commit"
        and event.repository_id == repository_id
        and event.data.get("parents") == []
        and isinstance((sha := event.data.get("sha")), str)
    )
    totals: list[int | None] = []
    for batch in _chunks(candidates, _BATCH_SIZE):
        totals.extend(_numstat_totals(root, batch, roots))
    eligible: list[ChangeUnit] = []
    skipped_large: list[str] = []
    skipped_empty = 0
    for unit, total in zip(candidates, totals, strict=True):
        if total is None:
            skipped_empty += 1
            continue
        if total > MAX_LINES_PER_COMMIT:
            skipped_large.append(unit.prediction_sha)
            continue
        eligible.append(unit)
    if len(skipped_large) > MAX_REWORK_SKIPPED_SHAS:
        raise ModelError(
            f"rework scan skipped {len(skipped_large)} oversized commits, more than the "
            f"{MAX_REWORK_SKIPPED_SHAS} that window negatives can account for"
        )

    index: dict[str, dict[bytes, list[tuple[str, datetime]]]] = defaultdict(dict)
    index_entries = 0
    index_peak = 0
    rework_events: list[HistoricalEvent] = []
    reworked: set[str] = set()
    processed = 0
    instants = {unit.prediction_sha: parse_timestamp(unit.prediction_at) for unit in candidates}

    def prune(now: datetime) -> None:
        nonlocal index_entries
        for path in list(index):
            by_key = index[path]
            for key in list(by_key):
                kept = [item for item in by_key[key] if now - item[1] <= window]
                index_entries -= len(by_key[key]) - len(kept)
                if kept:
                    by_key[key] = kept
                else:
                    del by_key[key]
            if not by_key:
                del index[path]

    for batch in _chunks(eligible, _BATCH_SIZE):
        stdout = _patch_stdout(root, batch, roots)
        patches = _parse_patch_batch(stdout, batch)
        for unit, patch in zip(batch, patches, strict=True):
            sha = unit.prediction_sha
            now = instants[sha]
            moved = {key for keys in patch.added.values() for key in keys}
            hits: Counter[str] = Counter()
            files: dict[str, set[str]] = defaultdict(set)
            for path, keys in patch.deleted.items():
                by_key = index.get(path)
                if not by_key:
                    continue
                for key in keys:
                    if key in moved:
                        continue
                    for earlier_sha, earlier_at in by_key.get(key, ()):
                        if earlier_sha == sha or now - earlier_at > window or now < earlier_at:
                            continue
                        hits[earlier_sha] += 1
                        files[earlier_sha].add(path)
            for earlier_sha, count in sorted(hits.items()):
                earlier_author = author_by_sha.get(earlier_sha)
                later_author = author_by_sha.get(sha)
                same_author: JsonValue = (
                    None
                    if earlier_author is None or later_author is None
                    else earlier_author == later_author
                )
                touched = sorted(files[earlier_sha])
                linked_change = f"change.git_commit.{earlier_sha}"
                rework_events.append(
                    HistoricalEvent(
                        schema_version=1,
                        id=f"event.git_rework.{sha}.{earlier_sha}",
                        repository_id=repository_id,
                        kind="rework",
                        occurred_at=unit.prediction_at,
                        available_at=unit.prediction_at,
                        provider="git",
                        source_ref=sha,
                        independent_group=f"change.git_commit.{sha}",
                        change_id=linked_change,
                        data={
                            "adapter": REWORK_SCAN_ADAPTER_VERSION,
                            "sha": sha,
                            "reworked_sha": earlier_sha,
                            "linked_change_id": linked_change,
                            "link_kind": GIT_LINE_CONTENT_LINK_KIND,
                            "evidence_grade": "weak_heuristic",
                            "reworked_lines": count,
                            "files": cast(JsonValue, touched[:_MAX_FILES_PER_EVENT]),
                            "files_truncated": max(0, len(touched) - _MAX_FILES_PER_EVENT),
                            "same_author": same_author,
                            "days_after": round(
                                (now - instants[earlier_sha]).total_seconds() / 86_400, 3
                            ),
                        },
                    )
                )
                reworked.add(earlier_sha)
            for path, keys in patch.added.items():
                by_key = index[path]
                for key in keys:
                    by_key.setdefault(key, []).append((sha, now))
                    index_entries += 1
            index_peak = max(index_peak, index_entries)
            if index_entries > MAX_INDEX_ENTRIES:
                raise ModelError(
                    f"rework scan exceeded {MAX_INDEX_ENTRIES} indexed lines; reduce the "
                    "bootstrapped history or the registered rework window"
                )
            processed += 1
            if processed % _PRUNE_EVERY == 0:
                prune(now)

    scanned_until = max(unit.prediction_at for unit in candidates)
    skipped_sorted = sorted(skipped_large)
    digest = hashlib.sha256(
        "\x00".join(
            [
                repository_id,
                scanned_until,
                str(window_days),
                REWORK_SCAN_ADAPTER_VERSION,
                *skipped_sorted,
            ]
        ).encode("utf-8")
    ).hexdigest()[:20]
    scan_id = f"event.{GIT_REWORK_SCAN_EVENT_KIND}.{digest}"
    scan_event = HistoricalEvent(
        schema_version=1,
        id=scan_id,
        repository_id=repository_id,
        kind=GIT_REWORK_SCAN_EVENT_KIND,
        occurred_at=scanned_until,
        available_at=scanned_until,
        provider="git",
        source_ref=candidates[-1].prediction_sha,
        independent_group=scan_id,
        change_id=None,
        data={
            "adapter": REWORK_SCAN_ADAPTER_VERSION,
            "window_days": window_days,
            "scanned_until": scanned_until,
            "commits_scanned": len(eligible),
            "commits_skipped_large": len(skipped_large),
            "commits_skipped_binary_or_empty": skipped_empty,
            "skipped_shas": cast(JsonValue, skipped_sorted),
            "min_line_chars": MIN_LINE_CHARS,
            "min_alnum_chars": MIN_ALNUM_CHARS,
            "max_lines_per_commit": MAX_LINES_PER_COMMIT,
            "selection": "git_commit_units_chronological_first_parent_diffs",
        },
    )
    warnings: list[str] = []
    if skipped_large:
        warnings.append(
            f"{len(skipped_large)} commit(s) above {MAX_LINES_PER_COMMIT} changed lines were "
            "not indexed; they receive no rework votes and no window negative"
        )
    return ReworkScanReport(
        events=(*rework_events, scan_event),
        scan_event=scan_event,
        commits_examined=len(candidates),
        commits_scanned=len(eligible),
        commits_skipped_large=len(skipped_large),
        commits_skipped_binary_or_empty=skipped_empty,
        rework_events=len(rework_events),
        reworked_commits=len(reworked),
        scanned_until=scanned_until,
        window_days=window_days,
        index_entries_peak=index_peak,
        warnings=tuple(warnings),
    )


__all__ = [
    "MAX_INDEX_ENTRIES",
    "MAX_LINES_PER_COMMIT",
    "MIN_ALNUM_CHARS",
    "MIN_LINE_CHARS",
    "REWORK_SCAN_ADAPTER_VERSION",
    "ReworkScanReport",
    "normalize_line",
    "scan_rework",
]
