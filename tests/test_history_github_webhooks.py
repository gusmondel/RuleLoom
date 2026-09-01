from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import ruleloom.history.github_webhooks as github_webhooks
from ruleloom.history.github_webhooks import (
    GITHUB_WEBHOOK_ADAPTER_VERSION,
    MAX_GITHUB_DELIVERY_BYTES,
    GitHubLabelOutcome,
    GitHubWebhookCapture,
    GitHubWebhookCaptureError,
    capture_github_actions_event,
    capture_github_actions_event_file,
    capture_github_webhook,
    finalize_github_capture_units,
    github_label_policy_hash,
    ingest_github_capture,
    ingest_github_capture_directory,
    load_github_capture_bundle,
    parse_github_label_policy,
    verify_github_webhook_signature,
    write_github_capture_bundle,
)
from ruleloom.history.materialize import materialize_history
from ruleloom.history.models import ChangeUnit, HistoricalEvent
from ruleloom.history.storage import (
    change_units_path,
    events_path,
    load_change_units,
    load_events,
    load_history_snapshot,
    upsert_history_batch,
)
from ruleloom.history.units import validate_history_snapshot
from ruleloom.models import JsonObject, LabelValue, ModelError, canonical_json, content_hash
from ruleloom.project import initialize_project

WEBHOOK_SECRET = b"webhook-secret-with-sufficient-entropy"
IDENTITY_KEY = b"identity-key-with-sufficient-entropy"
ENVELOPE_KEY = b"envelope-key-with-sufficient-entropy"
OTHER_ENVELOPE_KEY = b"different-envelope-key-sufficiently-long"
RECEIVED_AT = "2026-05-02T12:00:00Z"
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
MERGE_SHA = "c" * 40


def _pull_payload(
    *,
    action: str = "labeled",
    repository_id: int = 101,
    sender_id: int = 7,
    author_id: int = 9,
    label_name: str = "ruleloom:validation:positive",
    label_id: int = 88,
) -> JsonObject:
    pull: JsonObject = {
        "number": 42,
        "user": {"id": author_id, "login": "pull-author"},
        "created_at": "2026-05-01T10:00:00Z",
        "updated_at": "2026-05-02T11:59:00Z",
        "base": {"sha": BASE_SHA},
        "head": {"sha": HEAD_SHA},
        # This mutable collection is deliberately ignored by the adapter.
        "labels": [{"id": 999, "name": "ruleloom:validation:negative"}],
    }
    if action == "closed":
        pull.update(
            {
                "merged": True,
                "merged_at": "2026-05-02T11:58:00Z",
                "closed_at": "2026-05-02T11:58:30Z",
                "merge_commit_sha": MERGE_SHA,
            }
        )
    return {
        "action": action,
        "repository": {
            "id": repository_id,
            "full_name": "acme/widgets",
            "name": "widgets",
        },
        "sender": {"id": sender_id, "login": "label-actor"},
        "pull_request": pull,
        "label": {"id": label_id, "name": label_name, "color": "ffffff"},
    }


def _bytes(payload: JsonObject) -> bytes:
    return canonical_json(payload).encode("utf-8")


def _headers(
    payload: bytes,
    *,
    event: str = "pull_request",
    delivery: str = "delivery-0001",
    secret: bytes = WEBHOOK_SECRET,
) -> dict[str, str]:
    signature = hmac.new(secret, payload, hashlib.sha256).hexdigest()
    return {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": f"sha256={signature}",
    }


def _policy(*, actors: frozenset[int] = frozenset({7})) -> tuple[GitHubLabelOutcome, ...]:
    return (
        GitHubLabelOutcome(
            name="ruleloom:validation:positive",
            target="validation_rework_required",
            value="positive",
            evidence_complete=True,
            authorized_actor_ids=actors,
        ),
    )


def _expected_policy_hash(
    policy: tuple[GitHubLabelOutcome, ...] | None = None,
) -> str:
    return github_label_policy_hash(_policy() if policy is None else policy, IDENTITY_KEY)


def _capture(
    payload: JsonObject | None = None,
    *,
    delivery: str = "delivery-0001",
    received_at: str = RECEIVED_AT,
    label_policy: tuple[GitHubLabelOutcome, ...] | None = None,
    expected_provider_repository_id: int = 101,
    repository_id: str = "repo.widgets",
) -> GitHubWebhookCapture:
    raw = _bytes(payload or _pull_payload())
    return capture_github_webhook(
        raw,
        _headers(raw, delivery=delivery),
        received_at=received_at,
        repository_id=repository_id,
        expected_provider_repository_id=expected_provider_repository_id,
        webhook_secret=WEBHOOK_SECRET,
        identity_key=IDENTITY_KEY,
        envelope_key=ENVELOPE_KEY,
        label_policy=_policy() if label_policy is None else label_policy,
    )


def _initialize_git_repository(path: Path) -> None:
    subprocess.run(
        ["git", "init", "--quiet", str(path)],
        check=True,
        capture_output=True,
    )


def _git(path: Path, *arguments: str, environment: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return result.stdout.strip()


def _commit(path: Path, filename: str, content: str, timestamp: str) -> str:
    (path / filename).write_text(content, encoding="utf-8")
    _git(path, "add", "--", filename)
    _git(
        path,
        "commit",
        "-m",
        f"add {filename}",
        environment={
            **os.environ,
            "GIT_AUTHOR_DATE": timestamp,
            "GIT_COMMITTER_DATE": timestamp,
        },
    )
    return _git(path, "rev-parse", "HEAD")


def test_signed_label_delivery_emits_exact_authorized_point_in_time_outcome() -> None:
    capture = _capture()

    assert capture.signature_verified is True
    assert capture.transport == "github_webhook_hmac"
    assert [event.kind for event in capture.events] == [
        "provider_delivery",
        "provider_label_applied",
        "change_finalized",
    ]
    applied, outcome = capture.events[1:]
    assert applied.data["label_name_at_delivery"] == "ruleloom:validation:positive"
    assert applied.data["point_in_time_label_name"] is True
    assert applied.data["actor_key"] != applied.data["author_key"]
    assert outcome.data["target"] == "validation_rework_required"
    assert outcome.data["value"] == "positive"
    assert outcome.data["evidence_complete"] is True
    assert outcome.data["authorization"] == "registered_provider_actor_allowlist"
    serialized = canonical_json(capture.to_dict())
    assert "pull-author" not in serialized
    assert "label-actor" not in serialized
    assert "ruleloom:validation:negative" not in serialized
    capture.verify(ENVELOPE_KEY)


@pytest.mark.parametrize(
    "payload",
    [
        _pull_payload(sender_id=9, author_id=9),
        _pull_payload(sender_id=77),
        _pull_payload(action="unlabeled"),
    ],
)
def test_label_outcome_requires_application_authorization_and_independence(
    payload: JsonObject,
) -> None:
    capture = _capture(payload)

    assert "change_finalized" not in {event.kind for event in capture.events}
    assert any(event.kind.startswith("provider_label_") for event in capture.events)


def test_non_label_action_never_reads_mutable_pull_label_collection() -> None:
    payload = _pull_payload(action="synchronize")

    capture = _capture(payload)

    assert [event.kind for event in capture.events] == [
        "provider_delivery",
        "change_snapshot",
    ]
    assert all("label_name_at_delivery" not in event.data for event in capture.events)


def test_signature_body_repository_and_header_boundaries_fail_closed() -> None:
    raw = _bytes(_pull_payload())
    bad_signature = _headers(raw)
    bad_signature["X-Hub-Signature-256"] = "sha256=" + "0" * 64
    with pytest.raises(GitHubWebhookCaptureError, match="signature verification failed"):
        capture_github_webhook(
            raw,
            bad_signature,
            received_at=RECEIVED_AT,
            repository_id="repo.widgets",
            expected_provider_repository_id=101,
            webhook_secret=WEBHOOK_SECRET,
            identity_key=IDENTITY_KEY,
            envelope_key=ENVELOPE_KEY,
        )

    with pytest.raises(GitHubWebhookCaptureError, match="does not match"):
        capture_github_webhook(
            raw,
            _headers(raw),
            received_at=RECEIVED_AT,
            repository_id="repo.widgets",
            expected_provider_repository_id=999,
            webhook_secret=WEBHOOK_SECRET,
            identity_key=IDENTITY_KEY,
            envelope_key=ENVELOPE_KEY,
        )

    duplicate_case = _headers(raw)
    duplicate_case["x-github-event"] = "pull_request"
    with pytest.raises(GitHubWebhookCaptureError, match="duplicate GitHub webhook header"):
        capture_github_webhook(
            raw,
            duplicate_case,
            received_at=RECEIVED_AT,
            repository_id="repo.widgets",
            expected_provider_repository_id=101,
            webhook_secret=WEBHOOK_SECRET,
            identity_key=IDENTITY_KEY,
            envelope_key=ENVELOPE_KEY,
        )


def test_payload_size_and_duplicate_json_keys_are_rejected_before_normalization() -> None:
    oversized = b"{" + b" " * MAX_GITHUB_DELIVERY_BYTES + b"}"
    with pytest.raises(GitHubWebhookCaptureError, match="no larger"):
        capture_github_webhook(
            oversized,
            _headers(oversized),
            received_at=RECEIVED_AT,
            repository_id="repo.widgets",
            expected_provider_repository_id=101,
            webhook_secret=WEBHOOK_SECRET,
            identity_key=IDENTITY_KEY,
            envelope_key=ENVELOPE_KEY,
        )

    duplicate = b'{"repository":{"id":101},"repository":{"id":101}}'
    with pytest.raises(GitHubWebhookCaptureError, match="duplicate object key"):
        capture_github_webhook(
            duplicate,
            _headers(duplicate),
            received_at=RECEIVED_AT,
            repository_id="repo.widgets",
            expected_provider_repository_id=101,
            webhook_secret=WEBHOOK_SECRET,
            identity_key=IDENTITY_KEY,
            envelope_key=ENVELOPE_KEY,
        )


def test_append_only_ingestion_is_idempotent_and_conflicting_delivery_fails(
    tmp_path: Path,
) -> None:
    _initialize_git_repository(tmp_path)
    first = _capture()

    assert ingest_github_capture(
        tmp_path,
        first,
        expected_repository_id="repo.widgets",
        expected_label_policy_hash=_expected_policy_hash(),
        envelope_key=ENVELOPE_KEY,
    ) == (
        (3, 0),
        (0, 0),
    )
    assert ingest_github_capture(
        tmp_path,
        first,
        expected_repository_id="repo.widgets",
        expected_label_policy_hash=_expected_policy_hash(),
        envelope_key=ENVELOPE_KEY,
    ) == (
        (0, 3),
        (0, 0),
    )

    renamed_payload = _pull_payload(label_name="renamed-after-first-delivery")
    conflicting = _capture(renamed_payload)
    with pytest.raises(GitHubWebhookCaptureError, match="conflicting immutable GitHub"):
        ingest_github_capture(
            tmp_path,
            conflicting,
            expected_repository_id="repo.widgets",
            expected_label_policy_hash=_expected_policy_hash(),
            envelope_key=ENVELOPE_KEY,
        )
    assert load_events(events_path(tmp_path)) == list(first.events)


def test_direct_ingest_requires_independent_repository_and_policy_pins(
    tmp_path: Path,
) -> None:
    _initialize_git_repository(tmp_path)
    capture = _capture()

    with pytest.raises(GitHubWebhookCaptureError, match=r"repo\.widgets.*repo\.other"):
        ingest_github_capture(
            tmp_path,
            capture,
            expected_repository_id="repo.other",
            expected_label_policy_hash=_expected_policy_hash(),
            envelope_key=ENVELOPE_KEY,
        )
    with pytest.raises(GitHubWebhookCaptureError, match="independently frozen"):
        ingest_github_capture(
            tmp_path,
            capture,
            expected_repository_id="repo.widgets",
            expected_label_policy_hash="0" * 64,
            envelope_key=ENVELOPE_KEY,
        )

    assert not events_path(tmp_path).exists()
    assert not change_units_path(tmp_path).exists()


def test_existing_webhook_history_rejects_a_new_label_policy_pin(tmp_path: Path) -> None:
    _initialize_git_repository(tmp_path)
    first = _capture(delivery="policy-first")
    ingest_github_capture(
        tmp_path,
        first,
        expected_repository_id="repo.widgets",
        expected_label_policy_hash=_expected_policy_hash(),
        envelope_key=ENVELOPE_KEY,
    )
    alternative = (
        GitHubLabelOutcome(
            name="ruleloom:validation:positive",
            target="validation_rework_required",
            value="negative",
            evidence_complete=True,
            authorized_actor_ids=frozenset({7}),
        ),
    )
    changed = _capture(
        delivery="policy-changed",
        label_policy=alternative,
    )

    with pytest.raises(GitHubWebhookCaptureError, match="independently frozen"):
        ingest_github_capture(
            tmp_path,
            changed,
            expected_repository_id="repo.widgets",
            expected_label_policy_hash=_expected_policy_hash(alternative),
            envelope_key=ENVELOPE_KEY,
        )

    assert load_events(events_path(tmp_path)) == list(first.events)


def test_later_label_rename_delivery_never_rewrites_prior_exact_name(tmp_path: Path) -> None:
    _initialize_git_repository(tmp_path)
    original = _capture(delivery="delivery-old-name")
    renamed = _capture(
        _pull_payload(label_name="renamed-later"),
        delivery="delivery-new-name",
    )

    ingest_github_capture(
        tmp_path,
        original,
        expected_repository_id="repo.widgets",
        expected_label_policy_hash=_expected_policy_hash(),
        envelope_key=ENVELOPE_KEY,
    )
    ingest_github_capture(
        tmp_path,
        renamed,
        expected_repository_id="repo.widgets",
        expected_label_policy_hash=_expected_policy_hash(),
        envelope_key=ENVELOPE_KEY,
    )

    names = [
        event.data["label_name_at_delivery"]
        for event in load_events(events_path(tmp_path))
        if event.kind == "provider_label_applied"
    ]
    assert set(names) == {"ruleloom:validation:positive", "renamed-later"}


def test_open_label_close_capture_finalizes_confirmatory_unit_and_materializes(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet", "-b", "main")
    _git(repo, "config", "user.name", "RuleLoom Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    base_sha = _commit(repo, "base.txt", "base\n", "2026-05-01T08:00:00Z")
    head_sha = _commit(repo, "feature.txt", "feature\n", "2026-05-02T09:00:00Z")
    initialize_project(repo, "GitHubCaptureLifecycle")
    from ruleloom.config import RuleLoomConfig

    config = RuleLoomConfig.load(repo)

    def payload(action: str) -> JsonObject:
        value = _pull_payload(action=action)
        pull = value["pull_request"]
        assert isinstance(pull, dict)
        pull["base"] = {"sha": base_sha}
        pull["head"] = {"sha": head_sha}
        if action == "closed":
            pull["merge_commit_sha"] = head_sha
        return value

    opened = _capture(
        payload("opened"),
        delivery="lifecycle-opened",
        received_at="2026-05-02T10:00:00Z",
        repository_id=config.protocol.repository_id,
    )
    labeled = _capture(
        payload("labeled"),
        delivery="lifecycle-labeled",
        received_at="2026-05-02T11:00:00Z",
        repository_id=config.protocol.repository_id,
    )
    closed = _capture(
        payload("closed"),
        delivery="lifecycle-closed",
        received_at="2026-05-02T12:00:00Z",
        repository_id=config.protocol.repository_id,
    )

    assert ingest_github_capture(
        repo,
        opened,
        expected_repository_id=config.protocol.repository_id,
        expected_label_policy_hash=_expected_policy_hash(),
        envelope_key=ENVELOPE_KEY,
    ) == (
        (2, 0),
        (0, 0),
    )
    assert ingest_github_capture(
        repo,
        labeled,
        expected_repository_id=config.protocol.repository_id,
        expected_label_policy_hash=_expected_policy_hash(),
        envelope_key=ENVELOPE_KEY,
    ) == (
        (3, 0),
        (0, 0),
    )
    assert ingest_github_capture(
        repo,
        closed,
        expected_repository_id=config.protocol.repository_id,
        expected_label_policy_hash=_expected_policy_hash(),
        envelope_key=ENVELOPE_KEY,
    ) == (
        (2, 0),
        (1, 0),
    )
    assert finalize_github_capture_units(
        repo,
        expected_repository_id=config.protocol.repository_id,
        expected_label_policy_hash=_expected_policy_hash(),
    ) == (0, 1)

    events, units = load_history_snapshot(events_path(repo), change_units_path(repo))
    assert len(units) == 1
    unit = units[0]
    assert unit.confirmatory is True
    assert unit.evidence_quality == "rich"
    assert unit.base_sha == base_sha
    assert unit.prediction_sha == head_sha
    assert unit.final_sha == head_sha

    report = materialize_history(repo, config, units, events)
    assert report.examined == 1
    assert report.positive == 1
    assert report.confirmatory == 1
    assert report.observations[0].labels[config.target] is LabelValue.POSITIVE


def test_finalize_rechecks_pins_for_events_inserted_outside_capture_ingest(
    tmp_path: Path,
) -> None:
    _initialize_git_repository(tmp_path)
    opened = _capture(
        _pull_payload(action="opened"),
        delivery="imported-opened",
        received_at="2026-05-02T10:00:00Z",
    )
    closed = _capture(
        _pull_payload(action="closed"),
        delivery="imported-closed",
        received_at="2026-05-02T12:00:00Z",
    )
    upsert_history_batch(
        events_path(tmp_path),
        (*opened.events, *closed.events),
        change_units_path(tmp_path),
        (),
    )

    with pytest.raises(GitHubWebhookCaptureError, match="independently frozen"):
        finalize_github_capture_units(
            tmp_path,
            expected_repository_id="repo.widgets",
            expected_label_policy_hash="0" * 64,
        )
    with pytest.raises(GitHubWebhookCaptureError, match="repository id"):
        finalize_github_capture_units(
            tmp_path,
            expected_repository_id="repo.other",
            expected_label_policy_hash=_expected_policy_hash(),
        )
    assert load_change_units(change_units_path(tmp_path)) == []

    assert finalize_github_capture_units(
        tmp_path,
        expected_repository_id="repo.widgets",
        expected_label_policy_hash=_expected_policy_hash(),
    ) == (1, 0)


def test_capture_refuses_to_upgrade_existing_archive_unit(tmp_path: Path) -> None:
    _initialize_git_repository(tmp_path)
    opened = _capture(
        _pull_payload(action="opened"),
        delivery="archive-conflict-opened",
    )
    repository_key = opened.provider_repository_key
    change_id = opened.events[0].change_id
    assert change_id is not None
    source_ref = f"github:{repository_key}:pull:42"
    archive_event = HistoricalEvent(
        id="event.archive.snapshot.42",
        repository_id="repo.widgets",
        kind="change_snapshot",
        occurred_at="2026-05-01T10:00:00Z",
        available_at="2026-05-01T10:00:00Z",
        provider="github",
        source_ref=f"{source_ref}:archive-snapshot",
        change_id=change_id,
        independent_group=change_id,
        data={
            "adapter": "ruleloom-github/1",
            "base_sha": BASE_SHA,
            "head_sha": HEAD_SHA,
            "commits": [HEAD_SHA],
            "point_in_time": False,
        },
    )
    archive_unit = ChangeUnit(
        id=change_id,
        repository_id="repo.widgets",
        kind="github_archive_change",
        base_sha=BASE_SHA,
        prediction_sha=HEAD_SHA,
        prediction_at=archive_event.occurred_at,
        commits=(HEAD_SHA,),
        event_ids=(archive_event.id,),
        provider="github",
        source_ref=source_ref,
        evidence_quality="git_only",
        confirmatory=False,
    )
    upsert_history_batch(
        events_path(tmp_path),
        (archive_event,),
        change_units_path(tmp_path),
        (archive_unit,),
    )

    with pytest.raises(
        GitHubWebhookCaptureError,
        match="cannot upgrade existing github_archive_change",
    ):
        ingest_github_capture(
            tmp_path,
            opened,
            expected_repository_id="repo.widgets",
            expected_label_policy_hash=_expected_policy_hash(),
            envelope_key=ENVELOPE_KEY,
        )

    events, units = load_history_snapshot(events_path(tmp_path), change_units_path(tmp_path))
    assert events == [archive_event]
    assert units == [archive_unit]


def test_directory_ingest_is_sorted_atomic_and_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _initialize_git_repository(root)
    inbox = tmp_path / "inbox"
    inbox.mkdir(mode=0o700)
    opened = _capture(
        _pull_payload(action="opened"),
        delivery="batch-opened",
        received_at="2026-05-02T10:00:00Z",
    )
    labeled = _capture(
        delivery="batch-labeled",
        received_at="2026-05-02T11:00:00Z",
    )
    closed = _capture(
        _pull_payload(action="closed"),
        delivery="batch-closed",
        received_at="2026-05-02T12:00:00Z",
    )
    write_github_capture_bundle(inbox / "20-labeled.json", labeled, envelope_key=ENVELOPE_KEY)
    write_github_capture_bundle(inbox / "10-opened.json", opened, envelope_key=ENVELOPE_KEY)
    write_github_capture_bundle(inbox / "30-closed.json", closed, envelope_key=ENVELOPE_KEY)
    write_github_capture_bundle(
        inbox / "40-exact-replay.json",
        labeled,
        envelope_key=ENVELOPE_KEY,
    )

    first = ingest_github_capture_directory(
        root,
        inbox,
        expected_repository_id="repo.widgets",
        expected_label_policy_hash=_expected_policy_hash(),
        envelope_key=ENVELOPE_KEY,
    )
    second = ingest_github_capture_directory(
        root,
        inbox,
        expected_repository_id="repo.widgets",
        expected_label_policy_hash=_expected_policy_hash(),
        envelope_key=ENVELOPE_KEY,
    )

    assert first.processed_bundles == (
        "10-opened.json",
        "20-labeled.json",
        "30-closed.json",
        "40-exact-replay.json",
    )
    assert first.unique_deliveries == 3
    assert first.duplicate_replays == 1
    assert (first.events_inserted, first.events_unchanged) == (7, 0)
    assert (first.units_inserted, first.units_unchanged) == (1, 0)
    assert (second.events_inserted, second.events_unchanged) == (0, 7)
    assert (second.units_inserted, second.units_unchanged) == (0, 1)
    events, units = load_history_snapshot(events_path(root), change_units_path(root))
    assert len(events) == 7
    assert len(units) == 1
    assert units[0].confirmatory is True


def test_directory_ingest_rejects_symlink_without_writing(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _initialize_git_repository(root)
    inbox = tmp_path / "inbox"
    inbox.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    bundle = outside / "capture.json"
    write_github_capture_bundle(bundle, _capture(), envelope_key=ENVELOPE_KEY)
    (inbox / "capture.json").symlink_to(bundle)

    with pytest.raises(GitHubWebhookCaptureError, match="must not be a symlink"):
        ingest_github_capture_directory(
            root,
            inbox,
            expected_repository_id="repo.widgets",
            expected_label_policy_hash=_expected_policy_hash(),
            envelope_key=ENVELOPE_KEY,
        )
    assert not (root / ".ruleloom/history/events.jsonl").exists()
    assert bundle.exists()


def test_directory_ingest_enforces_bundle_cap_before_writing(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _initialize_git_repository(root)
    inbox = tmp_path / "inbox"
    inbox.mkdir(mode=0o700)
    write_github_capture_bundle(
        inbox / "one.json",
        _capture(delivery="cap-one"),
        envelope_key=ENVELOPE_KEY,
    )
    write_github_capture_bundle(
        inbox / "two.json",
        _capture(delivery="cap-two"),
        envelope_key=ENVELOPE_KEY,
    )

    with pytest.raises(GitHubWebhookCaptureError, match="exceeds max_bundles=1"):
        ingest_github_capture_directory(
            root,
            inbox,
            expected_repository_id="repo.widgets",
            expected_label_policy_hash=_expected_policy_hash(),
            envelope_key=ENVELOPE_KEY,
            max_bundles=1,
        )
    assert not (root / ".ruleloom/history/events.jsonl").exists()


def test_directory_ingest_names_corrupt_bundle_and_writes_no_prefix(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _initialize_git_repository(root)
    inbox = tmp_path / "inbox"
    inbox.mkdir(mode=0o700)
    write_github_capture_bundle(
        inbox / "10-valid.json",
        _capture(),
        envelope_key=ENVELOPE_KEY,
    )
    (inbox / "20-corrupt.json").write_text('{"not":"a capture"}\n', encoding="utf-8")

    with pytest.raises(
        GitHubWebhookCaptureError,
        match=r"20-corrupt\.json.*failed verification.*no changes were written",
    ):
        ingest_github_capture_directory(
            root,
            inbox,
            expected_repository_id="repo.widgets",
            expected_label_policy_hash=_expected_policy_hash(),
            envelope_key=ENVELOPE_KEY,
        )
    assert not (root / ".ruleloom/history/events.jsonl").exists()
    assert (inbox / "10-valid.json").exists()
    assert (inbox / "20-corrupt.json").exists()


def test_directory_ingest_pins_configured_repository_before_first_write(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _initialize_git_repository(root)
    inbox = tmp_path / "inbox"
    inbox.mkdir(mode=0o700)
    write_github_capture_bundle(
        inbox / "capture.json",
        _capture(),
        envelope_key=ENVELOPE_KEY,
    )

    with pytest.raises(
        GitHubWebhookCaptureError,
        match=r"capture\.json.*no changes were written.*repo\.widgets.*repo\.other",
    ):
        ingest_github_capture_directory(
            root,
            inbox,
            expected_repository_id="repo.other",
            expected_label_policy_hash=_expected_policy_hash(),
            envelope_key=ENVELOPE_KEY,
        )

    assert not events_path(root).exists()
    assert not change_units_path(root).exists()


def test_directory_ingest_pins_policy_independently_before_first_write(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _initialize_git_repository(root)
    inbox = tmp_path / "inbox"
    inbox.mkdir(mode=0o700)
    write_github_capture_bundle(
        inbox / "capture.json",
        _capture(),
        envelope_key=ENVELOPE_KEY,
    )

    with pytest.raises(
        GitHubWebhookCaptureError,
        match=r"capture\.json.*experiment pinning.*no changes.*independently frozen",
    ):
        ingest_github_capture_directory(
            root,
            inbox,
            expected_repository_id="repo.widgets",
            expected_label_policy_hash="0" * 64,
            envelope_key=ENVELOPE_KEY,
        )

    assert not events_path(root).exists()
    assert not change_units_path(root).exists()


def test_directory_ingest_preflights_conflicting_delivery_before_writing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _initialize_git_repository(root)
    inbox = tmp_path / "inbox"
    inbox.mkdir(mode=0o700)
    first = _capture(delivery="reused-delivery")
    changed = _capture(
        _pull_payload(label_name="changed-under-same-delivery"),
        delivery="reused-delivery",
    )
    write_github_capture_bundle(
        inbox / "10-first.json",
        first,
        envelope_key=ENVELOPE_KEY,
    )
    write_github_capture_bundle(
        inbox / "20-conflict.json",
        changed,
        envelope_key=ENVELOPE_KEY,
    )

    with pytest.raises(
        GitHubWebhookCaptureError,
        match=r"20-conflict\.json.*conflicts.*no changes were written",
    ):
        ingest_github_capture_directory(
            root,
            inbox,
            expected_repository_id="repo.widgets",
            expected_label_policy_hash=_expected_policy_hash(),
            envelope_key=ENVELOPE_KEY,
        )
    assert not (root / ".ruleloom/history/events.jsonl").exists()


def test_bundle_write_load_and_mac_verification_are_replay_safe(tmp_path: Path) -> None:
    capture = _capture()
    tmp_path.chmod(0o700)
    path = tmp_path / "capture.json"

    assert write_github_capture_bundle(path, capture, envelope_key=ENVELOPE_KEY) is True
    assert write_github_capture_bundle(path, capture, envelope_key=ENVELOPE_KEY) is False
    assert path.stat().st_mode & 0o777 == 0o600
    assert load_github_capture_bundle(path, envelope_key=ENVELOPE_KEY) == capture
    with pytest.raises(GitHubWebhookCaptureError, match="envelope MAC"):
        load_github_capture_bundle(path, envelope_key=OTHER_ENVELOPE_KEY)

    changed = replace(capture.events[-1], available_at="2026-05-02T12:00:01Z")
    raw = capture.to_dict()
    raw["events"] = [*raw["events"][:-1], changed.to_dict()]  # type: ignore[index]
    with pytest.raises(GitHubWebhookCaptureError, match=r"availability time|envelope hash"):
        GitHubWebhookCapture.from_dict(raw)

    invalid_mac = replace(capture, envelope_mac_sha256="0" * 64)
    with pytest.raises(GitHubWebhookCaptureError, match="envelope MAC"):
        write_github_capture_bundle(
            tmp_path / "invalid-mac.json",
            invalid_mac,
            envelope_key=ENVELOPE_KEY,
        )
    assert not (tmp_path / "invalid-mac.json").exists()


def test_event_and_bundle_symlinks_are_rejected(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    event_file = tmp_path / "event.json"
    event_file.write_bytes(_bytes(_pull_payload()))
    event_link = tmp_path / "event-link.json"
    event_link.symlink_to(event_file)

    with pytest.raises(GitHubWebhookCaptureError, match="non-symlink"):
        capture_github_actions_event_file(
            event_link,
            event_name="pull_request",
            run_id=123,
            received_at=RECEIVED_AT,
            repository_id="repo.widgets",
            expected_provider_repository_id=101,
            identity_key=IDENTITY_KEY,
            envelope_key=ENVELOPE_KEY,
        )

    outside = tmp_path / "outside.json"
    outside.write_text("do not overwrite", encoding="utf-8")
    bundle_link = tmp_path / "capture.json"
    bundle_link.symlink_to(outside)
    with pytest.raises(GitHubWebhookCaptureError, match="non-symlink"):
        write_github_capture_bundle(bundle_link, _capture(), envelope_key=ENVELOPE_KEY)
    assert outside.read_text(encoding="utf-8") == "do not overwrite"


def test_actions_capture_is_honest_about_runner_context_and_has_stable_run_identity() -> None:
    raw = _bytes(_pull_payload(action="opened"))

    capture = capture_github_actions_event(
        raw,
        event_name="pull_request",
        run_id=123456,
        received_at=RECEIVED_AT,
        repository_id="repo.widgets",
        expected_provider_repository_id=101,
        identity_key=IDENTITY_KEY,
        envelope_key=ENVELOPE_KEY,
    )

    assert capture.transport == "github_actions_event_file"
    assert capture.delivery_id == "actions-123456"
    assert capture.signature_verified is False
    assert capture.events[0].data["capture"]["signature_verified"] is False  # type: ignore[index]
    capture.verify(ENVELOPE_KEY)


@pytest.mark.parametrize(
    ("payload", "event_name", "message"),
    [
        (
            _pull_payload(action="opened")
            | {
                "pull_request": _pull_payload(action="opened")["pull_request"]
                | {"created_at": "2026-05-03T00:00:00Z"}  # type: ignore[operator]
            },
            "pull_request",
            "cannot postdate capture time",
        ),
        (
            {
                "action": "submitted",
                "repository": {"id": 101, "full_name": "acme/widgets"},
                "sender": {"id": 7},
                "pull_request": {
                    "number": 42,
                    "user": {"id": 9},
                    "created_at": "2026-05-01T00:00:00Z",
                },
                "review": {
                    "id": 55,
                    "user": {"id": 7},
                    "submitted_at": "2026-05-03T00:00:00Z",
                    "state": "approved",
                    "commit_id": HEAD_SHA,
                },
            },
            "pull_request_review",
            "cannot postdate capture time",
        ),
        (
            {
                "action": "completed",
                "repository": {"id": 101, "full_name": "acme/widgets"},
                "sender": {"id": 7},
                "check_run": {
                    "id": 71,
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": "2026-05-03T00:00:00Z",
                    "head_sha": HEAD_SHA,
                    "name": "tests",
                    "app": {"id": 3},
                    "pull_requests": [{"number": 42}],
                },
            },
            "check_run",
            "cannot postdate capture time",
        ),
    ],
)
def test_provider_timestamps_cannot_postdate_capture(
    payload: JsonObject,
    event_name: str,
    message: str,
) -> None:
    raw = _bytes(payload)
    with pytest.raises(GitHubWebhookCaptureError, match=message):
        capture_github_webhook(
            raw,
            _headers(raw, event=event_name),
            received_at=RECEIVED_AT,
            repository_id="repo.widgets",
            expected_provider_repository_id=101,
            webhook_secret=WEBHOOK_SECRET,
            identity_key=IDENTITY_KEY,
            envelope_key=ENVELOPE_KEY,
        )


def test_review_and_check_capture_keep_actor_author_and_check_names_pseudonymous() -> None:
    review_payload: JsonObject = {
        "action": "submitted",
        "repository": {"id": 101, "full_name": "acme/widgets"},
        "sender": {"id": 7, "login": "review-actor"},
        "pull_request": {
            "number": 42,
            "user": {"id": 9, "login": "pull-author"},
            "created_at": "2026-05-01T00:00:00Z",
        },
        "review": {
            "id": 55,
            "user": {"id": 7, "login": "reviewer"},
            "submitted_at": "2026-05-02T11:00:00Z",
            "state": "changes_requested",
            "commit_id": HEAD_SHA,
            "body": "free form content must not be copied",
        },
    }
    review_raw = _bytes(review_payload)
    review = capture_github_webhook(
        review_raw,
        _headers(review_raw, event="pull_request_review", delivery="review-1"),
        received_at=RECEIVED_AT,
        repository_id="repo.widgets",
        expected_provider_repository_id=101,
        webhook_secret=WEBHOOK_SECRET,
        identity_key=IDENTITY_KEY,
        envelope_key=ENVELOPE_KEY,
    )
    review_event = review.events[-1]
    assert review_event.kind == "review"
    assert review_event.data["actor_key"] == review_event.data["reviewer_key"]
    assert review_event.data["actor_key"] != review_event.data["author_key"]
    assert review_event.data["category"] == "unspecified"
    assert "free form" not in canonical_json(review.to_dict())

    check_payload: JsonObject = {
        "action": "completed",
        "repository": {"id": 101, "full_name": "acme/widgets"},
        "sender": {"id": 7},
        "check_run": {
            "id": 71,
            "status": "completed",
            "conclusion": "failure",
            "completed_at": "2026-05-02T11:30:00Z",
            "head_sha": HEAD_SHA,
            "name": "Secret Internal Check Name",
            "app": {"id": 3},
            "pull_requests": [{"number": 42}],
        },
    }
    check_raw = _bytes(check_payload)
    check = capture_github_webhook(
        check_raw,
        _headers(check_raw, event="check_run", delivery="check-1"),
        received_at=RECEIVED_AT,
        repository_id="repo.widgets",
        expected_provider_repository_id=101,
        webhook_secret=WEBHOOK_SECRET,
        identity_key=IDENTITY_KEY,
        envelope_key=ENVELOPE_KEY,
    )
    check_event = check.events[-1]
    assert check_event.kind == "ci_run"
    assert check_event.data["attributable_to_change"] is False
    assert "Secret Internal Check Name" not in canonical_json(check.to_dict())


def test_repository_label_edit_records_old_and_new_names_without_outcome() -> None:
    payload: JsonObject = {
        "action": "edited",
        "repository": {"id": 101, "full_name": "acme/widgets"},
        "sender": {"id": 7},
        "label": {"id": 88, "name": "new-name"},
        "changes": {"name": {"from": "old-name"}},
    }
    raw = _bytes(payload)

    capture = capture_github_webhook(
        raw,
        _headers(raw, event="label", delivery="label-edit-1"),
        received_at=RECEIVED_AT,
        repository_id="repo.widgets",
        expected_provider_repository_id=101,
        webhook_secret=WEBHOOK_SECRET,
        identity_key=IDENTITY_KEY,
        envelope_key=ENVELOPE_KEY,
        label_policy=_policy(),
    )

    assert [event.kind for event in capture.events] == [
        "provider_delivery",
        "provider_label_definition",
    ]
    assert capture.events[-1].data["previous_name_at_delivery"] == "old-name"
    assert capture.events[-1].data["label_name_at_delivery"] == "new-name"


def test_label_policy_parser_is_strict_and_hashable() -> None:
    policy = parse_github_label_policy(
        json.dumps(
            {
                "schema_version": 1,
                "labels": [
                    {
                        "name": "ruleloom:validation:positive",
                        "target": "validation_rework_required",
                        "value": "positive",
                        "evidence_complete": True,
                        "authorized_actor_ids": [7],
                    }
                ],
            }
        )
    )
    assert policy == _policy()
    assert github_label_policy_hash(policy, IDENTITY_KEY) == _capture().label_policy_hash
    assert github_label_policy_hash(policy, IDENTITY_KEY) != github_label_policy_hash(
        policy,
        b"another-identity-key-long-enough",
    )

    with pytest.raises(GitHubWebhookCaptureError, match="names must be unique"):
        parse_github_label_policy(
            json.dumps(
                {
                    "schema_version": 1,
                    "labels": [policy[0].to_dict(), policy[0].to_dict()],
                }
            )
        )
    with pytest.raises(GitHubWebhookCaptureError, match="field"):
        parse_github_label_policy(
            json.dumps(
                {
                    "schema_version": 1,
                    "labels": [{**policy[0].to_dict(), "unexpected": True}],
                }
            )
        )


def test_archive_and_webhook_identity_pins_agree_only_for_same_numeric_repository() -> None:
    webhook = _capture().events[0]
    repository_key = _capture().provider_repository_key
    archive = HistoricalEvent(
        id="event.archive.same",
        repository_id="repo.widgets",
        kind="revert",
        occurred_at="2026-05-01T00:00:00Z",
        available_at="2026-05-01T00:00:00Z",
        provider="github",
        source_ref=f"github:{repository_key}:commit:{HEAD_SHA}",
        independent_group="archive.same",
        data={"adapter": "ruleloom-github/1"},
    )
    validate_history_snapshot([archive, webhook], [])

    other = _capture(
        _pull_payload(repository_id=202),
        delivery="other-repository",
        expected_provider_repository_id=202,
    )
    other_event = replace(other.events[0], repository_id="repo.widgets")
    with pytest.raises(ModelError, match="multiple built-in GitHub repository identities"):
        validate_history_snapshot([archive, other_event], [])


def test_webhook_adapter_cannot_launder_archive_source_ref() -> None:
    capture = _capture()
    event = capture.events[0]
    invalid = replace(
        event,
        source_ref=f"github:{capture.provider_repository_key}:commit:{HEAD_SHA}",
        data={**event.data, "adapter": GITHUB_WEBHOOK_ADAPTER_VERSION},
    )

    with pytest.raises(ModelError, match="invalid provider provenance"):
        validate_history_snapshot([invalid], [])


def test_composite_action_wrapper_captures_without_caller_python_path(tmp_path: Path) -> None:
    event_path = tmp_path / "event.json"
    event_path.write_bytes(_bytes(_pull_payload(action="opened")))
    output_directory = tmp_path / "captures"
    output_directory.mkdir(mode=0o700)
    script = Path(__file__).resolve().parents[1] / "integrations" / "github-action" / "capture.py"
    environment = {
        "GITHUB_EVENT_PATH": str(event_path),
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_RUN_ID": "9001",
        "GITHUB_REPOSITORY_ID": "101",
        "RULELOOM_REPOSITORY_ID": "repo.widgets",
        "RULELOOM_CAPTURE_OUTPUT_DIRECTORY": str(output_directory),
        "RULELOOM_CAPTURE_IDENTITY_KEY": IDENTITY_KEY.decode(),
        "RULELOOM_CAPTURE_ENVELOPE_KEY": ENVELOPE_KEY.decode(),
        "RULELOOM_CAPTURE_LABEL_POLICY_JSON": '{"schema_version":1,"labels":[]}',
    }

    first = subprocess.run(
        [sys.executable, "-I", str(script)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert first.returncode == 0, first.stderr
    capture_path = output_directory / "github-actions-101-9001.json"
    first_bundle = capture_path.read_bytes()
    second = subprocess.run(
        [sys.executable, "-I", str(script)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert second.returncode == 0, second.stderr
    assert "capture-created=true" in first.stdout
    assert "capture-created=false" in second.stdout
    assert capture_path.read_bytes() == first_bundle
    capture = load_github_capture_bundle(capture_path, envelope_key=ENVELOPE_KEY)
    assert capture.transport == "github_actions_event_file"
    assert capture.signature_verified is False


def test_action_template_has_no_checkout_or_unpinned_dependency() -> None:
    root = Path(__file__).resolve().parents[1]
    action = (root / "integrations/github-action/action.yml").read_text(encoding="utf-8")
    workflow = (root / "integrations/github-action/example-workflow.yml").read_text(
        encoding="utf-8"
    )

    assert "actions/checkout" not in action
    assert "uses:" not in action
    assert 'python3 -I "$GITHUB_ACTION_PATH/capture.py"' in action
    assert "permissions: {}" in workflow
    assert "FULL_40_CHARACTER_COMMIT_SHA" in workflow
    assert "actions/checkout" not in workflow


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"target": "unregistered_outcome"}, "unsupported.*target"),
        ({"value": "unknown"}, "must be positive or negative"),
        ({"evidence_complete": False}, "evidence_complete=true"),
        ({"authorized_actor_ids": []}, "at least one authorized actor"),
        ({"authorized_actor_ids": [7, 7]}, "cannot contain duplicates"),
        ({"authorized_actor_ids": "7"}, "must be an array"),
        ({"evidence_complete": "yes"}, "must be a boolean"),
    ],
)
def test_label_policy_declarations_fail_closed(
    changes: JsonObject,
    message: str,
) -> None:
    declaration: JsonObject = {
        "name": "ruleloom:validation:positive",
        "target": "validation_rework_required",
        "value": "positive",
        "evidence_complete": True,
        "authorized_actor_ids": [7],
    }
    declaration.update(changes)

    with pytest.raises(GitHubWebhookCaptureError, match=message):
        GitHubLabelOutcome.from_dict(declaration)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"{}", "must be UTF-8 JSON text"),
        ("{" + "x" * 65_536 + "}", "exceeds"),
        ("{", "invalid GitHub label policy"),
        ("[]", "must contain only"),
        ('{"schema_version":2,"labels":[]}', "unsupported.*schema"),
        ('{"schema_version":true,"labels":[]}', "unsupported.*schema"),
        ('{"schema_version":1.0,"labels":[]}', "unsupported.*schema"),
        ('{"schema_version":1,"labels":{}}', "labels must be an array"),
        ('{"schema_version":1,"labels":[false]}', "must be an object"),
    ],
)
def test_label_policy_document_is_bounded_and_strict(
    content: object,
    message: str,
) -> None:
    with pytest.raises(GitHubWebhookCaptureError, match=message):
        parse_github_label_policy(content)  # type: ignore[arg-type]


def test_public_capture_inputs_fail_with_domain_errors() -> None:
    raw = _bytes(_pull_payload(action="opened"))

    with pytest.raises(GitHubWebhookCaptureError, match="run id must be a positive integer"):
        capture_github_actions_event(
            raw,
            event_name="pull_request",
            run_id=0,
            received_at=RECEIVED_AT,
            repository_id="repo.widgets",
            expected_provider_repository_id=101,
            identity_key=IDENTITY_KEY,
            envelope_key=ENVELOPE_KEY,
        )
    with pytest.raises(GitHubWebhookCaptureError, match="identity key"):
        capture_github_actions_event(
            raw,
            event_name="pull_request",
            run_id=1,
            received_at=RECEIVED_AT,
            repository_id="repo.widgets",
            expected_provider_repository_id=101,
            identity_key=b"short",
            envelope_key=ENVELOPE_KEY,
        )
    with pytest.raises(GitHubWebhookCaptureError, match="unsupported GitHub webhook event"):
        capture_github_actions_event(
            raw,
            event_name="push",
            run_id=1,
            received_at=RECEIVED_AT,
            repository_id="repo.widgets",
            expected_provider_repository_id=101,
            identity_key=IDENTITY_KEY,
            envelope_key=ENVELOPE_KEY,
        )
    with pytest.raises(GitHubWebhookCaptureError, match="label policy names must be unique"):
        capture_github_actions_event(
            raw,
            event_name="pull_request",
            run_id=1,
            received_at=RECEIVED_AT,
            repository_id="repo.widgets",
            expected_provider_repository_id=101,
            identity_key=IDENTITY_KEY,
            envelope_key=ENVELOPE_KEY,
            label_policy=(*_policy(), *_policy()),
        )
    with pytest.raises(GitHubWebhookCaptureError, match="cannot be empty"):
        capture_github_actions_event(
            b"",
            event_name="pull_request",
            run_id=1,
            received_at=RECEIVED_AT,
            repository_id="repo.widgets",
            expected_provider_repository_id=101,
            identity_key=IDENTITY_KEY,
            envelope_key=ENVELOPE_KEY,
        )
    with pytest.raises(GitHubWebhookCaptureError, match="must be UTF-8"):
        capture_github_actions_event(
            b"\xff",
            event_name="pull_request",
            run_id=1,
            received_at=RECEIVED_AT,
            repository_id="repo.widgets",
            expected_provider_repository_id=101,
            identity_key=IDENTITY_KEY,
            envelope_key=ENVELOPE_KEY,
        )


def test_webhook_header_and_signature_shapes_fail_closed() -> None:
    raw = _bytes(_pull_payload())
    missing = _headers(raw)
    del missing["X-GitHub-Event"]
    with pytest.raises(GitHubWebhookCaptureError, match="missing required"):
        capture_github_webhook(
            raw,
            missing,
            received_at=RECEIVED_AT,
            repository_id="repo.widgets",
            expected_provider_repository_id=101,
            webhook_secret=WEBHOOK_SECRET,
            identity_key=IDENTITY_KEY,
            envelope_key=ENVELOPE_KEY,
        )

    malformed = _headers(raw)
    malformed["X-Hub-Signature-256"] = "sha1=bad"
    with pytest.raises(GitHubWebhookCaptureError, match="signature is malformed"):
        capture_github_webhook(
            raw,
            malformed,
            received_at=RECEIVED_AT,
            repository_id="repo.widgets",
            expected_provider_repository_id=101,
            webhook_secret=WEBHOOK_SECRET,
            identity_key=IDENTITY_KEY,
            envelope_key=ENVELOPE_KEY,
        )

    with pytest.raises(GitHubWebhookCaptureError, match="headers must be strings"):
        capture_github_webhook(
            raw,
            {1: "value"},  # type: ignore[dict-item]
            received_at=RECEIVED_AT,
            repository_id="repo.widgets",
            expected_provider_repository_id=101,
            webhook_secret=WEBHOOK_SECRET,
            identity_key=IDENTITY_KEY,
            envelope_key=ENVELOPE_KEY,
        )
    with pytest.raises(GitHubWebhookCaptureError, match="header is malformed"):
        capture_github_webhook(
            raw,
            {"bad\nheader": "value"},
            received_at=RECEIVED_AT,
            repository_id="repo.widgets",
            expected_provider_repository_id=101,
            webhook_secret=WEBHOOK_SECRET,
            identity_key=IDENTITY_KEY,
            envelope_key=ENVELOPE_KEY,
        )
    with pytest.raises(GitHubWebhookCaptureError, match="payload must be raw bytes"):
        verify_github_webhook_signature(  # type: ignore[arg-type]
            "not-bytes",
            "sha256=" + "0" * 64,
            WEBHOOK_SECRET,
        )


def test_review_check_and_closed_variants_remain_structural() -> None:
    review_payload: JsonObject = {
        "action": "edited",
        "repository": {"id": 101, "full_name": "acme/widgets"},
        "sender": {"id": 11},
        "pull_request": {
            "number": 42,
            "user": {"id": 9},
            "created_at": "2026-05-01T00:00:00Z",
        },
        "review": {
            "id": 55,
            "user": {"id": 7},
            "submitted_at": "2026-05-02T11:00:00Z",
            "state": "novel-provider-state",
        },
    }
    review_raw = _bytes(review_payload)
    review = capture_github_webhook(
        review_raw,
        _headers(review_raw, event="pull_request_review", delivery="review-edited"),
        received_at=RECEIVED_AT,
        repository_id="repo.widgets",
        expected_provider_repository_id=101,
        webhook_secret=WEBHOOK_SECRET,
        identity_key=IDENTITY_KEY,
        envelope_key=ENVELOPE_KEY,
    )
    assert review.events[-1].kind == "provider_review_changed"
    assert review.events[-1].data["decision"] == "unspecified"
    assert "commit_sha" not in review.events[-1].data

    check_payload: JsonObject = {
        "action": "completed",
        "repository": {"id": 101, "full_name": "acme/widgets"},
        "sender": {"id": 7},
        "check_run": {
            "id": 71,
            "status": "completed",
            "conclusion": "success",
            "completed_at": "2026-05-02T11:30:00Z",
            "head_sha": HEAD_SHA,
            "name": "tests",
            "app": {"id": 3},
            "pull_requests": [],
        },
    }
    check_raw = _bytes(check_payload)
    check = capture_github_webhook(
        check_raw,
        _headers(check_raw, event="check_run", delivery="check-no-pull"),
        received_at=RECEIVED_AT,
        repository_id="repo.widgets",
        expected_provider_repository_id=101,
        webhook_secret=WEBHOOK_SECRET,
        identity_key=IDENTITY_KEY,
        envelope_key=ENVELOPE_KEY,
    )
    assert [event.kind for event in check.events] == ["provider_delivery"]
    assert check.events[0].change_id is None

    closed_payload = _pull_payload(action="closed")
    pull = closed_payload["pull_request"]
    assert isinstance(pull, dict)
    pull["merged"] = False
    pull.pop("merged_at")
    closed = _capture(closed_payload, delivery="closed-without-merge")
    assert closed.events[-1].kind == "change_closed"
    assert "merge_sha" not in closed.events[-1].data

    malformed_closed = _pull_payload(action="closed")
    malformed_pull = malformed_closed["pull_request"]
    assert isinstance(malformed_pull, dict)
    malformed_pull.pop("merged")
    with pytest.raises(GitHubWebhookCaptureError, match=r"pull_request\.merged must be a boolean"):
        _capture(malformed_closed, delivery="closed-missing-merged")


@pytest.mark.parametrize(
    ("event_name", "payload", "message"),
    [
        ("pull_request", _pull_payload(action="assigned"), "unsupported.*pull_request action"),
        (
            "pull_request_review",
            {
                "action": "created",
                "repository": {"id": 101, "full_name": "acme/widgets"},
            },
            "unsupported.*review action",
        ),
        (
            "check_run",
            {
                "action": "requested_action",
                "repository": {"id": 101, "full_name": "acme/widgets"},
            },
            "unsupported.*check_run action",
        ),
        (
            "label",
            {
                "action": "transferred",
                "repository": {"id": 101, "full_name": "acme/widgets"},
            },
            "unsupported.*label action",
        ),
    ],
)
def test_unsupported_provider_actions_are_rejected(
    event_name: str,
    payload: JsonObject,
    message: str,
) -> None:
    raw = _bytes(payload)
    with pytest.raises(GitHubWebhookCaptureError, match=message):
        capture_github_webhook(
            raw,
            _headers(raw, event=event_name),
            received_at=RECEIVED_AT,
            repository_id="repo.widgets",
            expected_provider_repository_id=101,
            webhook_secret=WEBHOOK_SECRET,
            identity_key=IDENTITY_KEY,
            envelope_key=ENVELOPE_KEY,
        )


def test_bundle_parser_rejects_shape_encoding_and_post_creation_tampering(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    capture = _capture()

    invalid_utf8 = tmp_path / "invalid-utf8.json"
    invalid_utf8.write_bytes(b"\xff\n")
    with pytest.raises(GitHubWebhookCaptureError, match="must be UTF-8"):
        load_github_capture_bundle(invalid_utf8, envelope_key=ENVELOPE_KEY)

    multiple_lines = tmp_path / "multiple-lines.json"
    multiple_lines.write_text("{}\n{}\n", encoding="utf-8")
    with pytest.raises(GitHubWebhookCaptureError, match="one JSON line"):
        load_github_capture_bundle(multiple_lines, envelope_key=ENVELOPE_KEY)

    invalid_json = tmp_path / "invalid-json.json"
    invalid_json.write_text("{\n", encoding="utf-8")
    with pytest.raises(GitHubWebhookCaptureError, match="invalid GitHub capture bundle"):
        load_github_capture_bundle(invalid_json, envelope_key=ENVELOPE_KEY)

    raw = capture.to_dict()
    raw.pop("events")
    with pytest.raises(GitHubWebhookCaptureError, match="invalid field set"):
        GitHubWebhookCapture.from_dict(raw)

    object.__setattr__(capture, "envelope_sha256", "0" * 64)
    with pytest.raises(GitHubWebhookCaptureError, match="envelope hash"):
        capture.verify(ENVELOPE_KEY)
    with pytest.raises(GitHubWebhookCaptureError, match="changed after creation"):
        capture.to_dict()


def test_bundle_nested_capture_provenance_is_bound_to_top_level() -> None:
    capture = _capture()
    raw = capture.to_dict()
    events = raw["events"]
    assert isinstance(events, list)
    first = events[0]
    assert isinstance(first, dict)
    data = first["data"]
    assert isinstance(data, dict)
    nested = data["capture"]
    assert isinstance(nested, dict)
    nested["event_name"] = "check_run"

    envelope = {key: value for key, value in raw.items() if not key.startswith("envelope_")}
    raw["envelope_sha256"] = content_hash(envelope)
    raw["envelope_mac_sha256"] = hmac.new(
        ENVELOPE_KEY,
        b"ruleloom-github-capture-envelope-v1\x00" + canonical_json(envelope).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    with pytest.raises(GitHubWebhookCaptureError, match="inconsistent provenance"):
        GitHubWebhookCapture.from_dict(raw)


def test_bundle_output_and_inbox_directory_permissions_fail_closed(tmp_path: Path) -> None:
    missing_parent = tmp_path / "missing" / "capture.json"
    with pytest.raises(GitHubWebhookCaptureError, match="must already exist"):
        write_github_capture_bundle(
            missing_parent,
            _capture(),
            envelope_key=ENVELOPE_KEY,
        )

    insecure = tmp_path / "insecure"
    insecure.mkdir(mode=0o777)
    insecure.chmod(0o777)
    with pytest.raises(GitHubWebhookCaptureError, match="group/world writable"):
        write_github_capture_bundle(
            insecure / "capture.json",
            _capture(),
            envelope_key=ENVELOPE_KEY,
        )

    root = tmp_path / "repo"
    root.mkdir()
    _initialize_git_repository(root)
    inbox = tmp_path / "empty-inbox"
    inbox.mkdir(mode=0o700)
    report = ingest_github_capture_directory(
        root,
        inbox,
        expected_repository_id="repo.widgets",
        expected_label_policy_hash=_expected_policy_hash(),
        envelope_key=ENVELOPE_KEY,
    )
    assert report.to_dict()["bundles_examined"] == 0

    with pytest.raises(GitHubWebhookCaptureError, match="at least 16 bytes"):
        ingest_github_capture_directory(
            root,
            inbox,
            expected_repository_id="repo.widgets",
            expected_label_policy_hash=_expected_policy_hash(),
            envelope_key=b"short",
        )
    with pytest.raises(GitHubWebhookCaptureError, match="lowercase SHA-256"):
        ingest_github_capture_directory(
            root,
            inbox,
            expected_repository_id="repo.widgets",
            expected_label_policy_hash="A" * 64,
            envelope_key=ENVELOPE_KEY,
        )

    with pytest.raises(GitHubWebhookCaptureError, match="max_bundles must be between"):
        ingest_github_capture_directory(
            root,
            inbox,
            expected_repository_id="repo.widgets",
            expected_label_policy_hash=_expected_policy_hash(),
            envelope_key=ENVELOPE_KEY,
            max_bundles=0,
        )
    (inbox / "notes.txt").write_text("not a bundle", encoding="utf-8")
    with pytest.raises(GitHubWebhookCaptureError, match="unsafe/non-bundle"):
        ingest_github_capture_directory(
            root,
            inbox,
            expected_repository_id="repo.widgets",
            expected_label_policy_hash=_expected_policy_hash(),
            envelope_key=ENVELOPE_KEY,
        )


def test_directory_ingest_caps_cumulative_bytes_and_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _initialize_git_repository(root)
    inbox = tmp_path / "inbox"
    inbox.mkdir(mode=0o700)
    write_github_capture_bundle(
        inbox / "capture.json",
        _capture(),
        envelope_key=ENVELOPE_KEY,
    )

    monkeypatch.setattr(github_webhooks, "MAX_GITHUB_CAPTURE_BATCH_BYTES", 1)
    with pytest.raises(GitHubWebhookCaptureError, match="cumulative byte limit"):
        ingest_github_capture_directory(
            root,
            inbox,
            expected_repository_id="repo.widgets",
            expected_label_policy_hash=_expected_policy_hash(),
            envelope_key=ENVELOPE_KEY,
        )
    assert not events_path(root).exists()

    monkeypatch.setattr(github_webhooks, "MAX_GITHUB_CAPTURE_BATCH_BYTES", 64 * 1024 * 1024)
    monkeypatch.setattr(github_webhooks, "MAX_GITHUB_CAPTURE_BATCH_EVENTS", 1)
    with pytest.raises(GitHubWebhookCaptureError, match="normalized event limit"):
        ingest_github_capture_directory(
            root,
            inbox,
            expected_repository_id="repo.widgets",
            expected_label_policy_hash=_expected_policy_hash(),
            envelope_key=ENVELOPE_KEY,
        )
    assert not events_path(root).exists()
