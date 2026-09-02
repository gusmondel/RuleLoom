#!/usr/bin/env python3
"""Export a bounded, prose-free GH Archive projection from public ClickHouse."""

from __future__ import annotations

import argparse
import hashlib
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import UTC, timedelta
from pathlib import Path

from ruleloom.history.github_event_archive import (
    GitHubEventArchiveError,
    GitHubEventArchiveManifest,
    build_clickhouse_file_hours_query,
    build_clickhouse_gharchive_query,
    clickhouse_dataset_max_query,
    utc_now,
)
from ruleloom.history.storage import HISTORY_JSONL_MAX_BYTES
from ruleloom.models import ModelError, canonical_json, parse_timestamp

_DEFAULT_ENDPOINT = "https://play.clickhouse.com/"
_USER_AGENT = "RuleLoom-GHArchive-Exporter/2"
_MAX_COVERAGE_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_COVERAGE_HOURS = 100_000


def _endpoint(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    try:
        _ = parsed.port
    except ValueError as exc:
        raise argparse.ArgumentTypeError("endpoint contains an invalid port") from exc
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise argparse.ArgumentTypeError(
            "endpoint must be an HTTPS origin without credentials, query, or fragment"
        )
    return value.rstrip("/") + "/"


def _output_path(value: str) -> Path:
    path = Path(os.path.abspath(os.fspath(Path(value).expanduser())))
    if path.is_symlink():
        raise argparse.ArgumentTypeError("output cannot be a symlink")
    if not path.parent.is_dir():
        raise argparse.ArgumentTypeError("output parent must already exist")
    return path


def _post(endpoint: str, query: str, *, maximum: int) -> bytes:
    # The public playground enforces a 60-second limit that includes transfer time;
    # a compressed transfer keeps the frozen projection inside it. Decompressed bytes
    # are hashed, so the manifest is identical to an uncompressed export.
    url = endpoint + "?" + urllib.parse.urlencode({"user": "play", "enable_http_compression": 1})
    request = urllib.request.Request(
        url,
        data=query.encode("utf-8"),
        headers={
            "Content-Type": "text/plain; charset=utf-8",
            "User-Agent": _USER_AGENT,
            "Accept-Encoding": "gzip",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            final = urllib.parse.urlsplit(response.geturl())
            expected = urllib.parse.urlsplit(endpoint)
            try:
                final_port = final.port
                expected_port = expected.port
            except ValueError as exc:
                raise GitHubEventArchiveError(
                    "ClickHouse returned an invalid redirect URL"
                ) from exc
            if (
                final.scheme != "https"
                or final.hostname != expected.hostname
                or final_port != expected_port
                or final.username is not None
                or final.password is not None
            ):
                raise GitHubEventArchiveError("ClickHouse redirected outside the pinned host")
            compressed = response.headers.get("Content-Encoding") == "gzip"
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > maximum:
                    raise GitHubEventArchiveError(
                        f"ClickHouse response exceeds the {maximum}-byte export limit"
                    )
                chunks.append(chunk)
            payload = b"".join(chunks)
            if not compressed:
                return payload
            decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
            try:
                inflated = decompressor.decompress(payload, maximum + 1)
                inflated += decompressor.flush()
            except zlib.error as exc:
                raise GitHubEventArchiveError(
                    "ClickHouse returned an invalid compressed response"
                ) from exc
            if len(inflated) > maximum or decompressor.unconsumed_tail:
                raise GitHubEventArchiveError(
                    f"ClickHouse response exceeds the {maximum}-byte export limit"
                )
            return inflated
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GitHubEventArchiveError(f"cannot query public ClickHouse endpoint: {exc}") from exc


def _atomic_write(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Export exact PR opening, merge, approval, and changes-requested events "
            "without repository prose or account names."
        )
    )
    parser.add_argument("repository", metavar="OWNER/NAME")
    parser.add_argument("--provider-repository-id", type=int, required=True)
    parser.add_argument("--since", required=True, help="aware ISO-8601 inclusive lower bound")
    parser.add_argument("--until", required=True, help="aware ISO-8601 exclusive upper bound")
    parser.add_argument("--preregistration-sha256", required=True)
    parser.add_argument("--events", type=_output_path, required=True)
    parser.add_argument("--manifest", type=_output_path, required=True)
    parser.add_argument("--endpoint", type=_endpoint, default=_DEFAULT_ENDPOINT)
    parser.add_argument(
        "--collected-at",
        help="aware ISO-8601 timestamp for reproducible tests (default: current UTC)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.events == args.manifest:
        raise GitHubEventArchiveError("events and manifest outputs must be different files")
    start = parse_timestamp(args.since).astimezone(UTC)
    end = parse_timestamp(args.until).astimezone(UTC)
    if end <= start:
        raise GitHubEventArchiveError("until must follow since")
    if any(value.minute or value.second or value.microsecond for value in (start, end)):
        raise GitHubEventArchiveError("collection boundaries must use exact whole hours")
    expected_hours = int((end - start).total_seconds()) // 3600
    if expected_hours > _MAX_COVERAGE_HOURS:
        raise GitHubEventArchiveError(
            f"collection spans {expected_hours} hours; limit is {_MAX_COVERAGE_HOURS}"
        )
    query = build_clickhouse_gharchive_query(args.repository, args.since, args.until)
    coverage_query = build_clickhouse_file_hours_query(args.since, args.until)
    max_raw = _post(args.endpoint, clickhouse_dataset_max_query(), maximum=4096)
    try:
        dataset_max_at = max_raw.decode("utf-8").strip()
        parse_timestamp(dataset_max_at)
    except (UnicodeDecodeError, ModelError) as exc:
        raise GitHubEventArchiveError("ClickHouse returned an invalid dataset maximum") from exc
    if parse_timestamp(dataset_max_at) < parse_timestamp(args.until):
        raise GitHubEventArchiveError(
            "public ClickHouse data does not yet cover the requested exclusive upper bound"
        )

    coverage_bytes = _post(
        args.endpoint,
        coverage_query,
        maximum=_MAX_COVERAGE_RESPONSE_BYTES,
    )
    try:
        coverage_text = coverage_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GitHubEventArchiveError("ClickHouse file-hour coverage must be UTF-8") from exc
    observed: set[str] = set()
    for line_number, raw_hour in enumerate(coverage_text.splitlines(), 1):
        hour = raw_hour.strip()
        try:
            parsed = parse_timestamp(hour).astimezone(UTC)
        except ModelError as exc:
            raise GitHubEventArchiveError(
                f"ClickHouse returned an invalid file hour at line {line_number}"
            ) from exc
        if parsed.minute or parsed.second or parsed.microsecond or not start <= parsed < end:
            raise GitHubEventArchiveError(
                f"ClickHouse returned an out-of-window file hour at line {line_number}"
            )
        observed.add(parsed.strftime("%Y-%m-%dT%H:00:00Z"))
    expected = tuple(
        (start + timedelta(hours=index)).strftime("%Y-%m-%dT%H:00:00Z")
        for index in range(expected_hours)
    )
    missing_hours = tuple(hour for hour in expected if hour not in observed)

    event_bytes = _post(args.endpoint, query, maximum=HISTORY_JSONL_MAX_BYTES)
    if event_bytes and not event_bytes.endswith(b"\n"):
        raise GitHubEventArchiveError("ClickHouse JSONEachRow response lacks a final newline")
    collected_at = args.collected_at or utc_now()
    manifest = GitHubEventArchiveManifest(
        repository=args.repository,
        provider_repository_id=args.provider_repository_id,
        collection_start=args.since,
        collection_end=args.until,
        dataset_max_at=dataset_max_at,
        collected_at=collected_at,
        query_sha256=hashlib.sha256(query.encode("utf-8")).hexdigest(),
        coverage_query_sha256=hashlib.sha256(coverage_query.encode("utf-8")).hexdigest(),
        events_sha256=hashlib.sha256(event_bytes).hexdigest(),
        preregistration_sha256=args.preregistration_sha256,
        window_complete=True,
        expected_hours=expected_hours,
        observed_hours=len(observed),
        missing_hours=missing_hours,
        source_url=args.endpoint,
    )
    manifest_bytes = (canonical_json(manifest.to_dict()) + "\n").encode("utf-8")
    _atomic_write(args.events, event_bytes)
    _atomic_write(args.manifest, manifest_bytes)
    print(canonical_json({"events": str(args.events), "manifest": manifest.to_dict()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
