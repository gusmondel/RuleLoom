# Research basis

## Scope and evidence standard

This review was checked against primary papers and official project
documentation available on 1 September 2026. Peer-reviewed work is distinguished
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

The term ILP is deliberately scoped: RuleLoom 0.7.0 propositionalizes each change
into Boolean unary predicates over one variable and learns bounded Horn
conjunctions. It is not a full relational ILP implementation with multiple
entities, joins, recursion, predicate invention, or arbitrary logic programs.

The implementation now separates that ILP and the entire policy lifecycle from
language-specific extraction. One versioned evidence pack maps normalized Git
evidence to facts for an experiment; learning, evaluation, promotion, reporting,
and agent synchronization consume the persisted Boolean vocabulary without
language branches. New schema-v2 initializations default to
`generic_changes@1`; schema-v3 `configured_paths@1` adds a canonical
repository-defined path vocabulary. The built-in registry contains only three
pack families: `generic_changes@1`, `configured_paths@1`, frozen compatibility
pack `flutter_testing@1`, and current `flutter_testing@2`. External pack plugins
and multi-pack experiments are not supported. This is an implemented
architectural boundary, not research evidence that predicates or learned rules
transfer across languages or repositories.

## Evidence-to-design matrix

| Source | Status | Reported evidence | RuleLoom implication | Important limit |
|---|---|---|---|---|
| Cropper et al., *Inductive Logic Programming at 30* | Peer-reviewed review, 2022 | ILP induces logic programs from examples and background knowledge; the review emphasizes that suitable background knowledge is crucial and traditionally difficult or costly to hand-craft. | Use ILP only where predicates and labels have explicit domain meaning; govern a repository-configured vocabulary as pre-registered background knowledge. | A general ILP review does not validate `configured_paths@1`, any particular glob library, or coding-agent utility. |
| Srinivasan, King & Bain, *An Empirical Study of the Use of Relevance Information in Inductive Logic Programming* | Peer-reviewed, JMLR 2003 | In two biochemical tasks, incrementally adding expert-ranked groups of background predicates found models of comparable predictive accuracy substantially faster than starting with all background information; relevance ordering outperformed random ordering. | Treat vocabulary size and relevance as substantive design concerns. Use outcome-blind architecture expertise and structural audits to identify a defensible first-cut predicate library before labels are opened. | Two biochemical domains and an expert relevance ordering do not validate path prevalence as relevance, establish a universal ranking, or show that RuleLoom's audit improves predictive performance. |
| Cropper & Morel, *Learning Programs by Learning from Failures* | Peer-reviewed, Machine Learning, 2021 | Popper's generate-test-constrain loop prunes failed hypothesis regions and learns textually minimal logic programs in its evaluated domains. | Offer Popper as an optional serious ILP engine; bound and record its hypothesis bias. | The original clean-example objective is not a license to treat repository labels as noise-free. |
| Cropper & Cerna, *Efficient Rule Induction by Ignoring Pointless Rules* (REDUCER) | Peer-reviewed, AAAI 2026 | REDUCER identifies reducible rules containing redundant literals and indiscriminate rules that cannot distinguish negative training examples, then soundly prunes their specializations under its formal assumptions. Across the evaluated visual-reasoning, game-playing, and other domains, the authors report learning-time reductions up to 99% while maintaining predictive accuracy. | Flag observed equivalence and implication as candidates for redundancy review, exclude constant facts from the bounded search, and assess outcome discrimination only on training data. | RuleLoom's finite-sample co-occurrence relations are empirical diagnostics, not REDUCER's semantic entailments or sound pruning constraints. REDUCER's indiscrimination test reads negative training examples; it cannot justify outcome-blind deletion or automatic activation. |
| Hocquette et al., *Learning MDL Logic Programs from Noisy Data* | Peer-reviewed, AAAI 2024 | MAXSYNTH trades program size against fit and outperformed compared approaches in several domains under moderate label noise. | Prefer an MDL/noisy mode when labels are imperfect; record engine revision and cost assumptions. | The evaluated domains were not coding-agent policy learning. Current Popper behavior must be tested against a pinned revision rather than inferred from the paper artifact. |
| Law et al., *FastLAS* | Peer-reviewed, AAAI 2020 | FastLAS supports user-defined hypothesis scoring; the evaluated access-control tasks showed that scoring can target domain-specific objectives and that the system was faster and more scalable than compared ILP systems. | A future engine could encode asymmetric interruption and missed-risk costs explicitly. | RuleLoom 0.7.0 does not implement FastLAS, and access-control results do not establish coding-agent value. |
| Law, *Conflict-driven ILP* | Peer-reviewed, TPLP 2023 | Formalizes conflict-driven ILP and reports ILASP3/4 scalability gains over earlier ILASP systems, particularly with noise. | Constraint learning from failed hypotheses is a credible route beyond exhaustive rule enumeration. | It learns answer-set programs, not RuleLoom's current bounded Horn model. |
| Ma et al., *AutoSpec* | arXiv preprint v3, 7 July 2026 | ILP-guided CEGIS evolves expert LLM-agent safety rules from safe/unsafe trace annotations. Across 291 code-execution and embodied-agent traces, the authors report F1 0.98 and 0.933, up to 94% false-positive reduction, and convergence in 4–5 iterations. | This is the closest direct evidence that ILP can turn agent feedback into readable, auditable rule revisions. Counterexamples, review, and selective rule lifecycle are well-motivated. | It covers two safety domains, assumes fixed domain predicate libraries and usually an expert seed rule, and relies on human labels. Its RQ3 generalization analysis uses a random 70/30 split, not temporal validation. Its 10-practitioner study evaluates four supplied scenarios rather than field maintenance. Candidate evaluation against labeled traces is not formal verification. Web/database agents and native temporal operators remain untested; it is a preprint and does not test coding-quality guidance. |
| Yu et al., *ADVENT: LLM-Driven Automatic Predicate Invention for ILP* | arXiv preprint v1, 2 July 2026 | ADVENT couples LLM predicate proposals with Prolog execution and refinement. On nine transformed poker-hand concepts across seven LLMs, the authors report 58% success without verification, 80% with formal deductive feedback, and cross-task gains up to 31 percentage points for some compositional concepts. | LLM-assisted predicate invention is a credible future research direction only when proposals are deterministically executable, reviewed, versioned, and evaluated as a new experiment. An LLM should be a proposer, never an activator. | The study is a preprint on one transformed poker domain. Its verifier exposes labeled positive/negative examples, its unfiltered knowledge pool hurt a simpler task, and neither its relational predicates nor results validate repository-path concepts, temporal evaluation, or autonomous policy publication. RuleLoom v0.7.0 does not implement ADVENT or predicate invention. |
| Mehta et al., *Rex* | Peer-reviewed, NSDI 2020 | Rex learns correlated file-change rules using machine learning and program analysis. In a 14-month deployment over 360 Microsoft repositories, the authors counted 4,926 suggestions as true positives when engineers added the suggested related change. | Repository-specific change suggestions can be learned and surfaced at change time. | Suggestion acceptance is a dependent operational outcome, not independent ground truth that a bug was prevented. The proprietary, non-randomized deployment is not outcome-labeled ILP or a causal estimate. |
| Kamei et al., *A Large-Scale Empirical Study of Just-in-Time Quality Assurance* | Peer-reviewed, IEEE TSE 2013 | Establishes change-level, effort-aware defect prediction as a practical quality-assurance setting across a large empirical study. | The observation unit and decision point should be a change, and evaluation should reflect review effort rather than accuracy alone. | Defect-inducing labels and conventional change metrics are not RuleLoom's `needs_extra_validation` target or an agent intervention. |
| Falessi et al., *On the Need of Preserving Order of Data* | Peer-reviewed, Empirical Software Engineering 2020 | Across nine classifiers and 15 projects, walk-forward statistically outperformed 10-fold cross-validation and bootstrap in the reported classifier-selection AUC, bias, and absolute-bias metrics. | Preserve chronological order; a random split can answer the wrong deployment question. | Component-defect classification is not RuleLoom, and temporal splitting alone does not remove label leakage or drift. |
| Zeng et al., *Deep Just-in-Time Defect Prediction: How Far Are We?* | Peer-reviewed, ISSTA 2021 | On 310,370 changes, the study found deep JIT approaches did not consistently outperform traditional models; a simple added-lines logistic baseline outperformed DeepJIT and CC2Vec in the reported comparison and was far faster. | Always compare learned clauses with trivial and single-feature baselines before crediting ILP complexity. | The size baseline predicts defect-inducing changes, not missing validation or agent benefit. |
| Herbold et al., *Problems with SZZ and Features* | Peer-reviewed, Empirical Software Engineering 2022 | Manual/heuristic analysis of 398 releases from 38 Apache projects found severe SZZ label problems; only about half of commits labeled bug-fixing were actually bug-fixing, with substantial false and missed defect labels. | Outcome provenance, availability time, `unknown`, and manual audit—especially of evaluation labels—are central validity controls. | The findings concern SZZ-derived defect data; they do not quantify noise in a target repository's review labels. |
| Fregnan et al., *The Evolution of the Code During Review* | Peer-reviewed, Empirical Software Engineering 2022 | Across three open-source projects, review changes were mostly evolvability-related, initial-patch size/new lines related to review-change count, and more than 60% of review changes were not explicitly triggered by reviewer comments. | Preserve the initial patch separately from later revisions; an explicit review request is strong evidence, but later code movement alone is only a weak proxy. | Three Gerrit-based projects do not establish the same review process or label distribution elsewhere. |
| Gallaba et al., *Noise and Heterogeneity in Historical Build Data* | Peer-reviewed, ASE 2018 | In 3.7 million Travis CI jobs from 1,276 projects, 12% of passing builds contained an actively ignored failure, 9% of builds had a misleading/incorrect outcome on average, and at least 44% of broken builds contained passing jobs. | Never map a single CI status directly to truth; require attributable event sequences and preserve check identity. | Travis CI behavior from the study period does not quantify current noise in a target provider. |
| Huang et al., *Is This Build Failure Related to My Patch?* | Peer-reviewed, Empirical Software Engineering 2026 | The study extracted 77,354 failures from seven Apache projects and documented legitimate failures unrelated to the current push; its curated/semi-supervised analysis separates relatedness from flakiness. | A positive CI label requires explicit change attribution plus a later code change and success of the same check; an arbitrary failure abstains. | The proposed PU models and Apache/JIRA workflow are not implemented by RuleLoom and their reported performance does not transfer. |
| McIntosh & Kamei, *Are Fix-Inducing Changes a Moving Target?* | Peer-reviewed, IEEE TSE 2018 | In 37,524 Qt/OpenStack changes, discrimination, calibration, and feature roles shifted over time; the authors recommend recent training for prediction and longer windows for stable quality planning in their setting. | Ingest all available history for audit, but evaluate recency windows chronologically and monitor drift instead of assuming older examples remain exchangeable. | Its target, projects, and suggested windows are not universal RuleLoom thresholds. |
| Shrikanth et al., *Early Life Cycle Software Defect Prediction* | Peer-reviewed, ICSE 2021 | Across hundreds of GitHub projects, predictors using roughly the first 150 commits/four months performed comparably to later-history alternatives in the studied defect task. | A repository need not wait a year before instrumentation; test whether existing early history is sufficient against simple baselines. | This does not make four months or 150 commits a universal minimum, nor validate RuleLoom outcomes. |
| Wang et al., *Improving Code Localization with Repository Memory* (RepoMem) | ICLR 2026 paper | Non-parametric memory over commits/issues and active-file summaries improved LocAgent on SWE-bench Verified and Live; reported gains were not uniform and the low-history subgroup worsened. | Repository history can carry useful signal, but RuleLoom must measure evidence sufficiency and abstain when history is weak. | Code localization is narrower than end-to-end code quality; retrieval also increased cost. |
| Lindenbauer et al., *From Knowledge to Noise* (CTIM-Rover) | Peer-reviewed REALM workshop paper, 2025 | Broad cross-task memory did not outperform AutoCodeRover in the reported configurations; distractors were identified as a likely cause and token use increased. | Do not dump raw experience into context. Keep rules few, selective, evaluated, and removable. | A negative result for episodic memory is not a direct test of ILP rules. |
| Chen et al., *SWE-Exp* | arXiv preprint v2, 2 February 2026 | Compact successful and failed experiences with retrieval/reranking improved reported SWE-bench Verified results; removing abstraction or reranking hurt. | Preserve negative examples and evaluate abstraction/selectivity as core components, not formatting details. | Preprint results are benchmark- and implementation-specific. |
| Lin et al., *LLMs as Continuous Learners* (EvoCoder) | arXiv preprint, 2024 | A hierarchical general/repository-specific experience pool with add, modify, merge, endorse, and remove operations improved issue-reproduction results in the reported setup. | Rules need lifecycle operations, repository scope, and deprecation—not append-only memory. | The task used issue reproduction and golden-patch-oriented evaluation, not live policy guidance. |
| Shen et al., *Structurally Aligned Subtask-Level Memory* | arXiv preprint, 2026 | Subtask-aligned memory improved mean SWE-bench Verified Pass@1 by 4.7 percentage points over vanilla agents in the reported experiments. | Match rule granularity to a concrete decision such as extra validation, rather than maintaining a monolithic “repository memory.” | Recent preprint; no evidence yet for any RuleLoom evidence pack or ILP. |
| Xu et al., *STAIR* | arXiv preprint, 2026 | Hierarchical trajectory abstraction transferred better than raw trajectories and improved reported Pass@1 across agent integrations. | Render compact abstractions, not raw traces; keep the canonical policy independent of the agent adapter. | Repair-plan retrieval is not the same intervention as Horn-rule guidance. |
| Song, Minku & Yao, *Validity of Retrospective Predictive Performance Evaluation in JIT-SDP* | Peer-reviewed, Empirical Software Engineering, 2023 | Across 13 projects, varying evaluation waiting time among 15/30/60/90 days had no significant effect in the reported model (`p=.564`), while longer training waiting time had a significant positive effect (`p=.028`, standardized coefficient `.22`). | Register training-label availability and evaluation maturation separately; test both locally rather than treating “waiting time” as one universal bias. | Defect-inducing changes are only one possible RuleLoom target; its waiting periods and label delays need not transfer to review or incident outcomes. |

## What the literature supports

### 1. ILP for agent rules is plausible, not yet proven for this product

AutoSpec is the strongest direct support for the mechanism. It shows in a 2026
preprint that labeled agent traces, a fixed predicate vocabulary, ILP guidance,
and counterexample-driven synthesis can produce compact rule revisions with
strong held-out performance in two safety domains. FastLAS and conflict-driven
ILP separately support expressive scoring and failure-driven search.

RuleLoom deliberately does not import AutoSpec's effect sizes as expectations.
AutoSpec evolves safety guardrails, usually from an expert rule; RuleLoom learns
repository-quality guidance from change outcomes. AutoSpec's RQ3 generalization
analysis uses a random 70/30 split, whereas RuleLoom's deployment question
requires forward time. Its small practitioner study presents four supplied
scenarios, not a live maintenance intervention. The preprint also assumes
predicate quality and does not test whether showing a rule improves coding
outcomes. Its candidate scoring against labeled traces should not be described
as formal verification. This is evidence that the idea is technically serious,
not that a particular repository will benefit.

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
suggestions can be deployed at scale. Its reported 4,926 true positives were
suggestions followed by engineers adding the related change; that operational
acceptance definition is not independent evidence that defects were prevented.
The Microsoft deployment, different learning method, and non-randomized outcome
therefore strengthen problem plausibility without validating RuleLoom's outcome
or causal claims.

### 3. Logic programs are an auditable abstraction

ILP represents a hypothesis as clauses over named predicates. This is useful for
a human-governed policy because a reviewer can inspect the conjunction, its
support, counterexamples, and complexity. Popper provides a mature
generate-test-constrain implementation; MAXSYNTH provides peer-reviewed support
for an MDL objective under noisy labels.

FastLAS shows why domain-specific objectives matter: false interruptions and
missed risky changes need not have equal cost. Conflict-driven ILP and Popper
show how failed hypotheses can constrain later search. REDUCER further shows
that, under its formal logic-program setting, rules with semantically redundant
literals or literals unable to discriminate negative training examples can be
pruned soundly and efficiently. RuleLoom's built-in learner is deliberately
smaller: a deterministic, separate-and-conquer search over bounded conjunctions
of unary Boolean facts. Its outcome-blind audit reports only relations observed
in a finite repository sample. Those relations can prompt review, but they are
not logical entailments and do not reproduce REDUCER's pruning theorem.
RuleLoom is a portable baseline, not a reimplementation of REDUCER, Popper,
FastLAS, ILASP, or AutoSpec's CEGIS loop. Although the Prolog rendering uses a
variable, every literal refers to the same change observation; the v0.7.0
learner does not discover relations among entities.

The evidence-pack boundary preserves that same core across languages. The
`generic_changes@1` pack uses portable Git path and change-shape signals such as
tests, documentation, CI, dependency manifests, churn, and file count without
parsing source syntax. That mechanism is language-neutral, but its filename
conventions are heuristics, not a claim of equal semantic coverage in every
ecosystem. Schema-v3 `configured_paths@1` adds repository-defined `touches_*`
facts over normalized paths plus the shared generic facts; the protocol requires
their design to be outcome-blind. It does not read content. `flutter_testing@2` layers Dart/Flutter predicates on the shared
evidence contract and, unlike frozen `flutter_testing@1`, recognizes both
qualified `.state =` and bare Riverpod `state =` mutations. A semantic extractor
or configured-vocabulary change receives a new evidence identity rather than
reinterpreting old observations. One experiment selects one pack/version and
one canonical configuration; no cited result justifies combining different
predicate vocabularies as if they were one sample.

Pack-neutral `EvidenceConfig` supplies repository-relative outcome-eligibility
scopes and configurable churn/file-count thresholds. Schema v2 binds those
settings, the pack version, and extractor identity into
`evidence_protocol_hash`; schema v3 additionally binds canonical `pack_config`.
Configured predicate globs create features only over already scoped files and
never widen the outcome cohort. The collector also bounds sampled file-path
metadata for large changes while retaining aggregate counts, explicit truncation counts,
and a full change-manifest hash. These are reproducibility and integrity
controls; they do not establish that the extracted facts are useful predictors.

The optional Popper adapter should not be confused with a reproduced paper
artifact. Version 0.7.0 accepts only one non-recursive learned rule, disables
RuleLoom bootstrap reruns, fingerprints an explicitly configured checkout, and
requires an already provisioned compatible Python environment, SWI-Prolog, and
GNU `timeout`. The adapter boundary is tested with controlled process output,
but no real Popper end-to-end run was completed in this development checkout
because SWI-Prolog was unavailable. MAXSYNTH and Popper therefore justify the
engine direction, not a claim that this particular integration reproduced their
results. Candidate identity composes RuleLoom's versioned Popper
export/validation adapter with the external checkout-and-environment
fingerprint, so a change to either side cannot masquerade as the same engine
revision.

### 4. Configurable vocabularies require outcome-blind governance

The ILP review supports treating predicate/background-knowledge design as a
substantive modeling decision: choosing useful background knowledge is crucial,
and hand-crafting it is traditionally difficult and costly. That observation
motivates an inspectable `pack_config`; it does not establish that any chosen
component glob predicts a later outcome. Rex supports the plausibility of
repository-specific file associations, but Rex learned correlations from
history and used suggestion acceptance as its deployment outcome. It does not
validate hand-authored configured paths.

Srinivasan, King, and Bain provide primary evidence that irrelevant background
predicates can hinder ILP search and that expert relevance ordering can produce
a useful first model faster in their two biochemical tasks. RuleLoom adopts the
conservative part of that result: before outcomes are opened, inspect the frozen
vocabulary's prevalence, path examples, constant or extreme behavior, observed
relations, and temporal drift. The `ruleloom predicates audit` command uses
facts, chronology, and extraction metadata only. It does not measure association
with the target and it does not delete predicates. A predicate true on roughly
half the observations is not thereby relevant or irrelevant; predictive
selection requires labeled training data.

REDUCER motivates looking for redundancy, but its formal semantic implication
tests are stronger than RuleLoom's observed equivalence and implication flags.
An implication seen in one repository window can disappear after a path move or
new change pattern. Likewise, a constant or saturated predicate is
non-discriminative in the audited window, not proven useless forever. The audit
therefore diagnoses vocabulary mechanics and drift; train-only ranking and rule
induction answer the separate predictive question, and the chronological
holdout remains untouched until evaluation.

To keep finite-sample relations inspectable, exact equivalence and high overlap
need at least two supporting observations, while one-way implications require an
antecedent at least as frequent as the audit's rare threshold. These are report
filters, not semantic entailment tests or learned pruning rules.

During learning, RuleLoom applies a narrower redundancy reduction: using only
the temporally eligible training cohort, it removes constant truth columns and
keeps the lexically first representative of each exact duplicate column. The
candidate records all observed exclusions and aliases; the outcome-blind audit
separately identifies declared predicates that never occur. The future holdout
can expose that an alias diverged, but cannot retroactively choose a different
representative. This is an engineering reduction for the propositionalized
search, not a claim to implement REDUCER's semantic pruning.

A configurable vocabulary creates an additional researcher degree of freedom.
If paths are selected after inspecting positives, learned rules, or holdout
errors, the vocabulary itself has been fit to the outcome. Giving it a new
experiment ID does not restore an untouched test. RuleLoom therefore requires
the full path library, design revision, roles, rationale, and canonical hash to
be locked before outcome access, with every attempted configuration retained.
Any outcome-informed revision must be evaluated on a new future confirmation
window. The best-single-literal baseline must include configured predicates so
a component flag is not mistaken for value from rule conjunctions.

Path matching is language-neutral only at the extraction boundary. A taxonomy
such as `touches_client_ui` is semantically specific to one repository and may
drift when ownership or layout changes. No cited paper validates
`configured_paths@1`, schema v3, its safety limits, or predictive transfer of
its user-supplied globs.

ADVENT suggests that an LLM can help propose semantically named predicates when
paired with executable deductive feedback, but it does not justify autonomous
predicate activation. Its experiments are limited to transformed poker concepts
and its refinement loop uses labeled examples. In RuleLoom, an LLM may propose
concepts from outcome-blind repository structure and documentation; deterministic
extraction plus human review must accept or reject them. Any accepted semantic
change creates a new protocol hash and experiment before outcomes are opened. If
labels informed the proposal or verification, the affected sample is design
data and confirmation requires an untouched future window.

The same boundary applies to existing repository guidance. AutoSpec usually
starts from an expert rule, and the broader ILP literature treats background
knowledge as a substantive modeling decision. Together they support an expert
rule as an auditable seed hypothesis, not as a validated policy. No cited result
shows that an instruction in `AGENTS.md`, `CLAUDE.md`, or another document is
correct, current, predictive, or safe merely because it already exists.
RuleLoom therefore requires an explicit human translation, hashes optional
source spans without parsing their prose, and treats historical
coverage/association as post-hoc diagnostics. This seed does not create or
mature outcomes, so it does not solve the label cold start. A manual rule skips
claims of retrospective discovery and can earn approval only from later
prospective shadow evidence.

### 5. Time, labels, and simple baselines are part of the problem

Kamei establishes the practical change-level quality-assurance setting. Falessi
shows that validation order can materially alter the measured performance of
within-project defect classifiers. Song, Minku, and Yao found no significant
effect from varying the evaluation waiting time in their reported model, but
did find a significant positive effect from longer training waiting time.
RuleLoom therefore registers training-label availability and evaluation
maturation separately instead of generalizing one “waiting-time” conclusion.
Repository outcomes are still delayed: calling an uneventful new change `negative` creates
false certainty, while random splitting can train on the future and test on the
past.

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
the evidence protocol. For schema v2/v3 that identity includes experiment,
repository, observation unit, outcome definition, target, pack name/version,
extractor identity, and exact `EvidenceConfig` scopes and thresholds; schema v3
also includes canonical `pack_config`. The compact Prediction protocol binds the
complete configuration hash but does not embed a self-contained copy of those
settings, so independent reports must preserve the canonical config.

A chronological split cannot repair outcome-caused features. For a review-time
target, the final merge/squash diff may already contain validation added in
response to review. Version 0.7.0 represents one logical `ChangeUnit` with an
exact point-in-time base/prediction SHA and groups it by stable `change_id`.
Materialization emits one observation per unit and rejects duplicate mature
change IDs, preventing related snapshots from crossing train and test. A
Git-only commit or final-state reconstruction is retained as exploratory and
cannot support approval. Range and worktree snapshots remain prospective.

Herbold's results make label provenance a first-order requirement, not optional
metadata. RuleLoom therefore requires `label_evidence` for mature labels and
keeps unresolved cases `unknown`. Zeng shows why a sophisticated model can look
useful until compared with a trivial feature: RuleLoom records `never_alert`,
`always_alert`, `train_majority`, and a training-selected
`best_single_literal`, and approval requires the learned rule's test MCC to beat
the best baseline by default.

### 6. Cold start should reuse evidence, not manufacture certainty

The literature does not support waiting a fixed year before trying a repository
model. Shrikanth et al. found useful early-life defect signal in their studied
projects, while McIntosh and Kamei showed that old and recent periods may not be
exchangeable. Together they support ingesting the complete available graph for
audit, then selecting/evaluating windows in forward time rather than imposing a
universal calendar minimum.

Repository archives are not automatically labels. Fregnan et al. show why the
initial review patch must be separated from later revisions and why a later test
change without an explicit trigger is ambiguous. Gallaba et al. and Huang et al.
show why CI status alone is noisy and why attribution to the current patch is a
separate question. Herbold et al. show why SZZ-derived links are not ground
truth. RuleLoom v0.7.0 therefore implements three evidence grades:

- `rich`: a point-in-time logical change plus ordered independent events;
- `git_only`: topology/metadata without a provider decision point; and
- `final_only`: a merge/final state that may contain outcome-caused changes.

All three can be inspected on day one. Only rich units with strong, strictly
later evidence are confirmatory. Strong labels require an explicit completed
outcome, an independent review validation request, an attributable CI
fail–code-change–same-check-pass sequence, or an explicit revert/incident link.
Later test changes, fix keywords, SZZ, an unattributed merge-result failure, and
an exact Git revert-trailer association are weak opt-in votes. Absence and
conflict remain `unknown`. This is a conservative weak-supervision layer, not a
claim that archive mining recovers truth.

The built-in GitHub adapter implements the conservative archive case: it groups
closed PR lineage, review/check events, and bounded revert evidence, while
marking the reconstructed predictor snapshot `point_in_time=false` and every
unit `git_only`. It deliberately ignores archive timeline label names: the
provider can join an old application event to a Label object's current mutable
name, so a rename can create retrospective temporal leakage. Strong label-backed
evidence requires a separate point-in-time webhook/export/immutable ledger and
normalized event import. This is a measurement-integrity requirement derived
from the provider contract: GitHub's GraphQL reference exposes
[`LabeledEvent.label` as a `Label` object](https://docs.github.com/en/graphql/reference/issues#labeledevent),
that object has mutable `updatedAt`, and the REST API explicitly permits
[renaming a label](https://docs.github.com/en/rest/issues/labels#update-a-label).
It is not a claim established by the cited research literature. RuleLoom v0.7.0
ships a local Action/webhook capture substrate for future events, but the archive
adapter still cannot reconstruct this state retrospectively and automatic label
supply remains an operational claim that must be measured.

This design reduces several leakage paths but does not eliminate all leakage.
Feature-library selection, outcome-caused diffs, label definitions, duplicated
or cross-split change groups, release cycles, and developer identity can still
leak or confound results and require review.

## What the literature does not support

No cited result establishes that:

- a RuleLoom rule improves a Codex or Claude session;
- the generic pack is equally predictive across programming languages;
- a specialized pack or learned rule transfers across repositories, languages,
  pack families, or pack versions;
- a schema-v3 configured path library is outcome-independent merely because its
  JSON is canonical or its extractor is deterministic;
- repository-specific `touches_*` globs transfer semantically or predictively
  to another repository;
- an outcome-blind prevalence, overlap, or drift flag establishes predictive
  relevance or irrelevance;
- an existing hand-authored instruction is valid because it has broad
  historical trigger coverage;
- an archived provider label name records what that label was called when the
  historical application occurred, or is ground truth because it matches a
  chosen syntax;
- an LLM-proposed predicate is correct, safe to activate, or portable merely
  because it has a meaningful name or executes successfully;
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
| Different components contaminate one observation | Repository-relative include/exclude scopes fixed before collection |
| Extractor semantics drift or vocabularies are mixed | One versioned pack/configuration per experiment; pack, extractor, canonical `pack_config`, scopes, and thresholds bound into `evidence_protocol_hash` |
| Configured vocabulary is chosen using outcomes or holdout errors | Outcome-blind feature-design roles, pre-registered config hash and attempt log, plus an untouched future confirmation window after any informed revision |
| Predicate library is mechanically empty, constant, redundant, saturated, or drifting before labels | Outcome-blind `predicates audit` over prevalence, bounded path examples, observed relations, and early/late windows; semantic repairs start a new experiment/hash |
| Existing guidance is mistaken for a learned or validated rule | Strict human-authored Horn manifest, source hashing without prose interpretation, explicitly post-hoc audit, and prospective-only approval evidence |
| Final diff contains files added because of the outcome | Reconstruct one point-in-time `ChangeUnit` from its pre-event base/prediction SHA; mark Git-only/final-only cases exploratory and block their approval |
| Related commits cross train and test | Materialize one observation per stable `change_id`; reject duplicate mature IDs and mixed observation-unit cohorts |
| CI status is noisy or unrelated to the patch | Require explicit attribution plus fail–code-change–same-check-pass for the strong CI target; otherwise abstain |
| Weak archive heuristic is mistaken for ground truth | Strong-only labels by default; weak votes require opt-in, retain provenance, and make the dependent case non-confirmatory |
| A mutable archive label name is mistaken for point-in-time adjudication | Ignore GitHub archive timeline label names; require a separately captured immutable webhook/export/ledger event for any strong label-backed outcome |
| Very large changes exceed practical artifact limits | Bounded path/per-file metadata, explicit truncation counts, aggregate totals, and a full manifest hash |
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
- config schema, the experiment's single predicate pack/version, extractor
  identity, canonical `pack_config`/hash when applicable, feature-design
  revision/lock time, and complete configuration-attempt count;
- `EvidenceConfig` include/exclude scopes, large-change and multi-file
  thresholds, metadata limit, and any metadata truncation counts;
- number of eligible, collected, labeled, positive, negative, and unknown cases;
- historical event/change-unit counts by `rich`, `git_only`, and `final_only`,
  plus strong/weak/conflicting vote counts and shallow/truncated Git status;
- for a GitHub archive import: exact command and JSON report, public-host/origin
  verification or explicit `repository_binding` override, the precise
  `created_at`/as-of window semantics, per-endpoint and global bounds, global
  budget policy/limits/use, `manifest_hash`, normalized/skipped PRs,
  materialization skips caused by unavailable Git objects,
  truncation/warnings, evidence-grade counts, and confirmation that archive
  timeline label names contributed no outcomes; verify the emitted compact
  pre-hash `manifest` and preserve the canonical logs containing the exact
  normalized records alongside the report;
- for any external label-backed evidence: point-in-time capturer/exporter
  version, immutable source-ledger reference, original event timestamp,
  repository/change binding, actor authorization/independence, target/value,
  maturity/completeness, conflicts, and corrections;
- class prevalence and missing/extraction errors;
- the pre-outcome predicate-audit artifact, its ordering/window sizes and
  thresholds, repository/experiment/target/config/protocol identity,
  outcome-blind observation and complete-audit manifest hashes, configured
  coverage, predicate prevalence/flags, observed relations, bounded path
  examples, warnings, and any resulting new experiment;
- chronological train/test boundary and exact IDs;
- predictor base/SHA/time, stable logical change ID, external independence audit,
  and count excluded because no pre-outcome rich snapshot was available;
- learner name, revision/version, bias, limits, seed, and timeout;
- never-alert, always-alert, train-majority, and best-single-literal results;
- configured-predicate train/test/shadow prevalence, zero/always-true facts,
  observed overlaps, and path-layout drift when applicable;
- confusion counts, precision, recall, F1, accuracy, balanced accuracy, MCC,
  prevalence, and predicted-positive rate;
- rule count, literals per clause, support, and bootstrap stability;
- for a manual rule: exact declaration/source hashes and statuses, historical
  coverage, censored/unknown count, post-hoc metrics/baselines, and an explicit
  statement that the audit was non-confirmatory and approval evidence is
  prospective-only;
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
- Ashwin Srinivasan, Ross D. King, and Michael E. Bain. [An Empirical Study of
  the Use of Relevance Information in Inductive Logic
  Programming](https://jmlr.org/papers/v4/srinivasan03a.html). *Journal of
  Machine Learning Research*, 2003.
- Andrew Cropper and Rolf Morel. [Learning Programs by Learning from
  Failures](https://doi.org/10.1007/s10994-020-05934-z). *Machine Learning*,
  2021.
- Andrew Cropper and David M. Cerna. [Efficient Rule Induction by Ignoring
  Pointless
  Rules](https://doi.org/10.1609/aaai.v40i23.38972). *AAAI*, 2026. The paper
  introduces REDUCER; the authors provide an [experiment
  artifact](https://github.com/logic-and-learning-lab/aaai26-implications).
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
  Inductive Logic Programming](https://arxiv.org/abs/2606.24245v3), arXiv v3, 7
  July 2026.
- Tingting Yu, Pei-Cing Huang, Chan Hsu, Chan-Tung Ku, and Yihuang Kang.
  [ADVENT: LLM-Driven Automatic Predicate Invention for
  ILP](https://arxiv.org/abs/2607.01585), arXiv v1, 2 July 2026.
- [Popper official repository and current usage
  documentation](https://github.com/logic-and-learning-lab/Popper).

### Repository experience for coding agents

- Boshi Wang et al. [Improving Code Localization with Repository
  Memory](https://arxiv.org/abs/2510.01003), 2025/ICLR 2026.
- Tobias Lindenbauer, Georg Groh, and Hinrich Schütze. [From Knowledge to Noise:
  CTIM-Rover and the Pitfalls of Episodic Memory in Software Engineering
  Agents](https://aclanthology.org/2025.realm-1.30/). *REALM*, 2025.
- Silin Chen et al. [SWE-Exp: Experience-Driven Software Issue
  Resolution](https://arxiv.org/abs/2507.23361v2), arXiv v2, 2 February 2026.
- Yalan Lin et al. [LLMs as Continuous Learners: Improving the Reproduction of
  Defective Code in Software Issues](https://arxiv.org/abs/2411.13941), 2024.
- Kangning Shen et al. [Structurally Aligned Subtask-Level Memory for Software
  Engineering Agents](https://arxiv.org/abs/2602.21611), 2026.
- Yisen Xu et al. [Reusing Past Repairs Through Hierarchical Trajectory
  Abstraction for Coding Agents](https://arxiv.org/abs/2607.29658), 2026.

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
- Enrico Fregnan, Fernando Petrulio, and Alberto Bacchelli. [The Evolution of
  the Code During Review: An Investigation on Review
  Changes](https://doi.org/10.1007/s10664-022-10205-7). *Empirical Software
  Engineering*, 2022.
- Keheliya Gallaba, Christian Macho, Martin Pinzger, and Shane McIntosh. [Noise
  and Heterogeneity in Historical Build Data: An Empirical Study of Travis
  CI](https://doi.org/10.1145/3238147.3238171). *ASE*, 2018.
- Yonghui Andie Huang et al. [Is This Build Failure Related to My Patch? An
  Empirical Study of Unrelated Build Failures in Continuous
  Integration](https://doi.org/10.1007/s10664-026-10874-8). *Empirical
  Software Engineering*, 2026.
- Shane McIntosh and Yasutaka Kamei. [Are Fix-Inducing Changes a Moving Target?
  A Longitudinal Case Study of Just-in-Time Defect
  Prediction](https://doi.org/10.1109/TSE.2017.2693980). *IEEE Transactions on
  Software Engineering*, 2018.
- N. C. Shrikanth, Suvodeep Majumder, and Tim Menzies. [Early Life Cycle
  Software Defect Prediction. Why? How?](https://doi.org/10.1109/ICSE43902.2021.00050).
  *ICSE*, 2021.
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
