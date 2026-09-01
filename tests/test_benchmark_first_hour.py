from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ruleloom import __version__
from ruleloom.first_hour import FIRST_HOUR_REPORT_ENGINE_VERSION

_SCRIPT = Path(__file__).parents[1] / "scripts" / "benchmark_first_hour.py"


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(
        ("git", "-C", str(repo), *arguments),
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "RuleLoom Benchmark Test",
            "GIT_AUTHOR_EMAIL": "benchmark@example.invalid",
            "GIT_COMMITTER_NAME": "RuleLoom Benchmark Test",
            "GIT_COMMITTER_EMAIL": "benchmark@example.invalid",
        },
    )


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    for index in range(3):
        (repo / f"file-{index}.txt").write_text(f"revision {index}\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", f"revision {index}")
    return repo


@pytest.mark.parametrize(
    "arguments",
    (
        ("--batch-size", "1", "--repeats", "2"),
        ("--batch-size", "513", "--repeats", "2"),
        ("--batch-size", "2", "--repeats", "1"),
        ("--batch-size", "2", "--repeats", "21"),
        ("--max-commits", "10001", "--batch-size", "2", "--repeats", "2"),
    ),
)
def test_benchmark_rejects_ambiguous_or_unbounded_arguments(
    tmp_path: Path,
    arguments: tuple[str, ...],
) -> None:
    completed = subprocess.run(
        (sys.executable, str(_SCRIPT), str(tmp_path), *arguments),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "error:" in completed.stderr


def test_benchmark_reports_versions_and_full_structural_equivalence(tmp_path: Path) -> None:
    repo = _repository(tmp_path)

    completed = subprocess.run(
        (
            sys.executable,
            str(_SCRIPT),
            str(repo),
            "--max-commits",
            "3",
            "--batch-size",
            "2",
            "--repeats",
            "2",
        ),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["environment"]["ruleloom"] == __version__
    assert payload["environment"]["first_hour_engine"] == FIRST_HOUR_REPORT_ENGINE_VERSION
    assert payload["baseline"]["diff_batch_size"] == 1
    assert len(payload["baseline"]["seconds"]) == 2
    assert payload["batched"]["diff_batch_size"] == 2
    assert len(payload["batched"]["seconds"]) == 2
    assert payload["equivalence"] == {
        "evidence_manifest_hash": payload["equivalence"]["evidence_manifest_hash"],
        "evidence_manifest_hash_equal": True,
        "normalized_structural_report_equal": True,
        "volume_equal": True,
    }
