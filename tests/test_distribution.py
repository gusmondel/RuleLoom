from __future__ import annotations

import re
import tomllib
from pathlib import Path

import ruleloom

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def test_all_reusable_actions_are_pinned_to_full_commit_sha() -> None:
    references: list[tuple[Path, str]] = []
    workflows = sorted((*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")))
    for workflow in workflows:
        for line in workflow.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith("uses:") and not stripped.startswith("- uses:"):
                continue
            reference = stripped.split("uses:", 1)[1].split("#", 1)[0].strip()
            references.append((workflow, reference))
            assert re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", reference), (
                f"{workflow} must pin {reference!r} to a full commit SHA"
            )
    assert references


def test_release_workflow_uses_scoped_oidc_without_package_token() -> None:
    release = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    build_job = release.split("  build:\n", 1)[1].split("\n  publish:\n", 1)[0]
    publish_job = release.split("\n  publish:\n", 1)[1]
    publish_actions = [
        line.strip().split("uses:", 1)[1].strip()
        for line in publish_job.splitlines()
        if line.strip().startswith("uses:")
    ]

    assert "pull_request_target" not in release
    assert "id-token: write" not in build_job
    assert "actions/upload-artifact@" in build_job
    assert "needs: build" in publish_job
    assert "id-token: write" in publish_job
    assert "environment: pypi" in publish_job
    assert "actions/download-artifact@" in publish_job
    assert "actions/checkout@" not in publish_job
    assert "setup-uv@" not in publish_job
    assert "run:" not in publish_job
    assert len(publish_actions) == 2
    assert publish_actions[0].startswith("actions/download-artifact@")
    assert publish_actions[1].startswith("pypa/gh-action-pypi-publish@")
    assert "persist-credentials: false" in release
    assert "PYPI_API_TOKEN" not in release
    assert "password:" not in release


def test_distribution_metadata_is_release_ready_but_not_claimed_published() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]

    assert pyproject["build-system"]["requires"] == ["hatchling==1.32.0"]
    assert pyproject["tool"]["uv"]["required-version"] == "==0.10.6"
    assert "twine==7.0.0" in pyproject["dependency-groups"]["dev"]
    assert project["version"] == ruleloom.__version__
    assert project["urls"]["Repository"] == "https://github.com/gusmondel/RuleLoom.git"
    included = pyproject["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    assert "/CHANGELOG.md" in included
    assert "/examples" in included
    assert "/integrations" in included
    assert "/scripts" in included
    assert "/.github/workflows" in included
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "pipx install ruleloom" not in readme


def test_ci_and_release_pin_the_build_toolchain() -> None:
    ci = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    release = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert 'version: "0.10.6"' in ci
    assert 'version: "0.10.6"' in release
    assert "\tuv build --no-sources\n" in makefile
    assert "\tuv run --frozen twine check --strict dist/*.whl dist/*.tar.gz\n" in makefile


def test_release_smokes_exact_artifacts_before_upload() -> None:
    release = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    build_job = release.split("  build:\n", 1)[1].split("\n  publish:\n", 1)[0]

    assert build_job.index("Install the exact distributions") < build_job.index(
        "Stage immutable distributions"
    )
    assert build_job.count("uv pip check --python") == 3
    assert '"$wheel_path"' in build_job
    assert '"$sdist_path"' in build_job
    assert '"${wheel_path}[mcp]"' in build_job
