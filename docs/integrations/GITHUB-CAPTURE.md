# Point-in-time GitHub capture

RuleLoom's archive importer cannot prove what a mutable GitHub label was named
when it was applied. The point-in-time adapter closes that specific gap by
normalizing a delivery while its exact payload is available. It is independent
of programming language, build system, and repository layout.

The adapter has no network client and never executes repository code. It accepts
only `pull_request`, `pull_request_review`, `check_run`, and repository `label`
deliveries. Unsupported actions fail closed. Bodies, titles, comments, login
names, review prose, and check names are not retained. Provider user and check
identities are pseudonymized with a caller-owned key.

## Trust models

There are two deliberately different entry points:

- `capture_github_webhook` verifies `X-Hub-Signature-256` against the exact raw
  bytes before parsing. GitHub's HMAC covers the body, not the delivery/event
  headers; TLS and the receiver still protect those transport fields.
- `capture_github_actions_event_file` safely reads `GITHUB_EVENT_PATH` but marks
  `signature_verified=false`. RuleLoom cannot reconstruct a provider webhook
  signature inside an Actions job. Trust is placed in the pinned action, runner,
  workflow definition, and event file.

Both paths require a separate envelope key. The resulting bundle contains a
SHA-256 content hash and HMAC over the complete normalized event set. A bundle
must pass both checks before replay or ingestion. The raw payload is hashed but
not copied into the bundle.

Use independent high-entropy values for the webhook secret, identity key, and
envelope key. Rotating the identity key changes pseudonyms and therefore starts
a new identity namespace. Rotating the envelope key requires keeping the old key
available to verify old bundles or explicitly re-enveloping them in a separately
audited migration.

## Direct webhook API

Record the receive time at ingress, before asynchronous processing:

```python
from pathlib import Path

from ruleloom.history import (
    GitHubLabelOutcome,
    capture_github_webhook,
    finalize_github_capture_units,
    github_label_policy_hash,
    ingest_github_capture,
    ingest_github_capture_directory,
    write_github_capture_bundle,
)

policy = (
    GitHubLabelOutcome(
        name="ruleloom:validation:positive",
        target="validation_rework_required",
        value="positive",
        evidence_complete=True,
        authorized_actor_ids=frozenset({123456}),
    ),
)

# Compute this before the pilot, record it in a reviewed experiment manifest,
# and give the later ingestion observer only the frozen hash (not the identity
# key used by this capture process).
expected_label_policy_hash = github_label_policy_hash(policy, identity_key)

capture = capture_github_webhook(
    raw_request_body,
    request_headers,
    received_at=receive_time,
    repository_id="repository.example",
    expected_provider_repository_id=987654,
    webhook_secret=webhook_secret,
    identity_key=identity_key,
    envelope_key=envelope_key,
    label_policy=policy,
)

write_github_capture_bundle(bundle_path, capture, envelope_key=envelope_key)
event_counts, unit_counts = ingest_github_capture(
    Path("/path/to/repository"),
    capture,
    expected_repository_id="repository.example",
    expected_label_policy_hash=expected_label_policy_hash,
    envelope_key=envelope_key,
)

# Safe idempotent convergence pass after a delivery batch. The convergence
# helper rechecks the same independent pins before creating any unit.
finalized_units = finalize_github_capture_units(
    Path("/path/to/repository"),
    expected_repository_id="repository.example",
    expected_label_policy_hash=expected_label_policy_hash,
)

# Or ingest a durable Action inbox in one bounded atomic batch.
batch = ingest_github_capture_directory(
    Path("/path/to/repository"),
    Path("/var/lib/ruleloom/github-captures"),
    expected_repository_id="repository.example",
    expected_label_policy_hash=expected_label_policy_hash,
    envelope_key=envelope_key,
    max_bundles=1000,
)
print(batch.to_dict())
```

`repository.id`, not mutable `owner/name`, pins provider identity. The current
`repository.full_name` is preserved only as point-in-time provenance. Archive
and webhook records for different pull requests in the same numeric repository
may coexist; mixing different numeric repositories under one RuleLoom
repository ID is rejected.

The label-policy hash is a keyed, canonical pin over exact names, targets,
values, completeness, and provider actor IDs. Compute it from the reviewed
policy with `github_label_policy_hash` before the first eligible delivery, then
store that digest in the experiment's protected manifest. Ingestion requires
the digest independently of every bundle and rejects any policy change. The
digest prevents a capture from self-declaring a new policy; RuleLoom cannot
prove when an operator created an external manifest, so its review history and
access controls remain part of the preregistration trust boundary.

`write_github_capture_bundle` creates an owner-only, create-once bundle. An
exact replay returns `False`; the same path with different content is rejected.
`ingest_github_capture` appends the events and, when both an authenticated
point-in-time snapshot and a structural close/merge are present, persists the
complete rich `ChangeUnit`. It never persists an open/final-only unit that would
later need mutation. `finalize_github_capture_units` is an idempotent convergence
pass for batches or concurrent delivery workers. An exact capture replay is
unchanged, while a reused delivery ID with a changed payload conflicts on its
mandatory `provider_delivery` event.

An existing `github_archive_change` with the same pull-request change ID is not
upgraded in place: archive and point-in-time units make different evidence
claims. Ingestion rejects that collision before writing the capture and requires
a new experiment that begins before point-in-time capture. Archive records for
other pull requests and webhook records for the same numeric repository identity
can coexist.

Capture the returned bundle before acknowledging an external webhook. If GitHub
redelivers a request, replay the stored bundle for that delivery ID. Calling the
normalizer again with a new receive timestamp intentionally conflicts rather
than silently rewriting the original availability time.

## Exact label outcomes

RuleLoom reads only the top-level `label.name` on a `pull_request` action whose
action is exactly `labeled`. It never infers an application from the mutable
`pull_request.labels` collection, an archive timeline, or a later label
definition. Every application/removal remains an immutable provider fact.

The normalizer emits a candidate strong `change_finalized` assertion only when:

1. the exact point-in-time name matches the policy supplied to capture;
2. that entry declares `evidence_complete=true` for one supported atomic target;
3. the delivery sender's numeric provider ID was allow-listed before capture;
4. the sender is not the pull-request author; and
5. the action is `labeled`, never `unlabeled` or a repository label rename.

It becomes eligible evidence only when ingestion also matches the independently
frozen policy hash. This second boundary is what prevents a bundle from proving
its own preregistration; the external manifest must itself have been reviewed
before capture.

Removing or renaming a label never rewrites or retracts old evidence. Model a
correction as a new, independently authorized assertion. Conflicting positive
and negative assertions remain `unknown` under RuleLoom's ordinary vote rules.

The strict JSON form used by the Action is:

```json
{
  "schema_version": 1,
  "labels": [
    {
      "name": "ruleloom:validation:positive",
      "target": "validation_rework_required",
      "value": "positive",
      "evidence_complete": true,
      "authorized_actor_ids": [123456]
    }
  ]
}
```

## Local-first Action template

[`integrations/github-action/example-workflow.yml`](../../integrations/github-action/example-workflow.yml)
is a template, not an enabled workflow. It grants no `GITHUB_TOKEN` permissions,
performs no checkout, calls no API, and has no third-party action dependency.
Pin the RuleLoom action itself to a reviewed full 40-character commit SHA.

The template targets a trusted self-hosted runner because durable local-first
storage must outlive the job. Before using it:

1. create an owner-only directory such as `/var/lib/ruleloom/github-captures`
   (`0700`) on the runner;
2. configure independent `RULELOOM_IDENTITY_KEY` and
   `RULELOOM_ENVELOPE_KEY` secrets;
3. register `RULELOOM_LABEL_POLICY_JSON` as a repository variable;
4. protect changes to the workflow, label policy, runner labels, and secrets;
5. replace the action placeholder with the audited commit SHA; and
6. run capture before any checkout or command sourced from a pull request.

The wrapper launches Python in isolated mode (`-I`) and adds only the pinned
RuleLoom action source. It never interpolates payload fields into the shell. Its
file name is stable by GitHub run ID, so a rerun on persistent storage returns
the original first-capture bundle when body, repository, event, and policy hash
match; a mismatch fails closed.

### Local inbox observer

The CLI reads the HMAC key from an environment variable so it does not appear
in process arguments or command history:

```bash
ruleloom history --root /path/to/repository \
  ingest-github-captures /var/lib/ruleloom/github-captures \
  --envelope-key-env RULELOOM_GITHUB_ENVELOPE_KEY \
  --expected-label-policy-hash "$RULELOOM_FROZEN_LABEL_POLICY_HASH" \
  --max-bundles 1000
```

Provision that variable through the observer's secret manager. Do not paste the
key into a checked-in workflow or shell script.

Provision `RULELOOM_FROZEN_LABEL_POLICY_HASH` from the reviewed experiment
manifest. It is not secret, but the observer must never derive it from an
incoming bundle; doing so would let the evidence choose its own policy.

`ingest_github_capture_directory` is the bounded bridge from the Action's
durable directory into RuleLoom history. It:

- accepts only safe `.json` regular files from an owner-only, non-symlink
  directory;
- sorts filenames deterministically and enforces `max_bundles` (hard maximum
  10,000);
- caps the whole scanned batch at 64 MiB and 50,000 normalized events, in
  addition to the per-bundle limits;
- loads and verifies every bundle HMAC before the first history write;
- requires the repository ID from the local RuleLoom protocol and an
  independently frozen label-policy hash, and verifies every bundle against
  both even when history is still empty;
- rejects cross-repository batches, reused delivery IDs with different content,
  corrupt bundles, archive-unit upgrades, and event/unit conflicts;
- collapses exact duplicate delivery bundles as replays; and
- commits all unique events and any complete rich units in one recoverable
  RuleLoom history transaction.

The helper never deletes, renames, or moves inbox files. Its report separates
unique deliveries, duplicate replays, event inserts/unchanged records, and unit
inserts/unchanged records. Verification and same-delivery failures name the
failing bundle; relational conflicts name the immutable event or change ID. No
history prefix has been written on either kind of preflight failure. A
transaction failure is rolled back by the existing paired-log journal; no
partial success is reported.
The caller owns retention/quarantine and scheduling, so this remains an API for
a local observer rather than a hidden daemon.

On GitHub-hosted runners, the output file is ephemeral. Uploading it to an
artifact service changes the storage trust model and requires a separately
audited, SHA-pinned upload step. The provided template does not pretend that an
ephemeral runner is a durable ledger and does not push evidence into the source
repository.

GitHub does not pass ordinary repository secrets to `pull_request` workflows
from forks, so the template will not capture those deliveries as written. Do not
silently replace it with `pull_request_target`: that event changes the security
boundary around secrets and untrusted contributions. For fork-heavy projects,
prefer the HMAC-verifying webhook receiver or perform a dedicated threat review
of a no-checkout, fully pinned `pull_request_target` workflow.

## Bounded scope and remaining limits

- Payloads are limited to 2 MiB and bundles to 4 MiB. Larger deliveries are
  rejected rather than truncated.
- Public GitHub payload shapes are supported; GitHub Enterprise host identity
  and secret rotation metadata are not modeled in this version.
- A check event is recorded as provider evidence but remains unattributed to a
  change. It cannot independently prove that the change caused a failure.
- Review state is captured, but free-form review prose is deliberately ignored;
  therefore a changes-requested review has category `unspecified` unless another
  structured source supplies a registered category.
- A capture supplies events, not a complete pull-request commit lineage. Use it
  alongside the Git/archive bootstrap or point-in-time snapshots. It does not
  execute CI, inspect code, or decide whether web/mobile/other surfaces agree.
- The provided Action is a capture transport, not a complete ingestion daemon.
  A trusted local observer must load each MAC-verified bundle and call
  `ingest_github_capture_directory` (or `ingest_github_capture` per bundle); the
  template does not claim automatic label supply while that observer is absent.
- A confirmatory unit requires capture of both an opening/synchronization
  snapshot and the structural close/merge. Starting capture only after a pull
  request closed cannot reconstruct the missing point-in-time state.
- Protect the bundle directory and key material. Append-only files plus HMAC
  detect modification; they do not make a compromised receiver or runner
  trustworthy.
