# Security policy

## Supported versions

RuleLoom is alpha software. Security fixes are applied to the latest released
minor version only. Unreleased checkouts and older versions may receive a fix at
maintainer discretion. No version is currently approved as a security or merge
control.

## Reporting a vulnerability

Do not report vulnerabilities in a public issue, discussion, dataset, or agent
transcript.

Use the private vulnerability-reporting feature or security contact advertised
by the project's current distribution page. If no private route is available,
request one without vulnerability details and wait for a private channel before
sharing reproduction material. Do not send proprietary repository data.

Include, when possible:

- affected RuleLoom and Python versions;
- operating system and optional engine revision;
- minimal reproduction using synthetic data;
- expected and observed behavior;
- impact and prerequisites;
- whether untrusted repository content is involved;
- suggested mitigation, if known.

Maintainers should acknowledge reports privately, reproduce them, agree on a
disclosure plan, and credit the reporter if requested. Response times are
best-effort while the project is pre-1.0; do not interpret silence as permission
to expose user data.

## Threat model

RuleLoom processes repositories that may be untrusted. Relevant attacker-
controlled input includes filenames, file content, diffs, commit metadata,
configuration, JSONL evidence, labels, candidate artifacts, Prolog output, and
generated agent-skill text.

High-priority vulnerability classes include:

- shell or argument injection through Git references, paths, evidence, or
  optional-engine invocation;
- path traversal, unsafe symlink following, or writing outside the initialized
  repository;
- arbitrary code execution while parsing data or Popper output;
- prompt/instruction injection through facts, rules, evidence, or generated
  Codex/Claude skills;
- automatic promotion or synchronization of an unapproved candidate;
- hash/provenance confusion that evaluates one dataset and publishes another;
- treating version-controlled review fields or copied prediction timestamps as
  proof that a transition or prediction occurred locally at the claimed time;
- destructive overwrite of observations, labels, candidates, or user-authored
  agent files;
- secret, source, personal-data, or repository-metadata disclosure;
- denial of service through adversarial input or searches near configured
  resource bounds;
- unsafe temporary files, permissions, or subprocess environment inheritance.

RuleLoom does not attempt to sandbox Git, Prolog, a coding agent, or the host
operating system. Run it with the least privileges appropriate for the
repository. Never use an untrusted Popper checkout or solver binary.

A malicious process running as the same OS user is outside the protection
offered by RuleLoom's local files: it can read or rewrite observations,
predictions, shadow artifacts, lock state, timestamps, and Git-private
attestations. The hash and attestation checks are defenses against copying,
accidental corruption, and unsophisticated tampering, not a same-user security
boundary. Use a separate account, ACLs, or an isolated CI service for an
adversarial or scientifically blinded workflow.

All configurable managed paths must remain under `.ruleloom/`, and managed
paths cannot overlap. RuleLoom rejects path escapes and managed symlink
components, but this does not make the surrounding repository trustworthy or
sandbox Git and external executables.

## Local attestations

Lifecycle review JSON is versionable evidence, not sufficient authorization.
When RuleLoom creates or explicitly trusts a `shadow`, `approved`, or
`deprecated` artifact, it writes a separate, non-versioned attestation in the
Git-private metadata associated with that checkout/worktree and binds it to the
exact artifact hash. This lets the normal loader reject an unattested copy or a
mismatched artifact. A clone or another worktree must inspect the artifact and
run `ruleloom trust` before using it as active policy. Never automate that
command merely because a file was committed by a trusted branch, and do not
treat the attestation as proof against a malicious same-user process.

Prediction attestations are created automatically only when `assess` appends a
prediction close to its declared `predicted_at`. There is intentionally no
supported command to attest an imported prediction retroactively, so an
ordinary copied `.ruleloom/predictions.jsonl` is rejected as prospective
evidence in a new worktree. Preserve reports with their originating experiment
records, and do not claim that copied JSON or a local attestation proves timing
against an actor with same-user filesystem access.

For shadow assessment, `--include-shadow` requires `--blind` and recording.
Blind mode redacts rule matches and recommendations from stdout only; it still
writes observations, predictions, and attestations. It does not conceal
`.ruleloom/shadow/` or `.ruleloom/predictions.jsonl` from the same user. A valid
scientific shadow run therefore uses an isolated observer and prevents the
coding agent and outcome adjudicator from accessing those files or observer
logs.

## Data handling

`.ruleloom` and generated skills may reveal repository structure, outcome labels,
review references, and evidence excerpts. Before committing or sharing them:

- inspect the files;
- remove secrets and unnecessary source excerpts;
- follow repository-owner and data-retention policy;
- prefer path/line references or hashes over copied content;
- keep private repository artifacts out of public bug reports;
- remember that deleting the source repository does not delete published agent
  transcripts or copied artifacts.

RuleLoom should not make network requests or upload telemetry without an
explicit, documented feature and user action.

## Safe use of generated policies

An approved rule is still untrusted guidance, not authorization. Generated
skills must not:

- expand agent tool permissions;
- override system, user, or repository security policy;
- execute evidence text;
- expose secrets to a model or remote service;
- bypass human review or CI;
- claim that a correlation is causal;
- turn a first-day pilot into an enforcement gate.

Review generated skill diffs before use. Shadow policies are never rendered by
`sync-agents`; during shadow mode, do not make the policy artifacts, predictions,
or observer output visible to the agent or people who determine outcome labels.

## Dependency and release hygiene

Version 0.1 supports macOS and Linux and relies on POSIX `fcntl` locking; Windows
is not supported. The core intentionally has no runtime Python dependencies.
Optional Popper and solver integrations expand the trusted computing base and
must be pinned, recorded, and tested. RuleLoom does not download or install
Popper at runtime;
the operator must provision the checkout, its compatible Python environment,
SWI-Prolog, and GNU `timeout` before an offline run. The current adapter permits
only `max_rules=1` and `bootstrap_runs=0`. Its parser and invocation boundary
have automated tests, but this development checkout has no real end-to-end
Popper result because SWI-Prolog is absent; do not describe that path as runtime
validated until the pinned environment is exercised.

Releases should run formatting, linting, type checks, tests, build verification,
and a clean-install smoke test. Source distributions and wheels should contain
the license and must not contain pilot data.
