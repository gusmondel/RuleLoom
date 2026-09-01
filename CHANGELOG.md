# Changelog

All notable changes to RuleLoom are documented here. The project follows
[Semantic Versioning](https://semver.org/) while it is in alpha; minor releases
may add persisted schemas or commands, but incompatible evidence semantics
always require a new explicit schema, adapter, pack, or experiment version.

## Unreleased

## 0.9.0 - 2026-09-01

### Added

- Configuration schema v4 with a frozen future holdout, preregistered
  signal-probe settings, and relative Horn learner gates.
- `ruleloom signal-probe`, which evaluates class-balanced Boolean logistic and
  shallow-tree families in label-availability-aware rolling-origin folds over
  pre-holdout data only.
- Content-addressed signal-probe artifacts with MCC, average precision,
  selective risk, alert rate, Wilson proportion intervals, and an explicitly
  descriptive conservative lift diagnostic.
- Train-only Horn near-miss reports containing top rejected clauses, confusion
  counts, support, rejection reasons, and hypotheses examined with a
  multiple-testing warning.
- `generic_changes@2`, adding language-neutral ordinal churn/file-count bands,
  change diffusion, strictly prior path hotspots and dormancy, bounded missing
  co-change partners, and prior-snapshot CODEOWNERS boundary facts.
- Outcome-stratified materialization retention counts and rates, exposing when
  missing Git evidence preferentially removes one class.
- A signal-first protocol and expanded research matrix covering temporal
  validation, delayed labels, selective classification, Wilson intervals,
  multiple testing, co-change, ownership, SZZ noise, drift, and cross-project
  cold start.

### Changed

- New projects initialize with schema v4 and `generic_changes@2`; legacy config
  schemas and pack versions retain their exact hashes and semantics.
- Horn v0.5 may gate selective clauses relative to cohort prevalence and stores
  diagnostic near-misses without using them as confirmatory evidence.
- CODEOWNERS history uses bounded native `git cat-file` batches and deduplicated
  blob reads instead of one Git process per commit; owner identities are never
  persisted.
- Time-window historical facts abstain after non-monotonic timestamps, exact-path
  facts abstain on truncated manifests, and co-change work has explicit path and
  pair budgets.
- Exact diff and content reads disable hidden partial-clone lazy fetching;
  missing promisor blobs abort materialization transactionally with an explicit
  hydration/full-clone remedy.
- A pinned Flask/ripgrep/Express portability smoke result records extraction,
  retention, timings, predicate prevalence, and the expected no-label
  abstention.

### Scientific integrity

- The signal stage is named a signal-availability probe rather than a ceiling;
  passing it does not establish that Horn or RuleLoom is useful.
- The Wilson-endpoint lift ratio is labeled descriptive, not a formal
  post-selection confidence interval.
- Fix keywords, SZZ links, and approximate merge-base snapshots remain weak or
  abstaining evidence; they are not promoted to strong predictor truth.
- A failed or inconclusive signal probe blocks holdout evaluation so repeated
  vocabulary iteration cannot silently consume the deployment holdout.

## 0.8.0 - 2026-09-01

### Added

- A bounded, manifest-bound GH Archive/ClickHouse adapter for public GitHub PR
  opening, merge, approval, and changes-requested events, with remote actor
  hashing, exact hourly source-continuity auditing, and no prose or
  source-content collection.
- The atomic `independent_review_changes_requested` outcome and
  `provider_change` prediction unit for exact opening-snapshot experiments.
- Exact preregistered temporal boundaries through `evaluation.test_start_at`,
  plus fixed size-only and deterministic class-balanced Boolean logistic
  baselines.
- A preregistered Apache Airflow case study that publishes its failed success
  criterion, cohort attrition, selection threat, metrics, and evidence hashes.

### Changed

- Historical materialization validates repository identity and required Git
  objects once per cohort, disables hidden lazy fetches on the aggregate
  tree-path route, classifies every skip, and supports exact tree paths with
  complete provider aggregate statistics.
- Root commits accept only Git's canonical empty-tree object as their base,
  fixing the lost-root-unit bug without weakening commit validation.
- The Horn learner evaluates Boolean bodies with bitsets and accounts work in
  bounded machine-word units; the engine provenance is now
  `ruleloom-horn/0.4`.
- Candidate evaluation records all baseline definitions and parameters, while
  keeping threshold and predicate selection training-only.

### Performance

- In the public Airflow run, an idempotent 6,314-unit materialization completed
  in about 4.4 minutes and 100 Horn bootstrap runs in about 41 seconds. These are
  environment-local observations, not portable guarantees.

### Scientific integrity

- The Airflow Horn model abstained and failed the frozen criterion (holdout MCC
  `0.000`); no policy was promoted. The supplementary logistic baseline reached
  MCC `0.136` but only `0.159` precision.
- A post-analysis source audit found 42 missing GH Archive hours. The original
  result is invalidated; the disclosed corrected rerun conservatively turns
  gap-crossing negatives into unknown outcomes and still yields a null result.
- Materialization retention differed significantly between known positives and
  negatives, so metrics are explicitly scoped to the retained cohort.

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
