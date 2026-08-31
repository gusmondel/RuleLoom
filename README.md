# RuleLoom

**Inductive logic programming for evidence-backed coding-agent policies.**

RuleLoom is an experimental, provider-neutral CLI that learns small Horn-rule
policies from a repository's own history. Version 0.1 propositionalizes each
change as Boolean unary predicates over one stable observation unit; it is not a
full relational ILP system with entity variables, joins, recursion, or predicate
invention. It turns deterministic facts about code changes and delayed outcomes
into inspectable candidate rules, evaluates them on later changes, and only
exposes explicitly approved rules to coding agents such as Codex and Claude
Code.

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
rule causes better software. Version 0.1 is an instrumented hypothesis: a
repository may contain recurring combinations of Boolean change properties that
are useful enough to guide an agent, but only local prospective evidence can
establish that for a particular team.

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
[AutoSpec](https://arxiv.org/abs/2606.24245) preprint applied ILP-guided rule
evolution to LLM-agent safety traces. Neither tested RuleLoom's repository
outcome, Flutter pack, temporal protocol, or effect on Codex/Claude. Every
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

RuleLoom is alpha research software. The initial pack targets Flutter testing
decisions and the initial target is `needs_extra_validation`. The built-in Horn
learner is a deterministic, dependency-free baseline. A Popper adapter is
optional for MDL learning with noisy labels; Popper and its system dependencies
are not bundled. The supported Popper fragment is deliberately narrow:
non-recursive unary rules, `max_rules=1`, and `bootstrap_runs=0`.

Do not use RuleLoom as a merge gate, security control, or autonomous policy
publisher during the initial pilot.

## Install

RuleLoom requires Python 3.11 or newer on macOS or Linux. Version 0.1 relies on
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
ruleloom init . --project example-project
ruleloom doctor

ruleloom collect git --last 50
ruleloom validate
ruleloom readiness

ruleloom label <observation-id> positive \
  --kind review \
  --source "review/<stable-reference>" \
  --available-at "2026-01-15T15:00:00Z" \
  --reason "Independent review required an additional widget test"

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

### Collection modes

`collect git` supports three mutually exclusive modes:

```bash
# Last N first-parent commits from a ref (HEAD by default)
ruleloom collect git --last 50 --ref main

# One committed range
ruleloom collect git --base <base> --head <head>

# Staged, unstaged, and untracked changes against a ref
ruleloom collect git --working-tree --ref HEAD
```

Most commands discover the initialized root from the current directory. Use
`--root /path/to/project` when invoking them elsewhere.

## Lifecycle

1. `init [path] [--project NAME]` requires either `remote.origin.url` or at
   least one commit, then creates `.ruleloom/config.json` and empty
   observation/prediction logs. Agent skills are not installed by default.
   Every configurable evidence/artifact path must stay below `.ruleloom/`;
   managed paths may not overlap.
2. `collect git` extracts deterministic facts from changes and records their
   provenance.
3. `label ID VALUE` or `import-labels CSV` attaches an outcome and the time and
   source at which it became knowable. `readiness` reports evidence coverage and
   sample stage. `validate` checks observations and their temporal invariants,
   content-addressed candidate manifests, active shadow/approved artifacts,
   deprecation tombstones, locally trusted transitions, and locally attested
   prediction records.
4. `learn` performs a chronological split, removes training labels unavailable
   at the holdout boundary, learns on the eligible past, evaluates on the future,
   and records four baselines.
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
   policy experiment. Version 0.1 does not emit confidence intervals or
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
matching clauses. Their exact protocol snapshot binds experiment, repository,
observation unit, outcome definition, target, pack, extractor, configuration,
and `evidence_protocol_hash`; the prediction's `protocol_hash` binds that
snapshot, and `policy_set_hash` binds the protocol hash plus the ordered policy
set. The embedded observation's `protocol_hash` must equal the protocol's
`evidence_protocol_hash`. Both artifact types are content-addressed and
immutable. This makes a result reviewable without treating it as causal
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
separately in the pilot log. Version 0.1 does not expose general uncertainty
intervals or a per-rule prospective report.

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
commit metadata, evidence excerpts, labels, and reviewer notes. Treat it as
sensitive:

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
