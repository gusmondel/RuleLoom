# Data schema

## Principles

RuleLoom configuration schema versions 1–3 and artifact schema version 1 use
local, provider-neutral JSON/JSONL. Their persisted
artifacts are designed to answer:

- what was known at prediction time;
- where each fact and label came from;
- which observations trained and tested a rule;
- which learner/configuration produced it;
- whether a human reviewed it for shadow or approved use;
- which exact policy set made each prospective prediction; and
- whether this checkout/worktree locally attested a reviewed transition or a
  timely prediction.

JSON is canonicalized with sorted keys and compact separators before hashing.
Arrays whose order is not semantic are written deterministically. Timestamps are
ISO 8601 strings with an explicit timezone.

## Project layout

Default paths are relative to the initialized repository:

```text
.ruleloom/
  config.json
  observations.jsonl
  history/
    events.jsonl
    change-units.jsonl
  candidates/
    <candidate-id>.json
  shadow/
    <candidate-id>.json
  approved/
    <candidate-id>.json
  deprecated/
    <candidate-id>.json
  predictions.jsonl
```

Agent adapters are derived artifacts, not the source of truth:

```text
.agents/skills/ruleloom/SKILL.md
.claude/skills/ruleloom/SKILL.md
```

Shadow candidates can be included only by an explicit assessment flag. Only
approved candidates may be rendered into agent adapters.

Hash-bound trust attestations are deliberately not stored in this versioned
layout. They live in Git-private metadata namespaced to the resolved
checkout/worktree. Consequently, copying `.ruleloom/` copies evidence but not
local authority recognized by the normal loader. This is a copy/accidental-
tampering defense, not proof against a malicious process with access as the same
OS user.

## Configuration

`.ruleloom/config.json` follows this shape:

```json
{
  "schema_version": 2,
  "project": "example-project",
  "target": "needs_extra_validation",
  "pack": "generic_changes",
  "pack_version": 1,
  "dataset": ".ruleloom/observations.jsonl",
  "candidates_dir": ".ruleloom/candidates",
  "shadow_dir": ".ruleloom/shadow",
  "approved_dir": ".ruleloom/approved",
  "deprecated_dir": ".ruleloom/deprecated",
  "predictions": ".ruleloom/predictions.jsonl",
  "protocol": {
    "experiment_id": "example-shadow-v2",
    "repository_id": "repo.0123456789abcdef0123",
    "prediction_unit": "git_worktree",
    "outcome_definition": "Independent review outcome recorded after the prediction"
  },
  "evidence": {
    "include_paths": ["**"],
    "exclude_paths": [],
    "large_change_churn": 200,
    "multi_file_count": 3,
    "metadata_file_limit": 512
  },
  "learner": {
    "engine": "horn",
    "max_body": 3,
    "max_rules": 3,
    "allow_negation": true,
    "min_precision": 0.7,
    "min_support": 2,
    "false_positive_cost": 1.5,
    "bootstrap_runs": 30,
    "max_predicates": 12,
    "popper_dir": null,
    "popper_timeout_seconds": 120
  },
  "evaluation": {
    "test_fraction": 0.25,
    "min_train_examples": 8,
    "min_test_examples": 4,
    "seed": 17,
    "test_start_at": "2025-01-01T00:00:00Z"
  },
  "promotion": {
    "min_test_precision": 0.75,
    "min_test_recall": 0.5,
    "min_stability": 0.4,
    "require_test_set": true,
    "min_positive_for_shadow": 20,
    "min_positive_for_approval": 50,
    "require_baseline_improvement": true,
    "min_shadow_predictions_for_approval": 30,
    "min_shadow_mature_outcomes_for_approval": 30,
    "min_shadow_days_for_approval": 7,
    "min_shadow_precision": 0.7,
    "min_shadow_recall": 0.5,
    "min_shadow_mcc": 0.1,
    "min_shadow_positive_outcomes_for_approval": 10,
    "min_shadow_negative_outcomes_for_approval": 10,
    "min_shadow_matches_per_rule_for_approval": 10
  }
}
```

Version 0.8.0 defaults to configuration schema v2 and the language-neutral
`generic_changes@1` pack. It also ships schema-v3 `configured_paths@1` and
`flutter_testing@2`. The frozen `flutter_testing@1` implementation exists
only to read structurally and reproduce the hashes of historical configuration
schema-v1 experiments. That compatibility path does not inherit the collection
and validation guarantees of schema v2 and is not a current ingestion profile;
re-extract the complete history into a fresh supported schema-v2 or schema-v3
experiment before making new comparisons or policy decisions. `ruleloom packs
list` reports the exact extractor and static/shared predicates for each built-in
version and marks configurable packs; the resolved configured-path vocabulary
exists only after loading one project's canonical `pack_config`. External
executable plugins are not loaded in this release: adding a built-in language
pack uses the same explicit contract and registry without changing learning,
evaluation, or policy lifecycle code.

`protocol.prediction_unit` accepts `git_commit`, `git_range`, `git_worktree`, or
`provider_change`; the last value identifies one provider-grouped change whose
exact snapshot is supplied by a point-in-time adapter. The optional aware
`evaluation.test_start_at` freezes an exact chronological boundary: observations
strictly before it are training candidates and observations at or after it are
holdout candidates. When absent, `test_fraction` selects the latest fraction.
In both cases, training labels unavailable at the holdout start are embargoed.

`evidence.include_paths` and `exclude_paths` define one repository-relative
scope per experiment. Change-size thresholds and the metadata preview limit are
also explicit. Configure these from a pre-outcome design sample and freeze them
before collection; changing any of them creates a different evidence protocol.
For schema-v2/v3 collection, the include set is an outcome-eligibility boundary:
direct collection rejects mixed inside/outside units, and backfill omits mixed
or wholly out-of-scope commits. Excludes within the include set may remove
generated or vendored paths without making the unit mixed.

### Schema-v3 configured path predicates

Configuration schema v3 adds required `pack_config`. Static packs accept only
the explicit empty object `{}`. `configured_paths@1` requires the following
fields; this is an excerpt, so retain the remaining top-level fields from the
complete configuration above:

```json
{
  "schema_version": 3,
  "pack": "configured_paths",
  "pack_version": 1,
  "pack_config": {
    "path_predicates": [
      {
        "predicate": "touches_client_ui",
        "include_paths": ["components/client_ui/**"],
        "exclude_paths": ["components/client_ui/generated/**"]
      },
      {
        "predicate": "touches_shared_contract",
        "include_paths": ["interfaces/contracts/**"],
        "exclude_paths": []
      }
    ]
  },
  "evidence": {
    "include_paths": ["components/**", "interfaces/**"],
    "exclude_paths": ["**/vendor/**"],
    "large_change_churn": 200,
    "multi_file_count": 3,
    "metadata_file_limit": 512
  }
}
```

The two path layers are deliberately different. `evidence.include_paths` and
`evidence.exclude_paths` define whether the complete change and its eventual
outcome belong to the experiment. Each `pack_config.path_predicates` entry is a
feature definition applied only to the visible files already admitted by that
scope. A configured feature never widens eligibility and cannot make an
out-of-scope file part of the observation.

For one configured predicate and one normalized in-scope path, matching means
“matches at least one `include_paths` glob and no `exclude_paths` glob.” The
predicate is true if any visible changed path satisfies that condition. A path
may activate several predicates. Added, modified, deleted, renamed, and binary
paths participate through normalized Git path evidence; source contents do not.
RuleLoom-managed paths and generated RuleLoom agent adapters are internal and do
not activate predicates.

Configured names must be unique lowercase predicates beginning with
`touches_`, at most 64 characters, distinct from the target and shared pack
facts. Every entry must include a non-empty `include_paths` array and an explicit
`exclude_paths` array, which may be empty. Globs are repository-root-anchored and
support literals, `*`, `?`, and `**` only as a complete segment. They are not
Git pathspecs or `.gitignore` syntax. Absolute paths, empty or `.`/`..` segments,
backslashes, Git pathspec magic, bracket/brace classes, control characters,
duplicates, and non-portable `**` placement are rejected.

The fail-closed bounds are 32 predicates, 32 include and 32 exclude globs per
predicate, 256 include/exclude globs in total, 256 characters per glob, and
5,000,000 potential path/glob comparisons per extraction. A second complexity
gate permits at most 200,000,000 estimated matcher work units; each evidence path
is limited to 4,096 characters and 256 components. Predicate entries and their
glob arrays are sorted canonically before hashing, so order alone does not change
identity. `pack_config_hash` is the SHA-256 content hash of that canonical object.
`configured_paths@1` layers these dynamic facts on the shared generic
facts `large_change`, `multi_file_change`, `touches_ci`,
`touches_dependencies`, `touches_docs`, and `touches_test`.

The bundled JSON Schema validates local structure and per-field bounds, but it
is not the complete semantic authority. Cross-field and aggregate invariants—
including at most 256 globs across all predicates, unique predicate names,
collision with the target/shared facts, canonical ordering, and the weighted
matcher budget—are enforced by the Python configuration/pack loader and
extractor runtime. Passing `config.schema.json` alone does not make a
configuration executable or valid; load it through RuleLoom and run `validate`.

The configured vocabulary is hand-authored feature selection, not a learned or
portable ontology. Freeze its full list from outcome-blind architecture evidence
before inspecting labels, learned rules, metrics, or holdout errors. Record its
author, design revision, rationale, lock time, and hash in the external
pre-registration because those audit fields are not accepted inside strict
`pack_config`. Changing a glob or predicate after outcome inspection creates a
new experiment and requires an untouched future confirmation window; the
already inspected holdout is no longer a test set.

All six configurable managed paths must be repository-relative, remain below
`.ruleloom/`, contain no `..` or
control characters, and be pairwise non-overlapping after portable Unicode/case
normalization. Managed symlink components are rejected at access time.
Predicate-like fields start with a lowercase letter and contain lowercase ASCII
letters, numbers, and underscores. `init` derives `repository_id` from
`remote.origin.url`, or from root commits when no origin exists; initialization
therefore requires an origin or at least one commit. The storage lock uses POSIX
`fcntl`: version 0.8.0 supports macOS and Linux, not Windows.

The built-in search also enforces finite operational bounds:
`max_body` 1–4, `max_rules` 1–10, `max_predicates` 1–32,
`bootstrap_runs` 0–100, and Popper timeout 1–3600 seconds, plus a combined
hypothesis/work budget. For `engine="popper"`, version 0.8.0 requires
`max_rules=1`, `bootstrap_runs=0`, and the Horn-specific support/precision/cost
settings at their defaults. Popper is an offline adapter to an explicitly
configured, already provisioned checkout; RuleLoom does not install it.
Its candidate `engine_version` combines the RuleLoom adapter revision with the
fingerprint of the external checkout and probed runtime environment.

Changing configuration changes `config_hash`. A candidate must retain the hash
of the configuration used to generate it. Separately, `evidence_protocol_hash`
hashes `schema_version`, `experiment_id`, `repository_id`, `prediction_unit`,
`outcome_definition`, `target`, `pack`, `pack_version`, the exact extractor, and
the complete evidence scope/threshold profile. In schema v3 it additionally
hashes the complete canonical `pack_config`, including `{}` for a static pack.
Every observation records that hash, preventing evidence from different
experiments, repositories, units, outcome definitions, pack versions, pack
configurations, scopes, or thresholds from being pooled accidentally. The
positive-count gates are readiness heuristics, not a
statistical power calculation. With
`require_baseline_improvement`, approval also requires test MCC to be strictly
greater than the best recorded baseline MCC. Approval additionally requires
attributable evidence from the exact shadow manifest: by default 30 independent
unit predictions, 30 outcomes that matured later, at least 10 positive and 10
negative outcomes, 10 matches per clause, and at least seven days between the
first and last earliest unit prediction. Precision 0.70 and recall 0.50 are
Wilson 95% lower-bound floors; MCC 0.10 is a point-estimate floor. Each clause
also has its prospective match and Wilson precision gates. The shadow and
per-clause integrity gates cannot be bypassed with `--override`.

## Observation record

Each non-empty line in `.ruleloom/observations.jsonl` is one object:

```json
{
  "schema_version": 1,
  "id": "commit.a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
  "observed_at": "2026-08-20T14:31:22-04:00",
  "protocol_hash": "be2523c451e7156855f365ecc6e0100a59f9f195abb3726a59fdf286a1e84845",
  "facts": [
    "large_change",
    "touches_test"
  ],
  "labels": {
    "needs_extra_validation": "positive"
  },
  "label_evidence": {
    "needs_extra_validation": {
      "kind": "review",
      "available_at": "2026-08-21T11:10:00-04:00",
      "source": "review/123",
      "reason": "Independent review required an additional validation",
      "confidence": 1.0
    }
  },
  "fact_evidence": {
    "large_change": {
      "kind": "deterministic",
      "extractor": "ruleloom.generic_changes.git.v1",
      "evidence": ["churn:240>=200"]
    },
    "touches_test": {
      "kind": "deterministic",
      "extractor": "ruleloom.generic_changes.git.v1",
      "evidence": ["path:tests/settings_test.py"]
    }
  },
  "source": {
    "kind": "git_commit",
    "repository": "repo.0123456789abcdef0123",
    "base": "91ab20ef1234567890abcdef1234567890abcdef",
    "head": "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
    "pack": "generic_changes",
    "pack_version": 1,
    "extractor": "ruleloom.generic_changes.git.v1"
  },
  "metadata": {"topological_index": 418}
}
```

### Observation fields

| Field | Type | Contract |
|---|---|---|
| `schema_version` | integer | Must be artifact schema `1` in version 0.8.0. |
| `id` | string | Unique within the dataset; lowercase letters/numbers plus `.`, `_`, or `-`. |
| `observed_at` | string | Decision-time timestamp with timezone; used for chronological splitting. |
| `protocol_hash` | string | Lowercase SHA-256 of the configured evidence protocol; evidence with a different hash must not be pooled. |
| `facts` | array of strings | Unique unary Boolean predicates true for this observation. |
| `labels` | object | Target predicate to `positive`, `negative`, or `unknown`. |
| `label_evidence` | object | Outcome provenance keyed by target; required for every mature label. |
| `fact_evidence` | object | Structurally optional for partial readiness records; every fact in a validated built-in-pack observation requires matching deterministic provenance. |
| `source` | object | Provider-neutral source identity, references, and collection context. |
| `metadata` | object | JSON audit information not consumed as facts unless an extractor explicitly emits it. |

Every schema-v2/v3 collected source records `kind`, the derived stable
`repository`, `pack_version`, `pack`, and versioned `extractor`; frozen schema-v1
records retain their original shape without `pack_version`. A configured-path
source additionally records `pack_config_hash`, which must match both the
canonical project configuration and the resolved pack descriptor. Prospective
sources record `change_id`, which must match the Prediction `unit_id`. Git
sources also retain the relevant `base` and `head`; these identify the immutable
snapshot, while `change_id` identifies the independent real-world change across
snapshots.

Change metadata keeps exact aggregate counts and a SHA-256 manifest of every
scoped path/churn tuple, but bounds path previews by count and byte budget.
`metadata_files_truncated` states how many entries were omitted from the
preview. Facts are still computed over the complete scoped diff. If required
content exceeds its safety budget, collection fails closed instead of storing a
partial observation whose missing facts could be mistaken for logical falsehood.
Validated observations must use only predicates declared by their exact pack,
have one `fact_evidence` entry per fact, and name that pack's deterministic
extractor in every entry. Scope metadata records total, included, outside, and
explicitly excluded file counts. New collection never persists mixed or empty
scope units. Git text/path output must be UTF-8; lossy replacement is forbidden.
For `configured_paths@1`, metadata additionally records
`configured_paths_config_hash`, per-predicate
`configured_path_match_counts`, `configured_unmatched_files`,
`configured_overlapping_files`, and the full
`configured_match_manifest_hash`. These audit fields do not alter the fact
vocabulary.

For an amended provider experiment, extraction may instead combine exact path
names enumerated from local Git trees with complete aggregate additions,
deletions, and changed-file count captured at prediction time. It is accepted
only when the provider count exactly equals the Git path manifest, no configured
exclusion participated, scope is complete, and the pack does not require file
contents. Metadata marks `file_churn_available=false`; per-file churn and entropy
remain unavailable rather than being imputed. The aggregate source and values
are included in the observation manifest and evidence provenance.

Observation IDs identify immutable snapshots and cannot repeat. For prospective
assessment, `source.change_id` is the stable independent-unit key: repeated
snapshots of one PR/task/change reuse it, while unrelated changes must never do
so. Collection may merge the same immutable snapshot, and labeling may perform
one `unknown`-to-mature transition for its target. The
CLI rejects changing an existing `positive` or `negative` label; record a
corrected observation with a new identity and preserve the correction in the
authorized audit system. Persisted Git history is ordered by first-parent
topological position when that provenance is available, otherwise by timestamp
then ID.

First-parent order establishes ordering, not a valid decision point. A merge or
squash may already contain validation added because of CI or review. Version
0.8.0 therefore distinguishes raw `git_commit` evidence from grouped
`historical_change` observations. The latter bind one stable `change_id` to an
exact `base_sha`, `prediction_sha`, and prediction time. Only a `rich` unit with
a genuinely persisted point-in-time snapshot can be confirmatory. `git_only`
and `final_only` cases remain useful for exploration but cannot support approval.
`git_range` and `git_worktree` remain prospective units. Never alter
`observed_at` or `available_at` to force eligibility.

## Historical bootstrap records

Historical records use their own artifact schema v1. They are independent of a
programming language and provider: adapters normalize forge, review, CI, revert,
and incident data before the evidence pack re-extracts facts from Git.

`ruleloom history import-github --repository OWNER/NAME` is a bounded adapter
over the authenticated `gh api --hostname github.com` transport. By default the
requested repository must equal the public-GitHub `OWNER/NAME` parsed from an
unambiguous HTTPS, SSH, or SCP-style `remote.origin.url`. A reviewed mirror or
import can use `--allow-unverified-repository`; the report and manifest hash
record the resulting `repository_binding`. GitHub Enterprise hosts are not
supported by this adapter version.

`since` filters PR creation times and the repository-commit scan. `until` is an
inclusive as-of cutoff for PR creation/finalization and retained review, check,
and revert events; a PR finalized after it is skipped. It
defaults to collection time and does not reconstruct a historical provider
snapshot. It filters one collection and cannot remove records already imported
into the append-only ledger; an earlier as-of evaluation therefore needs a
clean experiment/log. The adapter requests closed PRs only, groups their
currently available commit lineage, and records the reconstructed prediction
event with `point_in_time: false`. An unexpectedly non-closed or force-pushed PR
that cannot be normalized safely is skipped. Every resulting
unit has `kind=github_archive_change`, `evidence_quality=git_only`, and
`confirmatory=false`: archived state cannot prove the exact patch visible at PR
opening. Pagination bounds and skipped/truncated counts remain part of the
adapter report; persistence separately enforces the canonical history-log
limits. Collection also has global request and top-level provider-record
budgets (20,000 and 250,000 by default). Exhausting either aborts without
persistence. A successful report exposes the global limits/use, policy, a
compact `manifest`, and `manifest_hash`. The emitted pre-hash manifest binds all
per-endpoint/global limits, warnings and counts, plus content hashes for the
exact normalized event and unit sets. Preserve the exact command, report, and
canonical logs: the compact manifest deliberately avoids duplicating every
normalized record in stdout. The
normalizer does not retain free-form titles, bodies, review text,
reviewer names, or check names. User and check identities are repository-scoped
and pseudonymized; stable PR and event numbers remain for provenance. Review
submissions are stored with a state-neutral `decision=unspecified`;
provider-side dismissal therefore cannot mutate the prior record. Check events
are versioned by PR plus normalized check content, so an updated conclusion or
completion time appends a distinct immutable event. The paired history-log
validator also pins the first built-in GitHub numeric repository identity used
for each RuleLoom `repository_id`; a different provider repository requires a
new experiment.

Provider collection does not fetch Git objects. Materialization needs each
unit's `base_sha` and `prediction_sha` in the local object database and reports
unavailable units through `skipped`, `skipped_preview`, and
`skipped_manifest_hash`; those omissions belong in coverage reporting.

The separate `ruleloom-github-event-archive/2` adapter imports a strict,
manifest-bound projection of public `PullRequestEvent` and
`PullRequestReviewEvent` records. It permits only opened/merged PR events and
created approval/changes-requested decisions, binds the repository, numeric
provider identity, time window, preregistration digest, query digest, and event
digest. The exporter separately enumerates every expected source-wide UTC hour;
the manifest records `coverage_query_sha256`, `expected_hours`,
`observed_hours`, and the bounded, sorted `missing_hours` list. Endpoint
freshness (`window_complete`) does not imply internal continuity. A negative is
eligible only if no missing hour overlaps the interval after its opening became
available and through its merge becoming available; otherwise it remains
unknown. Observed positive decisions do not depend on absence and remain
positive. The adapter rejects symlinks, oversized input, stale endpoints,
inconsistent coverage counts, and unknown fields. Actor names are pseudonymized
by the exporter before download; prose, labels, and source content are not
accepted. This research adapter can derive the atomic
`independent_review_changes_requested` target when the exact opening base/head
and sufficient event evidence are present.

`.ruleloom/history/events.jsonl` stores immutable `HistoricalEvent` objects:

| Field | Contract |
|---|---|
| `id`, `repository_id`, `kind`, `provider` | Safe lowercase identifiers. |
| `occurred_at` | When the source event happened. |
| `available_at` | When it became observable; never earlier than `occurred_at`. |
| `source_ref` | Stable, nonblank provider reference without control characters; imported text is untrusted. |
| `change_id` | Stable logical change or `null` when not linked. |
| `independent_group` | Evidence-source group used to avoid double-counting dependent votes. |
| `data` | Provider-normalized JSON; the core taxonomy makes no source-language assumptions. |

Supported semantic event kinds are `change_opened`, `change_snapshot`,
`change_merged`, `change_closed`, `change_finalized`, `review`, `ci_run`,
`revert`, and `incident`. Git ingestion additionally emits `git_commit` and
`git_merge` metadata events. A point-in-time snapshot carries `base_sha`,
`head_sha`/`prediction_sha`, and `point_in_time: true`. A structural final event
carries `final_sha`/`merge_sha`/`head_sha`; an explicit matured
`change_finalized` outcome instead carries `target`, `value`, and
`evidence_complete`. It remains an outcome—not structural finalization—even if
an adapter also includes a SHA. The assembler does not conflate the two roles.

`.ruleloom/history/change-units.jsonl` stores one immutable `ChangeUnit` per
logical change:

| Field | Contract |
|---|---|
| `id`, `repository_id`, `kind` | Stable logical identity and repository boundary. |
| `base_sha`, `prediction_sha`, `prediction_at` | Exact predictor snapshot and historical decision time. |
| `final_sha`, `finalized_at` | Both set or both `null`; a rich earlier snapshot never uses the final state as its predictor. |
| `commits`, `event_ids` | Deduplicated provenance; every event ID must resolve in the same repository and may be unscoped only through this explicit attachment. |
| `provider`, `source_ref` | Origin of the prediction snapshot. |
| `evidence_quality` | `rich`, `git_only`, or `final_only`. |
| `confirmatory` | May be true only for `rich` point-in-time units. |

For a confirmatory unit, the matching snapshot event must itself have been
available no later than `prediction_at`; a snapshot reconstructed or exposed
only afterward remains non-confirmatory even when its SHAs are exact.

The JSONL stores are canonical, size-bounded, lock-protected, and immutable by
ID. An import that adds events and change units is committed as one recoverable
two-log batch: readers take the same transaction lock and roll back an
interrupted prepared batch before exposing either file. The bounded recovery
journal and stages live in Git-private RuleLoom state rather than in
repository-controlled `.ruleloom/history`; a successful transaction removes its
journal and stages, leaving the canonical logs as the durable data. Each history
log and imported JSONL is limited to 64 MiB, each persisted canonical record or
imported input line to 1 MiB, and each file to 250,000 records. Imports use the
same shared limits as persistence, so an exported valid store remains importable.
Conflicting ID reuse fails before overwrite.

Git bootstrap additionally caps traversal at 100,000 commits and retains the
most recent prefix whose canonical event and change-unit logs both fit. Its
report exposes `storage_truncated`, `storage_byte_limit`,
`storage_line_byte_limit`, `event_log_bytes`, and `change_unit_log_bytes`.
Those fields, the ordinary `truncated` decision, and the retained records are
covered by `manifest_hash`; a consumer must not interpret a storage-truncated
sample as complete repository history.

`history materialize` re-extracts the configured pack at the prediction SHA,
sets `source.kind=historical_change`, and attaches only strictly later outcomes.
New immutable outcome events can be appended and materialized again. That may
advance an `unknown` derived label to `positive` or `negative`; a mature label,
prediction snapshot, evidence protocol, outcome target, and weak-evidence mode
cannot be rewritten in place. Each observation hashes only the events linked to
its logical change, so unrelated imports do not mutate its provenance.
Structural snapshot and finalization events must be complete before the unit ID
is assembled. Streaming exporters should import early structural events with
`--no-assemble`, then assemble once; the current schema does not mutate an open
unit into finalized or upgrade `final_only` into `rich`.

Five outcome targets remain separate:

- `validation_rework_required`;
- `independent_review_changes_requested`;
- `change_attributable_ci_failure`;
- `post_merge_revert_or_hotfix`; and
- `post_merge_defect`.

The selected atomic outcome is registered through the frozen config `target`
(the legacy `needs_extra_validation` target maps only to
`validation_rework_required`). `--outcome-target` can assert that mapping but
cannot override it. Studying another outcome requires a separate experiment
whose target and operational `protocol.outcome_definition` were fixed before
labels were inspected; both are covered by the evidence protocol hash.

Strong review/CI/link evidence and explicit complete maturation records are
enabled by default. Test changes alone, fix keywords, SZZ links, and a GitHub
revert associated only through an exact Git revert trailer are weak and require
explicit opt-in. The GitHub adapter also records a failed check on the merge
result as `attribution=unattributed_merge_result` and
`evidence_grade=weak_heuristic`; it is an opt-in weak positive vote for
`change_attributable_ci_failure`, never strong attribution. Absence, malformed
events, and conflicting independent votes
produce `unknown`; they never produce an implicit negative. Weak-dependent
labels and non-rich units are non-confirmatory, and approval rejects a candidate
trained from any such historical case.

A strong CI sequence requires strictly ordered failure, code-change, and success
times, the same provider, and the same provider-scoped stable `check_id` for the
failure and success. Adapters must namespace that identity where provider names
are not globally unique.

The GitHub archive adapter ignores every timeline label name during outcome
derivation. A timeline application record can reference the provider's mutable
Label object, whose current name may differ from its name when the historical
event occurred. Consequently, even an apparently structured name cannot prove
the original point-in-time assertion; `since`/`until`, actor separation, and
syntax checks cannot restore that lost state.

A label-backed strong outcome requires a point-in-time webhook, exporter, or
append-only adjudication ledger that captured the application point-in-time.
That trusted source must preserve the original timestamp, repository/change
identity, authorized independent actor, target, value, maturity/completeness,
and correction provenance. Its adapter may emit an immutable normalized
`change_finalized` event with explicit `target`, `value`, and
`evidence_complete`; RuleLoom then applies the ordinary chronology, conflict,
and atomic-target rules during import and materialization. RuleLoom v0.8.0 ships
a local GitHub Action/webhook capture substrate for future deliveries. It does
not reconstruct historical label names, run a durable observer daemon, or make
automatic-label coverage claims without an operational audit.

An archive label name by itself therefore contributes no vote—not even weak
evidence—and absence of such a name cannot produce a negative. The existing
unattributed merge-result check and heuristic Git revert votes remain separate
weak opt-ins; neither becomes confirmatory.

### Labels

`positive` means the target occurred under the experiment's registered
definition. `negative` means it did not occur after the registered maturity
condition. `unknown` means the outcome is not mature, unavailable, disputed, or
out of scope.

Unknown observations are excluded from learning and confusion metrics. They
must still be counted in data-readiness and label-maturity reporting.

Every `positive` or `negative` label requires a `label_evidence` entry. An
`unknown` label has none. Each entry contains:

| Field | Type | Contract |
|---|---|---|
| `kind` | enum | `ci`, `review`, `incident`, `human`, `imported`, or `synthetic`. |
| `available_at` | string | Timezone-aware time at which the outcome first became knowable; it must be strictly later than `observed_at`. |
| `source` | string | Non-empty stable source reference or authorized pseudonymous adjudicator ID. |
| `reason` | string | Concise audit explanation; it may be empty. |
| `confidence` | number or absent | Optional value from 0 through 1. |

`label_evidence` captures the resolved outcome and its provenance; version 1 is
not a multi-reviewer event log. Preserve disagreements and correction history
in a separate authorized audit system, and keep the machine label `unknown`
until the registered adjudication rule resolves them.

Do not place a future outcome in `facts` or change `observed_at` to the label
time. That would leak the answer into the predictors or temporal split. During
learning, RuleLoom splits chronologically and removes a training label whose
`available_at` is later than the start of the test period. During prospective
reporting, RuleLoom resolves one non-conflicting mature outcome across all
snapshots sharing the prediction's `unit_id`; it contributes to confusion counts
only when `available_at` is strictly later than that unit's earliest
`predicted_at` within the policy set.

The same independent change must not appear on both sides of a retrospective
split through multiple snapshots. Version 0.8.0 materializes one observation per
`ChangeUnit`, and `learn` rejects duplicate mature `change_id` values. It also
rejects mixing `git_commit` and `historical_change` cohorts. A raw commit cohort
still needs manual independence auditing; grouped history is the preferred
bootstrap path.

### Fact evidence

Each `fact_evidence` entry contains:

| Field | Type | Contract |
|---|---|---|
| `kind` | enum | `deterministic`, `agent`, `human`, or `imported`. |
| `extractor` | string | Non-empty extractor/model name and version. |
| `evidence` | array of strings | Minimal paths, line references, hashes, or source IDs supporting the fact. |
| `confidence` | number or absent | Optional value from 0 through 1, mainly for non-deterministic facts. |

`agent`, `human`, and `imported` are reserved wire-format values for future
extractors and migration tooling. In version 0.8.0, current built-in-pack
observations are accepted for validation, learning, and prediction only when
every fact has `kind: "deterministic"` and names the exact configured extractor.
A record using a reserved non-deterministic kind may be read structurally, but
it is not an accepted learning observation today.

Evidence text is untrusted repository data. It must never be interpolated as
agent instructions or a shell command. Prefer references and hashes to source
excerpts because artifacts may be committed or shared.

### Closed-world negation

The built-in learner interprets a negated literal such as `not_mutates_state` as
absence of `mutates_state` in `facts`. This is a closed-world assumption.

Consequently, every extractor must distinguish:

1. verified absence of a fact;
2. unsupported or unparsed input; and
3. extractor failure.

Only the first may safely behave as logical negation. Invalid/incomplete
observations should be rejected or marked out of scope, not silently saved with
an empty fact set. Changing predicate semantics requires a pack version change
and a new experiment.

For `configured_paths@1`, absence is verified only after complete enumeration of
all normalized, visible, in-scope changed paths against the locked canonical
glob library. A comparison-budget failure, invalid path, mixed-scope unit, or
missing diff is extraction failure or ineligibility, never logical falsehood.

## Rule representation

Version 0.8.0 is propositionalized ILP over one change at a time. Each fact is a
Boolean unary predicate of the same observation variable `A`; there are no
multiple entity variables, relations between files/types/people, relational
joins, recursion, predicate invention, or arbitrary Prolog programs. A rule set
is a disjunction of Horn clauses in that bounded fragment. Each clause body is a
conjunction of literals:

```json
{
  "target": "needs_extra_validation",
  "clauses": [
    {
      "target": "needs_extra_validation",
      "body": [
        {"predicate": "touches_ci", "negated": false},
        {"predicate": "touches_test", "negated": true}
      ]
    }
  ]
}
```

Equivalent Prolog rendering:

```prolog
needs_extra_validation(A) :- touches_ci(A), not_touches_test(A).
```

A clause matches only when every literal matches. A rule set predicts positive
when any clause matches. An empty rule set always abstains/predicts false.

The schema disallows an empty clause, duplicate literals, or both positive and
negated forms of the same predicate in one body.

### Explicit manual-rule manifest

`ruleloom rules import MANIFEST.json` accepts an explicit schema-v1 object with
exactly these top-level fields:

| Field | Contract |
|---|---|
| `schema_version` | Integer `1`. |
| `policy_id` | Stable safe identifier for the human assertion. |
| `revision` | Integer greater than or equal to one. |
| `claim_kind` | Exactly `risk_trigger`; prescriptive actions and causal claims are outside this contract. |
| `summary` | Bounded, single-line human description. |
| `rules` | Non-empty bounded `RuleSet` whose target equals the experiment target and whose predicates belong to the frozen pack. |
| `sources` | Optional array of repository-relative source references with `path` and either both or neither of `start_line`/`end_line`. |

Source paths cannot escape the repository or point into `.git`, `.ruleloom`, or
generated RuleLoom agent paths. At declaration, RuleLoom records SHA-256 hashes
for each complete source document and selected excerpt, plus bounded size/line
metadata. It later reports `unchanged`, `changed`, or `unavailable`. It never
parses, executes, or treats the source prose as instructions. A referenced
`AGENTS.md` or `CLAUDE.md` span is provenance for a human translation, not
machine input to rule discovery.

The manifest is bound to the exact repository, config, evidence protocol, pack,
pack version, extractor, and configured-pack hash. Import evaluates its Horn
clauses over the current observations and creates a content-addressed
`engine=manual` candidate. That audit is always
`retrospective_post_hoc_exploratory` and `confirmatory=false`; unknown or
not-yet-available labels remain censored. Match coverage describes where a rule
would have fired. It does not establish relevance, correctness, or causality.

## Metrics

Every metric object stores raw counts and derived values:

```json
{
  "true_positive": 6,
  "false_positive": 2,
  "true_negative": 9,
  "false_negative": 3,
  "precision": 0.75,
  "recall": 0.6666666666666666,
  "f1": 0.7058823529411765,
  "accuracy": 0.75,
  "balanced_accuracy": 0.7424242424242424,
  "matthews_correlation": 0.4923659639173309,
  "prevalence": 0.45,
  "predicted_positive_rate": 0.4
}
```

`prevalence` is `(TP + FN) / total`; `predicted_positive_rate` is
`(TP + FP) / total`. Matthews correlation coefficient (MCC) uses all four cells and returns
zero when its denominator is zero. Consumers must not recompute a different
denominator by dropping abstentions or unfavorable cases. Unknown labels are
excluded by contract; their count is reported separately.

## Candidate artifact

Candidate files are immutable experiment records. Their structural contract is:

```text
Candidate {
  schema_version: 1
  id: string
  created_at: timezone-aware timestamp
  status: candidate | shadow | approved | rejected | deprecated
  engine: horn | popper | manual
  engine_version: string
  dataset_hash: string
  config_hash: string
  rules: RuleSet
  metrics: {train: Metrics, test: Metrics} | {historical: Metrics}
  baselines:
    learned: {never_alert, always_alert, train_majority, best_single_literal,
              size_only, logistic_regression_boolean_facts}
    manual: {never_alert, always_alert}
  stability: number in [0, 1]
  train_ids: string[]
  test_ids: string[]
  warnings: string[]
  metadata: object
  review: object
}
```

All baseline metrics use the same chronological test IDs as the learned rule.
`train_majority`, `best_single_literal`, and the logistic threshold are selected
using training data only; for a configured-path experiment, both train-selected
models include dynamic configured predicates. `size_only` is the fixed rule
`large_change OR multi_file_change`. The logistic model is deterministic,
dependency-free, class-balanced, L2-regularized, and persists its ordered
predicates, weights, intercept, fit parameters, and threshold in candidate
metadata. Current metadata also records readiness, the selected single literal,
rule cards with support/counterexamples, and an evaluation object whose method
is `temporal_holdout`, whose effective and optional configured test starts are
explicit, and whose `label_availability_enforced` flag is true.

A manual candidate instead stores its immutable declaration and full post-hoc
audit under metadata, uses `historical` metrics, and has no train/test split.
Those fields cannot satisfy learned retrospective gates. After human review it
may enter shadow with no retrospective positives, but approval still requires
the exact prior shadow manifest and all non-overridable prospective prediction,
maturity, class-balance, elapsed-time, aggregate, and per-clause gates. Source
drift or inability to reproduce the declaration blocks its first transition.

Candidate metadata binds the exact pack name/version, extractor,
`evidence_protocol_hash`, and, for a configurable pack, `pack_config_hash`.
That digest protects identity but does not explain a dynamic predicate by
itself; preserve the canonical config and its outcome-blind pre-registration
with every candidate/report intended for independent audit.

`promote` copies the immutable candidate into `shadow/` or `approved/` with its
review record: reviewer, review time, note, whether an override was used, and
all unmet gates. It does not rewrite the original candidate. A changed dataset,
configuration, engine, rule, or split creates a new candidate ID.

At the first reviewed transition, the current observation hash must still equal
`dataset_hash`, and relearning from the current evidence/configuration must
reproduce the candidate identity payload. Approval must find an exact active
shadow copy and attributable trusted predictions whose policy snapshot contains
that shadow manifest hash. Missing/inconsistent artifacts, temporal or
per-clause evidence, and the configured prospective shadow floors are blocking
gates; `--override` cannot bypass them. Overrideable retrospective failures are
stored with the review note.

`deprecate` writes an immutable, reviewed artifact under `deprecated/`; it does
not delete the original shadow/approved evidence. Loaders exclude a candidate ID
with a valid tombstone from active policies.

`dataset_hash` binds the candidate to canonical observations used for the run.
`config_hash` binds it to the full learning, evaluation, and promotion settings.
`train_ids` and `test_ids` expose the chronological partition for independent
audit.

The versioned review object is not local authority. Promotion/deprecation writes
a hash-bound, non-versioned transition attestation in Git-private metadata for
the current checkout/worktree. In a clone or another worktree, `ruleloom trust`
can create a new attestation only after explicit review of the exact
`shadow`/`approved`/`deprecated` artifact. Trust does not propagate through Git.
These checks protect the normal workflow from copied state and accidental
tampering; they do not constrain a malicious process running as the same user.

## Prediction record

Each non-empty line in `.ruleloom/predictions.jsonl` is an immutable prospective
decision. A representative record is:

```json
{
  "schema_version": 1,
  "id": "prediction.163d9e8a74bae4cc9806",
  "predicted_at": "2026-01-15T13:00:00Z",
  "observation": {
    "schema_version": 1,
    "id": "worktree.4e8c82b7d61e12345678",
    "observed_at": "2026-01-15T12:59:59Z",
    "protocol_hash": "be2523c451e7156855f365ecc6e0100a59f9f195abb3726a59fdf286a1e84845",
    "facts": ["large_change", "touches_test"],
    "labels": {"needs_extra_validation": "unknown"},
    "label_evidence": {},
    "fact_evidence": {
      "large_change": {
        "kind": "deterministic",
        "extractor": "ruleloom.generic_changes.git.v1",
        "evidence": ["churn:240>=200"]
      },
      "touches_test": {
        "kind": "deterministic",
        "extractor": "ruleloom.generic_changes.git.v1",
        "evidence": ["path:tests/settings_test.py"]
      }
    },
    "source": {
      "kind": "git_worktree",
      "repository": "repo.0123456789abcdef0123",
      "change_id": "pr-123",
      "base": "91ab20ef1234567890abcdef1234567890abcdef",
      "head": "WORKTREE",
      "pack": "generic_changes",
      "pack_version": 1,
      "extractor": "ruleloom.generic_changes.git.v1"
    },
    "metadata": {
      "snapshot_kind": "working_tree",
      "base_commit": "91ab20ef1234567890abcdef1234567890abcdef",
      "snapshot_fingerprint": "4e8c82b7d61e12345678",
      "topological_index": 419
    }
  },
  "target": "needs_extra_validation",
  "unit_id": "pr-123",
  "protocol_hash": "eb4153cda795991f57ec876bb3f537c2be843de02a314d9a864e713d128a506c",
  "protocol": {
    "experiment_id": "example-shadow-v2",
    "repository_id": "repo.0123456789abcdef0123",
    "observation_unit": "git_worktree",
    "outcome_definition": "Independent review outcome recorded after the prediction",
    "target": "needs_extra_validation",
    "pack": "generic_changes",
    "extractor": "ruleloom.generic_changes.git.v1",
    "config_hash": "a8e2bb94e07a6754bdabdcbf54b21fa1411d20953ecfd3fe5af551d1959a2fdf",
    "evidence_protocol_hash": "be2523c451e7156855f365ecc6e0100a59f9f195abb3726a59fdf286a1e84845"
  },
  "policy_set_hash": "0cc4420e9a1923457214b9ad4341dc627879b596b55f2cbdd1c6dd43a72a5a7b",
  "policies": [
    {
      "candidate_id": "cand-0123456789abcdef",
      "status": "shadow",
      "target": "needs_extra_validation",
      "manifest_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "rule_signatures": ["needs_extra_validation:-large_change"]
    }
  ],
  "matches": [
    {
      "candidate_id": "cand-0123456789abcdef",
      "status": "shadow",
      "rule": {
        "target": "needs_extra_validation",
        "body": [{"predicate": "large_change", "negated": false}]
      },
      "prolog": "needs_extra_validation(A) :- large_change(A)."
    }
  ],
  "abstained": false
}
```

The embedded observation is the decision-time snapshot; later labeling updates
the observation dataset, never this prediction. The declared `target` must be
present and `unknown` in the snapshot, with no target label evidence.
`unit_id` must equal `observation.source.change_id`; it is the stable key for
deduplication, outcome resolution, temporal joining, and elapsed-day counting.
The Prediction `protocol` object contains only `experiment_id`, `repository_id`,
`observation_unit`, `outcome_definition`, `target`, `pack`, `extractor`,
`config_hash`, and `evidence_protocol_hash`. The last value must equal the
embedded observation's `protocol_hash`, and top-level `protocol_hash` is the
canonical content hash of this protocol object. Pack version, `EvidenceConfig`,
and schema-v3 `pack_config` are transitively bound through the two configuration
hashes but are not embedded as a self-contained reconstruction. Preserve the
canonical `.ruleloom/config.json` and pre-registration alongside an exported
prediction; otherwise describe the record as hash-bound to an external protocol,
not as a complete standalone protocol snapshot.
Each `policies` item freezes the candidate ID, lifecycle status, target, hash of
the exact reviewed manifest, and its rule signatures. `policy_set_hash` is the
canonical hash of `{protocol_hash, target, policies}`; every match must name one
of those exact candidate/signature pairs, contain a clause that really matches
the snapshot, and reproduce its Prolog rendering. `abstained` must equal
`(matches.length == 0)`.

The prediction ID content-addresses the complete identity payload: timestamp,
observation snapshot, target, `unit_id`, protocol object/hash, policy
snapshot/hash, matches, and abstention.
Duplicate IDs are rejected, and existing JSONL records are never updated in
place. The illustrative record above is structurally complete and its protocol,
policy-set, and prediction hashes are internally consistent.

`assess` requires `--change-id` and appends both its observation and prediction
unless `--no-record` is used. Including shadow policies also requires `--blind`,
which requires recording and redacts stdout only; it does not hide shadow or
prediction files from the same user. Scientific blinding therefore requires an
isolated observer plus OS/CI access controls. Reassessment may create a later
snapshot and prediction for the same stable unit. `report` deliberately keeps
only the earliest prediction per `unit_id` and policy set, resolves one consistent
outcome across all observations with the same `source.change_id`, then includes
it only if its evidence became available strictly after that prediction. Policy
sets and protocols are never pooled. By default, its top-level output is:

```text
{
  target,
  readiness,
  policy_sets: {
    <policy_set_hash>: {
      readiness,
      predictions,
      unique_observations,
      duplicate_predictions,
      mature_after_prediction,
      still_unknown,
      excluded_preexisting_outcome,
      matched,
      abstained,
      coverage,
      evaluated_matched,
      evaluated_abstained,
      prospective_metrics: Metrics,
      interpretation
    }
  },
  note
}
```

The retained field name `unique_observations` counts unique stable units, not
snapshot IDs. `matched` and `abstained` cover every unique earliest unit
prediction in that policy set, so `coverage = matched / unique_observations`
remains meaningful before labels mature. `evaluated_matched` and
`evaluated_abstained` cover only outcomes
eligible for prospective confusion metrics. `excluded_preexisting_outcome`
prevents a retrospectively known answer from masquerading as a prediction. Use
`report --policy-set <hash>` for the unwrapped single-policy report. The report
describes aggregate prospective association, never a causal estimate; causal
impact needs a randomized or pre-specified staged advisory rollout. Version 0.8.0
does not emit confidence intervals or per-clause prospective tables in this
report. Promotion separately evaluates the registered Wilson lower-bound and
per-clause gates described above.

Appending a prediction also creates a separate local recording attestation that
binds the artifact to a wall-clock append no more than five minutes after
`predicted_at`. `report` and approval load only predictions accepted by that
local check. There is no supported command to re-attest an imported or cloned
prediction retroactively, so ordinary copies are rejected in a new worktree.
This detects copied or accidentally altered records; a same-user malicious
process can alter local files and attestations, so transport the derived report
and protocol record without claiming that copied JSON proves prospective timing.

## Project validation

`ruleloom validate` loads the configuration and observations, checks
cross-record temporal invariants, then validates every candidate manifest,
shadow/approved active policy, deprecation tombstone, local transition
attestation, and locally attested prediction record. It fails closed on unknown
fields, mismatched content identities/filenames, stale configuration or pack
provenance on active policies, malformed review evidence, missing local trust,
and prediction snapshot/hash inconsistencies. Historical candidate manifests
may legitimately retain an older configuration but cannot be promoted under the
current one. Validation is a structural and provenance check, not a
claim that labels are correct or rules are useful.

## Schema evolution

Readers reject unsupported `schema_version` values rather than guessing. A
future breaking change must:

1. increment the relevant schema version;
2. include an explicit, tested migration;
3. retain original hashes/artifacts or record their lineage;
4. state whether predicate or label semantics changed;
5. regenerate agent adapters only after approved artifacts are migrated and
   reviewed.

Adding an optional metadata key is not automatically a fact-semantic change.
Changing how a predicate is detected is semantic and must invalidate direct
comparisons unless the historical data is re-extracted consistently.

The supported configuration compatibility contract is:

| Configuration schema | Pack contract | Intended use |
|---|---|---|
| v1 | Frozen `flutter_testing@1`, no explicit evidence profile | Structural reading and historical hash reproduction only |
| v2 | One static pack/version plus complete `evidence` profile | Default new `generic_changes@1` and current `flutter_testing@2` experiments |
| v3 | v2 fields plus required canonical `pack_config` | Required for `configured_paths@1`; static packs accept only `{}` |

Artifact records remain schema v1; configuration schema and artifact schema are
independent version axes. Configuration schema v1 remains structurally readable
with its exact historical semantics and hashes, but is not endorsed for new
collection or policy decisions. There is no implicit upgrade that reinterprets
old observations. Moving to schema v3, a newer pack, a different dynamic
vocabulary, scope, threshold, target, or outcome contract creates a new
experiment and requires complete, consistent re-extraction. A holdout already
inspected while choosing a configured vocabulary cannot become the new
experiment's confirmatory test; use a later untouched window and retain every
attempt in the audit record.
