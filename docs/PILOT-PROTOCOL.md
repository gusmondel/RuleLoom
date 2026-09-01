# Repository pilot protocol

## Purpose

The first installation in a target repository is a measurement pilot, not a
production rollout. The initial run is shadow-only and ends before any approval,
synchronization, or visible intervention. Its purpose is to answer two ordered
questions:

1. Can RuleLoom collect trustworthy, decision-time facts and mature outcome
   labels from the target repository?
2. Do induced rules predict later outcomes better than simple baselines?

Day one can answer only parts of question 1. It cannot establish causality, and
it may not contain enough mature labels even for a credible retrospective test.
Any later controlled-visibility question requires a separately pre-registered
experiment and is outside this runbook.

RuleLoom learns propositionalized Horn clauses over Boolean unary predicates of
one change. It is not full relational ILP: it does not learn relations among
files, types, people, or other entities, recursion, or invented predicates. The
learner and lifecycle are language-neutral; a versioned evidence pack decides
which deterministic predicates a repository exposes.

## Pre-registration record

Complete this table before inspecting learned rules or metrics. Commit the
record with the pilot artifacts if the repository owner's data policy permits it.

| Field | Decision to record |
|---|---|
| Experiment ID | One stable ID for this pre-registered protocol; change it when the contract changes |
| Repository ID | The identity derived by `init`; never pool another repository |
| Unit of observation | Prefer one grouped `historical_change` per logical change. Raw `git_commit` cohorts remain supported separately and exploratory unless independently curated. `git_range`/`git_worktree` are prospective units; never pool unit kinds. |
| Independent-unit/group key | Stable PR/task/change ID. Materialization emits one observation per `ChangeUnit`; `learn` rejects duplicate mature `change_id` values. Evidence-source `independent_group` prevents dependent votes from masquerading as corroboration. |
| Prediction time | Exact workflow point and immutable commit SHA for retrospective training, or base/head/worktree snapshot for prospective prediction |
| Target | One outcome with a single operational definition |
| Evidence profile | Schema version plus one exact `pack@version`; use schema-v4 `generic_changes@2` unless the experiment has pre-registered a supported specialized or configurable pack |
| Included paths | Repository-relative `evidence.include_paths` globs defining the eligible component(s) |
| Excluded paths | Repository-relative `evidence.exclude_paths` globs defining generated, vendored, or otherwise ineligible material |
| Configured feature library | For `configured_paths@1`, the complete canonical `pack_config`, resolved predicate list, `pack_config_hash`, outcome-blind design source/revision, author, and rationale |
| Feature-library lock | Timestamp and independent witness that the vocabulary was frozen before access to outcomes, candidate rules, metrics, or holdout errors |
| Predicate audit | Preserved pre-outcome `ruleloom predicates audit` JSON, command revision, repository/experiment/target/config/protocol identity, outcome-blind observation and complete-audit manifest hashes, chronology/window sizes, thresholds, warnings, reviewer decision, and any superseded experiment ID |
| Configuration attempts | Every vocabulary/scope/threshold tried, including abandoned attempts |
| Change thresholds | Exact `large_change_churn`, `multi_file_count`, and `metadata_file_limit` values |
| Positive label | Independent event that counts as positive |
| Negative label | Maturity condition plus absence of a positive event |
| Unknown label | Every case not yet mature or not adjudicable |
| Maturation window | Time or workflow event after which negative is allowed |
| Outcome adjudication transport | Normalized immutable events; capture mechanism, authorized independent actors, retained point-in-time evidence, maturity/completeness, conflict/correction policy, and exporter version. Archive timeline label names are ineligible. |
| Candidate origin | Learned from the locked training partition, or one explicit reviewed manual Horn seed; never describe the latter as learned |
| Retrospective window | Oldest and newest eligible historical change |
| Holdout rule | Latest chronological grouped changes, never random. In a raw-commit fallback, admit only one independently audited commit per real-world change or classify it as exploratory. |
| Fixed holdout boundary | Required aware `evaluation.test_start_at` for schema v4; observations before it are eligible for probe/train and observations at/after it form the untouched holdout. Labels unavailable at that instant are embargoed from training. |
| Signal probe | Frozen model families, folds, minimum train/validation sizes, MCC/lift/alert-rate thresholds, confidence level, tree depth, and predicate cap. Fail/inconclusive blocks holdout access. |
| Horn gate | Absolute precision for legacy schemas or schema-v4 relative lift plus alert-rate threshold; preserve near-miss limit and hypotheses examined. |
| Confirmation window | Untouched future interval reserved for any design selected after exploratory analysis |
| Primary predictive metric | Suggested: MCC for the promotion comparison; precision may be the operational priority when false prompts dominate cost |
| Secondary metrics | Precision, recall, F1, balanced accuracy, prevalence, predicted-positive rate, coverage, confusion counts |
| Baselines | Never alert, always alert, train-majority, fixed size-only, best train-selected single literal, and deterministic Boolean logistic regression |
| Shadow duration | A time window and/or number of mature eligible changes |
| Product outcome | One later workflow/quality measure for a controlled phase |
| Cost guardrails | Latency, tokens, reviewer time, extra validations, false alarms |
| Stop conditions | Conditions listed below, customized before data inspection |

Choose and record the pack version, canonical pack configuration, path scope,
and change thresholds before inspecting outcomes or collecting the experiment
dataset. “Inspecting outcomes” includes raw CI/review/incident sources, outcome
proxies, learned rules, confusion tables, and individual holdout errors. Do not
redefine the target, window, primary metric, pack version, configured vocabulary,
scope, or thresholds after seeing which choice produces a better rule. If any
definition must change, preserve the previous attempt, start a new experiment ID
and dataset, and recollect under the new profile. If the change was informed by
labels or metrics, the old holdout is now design data: confirmation requires an
untouched later window, not merely a renamed experiment over the same sample.
Never append observations from two evidence profiles to one dataset or pool
their candidates, predictions, or metrics.

## Feature-design anti-leakage lock

For a static pack, audit the versioned built-in vocabulary. For
`configured_paths@1`, a label-blind feature designer must derive component and
contract globs only from architecture/ownership documentation and the repository
tree at a recorded revision. The outcome adjudicator must not edit that library,
and the evaluator must not disclose holdout errors before the lock. If role
separation is impossible, record the conflict and treat retrospective results as
exploratory rather than confirmatory.

Configured predicates must describe path contact with stable repository
surfaces, use the required `touches_*` naming, and depend only on files visible
at prediction time. Do not encode outcome/result concepts, reviewer or developer
identity, incident/release labels, PR status, or paths selected because they
occurred in positives. RuleLoom files, prediction ledgers, generated agent
skills, and files added only after review are not eligible features.

Sign or otherwise preserve the canonical config hash, design revision, lock
time, roles, rationale, and complete attempt log before labels are opened. A
post-lock semantic change—predicate, glob, scope, threshold, target, extraction
meaning, or grouping rule—starts a different protocol. Order-only canonical
reformatting may retain identity only when the resulting hash is unchanged.

An LLM may propose predicate names and deterministic definitions from the
recorded repository tree, architecture/ownership documents, and an outcome-blind
predicate audit. Treat its output as untrusted design input. It must not read
outcomes for a pre-lock proposal, write an activated policy, approve its own
proposal, or silently edit the frozen configuration. A human reviewer must
accept the semantics and extraction evidence. Every accepted addition, removal,
rename, or definition change is a new experiment/protocol hash. If the LLM or
human designer used labels, rules, metrics, or holdout errors, reserve an
untouched future confirmation window.

## Recommended first target

The default configured target is `needs_extra_validation`; historical
materialization maps it to the atomic event target
`validation_rework_required`. Keep it narrow and tied to the normal review
process:

- **Positive:** after the prediction point, an independent human review request
  identifies a concrete missing validation (for example, an accepted request
  for an automated test, contract check, or reproducible failing scenario). A
  later validating update may corroborate the request but is not required by
  this target. A provider adapter may instead emit an explicit, complete matured
  outcome under this same registered definition.
- **Negative:** the chosen review/maturation event completes without such a
  signal.
- **Unknown:** review is ongoing, the outcome is ambiguous, the change was
  abandoned for unrelated reasons, or source linkage is incomplete.

“The change looks risky” is not an outcome label. A test that was already present
at prediction time is a feature, not proof of a later positive outcome. Do not
label from the same diff pattern used to generate the facts.

Conversely, a test or validation path added in response to the review request is
part of the outcome process and must not appear in the predictor snapshot. For
this target, a final merge/squash diff is usually too late unless evidence proves
that no target event preceded or changed it. A confirmatory historical
`ChangeUnit` must carry an exact point-in-time base/prediction SHA before the
event and `evidence_quality=rich`. Git-only or final-only reconstruction is
exploratory and cannot support approval. If reliable event ordering is
unavailable, keep the case `unknown` or collect prospectively; never rewrite its
timestamps.

Post-merge regressions should use the separate `post_merge_defect` target with a
longer registered maturity window. Combining review requests and production
defects in one label makes the learned rule difficult to interpret.

## Phase 0 — privacy and workflow mapping

Before initializing the target repository:

- confirm that local processing of commit paths, metadata, and labels is allowed;
- identify how commits map to pull requests, CI outcomes, review requests, and
  later incidents;
- choose the observation unit, stable group key, pre-outcome snapshot source,
  and target contract above;
- run `ruleloom packs list` and choose one exact `pack@version`; for a static
  pack, audit its listed vocabulary, while a configurable pack requires loading
  and independently locking the exact project `pack_config` and resolved
  vocabulary;
- pre-register included/excluded path globs and the large-change, multi-file,
  and metadata-sampling thresholds before inspecting outcome labels;
- for `configured_paths@1`, preserve the canonical config/hash, outcome-blind
  design revision and rationale, roles, lock time, and all configuration
  attempts before opening outcome sources;
- verify that every historical predictor snapshot predates its review/CI
  outcome; require one stable `ChangeUnit` ID per logical change and audit any
  raw-commit fallback manually;
- decide which retrospective `.ruleloom` artifacts data policy permits; keep
  shadow and prediction material out of any checkout visible to the agent or
  outcome adjudicator;
- designate a human reviewer for the shadow transition;
- designate an isolated observer account or CI job that, during prospective
  collection, alone can read shadow policy, prediction, attestation, and
  assessment-output files;
- capture the current agent instructions and testing workflow as the baseline.

Do not ingest secrets, issue bodies with personal data, production logs, or full
source excerpts unless they are explicitly required and authorized.

## Phase 1 — install and instrumentation

Install a reviewed release, tag, or immutable commit. For example, from a
reviewed local checkout:

```bash
uv tool install /absolute/path/to/ruleloom
ruleloom --version
ruleloom packs list
```

Then initialize the observer checkout of the target repository with an exact
pack version and without agent integration:

```bash
cd /absolute/path/to/observer-checkout
ruleloom init . --project example-project \
  --pack generic_changes --pack-version 2 --agents none
ruleloom doctor
ruleloom readiness
```

`ruleloom init` requires an existing Git repository with either
`remote.origin.url` configured or at least one commit; establish one of those
identity anchors first. The current release supports macOS and Linux, not
Windows, because its storage lock uses POSIX `fcntl`. `generic_changes@2` is the
schema-v4 default and provides language-neutral change-shape, path-role,
ordinal, diffusion, hotspot, dormancy, co-change, and ownership-boundary facts.
Passing no `--pack-version`
selects the latest registered version, but a pre-registered pilot should always
pin it explicitly.

Inspect `.ruleloom/config.json` before collection. New pilots use
`schema_version: 4`; `configured_paths@1` is also supported under schema v4. Freeze its
`experiment_id`, derived `repository_id`,
`prediction_unit`, `outcome_definition`, target, `pack`, `pack_version`, and the
entire `evidence`, `pack_config`, `signal_probe`, learner-gate, and holdout
objects. RuleLoom
includes the resolved extractor and all these fields in the evidence-protocol
hash recorded on every observation. A typical
language-neutral evidence profile is:

```json
{
  "schema_version": 2,
  "pack": "generic_changes",
  "pack_version": 1,
  "evidence": {
    "include_paths": ["**"],
    "exclude_paths": [],
    "large_change_churn": 200,
    "multi_file_count": 3,
    "metadata_file_limit": 512
  }
}
```

Treat this as an excerpt: retain the remaining fields generated by `init`.
Adjust scope and thresholds once, before collection, when repository structure
requires it. An include scope such as `services/api/**` prevents facts from an
unrelated component entering the same observation; excludes can remove
generated or vendored subtrees. Do not edit the profile after the first
observation. Validation rejects observations whose protocol hash, pack version,
or extractor does not match the configured profile; do not work around that by
rewriting hashes or records.

The include scope defines eligible outcome units. Schema-v2/v3 direct collection
fails closed for a unit that mixes included and outside-include files; backfill
skips mixed units and units with no included files. Widen the pre-registered
include set when an outcome legitimately covers several components, or use a
component-specific change/outcome unit. Configured excludes within the include
set do not make a unit mixed and are intended for generated or vendored paths.
Archive each backfill's JSON audit fields: `examined`, `eligible`, `skipped`,
`skipped_by_reason`, the bounded `skipped_preview`, its truncation count, and
`skipped_manifest_hash`. These preserve the sampling denominator and distinguish
mixed units from wholly out-of-scope units without emitting an unbounded list.

The pack-specific feature filters are not another outcome scope. When a
heterogeneous repository needs stable component facts, initialize a separate
schema-v3 configured-path experiment, for example:

```bash
ruleloom init . --project example-configured-pilot \
  --pack configured_paths --pack-version 1 --agents none \
  --path-predicate 'touches_client_ui=components/client_ui/**' \
  --path-exclude 'touches_client_ui=components/client_ui/generated/**' \
  --path-predicate 'touches_shared_contract=interfaces/contracts/**'
```

The generated `pack_config.path_predicates` is canonical. Each predicate is true
when at least one visible, already in-scope changed path matches one of its
include globs and none of its predicate-local excludes; overlapping predicates
may co-occur. These globs create features only. The separate
`evidence.include_paths`/`exclude_paths` still decide whether the entire change
and outcome are eligible. `configured_paths@1` also emits the shared generic
facts and does not read source content. Its path matching is language-neutral,
but the chosen taxonomy is repository-specific background knowledge, not a
portable result.

Before labeling, audit at least one match and non-match for every configured
predicate, every predicate-local exclusion, one file matching multiple
predicates, a file matched by none, and representative added/deleted/binary
paths. Check the configured match counts, unmatched/overlap counts, match
manifest hash, `pack_config_hash`, and the common generic facts. A pattern-limit
or comparison/work-budget failure is extraction failure, never evidence that a
fact is false. Do not alter a glob to make these audits agree with an outcome; repair
mechanical misunderstanding before labels are opened, record the attempt, and
re-lock the full library.

The initial deterministic learner is the appropriate smoke-test engine because
it has no external solver.
All managed data/artifact paths must remain below `.ruleloom/` and must not
overlap. Configure Popper only as a separately pinned experiment with
`max_rules=1` and `bootstrap_runs=0`, after provisioning its compatible Python
environment, SWI-Prolog, and GNU `timeout` offline. RuleLoom does not download or
install them. The Popper adapter has not yet completed a real end-to-end run in
the reference development environment, so an initial installation must use
`horn`.

Bootstrap the existing Git graph before waiting for new changes:

```bash
ruleloom history bootstrap-git --all
ruleloom history materialize
ruleloom history status
ruleloom predicates audit
ruleloom diagnose
```

`--all` retains the most recent reachable prefix until the first of three bounds:
100,000 commits, 64 MiB in either canonical history JSONL, or 1 MiB for one
canonical record. The JSON report states shallow/truncated status,
`storage_truncated`, exact event/unit byte totals, both storage limits, and a
manifest hash covering those decisions. Record these fields so a byte-limited
sample is not mistaken for complete history. This step is language-neutral and
immediately measures repository volume, but its `git_only`/`final_only` units
are exploratory because Git alone does not prove a PR-time decision point or
independent outcome.

`ruleloom diagnose` is a read-only onboarding summary over readiness, history
status, and the outcome-blind predicate audit. Archive its JSON output when it
helps explain a decision, but do not treat its recommended next command as
evidence. It neither imports data nor changes a threshold or gate.

Run `predicates audit` and preserve its JSON **before exporting, opening, or
importing outcome sources**. The command is outcome-blind and reports:

- total observations and the ordering used: complete single-repository
  first-parent topology when available, otherwise `observed_at`;
- equal early/late chronological halves, with the later half receiving the
  extra observation when the total is odd;
- per-predicate counts and prevalence, plus `never_true`, `always_true`, `rare`,
  `saturated`, and absolute prevalence-drift flags;
- configured-path match counts and up to eight deterministic path examples per
  configured predicate;
- coverage of the configured predicate union; and
- observed equivalence, complementarity, one-way implication, and sufficiently
  high Jaccard-overlap relations.

The default diagnostic thresholds are 0.01 for rare, 0.99 for saturated, 0.20
absolute prevalence shift for drift, and 0.90 Jaccard similarity for overlap.
Any overrides are analysis settings rather than evidence-protocol fields, but
must be recorded so another reviewer can reproduce the flags.
Equivalence and high-overlap relations require a union supported by at least two
observations. A one-way implication additionally requires antecedent support of
`max(2, ceil(rare_threshold * observations))`, which suppresses trivial subset
relations from sparse facts without hiding rare exact aliases. The JSON records
both effective support minima.

The report includes `experiment_id`, `target`, `config_hash`,
`evidence_protocol_hash`, an outcome-blind manifest over exactly the observation
fields consumed by the audit, and a manifest of the complete report payload.
The latter excludes only its own field before hashing. Verify and retain those
identities with the artifact; matching pack/glob names alone do not establish
that two audits used the same scope, target, thresholds, or input evidence.

These are structural diagnostics, not target associations or logical proofs.
An observed implication may be an accident of the sampled history, and a
prevalence near 50% proves neither relevance nor irrelevance. The audit must not
rank predicates using labels, and it never authorizes deletion or activation.
Predictive ranking happens later using only the training partition.

Use the audit to repair a mechanically wrong glob, an empty concept, empirical
duplication, extreme saturation, or path-layout drift while the process is still
outcome-blind. Preserve every attempt. Any semantic repair—including a changed
predicate, glob, evidence scope, change threshold, extractor, or target—requires
a fresh experiment and protocol hash followed by rematerialization and a new
pre-outcome audit. Never rewrite old observations to adopt the new meaning. If
outcomes were already opened, the old sample is design data and the revision
needs an untouched future confirmation window.

For an authorized GitHub repository, the built-in adapter can group bounded
archived evidence without a custom exporter:

```bash
gh auth status --hostname github.com
ruleloom history import-github --repository OWNER/NAME
ruleloom history materialize
ruleloom history status
```

The v0.9.0 archive adapter supports the explicit `github.com` host. By default it
requires `OWNER/NAME` to equal the repository parsed from an unambiguous
public-GitHub HTTPS, SSH, or SCP-style `remote.origin.url`. A reviewed mirror or
checkout without a verifiable matching origin requires
`--allow-unverified-repository`; preserve the resulting `repository_binding`
value as a protocol deviation, never as proof of identity.

`--since` filters PR `created_at` values and the repository-commit scan.
`--until` is an inclusive as-of cutoff for PR creation/finalization and retained
review, check, and revert events; PRs finalized afterward are
skipped. If omitted it defaults to collection time, and in neither case does it
reconstruct a historical provider snapshot. Because import is append-only, a
later run with an earlier cutoff does not retract existing events; use a clean
experiment/log for that as-of sample. Record the exact command, complete
JSON report, canonical history logs, requested window, bounds, warnings, skipped
PR count, truncation status,
`repository_binding`, global API/record limits and use, and `manifest_hash`. The
CLI also emits the compact pre-hash `manifest`, which binds the per-endpoint
limits, warnings/counts, and content hashes of the normalized records without
duplicating those records in stdout. Verify it against the canonical logs. A
global-budget exhaustion is a failed import with no persistence, not a partial
sample; per-endpoint truncation remains explicit in a successful report.

The adapter requests closed PRs only and skips an unexpectedly non-closed or
force-pushed PR it cannot reconstruct safely. It
collects provider metadata but does not fetch Git objects. Ensure every recorded
`base_sha` and `prediction_sha` needed for extraction exists in the observer
clone, and treat `history materialize`'s `skipped`/`skipped_preview` as part of
the sampling denominator. More importantly, the reconstructed archive snapshot
has `point_in_time=false`, so every resulting unit remains exploratory
`git_only` and non-confirmatory. Reviews are category-unspecified, checks are
unattributed, and exact Git revert trailers are weak heuristic links. Do not
present this convenient grouping as PR-opening history or use it to satisfy a
confirmatory historical gate.

Treat provider mutation as append-only evidence, not as a historical rewind.
Review submissions are state-neutral, and check events are versioned by PR plus
normalized check content; later provider changes therefore append rather than
rewrite. The first built-in GitHub numeric repository identity stored for a
RuleLoom `repository_id` is pinned across both history logs. A different
provider repository requires a fresh experiment.

For confirmatory reconstruction, export authorized forge/review/CI/incident
history into the normalized historical-event v1 JSONL contract, then run:

```bash
ruleloom history import --events /absolute/path/to/historical-events.jsonl
ruleloom history materialize
ruleloom history status
```

The importer assembles logical changes by stable `change_id`. A
`change_snapshot` becomes rich only when it contains exact `base_sha`,
`head_sha`/`prediction_sha`, and `point_in_time: true`. Provider text is never
treated as instructions or as a fact. Strong outcomes are enabled by default;
do not pass `--include-weak` in a confirmatory pilot. If an adapter already
emits reviewed `ChangeUnit` records, import them with `--units`. The two JSONL
stores are immutable by ID, so preserve the complete extraction and do not
reuse an ID for corrected semantics.

For a streaming export, import incomplete structural lifecycle events with
`--no-assemble` and assemble only after the prediction/finalization set is
complete. Later outcome-only events can be appended and rematerialized; an
already-created unit cannot be upgraded from open to finalized or from
`final_only` to `rich` in schema v1.

Collect a small known range first:

```bash
ruleloom collect git --base <old-ref> --head <new-ref>
```

This `git_range` mode is an extraction/prospective-instrumentation smoke test. It
is not accepted by retrospective `learn`; use `collect git --last` to create
eligible `git_commit` training observations.

For a recent-history smoke test, use:

```bash
ruleloom collect git --last 20 --ref main
```

For an uncommitted change, use:

```bash
ruleloom collect git --working-tree --ref HEAD
```

These collection modes are mutually exclusive.

Before labeling, manually audit at least a small in-scope change, an eligible
change with no pack-specific signal, a multi-file or large change, and both
sides of every include/exclude boundary. For every audited observation check:

- ID and timestamp identify the intended change;
- base/head identify the immutable pre-outcome snapshot, not a final diff that
  already contains the response to review;
- fact evidence points to the correct path, changed-line marker, or threshold
  calculation, as applicable to the selected pack;
- no predicate uses information created after prediction time;
- missing facts mean “verified absent,” not “extractor failed”;
- only in-scope files contribute to counts, thresholds, or pack predicates;
- `scope_outside_files` is zero; mixed and wholly out-of-scope units were not
  admitted to the dataset;
- labels remain `unknown` until the registered maturity event;
- the preferred historical cohort contains exactly one materialized observation
  per stable `ChangeUnit`; a raw-commit fallback contains at most one independently
  audited commit per real-world change;
- a configurable-pack source's `pack_config_hash` and extraction metadata's
  `configured_paths_config_hash` both equal the exact locked hash;
- rerunning collection produces the same facts and no duplicate ID.

For a large or multi-file change, audit `files_changed`, churn totals,
`change_manifest_hash`, `metadata_files_truncated`, and the sampled
`changed_files`/`file_churn` fields. RuleLoom keeps the totals and manifest hash
for the full in-scope change while bounding the human-readable path metadata by
`metadata_file_limit` and a byte cap. This truncates only stored previews, not
the normalized diff input supplied to the pack or the evidence used to compute
facts. Human-readable `fact_evidence.evidence` explanations are also bounded;
when necessary they carry a SHA-256 truncation marker rather than silently
claiming to be the complete textual provenance. If a selected pack needs
changed-line content and RuleLoom cannot collect that input completely within
its safety limits, collection fails closed instead of recording partial facts.
Never interpret a failed content extraction as absence of a predicate. The
content collector also bounds total wall time, Git subprocess batches, output
bytes, and argument bytes. Non-UTF-8 Git paths are rejected rather than decoded
lossily.

If the experiment specifically needs Flutter/Dart signals, initialize a fresh
observer checkout and separate dataset with this profile instead:

```bash
ruleloom init . --project example-flutter-pilot \
  --pack flutter_testing --pack-version 2 --agents none
```

`flutter_testing@2` layers deterministic Dart/Flutter predicates on the shared
language-neutral facts. In that profile, additionally audit a Dart change that
should match a Flutter-specific predicate, one that should not, and an
out-of-scope Dart change. `flutter_testing@1` is frozen compatibility behavior
for reproducing schema-v1 work, not the default for a new pilot. Do not mix the
generic, configured-path, and Flutter profiles. Compare packs only as separately
pre-registered experiments on an untouched common future window; choosing a
winner on a reused holdout is model selection, not confirmation.

Record extraction coverage as:

```text
eligible changes with a valid observation / all eligible changes
```

Also record parse failures, unsupported changes, duplicate IDs, and collection
duration. A high model score cannot repair untrustworthy extraction.

## Phase 2 — label historical outcomes

Normalized history can derive labels while retaining every vote and timestamp.
Use separate atomic targets: `validation_rework_required`,
`change_attributable_ci_failure`, `post_merge_revert_or_hotfix`, and
`post_merge_defect`. Absence, disagreement, malformed linkage, or an unfinished
maturity window remains `unknown`; a normal merge is not a negative. Review
requests and attributable fail-change-pass CI sequences are strong positives.
Test changes alone, message keywords, SZZ linkage, and an exact Git revert
trailer without an explicit provider link are weak and disabled by default. A
failed GitHub check on the recorded merge result is likewise an unattributed
weak vote for `change_attributable_ci_failure`, not strong CI evidence.

Do not derive an outcome from names found in the GitHub archive timeline. Those
historical application records can expose the current name of a mutable Label
object. A rename after the PR event can therefore retroactively make an ordinary
application look like a structured assertion. The archive cannot recover the
original name, and an as-of cutoff or actor check does not fix the ambiguity.

If the workflow uses labels for adjudication, install the supplied Action, an
authorized webhook, an exporter, or an append-only ledger **before** the
relevant changes. It
must capture each application point-in-time and retain the original timestamp,
repository/change identity, authorized independent actor, atomic target,
positive/negative value, complete maturity evidence, and correction history.
Export that record as an immutable normalized outcome event and import it with
`ruleloom history import`. Pre-register the capturer/exporter version and audit
at least one positive, one mature negative, one conflict, and one correction
against the source ledger.

RuleLoom v0.9.0 ships a local GitHub Action/webhook capture substrate and bounded
inbox ingestion, not a hosted App or durable observer daemon. Follow
[`integrations/GITHUB-CAPTURE.md`](integrations/GITHUB-CAPTURE.md), preregister
the strict label policy, and measure delivery and maturity coverage. Until a
point-in-time bundle exists for a change, archive label names contribute no
strong or weak vote and a label-only case remains `unknown`. The weak
merge-result CI and Git revert heuristics above remain available only through
`--include-weak` and are still non-confirmatory.

For a manually curated fallback on ordinary `git_commit` or prospective
observations, apply labels from independent outcome evidence:

```bash
ruleloom label <observation-id> positive \
  --target needs_extra_validation \
  --kind review \
  --source "review/<stable-reference>" \
  --available-at "2026-01-15T15:00:00Z" \
  --reason "Independent review required an additional automated validation"

ruleloom label <observation-id> negative \
  --target needs_extra_validation \
  --kind review \
  --source "review/<stable-reference>" \
  --available-at "2026-01-15T15:00:00Z" \
  --reason "Registered maturity event completed with no missing validation"
```

Do not use `label` or `import-labels` on `historical_change` observations.
Those labels are derived and revalidated from the append-only event log; import
an explicit complete `change_finalized` outcome event and rerun
`ruleloom history materialize` instead. This prevents a synthetic/manual label
from making a rich unit appear confirmatory without recomputable strong
evidence.

`positive` and `negative` require label evidence. `--kind` accepts `ci`,
`review`, `incident`, `human`, `imported`, or `synthetic`; `--available-at` is
when the outcome became knowable, not when somebody entered it. `--source`
should be a stable reference or an authorized pseudonymous adjudicator ID.
Optional `--confidence` is between 0 and 1. Prefer `unknown` when uncertain;
an unknown target has no label evidence. Once the CLI records `positive` or
`negative`, that observation's mature target is immutable. If adjudication later
finds an error, preserve the correction externally and record a new observation
identity rather than rewriting the original outcome.

For bulk labeling, use `ruleloom import-labels outcomes.csv`. Required columns
are `id,value,available_at,kind,source`; optional columns are
`reason,confidence,target`. For example:

```csv
id,value,available_at,kind,source,reason,confidence,target
commit.a1b2c3d4e5f60718293a4b5c6d7e8f9012345678,positive,2026-01-15T15:00:00Z,review,review/123,Added validation requested,1.0,needs_extra_validation
```

If a second reviewer disagrees, retain that disagreement in an external audit
record and leave the RuleLoom value `unknown` until the registered adjudication
contract resolves it. The current schema stores the resolved evidence, not a
multi-reviewer event history.

Historical reconstruction remains vulnerable to survivorship bias,
outcome-caused features, and incomplete linkage. `history bootstrap-git` does
not reconstruct an initial PR head or prove that review happened later. A rich
provider `ChangeUnit` is eligible only when its exact diff and `prediction_at`
predate the independent event and any response. A final merge/squash containing
that response is not a predictor observation. If provider history cannot supply
a point-in-time snapshot and reliable order, RuleLoom retains the case as
exploratory or unknown; it does not upgrade Git topology into ground truth.

Preserve `retention_by_outcome` from every materialization run. It compares the
positive, negative, and unknown outcomes derivable before Git-object filtering
with the units actually materialized. A class-specific retention gap is a
missing-data threat and must be reported. Do not impute a negative or silently
substitute a merge-base snapshot; capture future opening/synchronization SHAs
continuously so the missing-object mechanism does not recur.

Audit at least one positive and one negative against their independent sources,
including the predictor SHA, event timestamp, response commit, maturity event,
and stable PR/change identity. Ensure `available_at` is when the outcome first
became knowable, never a later import time chosen to pass validation. Report
historical results separately from prospectively collected labels. This outcome
audit is separate from the pre-label extraction audit in Phase 1.

## Phase 3 — retrospective temporal evaluation

Validate and learn:

```bash
ruleloom validate
ruleloom readiness
ruleloom signal-probe --json
ruleloom learn --engine horn
ruleloom candidate list
ruleloom candidate show <candidate-id>
```

`validate` is a whole-project fail-closed check: it reads observations,
candidate manifests, active shadow/approved artifacts, deprecation tombstones,
local transition attestations, and locally attested prediction records. A
successful result establishes structural/provenance consistency, not label
truth or predictive value.

The schema-v4 signal probe is a train-only gate, not a candidate model or a
theoretical ceiling. It uses expanding-window logistic and shallow-tree models,
enforces label availability at each fold, and excludes the fixed holdout
completely. Preserve its content-addressed report. A `fail` or `inconclusive`
result ends this experiment before `learn`; revise the target or vocabulary only
under a new registered design and future confirmation window.

There is a separate, prospective-only track for one already-reviewed manual
risk assertion. A human must translate it into the strict manifest documented
in `DATA-SCHEMA.md`, using only predicates declared by this experiment, then
run:

```bash
ruleloom rules import /absolute/path/to/manual-rule.json
ruleloom candidate show <candidate-id>
```

RuleLoom hashes optional cited source spans but never interprets `AGENTS.md`,
`CLAUDE.md`, or other prose. An LLM may draft a manifest for human review; it
cannot activate a rule or invent a predicate. Verify the target, every literal,
closed-world negation, source span, configured pack, and intended risk meaning
before promotion.

The import reports outcome-blind trigger coverage plus post-hoc association with
whatever labels were mature at audit time. Coverage is useful for detecting an
empty or over-broad trigger, but it does not prove validity. The complete manual
audit remains `confirmatory=false`; do not compare it with learned temporal
holdout metrics as if it were a pre-specified test. Preserve it as hypothesis
provenance.

After human review, a non-empty, reproducible manual rule may enter shadow even
with zero retrospective positives. Its cited sources must remain unchanged and
available at the first transition. It can reach approval only through the exact
prior shadow artifact and the non-overridable prospective requirements below:
distinct predictions, later mature outcomes, both classes, elapsed time,
aggregate precision/recall/MCC, and per-rule matches/precision. Retrospective
coverage or post-hoc manual metrics never substitute for those gates.

When all eligible raw commits carry compatible repository topology, the latest
first-parent positions form the holdout; grouped historical changes use their
prediction times. Ties and non-monotonic timestamps produce warnings.
Retrospective `learn` accepts either one canonical `git_commit` cohort or one
`historical_change` cohort, never a mixture. Grouped history rejects duplicate
mature `change_id` values, so one logical change cannot cross train and test.
Chronology still cannot repair a snapshot created after its label-generating
event, and a raw-commit cohort still requires manual independence auditing.

The current default mechanical
minimum is eight training and four test examples. That minimum allows the
pipeline to run; it is not enough by itself for a reliable business conclusion.
Always show raw confusion counts. Candidate/report metrics are point estimates;
the prospective promotion gate separately computes Wilson 95% lower confidence
bounds for precision and recall. If the pilot needs broader uncertainty
analysis, pre-register and run it separately. If one class is absent from train
or test, stop and collect more evidence.

Schema-v4 Horn search uses the preregistered relative-lift and alert-rate gates.
Its ratio of Wilson endpoints is a descriptive conservative diagnostic, not a
formal confidence interval for post-selection lift. When no clause qualifies,
preserve the top train-only near-misses, rejection reasons, and hypotheses
examined; do not tune the current gates to rescue one of them.

All target-aware selection is confined to the temporally eligible training
partition. The built-in learner excludes training-constant columns, collapses
exact duplicate truth columns to their lexical representative, ranks the
remaining predicates by the absolute positive/negative prevalence-rate gap when
negation is enabled, or by the signed positive-minus-negative gap when it is
disabled, applies its configured search bound, and learns rules on training
observations.
The candidate records observed constants, alias groups, representatives, and
that the holdout was not consulted; the pre-outcome audit separately reports
declared predicates that never occur. Popper receives that same training cohort
only. The `best_single_literal` baseline is likewise selected on training data.
The chronological test partition evaluates the already frozen rule and
baseline—even if previously duplicate columns diverge there—and must never
select predicates or be used to revise the vocabulary.

Review the candidate against all of the following:

- `never_alert`, `always_alert`, and `train_majority` on the same holdout;
- `best_single_literal`, selected on training data and evaluated on the holdout;
- precision, recall, F1, balanced accuracy, MCC, prevalence,
  predicted-positive rate, and confusion counts;
- rule count and literals per clause;
- support and counterexamples;
- bootstrap rule-set stability;
- any split, extraction, timeout, or sample-size warning;
- whether the rule is useful beyond restating an obvious predicate;
- whether the rule could encode a developer, release, or time-period confounder.

For `configured_paths@1`, preserve the pre-outcome vocabulary audit, confirm
that train-only selection was applied, and separately report each predicate's
train/test prevalence, zero- or always-true status, pairwise/observed overlap,
and path-rename drift. A rule that merely restates one component flag has not
demonstrated value from ILP complexity. Never edit the path library after seeing
target-aware training or holdout results and continue to call the same holdout a
test.

For a learned candidate, the default `shadow` gate requires at least 20 positive
outcomes and a non-empty learned rule set. Before this first reviewed transition, RuleLoom requires the
current dataset hash to match, relearns the candidate from the exact
evidence/configuration, and requires an identical identity payload; a
non-reproducible manifest cannot enter shadow. The built-in Horn path is
deterministic; an optional external engine still has to reproduce exactly.

For a learned candidate, the retrospective portion of the default `approved`
gate expects at least 50 positives, a non-empty
chronological test set, aggregate holdout precision at least 0.75, recall at
least 0.50, stability at least 0.40, and holdout MCC strictly greater than the
best of all registered baselines. Approval of either a learned or manual candidate
also requires the exact prior shadow artifact and the following attributable
prospective evidence:

- at least 30 shadow predictions on distinct stable units;
- at least 30 outcomes that became knowable strictly after prediction, including
  at least 10 positive and 10 negative outcomes;
- a span of at least seven days between the earliest retained predictions for
  the first and last stable units;
- aggregate shadow precision with a Wilson 95% lower bound of at least 0.70,
  recall with a Wilson 95% lower bound of at least 0.50, and point-estimate MCC
  at least 0.10; and
- for every clause, at least 10 prospective matches and a prospective Wilson
  95% precision lower bound of at least 0.70; a learned clause additionally
  needs at least one temporal-holdout match and temporal point precision at
  least 0.75.

Dataset, configuration, pack version/configuration, scope, threshold, and target
identity; candidate reproduction; the recorded shadow transition; temporal sample/metrics
completeness when applicable, and the prospective/per-clause requirements are non-overridable.
The positive-count, aggregate retrospective
performance, baseline, and stability thresholds can be overridden only where
the implementation classifies them as non-blocking, with a recorded note. These
defaults are operating/readiness heuristics, not a power calculation or
scientifically universal thresholds. Pre-register any protocol change before
learning.

After human review, promote only to `shadow` for this pilot:

```bash
ruleloom promote <candidate-id> --to shadow --reviewer <reviewer> \
  --note "Reviewed for out-of-band shadow assessment only"
```

Create that local transition in the observer-controlled checkout that will own
the prediction ledger. Once prospective collection starts, its RuleLoom state
and terminal/CI logs must not be readable by the coding agent or outcome
adjudicator.

`--override` exists only for an explicitly documented non-blocking exception and
requires a non-empty note; it cannot bypass the integrity, temporal per-clause,
or prospective shadow gates. It remains visible in the review record and does
not turn weak evidence into strong evidence. Do not use it merely to start
sooner. For an initial shadow protocol, never promote to `approved` and never run
`sync-agents`, even if the configured numeric gates happen to pass.

## Phase 4 — prospective shadow mode

For each eligible new change:

1. assign one stable PR/task/change ID and reuse it for every snapshot of that
   change, never for an independent change;
2. have the isolated observer capture facts at the registered prediction time;
3. have that observer run `ruleloom assess` out of band with shadow policies
   included and blind stdout;
4. keep the observer account/CI workspace and logs inaccessible to the coding
   agent, developer, reviewer, and outcome adjudicator;
5. preserve the immutable prediction record: embedded decision-time
   observation with unknown target and `protocol_hash`, stable `unit_id`, exact
   hash-bound protocol object, target, exact policy snapshots
   (candidate/status/target/manifest hash/rule signatures), policy-set hash,
   matches, abstention, and timestamp;
6. preserve the canonical config/pre-registration separately because the
   Prediction object binds but does not embed every evidence or pack-config
   field;
7. let the normal repository process continue unchanged;
8. attach the outcome only when it matures.

Example assessment:

```bash
CHANGE_ID=pr-123
ruleloom assess --base origin/main --change-id "$CHANGE_ID" \
  --include-shadow --blind --json
ruleloom report
```

`--head` defaults to `WORKTREE`; pass a commit or ref explicitly when assessing
a committed range. Assessment appends an observation and prediction by default.
Do not use `--no-record` for pilot measurements; that option is for dry runs.
`--include-shadow` requires `--blind`, and blind mode cannot use `--no-record`.
Crucially, `--blind` redacts match details from stdout only: it does not hide
`.ruleloom/shadow/`, `.ruleloom/predictions.jsonl`, attestations, or other local
files from the same OS user. A scientifically valid shadow run therefore needs
a separate service account with ACLs or an isolated CI observer whose workspace
and logs the agent and outcome adjudicator cannot access. If both run as the same
user with access to those files, the condition is not blinded.

Repeated assessments remain in the immutable log, but `report` groups by exact
`policy_set_hash` and keeps only the earliest prediction for each stable
`unit_id`. It resolves a single consistent outcome across observations whose
`source.change_id` is that unit, requires the outcome to mature later, and never
pools different experiments, repositories, observation units, outcome
definitions, targets, pack versions, extractors, include/exclude scopes,
pack configurations, thresholds, configurations, or policy sets. Shadow elapsed
days are likewise calculated from earliest retained unit predictions.

`assess` also writes a non-versioned, hash-bound recording attestation in
Git-private metadata for this checkout/worktree, within five minutes of
`predicted_at`. Reporting accepts only predictions that pass this local check.
Do not move the pilot between worktrees mid-window: the supported CLI will reject
ordinary copied JSONL records because it has no command to attest past
predictions. A reviewed shadow artifact copied into a new worktree is likewise
inactive until a human inspects the exact manifest and runs `ruleloom trust`.
These controls catch copies and accidental tampering; they are not proof against
a malicious process with same-user access to files and Git-private metadata.

Hiding the result is essential: if the outcome adjudicator sees a suggestion and
asks for a test because of it, the prediction changes its own label.

### Shadow metrics

The built-in `report` is aggregate per exact policy set, not per rule. It emits
the following outcome/coverage metrics; clause-specific test and shadow metrics
are enforced during approval but are not exposed as a general prospective
per-rule report:

| Metric | Definition | Why it matters |
|---|---|---|
| Precision | `TP / (TP + FP)` | Cost of distracting suggestions |
| Recall | `TP / (TP + FN)` | Fraction of later needs identified |
| F1 | Harmonic mean of precision and recall | Compact comparison, not sufficient alone |
| Balanced accuracy | Mean of sensitivity and specificity | More informative under class imbalance |
| MCC | Correlation of prediction and outcome from all four confusion cells | Primary balanced comparator for promotion |
| Prevalence | Mature positives / all mature post-prediction outcomes | Makes class balance visible |
| Predicted-positive rate | Matches / all mature post-prediction outcomes | Reveals over-alerting independently of prevalence |
| Coverage | Stable units whose earliest prediction matched / all stable units in one policy set | How often guidance would exist |
| Abstention | Stable units whose earliest prediction had no match / all stable units in one policy set | Selectivity; in the binary outcome metric it is the negative prediction |

The following are required manual pilot measurements in the current CLI, not
fields produced by `ruleloom report`:

| Metric | Definition | Why it matters |
|---|---|---|
| False-positive burden | False matches per developer-week and minutes to dismiss | Human cost |
| Lead time | Outcome time minus assessment time | Whether the signal arrives early enough |
| Stability | Similarity of rules under bootstrap relearning | Sensitivity to the sample |
| Latency | Assessment wall time, median and tail | Workflow overhead |
| Context size | Rendered rule bytes/tokens and number of clauses | Agent-context cost |
| Drift | Metric and predicate-prevalence change over time | Need to retrain or deprecate |

Keep unknown outcomes out of confusion counts but include them in the
`still_unknown` count. `report` also exposes total predictions, unique
observations (the retained name for unique stable units), duplicate predictions,
mature-after-prediction cases,
pre-existing outcomes excluded from prospective evaluation, total matches and
abstentions, coverage, and the matched/abstained counts actually eligible for
outcome metrics. A label is prospective only when its `available_at` is
strictly later than the earliest `predicted_at` for that `unit_id` and policy
set. Report the denominator for every percentage.

Reports are separated by immutable `policy_set_hash`; never pool predictions
made under different policies. Use `ruleloom report --policy-set <hash>` when a
single unwrapped policy-set report is needed. The report itself does not emit
confidence intervals. Promotion's precision and recall gates use Wilson 95%
lower bounds, while its MCC gate uses the point estimate; any broader
uncertainty analysis is a separate, pre-registered artifact.

### Day-one outputs

Save these three outputs before interpreting any rule:

```bash
ruleloom doctor
ruleloom readiness
ruleloom report
```

`readiness` reports observations, label counts, fact/label evidence coverage,
distinct predicates, a coarse stage, and warnings. Its stages—collection below
20 positives, shadow at 20–49, and preliminary evaluation at 50 or more—mirror
readiness gates; they are not statistical power claims. Immediately after
initialization, before collection or prediction, `report` is expected to be
empty and valid:

```json
{
  "note": "Policy sets are reported separately and must not be pooled.",
  "policy_sets": {},
  "readiness": {
    "distinct_predicates": 0,
    "fact_evidence_coverage": 0.0,
    "label_evidence_coverage": 0.0,
    "labeled": 0,
    "negative": 0,
    "observations": 0,
    "positive": 0,
    "stage": "collection",
    "unknown": 0,
    "warnings": ["fewer than 20 positive outcomes: learn only exploratory rules"]
  },
  "target": "needs_extra_validation"
}
```

After the first assessment, its hash appears under `policy_sets`. Each nested
report includes `readiness`, counts, coverage, prospective metrics, and an
`interpretation` string. Zero metrics with no mature post-prediction cases mean
“not measurable yet,” not evidence of no effect. An assessment with no reviewed
policies is also recorded: it forms an empty-policy set and correctly abstains.

### Daily pilot log

```text
date:
experiment_id / evidence_protocol_hash:
pack@version / pack_config_hash / resolved predicates:
feature-design revision / lock time / configuration attempt:
include-exclude outcome scope / thresholds:
policy_set_hash:
eligible changes:
observations collected / failures:
new mature positive / negative / unknown:
total matches / abstentions / coverage:
evaluated matches / abstentions:
TP / FP / TN / FN available today:
median / p95 assessment latency:
rule or extractor warnings:
label disagreements:
protocol deviations:
decision: continue shadow | pause | repair instrumentation | stop
```

## End of this pilot — no controlled visibility

Stop after Phase 4. The initial experiment is shadow-only: never promote a
candidate to `approved`, never run `sync-agents`, and never reveal shadow
matches, policies, predictions, or observer logs to the coding agent or outcome
adjudicator. Passing a configured gate is a measurement result, not permission
to change that protocol.

A future controlled-visibility study would need a new experiment ID, outcome
contract, access plan, pre-registration, and runbook reviewed independently of
this pilot. It should use a suitable comparison such as randomized eligible
changes or a pre-specified stepped rollout, record model/tool/workflow
confounders, and choose its power and uncertainty analysis from the baseline
rate and minimum worthwhile effect. None of those actions is authorized by this
document.

## Stop, repair, and continue criteria

Pause and repair instrumentation when:

- extraction coverage falls below the pre-registered gate;
- any feature uses post-outcome information;
- a merge/final diff contains files added because of the target event;
- absence is being confused with extractor failure;
- content required by the selected pack cannot be collected completely;
- mixed or out-of-scope units cannot be linked to a component-specific outcome;
- observations were collected under different pack versions, pack
  configurations, scopes, or thresholds;
- configured predicates, globs, or scopes were chosen or edited after labels,
  learned rules, metrics, or holdout errors were inspected;
- the retrospective cohort contains multiple commits from one real-world change
  or commit independence cannot be audited;
- a tuned protocol reuses its already inspected holdout as confirmatory evidence;
- commit/PR/outcome linkage is unreliable;
- duplicate observations or non-deterministic facts appear;
- labels cannot be applied consistently;
- the holdout lacks either class.

Keep RuleLoom in research-only shadow mode when:

- its holdout MCC does not beat the best registered baseline;
- a separately pre-registered uncertainty analysis remains too inconclusive for
  a decision (the current CLI does not compute intervals itself);
- stability is below the registered gate;
- clauses encode obvious confounders or are not actionable;
- useful coverage is negligible;
- false-positive burden or latency exceeds its guardrail.

Stop the product experiment when representative prospective evidence continues
to show no net value, labels are economically impossible to maintain, or users
do not trust and act on otherwise accurate guidance.

Do not advance to controlled visibility within this pilot. Document whether the
shadow run continued, paused, or stopped, including negative evidence and any
overrides.

If the shadow rule drifts, becomes noisy, or its predicate/target contract
changes, stop using it with a reviewed tombstone:

```bash
ruleloom deprecate <candidate-id> --reviewer <reviewer> \
  --note "Reason this exact active policy must no longer be used"
```

Deprecation preserves the shadow record for audit while excluding that candidate
ID from active policy loading. No agent skill exists in this shadow-only pilot,
so there is nothing to synchronize.

## Interpretation template

Use language that matches the phase:

- Day one: “RuleLoom collected X/Y eligible changes; no effectiveness conclusion
  is possible.”
- Retrospective: “The rule set was associated with target outcomes in a later
  holdout and did/did not beat the registered baselines.”
- Shadow: “Without being shown, matching rules prospectively predicted X of Y
  mature outcomes at the observed coverage and cost.”
- Controlled: “Making guidance visible changed the pre-registered outcome by
  the estimated amount under this design and uncertainty interval.”

Never translate a retrospective or uncontrolled association into “RuleLoom
prevented defects.”
