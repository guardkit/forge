"""D4 BUILD admission: one policy across canonical names and rewritten paths."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from forge.config.build_admission import build_admission
from forge.config.models import ForgeConfig

SANDBOXED = "appmilla/study-tutor"
UNSANDBOXED = "example/plain"
REWRITTEN = "/var/lib/forge/projects/study-tutor"


def _config(*, strict: bool, sandboxes: dict | None = None) -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/var/lib/forge/projects"]}},
            "planning": {
                "target_repo_paths": {
                    SANDBOXED: REWRITTEN,
                    UNSANDBOXED: "/var/lib/forge/projects/plain",
                },
                "sandboxes": sandboxes
                or {
                    SANDBOXED: {
                        "name": "study-tutor",
                        "sidecar_url": "http://sandbox:8125",
                        "runner_url": "http://sandbox:8124",
                    }
                },
            },
            "publication": {
                "builds_may_run_inside_the_coordinator": not strict,
            },
        }
    )


@pytest.mark.parametrize("strict", [False, True])
def test_registered_sandbox_resolves_the_same_canonical_key_from_name_and_path(
    strict: bool,
) -> None:
    config = _config(strict=strict)

    by_name = build_admission(config, target_repo=SANDBOXED)
    by_path = build_admission(config, repo_path=REWRITTEN)

    assert by_name.allowed is True
    assert by_path.allowed is True
    if strict:
        assert by_name.repo_key == SANDBOXED
        assert by_path.repo_key == SANDBOXED


@pytest.mark.parametrize("target", [None, "", "unknown/repository", UNSANDBOXED])
def test_strict_mode_refuses_missing_unknown_and_unsandboxed_targets(target) -> None:
    decision = build_admission(_config(strict=True), target_repo=target)

    assert decision.allowed is False
    assert decision.reason is not None
    assert decision.reason.startswith("sandbox-required: repository")


def test_strict_path_resolution_refuses_ambiguous_declared_paths() -> None:
    config = SimpleNamespace(
        publication=SimpleNamespace(builds_may_run_inside_the_coordinator=False),
        planning=SimpleNamespace(
            target_repo_paths={
                "one/project": REWRITTEN,
                "two/project": REWRITTEN,
            },
            sandboxes={},
        ),
    )

    decision = build_admission(config, repo_path=REWRITTEN)

    assert decision.allowed is False
    assert "ambiguous" in (decision.reason or "")


def test_repair_requires_a_usable_sidecar_as_well_as_the_runner() -> None:
    config = SimpleNamespace(
        publication=SimpleNamespace(builds_may_run_inside_the_coordinator=False),
        planning=SimpleNamespace(
            target_repo_paths={SANDBOXED: REWRITTEN},
            sandboxes={
                SANDBOXED: {
                    "runner_url": "https://runner.example",
                    "sidecar_url": "",
                }
            },
        ),
    )

    build = build_admission(config, target_repo=SANDBOXED)
    repair = build_admission(
        config, target_repo=SANDBOXED, require_sidecar=True
    )

    assert build.allowed is True
    assert repair.allowed is False
    assert "sidecar" in (repair.reason or "")


def test_legacy_omitted_policy_preserves_unknown_global_routing() -> None:
    legacy = SimpleNamespace(planning=SimpleNamespace(target_repo_paths={}, sandboxes={}))

    decision = build_admission(legacy, target_repo="legacy/project")

    assert decision.allowed is True
    assert decision.repo_key == "legacy/project"


def test_strict_mode_refuses_a_registered_sandbox_without_a_runner_route() -> None:
    config = SimpleNamespace(
        publication=SimpleNamespace(builds_may_run_inside_the_coordinator=False),
        planning=SimpleNamespace(
            target_repo_paths={SANDBOXED: REWRITTEN},
            sandboxes={
                SANDBOXED: {
                    "runner_url": "",
                    "sidecar_url": "https://sidecar.example",
                }
            },
        ),
    )

    decision = build_admission(config, target_repo=SANDBOXED)

    assert decision.allowed is False
    assert "runner" in (decision.reason or "")


def test_explicit_legacy_true_preserves_unknown_global_routing() -> None:
    legacy = SimpleNamespace(
        publication=SimpleNamespace(builds_may_run_inside_the_coordinator=True),
        planning=SimpleNamespace(target_repo_paths={}, sandboxes={}),
    )

    decision = build_admission(legacy, target_repo="legacy/project")

    assert decision.allowed is True
    assert decision.repo_key == "legacy/project"
