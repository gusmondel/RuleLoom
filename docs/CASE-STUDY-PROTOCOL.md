# Public case-study protocol

This is the reusable protocol used for RuleLoom's first public evaluation. It
prevents choosing a famous repository first and inventing a convenient outcome
after seeing the results. Apache Airflow was selected through the frozen rubric;
the preregistration and failed success criterion are published under
[`case-studies/apache-airflow`](../case-studies/apache-airflow/README.md).

That retrospective result makes no product-effect claim.

## Phase 1: outcome-blind feasibility

Evaluate at least three public candidates using only repository structure and
provider-data availability. Do not inspect RuleLoom target labels or candidate
performance during selection.

Record for each candidate:

- license and permission to process the relevant public metadata;
- reachable Git depth and number of logical changes;
- availability of exact PR prediction snapshots rather than final-only diffs;
- coverage and retention of review/check events;
- explicit links among reverts, incidents, defects, and originating changes;
- expected maturation window and whether a negative can actually be observed;
- rate of force-pushed, missing, truncated, or duplicate changes;
- time, API, storage, and manual-adjudication budget;
- whether one person or team dominates the history enough to confound results.

Choose the repository using a frozen scoring rubric. Preserve the rejected
candidates and scores.

## Phase 2: preregistration

Before opening outcomes, freeze:

| Field | Required decision |
|---|---|
| Repository and revision | Exact public identity, provider numeric identity, refs, and collection cutoff |
| Atomic target | One operational outcome; no composite “quality” label |
| Unit | One independently grouped real-world change |
| Prediction point | Exact base and head snapshot available before the outcome-generating event |
| Positive | Independent observable event and attribution rule |
| Negative | Complete maturity window and all gates that must remain observable |
| Unknown | Every incomplete, ambiguous, conflicting, or unattributable case |
| Vocabulary | Pack, version, configured predicates, scopes, thresholds, and outcome-blind audit |
| Attempts | Every vocabulary and cohort revision, including abandoned ones |
| Temporal split | Frozen train/holdout boundary plus an untouched future confirmation window if design changes follow |
| Primary metric | MCC by default; precision may be the operational priority when interruption cost dominates |
| Baselines | Never alert, always alert, train-majority, size-only, best train-selected literal, and one simple statistical model |
| Success criterion | Improvement over the strongest frozen baseline plus an operationally acceptable alert rate |

Publish the preregistration hash and collection command before calculating target
associations.

## Phase 3: collection and adjudication

- Capture or import point-in-time provider records; a current archive view is
  exploratory when it cannot reconstruct original state.
- Never execute repository or PR code while collecting metadata.
- Keep provider prose out of deterministic features unless a separate reviewed
  study explicitly preregisters a text model.
- Resolve duplicates and cross-split change groups before learning.
- Stratify a manual audit across positives, negatives, unknowns, weak signals,
  large changes, and time periods.
- Preserve exclusions with machine-readable reason codes.

## Phase 4: analysis

Report:

- cohort flow from discovered changes to eligible mature cases;
- both class counts, prevalence, unknown rate, and evidence grades;
- precision, recall, F1, balanced accuracy, MCC, alert rate, and coverage;
- confusion counts and uncertainty intervals;
- metrics for every preregistered baseline on the identical holdout;
- rule support, counterexamples, abstentions, and stability;
- sensitivity to weak evidence only as a labeled secondary analysis;
- runtime, storage, API requests, and manual review cost.

Do not tune on the holdout and then report it as untouched. If the first result
changes the vocabulary or cohort, start a new experiment and reserve a future
confirmation window.

## Phase 5: interpretation

A retrospective win supports only this claim:

> Under the frozen protocol, the rule anticipated the defined outcome in later
> historical changes better than the frozen comparators.

It does not show that displaying the rule prevents regressions. That requires a
prospective shadow confirmation followed by a randomized or preregistered staged
advisory rollout. A null or negative result must remain publishable.

The rationale for temporal ordering, conservative CI attribution, noisy defect
links, and correlated-change feasibility is maintained in
[RESEARCH.md](RESEARCH.md).
