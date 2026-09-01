# Product thesis

## Decision first

RuleLoom addresses a real and costly problem—coding agents do not reliably
retain repository-specific experience, and teams miss repository-specific
correlated changes—but its exact proposed solution remains an unvalidated
product hypothesis.

That distinction is the project decision: the problem is supported by deployed
correlated-change systems and mixed positive/negative repository-memory
evidence; the claim that bounded ILP plus versioned evidence packs and thin
Codex/Claude skills solves it is not. RuleLoom is therefore worth building as a
falsifiable instrument, but not yet worth trusting as an enforcement mechanism.

The evidence is strong enough to justify a disciplined experiment, not strong
enough to justify automatic deployment. Several benchmark studies report that
selected, repository-aware experience can improve coding-agent performance;
other evaluated systems report that noisy or overly broad memory can reduce
performance and increase token usage. Rex reports real deployment of learned
correlated-change suggestions, but its 4,926 operational true positives were
defined by engineers adding the suggested related change, not by an independent
measure of defects prevented. The 2026 AutoSpec preprint is direct evidence
that ILP-guided evolution of
interpretable LLM-agent safety rules can work in two evaluated domains. Neither
establishes that ILP-generated repository-quality policies improve real
repository delivery outcomes or Codex/Claude behavior.

## Precise thesis

> Given timestamped, provenance-bearing facts about repository changes and
> delayed positive/negative outcomes, bounded propositionalized ILP can induce a
> small set of stable, human-readable rules whose prospective use improves a
> coding agent's repository-specific decisions enough to outweigh false
> positives, maintenance cost, latency, and context overhead.

Each part is testable:

- **Timestamped:** every feature must have been available when the prediction
  would have been made.
- **Provenance-bearing:** a reviewer can trace each fact and label to its source.
- **Delayed outcomes:** uncertain recent changes remain `unknown`.
- **Bounded:** the hypothesis space and rule complexity are explicit.
- **Stable:** resampling the evidence should not produce unrelated rules.
- **Readable:** a maintainer can understand and challenge each clause.
- **Prospective:** later changes, not training examples, decide usefulness.
- **Net useful:** quality gains must exceed interruptions and operational cost.

## Why this might work

Software repositories offer structured, repeated decisions. Some signals are
language-neutral—change size, file distribution, tests, documentation, CI, and
dependency manifests—while specialized packs can add facts about syntax and
framework behavior such as state mutation, asynchronous code, or navigation.
RuleLoom 0.4.0 encodes each change as Boolean unary predicates and represents
conjunctions of those properties as rules rather than opaque scores. It does not
learn relations among multiple entities.

The narrow RuleLoom loop is:

```text
change-time evidence
       |
       v
 deterministic predicates ------> delayed, independent outcome
       |                                      |
       +--------------- ILP ------------------+
                           |
                    candidate clauses
                           |
              forward holdout + baselines
                           |
                 human shadow review
                           |
          prospective isolated shadow prediction
                           |
                outcome-time evaluation
                           |
             separate future experiment
```

This aligns with recent coding-agent research in three ways:

1. repository history can contain reusable signal;
2. abstraction and retrieval granularity matter more than indiscriminate memory;
3. noise and distractors can erase or reverse gains.

RuleLoom's additional bet is that logical rule induction is a useful abstraction
mechanism for repository-change outcomes and, after prospective validation, for
coding guidance. AutoSpec makes that link less speculative for agent safety,
but it remains unvalidated for this product target.

Cold start does not mean waiting for a year of future labels. Version 0.4.0 can
ingest the complete reachable Git graph immediately and combine it with
authorized, normalized forge/review/CI/revert/incident events already retained
by the repository workflow. That accelerates instrumentation, not certainty:
Git-only and final-state cases are exploratory; only a point-in-time change with
strictly later, independent strong evidence can support confirmation. No fixed
calendar window is assumed—readiness, class balance, chronology, baselines, and
drift decide whether the existing evidence is usable.

The implementation keeps the language boundary narrow. A selected, versioned
evidence pack deterministically maps normalized Git evidence to facts and
provenance; the ILP engine, chronological evaluation, lifecycle gates,
reporting, and agent adapters consume the persisted facts without pack-specific
branches. Each experiment uses one pack, not a union of vocabularies. Schema-v2
configuration binds the pack name/version and pack-neutral `EvidenceConfig`
scopes and thresholds into `evidence_protocol_hash`, preventing observations
with different extraction semantics from being silently pooled. Schema v3 adds
canonical `pack_config` to that identity for configurable packs. Path matching
can be language-neutral while the configured component taxonomy remains
repository-specific background knowledge.

## Why ILP rather than a larger prompt

A hand-written prompt can encode known practices, but it cannot establish which
ones recur in observed outcomes. A vector memory can retrieve similar episodes,
but similarity does not explain which conditions jointly matter. A black-box
classifier can predict risk, but its behavior is harder to review and convert
into a precise agent instruction.

This bounded ILP fragment is attractive when:

- examples are limited but have meaningful Boolean properties and conjunctions;
- predicates have domain meaning;
- compact conjunctions are more useful than calibrated risk scores;
- a human approval boundary is required;
- counterexamples should directly challenge a rule.

ILP is a poor fit when facts are unreliable, the target cannot be labeled,
relationships are mostly continuous, behavior drifts faster than labels mature,
or the task requires high-dimensional semantic understanding not captured by the
predicate vocabulary.

## Falsifiable hypotheses

### H1 — predictive value

On a chronological holdout, the learned rule set improves the pre-registered
primary metric over `never_alert`, `always_alert`, `train_majority`, and the
best single literal selected only on training data. Precision, recall, F1,
accuracy, balanced accuracy, Matthews correlation coefficient (MCC),
prevalence, predicted-positive rate, and confusion counts must all be reported
so class imbalance cannot hide failure. The default approval gate uses strict
MCC improvement over the best baseline; this is a product gate, not proof of
statistical significance.

For a configured-path experiment, the registered single-literal baseline must
include every dynamic predicate; otherwise a component flag could be credited
as value from ILP complexity.

H1 fails when RuleLoom does not beat the registered baselines, uncertainty is
too wide for a decision, or performance collapses on later periods.

### H2 — selective operational value

In prospective shadow mode, matching rules identify changes that later satisfy
the target with acceptable false-positive rate and useful lead time. Abstention
is expected and measured. The decision-time observation and exact reviewed
policy set are captured before the outcome becomes knowable. Its protocol binds
experiment, repository, unit, outcome definition, target, pack name/version,
extractor, canonical pack configuration when present, and `EvidenceConfig`
scopes and thresholds so incompatible evidence cannot be pooled. Historical H1
training prefers one rich, point-in-time `ChangeUnit` per logical change; a raw
commit cohort is supported separately but remains manually audited. Prospective
H2 assessment may use a range/worktree snapshot. Neither may use a final diff
containing validation added because of review.

H2 fails when rules fire too often, rarely fire, become stale, generate mostly
false positives, or require information that was not available at assessment
time. An outcome known at or before prediction is excluded rather than counted
as prospective evidence.

### H3 — agent value

After shadow validation, a controlled rollout of approved guidance reduces a
pre-registered undesirable outcome or improves a delivery outcome without
material increases in rework, latency, tokens, or reviewer burden.

H3 cannot be tested on installation day. It requires a comparison group or a
credible staged design and sufficient mature outcomes. Before that phase, all
quality differences are observational.

### H4 — portability

The canonical approved rule set can be rendered for Codex and Claude without
changing rule semantics, while adapter-specific text remains thin and auditable.

H4 fails if provider-specific prompts become the real policy, or if the same
rule systematically produces incompatible behavior across agents.

### H5 — language-boundary portability

The same ILP and lifecycle can operate on facts emitted by the generic pack, a
repository-configured path pack, or a specialized language pack without
changing learning, evaluation, promotion, reporting, or agent integration. This
is an architectural claim, not a claim that a configured taxonomy, learned
rule, or predictive performance transfers between languages, repositories,
pack families, pack configurations, or pack versions.

H5 fails if adding a built-in language pack requires language-specific behavior
inside the core lifecycle, or if pack identity and extraction settings are not
sufficiently isolated to prevent incompatible evidence from being pooled. The
current implementation exercises only three built-in pack families and does not
yet support third-party pack plugins, so broader extensibility remains unproven.

## Safety invariants

- Collection does not alter application code.
- The default evidence path is deterministic and the selected pack must attach
  provenance to every emitted fact. Although the artifact schema reserves
  `agent`, `human`, and `imported` provenance kinds, version 0.4.0 accepts only
  exact deterministic built-in-pack provenance for validation, learning, and
  prediction; non-deterministic facts are future work.
- Each experiment selects exactly one pack name/version. Pack identity,
  extractor identity, include/exclude scopes, change-shape thresholds, and
  metadata limit are bound into `evidence_protocol_hash`; schema v3 additionally
  binds canonical `pack_config`.
- `evidence` path globs define experiment/outcome eligibility; configured
  predicate globs create features only over already in-scope files and never
  widen that cohort.
- A configured vocabulary is frozen outcome-blind with its design revision,
  roles, rationale, attempt log, and hash. Any outcome-informed revision needs a
  new experiment and untouched future confirmation window.
- Large changes retain exact aggregate counts and a full change-manifest hash;
  sampled path and per-file metadata remain bounded and disclose truncation.
- Unknown is distinct from negative.
- Every mature label has evidence and an explicit `available_at`; a nominal
  training label unavailable when the holdout begins is excluded.
- A predictor snapshot must precede its outcome-generating event. A final diff
  containing validation added because of review is not a valid review-time
  predictor, regardless of chronological ordering.
- Training never uses observations newer than the holdout.
- Version 0.4.0 historical materialization emits one observation per stable
  `ChangeUnit`; `learn` rejects duplicate mature `change_id` values and mixed
  unit cohorts. `git_only`/`final_only` and weak-dependent cases are exploratory,
  and approval requires rich point-in-time evidence.
- Rule selection is compared with four simple baselines on the same later set.
- A candidate is immutable and content-addressed by its inputs.
- Candidate, shadow, and approved are distinct reviewed states; learning,
  promotion, assessment, and syncing are separate commands.
- The first reviewed transition must reproduce the candidate from the exact
  current evidence/configuration; approval must refer to the exact prior shadow
  manifest.
- Approval requires attributable predictions made during shadow, later-matured
  outcomes, elapsed time, both outcome classes, aggregate prospective floors,
  and per-clause matches and precision; the default precision/recall floors use
  Wilson 95% lower bounds and the MCC floor uses its point estimate. These
  integrity/prospective gates cannot be overridden.
- Only approved rules are rendered into agent skills.
- Shadow rules are never rendered or synchronized into agent skills.
- No rule match produces no recommendation.
- Assessments append immutable prediction records, and prospective reporting
  uses only the earliest prediction for each stable `unit_id` in each policy
  set. Repeated snapshots share `source.change_id`; outcome joining and elapsed
  time use that same independent-unit key.
- Every prediction binds its target, decision-time snapshot, exact reviewed
  policy manifests/rule signatures, protocol object/hash, policy-set hash,
  matches, and abstention into content identities. Its embedded observation's
  `protocol_hash` must equal `protocol.evidence_protocol_hash`. Changing the
  configured experiment, repository, outcome, target, pack name/version,
  `pack_config`, scope, or extraction threshold cannot reinterpret an old
  record.
- Reviewed transitions and timely predictions require local, non-versioned
  attestations tied to the current Git checkout/worktree. Version-controlled
  status fields alone confer no trust, and the supported CLI rejects ordinary
  copied predictions as prospective. These controls catch copying and
  accidental tampering, not a malicious process with same-user filesystem
  access.
- A reviewed deprecation tombstone excludes a policy without deleting its audit
  history.
- Generated rule text is untrusted data and cannot grant tools or permissions.
- Initial deployment is shadow-only, uses an isolated observer/ACL/CI boundary,
  does not expose shadow artifacts or predictions to the coding agent or outcome
  adjudicator, and cannot block a merge. `--blind` redacts stdout only and is not
  that isolation boundary. An initial shadow pilot never approves or syncs a
  policy.

## What would make this merely a nice experiment

RuleLoom should not graduate into a production dependency if any of these remain
true after a representative pilot:

- labels are subjective, circular, or too delayed to maintain;
- deterministic facts miss the distinctions reviewers actually use;
- configured predicates were selected from outcomes or holdout errors rather
  than locked from outcome-blind architecture evidence;
- historical predictor diffs already contain changes caused by the target event;
- the learned rules restate obvious single predicates;
- baselines perform equally well or better;
- rules change substantially with small sample perturbations;
- useful rules cover too few changes to repay operating cost;
- false positives cause prompt noise or unnecessary tests;
- maintainers do not trust or act on the explanations;
- the Codex/Claude adapters add more context cost than value;
- observed gains disappear in a prospective or controlled phase.

Failure is an acceptable result. The pilot is designed to distinguish a useful
repository-learning loop from a polished demonstration.

## Scope of version 0.4.0

Included:

- propositionalized unary Boolean facts over one repository change variable;
- `positive`, `negative`, and `unknown` labels;
- schema-v2/v3 single-pack experiment protocols with pack version,
  pack-neutral collection settings, and schema-v3 canonical `pack_config`
  included in their evidence identity;
- `generic_changes@1` for language-neutral Git path and change-shape facts;
- schema-v3 `configured_paths@1` for bounded, canonical, repository-defined
  `touches_*` path facts plus the common generic facts;
- frozen `flutter_testing@1` compatibility plus the current
  `flutter_testing@2` extractor, which recognizes both `.state =` and bare
  Riverpod `state =` mutations;
- repository-relative include/exclude scopes, configurable large-change and
  multi-file thresholds, and compact metadata with a full manifest hash;
- provider-neutral historical events, immutable logical `ChangeUnit` records,
  full bounded Git-graph bootstrap, point-in-time materialization, and evidence
  grades `rich`, `git_only`, and `final_only`;
- four separate review/CI/revert/incident outcomes, strong-only derivation by
  default, explicit weak-vote opt-in, provenance, and `unknown` on absence or
  conflict;
- grouped retrospective learning with duplicate-change rejection and a hard
  approval block for non-confirmatory historical evidence;
- a pack-agnostic ILP, evaluation, reporting, and policy lifecycle;
- bounded Horn clauses with optional closed-world negation;
- optional, externally provisioned Popper/MDL integration for noisy labels,
  restricted in version 0.4.0 to one non-recursive rule and no bootstrap reruns;
- chronological holdout, baselines, confusion metrics, and bootstrap stability;
- label-availability filtering at the holdout boundary;
- candidate, shadow, approval, deprecation, local-trust, and agent-sync
  lifecycle;
- immutable local prediction records, prospective reporting, and local
  assessment for Codex and Claude skills.

Not included:

- full relational ILP with multiple entity variables, entity joins, recursion,
  predicate invention, or arbitrary Prolog;
- causal estimation;
- automatic policy promotion;
- remote telemetry or hosted data;
- model fine-tuning;
- enforcement of security or merge policy;
- guarantees that learned correlations remain valid after repository drift;
- a universal predicate vocabulary for all languages and teams;
- evidence that a user-configured path taxonomy transfers semantically or
  predictively across repositories;
- built-in language-specific extraction beyond Flutter/Dart;
- loading third-party evidence packs through an external plugin API;
- built-in network collectors for any hosted forge, CI, or incident provider;
  those systems must currently export the normalized JSONL contract;
- combining multiple packs within one experiment.

## Evidence boundary

The cited literature supports the plausibility of repository experience,
selective abstraction, interpretable ILP, noisy-label handling, and temporal
evaluation. It does not validate RuleLoom as a combined system, show that the
generic facts are predictive in every language, or establish transfer between
pack families or configured vocabularies. Separating versioned extraction from
a pack-agnostic core is an implemented software boundary, not empirical
evidence of portability or value. Canonical schema-v3 globs make a
repository-specific hypothesis reproducible; they do not make it
outcome-independent or scientifically validated.
The evidence matrix in [RESEARCH.md](RESEARCH.md) records this distinction
explicitly; the [repository pilot protocol](PILOT-PROTOCOL.md) defines the first
local test. Passing the default
retrospective or shadow thresholds would show that the implemented measurement
contract was satisfied for one policy manifest; it would still not prove causal
agent benefit.
