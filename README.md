# RuleLoom

**Inductive logic programming for evidence-backed coding-agent policies.**

RuleLoom is an experimental, provider- and language-neutral CLI core that learns
small Horn-rule policies from a repository's own history. A versioned evidence
pack projects each change into Boolean unary predicates over one stable
observation unit; the ILP, evaluation, promotion, reporting, and agent lifecycle
consume those persisted facts without knowing the implementation language.
Version 0.3.0 is not a full relational ILP system with entity variables, joins,
recursion, or predicate invention. It turns deterministic facts about code
changes and delayed outcomes into inspectable candidate rules, evaluates them on
later changes, and only exposes explicitly approved rules to coding agents such
as Codex and Claude Code.

```text
git history -> timestamped facts + delayed labels -> ILP -> candidate rules
                                                        |
                                      temporal evaluation + baselines
                                                        |
                                             human approval gate
                                                        |
                                  Codex / Claude skill + local assessment
```

RuleLoom is not an autonomous self-improvement system and does not claim that a
rule causes better software. Version 0.3.0 is an instrumented hypothesis: a
repository may contain recurring combinations of Boolean change properties that
predict later outcomes for a particular team. A blinded prospective shadow run
can establish only that association; whether visible guidance benefits an agent
requires a separate controlled rollout.

## The real problem

Coding agents repeatedly rediscover repository-specific practices. Teams often
respond by accumulating static prompt rules, which become stale and are rarely
linked to evidence. Recent research presents both sides of the opportunity:
carefully selected and abstracted repository experience can improve software
agents, while broad or noisy memory can make them worse and more expensive.

RuleLoom tests a narrower proposition: can ILP learn a few readable rules, such
as “changes with properties X and Y often needed extra validation,” from facts
available at decision time and outcomes observed later?

Two papers make this more than a decorative demo while preserving the evidence
boundary: [Rex](https://www.usenix.org/conference/nsdi20/presentation/mehta)
reported deployed repository-specific correlated-change rules, and the 2026
[AutoSpec](https://arxiv.org/abs/2606.24245v3) preprint applied ILP-guided rule
evolution to LLM-agent safety traces. Neither tested RuleLoom's repository
outcome, evidence packs, temporal protocol, or effect on Codex/Claude. Every
repository therefore remains a local, unvalidated test.

The design follows four constraints:

- **Evidence before policy.** Facts, labels, provenance, configuration, and
  evaluation IDs are persisted.
- **Time flows forward.** Older labeled observations train; newer labeled
  observations test. Random cross-validation is not the default.
- **Abstention is valid.** No matching approved rule means no recommendation.
- **Approval is explicit.** Learned candidates never become agent instructions
  automatically.

The research basis and its limits are documented in
[docs/RESEARCH.md](docs/RESEARCH.md); the falsifiable product thesis is in
[docs/THESIS.md](docs/THESIS.md).

## Status

RuleLoom is alpha research software. New `init` runs select
`generic_changes@1` and configuration schema v2 by default, and retain
`needs_extra_validation` as the default target. Selecting the configurable
`configured_paths@1` pack creates a schema-v3 experiment. The built-in evidence
packs are deliberately small and versioned:

- `generic_changes@1` extracts language-neutral change-shape, test, docs, CI,
  and dependency facts from Git paths and churn;
- `configured_paths@1` adds repository-configured `touches_*` path predicates
  to those generic facts without reading source contents;
- `flutter_testing@1` is frozen for schema-v1 compatibility and reproducibility;
- `flutter_testing@2` is the current Flutter/Dart pack and layers domain facts
  on the shared generic change contract, including Riverpod mutations written
  as either `.state =` or bare `state =` assignments.

These are only three pack families, not comprehensive language support. Path
matching is language-neutral as a mechanism, but a configured component
taxonomy is repository-specific and is not evidence of cross-repository
portability. External pack plugins are not supported yet, and an experiment
selects exactly one pack;
facts from different packs or versions are not pooled. The built-in Horn learner
is a deterministic, dependency-free baseline. A Popper adapter is optional for
MDL learning with noisy labels; Popper and its system dependencies are not
bundled. The supported Popper fragment is deliberately narrow: non-recursive
unary rules, `max_rules=1`, and `bootstrap_runs=0`.

For Python API compatibility with version 0.1, bare `collect_snapshot`,
`collect_worktree`, and `backfill_commits` calls retain the frozen
`flutter_testing@1` default. New integrations should use `default_config()` and
pass its pack, pack version, evidence profile, protocol hash, and repository ID
explicitly, as the CLI does. Do not infer the schema-v2/v3 CLI behavior from
those legacy bare-call defaults.

Do not use RuleLoom as a merge gate, security control, or autonomous policy
publisher during the initial pilot.

## Install

RuleLoom requires Python 3.11 or newer on macOS or Linux. Version 0.3.0 relies on
POSIX `fcntl` locking and does not support Windows. From a local checkout:

```bash
uv tool install .
```

Alternatives:

```bash
pipx install .
python -m pip install .
```

For development:

```bash
uv sync
uv run ruleloom --help
make check
```

The core has no runtime Python dependencies. The optional Popper engine requires
a compatible pinned checkout, SWI-Prolog, GNU `timeout`, and an already
provisioned Popper Python environment (the currently supported checkout expects
Python 3.14 or newer). RuleLoom does not clone Popper, install packages, or fetch
dependencies while learning. `doctor` performs static prerequisite checks by
default; `doctor --probe-popper-runtime` explicitly executes the configured
external Popper Python for a compatibility probe, and an actual Popper run
repeats that probe. The checkout must be an absolute path outside the target
repository. Follow the [Popper
project](https://github.com/logic-and-learning-lab/Popper), record the exact
checkout fingerprint, and do not assume that its moving default branch matches
a published artifact. The adapter has unit coverage but has not been exercised
end to end in this development checkout because SWI-Prolog is unavailable; use
the built-in Horn engine for an initial smoke test.

## Quick start

Run the following inside the repository that will supply the evidence. Before
`init`, configure `remote.origin.url` or create at least one commit so RuleLoom
can derive a stable repository identity:

```bash
ruleloom init . --project example-project --pack generic_changes
ruleloom doctor

ruleloom collect git --last 50
ruleloom validate
ruleloom readiness

ruleloom label <observation-id> positive \
  --kind review \
  --source "review/<stable-reference>" \
  --available-at "2026-01-15T15:00:00Z" \
  --reason "Independent review required additional validation"

# Repeat collection and independent labeling until `readiness` reports enough
# mature positive and negative outcomes for a chronological train/holdout split.
ruleloom readiness

ruleloom learn --engine horn
ruleloom candidate list
ruleloom candidate show <candidate-id>

ruleloom promote <candidate-id> --to shadow --reviewer <reviewer> \
  --note "Reviewed for prospective shadow evaluation"
# Run this as an isolated observer; reuse one stable ID for repeated snapshots
# of the same independently adjudicated change.
CHANGE_ID=pr-123
ruleloom assess --base origin/main --change-id "$CHANGE_ID" \
  --include-shadow --blind --json
ruleloom report
```

The single `label` command above only illustrates the provenance contract; it
is not enough evidence to learn or promote a rule. Pre-register the maturity
condition and minimum positive/negative sample, then repeat collection and
independent outcome labeling before `learn` or `promote`. Follow the complete
[shadow-pilot protocol](docs/PILOT-PROTOCOL.md), including its readiness and
promotion gates.

The JSON returned by a backfill separates `examined`, `eligible`, and `skipped`
changes, counts `mixed_scope` and `no_in_scope_files` skips, and includes a
bounded skip preview plus a manifest hash. Preserve that output with the pilot
record; `collected` alone is not the sampling denominator.

First-parent commit backfill is not automatically a valid retrospective sample
for a review-time target. A merge or squash commit may already contain the test
or validation added in response to review, so its final diff can leak the
outcome into path facts. Version 0.3.0 retrospective `learn` accepts only
`git_commit` observations, so a historical case is eligible only when the commit
itself is a genuine pre-event decision point. `git_range` and `git_worktree` are
prospective collection/prediction snapshots, not retrospective training input.
If only a final merge or reconstructed range is available, treat the historical
result as exploratory and collect the target prospectively. Never move
`observed_at` or `available_at` merely to make a post-review commit satisfy the
temporal contract. Group-aware training over PR/change IDs or ranges is future
work.

`generic_changes@1` is the default and does not inspect language syntax. For a
repository-specific, language-neutral path vocabulary, initialize a separate
schema-v3 experiment and repeat each option as needed:

```bash
ruleloom init . --project configured-example \
  --pack configured_paths --pack-version 1 \
  --path-predicate 'touches_client_ui=components/client_ui/**' \
  --path-exclude 'touches_client_ui=components/client_ui/generated/**' \
  --path-predicate 'touches_shared_contract=interfaces/contracts/**'
```

Design and freeze the complete predicate library before inspecting outcome
labels, learned rules, metrics, or holdout errors. A later change requires a new
experiment and an untouched future confirmation window; renaming the experiment
while reusing the inspected holdout does not make it a new test.

For a new Flutter experiment, select the current specialized pack explicitly
(omitting `--pack-version` also selects the latest registered version):

```bash
ruleloom init . --project flutter-example \
  --pack flutter_testing --pack-version 2
```

That is the end of the initial shadow-pilot runbook: never approve or synchronize
the pilot policy. The observer must run under a separate account, ACL boundary,
or isolated CI job and must not expose `.ruleloom/shadow/` or
`.ruleloom/predictions.jsonl` to the coding agent or outcome adjudicator.
`--blind` is required with `--include-shadow`, but it only redacts assessment
details from stdout; it still records local files and is not a secrecy boundary
against another process running as the same user.

For a different, separately approved deployment—not the initial shadow
experiment—an active policy that becomes stale or unsafe can be tombstoned and
its adapters regenerated:

```bash
ruleloom deprecate <candidate-id> --reviewer <reviewer> \
  --note "Predicate or outcome behavior drifted"
ruleloom sync-agents
```

Use `unknown`, not `negative`, while an outcome is still immature. Labels must
come from information observed after the change and must not be reconstructed
from the same fact used as a predictor. Every mature label requires evidence
with its kind, source, and availability time. For batch work, use
`ruleloom import-labels outcomes.csv`. See
[docs/DATA-SCHEMA.md](docs/DATA-SCHEMA.md) for the contract and CSV columns.

During an initial pilot, promote at most to `shadow` and let the isolated
observer run `assess --change-id ID --include-shadow --blind` out of band. Do
not run `promote --to approved` or `sync-agents`, and do not expose
recommendations or shadow/prediction files to the developer, adjudicator, or
coding agent. The runbook is in
[docs/PILOT-PROTOCOL.md](docs/PILOT-PROTOCOL.md).

### Evidence packs and experiment scope

Run `ruleloom packs list` to inspect the built-in name/version pairs. The
listing marks configurable packs and shows their static/shared predicates; the
resolved dynamic vocabulary for `configured_paths@1` comes from the project's
canonical `pack_config`. A pack is a
deterministic projection from normalized Git diff evidence to named Boolean
facts with provenance. Pack-specific syntax stays at that boundary; the learner
and policy lifecycle operate on the resulting fact vocabulary. Adding another
built-in language pack therefore does not require a language branch in the ILP
or promotion code, although the current CLI has no external pack-plugin loader.

Schema-v2 and schema-v3 configurations record one `pack` and `pack_version` per
experiment. Their pack-neutral `evidence` object controls repository-relative
include/exclude globs, the churn threshold for `large_change`, the file-count threshold for
`multi_file_change`, and the maximum number of file paths retained in metadata:

```json
{
  "schema_version": 2,
  "pack": "generic_changes",
  "pack_version": 1,
  "evidence": {
    "include_paths": ["services/api/**"],
    "exclude_paths": ["**/vendor/**"],
    "large_change_churn": 400,
    "multi_file_count": 5,
    "metadata_file_limit": 256
  }
}
```

Schema v3 adds canonical pack-specific configuration. It is required for
`configured_paths@1`; static packs use an explicit empty `pack_config` when
represented in schema v3:

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
    "exclude_paths": ["**/generated/**"],
    "large_change_churn": 400,
    "multi_file_count": 5,
    "metadata_file_limit": 256
  }
}
```

These two kinds of path configuration have different meanings:

- `evidence.include_paths` and `evidence.exclude_paths` decide which whole
  changes are eligible for the experiment and outcome;
- each `pack_config.path_predicates` entry creates one feature over the already
  scoped files and never widens experiment eligibility.

A configured predicate is true when at least one visible, in-scope changed path
matches one of its `include_paths` and none of that predicate's
`exclude_paths`. Predicates may overlap. Names must be unique `touches_*`
predicates and cannot collide with shared facts or the target. Globs are
root-anchored repository paths using literals, `*`, `?`, and a complete `**`
segment; they are not Git pathspecs or `.gitignore` rules. The pack accepts at
most 32 predicates, 32 include and 32 exclude globs per predicate, 256 total
globs, 256 characters per glob, 5,000,000 path/glob comparisons, and
200,000,000 estimated matcher work units per extraction. Evidence paths are
limited to 4,096 characters and 256 components. Invalid, duplicate, absolute,
parent-traversing, or ambiguous patterns and non-UTF-8 Git paths fail closed.

Set these fields before collecting the experiment. Pack name/version, canonical
pack configuration, scopes, thresholds, and metadata limit are part of
`evidence_protocol_hash`, so changing them creates an incompatible evidence
protocol rather than silently
reinterpreting old observations. Large diffs keep aggregate counts and a full
change-manifest hash while truncating sampled path and per-file-churn metadata to
configured and byte bounds.

Scope is also an eligibility boundary for the outcome, not just a fact filter.
Schema-v2/v3 collection rejects a change that mixes files inside and outside the
include scope, and history backfill skips both mixed and wholly out-of-scope
commits. If one outcome legitimately covers multiple components, include all of
them in the pre-registered scope; otherwise use a component-specific change and
outcome unit. Explicit excludes inside that include scope may still remove
generated or vendored files. Git paths must be UTF-8; collection fails rather
than decode an identity with replacement characters.

### Collection modes

`collect git` supports three mutually exclusive modes:

```bash
# Retrospective training input: first-parent git_commit observations
ruleloom collect git --last 50 --ref main

# Prospective/instrumentation snapshot; not accepted by retrospective learn
ruleloom collect git --base <base> --head <head>

# Prospective worktree snapshot; not accepted by retrospective learn
ruleloom collect git --working-tree --ref HEAD
```

Most commands discover the initialized root from the current directory. Use
`--root /path/to/project` when invoking them elsewhere.

## Lifecycle

1. `init [path] [--project NAME] [--pack NAME] [--pack-version N]` requires
   either `remote.origin.url` or at least one commit, then creates a schema-v2
   `.ruleloom/config.json` by default or schema v3 for `configured_paths`, plus
   empty observation/prediction logs. The configurable pack requires at least
   one `--path-predicate PREDICATE=GLOB` and accepts matching
   `--path-exclude PREDICATE=GLOB` options. The default remains
   `generic_changes@1`; an omitted version selects the latest registered version of the chosen pack. Agent
   skills are not installed by default. Every
   configurable evidence/artifact path must stay below `.ruleloom/`; managed
   paths may not overlap.
2. `collect git` applies the experiment's single evidence pack, its canonical
   pack configuration when present, and the pack-neutral scope/threshold
   configuration, then records deterministic facts and their provenance.
3. `label ID VALUE` or `import-labels CSV` attaches an outcome and the time and
   source at which it became knowable. `readiness` reports evidence coverage and
   sample stage. `validate` checks observations and their temporal invariants,
   content-addressed candidate manifests, active shadow/approved artifacts,
   deprecation tombstones, locally trusted transitions, and locally attested
   prediction records.
4. `learn` accepts retrospective `git_commit` observations only, performs a
   chronological split, removes training labels unavailable at the holdout
   boundary, learns on the eligible past, evaluates on the future, and records
   four baselines. It does not group commits by PR/change ID in version 0.3.0.
5. `promote ID --to shadow|approved` records a reviewed lifecycle transition.
   Before the first transition, RuleLoom relearns from the exact current dataset
   and configuration and requires the candidate payload to reproduce. Approval
   additionally requires an exact prior shadow artifact and attributable
   prospective shadow evidence. Some retrospective thresholds may be overridden
   with a recorded note; integrity, temporal/per-clause evidence, and prospective
   shadow gates cannot be overridden.
6. `assess --base B --change-id ID [--head H]` evaluates approved rules against
   a committed range or, by default, the working tree. The stable change ID is
   the prospective unit key and must be reused for snapshots of the same change,
   never for independent changes. `--include-shadow` adds reviewed shadow
   policies and requires `--blind`; blind mode requires recording and redacts
   stdout only. Scientific shadow isolation still requires a separate observer
   and file/CI access controls.
7. `report` groups predictions by immutable policy-set hash, joins the earliest
   prediction for each stable `unit_id` only to outcomes for that unit that
   became available later, and emits aggregate prospective association metrics
   without pooling policy sets. `report --policy-set HASH` selects one exact
   policy experiment. Version 0.3.0 does not emit confidence intervals or
   per-clause prospective tables in this report; promotion separately uses
   Wilson 95% lower confidence bounds for precision and recall gates.
8. `sync-agents` renders only approved rules into portable agent skills.
   `deprecate` removes an active policy through an immutable reviewed tombstone;
   `candidate list|show` inspects immutable manifests; `doctor` checks required
   Git/Python and optional Popper prerequisites.

Candidate artifacts include data/configuration hashes, the learner and version,
train/test observation IDs, metrics, four baselines, bootstrap stability,
warnings, rule cards, and readable Prolog forms. Prediction records embed the
decision-time observation, stable `unit_id`, target, exact policy snapshots
(candidate ID, status, target, reviewed-manifest hash, and rule signatures), and
matching clauses. Their protocol identity binds experiment, repository,
observation unit, outcome definition, target, pack name/version, extractor,
canonical schema-v3 `pack_config`, and the exact `EvidenceConfig` scopes and
thresholds in `evidence_protocol_hash`;
the prediction's `protocol_hash` binds that protocol object, and `policy_set_hash`
binds the protocol hash plus the ordered policy set. The embedded observation's
`protocol_hash` must equal the protocol's `evidence_protocol_hash`. Both
artifact types are content-addressed and immutable. The Prediction protocol
object stores hashes rather than an independently reconstructible copy of every
configuration field, so preserve the canonical config and pre-registration with
the artifact. This makes a result reviewable without treating it as causal
evidence.

### Local trust boundary

Reviewed JSON files are portable evidence, but their `shadow`, `approved`, or
`deprecated` field is not authority by itself. A transition created locally is
bound to a non-versioned attestation in Git-private metadata for the current
checkout/worktree. This catches copied state and accidental or unsophisticated
tampering; it is not protection from a malicious process with the same OS-user
access, which can read or alter RuleLoom files and Git-private metadata. After
inspecting a reviewed artifact copied by Git, establish trust explicitly in
that worktree:

```bash
ruleloom trust <candidate-id> --status shadow --reviewer <reviewer> \
  --note "Reviewed this exact manifest in this worktree"
```

Use `approved` or `deprecated` for `--status` when applicable. Trust is local and
does not propagate to another clone or worktree. Predictions have a stricter
application rule: `assess` creates their local recording attestations when it
appends them near their declared prediction time, and the supported CLI has no
retroactive attestation command. This rejects an ordinary copied log in a fresh
worktree, but does not prove timing against a malicious same-user process.

With the default configuration, retrospective readiness expects 20 mature
positive outcomes before shadow and 50 before approval. The prospective
approval gate additionally requires 30 attributable predictions on distinct
stable units, 30 later-matured outcomes, at least 10 positive and 10 negative
outcomes, at least 10 matches for every clause, and at least seven elapsed days
between the first and last attributable unit prediction. Aggregate precision
must have a Wilson 95% lower bound of at least 0.70, aggregate recall a Wilson
95% lower bound of at least 0.50, and point-estimate MCC at least 0.10. Every
clause must also meet its prospective match count and Wilson precision floor,
and must have matched the temporal holdout at the configured precision. These
integrity and prospective/per-clause gates are non-overridable. The numbers are
configurable operating thresholds, not universal sample-size or significance
claims.

## Agent integration

RuleLoom's canonical policy is provider-neutral. `sync-agents` materializes an
adapter for each selected agent while preserving the approval boundary:

- Codex: `.agents/skills/ruleloom/SKILL.md`
- Claude Code: `.claude/skills/ruleloom/SKILL.md`

These locations follow the current [Codex skill
documentation](https://learn.chatgpt.com/docs/build-skills) and [Claude Code
skill documentation](https://code.claude.com/docs/en/skills). Generated skills
should be reviewed like source code. A rule's text and evidence are untrusted
repository data, not privileged instructions.

## What to measure

At minimum, compare RuleLoom on the same temporal holdout with `never_alert`,
`always_alert`, the training-majority predictor, and the best single literal
selected on training data. Candidate evaluation and the aggregate prospective
report record confusion counts, precision, recall, F1, accuracy, balanced
accuracy, Matthews correlation coefficient (MCC), class prevalence, and
predicted-positive rate. The prospective report also records
coverage/abstention and maturity counts for each exact policy set. Promotion
uses Wilson 95% lower bounds for precision and recall and the MCC point estimate;
those gates are distinct from the report schema. Track false-positive burden,
rule stability and complexity, extraction coverage, latency, tokens, and cost
separately in the pilot log. Version 0.3.0 does not expose general uncertainty
intervals or a per-rule prospective report.

For `configured_paths@1`, also report each configured predicate's prevalence in
train, test, and shadow; zero/always-true predicates; overlap between predicates;
and drift after path moves. `best_single_literal` must include the configured
facts so ILP receives no credit for merely rediscovering one component flag.

Day-one data establishes instrumentation, not causality. Product impact would
need a separately pre-registered future controlled experiment; it is not part of
an initial shadow run. Never report benchmark results from the cited papers as
an expected effect size for a target repository.

On day one, save the outputs of:

```bash
ruleloom doctor
ruleloom readiness
ruleloom report
```

Before any assessment, an empty `report` contains top-level `readiness` and an
empty `policy_sets` object; afterward each policy hash has its own counts and
metrics. A policy set with zero outcomes matured after the earliest prediction
for each stable unit is also a correct initial result, not evidence of no
effect. Its `interpretation` field explicitly describes prospective association
rather than bugs prevented.

## Data and privacy

RuleLoom operates on local repository evidence by default. All configurable
managed data paths remain under `.ruleloom`, which may contain filenames,
commit metadata, configured predicate names and globs, component taxonomies,
evidence excerpts, labels, and reviewer notes. Paths alone can disclose product,
team, customer, or service structure. Treat this material as sensitive:

- do not collect secrets or full source when a path, line range, or content hash
  is sufficient;
- decide explicitly which artifacts belong in version control;
- in a scientific shadow run, keep shadow and prediction files outside any
  checkout or account accessible to the coding agent or outcome adjudicator;
- review generated skills before committing them;
- do not upload pilot data without repository-owner authorization.

## Documentation

- [Product thesis and falsification criteria](docs/THESIS.md)
- [Research evidence matrix](docs/RESEARCH.md)
- [Repository pilot protocol](docs/PILOT-PROTOCOL.md)
- [Versioned data schema](docs/DATA-SCHEMA.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
