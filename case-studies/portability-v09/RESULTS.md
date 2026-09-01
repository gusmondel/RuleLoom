# RuleLoom v0.9 portability smoke test

## Outcome

The frozen schema-v4 cold-start pipeline executed unchanged on 500 recent
commits from three repositories with different primary implementation
languages. Exact materialization retained between `99.2%` and `100%` of the
selected units. The new historical predicates were populated without
language-specific parsing.

The signal probe was **inconclusive in every repository**, as it should be:
Git-only bootstrap supplied zero mature positive labels and zero mature
negative labels. No model, Horn candidate, or holdout metric was produced. This
run establishes implementation portability and safe abstention, not predictive
portability.

## Frozen setup

- RuleLoom `0.9.0`, configuration schema `4`, `generic_changes@2`;
- target: `post_merge_revert_or_hotfix`;
- 500 most recent first-parent units at each pinned revision;
- weak heuristics explicitly enabled only to test their path through the
  exploratory materializer;
- holdout frozen by `ruleloom init` before materialization;
- no provider PR, review, CI, incident, or maturity-window events imported;
- Apple Git `2.50.1` and Python `3.14.7` on macOS;
- run date: 1 September 2026.

The observer clones used depth 600 and were therefore marked shallow and
truncated. These are bounded smoke cohorts, not complete project histories.

## Revisions and results

| Repository | Pinned revision | Bootstrap | Materialization | Retained | Skipped | Mature + / - | Probe |
|---|---|---:|---:|---:|---:|---:|---|
| Flask | `d318b683471101618febed18996405ad26462110` | 0.64 s | 31.05 s | 496 / 500 | 4 empty/ineligible | 0 / 0 | inconclusive |
| ripgrep | `3fce3b5bb0236da2df6d99672afb8a719642eca7` | 0.62 s | 34.93 s | 498 / 500 | 2 empty/ineligible | 0 / 0 | inconclusive |
| Express | `023767fe9872e029271df1418f73401bff20ff40` | 0.62 s | 35.12 s | 500 / 500 | 0 | 0 / 0 | inconclusive |

Timings are environment-local observations, not benchmarks. Full-clone
materialization processed roughly 14–15 units per second in this bounded run.

## Vocabulary behavior

The outcome-blind predicate audit showed that the language-neutral history
features were neither identical nor universally absent:

| Predicate | Flask | ripgrep | Express |
|---|---:|---:|---:|
| `touches_recent_change_hotspot` | 69.6% | 64.5% | 65.2% |
| `missing_usual_cochange_partner` | 30.6% | 7.6% | 4.6% |
| `touches_dormant_area` | 4.2% | 8.4% | 8.4% |
| `crosses_codeowners_boundary` | 0.0% | 0.0% | 0.0% |

`crosses_codeowners_boundary` remained false because no usable prior
`CODEOWNERS` snapshot was present in these bounded cohorts. It was not inferred
from contributors, languages, or directory names. Flask also showed more than
0.20 early/late prevalence shift for file-count, multi-file, and missing-partner
facts; the audit flagged that drift rather than treating all 500 commits as
exchangeable.

## Partial-clone finding and safeguard

An initial `--filter=blob:none` observer run exposed a separate performance
hazard. Bootstrap still completed in about 0.6 seconds, but `git diff
--numstat` began demand-fetching historical blobs one at a time. The three
500-unit runs were interrupted after approximately 197.5 seconds with no
observations committed.

Git's own partial-clone documentation describes this one-object dynamic-fetch
pattern as potentially slow, and newer Git versions provide `git backfill` to
hydrate missing blobs in batches. RuleLoom now sets `GIT_NO_LAZY_FETCH=1` for
exact diff and content reads. Missing promisor blobs abort the whole
materialization transaction with an actionable error instead of causing hidden
network traffic or a path-dependent retained cohort. A fresh five-unit
partial-clone smoke test failed closed in 0.41 seconds.

Use a full trusted observer clone, or explicitly hydrate the selected history
before materialization. `git backfill` is experimental and is not available in
every Git distribution, so RuleLoom does not invoke it automatically.

## Interpretation

This run answers three narrow questions:

1. `generic_changes@2` executes across Python, Rust, and JavaScript repositories;
2. point-in-time hotspot, dormancy, and co-change features vary by repository;
3. the signal gate does not mistake abundant Git history for labeled evidence.

It does **not** answer whether any predicate predicts defects, reverts, review
rework, or agent benefit. That requires provider-backed outcomes, both mature
classes, train-only rolling-origin folds, and a later untouched interval in
each repository.
