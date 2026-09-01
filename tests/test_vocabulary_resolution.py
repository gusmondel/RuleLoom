from __future__ import annotations

import json
import os
import subprocess
from importlib.resources import files
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from ruleloom import cli
from ruleloom.config import RuleLoomConfig, default_config
from ruleloom.discovery import DiscoveryLimits, propose_vocabulary
from ruleloom.gitfacts import collect_snapshot, repository_identity
from ruleloom.history_features import enrich_history_features
from ruleloom.models import FactEvidence, LabelValue, ModelError, Observation
from ruleloom.packs import (
    ConfiguredPathsConfig,
    PartnerPredicateConfig,
    PathPredicateConfig,
    get_pack,
)
from ruleloom.packs.base import DiffEvidence, FileChange, PackOptions
from ruleloom.packs.generic_v3 import (
    EXTRACTOR as V3_EXTRACTOR,
)
from ruleloom.packs.generic_v3 import (
    generated_path_marker,
)
from ruleloom.repository_assertions import RepositoryAssertionManifest
from ruleloom.storage import dataset_path, load_observations

OPTIONS = PackOptions(large_change_churn=200, multi_file_count=3, metadata_file_limit=512)


def _git(repo: Path, *arguments: str, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return completed.stdout.strip()


def _commit(repo: Path, paths: dict[str, str], timestamp: str, message: str = "change") -> str:
    for relative, content in paths.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        _git(repo, "add", "--", relative)
    _git(
        repo,
        "commit",
        "-m",
        message,
        env={**os.environ, "GIT_AUTHOR_DATE": timestamp, "GIT_COMMITTER_DATE": timestamp},
    )
    return _git(repo, "rev-parse", "HEAD")


def _pack_config() -> ConfiguredPathsConfig:
    return ConfiguredPathsConfig(
        path_predicates=(
            PathPredicateConfig("touches_hotspot_registry", ("pkg/registry.go",)),
            PathPredicateConfig("touches_owner_area_abc", ("pkg/**",)),
        ),
        partner_predicates=(
            PartnerPredicateConfig(
                "missing_partner_registry_json", "pkg/registry.go", "pkg/registry.json"
            ),
        ),
    )


def test_generic_v3_adds_cumulative_ordinals_generated_hints_and_instantiated_facts() -> None:
    pack = get_pack("generic_changes", 3, _pack_config())
    evidence = DiffEvidence(
        changes=(
            FileChange("pkg/registry.go", 120, 40),
            FileChange("pkg/service/zz_generated.deepcopy.go", 400, 0),
            FileChange("docs/guide.md", 5, 1),
        )
    )

    extraction = pack.run(evidence, OPTIONS)

    assert pack.extractor == V3_EXTRACTOR
    assert {
        "churn_at_least_small",
        "churn_at_least_large",
        "files_at_least_few",
        "files_at_least_many",
        "touches_generated_artifact",
        "touches_hotspot_registry",
        "touches_owner_area_abc",
        "missing_partner_registry_json",
        "churn_band_large",
        "large_change",
        "multi_file_change",
        "touches_docs",
    } <= extraction.facts
    assert "files_at_least_wide" not in extraction.facts
    # 566 lines of churn sit below the 800-line extreme boundary (4x large_change_churn).
    assert "churn_at_least_extreme" not in extraction.facts
    assert "churn_band_extreme" not in extraction.facts
    assert extraction.provenance["missing_partner_registry_json"].evidence == (
        "path:pkg/registry.go;missing:pkg/registry.json",
    )
    assert extraction.provenance["touches_generated_artifact"].evidence == (
        "path:pkg/service/zz_generated.deepcopy.go;prefix:zz_generated",
    )
    assert all(item.extractor == V3_EXTRACTOR for item in extraction.provenance.values())
    assert extraction.metadata["configured_partner_status"] == {
        "missing_partner_registry_json": "violated"
    }
    assert extraction.metadata["configured_path_match_counts"] == {
        "touches_hotspot_registry": 1,
        "touches_owner_area_abc": 2,
    }
    assert extraction.metadata["configured_paths_config_hash"] == _pack_config().hash

    satisfied = pack.run(
        DiffEvidence(
            changes=(FileChange("pkg/registry.go", 1, 1), FileChange("pkg/registry.json", 1, 0))
        ),
        OPTIONS,
    )
    assert "missing_partner_registry_json" not in satisfied.facts
    assert satisfied.metadata["configured_partner_status"] == {
        "missing_partner_registry_json": "satisfied"
    }
    inactive = pack.run(DiffEvidence(changes=(FileChange("README.md", 1, 0),)), OPTIONS)
    assert inactive.metadata["configured_partner_status"] == {
        "missing_partner_registry_json": "inactive"
    }
    # One changed line sits below the 50-line "small" boundary (large_change_churn / 4).
    assert "churn_band_tiny" in inactive.facts
    assert "churn_at_least_small" not in inactive.facts
    assert "files_at_least_few" not in inactive.facts


def test_generated_path_markers_are_bounded_documented_conventions() -> None:
    assert generated_path_marker("api/v1/types.pb.go") == "suffix:.pb.go"
    assert generated_path_marker("web/__generated__/schema.ts") == "directory:__generated__"
    assert generated_path_marker("lib/model.g.dart") == "suffix:.g.dart"
    assert generated_path_marker("src/client.generated.ts") == "infix:.generated."
    assert generated_path_marker("src/generator.py") is None
    assert generated_path_marker("pkg/gen/thing.go") is None


def test_generic_v3_runs_with_an_empty_configuration_and_stable_identity() -> None:
    empty = get_pack("generic_changes", 3)
    explicit = get_pack("generic_changes", 3, ConfiguredPathsConfig())

    assert empty.configuration_hash == explicit.configuration_hash
    assert ConfiguredPathsConfig().to_dict() == {}
    assert ConfiguredPathsConfig().is_empty
    assert "touches_generated_artifact" in empty.predicates
    assert "owner_areas_at_least_3" in empty.predicates
    with pytest.raises(ModelError, match="collide"):
        get_pack(
            "generic_changes",
            3,
            ConfiguredPathsConfig((PathPredicateConfig("touches_docs", ("docs/**",)),)),
        )


def test_configured_paths_v1_keeps_its_contract_and_hash() -> None:
    legacy = ConfiguredPathsConfig((PathPredicateConfig("touches_web", ("apps/web/**",)),))
    assert legacy.to_dict() == {
        "path_predicates": [
            {"predicate": "touches_web", "include_paths": ["apps/web/**"], "exclude_paths": []}
        ]
    }
    assert "partner_predicates" not in legacy.to_dict()
    assert ConfiguredPathsConfig.from_dict(legacy.to_dict()) == legacy
    with pytest.raises(ModelError, match="does not support partner_predicates"):
        get_pack("configured_paths", 1, _pack_config())
    with pytest.raises(ModelError, match="at least one predicate"):
        get_pack("configured_paths", 1, ConfiguredPathsConfig())


@pytest.mark.parametrize(
    "kwargs, message",
    [
        (
            {"predicate": "touches_pair", "path": "a.go", "partner": "b.go"},
            "must start with 'missing_partner_'",
        ),
        (
            {"predicate": "missing_partner_same", "path": "a.go", "partner": "a.go"},
            "must differ",
        ),
        (
            {"predicate": "missing_partner_bad", "path": "/abs.go", "partner": "b.go"},
            "portable root-anchored glob",
        ),
    ],
)
def test_partner_predicates_are_validated(kwargs: dict[str, str], message: str) -> None:
    with pytest.raises(ModelError, match=message):
        PartnerPredicateConfig(**kwargs)


def test_configured_config_rejects_duplicate_names_across_families() -> None:
    with pytest.raises(ModelError, match="duplicate predicates"):
        ConfiguredPathsConfig(
            path_predicates=(PathPredicateConfig("touches_x", ("x/**",)),),
            partner_predicates=(
                PartnerPredicateConfig("missing_partner_x", "x/a", "x/b"),
                PartnerPredicateConfig("missing_partner_x", "x/c", "x/d"),
            ),
        )
    round_trip = ConfiguredPathsConfig.from_dict(_pack_config().to_dict())
    assert round_trip == _pack_config()
    assert round_trip.predicates == (
        "missing_partner_registry_json",
        "touches_hotspot_registry",
        "touches_owner_area_abc",
    )


def _v3_observation(index: int, day: int, paths: tuple[str, ...], base: str) -> Observation:
    from datetime import UTC, datetime, timedelta

    return Observation(
        id=f"commit.{index}",
        observed_at=(datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=day)).isoformat(),
        protocol_hash="a" * 64,
        facts=frozenset({"churn_band_tiny"}),
        labels={"post_merge_defect": LabelValue.UNKNOWN},
        fact_evidence={
            "churn_band_tiny": FactEvidence(
                kind="deterministic", extractor=V3_EXTRACTOR, evidence=("synthetic",)
            )
        },
        source={
            "kind": "git_commit",
            "repository": "repository.example",
            "pack": "generic_changes",
            "pack_version": 3,
            "extractor": V3_EXTRACTOR,
            "base": base,
        },
        metadata={
            "topological_index": index,
            "files_changed": len(paths),
            "changed_files": list(paths),
            "metadata_files_truncated": 0,
        },
    )


def test_enrichment_counts_owner_areas_and_reads_linguist_generated_attributes(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "owners"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Owners")
    _git(repo, "config", "user.email", "owners@example.invalid")
    base = _commit(
        repo,
        {
            ".github/CODEOWNERS": "pkg/api/ @team-api\npkg/db/ @team-db\nweb/ @team-web\n",
            ".gitattributes": "web/dist/** linguist-generated=true\n",
            "README.md": "# Owners\n",
        },
        "2024-01-01T00:00:00Z",
    )
    three_areas = _v3_observation(
        1, 1, ("pkg/api/handler.go", "pkg/db/schema.sql", "web/app.ts"), base
    )
    generated = _v3_observation(2, 2, ("web/dist/bundle.js",), base)
    single = _v3_observation(3, 3, ("pkg/api/handler.go",), base)

    enriched = enrich_history_features(
        [],
        [three_areas, generated, single],
        extractor=V3_EXTRACTOR,
        root=repo,
        pack_version=3,
    )
    by_id = {item.id: item for item in enriched}

    assert {"crosses_codeowners_boundary", "owner_areas_at_least_2", "owner_areas_at_least_3"} <= (
        by_id["commit.1"].facts
    )
    assert by_id["commit.1"].metadata["historical_context"]["codeowners"]["owner_boundaries"] == 3
    assert "touches_generated_artifact" in by_id["commit.2"].facts
    assert by_id["commit.2"].fact_evidence["touches_generated_artifact"].evidence == (
        "path:web/dist/bundle.js;gitattributes:linguist-generated",
    )
    assert by_id["commit.2"].metadata["historical_context"]["gitattributes"]["status"] == (
        "available"
    )
    assert not {"owner_areas_at_least_2", "touches_generated_artifact"} & by_id["commit.3"].facts

    legacy = enrich_history_features(
        [], [three_areas], extractor="ruleloom.generic_changes.git.v2", root=repo, pack_version=2
    )[0]
    assert "owner_areas_at_least_2" not in legacy.facts
    assert "gitattributes" not in legacy.metadata["historical_context"]


@pytest.fixture
def structured_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "structured"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Structure")
    _git(repo, "config", "user.email", "structure@example.invalid")
    _commit(
        repo,
        {
            ".github/CODEOWNERS": "pkg/ @team-core\nweb/ @team-web\n",
            "pkg/registry.go": "package pkg\n",
            "pkg/registry.json": "{}\n",
            "web/locales/en.json": "{}\n",
        },
        "2025-01-01T00:00:00Z",
    )
    day = 2
    for index in range(6):
        _commit(
            repo,
            {
                "pkg/registry.go": f"package pkg // {index}\n",
                "pkg/registry.json": f'{{"n": {index}}}\n',
                "web/locales/en.json": f'{{"n": {index}}}\n',
            },
            f"2025-01-{day:02d}T00:00:00Z",
        )
        day += 1
    for index in range(3):
        _commit(
            repo,
            {"web/locales/en.json": f'{{"m": {index}}}\n'},
            f"2025-01-{day:02d}T00:00:00Z",
        )
        day += 1
    _commit(repo, {"web/app.ts": "export {};\n"}, "2025-02-01T00:00:00Z")
    _commit(repo, {"pkg/other.go": "package pkg\n"}, "2025-03-01T00:00:00Z")
    return repo


def test_proposer_instantiates_hotspots_owner_areas_pairs_and_assertions(
    structured_repo: Path,
) -> None:
    limits = DiscoveryLimits(min_pair_support=5, min_pair_confidence=0.7, min_hotspot_changes=3)

    proposal = propose_vocabulary(structured_repo, until="2025-02-15T00:00:00Z", limits=limits)
    again = propose_vocabulary(structured_repo, until="2025-02-15T00:00:00Z", limits=limits)

    assert proposal.manifest_hash == again.manifest_hash
    assert proposal.commit_count == 11
    assert proposal.excluded_after_until == 1
    hotspots = {row["path"]: row["change_count"] for row in proposal.hotspots}  # type: ignore[index]
    assert hotspots["web/locales/en.json"] == 10
    assert hotspots["pkg/registry.go"] == 7
    assert len(proposal.owner_areas) == 2
    pairs = {(row["path"], row["partner"]): row for row in proposal.pairs}  # type: ignore[index]
    registry = pairs[("pkg/registry.go", "pkg/registry.json")]
    assert registry["support"] == 7
    assert registry["confidence"] == 1.0
    assert str(registry["predicate"]).startswith("missing_partner_registry_go_")
    assert registry["assertion_id"] is not None
    assert proposal.assertion_manifest is not None
    manifest = RepositoryAssertionManifest.from_dict(proposal.assertion_manifest.to_dict())
    drafted = next(
        item for item in manifest.assertions if item.assertion_id == registry["assertion_id"]
    )
    assert drafted.sources[0].path == "pkg/registry.go"
    assert drafted.antecedent[0].predicate in proposal.pack_config.predicates
    assert drafted.expectation[0].predicate in proposal.pack_config.predicates
    config = ConfiguredPathsConfig.from_dict(proposal.pack_config.to_dict())
    assert config == proposal.pack_config
    assert any(item.predicate.startswith("touches_owner_area_") for item in config.path_predicates)
    assert all("@team" not in json.dumps(proposal.to_dict()) for _ in [0])
    assert proposal.to_dict()["outcome_blind"] is True
    assert proposal.to_dict()["draft"] is True

    unbounded = propose_vocabulary(structured_repo, limits=limits)
    assert unbounded.commit_count == 12
    assert any("no holdout boundary" in warning for warning in unbounded.warnings)
    assert "Missing-partner predicates" in unbounded.render_text()


def test_cli_proposes_initializes_collects_and_declares_the_reviewed_vocabulary(
    structured_repo: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pack_config_file = tmp_path / "proposed-pack-config.json"
    assertions_file = tmp_path / "proposed-assertions.json"

    assert (
        cli.main(
            [
                "predicates",
                "propose",
                str(structured_repo),
                "--until",
                "2025-02-15T00:00:00Z",
                "--json",
                "--pack-config-output",
                str(pack_config_file),
                "--assertions-output",
                str(assertions_file),
            ]
        )
        == 0
    )
    proposal = json.loads(capsys.readouterr().out)
    assert proposal["pack"] == {"name": "generic_changes", "version": 3}
    assert pack_config_file.is_file()
    assert assertions_file.is_file()
    assert (
        cli.main(
            [
                "predicates",
                "propose",
                str(structured_repo),
                "--pack-config-output",
                str(pack_config_file),
            ]
        )
        == 2
    )
    assert "refusing to overwrite" in capsys.readouterr().err

    assert (
        cli.main(
            [
                "init",
                str(structured_repo),
                "--pack",
                "generic_changes",
                "--pack-config",
                str(pack_config_file),
            ]
        )
        == 0
    )
    capsys.readouterr()
    config = RuleLoomConfig.load(structured_repo)
    assert (config.pack, config.pack_version) == ("generic_changes", 3)
    assert config.pack_config is not None
    assert config.pack_config.partner_predicates
    partner = config.pack_config.partner_predicates[0].predicate
    assert partner in config.resolved_pack.predicates

    assert cli.main(["collect", "--root", str(structured_repo), "git", "--last", "12"]) == 0
    capsys.readouterr()
    observations = load_observations(dataset_path(structured_repo, config))
    assert observations
    assert any(partner in item.facts for item in observations)
    assert any("touches_hotspot" in fact for item in observations for fact in item.facts)
    assert all(item.source["pack_config_hash"] == config.pack_config_hash for item in observations)

    assert (
        cli.main(["assertions", "--root", str(structured_repo), "declare", str(assertions_file)])
        == 0
    )
    capsys.readouterr()
    assert cli.main(["assertions", "--root", str(structured_repo), "audit", "--json"]) == 0
    audit = json.loads(capsys.readouterr().out)
    assert audit["rows"]

    assert cli.main(["predicates", "--root", str(structured_repo), "audit"]) == 0
    predicate_audit = json.loads(capsys.readouterr().out)
    configured_rows = [row for row in predicate_audit["predicates"] if row["configured"]]
    assert any(row["predicate"] == partner for row in configured_rows)


def test_snapshot_collection_records_v3_configuration_provenance(structured_repo: Path) -> None:
    repository_id = repository_identity(structured_repo)
    config = default_config(
        "Structure",
        repository_id=repository_id,
        schema_version=5,
        test_start_at="2026-01-01T00:00:00Z",
        pack_config=_pack_config(),
    )
    head = _git(structured_repo, "rev-parse", "HEAD")
    base = _git(structured_repo, "rev-parse", "HEAD~1")

    observation = collect_snapshot(
        structured_repo,
        base,
        head,
        protocol_hash=config.evidence_protocol_hash,
        target=config.target,
        pack=config.pack,
        pack_version=config.pack_version,
        pack_config=config.pack_config,
        evidence_config=config.evidence,
        repository_id=repository_id,
    )

    assert observation.source["pack_version"] == 3
    assert observation.source["pack_config_hash"] == _pack_config().hash
    assert observation.source["extractor"] == V3_EXTRACTOR
    assert "touches_owner_area_abc" in observation.facts


def _validate_config(payload: dict[str, object]) -> None:
    resource = files("ruleloom").joinpath("schemas", "config.schema.json")
    schema = json.loads(resource.read_text(encoding="utf-8"))
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(payload)


def test_config_schema_v5_binds_generic_v3_pack_configuration() -> None:
    configured = default_config(
        "Structure",
        schema_version=5,
        test_start_at="2026-01-01T00:00:00Z",
        pack_config=_pack_config(),
    )
    empty = default_config("Structure", schema_version=5, test_start_at="2026-01-01T00:00:00Z")

    assert (configured.pack, configured.pack_version) == ("generic_changes", 3)
    assert configured.to_dict()["pack_config"] == _pack_config().to_dict()
    assert empty.to_dict()["pack_config"] == {}
    assert empty.pack_config is not None and empty.pack_config.is_empty
    assert configured.evidence_protocol_hash != empty.evidence_protocol_hash
    assert RuleLoomConfig.from_dict(configured.to_dict()) == configured
    assert RuleLoomConfig.from_dict(empty.to_dict()) == empty
    _validate_config(configured.to_dict())
    _validate_config(empty.to_dict())

    legacy = default_config("Structure", schema_version=4, test_start_at="2026-01-01T00:00:00Z")
    assert (legacy.pack, legacy.pack_version) == ("generic_changes", 2)
    assert legacy.pack_config is None
    with pytest.raises(ModelError, match="schema_version 5"):
        RuleLoomConfig(
            schema_version=4,
            project="Structure",
            pack="generic_changes",
            pack_version=3,
        )
    payload = configured.to_dict()
    payload["pack"] = "configured_paths"
    payload["pack_version"] = 1
    with pytest.raises(ValidationError):
        _validate_config(payload)
    with pytest.raises(ValidationError):
        stale = legacy.to_dict()
        stale["pack_version"] = 3
        _validate_config(stale)
