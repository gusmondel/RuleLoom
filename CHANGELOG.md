# Changelog

All notable changes to RuleLoom are documented here. The project follows
[Semantic Versioning](https://semver.org/) while it is in alpha; minor releases
may add persisted schemas or commands, but incompatible evidence semantics
always require a new explicit schema, adapter, pack, or experiment version.

## Unreleased

## 0.7.0 - 2026-09-01

### Added

- A read-only, outcome-blind first-hour repository audit with deterministic JSON
  output, bounded topology summaries, co-change findings, and visible truncation.
- Explicit repository-assertion manifests bound to hashed source spans, frozen
  vocabularies, and historical adherence audits.
- A point-in-time GitHub Actions capture path with MAC-protected bundles,
  repository pinning, replay protection, and atomic bounded inbox ingestion.
- An optional local stdio MCP server, built on the official Python SDK, that
  records idempotent assessments and returns approved-only guidance to coding
  agents.
- Versioned assertion and GitHub label-policy schemas, examples, integration
  templates, release automation, and packaging smoke tests.

### Changed

- Git topology analysis batches native `diff-tree` requests and history
  bootstrap accepts an ancestor cursor for proportional incremental collection.
- Incremental intervals now reject unrecorded cursors, divergent history,
  incompatible time filters, and every form of truncation before persistence.
- Historical materialization no longer repeats a discarded full first-parent
  position traversal for every retained unit.
- Git history budgets are caller-reducible while the canonical safety ceilings
  remain fail-closed and part of the evidence contract.
- Product documentation now separates immediate structural evidence, retrospective
  prediction, prospective shadow evidence, and controlled causal claims.

### Performance

- The bundled benchmark alternates one-commit and batched `diff-tree` runs and
  fails unless evidence hashes, volume, and the normalized structural report
  remain identical. Timing is intentionally reported only by the reproducible
  script because results depend on repository shape, cache, Git, and hardware.

### Security

- GitHub capture verifies every bundle before starting one ledger transaction,
  requires independently supplied repository and label-policy pins, rejects
  symlinks and mixed repositories, and never deletes observer inbox files.
- MCP is local, identity-bound, response-bounded, marks repository evidence as
  untrusted data, and does not reveal shadow-policy matches.
- The first-hour text renderer JSON-escapes adversarial paths so control
  characters cannot forge terminal lines.
- Repository-assertion source documents are cached and constrained by a global
  8 MiB read budget; audit examples and counts are streamed within fixed bounds.
- PyPI OIDC is isolated to a protected publish job that neither checks out nor
  executes repository code; distributions come from a separate unprivileged
  build job through SHA-pinned artifact actions.
- The existing 64 MiB sorted-JSONL ledger remains explicit; scalable storage is
  deferred until migration, recovery, parity, and million-record gates pass.

## 0.6.0 - 2026-09-01

### Added

- Bounded, provider-neutral Git and GitHub historical bootstrap.
- Logical change units, evidence grades, conservative atomic outcomes, and
  recoverable paired history-log persistence.
- Strict manual Horn manifests with source hashing, historical adherence audit,
  and prospective-only approval.
- Read-only onboarding diagnosis and language-neutral configured path packs.

### Changed

- Large-change extraction preserves complete aggregate manifests while keeping
  rendered metadata bounded.
- The README and scientific documentation now separate exploratory Git structure,
  retrospective prediction, prospective shadow evidence, and causal claims.

### Security

- GitHub archive label names are ignored as outcomes because historical timeline
  events can reference a mutable label object. Strong label-backed evidence now
  requires a separately captured point-in-time normalized event.
- History imports bind repository origin, provider host, numeric repository
  identity, resource budgets, and normalized content into auditable manifests.
