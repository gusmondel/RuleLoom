# Apache Airflow public case study

This directory contains the frozen protocol and compact results for RuleLoom's
first public retrospective evaluation. The result is intentionally published
even though the learned Horn model abstained and failed the preregistered
success criterion.

## Question

Can Boolean facts available when an Apache Airflow pull request opens predict
whether an independent reviewer later submits GitHub's structured
`changes_requested` decision?

This is narrower than predicting a defect, regression, or software quality. A
mature negative requires a complete event window, an independent approval, a
merge, and no independent request for changes.

## Frozen and amended protocol

- `preregistration.json` froze the repository, collection window, target,
  feature vocabulary, chronological split, learner, baselines, and success
  criterion. Its raw digest is in `preregistration.sha256`.
- `amendment-001.json` records the only semantic extraction amendment. After a
  zero-output performance failure, it replaced blob-level per-PR diffs with
  exact Git tree paths plus point-in-time aggregate opening statistics. It did
  not change the target, vocabulary, thresholds, split, or success criterion.
- `collection-manifest.json` binds the public query, event archive, time window,
  repository, preregistration, and exact expected/observed source hours.
- `correction-001.json` discloses a source-continuity defect found after the
  first metrics were inspected, invalidates that attempt, and specifies the
  conservative corrected rerun. Its raw digest is in `correction-001.sha256`.
- `results.json` is the compact machine-readable result. `RESULTS.md` explains
  the cohort flow, metrics, limitations, and interpretation.

Collection uses only structured GH Archive columns. Account names are hashed
in the remote query; titles, bodies, labels, paths, and review prose are neither
downloaded nor persisted. Repository code is never checked out or executed.

## Reproduce

Use a fresh directory and RuleLoom's source checkout. The source table is
public, but its availability and contents are outside RuleLoom's control.

```bash
git clone --filter=blob:none --no-checkout https://github.com/apache/airflow.git airflow-observer
cd airflow-observer
git update-ref refs/ruleloom/case-study-cutoff \
  93e6f0070aa4d295f348912c10037be63c419e0f

ruleloom init . --project bootstrap --pack generic_changes --agents none
cp /path/to/RuleLoom/case-studies/apache-airflow/config.json .ruleloom/config.json
ruleloom validate
```

The committed `config.json` is the exact executable evidence, configured-path,
learner, protocol, and evaluation profile derived from `preregistration.json`.
Its `config_hash` must be
`9bad563819f9928531b68db662a07c35d372941b541eb0b9a2597b71318f038d`.
Then export and import the frozen public event projection:

```bash
python /path/to/RuleLoom/scripts/export_gharchive_clickhouse.py apache/airflow \
  --provider-repository-id 33884891 \
  --since 2023-02-01T00:00:00Z \
  --until 2024-06-01T00:00:00Z \
  --preregistration-sha256 \
  8775e0f4006482af35cfaf7119409b4b42ccdc5c1df1d8a06407f768a870c349 \
  --events /absolute/path/events.jsonl \
  --manifest /absolute/path/collection-manifest.json

ruleloom history import-github-event-archive \
  --events /absolute/path/events.jsonl \
  --manifest /absolute/path/collection-manifest.json

python /path/to/RuleLoom/scripts/fetch_github_pull_refs.py apache/airflow \
  --root . --batch-size 64 --max-refs 7000

ruleloom history materialize \
  --outcome-target independent_review_changes_requested
ruleloom predicates audit > predicate-audit.json
ruleloom learn --engine horn --json > learn-summary.json
```

Compare every reported hash and count with `collection-manifest.json` and
`results.json`. The raw 11 MiB event archive, Git objects, and 51 MiB local
RuleLoom ledger are regenerable evidence and are not committed to this library.

## Result in one sentence

The exact preregistered Horn learner emitted no rule (holdout MCC `0.000`) while
a supplementary Boolean logistic baseline reached MCC `0.136`; therefore this
vocabulary does **not** support the target at the required precision and no
policy should be promoted.
