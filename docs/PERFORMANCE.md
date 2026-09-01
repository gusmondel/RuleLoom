# Git history performance and storage

RuleLoom treats performance limits as part of the evidence contract. A faster
collector is not acceptable if it silently skips a commit, changes Git diff
semantics, or persists only half of an event/change-unit transaction.

This document separates the improvements shipped in v0.7 from the storage
migration that still requires its own compatibility and fault-injection work.

## What v0.7 changes

### Batch change analysis through native Git

`ruleloom audit` collects ordinary commit numstats with Git's
[`diff-tree --stdin`](https://git-scm.com/docs/git-diff-tree) protocol. With the
default batch size of 128, `N` ordinary commits require approximately
`ceil(N / 128)` diff processes instead of `N`. A root commit is the one
exception because its base is the empty tree rather than another commit. Both
paths use `--no-renames`: a rename is represented by its deleted and added
paths, avoiding dependence on Git's heuristic rename detection.

The optimization has an equivalence test against one diff process per commit,
including empty commits and UTF-8 paths containing tabs or newlines. It also has
a process-count regression test. Path evidence is accepted one batch at a time;
if accepting a batch would cross the configured total-path budget, the audit
fails without retaining that batch.

`--diff-batch-size` can reduce the aggregate output of a process when several
ordinary commits are large. It cannot make one megachange smaller: if the
numstat for a single commit exceeds RuleLoom's fixed 64 MiB subprocess-output
safety cap, the audit fails closed even at batch size 1. Non-UTF-8 paths are
also rejected rather than decoded lossily.

### Incremental topology ingestion

`ruleloom history bootstrap-git --after <commit>` collects only commits after a
previous boundary. For the persistent CLI workflow, that value must exactly
match a Git commit or merge `source_ref` already stored in the same repository's
canonical ledger. The boundary must be an ancestor of the selected ref.
Rewritten, divergent, empty-ledger, or foreign-ledger cursors fail closed instead
of being mixed into the existing experiment. Incremental collection is rejected
for shallow repositories even when the boundary is a valid ancestor in the local
graph, because that local graph cannot prove the interval is complete. Fetch the
complete history before advancing a cursor; a shallow bootstrap without
`--after` remains explicitly incomplete and exploratory.

The read-only `collect_git_history` Python API has no ledger path and therefore
can analyze a range after any Git ancestor in a complete repository. It still
rejects shallow repositories, divergence, `after`/`since` combinations, and
incremental truncation. Code that persists an API result must separately bind
the cursor to its ledger; the CLI performs that check before collection.

Incremental collection is all-or-nothing. Commit-limit or storage-limit
truncation raises before persistence, so the reported tip cannot skip
intermediate commits. `--after` and `--since` are mutually exclusive because
commit timestamps are not monotonic traversal boundaries. Advance a cursor only
after a successful complete interval; schedule collection frequently enough for
each interval to remain within the fixed caps.

Git graph selection during bootstrap is bounded to the new interval after the
initial bootstrap; bootstrap does not extract file diffs. Replaying the same
interval is idempotent. Repository identity and ancestry checks still run for
every invocation, and persistence is not append-only: the atomic JSONL upsert
validates and rewrites the complete retained ledger, as described below.

Historical materialization is a separate full-ledger pass: it still collects a
snapshot and file diff for every retained unit selected by the command, so an
incremental bootstrap does not make later materialization incremental. The
materializer no longer asks Git to count the full first-parent chain for every
unit and then discard that position. Standalone `collect_snapshot` retains its
indexed behavior. Other per-unit snapshot work, including repository validation
and diff extraction, remains linear in the number and size of materialized
units.

### Bounded work remains explicit

The Python API exposes caller-reducible `GitHistoryBudgets`. These values can
lower time, process-output, record, and batch-storage ceilings; they cannot
increase RuleLoom's hard safety limits. A budget object is a circuit breaker,
not a scalability mechanism.

The zero-configuration audit additionally enforces its total-path budget while
parsing each completed batch, before constructing a record beyond the remaining
budget. All parsed paths count, including RuleLoom-internal paths later excluded
from the report. After acceptance, the raw batch container, parser temporaries,
and excluded internal-path objects are released before the next process; visible
path objects remain in the report and continue consuming the cumulative global
budget. This bounds the retained visible evidence plus the current parser batch;
it does not raise or replace the fixed subprocess-output ceiling described
above.

### Reproduce the batching benchmark

From a source checkout, run the versioned
[benchmark script](../scripts/benchmark_first_hour.py) against a separate Git
checkout:

```console
uv run python scripts/benchmark_first_hour.py /path/to/repository \
  --max-commits 500 --batch-size 128 --repeats 3
```

The script alternates baseline and batched execution, reports the Python, Git,
RuleLoom, and report-engine versions, and fails unless the evidence hash, volume,
and normalized structural report are equivalent. Timings are environment-local;
they are not a portable performance claim.

## The remaining storage limit

The v0.7 canonical ledger is still two atomically paired, sorted JSONL files.
Each file is limited to 64 MiB and 250,000 records, and each record is limited
to 1 MiB. Every upsert validates and rewrites the complete pair. This design is
easy to inspect and recover, but its write cost is linear in retained history
even when Git collection used an incremental `--after` interval. It cannot
represent an arbitrarily large installation.

RuleLoom deliberately does not disguise that boundary by increasing a constant.
Repositories that reach it need a versioned storage migration, not an
unreviewed larger allocation.

## Open technology evaluation

| Option | Best use | Decision |
|---|---|---|
| Native Git plumbing | Exact repository traversal, object lookup, and diff semantics | **Default now.** Git is already required; batch protocols remove most process startup without adding a runtime or packaging boundary. |
| libgit2 / pygit2 | In-process graph and tree traversal | Benchmark as a future optional backend. It can reduce traversal overhead, but requires native binaries and a parity suite for rename, path, SHA-256, shallow-repository, and diff behavior. |
| Dulwich | Pure-Python Git object access | Keep as a portability candidate, not the default. It removes subprocess startup but adds a dependency and still needs the same semantic parity work. |
| SQLite with WAL | Indexed local event ledger, incremental queries, transactions | Leading candidate for a v2 ledger or index. Python ships SQLite, but WAL, migration, recovery, locking, and canonical export must be tested before it can replace evidence JSONL. |
| Arrow / Parquet | Compact columnar snapshots for analytical scans | Export format only. Efficient analysis does not by itself provide the immutable identity, atomic paired updates, or replay semantics of the canonical ledger. |
| DuckDB | Local SQL over Parquet or exported snapshots | Optional analysis layer, not evidence authority. Results must remain bound to an immutable export manifest. |

Relevant primary documentation:

- Git's long-running object protocol is documented under
  [`cat-file --batch`](https://git-scm.com/docs/git-cat-file).
- Git can accelerate graph walks with a
  [commit-graph](https://git-scm.com/docs/commit-graph.html); RuleLoom may consume
  one maintained by Git, but a read-only audit does not write repository
  maintenance data.
- [libgit2](https://libgit2.org/docs/reference/) and
  [pygit2](https://www.pygit2.org/diff.html) expose in-process graph and diff
  APIs.
- [Dulwich](https://dulwich.readthedocs.io/en/stable/tutorial/index.html) is a
  pure-Python Git implementation.
- SQLite documents the behavior and operational constraints of
  [write-ahead logging](https://www.sqlite.org/wal.html).
- Apache Arrow defines its
  [columnar memory format](https://arrow.apache.org/docs/format/Columnar.html),
  while DuckDB documents
  [Parquet scans](https://duckdb.org/docs/stable/data/parquet/overview.html).

## Storage v2 acceptance gates

A segmented JSONL or SQLite design is not ready merely because it benchmarks
faster. Before it becomes canonical it must demonstrate:

1. byte-for-byte stable canonical export and record identities;
2. migration and rollback from the v1 JSONL pair without outcome reinterpretation;
3. atomic event/change-unit insertion under power-loss fault injection;
4. idempotent replay, conflict rejection, and repository-boundary checks;
5. concurrent reader/writer behavior on every supported operating system;
6. bounded memory on a public million-record fixture;
7. query and materialization benchmarks against the current baselines.

Until those gates pass, large installations should run incremental collection
often enough that each cursor interval completes within the bounds. A truncated
initial bootstrap may retain its explicit truncation state; a truncated
incremental interval fails and must not be joined to the ledger or used to
advance the cursor.
