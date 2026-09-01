from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

from jsonschema import Draft202012Validator

from ruleloom.history.github_webhooks import parse_github_label_policy
from ruleloom.repository_assertions import load_repository_assertion_manifest

ROOT = Path(__file__).parents[1]


def _schema(name: str) -> dict[str, object]:
    resource = files("ruleloom").joinpath("schemas", f"{name}.schema.json")
    value = json.loads(resource.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    Draft202012Validator.check_schema(value)
    return value


def test_repository_assertion_example_matches_public_schema_and_model() -> None:
    path = ROOT / "examples" / "repository-assertions.json"
    payload = json.loads(path.read_text(encoding="utf-8"))

    Draft202012Validator(_schema("repository-assertion-manifest")).validate(payload)
    assert load_repository_assertion_manifest(path).assertions[0].assertion_id == (
        "docs_expect_tests"
    )


def test_github_label_policy_example_matches_public_schema_and_model() -> None:
    content = (ROOT / "examples" / "github-label-policy.json").read_text(encoding="utf-8")
    payload = json.loads(content)

    Draft202012Validator(_schema("github-label-policy")).validate(payload)
    assert parse_github_label_policy(content)[0].target == "validation_rework_required"
