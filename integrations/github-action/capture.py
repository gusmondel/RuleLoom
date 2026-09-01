"""Isolated wrapper for the dependency-free RuleLoom GitHub capture API."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

# The composite action invokes Python with -I. Add only this pinned action's
# source tree; never add the caller workspace or inherit PYTHONPATH.
ACTION_DIRECTORY = Path(__file__).resolve().parent
RULELOOM_SOURCE = ACTION_DIRECTORY.parent.parent / "src"
sys.path.insert(0, str(RULELOOM_SOURCE))

from ruleloom.history.github_webhooks import (  # noqa: E402
    GitHubWebhookCaptureError,
    capture_github_actions_event_file,
    load_github_capture_bundle,
    parse_github_label_policy,
    write_github_capture_bundle,
)


def _required(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value or any(character in value for character in "\x00\r\n"):
        raise GitHubWebhookCaptureError(f"required action environment {name} is missing or unsafe")
    return value


def _positive_integer(name: str) -> int:
    raw = _required(name)
    try:
        value = int(raw, 10)
    except ValueError as exc:
        raise GitHubWebhookCaptureError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise GitHubWebhookCaptureError(f"{name} must be a positive integer")
    return value


def main() -> int:
    output_directory = Path(_required("RULELOOM_CAPTURE_OUTPUT_DIRECTORY"))
    if not output_directory.is_absolute():
        raise GitHubWebhookCaptureError("RuleLoom capture output directory must be absolute")
    run_id = _positive_integer("GITHUB_RUN_ID")
    provider_repository_id = _positive_integer("GITHUB_REPOSITORY_ID")
    identity_key = _required("RULELOOM_CAPTURE_IDENTITY_KEY").encode("utf-8")
    envelope_key = _required("RULELOOM_CAPTURE_ENVELOPE_KEY").encode("utf-8")
    policy = parse_github_label_policy(
        os.environ.get(
            "RULELOOM_CAPTURE_LABEL_POLICY_JSON",
            '{"schema_version":1,"labels":[]}',
        )
    )
    candidate = capture_github_actions_event_file(
        Path(_required("GITHUB_EVENT_PATH")),
        event_name=_required("GITHUB_EVENT_NAME"),
        run_id=run_id,
        received_at=datetime.now(UTC),
        repository_id=_required("RULELOOM_REPOSITORY_ID"),
        expected_provider_repository_id=provider_repository_id,
        identity_key=identity_key,
        envelope_key=envelope_key,
        label_policy=policy,
    )
    bundle_path = output_directory / (f"github-actions-{provider_repository_id}-{run_id}.json")
    created = True
    if bundle_path.exists() or bundle_path.is_symlink():
        existing = load_github_capture_bundle(bundle_path, envelope_key=envelope_key)
        stable_existing = (
            existing.transport,
            existing.delivery_id,
            existing.event_name,
            existing.repository_id,
            existing.provider_repository_id,
            existing.payload_sha256,
            existing.label_policy_hash,
        )
        stable_candidate = (
            candidate.transport,
            candidate.delivery_id,
            candidate.event_name,
            candidate.repository_id,
            candidate.provider_repository_id,
            candidate.payload_sha256,
            candidate.label_policy_hash,
        )
        if stable_existing != stable_candidate:
            raise GitHubWebhookCaptureError(
                "existing Actions capture conflicts with this run identity or payload"
            )
        candidate = existing
        created = False
    else:
        created = write_github_capture_bundle(
            bundle_path,
            candidate,
            envelope_key=envelope_key,
        )

    # These values contain only a caller-selected directory plus numeric IDs and
    # lowercase digests. The composite action redirects them to GITHUB_OUTPUT.
    print(f"capture-path={bundle_path}")
    print(f"envelope-sha256={candidate.envelope_sha256}")
    print(f"capture-created={'true' if created else 'false'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GitHubWebhookCaptureError as exc:
        print(f"RuleLoom GitHub capture failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
