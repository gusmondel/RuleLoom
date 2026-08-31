# Data schema

## Principles

RuleLoom schema version 1 is local, provider-neutral JSON/JSONL. Its persisted
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
  "schema_version": 1,
  "project": "example-project",
  "target": "needs_extra_validation",
  "pack": "flutter_testing",
  "dataset": ".ruleloom/observations.jsonl",
  "candidates_dir": ".ruleloom/candidates",
  "shadow_dir": ".ruleloom/shadow",
  "approved_dir": ".ruleloom/approved",
  "deprecated_dir": ".ruleloom/deprecated",
  "predictions": ".ruleloom/predictions.jsonl",
  "protocol": {
    "experiment_id": "example-shadow-v1",
    "repository_id": "repo.0123456789abcdef0123",
    "prediction_unit": "git_worktree",
    "outcome_definition": "Independent review outcome recorded after the prediction"
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
    "seed": 17
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

Version 0.1 supports only `flutter_testing`. All six configurable managed paths
must be repository-relative, remain below `.ruleloom/`, contain no `..` or
control characters, and be pairwise non-overlapping after portable Unicode/case
normalization. Managed symlink components are rejected at access time.
Predicate-like fields start with a lowercase letter and contain lowercase ASCII
letters, numbers, and underscores. `init` derives `repository_id` from
`remote.origin.url`, or from root commits when no origin exists; initialization
therefore requires an origin or at least one commit. The storage lock uses POSIX
`fcntl`: version 0.1 supports macOS and Linux, not Windows.

The built-in search also enforces finite operational bounds:
`max_body` 1–4, `max_rules` 1–10, `max_predicates` 1–32,
`bootstrap_runs` 0–100, and Popper timeout 1–3600 seconds, plus a combined
hypothesis/work budget. For `engine="popper"`, version 0.1 requires
`max_rules=1`, `bootstrap_runs=0`, and the Horn-specific support/precision/cost
settings at their defaults. Popper is an offline adapter to an explicitly
configured, already provisioned checkout; RuleLoom does not install it.

Changing configuration changes `config_hash`. A candidate must retain the hash
of the configuration used to generate it. Separately, `evidence_protocol_hash`
hashes exactly `schema_version`, `experiment_id`, `repository_id`,
`prediction_unit`, `outcome_definition`, `target`, and `pack`. Every observation
records that hash, preventing evidence from different experiments,
repositories, units, outcome definitions, targets, or packs from being pooled
accidentally. The positive-count gates are readiness heuristics, not a
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
  "protocol_hash": "e03d2ca587979900a7e950bdeffdb233345d59563acd3eaa2b56b60174772869",
  "facts": [
    "changes_dart",
    "mutates_state",
    "touches_widget"
  ],
  "labels": {
    "needs_extra_validation": "positive"
  },
  "label_evidence": {
    "needs_extra_validation": {
      "kind": "review",
      "available_at": "2026-08-21T11:10:00-04:00",
      "source": "review/123",
      "reason": "Independent review required an additional widget test",
      "confidence": 1.0
    }
  },
  "fact_evidence": {
    "changes_dart": {
      "kind": "deterministic",
      "extractor": "ruleloom.flutter_testing.git.v1",
      "evidence": ["path:lib/features/settings/view.dart"]
    },
    "mutates_state": {
      "kind": "deterministic",
      "extractor": "ruleloom.flutter_testing.git.v1",
      "evidence": ["diff-pattern:setState"]
    },
    "touches_widget": {
      "kind": "deterministic",
      "extractor": "ruleloom.flutter_testing.git.v1",
      "evidence": ["path:lib/features/settings/view.dart"]
    }
  },
  "source": {
    "kind": "git_commit",
    "repository": "repo.0123456789abcdef0123",
    "base": "91ab20ef1234567890abcdef1234567890abcdef",
    "head": "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
    "pack": "flutter_testing",
    "extractor": "ruleloom.flutter_testing.git.v1"
  },
  "metadata": {"topological_index": 418}
}
```

### Observation fields

| Field | Type | Contract |
|---|---|---|
| `schema_version` | integer | Must be `1` for this release. |
| `id` | string | Unique within the dataset; lowercase letters/numbers plus `.`, `_`, or `-`. |
| `observed_at` | string | Decision-time timestamp with timezone; used for chronological splitting. |
| `protocol_hash` | string | Lowercase SHA-256 of the configured evidence protocol; evidence with a different hash must not be pooled. |
| `facts` | array of strings | Unique unary Boolean predicates true for this observation. |
| `labels` | object | Target predicate to `positive`, `negative`, or `unknown`. |
| `label_evidence` | object | Outcome provenance keyed by target; required for every mature label. |
| `fact_evidence` | object | Optional provenance keyed only by predicates present in `facts`. |
| `source` | object | Provider-neutral source identity, references, and collection context. |
| `metadata` | object | JSON audit information not consumed as facts unless an extractor explicitly emits it. |

Every collected source records `kind`, the derived stable `repository`, `pack`,
and versioned `extractor`. Prospective sources additionally record
`change_id`, which must match the Prediction `unit_id`. Git sources also retain
the relevant `base` and `head`; these identify the snapshot, while `change_id`
identifies the independent real-world change across snapshots.

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
| `available_at` | string | Timezone-aware time at which the outcome first became knowable; it cannot precede `observed_at`. |
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

### Fact evidence

Each `fact_evidence` entry contains:

| Field | Type | Contract |
|---|---|---|
| `kind` | enum | `deterministic`, `agent`, `human`, or `imported`. |
| `extractor` | string | Non-empty extractor/model name and version. |
| `evidence` | array of strings | Minimal paths, line references, hashes, or source IDs supporting the fact. |
| `confidence` | number or absent | Optional value from 0 through 1, mainly for non-deterministic facts. |

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

## Rule representation

Version 0.1 is propositionalized ILP over one change at a time. Each fact is a
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
        {"predicate": "mutates_state", "negated": false},
        {"predicate": "adds_widget_test", "negated": true}
      ]
    }
  ]
}
```

Equivalent Prolog rendering:

```prolog
needs_extra_validation(A) :- mutates_state(A), not_adds_widget_test(A).
```

A clause matches only when every literal matches. A rule set predicts positive
when any clause matches. An empty rule set always abstains/predicts false.

The schema disallows an empty clause, duplicate literals, or both positive and
negated forms of the same predicate in one body.

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
  engine: string
  engine_version: string
  dataset_hash: string
  config_hash: string
  rules: RuleSet
  metrics: {train: Metrics, test: Metrics}
  baselines: {
    never_alert: Metrics,
    always_alert: Metrics,
    train_majority: Metrics,
    best_single_literal: Metrics
  }
  stability: number in [0, 1]
  train_ids: string[]
  test_ids: string[]
  warnings: string[]
  metadata: object
  review: object
}
```

All baseline metrics use the same chronological test IDs as the learned rule.
`train_majority` and `best_single_literal` are selected using training data
only. Current metadata records readiness, the selected single literal, rule
cards with support/counterexamples, and an evaluation object whose method is
`temporal_holdout`, whose `test_start` is explicit, and whose
`label_availability_enforced` flag is true.

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
  "id": "prediction.31cfb65aec2cf9db1026",
  "predicted_at": "2026-01-15T13:00:00Z",
  "observation": {
    "schema_version": 1,
    "id": "worktree.4e8c82b7d61e12345678",
    "observed_at": "2026-01-15T12:59:59Z",
    "protocol_hash": "e03d2ca587979900a7e950bdeffdb233345d59563acd3eaa2b56b60174772869",
    "facts": ["changes_dart", "mutates_state"],
    "labels": {"needs_extra_validation": "unknown"},
    "label_evidence": {},
    "fact_evidence": {
      "changes_dart": {
        "kind": "deterministic",
        "extractor": "ruleloom.flutter_testing.git.v1",
        "evidence": ["path:lib/features/settings/view.dart"]
      },
      "mutates_state": {
        "kind": "deterministic",
        "extractor": "ruleloom.flutter_testing.git.v1",
        "evidence": ["diff-pattern:setState"]
      }
    },
    "source": {
      "kind": "git_worktree",
      "repository": "repo.0123456789abcdef0123",
      "change_id": "pr-123",
      "base": "91ab20ef1234567890abcdef1234567890abcdef",
      "head": "WORKTREE",
      "pack": "flutter_testing",
      "extractor": "ruleloom.flutter_testing.git.v1"
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
  "protocol_hash": "8c874686b4050195791b8ab39734260ed78a5c0aebe86bfa2b2d896f5c12752f",
  "protocol": {
    "experiment_id": "example-shadow-v1",
    "repository_id": "repo.0123456789abcdef0123",
    "observation_unit": "git_worktree",
    "outcome_definition": "Independent review outcome recorded after the prediction",
    "target": "needs_extra_validation",
    "pack": "flutter_testing",
    "extractor": "ruleloom.flutter_testing.git.v1",
    "config_hash": "b15d11779fa0f25869d47b6c6b21525916816be792aee5d809c66c87b47392db",
    "evidence_protocol_hash": "e03d2ca587979900a7e950bdeffdb233345d59563acd3eaa2b56b60174772869"
  },
  "policy_set_hash": "8d1a4e91892bcefe373bb09c242f02ef6afcfec4079191672282d9a097bc0031",
  "policies": [
    {
      "candidate_id": "cand-0123456789abcdef",
      "status": "shadow",
      "target": "needs_extra_validation",
      "manifest_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "rule_signatures": ["needs_extra_validation:-mutates_state"]
    }
  ],
  "matches": [
    {
      "candidate_id": "cand-0123456789abcdef",
      "status": "shadow",
      "rule": {
        "target": "needs_extra_validation",
        "body": [{"predicate": "mutates_state", "negated": false}]
      },
      "prolog": "needs_extra_validation(A) :- mutates_state(A)."
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
The exact `protocol` snapshot contains only `experiment_id`, `repository_id`,
`observation_unit`, `outcome_definition`, `target`, `pack`, `extractor`,
`config_hash`, and `evidence_protocol_hash`. The last value must equal the
embedded observation's `protocol_hash`, and top-level `protocol_hash` is the
canonical content hash of the full snapshot.
Each `policies` item freezes the candidate ID, lifecycle status, target, hash of
the exact reviewed manifest, and its rule signatures. `policy_set_hash` is the
canonical hash of `{protocol_hash, target, policies}`; every match must name one
of those exact candidate/signature pairs, contain a clause that really matches
the snapshot, and reproduce its Prolog rendering. `abstained` must equal
`(matches.length == 0)`.

The prediction ID content-addresses the complete identity payload: timestamp,
observation snapshot, target, `unit_id`, protocol snapshot/hash, policy
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
impact needs a randomized or pre-specified staged advisory rollout. Version 0.1
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
