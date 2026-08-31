# Contributing to RuleLoom

Thank you for helping test whether evidence-backed, interpretable repository
policies can improve coding agents. RuleLoom is alpha research software, so
reproducibility and honest negative results matter as much as new features.

## Before contributing

Read:

- [the product thesis](docs/THESIS.md), especially its falsification criteria;
- [the research basis](docs/RESEARCH.md);
- [the schema contract](docs/DATA-SCHEMA.md);
- [the security policy](SECURITY.md).

By participating, you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

## Development setup

Requirements:

- Python 3.11 or newer;
- `uv` for the maintained development workflow;
- Git;
- optional Popper prerequisites only when working on that adapter.

```bash
git clone <your-checkout-source>
cd <ruleloom-checkout>
uv sync
make check
```

Run focused checks while iterating:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```

Do not add a runtime dependency when the standard library is sufficient. A new
dependency needs a concrete benefit, license/security review, and tests for the
failure mode it introduces.

## Change workflow

1. Start from a focused issue or written problem statement.
2. Keep unrelated formatting or refactoring out of the change.
3. Add tests that fail without the change.
4. Update public schema and CLI documentation in the same change.
5. Run `make check` from a clean environment.
6. Describe risk, evidence, limitations, and manual verification in the review.

Small changes are preferred. A pull request should be independently reviewable
and should not silently rewrite existing user evidence.

## Scientific and product claims

A citation alone does not validate a feature. For a research-derived change,
state:

- the primary source and publication/preprint status;
- the task, dataset, intervention, and comparator it evaluated;
- the finding used by RuleLoom;
- the limitation or transfer gap;
- how the implementation can be falsified locally.

Do not present a benchmark improvement as an expected repository or production
effect size. Preserve unsuccessful evaluations and warnings. Never tune a
default only on the test split and then report that split as an unbiased result.

## Core invariants

Contributions must preserve these boundaries:

- facts are available at prediction time and have provenance;
- `unknown` is not coerced to `negative`;
- chronological holdout is the default;
- baseline selection uses training data only;
- candidates are immutable and tied to data/configuration hashes;
- approval is explicit and separate from learning;
- only approved rules reach generated agent skills;
- no matching rule is a valid abstention;
- generated rule/evidence text is untrusted data;
- shadow mode never changes the outcome it is labeling.

Any proposed exception must be explicit in code, tests, CLI output, and
documentation. Quietly weakening an invariant is not acceptable.

## Tests

Add the smallest appropriate test layer:

- unit tests for parsing, extraction, rule matching, scoring, and validation;
- property or generative cases for canonicalization and boundary conditions;
- integration tests for CLI lifecycle and generated adapters;
- golden fixtures only when their review value exceeds maintenance cost;
- optional-engine tests that skip with a clear reason when prerequisites are
  absent, plus a deterministic adapter test in CI.

Test leakage boundaries: timestamps, immature labels, duplicated IDs, missing
facts, baseline fitting, path traversal, shell metacharacters, and untrusted text
rendered into skills.

The configured coverage threshold is a floor, not proof of test quality.

## Schema and extractor changes

Read [docs/DATA-SCHEMA.md](docs/DATA-SCHEMA.md) before changing persisted data.

- Backward-incompatible persisted changes require a schema version and tested
  migration.
- Changing predicate meaning requires an extractor/pack version change.
- Re-extraction must not overwrite user labels or destroy old artifacts.
- Migrations must be idempotent, inspectable, and recoverable.
- Examples must contain no proprietary repository data, secrets, or personal
  information.

## Agent adapters

Keep the canonical rule semantics provider-neutral. Codex- or Claude-specific
files should be thin renderers. Do not grant tools, broaden permissions, add
network calls, or automatically execute repository content through a generated
skill. Generated output needs snapshot tests and injection-focused review.

## AI-assisted contributions

AI assistance is welcome, but the human contributor remains responsible for the
code, licenses, citations, tests, and statements in the change. Disclose
material AI generation in the review description when it affects provenance or
review strategy. Do not submit generated code that you cannot explain.

## Reporting security issues

Do not open a public issue for a suspected vulnerability. Follow
[SECURITY.md](SECURITY.md).

## Licensing

By submitting a contribution, you agree that it may be distributed under the
project's [Apache License 2.0](LICENSE). Only submit work you have the right to
license. Identify copied or adapted material and retain required notices.
