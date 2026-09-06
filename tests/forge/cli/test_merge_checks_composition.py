"""Where the merge word's post-merge checks run, decided at daemon start-up.

With an address for the deploy sidecar the merge executor gets the run that
sends the work to the host; without one it gets the in-container run, exactly
as before. Either way one line in the log says which, in words a person reads.
"""

from __future__ import annotations

import pytest

from forge.adapters.guardkit.run import run as in_container_run
from forge.cli.serve import compose_merge_guardkit_run
from forge.config.models import ForgeConfig

REPO_KEY = "appmilla/api_test"


def _config(sidecar_url: str | None) -> ForgeConfig:
    deploy: dict[str, object] = {"enabled": True}
    if sidecar_url is not None:
        deploy["sidecar_url"] = sidecar_url
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {"target_repo_paths": {REPO_KEY: "/tmp/api_test"}},
            "deploy": deploy,
        }
    )


def test_with_a_sidecar_address_the_checks_go_to_the_host(caplog) -> None:
    with caplog.at_level("INFO"):
        run = compose_merge_guardkit_run(_config("http://127.0.0.1:8125"))

    assert run is not in_container_run
    assert run.__name__ == "run_merge_via_sidecar"
    lines = [record.getMessage() for record in caplog.records]
    assert any(
        "the merge word runs its checks on the host through the deploy "
        "sidecar at http://127.0.0.1:8125" in line
        for line in lines
    ), lines


def test_without_a_sidecar_address_the_checks_stay_in_the_container(caplog) -> None:
    with caplog.at_level("INFO"):
        run = compose_merge_guardkit_run(_config(""))

    assert run is in_container_run
    lines = [record.getMessage() for record in caplog.records]
    assert any(
        "the merge word runs its checks inside the forge container "
        "(no deploy sidecar configured)" in line
        for line in lines
    ), lines


def test_the_default_configuration_uses_the_sidecar() -> None:
    """The setting ships with the loopback address already in it, so a daemon
    that says nothing about it gets the host, which is what this lane is for."""
    run = compose_merge_guardkit_run(_config(None))
    assert run is not in_container_run


def test_settings_with_no_deploy_section_at_all_stay_in_the_container() -> None:
    """A configuration object from somewhere else, with no deploy settings on
    it, must not break start-up."""

    class _NoDeploySettings:
        planning = None

    run = compose_merge_guardkit_run(_NoDeploySettings())
    assert run is in_container_run


@pytest.mark.asyncio
async def test_the_composed_sidecar_run_carries_only_the_merge(tmp_path) -> None:
    """The composed callable is the merge-only door, not a general one."""
    from forge.adapters.guardkit.run_via_sidecar import MergeCallRefused

    run = compose_merge_guardkit_run(_config("http://127.0.0.1:8125"))
    with pytest.raises(MergeCallRefused):
        await run(
            subcommand="feature-spec",
            args=["propose"],
            repo_path=tmp_path,
            read_allowlist=[tmp_path],
            timeout_seconds=60,
            with_nats_streaming=False,
        )


@pytest.mark.asyncio
async def test_the_attended_merge_command_uses_the_same_chooser(monkeypatch) -> None:
    """2026-09-06: ``forge merge-deploy`` ran guardkit in the container after
    the daemon had moved the merge word's checks to the host. Both must go
    through one chooser."""
    import sys
    import types
    from forge.cli import merge_deploy as cmd

    class _Client:
        async def drain(self) -> None:
            return None

    async def _connect(servers=None):
        return _Client()

    monkeypatch.setitem(sys.modules, "nats", types.SimpleNamespace(connect=_connect))
    monkeypatch.setattr(cmd, "_resolve_db_path", lambda: ":memory:")
    monkeypatch.setattr(
        "forge.pipeline.merge_executor.build_in_daemon_deploy_dispatcher",
        lambda **kw: object(),
    )
    _publisher, guardkit_run, _dispatcher, close = await cmd._aopen_backends(
        _config("http://127.0.0.1:9")
    )
    await close()
    assert guardkit_run is not in_container_run
    assert guardkit_run.__name__ == "run_merge_via_sidecar"

