# Releasing RuleLoom

Only a reviewed GitHub release may publish a package. The release workflow uses
PyPI Trusted Publishing and contains no long-lived package token.

## One-time owner setup

1. Create or claim the `ruleloom` project on PyPI.
2. In PyPI, configure a GitHub trusted publisher for repository
   `gusmondel/RuleLoom`, workflow `release.yml`, and environment `pypi`.
3. Create the protected GitHub environment `pypi` and require an owner review.
4. Keep `id-token: write` scoped to the publish job. The unprivileged build job
   checks out and executes the tag, then uploads only wheel/sdist artifacts. The
   protected publish job has no checkout, setup step, or repository-authored
   command; it downloads those artifacts and invokes the pinned publisher.

These external settings cannot be validated from a source checkout. Until the
owner completes them, the repository is publication-ready but RuleLoom must not
claim that `pipx install ruleloom` is available from PyPI.

## Release checklist

1. Confirm `main` is clean and CI is green on every supported Python/OS pair.
2. Update `pyproject.toml`, `src/ruleloom/__init__.py`, README version statements,
   schemas, and `CHANGELOG.md` in one reviewed change.
3. Run `make check`; it builds from the sdist and applies strict Twine metadata
   validation. The unprivileged release build job also installs the exact core
   wheel, core sdist, and MCP-extra wheel into separate clean Python 3.11
   environments and runs `pip check` before it uploads any artifact.
4. Inspect wheel and sdist contents, metadata, licenses, entry point, schemas,
   and the exact build-job artifact before approving the protected `pypi`
   environment.
5. Create a signed `vX.Y.Z` tag whose value exactly matches
   `ruleloom.__version__`.
6. Publish a GitHub release from that tag. The protected `pypi` environment must
   receive its independent approval before upload.
7. Install the released package from PyPI in a fresh Python 3.11 environment and
   run `ruleloom --version` and `pip check`.
8. Preserve the GitHub run URL, artifact hashes, and smoke-test output in the
   release notes.

The workflow pins every reusable action to a full commit SHA. Updating a pin
requires verifying the upstream repository/tag and reviewing the diff between
the old and new commits. The repository also fixes the exact `uv` and Hatchling
versions used to build artifacts; update those pins only in a reviewed change
that rebuilds and smoke-tests both wheel and sdist. Release builds use
`uv build --no-sources` so unpublished local source overrides cannot affect the
distribution.

PyPI documents the OIDC trust exchange and its short-lived credentials in
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/).
