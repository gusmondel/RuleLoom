from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from typing import Literal, cast

import pytest
from mcp import Client, StdioServerParameters

from ruleloom.config import RuleLoomConfig
from ruleloom.mcp_sdk import _load_official_sdk, _safe_domain_call
from ruleloom.models import (
    Candidate,
    HornClause,
    Metrics,
    ModelError,
    RuleLiteral,
    RuleSet,
    content_hash,
)
from ruleloom.project import initialize_project
from ruleloom.storage import (
    load_predictions,
    predictions_path,
    record_transition_attestation,
    save_candidate,
    shadow_path,
)


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, RuleLoomConfig]:
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "RuleLoom MCP SDK Tests")
    _git(root, "config", "user.email", "mcp-sdk@example.invalid")
    (root / "service.txt").write_text("initial\n", encoding="utf-8")
    _git(root, "add", "service.txt")
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "-m", "initial"],
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_DATE": "2026-01-01T10:00:00Z",
            "GIT_COMMITTER_DATE": "2026-01-01T10:00:00Z",
        },
    )
    config = initialize_project(root, project="MCP SDK Test").config
    (root / "service.txt").write_text(
        "".join(f"changed line {index}\n" for index in range(300)),
        encoding="utf-8",
    )
    return root, config


def _server_parameters(root: Path) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "ruleloom.cli", "mcp", "serve", "--root", str(root)],
        cwd=Path.cwd(),
        env=dict(os.environ),
    )


async def _exercise_protocol(
    root: Path,
    mode: Literal["2026-07-28", "legacy"],
) -> tuple[str, dict[str, object]]:
    async with Client(
        _server_parameters(root),
        mode=mode,
        read_timeout_seconds=10,
    ) as client:
        listed = await client.list_tools()
        assert [tool.name for tool in listed.tools] == [
            "assess_change",
            "get_guidance",
            "explain_evidence",
        ]
        assessed = await client.call_tool(
            "assess_change",
            {"change_id": f"{mode}-change", "request_id": f"{mode}-request"},
        )
        assert assessed.is_error is False
        payload = cast(dict[str, object], assessed.structured_content)
        replayed = await client.call_tool(
            "assess_change",
            {"change_id": f"{mode}-change", "request_id": f"{mode}-request"},
        )
        replay_payload = cast(dict[str, object], replayed.structured_content)
        assert replay_payload["prediction_id"] == payload["prediction_id"]
        assert replay_payload["replayed"] is True
        guidance = await client.call_tool(
            "get_guidance", {"prediction_id": payload["prediction_id"]}
        )
        assert guidance.is_error is False
        return str(client.protocol_version), cast(dict[str, object], guidance.structured_content)


@pytest.mark.parametrize(
    ("mode", "expected_version"),
    [("2026-07-28", "2026-07-28"), ("legacy", "2025-11-25")],
)
def test_official_sdk_entrypoint_serves_modern_and_legacy_clients(
    tmp_path: Path,
    mode: Literal["2026-07-28", "legacy"],
    expected_version: str,
) -> None:
    root, _config = _repository(tmp_path)

    protocol, guidance = asyncio.run(_exercise_protocol(root, mode))

    assert protocol == expected_version
    assert guidance["recommendation"] == "no_approved_rule_matched"


def _shadow_candidate(config: RuleLoomConfig) -> Candidate:
    metric = Metrics.from_counts(4, 1, 4, 1)
    baseline = Metrics.from_counts(0, 0, 5, 5)
    descriptor = config.resolved_pack
    return Candidate(
        id="candidate.pending",
        created_at="2026-08-31T12:00:00Z",
        engine="horn",
        engine_version="mcp-sdk-shadow-secret",
        dataset_hash=content_hash([]),
        config_hash=config.hash,
        rules=RuleSet(
            config.target,
            (HornClause(config.target, (RuleLiteral("large_change"),)),),
        ),
        metrics={"train": metric, "test": metric},
        baselines={
            "never_alert": baseline,
            "always_alert": baseline,
            "train_majority": baseline,
            "best_single_literal": baseline,
        },
        stability=0.8,
        train_ids=("train-1", "train-2"),
        test_ids=("test-1",),
        metadata={
            "pack": config.pack,
            "pack_version": config.pack_version,
            "pack_config_hash": config.pack_config_hash,
            "repository_id": config.protocol.repository_id,
            "evidence_protocol_hash": config.evidence_protocol_hash,
            "extractors": [descriptor.extractor],
        },
        review={
            "reviewer": "MCP SDK test reviewer",
            "reviewed_at": "2026-08-31T13:00:00Z",
            "note": "shadow-secret",
            "override": False,
            "unmet_gates": [],
        },
        status="shadow",
    ).with_identity()


async def _shadow_responses(root: Path) -> list[object]:
    async with Client(
        _server_parameters(root),
        mode="2026-07-28",
        read_timeout_seconds=10,
    ) as client:
        assessed = await client.call_tool(
            "assess_change",
            {"change_id": "shadow-change", "request_id": "shadow-request"},
        )
        payload = cast(dict[str, object], assessed.structured_content)
        prediction_id = cast(str, payload["prediction_id"])
        guidance = await client.call_tool("get_guidance", {"prediction_id": prediction_id})
        explanation = await client.call_tool("explain_evidence", {"prediction_id": prediction_id})
        return [payload, guidance.structured_content, explanation.structured_content]


def test_official_sdk_boundary_never_discloses_shadow_policy(tmp_path: Path) -> None:
    root, config = _repository(tmp_path)
    shadow = _shadow_candidate(config)
    save_candidate(shadow_path(root, config, shadow.id), shadow)
    record_transition_attestation(root, shadow)

    responses = asyncio.run(_shadow_responses(root))

    prediction = load_predictions(predictions_path(root, config))[0]
    assert prediction.matches[0]["candidate_id"] == shadow.id
    rendered = json.dumps(responses, sort_keys=True)
    assert shadow.id not in rendered
    assert "shadow-secret" not in rendered
    assert cast(dict[str, object], responses[1])["recommendation"] == "no_approved_rule_matched"
    assert cast(dict[str, object], responses[2])["approved_matches"] == []


def test_official_sdk_stdio_emits_only_json_rpc_on_stdout(tmp_path: Path) -> None:
    root, _config = _repository(tmp_path)
    request = {
        "jsonrpc": "2.0",
        "id": "tools",
        "method": "tools/list",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientInfo": {"name": "wire-test", "version": "1"},
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        },
    }
    completed = subprocess.run(
        [_server_parameters(root).command, *_server_parameters(root).args],
        input=json.dumps(request) + "\n",
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
        cwd=Path.cwd(),
        env=dict(os.environ),
    )

    lines = [line for line in completed.stdout.splitlines() if line]
    assert len(lines) == 1
    response = json.loads(lines[0])
    assert response["id"] == "tools"
    assert [tool["name"] for tool in response["result"]["tools"]] == [
        "assess_change",
        "get_guidance",
        "explain_evidence",
    ]


def test_missing_optional_sdk_has_an_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(_distribution: str) -> str:
        raise PackageNotFoundError("mcp")

    monkeypatch.setattr("ruleloom.mcp_sdk.version", missing)

    with pytest.raises(ModelError, match=r"optional 'mcp' extra"):
        _load_official_sdk()


def test_sdk_boundary_rejects_oversized_domain_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _config = _repository(tmp_path)
    from ruleloom.mcp_server import RuleLoomMCPServer

    core = RuleLoomMCPServer(root)
    monkeypatch.setattr(
        core,
        "call_tool",
        lambda _operation, _arguments: {"fact_evidence": "x" * (256 * 1024)},
    )

    with pytest.raises(ValueError, match="safe size limit"):
        _safe_domain_call(core, "explain_evidence", {"prediction_id": "prediction.test"})
