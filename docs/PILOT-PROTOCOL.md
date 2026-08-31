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
| Unit of observation | Canonical `git_commit` for retrospective learning; configured `git_worktree` snapshots keyed by one stable PR/task/change ID for prospective assessment; never pool unit kinds in one cohort |
| Prediction time | Exact point at which all feature facts are available |
| Target | One outcome with a single operational definition |
| Evidence profile | Schema version plus one exact `pack@version`; use `generic_changes@1` unless the experiment has pre-registered a supported technology-specific pack |
| Included paths | Repository-relative `evidence.include_paths` globs defining the eligible component(s) |
| Excluded paths | Repository-relative `evidence.exclude_paths` globs defining generated, vendored, or otherwise ineligible material |
| Change thresholds | Exact `large_change_churn`, `multi_file_count`, and `metadata_file_limit` values |
| Positive label | Independent event that counts as positive |
| Negative label | Maturity condition plus absence of a positive event |
| Unknown label | Every case not yet mature or not adjudicable |
| Maturation window | Time or workflow event after which negative is allowed |
| Retrospective window | Oldest and newest eligible historical change |
| Holdout rule | Latest chronological fraction, never a random sample |
| Primary predictive metric | Suggested: MCC for the promotion comparison; precision may be the operational priority when false prompts dominate cost |
| Secondary metrics | Precision, recall, F1, balanced accuracy, prevalence, predicted-positive rate, coverage, confusion counts |
| Baselines | Never alert, always alert, train-majority, and best train-selected single literal |
| Shadow duration | A time window and/or number of mature eligible changes |
| Product outcome | One later workflow/quality measure for a controlled phase |
| Cost guardrails | Latency, tokens, reviewer time, extra validations, false alarms |
| Stop conditions | Conditions listed below, customized before data inspection |

Choose and record the pack version, path scope, and change thresholds before
inspecting outcomes or collecting the experiment dataset. Do not redefine the
target, window, primary metric, pack version, scope, or thresholds after seeing
which choice produces a better rule. If any of those definitions must change,
start a new experiment ID and dataset, recollect the observations under the new
profile, and preserve the previous result. Never append observations from two
evidence profiles to one dataset or pool their candidates, predictions, or
metrics.

## Recommended first target

The default target is `needs_extra_validation`. For the first pilot, keep it
narrow and tied to the normal review process:

- **Positive:** after the prediction point, an independent CI or human review
  signal identifies a concrete missing validation and the change is updated with
  that validation (for example, an accepted request for an automated test,
  contract check, or reproducible failing scenario).
- **Negative:** the chosen review/maturation event completes without such a
  signal.
- **Unknown:** review is ongoing, the outcome is ambiguous, the change was
  abandoned for unrelated reasons, or source linkage is incomplete.

“The change looks risky” is not an outcome label. A test that was already present
at prediction time is a feature, not proof of a later positive outcome. Do not
label from the same diff pattern used to generate the facts.

Post-merge regressions should be a separate target with a longer maturity
window, such as `linked_regression`. Combining review requests and production
defects in one label makes the learned rule difficult to interpret.

## Phase 0 — privacy and workflow mapping

Before initializing the target repository:

- confirm that local processing of commit paths, metadata, and labels is allowed;
- identify how commits map to pull requests, CI outcomes, review requests, and
  later incidents;
- choose the observation unit and target contract above;
- run `ruleloom packs list`, choose one exact `pack@version`, list its generated
  predicates, and verify that each uses only decision-time data;
- pre-register included/excluded path globs and the large-change, multi-file,
  and metadata-sampling thresholds before inspecting outcome labels;
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
  --pack generic_changes --pack-version 1 --agents none
ruleloom doctor
ruleloom readiness
```

`ruleloom init` requires an existing Git repository with either
`remote.origin.url` configured or at least one commit; establish one of those
identity anchors first. The current release supports macOS and Linux, not
Windows, because its storage lock uses POSIX `fcntl`. `generic_changes@1` is the
schema-v2 default and provides language-neutral change-shape, test-path,
documentation, CI, and dependency-file facts. Passing no `--pack-version`
selects the latest registered version, but a pre-registered pilot should always
pin it explicitly.

Inspect `.ruleloom/config.json` before collection. New pilots use
`schema_version: 2`. Freeze its `experiment_id`, derived `repository_id`,
`prediction_unit`, `outcome_definition`, target, `pack`, `pack_version`, and the
entire `evidence` object. RuleLoom includes the resolved extractor and all these
fields in the evidence-protocol hash recorded on every observation. A typical
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

The include scope defines eligible outcome units. Schema-v2 direct collection
fails closed for a unit that mixes included and outside-include files; backfill
skips mixed units and units with no included files. Widen the pre-registered
include set when an outcome legitimately covers several components, or use a
component-specific change/outcome unit. Configured excludes within the include
set do not make a unit mixed and are intended for generated or vendored paths.
Archive each backfill's JSON audit fields: `examined`, `eligible`, `skipped`,
`skipped_by_reason`, the bounded `skipped_preview`, its truncation count, and
`skipped_manifest_hash`. These preserve the sampling denominator and distinguish
mixed units from wholly out-of-scope units without emitting an unbounded list.

The initial deterministic learner is the appropriate smoke-test engine because
it has no external solver.
All managed data/artifact paths must remain below `.ruleloom/` and must not
overlap. Configure Popper only as a separately pinned experiment with
`max_rules=1` and `bootstrap_runs=0`, after provisioning its compatible Python
environment, SWI-Prolog, and GNU `timeout` offline. RuleLoom does not download or
install them. The Popper adapter has not yet completed a real end-to-end run in
the reference development environment, so an initial installation must use
`horn`.

Collect a small known range first:

```bash
ruleloom collect git --base <old-ref> --head <new-ref>
```

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
- fact evidence points to the correct path, changed-line marker, or threshold
  calculation, as applicable to the selected pack;
- no predicate uses information created after prediction time;
- missing facts mean “verified absent,” not “extractor failed”;
- only in-scope files contribute to counts, thresholds, or pack predicates;
- `scope_outside_files` is zero; mixed and wholly out-of-scope units were not
  admitted to the dataset;
- labels remain `unknown` until the registered maturity event;
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
generic and Flutter profiles; compare them only as separately pre-registered
experiments.

Record extraction coverage as:

```text
eligible changes with a valid observation / all eligible changes
```

Also record parse failures, unsupported changes, duplicate IDs, and collection
duration. A high model score cannot repair untrustworthy extraction.

## Phase 2 — label historical outcomes

Apply labels from independent outcome evidence:

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

Historical reconstruction is useful but vulnerable to survivorship bias and
incomplete linkage. Report its results separately from prospectively collected
labels. After importing labels, audit at least one positive and one negative
against their independent sources; this is an outcome audit, separate from the
pre-label extraction audit in Phase 1.

## Phase 3 — retrospective temporal evaluation

Validate and learn:

```bash
ruleloom validate
ruleloom readiness
ruleloom learn --engine horn
ruleloom candidate list
ruleloom candidate show <candidate-id>
```

`validate` is a whole-project fail-closed check: it reads observations,
candidate manifests, active shadow/approved artifacts, deprecation tombstones,
local transition attestations, and locally attested prediction records. A
successful result establishes structural/provenance consistency, not label
truth or predictive value.

When all eligible observations carry compatible repository topology, the latest
first-parent positions form the holdout; otherwise RuleLoom falls back to
timestamps. Commit timestamps remain audit evidence, and backdated/tied values
produce warnings. The current default mechanical
minimum is eight training and four test examples. That minimum allows the
pipeline to run; it is not enough by itself for a reliable business conclusion.
Always show raw confusion counts. Candidate/report metrics are point estimates;
the prospective promotion gate separately computes Wilson 95% lower confidence
bounds for precision and recall. If the pilot needs broader uncertainty
analysis, pre-register and run it separately. If one class is absent from train
or test, stop and collect more evidence.

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

The default `shadow` gate requires at least 20 positive outcomes and a non-empty
learned rule set. Before this first reviewed transition, RuleLoom requires the
current dataset hash to match, relearns the candidate from the exact
evidence/configuration, and requires an identical identity payload; a
non-reproducible manifest cannot enter shadow. The built-in Horn path is
deterministic; an optional external engine still has to reproduce exactly.

The default `approved` gate expects at least 50 positives, a non-empty
chronological test set, aggregate holdout precision at least 0.75, recall at
least 0.50, stability at least 0.40, and holdout MCC strictly greater than the
best of all four baselines. It also requires the exact prior shadow artifact and
the following attributable prospective evidence:

- at least 30 shadow predictions on distinct stable units;
- at least 30 outcomes that became knowable strictly after prediction, including
  at least 10 positive and 10 negative outcomes;
- a span of at least seven days between the earliest retained predictions for
  the first and last stable units;
- aggregate shadow precision with a Wilson 95% lower bound of at least 0.70,
  recall with a Wilson 95% lower bound of at least 0.50, and point-estimate MCC
  at least 0.10; and
- for every learned clause, at least one temporal-holdout match, at least 10
  prospective matches, temporal point precision at least 0.75, and a
  prospective Wilson 95% precision lower bound of at least 0.70.

Dataset/config/pack-version/scope/threshold/target identity, candidate
reproduction, the recorded shadow transition, temporal sample/metrics
completeness, and the prospective/per-clause requirements are non-overridable.
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
   protocol snapshot, target, exact policy snapshots
   (candidate/status/target/manifest hash/rule signatures), policy-set hash,
   matches, abstention, and timestamp;
6. let the normal repository process continue unchanged;
7. attach the outcome only when it matures.

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
thresholds, configurations, or policy sets. Shadow elapsed days are likewise
calculated from earliest retained unit predictions.

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
pack@version / include-exclude scope / thresholds:
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
- absence is being confused with extractor failure;
- content required by the selected pack cannot be collected completely;
- mixed or out-of-scope units cannot be linked to a component-specific outcome;
- observations were collected under different pack versions, scopes, or
  thresholds;
- commit/PR/outcome linkage is unreliable;
- duplicate observations or non-deterministic facts appear;
- labels cannot be applied consistently;
- the holdout lacks either class.

Keep RuleLoom in research-only shadow mode when:

- its holdout MCC does not beat the best of the four registered baselines;
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
