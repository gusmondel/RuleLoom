# Product thesis

## Decision first

RuleLoom addresses a real and costly problem—coding agents do not reliably
retain repository-specific experience, and teams miss repository-specific
correlated changes—but its exact proposed solution remains an unvalidated
product hypothesis.

That distinction is the project decision: the problem is supported by deployed
correlated-change systems and mixed positive/negative repository-memory
evidence; the claim that bounded ILP plus thin Codex/Claude skills solves it is
not. RuleLoom is therefore worth building as a falsifiable instrument, but not
yet worth trusting as an enforcement mechanism.

The evidence is strong enough to justify a disciplined experiment, not strong
enough to justify automatic deployment. Research shows that selected,
repository-aware experience can improve coding-agent performance. It also shows
that noisy or overly broad memory can reduce performance and increase token
usage. Rex reports real deployment of learned correlated-change rules, and the
2026 AutoSpec preprint is direct evidence that ILP-guided evolution of
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

Software repositories offer structured, repeated decisions: file types,
architectural layers, state mutation, asynchronous behavior, navigation,
authentication, payments, test additions, review outcomes, CI failures, and
later regressions. RuleLoom 0.1 encodes each change as Boolean unary predicates
and represents conjunctions of those properties as rules rather than opaque
scores. It does not learn relations among multiple entities.

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

H1 fails when RuleLoom does not beat the registered baselines, uncertainty is
too wide for a decision, or performance collapses on later periods.

### H2 — selective operational value

In prospective shadow mode, matching rules identify changes that later satisfy
the target with acceptable false-positive rate and useful lead time. Abstention
is expected and measured. The decision-time observation and exact reviewed
policy set are captured before the outcome becomes knowable. Its protocol binds
experiment, repository, unit, outcome definition, target, pack, extractor, and
configuration so incompatible evidence cannot be pooled.

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

## Safety invariants

- Collection does not alter application code.
- The default evidence path is deterministic; agent-inferred facts require an
  explicit provenance kind and confidence.
- Unknown is distinct from negative.
- Every mature label has evidence and an explicit `available_at`; a nominal
  training label unavailable when the holdout begins is excluded.
- Training never uses observations newer than the holdout.
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
  policy manifests/rule signatures, protocol snapshot/hash, policy-set hash,
  matches, and abstention into content identities. Its embedded observation's
  `protocol_hash` must equal `protocol.evidence_protocol_hash`. Changing the
  configured experiment, repository, outcome, target, or pack cannot reinterpret
  an old record.
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

## Scope of version 0.1

Included:

- propositionalized unary Boolean facts over one repository change variable;
- `positive`, `negative`, and `unknown` labels;
- deterministic Flutter-oriented extraction;
- bounded Horn clauses with optional closed-world negation;
- optional, externally provisioned Popper/MDL integration for noisy labels,
  restricted in version 0.1 to one non-recursive rule and no bootstrap reruns;
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
- a universal predicate vocabulary for all languages and teams.

## Evidence boundary

The cited literature supports the plausibility of repository experience,
selective abstraction, interpretable ILP, noisy-label handling, and temporal
evaluation. It does not validate RuleLoom as a combined system. The evidence
matrix in [RESEARCH.md](RESEARCH.md) records this distinction explicitly; the
[repository pilot protocol](PILOT-PROTOCOL.md) defines the first local test.
Passing the default
retrospective or shadow thresholds would show that the implemented measurement
contract was satisfied for one policy manifest; it would still not prove causal
agent benefit.
