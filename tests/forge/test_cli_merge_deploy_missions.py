"""``forge merge-deploy`` never presses a mission's build (missions, 17 October 2026).

A feature inside a mission lands on its mission's branch by itself; the
mission's own merge into main is pressed only by the answer to its merge card.
The command refuses both, in a plain sentence, before any backend or git.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from forge.cli import merge_deploy as merge_deploy_module
from forge.cli.merge_deploy import merge_deploy_cmd
from forge.config.models import ForgeConfig
from forge.lifecycle.missions import ensure_tables
from tests.forge.test_cli_merge_deploy import (  # noqa: F401 - fixtures
    BUILD_ID,
    FEATURE_ID,
    _insert_build,
    _receipts_env,
    config,
    fakes,
    pool,
    repo_root,
)


@pytest.mark.parametrize("role", ["feature", "mission-merge"])
def test_a_missions_build_is_refused(
    config, pool, fakes, monkeypatch: pytest.MonkeyPatch, role: str  # noqa: F811
) -> None:
    ensure_tables(pool.connection)
    _insert_build(pool)
    pool.connection.execute(
        "UPDATE builds SET mission_id = 'm-abc123', mission_role = ? WHERE build_id = ?",
        (role, BUILD_ID),
    )
    pool.connection.commit()

    async def forbidden_backends(_config: ForgeConfig):
        raise AssertionError("a mission's build must be refused before any backend")

    monkeypatch.setattr(merge_deploy_module, "_aopen_backends", forbidden_backends)

    result = CliRunner().invoke(merge_deploy_cmd, [FEATURE_ID], obj=config)

    assert result.exit_code != 0
    assert "mission m-abc123" in result.output
    assert "Nothing was merged." in result.output
    assert fakes["gk_calls"] == []


def test_an_ordinary_build_on_a_ledger_with_missions_is_pressed_as_before(
    config, pool, fakes  # noqa: F811
) -> None:
    ensure_tables(pool.connection)
    _insert_build(pool)

    result = CliRunner().invoke(merge_deploy_cmd, [FEATURE_ID], obj=config)

    assert result.exit_code == 0, result.output
