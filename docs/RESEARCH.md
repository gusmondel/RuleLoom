# Research basis

## Scope and evidence standard

This review was checked against primary papers and official project
documentation available on 31 August 2026. Peer-reviewed work is distinguished
from preprints. Benchmark results are treated as evidence of plausibility within
their evaluated settings, not as expected effect sizes for a target repository.

RuleLoom combines two research streams:

1. inductive logic programming that learns compact logic programs from examples
   and background knowledge; and
2. coding-agent systems that reuse repository history or prior trajectories.

AutoSpec (2026) is a direct recent precedent for using ILP to evolve
interpretable LLM-agent safety rules from labeled traces. It materially
strengthens plausibility, but does not validate RuleLoom's different target:
repository-change outcomes, coding guidance, temporal label maturity, or any
specific repository.
The exact RuleLoom integration therefore remains a new product hypothesis even
when each component has supporting evidence.

The term ILP is deliberately scoped: RuleLoom 0.1 propositionalizes each change
into Boolean unary predicates over one variable and learns bounded Horn
conjunctions. It is not a full relational ILP implementation with multiple
entities, joins, recursion, predicate invention, or arbitrary logic programs.

## Evidence-to-design matrix

| Source | Status | Reported evidence | RuleLoom implication | Important limit |
|---|---|---|---|---|
| Cropper et al., *Inductive Logic Programming at 30* | Peer-reviewed review, 2022 | ILP induces logic programs from examples and background knowledge; the review covers modern search, recursion, predicate invention, and limitations. | Use ILP only where predicates and labels have explicit domain meaning; keep the learned program inspectable. | A general ILP review does not establish coding-agent utility. |
| Cropper & Morel, *Learning Programs by Learning from Failures* | Peer-reviewed, Machine Learning, 2021 | Popper's generate-test-constrain loop prunes failed hypothesis regions and learns textually minimal logic programs in its evaluated domains. | Offer Popper as an optional serious ILP engine; bound and record its hypothesis bias. | The original clean-example objective is not a license to treat repository labels as noise-free. |
| Hocquette et al., *Learning MDL Logic Programs from Noisy Data* | Peer-reviewed, AAAI 2024 | MAXSYNTH trades program size against fit and outperformed compared approaches in several domains under moderate label noise. | Prefer an MDL/noisy mode when labels are imperfect; record engine revision and cost assumptions. | The evaluated domains were not coding-agent policy learning. Current Popper behavior must be tested against a pinned revision rather than inferred from the paper artifact. |
| Law et al., *FastLAS* | Peer-reviewed, AAAI 2020 | FastLAS supports user-defined hypothesis scoring; the evaluated access-control tasks showed that scoring can target domain-specific objectives and that the system was faster and more scalable than compared ILP systems. | A future engine could encode asymmetric interruption and missed-risk costs explicitly. | RuleLoom 0.1 does not implement FastLAS, and access-control results do not establish coding-agent value. |
| Law, *Conflict-driven ILP* | Peer-reviewed, TPLP 2023 | Formalizes conflict-driven ILP and reports ILASP3/4 scalability gains over earlier ILASP systems, particularly with noise. | Constraint learning from failed hypotheses is a credible route beyond exhaustive rule enumeration. | It learns answer-set programs, not RuleLoom's current bounded Horn model. |
| Ma et al., *AutoSpec* | arXiv preprint v3, 7 July 2026 | ILP-guided CEGIS evolves expert LLM-agent safety rules from safe/unsafe trace annotations. Across 291 code-execution and embodied-agent traces, the authors report F1 0.98 and 0.933, up to 94% false-positive reduction, and convergence in 4–5 iterations. | This is the closest direct evidence that ILP can turn agent feedback into readable, auditable rule revisions. Counterexamples, review, and selective rule lifecycle are well-motivated. | It covers two safety domains, assumes a predicate library and usually an expert seed rule, relies on human labels, uses a random 70/30 generalization split rather than temporal validation, reports a 10-person user study, has event/local-context rules, and makes no global-optimality claim. Web/database agents and native temporal operators remain untested. It is a preprint and does not test coding-quality guidance. |
| Mehta et al., *Rex* | Peer-reviewed, NSDI 2020 | Rex learns correlated file-change rules using machine learning and program analysis. The authors report a 14-month deployment over 360 Microsoft repositories and 4,926 affected changes. | Repository-specific change rules can solve a real maintenance problem and can be surfaced at change time. | Proprietary large-service evidence; correlated file edits are not outcome-labeled ILP, and the operational count is not a randomized causal estimate. |
| Kamei et al., *A Large-Scale Empirical Study of Just-in-Time Quality Assurance* | Peer-reviewed, IEEE TSE 2013 | Establishes change-level, effort-aware defect prediction as a practical quality-assurance setting across a large empirical study. | The observation unit and decision point should be a change, and evaluation should reflect review effort rather than accuracy alone. | Defect-inducing labels and conventional change metrics are not RuleLoom's `needs_extra_validation` target or an agent intervention. |
| Falessi et al., *On the Need of Preserving Order of Data* | Peer-reviewed, Empirical Software Engineering 2020 | Across 10 classifiers and 15 projects, 10-fold AUC differed from walk-forward AUC by -0.20 to 0.22 and was statistically different in 45% of cases. | Preserve chronological order; a random split can answer the wrong deployment question. | Component-defect classification is not RuleLoom, and temporal splitting alone does not remove label leakage or drift. |
| Zeng et al., *Deep Just-in-Time Defect Prediction: How Far Are We?* | Peer-reviewed, ISSTA 2021 | On 310,370 changes, the study found deep JIT approaches did not consistently outperform traditional models; a simple added-lines logistic baseline outperformed DeepJIT and CC2Vec in the reported comparison and was far faster. | Always compare learned clauses with trivial and single-feature baselines before crediting ILP complexity. | The size baseline predicts defect-inducing changes, not missing validation or agent benefit. |
| Herbold et al., *Problems with SZZ and Features* | Peer-reviewed, Empirical Software Engineering 2022 | Manual/heuristic analysis of 398 releases from 38 Apache projects found severe SZZ label problems; only about half of commits labeled bug-fixing were actually bug-fixing, with substantial false and missed defect labels. | Outcome provenance, availability time, `unknown`, and manual audit—especially of evaluation labels—are central validity controls. | The findings concern SZZ-derived defect data; they do not quantify noise in a target repository's review labels. |
| Wang et al., *Improving Code Localization with Repository Memory* (RepoMem) | ICLR 2026 paper | Non-parametric memory over commits/issues and active-file summaries improved LocAgent on SWE-bench Verified and Live; reported gains were not uniform and the low-history subgroup worsened. | Repository history can carry useful signal, but RuleLoom must measure evidence sufficiency and abstain when history is weak. | Code localization is narrower than end-to-end code quality; retrieval also increased cost. |
| Lindenbauer et al., *From Knowledge to Noise* (CTIM-Rover) | REALM 2025 / arXiv preprint | Broad cross-task memory did not outperform AutoCodeRover in the reported configurations; distractors were identified as a likely cause and token use increased. | Do not dump raw experience into context. Keep rules few, selective, evaluated, and removable. | A negative result for episodic memory is not a direct test of ILP rules. |
| Chen et al., *SWE-Exp* | arXiv preprint, 2025 | Compact successful and failed experiences with retrieval/reranking improved reported SWE-bench Verified results; removing abstraction or reranking hurt. | Preserve negative examples and evaluate abstraction/selectivity as core components, not formatting details. | Preprint results are benchmark- and implementation-specific. |
| Lin et al., *LLMs as Continuous Learners* (EvoCoder) | arXiv preprint, 2024 | A hierarchical general/repository-specific experience pool with add, modify, merge, endorse, and remove operations improved issue-reproduction results in the reported setup. | Rules need lifecycle operations, repository scope, and deprecation—not append-only memory. | The task used issue reproduction and golden-patch-oriented evaluation, not live policy guidance. |
| Shen et al., *Structurally Aligned Subtask-Level Memory* | arXiv preprint, 2026 | Subtask-aligned memory improved mean SWE-bench Verified Pass@1 by 4.7 percentage points over vanilla agents in the reported experiments. | Match rule granularity to a concrete decision such as extra validation, rather than maintaining a monolithic “repository memory.” | Recent preprint; no evidence yet for a specific Flutter repository or ILP. |
| Xu et al., *STAIR* | arXiv preprint, 2026 | Hierarchical trajectory abstraction transferred better than raw trajectories and improved reported Pass@1 across agent integrations. | Render compact abstractions, not raw traces; keep the canonical policy independent of the agent adapter. | Repair-plan retrieval is not the same intervention as Horn-rule guidance. |
| Zhang et al., *FastContext* | arXiv preprint, 2026 | A specialized repository explorer improved reported resolution rates by up to 5.5% while reducing coding-agent tokens by up to 60%. | Measure context/token overhead and return focused evidence. | This evaluates learned exploration models, not repository policy induction. |
| Song, Minku & Yao, *Validity of Retrospective Predictive Performance Evaluation in JIT-SDP* | Peer-reviewed, Empirical Software Engineering, 2023 | Change labels arrive with delay; waiting-time choices can introduce label noise, and predictive performance should be considered over time. | Keep recent outcomes `unknown`, define maturation before collection, and split chronologically. | Defect-inducing changes are only one possible RuleLoom target; label delays differ by workflow. |

## What the literature supports

### 1. ILP for agent rules is plausible, not yet proven for this product

AutoSpec is the strongest direct support for the mechanism. It shows in a 2026
preprint that labeled agent traces, a fixed predicate vocabulary, ILP guidance,
and counterexample-driven synthesis can produce compact rule revisions with
strong held-out performance in two safety domains. FastLAS and conflict-driven
ILP separately support expressive scoring and failure-driven search.

RuleLoom deliberately does not import AutoSpec's effect sizes as expectations.
AutoSpec evolves safety guardrails, usually from an expert rule; RuleLoom learns
repository-quality guidance from change outcomes. AutoSpec's reported
generalization uses a random 70/30 split, whereas RuleLoom's deployment question
requires forward time. The preprint also assumes predicate quality and does not
test whether showing a rule improves coding outcomes. This is evidence that the
idea is technically serious, not that a particular repository will benefit.

### 2. Repository experience can help, but selection is decisive

RepoMem, SWE-Exp, EvoCoder, subtask-level memory, and STAIR all report benefits
from reusing prior repository or repair experience. Their successful mechanisms
are not simply “more context”: they use summaries, hierarchical abstraction,
task alignment, or reranking.

CTIM-Rover is the necessary counterexample. Its reported degradation makes
selectivity, abstention, and cost first-class acceptance criteria. RuleLoom
therefore emits guidance only when an approved clause matches; it does not send
the entire evidence store to the agent.

Rex adds older production evidence that repository-specific correlated-change
rules can address missed maintenance steps at scale. It strengthens the “real
problem” side of the thesis, while its Microsoft deployment, different learning
method, and non-randomized operational count prevent it from validating
RuleLoom's outcome or causal claims.

### 3. Logic programs are an auditable abstraction

ILP represents a hypothesis as clauses over named predicates. This is useful for
a human-governed policy because a reviewer can inspect the conjunction, its
support, counterexamples, and complexity. Popper provides a mature
generate-test-constrain implementation; MAXSYNTH provides peer-reviewed support
for an MDL objective under noisy labels.

FastLAS shows why domain-specific objectives matter: false interruptions and
missed risky changes need not have equal cost. Conflict-driven ILP and Popper
show how failed hypotheses can constrain later search. RuleLoom's built-in
learner is deliberately smaller: a deterministic, separate-and-conquer search
over bounded conjunctions of unary Boolean facts. It is a portable baseline,
not a reimplementation of Popper, FastLAS, ILASP, or AutoSpec's CEGIS loop.
Although the Prolog rendering uses a variable, every literal refers to the same
change observation; the v0.1 learner does not discover relations among entities.

The optional Popper adapter should not be confused with a reproduced paper
artifact. Version 0.1 accepts only one non-recursive learned rule, disables
RuleLoom bootstrap reruns, fingerprints an explicitly configured checkout, and
requires an already provisioned compatible Python environment, SWI-Prolog, and
GNU `timeout`. The adapter boundary is tested with controlled process output,
but no real Popper end-to-end run was completed in this development checkout
because SWI-Prolog was unavailable. MAXSYNTH and Popper therefore justify the
engine direction, not a claim that this particular integration reproduced their
results.

### 4. Time, labels, and simple baselines are part of the problem

Kamei establishes the practical change-level quality-assurance setting. Falessi
shows that validation order can materially alter the measured performance of
within-project defect classifiers, while Song, Minku, and Yao show that outcome
delay and waiting policy affect retrospective validity. Repository outcomes are
delayed: calling an uneventful new change `negative` creates false certainty,
while random splitting can train on the future and test on the past.

RuleLoom orders mature labels by `observed_at`, trains on the older partition,
and tests on the newer partition. Before learning, it removes a nominal training
label if its `available_at` falls after the holdout starts. It persists the exact
IDs and split boundary. Prospective predictions preserve the decision-time
observation and are joined only to an outcome that became available later.
Repeated snapshots are deduplicated by stable `unit_id`, outcomes are resolved
across observations with the same `source.change_id`, and elapsed shadow time is
computed from the earliest retained unit predictions.
`Observation.protocol_hash` and
`Prediction.protocol.evidence_protocol_hash` are the same digest binding
experiment, repository, observation unit, outcome definition, target, and pack;
the full Prediction protocol additionally binds extractor and configuration.

Herbold's results make label provenance a first-order requirement, not optional
metadata. RuleLoom therefore requires `label_evidence` for mature labels and
keeps unresolved cases `unknown`. Zeng shows why a sophisticated model can look
useful until compared with a trivial feature: RuleLoom records `never_alert`,
`always_alert`, `train_majority`, and a training-selected
`best_single_literal`, and approval requires the learned rule's test MCC to beat
the best baseline by default.

This design reduces one leakage path but does not eliminate all leakage. Feature
extractors, label definitions, duplicated changes, release cycles, and developer
identity can still leak or confound results and require review.

## What the literature does not support

No cited result establishes that:

- a RuleLoom rule improves a Codex or Claude session;
- a Flutter predicate pack transfers from one repository to another;
- `needs_extra_validation` is a valid proxy for defects or product quality;
- a high retrospective F1 causes fewer regressions;
- readable rules are necessarily trusted or followed by developers;
- the default promotion thresholds are statistically optimal;
- benchmark improvements will reproduce in a target repository;
- a first-day metric can demonstrate causal impact;
- AutoSpec's reported safety-rule F1 transfers to repository-quality guidance;
- passing the 20/50-positive readiness gates implies adequate statistical power;
- prospective association without visible guidance measures product impact.

These are local experimental questions. They are why the product includes a
candidate state, baselines, temporal holdout, warnings, approval, shadow mode,
and deprecation.

## Design decisions derived from evidence

| Evidence risk | Required mechanism |
|---|---|
| History is sparse or irrelevant | Data-readiness warnings and abstention |
| Raw memory distracts agents | Compact matching rules only |
| Labels contain noise | Required `label_evidence`, `unknown` state, maturation policy, optional MDL engine |
| Future leakage inflates results | Chronological holdout, label-availability filtering, and persisted split IDs |
| Complex hypotheses overfit | Bounded literals/rules, complexity cost, stability score |
| Complexity receives undeserved credit | Never/always alert, train-majority, and best-single-literal baselines; test MCC must improve by default |
| Rules drift | Prospective monitoring, immutable candidates, deprecation/relearning |
| Agent prompts diverge | Provider-neutral canonical rules and thin adapters |
| Automation amplifies errors | Separate candidate/shadow/approved states; explicit human promotion; no automatic publication |
| Evaluation observes the answer first | Immutable prediction ledger and exclusion of pre-existing outcomes |
| Versioned status or timestamps are copied or accidentally altered | Hash-bound local transition and timely prediction attestations per checkout/worktree; ordinary copied predictions are rejected, but malicious same-user processes remain outside this boundary |
| Shadow output changes agent or adjudicator behavior | `--blind` stdout redaction plus a separate observer account/ACL/CI workspace; do not expose shadow or prediction files to the agent/adjudicator |
| Aggregate policy performance hides a weak clause | Non-overridable temporal and prospective match/precision gates for every clause before approval |
| Context costs erase gains | Token, latency, rule count, and match coverage metrics |

## Required experimental reporting

Every RuleLoom evaluation should report:

- repository and observation window;
- target definition and maturation rule;
- predicate pack and extractor versions;
- number of eligible, collected, labeled, positive, negative, and unknown cases;
- class prevalence and missing/extraction errors;
- chronological train/test boundary and exact IDs;
- learner name, revision/version, bias, limits, seed, and timeout;
- never-alert, always-alert, train-majority, and best-single-literal results;
- confusion counts, precision, recall, F1, accuracy, balanced accuracy, MCC,
  prevalence, and predicted-positive rate;
- rule count, literals per clause, support, and bootstrap stability;
- label-evidence coverage and the number of training labels excluded because
  they were not available at the holdout boundary;
- warnings, overrides, shadow/approval rationale, and unmet gates;
- prospective results separated by `policy_set_hash`: prediction count, unique
  stable units (reported under the retained `unique_observations` field),
  duplicate predictions, mature-after-prediction count,
  still-unknown count, excluded pre-existing outcomes, total and evaluated
  matches/abstentions, coverage, latency, token/cost overhead, and lead time;
- all deviations from the pre-registered pilot plan.

The built-in `ruleloom report` currently provides aggregate outcome and
coverage metrics for each immutable `policy_set_hash`; it does not emit
confidence intervals, per-clause prospective tables, latency, token/cost, or
lead-time statistics. Promotion separately uses Wilson 95% lower bounds for
precision and recall gates and the MCC point estimate. Other uncertainty and
protocol measures require a separate, pre-registered analysis. Per-clause
temporal and shadow metrics are approval gates for the exact candidate, not a
substitute for an exported per-rule report.

Do not report accuracy alone. Do not merge retrospective and prospective samples.
Do not drop non-matching changes when calculating coverage. Do not select a
target or maturity window after inspecting which one makes the rules look best.

## References

### Inductive logic programming

- Andrew Cropper, Sebastijan Dumančić, Richard Evans, and Stephen H. Muggleton.
  [Inductive Logic Programming at
  30](https://doi.org/10.1007/s10994-021-06089-1). *Machine Learning*, 2022.
- Andrew Cropper and Rolf Morel. [Learning Programs by Learning from
  Failures](https://doi.org/10.1007/s10994-020-05934-z). *Machine Learning*,
  2021.
- Céline Hocquette, Andreas Niskanen, Matti Järvisalo, and Andrew Cropper.
  [Learning MDL Logic Programs from Noisy
  Data](https://doi.org/10.1609/aaai.v38i9.28925). *AAAI*, 2024. The authors'
  [experiment artifact](https://github.com/celinehocquette/aaai24-maxsynth) is
  also available.
- Mark Law, Alessandra Russo, Elisa Bertino, Krysia Broda, and Jorge Lobo.
  [FastLAS: Scalable Inductive Logic Programming Incorporating Domain-Specific
  Optimisation
  Criteria](https://ojs.aaai.org/index.php/AAAI/article/view/5678). *AAAI*,
  2020.
- Mark Law. [Conflict-driven Inductive Logic
  Programming](https://doi.org/10.1017/S1471068422000011). *Theory and Practice
  of Logic Programming*, 2023.
- Pingchuan Ma et al. [AutoSpec: Safety Rule Evolution for LLM Agents via
  Inductive Logic Programming](https://arxiv.org/abs/2606.24245), arXiv v3, 7
  July 2026.
- [Popper official repository and current usage
  documentation](https://github.com/logic-and-learning-lab/Popper).

### Repository experience for coding agents

- Boshi Wang et al. [Improving Code Localization with Repository
  Memory](https://arxiv.org/abs/2510.01003), 2025/ICLR 2026.
- Tobias Lindenbauer, Georg Groh, and Hinrich Schütze. [From Knowledge to Noise:
  CTIM-Rover and the Pitfalls of Episodic Memory in Software Engineering
  Agents](https://arxiv.org/abs/2505.23422), 2025.
- Silin Chen et al. [SWE-Exp: Experience-Driven Software Issue
  Resolution](https://arxiv.org/abs/2507.23361), 2025.
- Yalan Lin et al. [LLMs as Continuous Learners: Improving the Reproduction of
  Defective Code in Software Issues](https://arxiv.org/abs/2411.13941), 2024.
- Kangning Shen et al. [Structurally Aligned Subtask-Level Memory for Software
  Engineering Agents](https://arxiv.org/abs/2602.21611), 2026.
- Yisen Xu et al. [Reusing Past Repairs Through Hierarchical Trajectory
  Abstraction for Coding Agents](https://arxiv.org/abs/2607.29658), 2026.
- Shaoqiu Zhang et al. [FastContext: Training Efficient Repository Explorer for
  Coding Agents](https://arxiv.org/abs/2606.14066), 2026.

### Repository change quality and evaluation

- Sonu Mehta et al. [Rex: Preventing Bugs and Misconfiguration in Large
  Services Using Correlated Change
  Analysis](https://www.usenix.org/conference/nsdi20/presentation/mehta).
  *NSDI*, 2020.
- Yasutaka Kamei et al. [A Large-Scale Empirical Study of Just-in-Time Quality
  Assurance](https://doi.org/10.1109/TSE.2012.70). *IEEE Transactions on
  Software Engineering*, 2013.
- Davide Falessi, Jacky Huang, Likhita Narayana, Jennifer Fong Thai, and Burak
  Turhan. [On the Need of Preserving Order of Data When Validating
  Within-Project Defect
  Classifiers](https://doi.org/10.1007/s10664-020-09868-x). *Empirical Software
  Engineering*, 2020. An [author preprint](https://arxiv.org/abs/1809.01510) is
  available.
- Zeng et al. [Deep Just-in-Time Defect Prediction: How Far Are
  We?](https://doi.org/10.1145/3460319.3464819). *ISSTA*, 2021.
- Steffen Herbold, Alexander Trautsch, Fabian Trautsch, and Benjamin Ledel.
  [Problems with SZZ and Features: An Empirical Study of the State of Practice
  of Defect Prediction Data
  Collection](https://doi.org/10.1007/s10664-021-10092-4). *Empirical Software
  Engineering*, 2022. An [author preprint](https://arxiv.org/abs/1911.08938) is
  available.
- Liyan Song, Leandro L. Minku, and Xin Yao. [On the Validity of Retrospective
  Predictive Performance Evaluation Procedures in Just-in-Time Software Defect
  Prediction](https://doi.org/10.1007/s10664-023-10341-8). *Empirical Software
  Engineering*, 2023.

## Maintenance of this review

When a paper changes a design decision, update both the matrix and the relevant
protocol before changing the implementation. Record paper version/date for
preprints. Prefer peer-reviewed versions and official artifacts when they become
available. A new citation is not evidence for RuleLoom unless its evaluated task,
intervention, comparator, and limitations are stated.
