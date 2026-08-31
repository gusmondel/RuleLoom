from __future__ import annotations

import json
from fnmatch import fnmatchcase
from itertools import product

import pytest

import ruleloom.packs.configured_paths as configured_paths_module
from ruleloom.config import EvidenceConfig, RuleLoomConfig
from ruleloom.models import FactEvidence, ModelError
from ruleloom.packs import (
    ConfiguredPathsConfig,
    DiffEvidence,
    EvidencePack,
    FileChange,
    PackExtraction,
    PathPredicateConfig,
    available_packs,
    get_pack,
    validate_persisted_extraction,
)


def test_registry_resolves_exact_pack_versions() -> None:
    assert get_pack("flutter_testing", 1).extractor == "ruleloom.flutter_testing.git.v1"
    assert get_pack("flutter_testing", 2).extractor == "ruleloom.flutter_testing.git.v2"
    assert get_pack("generic_changes", 1).content_path("service.py") is False
    with pytest.raises(ModelError, match="available packs"):
        get_pack("python_testing", 1)
    for invalid in (True, 1.0, 0):
        with pytest.raises(ModelError, match="integer >= 1"):
            get_pack("generic_changes", invalid)  # type: ignore[arg-type]


def _configured_paths(*predicates: PathPredicateConfig) -> ConfiguredPathsConfig:
    return ConfiguredPathsConfig(tuple(predicates))


def test_configured_pack_requires_resolution_and_declares_dynamic_vocabulary() -> None:
    template = next(item for item in available_packs() if item.name == "configured_paths")
    assert template.configurable is True
    with pytest.raises(ValueError, match="must be resolved"):
        template.run(DiffEvidence(changes=()), EvidenceConfig().pack_options)
    with pytest.raises(ModelError, match="requires a valid pack_config"):
        get_pack("configured_paths", 1)

    config = _configured_paths(
        PathPredicateConfig("touches_surface_web", ("apps/web/**",)),
        PathPredicateConfig("touches_shared_contract", ("packages/contracts/**",)),
    )
    pack = get_pack("configured_paths", 1, config)

    assert pack.configuration_hash == config.hash
    assert {
        "large_change",
        "multi_file_change",
        "touches_ci",
        "touches_dependencies",
        "touches_docs",
        "touches_test",
        "touches_surface_web",
        "touches_shared_contract",
    } == set(pack.predicates)
    with pytest.raises(ModelError, match="does not accept pack_config"):
        get_pack("generic_changes", 1, config)


def test_persisted_extraction_validator_preserves_static_pack_api_compatibility() -> None:
    static_pack = get_pack("generic_changes", 1)
    result = static_pack.run(
        DiffEvidence(changes=(FileChange("README.md", additions=1, deletions=0),)),
        EvidenceConfig().pack_options,
    )

    validate_persisted_extraction(
        static_pack,
        result.facts,
        result.provenance,
        subject="legacy static observation",
    )

    config = _configured_paths(PathPredicateConfig("touches_docs_area", ("docs/**",)))
    configured_pack = get_pack("configured_paths", 1, config)
    with pytest.raises(ModelError, match="inconsistent configured-path metadata"):
        validate_persisted_extraction(
            configured_pack,
            frozenset(),
            {},
            subject="configured observation without metadata",
        )


def test_configured_paths_matches_rooted_globs_exclusions_overlaps_and_internals() -> None:
    config = _configured_paths(
        PathPredicateConfig(
            "touches_surface_web",
            ("apps/web/**",),
            ("apps/web/generated/**",),
        ),
        PathPredicateConfig(
            "touches_shared_contract",
            ("**/contracts/?pi.*", "packages/contracts/**"),
        ),
        PathPredicateConfig("touches_direct_host", ("apps/*/main.*",)),
        PathPredicateConfig("touches_any_source", ("**/src/**",)),
        PathPredicateConfig("touches_internal_attempt", ("**",)),
    )
    evidence = DiffEvidence(
        changes=(
            FileChange("apps/web/main.ts", additions=1, deletions=0),
            FileChange("apps/web/src/page.ts", additions=1, deletions=0),
            FileChange("apps/web/generated/client.ts", additions=1, deletions=0),
            FileChange("packages/contracts/api.json", additions=1, deletions=0),
            FileChange(".ruleloom/config.json", additions=100, deletions=0),
            FileChange(".agents/skills/ruleloom/SKILL.md", additions=100, deletions=0),
            FileChange(".claude/skills/ruleloom/SKILL.md", additions=100, deletions=0),
        )
    )

    result = get_pack("configured_paths", 1, config).run(
        evidence,
        EvidenceConfig(large_change_churn=1_000, multi_file_count=100).pack_options,
    )

    assert {
        "touches_surface_web",
        "touches_shared_contract",
        "touches_direct_host",
        "touches_any_source",
        "touches_internal_attempt",
    } == result.facts
    assert result.provenance["touches_surface_web"].evidence == (
        "path:apps/web/main.ts",
        "path:apps/web/src/page.ts",
    )
    assert result.provenance["touches_internal_attempt"].evidence == (
        "path:apps/web/generated/client.ts",
        "path:apps/web/main.ts",
        "path:apps/web/src/page.ts",
        "path:packages/contracts/api.json",
    )
    assert result.metadata["configured_unmatched_files"] == 0
    assert result.metadata["configured_overlapping_files"] == 3
    assert result.metadata["excluded_internal_files"] == 3
    counts = result.metadata["configured_path_match_counts"]
    assert isinstance(counts, dict)
    assert counts["touches_surface_web"] == 2
    assert counts["touches_shared_contract"] == 1
    assert counts["touches_direct_host"] == 1
    assert counts["touches_any_source"] == 1
    assert counts["touches_internal_attempt"] == 4


def test_configured_path_manifest_and_hash_are_order_invariant() -> None:
    first_config = _configured_paths(
        PathPredicateConfig("touches_beta", ("b/**", "shared/**"), ("b/generated/**",)),
        PathPredicateConfig("touches_alpha", ("a/**",)),
    )
    second_config = _configured_paths(
        PathPredicateConfig("touches_alpha", ("a/**",)),
        PathPredicateConfig("touches_beta", ("shared/**", "b/**"), ("b/generated/**",)),
    )
    first_evidence = DiffEvidence(
        changes=(
            FileChange("b/main.go", additions=1, deletions=0),
            FileChange("a/main.py", additions=2, deletions=0),
        )
    )
    second_evidence = DiffEvidence(changes=tuple(reversed(first_evidence.changes)))

    first = get_pack("configured_paths", 1, first_config).run(
        first_evidence, EvidenceConfig().pack_options
    )
    second = get_pack("configured_paths", 1, second_config).run(
        second_evidence, EvidenceConfig().pack_options
    )

    assert first_config == second_config
    assert first_config.hash == second_config.hash
    assert first == second


def test_configured_globs_are_rooted_case_sensitive_and_double_star_matches_zero_segments() -> None:
    config = _configured_paths(
        PathPredicateConfig("touches_root_main", ("**/main.ts",)),
        PathPredicateConfig("touches_exact_case", ("Apps/Web/**",)),
        PathPredicateConfig("touches_unicode", ("módulos/niño?.py",)),
    )
    result = get_pack("configured_paths", 1, config).run(
        DiffEvidence(
            changes=(
                FileChange("main.ts", additions=1, deletions=0),
                FileChange("apps/web/main.ts", additions=1, deletions=0),
                FileChange("módulos/niño1.py", additions=1, deletions=0),
            )
        ),
        EvidenceConfig(large_change_churn=100, multi_file_count=100).pack_options,
    )

    assert "touches_root_main" in result.facts
    assert "touches_unicode" in result.facts
    assert "touches_exact_case" not in result.facts
    assert result.metadata["configured_path_match_counts"] == {
        "touches_exact_case": 0,
        "touches_root_main": 2,
        "touches_unicode": 1,
    }


def test_configured_component_matcher_agrees_with_portable_wildcard_semantics() -> None:
    alphabet = "ab*?"
    patterns = (
        "".join(characters)
        for length in range(5)
        for characters in product(alphabet, repeat=length)
    )
    values = [
        "".join(characters) for length in range(5) for characters in product("ab*", repeat=length)
    ]

    for pattern in patterns:
        for value in values:
            assert configured_paths_module._component_matches(pattern, value) is fnmatchcase(
                value, pattern
            )


@pytest.mark.parametrize(
    "pattern",
    [
        "",
        "/absolute/**",
        "trailing/",
        "double//slash",
        "./relative/**",
        "../outside/**",
        "safe/../outside",
        "back\\slash/**",
        ":(glob)magic/**",
        "foo/***",
        "foo**bar",
        "classes/[ab].py",
        "braces/{a,b}.py",
        "control/\x00.py",
        "surrogate/\ud800.py",
    ],
)
def test_configured_paths_rejects_ambiguous_or_unsafe_globs(pattern: str) -> None:
    with pytest.raises(ModelError):
        PathPredicateConfig("touches_surface", (pattern,))


@pytest.mark.parametrize(
    "predicate",
    [
        "surface_web",
        "touches_",
        "not_touches_web",
        "touches_" + "x" * 57,
        "touches_ci",
        "touches_test",
    ],
)
def test_configured_paths_rejects_semantically_invalid_or_colliding_names(
    predicate: str,
) -> None:
    if predicate in {"touches_ci", "touches_test"}:
        config = _configured_paths(PathPredicateConfig(predicate, ("apps/web/**",)))
        with pytest.raises(ModelError, match="collide"):
            get_pack("configured_paths", 1, config)
    else:
        with pytest.raises(ModelError):
            PathPredicateConfig(predicate, ("apps/web/**",))


def test_configured_paths_rejects_duplicates_limits_and_excessive_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ModelError, match="duplicate predicates"):
        _configured_paths(
            PathPredicateConfig("touches_surface", ("a/**",)),
            PathPredicateConfig("touches_surface", ("b/**",)),
        )
    with pytest.raises(ModelError, match="duplicate globs"):
        PathPredicateConfig("touches_surface", ("a/**", "a/**"))
    with pytest.raises(ModelError, match="at most 32 globs"):
        PathPredicateConfig(
            "touches_surface",
            tuple(f"path_{index}/**" for index in range(33)),
        )
    with pytest.raises(ModelError, match="at most 32 predicates"):
        _configured_paths(
            *(
                PathPredicateConfig(f"touches_surface_{index}", (f"p{index}/**",))
                for index in range(33)
            )
        )

    maximum_globs = _configured_paths(
        *(
            PathPredicateConfig(
                f"touches_surface_{predicate}",
                tuple(f"p{predicate}/path_{index}/**" for index in range(32)),
            )
            for predicate in range(8)
        )
    )
    assert maximum_globs.total_globs == 256
    with pytest.raises(ModelError, match="at most 256"):
        _configured_paths(
            *maximum_globs.path_predicates,
            PathPredicateConfig("touches_one_too_many", ("overflow/**",)),
        )

    config = _configured_paths(PathPredicateConfig("touches_surface", ("**",)))
    monkeypatch.setattr(configured_paths_module, "MAX_MATCH_COMPARISONS", 1)
    with pytest.raises(ValueError, match="safe limit"):
        get_pack("configured_paths", 1, config).run(
            DiffEvidence(
                changes=(
                    FileChange("a.txt", additions=1, deletions=0),
                    FileChange("b.txt", additions=1, deletions=0),
                )
            ),
            EvidenceConfig().pack_options,
        )


def test_configured_paths_metadata_remains_bounded_for_large_diffs() -> None:
    config = _configured_paths(PathPredicateConfig("touches_packages", ("packages/**",)))
    evidence = DiffEvidence(
        changes=tuple(
            FileChange(
                f"packages/component_{index:05d}/src/feature_{index:05d}.ts",
                additions=200,
                deletions=20,
            )
            for index in range(6_700)
        )
    )

    result = get_pack("configured_paths", 1, config).run(
        evidence,
        EvidenceConfig(metadata_file_limit=512).pack_options,
    )
    encoded = json.dumps(result.metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    assert "touches_packages" in result.facts
    assert result.metadata["configured_path_match_counts"] == {"touches_packages": 6_700}
    assert len(result.provenance["touches_packages"].evidence) == 12
    assert result.metadata["metadata_files_truncated"] > 0
    assert len(encoded) < 256 * 1024


def test_configured_paths_rejects_adversarial_matcher_work_before_matching() -> None:
    pattern = "*a" * 127 + "*"
    config = _configured_paths(PathPredicateConfig("touches_adversarial", (pattern,)))
    evidence = DiffEvidence(
        changes=tuple(
            FileChange(f"{'a' * 240}{index:05d}.ts", additions=1, deletions=0)
            for index in range(6_700)
        )
    )

    with pytest.raises(ValueError, match="matcher work units"):
        get_pack("configured_paths", 1, config).run(evidence, EvidenceConfig().pack_options)


def test_configured_path_reason_accumulator_is_bounded_during_extraction() -> None:
    reasons: dict[str, set[str]] = {}

    for index in range(100_000):
        configured_paths_module._record_bounded_reason(
            reasons,
            "touches_surface",
            f"surface/file_{index:06d}.ts",
        )

    assert len(reasons["touches_surface"]) == 12
    assert max(reasons["touches_surface"]) == "path:surface/file_000011.ts"


def test_pack_contract_requires_declared_facts_with_provenance() -> None:
    broken = EvidencePack(
        name="broken",
        version=1,
        extractor="tests.broken.v1",
        description="broken test pack",
        predicates=("risk",),
        content_path=lambda _path: False,
        extract=lambda _evidence, _options: PackExtraction(
            facts=frozenset({"risk"}),
            provenance={},
            metadata={},
        ),
    )

    with pytest.raises(ValueError, match="matching provenance"):
        broken.run(
            DiffEvidence(changes=()),
            EvidenceConfig().pack_options,
        )

    wrong_provenance = EvidencePack(
        name="wrong_provenance",
        version=1,
        extractor="tests.expected.v1",
        description="wrong provenance test pack",
        predicates=("risk",),
        content_path=lambda _path: False,
        extract=lambda _evidence, _options: PackExtraction(
            facts=frozenset({"risk"}),
            provenance={
                "risk": FactEvidence(
                    kind="human",
                    extractor="tests.untrusted.v1",
                    evidence=("synthetic",),
                )
            },
            metadata={},
        ),
    )
    with pytest.raises(ValueError, match="deterministic provenance"):
        wrong_provenance.run(DiffEvidence(changes=()), EvidenceConfig().pack_options)


def test_flutter_semantic_change_isolated_by_version_and_protocol_hash() -> None:
    evidence = DiffEvidence(
        changes=(FileChange("lib/notifier.dart", additions=1, deletions=0),),
        content_patch="+state = const AsyncLoading();",
    )
    options = EvidenceConfig().pack_options
    v1 = get_pack("flutter_testing", 1).run(evidence, options)
    v2 = get_pack("flutter_testing", 2).run(evidence, options)
    v2_config = RuleLoomConfig(
        schema_version=2,
        project="ExampleProject",
        pack="flutter_testing",
        pack_version=2,
    )
    v1_profile = RuleLoomConfig(project="ExampleProject")

    assert "mutates_state" not in v1.facts
    assert "mutates_state" in v2.facts
    assert v2.provenance["mutates_state"] == FactEvidence(
        kind="deterministic",
        extractor="ruleloom.flutter_testing.git.v2",
        evidence=("diff-pattern:provider state assignment",),
    )
    assert v1_profile.evidence_protocol_hash != v2_config.evidence_protocol_hash

    with pytest.raises(ModelError, match="frozen for schema-v1"):
        RuleLoomConfig(
            schema_version=2,
            project="ExampleProject",
            pack="flutter_testing",
            pack_version=1,
        )
