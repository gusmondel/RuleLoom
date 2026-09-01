from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_exporter_rejects_existing_and_broken_output_symlinks(tmp_path: Path) -> None:
    script = Path(__file__).parents[1] / "scripts" / "export_gharchive_clickhouse.py"
    target = tmp_path / "target.jsonl"
    target.write_text("do not overwrite\n", encoding="utf-8")
    existing_link = tmp_path / "events.jsonl"
    existing_link.symlink_to(target)
    broken_link = tmp_path / "manifest.json"
    broken_link.symlink_to(tmp_path / "missing.json")

    for link in (existing_link, broken_link):
        result = subprocess.run(
            [
                sys.executable,
                str(script),
                "acme/widgets",
                "--provider-repository-id",
                "123",
                "--since",
                "2025-01-01T00:00:00Z",
                "--until",
                "2025-01-02T00:00:00Z",
                "--preregistration-sha256",
                "a" * 64,
                "--events",
                str(link),
                "--manifest",
                str(tmp_path / "output-manifest.json"),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 2
        assert "output cannot be a symlink" in result.stderr
    assert target.read_text(encoding="utf-8") == "do not overwrite\n"
