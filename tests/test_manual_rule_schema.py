from __future__ import annotations

import copy
import json
from collections.abc import Callable
from importlib.resources import files

import pytest
from jsonschema import Draft202012Validator, ValidationError

from ruleloom.manual_rules import ManualRuleManifest
from ruleloom.models import ModelError


def _manifest() -> dict[str, object]:
    return {
        "schema_version": 1,
        "policy_id": "ci_without_tests",
        "revision": 1,
        "claim_kind": "risk_trigger",
        "summary": "CI changes without tests may require additional validation.",
        "rules": {
            "target": "validation_rework_required",
            "clauses": [
                {
                    "target": "validation_rework_required",
                    "body": [
                        {"predicate": "touches_ci", "negated": False},
                        {"predicate": "touches_test", "negated": True},
                    ],
                }
            ],
        },
        "sources": [
            {"path": "AGENTS.md", "start_line": 10, "end_line": 12},
            {"path": "docs/quality-policy.md"},
        ],
    }


def _schema() -> dict[str, object]:
    resource = files("ruleloom").joinpath("schemas", "manual-rule-manifest.schema.json")
    value = json.loads(resource.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    Draft202012Validator.check_schema(value)
    return value


def _validate(payload: object) -> None:
    Draft202012Validator(_schema()).validate(payload)


def _without(field: str) -> Callable[[dict[str, object]], None]:
    def mutate(payload: dict[str, object]) -> None:
        del payload[field]

    return mutate


def _set(field: str, value: object) -> Callable[[dict[str, object]], None]:
    def mutate(payload: dict[str, object]) -> None:
        payload[field] = value

    return mutate


def test_manual_rule_manifest_schema_accepts_full_and_loader_default_shapes() -> None:
    full = _manifest()
    minimal = copy.deepcopy(full)
    del minimal["sources"]
    literal = minimal["rules"]["clauses"][0]["body"][0]  # type: ignore[index]
    del literal["negated"]

    _validate(full)
    _validate(minimal)

    parsed_full = ManualRuleManifest.from_dict(full)
    parsed_minimal = ManualRuleManifest.from_dict(minimal)
    assert parsed_full.to_dict() == full
    assert parsed_minimal.sources == ()
    assert parsed_minimal.rules.clauses[0].body[0].negated is False


@pytest.mark.parametrize(
    "mutate",
    [
        _without("schema_version"),
        _without("policy_id"),
        _without("revision"),
        _without("claim_kind"),
        _without("summary"),
        _without("rules"),
        _set("schema_version", 2),
        _set("policy_id", "CI Without Tests"),
        _set("revision", 0),
        _set("revision", True),
        _set("claim_kind", "required_action"),
        _set("summary", "   "),
        _set("summary", "line one\nline two"),
        _set("summary", "x" * 501),
        _set("unexpected", True),
    ],
    ids=[
        "missing-schema-version",
        "missing-policy-id",
        "missing-revision",
        "missing-claim-kind",
        "missing-summary",
        "missing-rules",
        "unsupported-schema-version",
        "invalid-policy-id",
        "zero-revision",
        "boolean-revision",
        "unsupported-claim-kind",
        "blank-summary",
        "multiline-summary",
        "long-summary",
        "unknown-field",
    ],
)
def test_manual_rule_manifest_schema_and_loader_reject_invalid_top_level_contract(
    mutate: Callable[[dict[str, object]], None],
) -> None:
    payload = _manifest()
    mutate(payload)

    with pytest.raises(ValidationError):
        _validate(payload)
    with pytest.raises(ModelError):
        ManualRuleManifest.from_dict(payload)


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/AGENTS.md",
        "../AGENTS.md",
        "docs/../AGENTS.md",
        "docs//AGENTS.md",
        "./AGENTS.md",
        "docs/AGENTS.md/",
        ".git",
        ".git/config",
        ".ruleloom/generated.md",
        ".agents/skills/ruleloom/SKILL.md",
        ".claude/skills/ruleloom/SKILL.md",
        "docs\\AGENTS.md",
        "docs/line\nbreak.md",
        f"docs/{'x' * 508}.md",
    ],
)
def test_manual_rule_source_paths_match_schema_and_loader_rejections(path: str) -> None:
    payload = _manifest()
    payload["sources"] = [{"path": path}]

    with pytest.raises(ValidationError):
        _validate(payload)
    with pytest.raises(ModelError):
        ManualRuleManifest.from_dict(payload)


@pytest.mark.parametrize(
    "source",
    [
        {"path": "AGENTS.md", "unexpected": True},
        {"path": "AGENTS.md", "start_line": 1},
        {"path": "AGENTS.md", "end_line": 1},
        {"path": "AGENTS.md", "start_line": 0, "end_line": 1},
        {"path": "AGENTS.md", "start_line": 1, "end_line": False},
    ],
)
def test_manual_rule_source_shape_matches_schema_and_loader_rejections(
    source: dict[str, object],
) -> None:
    payload = _manifest()
    payload["sources"] = [source]

    with pytest.raises(ValidationError):
        _validate(payload)
    with pytest.raises(ModelError):
        ManualRuleManifest.from_dict(payload)


def test_manual_rule_schema_enforces_bounded_rule_and_source_collections() -> None:
    too_many_clauses = _manifest()
    too_many_clauses["rules"]["clauses"] = [  # type: ignore[index]
        {
            "target": "validation_rework_required",
            "body": [{"predicate": f"predicate_{index}"}],
        }
        for index in range(11)
    ]

    too_many_literals = _manifest()
    too_many_literals["rules"]["clauses"][0]["body"] = [  # type: ignore[index]
        {"predicate": f"predicate_{index}"} for index in range(5)
    ]

    too_many_sources = _manifest()
    too_many_sources["sources"] = [{"path": f"docs/policy-{index}.md"} for index in range(17)]

    for payload in (too_many_clauses, too_many_literals, too_many_sources):
        with pytest.raises(ValidationError):
            _validate(payload)
        with pytest.raises(ModelError):
            ManualRuleManifest.from_dict(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["rules"].update({"unexpected": True}),  # type: ignore[union-attr]
        lambda payload: payload["rules"].update({"clauses": []}),  # type: ignore[union-attr]
        lambda payload: payload["rules"]["clauses"][0].update(  # type: ignore[index]
            {"unexpected": True}
        ),
        lambda payload: payload["rules"]["clauses"][0].update(  # type: ignore[index]
            {"body": []}
        ),
        lambda payload: payload["rules"]["clauses"][0]["body"][0].update(  # type: ignore[index]
            {"unexpected": True}
        ),
        lambda payload: payload["rules"]["clauses"][0]["body"][0].update(  # type: ignore[index]
            {"predicate": "UPPER_CASE"}
        ),
        lambda payload: payload["rules"]["clauses"][0]["body"][0].update(  # type: ignore[index]
            {"negated": "false"}
        ),
    ],
    ids=[
        "unknown-rules-field",
        "empty-clauses",
        "unknown-clause-field",
        "empty-body",
        "unknown-literal-field",
        "invalid-predicate",
        "non-boolean-negation",
    ],
)
def test_manual_rule_nested_shapes_match_schema_and_loader_rejections(
    mutate: Callable[[dict[str, object]], None],
) -> None:
    payload = _manifest()
    mutate(payload)

    with pytest.raises(ValidationError):
        _validate(payload)
    with pytest.raises(ModelError):
        ManualRuleManifest.from_dict(payload)


@pytest.mark.parametrize(
    "source",
    [
        {"path": "AGENTS.md", "start_line": 2, "end_line": 1},
        {"path": "AGENTS.md", "start_line": 1, "end_line": 501},
    ],
)
def test_loader_enforces_source_range_relations_not_expressible_portably_in_schema(
    source: dict[str, object],
) -> None:
    payload = _manifest()
    payload["sources"] = [source]

    _validate(payload)
    with pytest.raises(ModelError, match="source line range"):
        ManualRuleManifest.from_dict(payload)
