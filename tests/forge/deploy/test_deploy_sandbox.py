"""Deploying into a Docker Sandbox — the profile block and its threading.

The 2026-09-06 decision: every merge deploys the feature into a Docker Sandbox,
a small virtual machine with its own kernel and its own Docker engine. A
repository says which sandbox it owns in a ``sandbox`` block in its deploy
profile; the deploy step puts that block's five settings into the environment
of every step that runs a script, and the repository's own wrapper reads them
from there.

Two halves are proven here:

* the block loads and is checked — a bad name, port or network rule is refused
  on load with one plain sentence, so nobody meets it as a puzzling failure
  halfway through a deploy;
* the five settings reach every ``deploy_compose`` and ``health_check`` step of
  every runbook the deploy stage builds — the deploy, the promote, the revert
  and the candidate teardown — and, with no block, not one step is touched.

Nothing here runs ``sbx``, creates a sandbox, or asks systemd for anything.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from forge.deploy.profile import (
    DeployProfileError,
    DeploySandbox,
    parse_deploy_profile,
)
from forge.deploy.runbook_builder import (
    build_candidate_teardown_runbook,
    build_deploy_runbook,
    build_revert_runbook,
    sandbox_env,
)
from forge.deploy.steps import make_deploy_compose_handler
from forge.persistence.repositories.runbook_models import Runbook

FIXED = datetime(2026, 9, 6, 12, 0, 0, tzinfo=UTC)

#: The five names the deploy step threads, exactly as the spec lists them.
FIVE_NAMES = (
    "SANDBOX_NAME",
    "SANDBOX_MEMORY",
    "SANDBOX_CPUS",
    "SANDBOX_PUBLISH",
    "SANDBOX_ALLOW_NETWORK",
)

SANDBOX_BLOCK: dict[str, Any] = {
    "name": "api-test-deploy",
    "memory": "6g",
    "cpus": 4,
    "publish": ["127.0.0.1:8901:8901", "127.0.0.1:8902:8902"],
    "allow_network": [
        "deb.debian.org",
        "security.debian.org",
        "*.debian.org",
        "pypi.org",
        "files.pythonhosted.org",
    ],
}


def _profile(*, sandbox: bool = True, **overrides: Any):
    raw: dict[str, Any] = {
        "env_id": "local",
        "compose": {"file": "docker-compose.yml", "script": "deploy/sandbox-deploy.sh"},
        "cwd": "/home/rich/Projects/appmilla_github/api_test",
        "health_checks": [{"cmd": "deploy/healthcheck.sh"}],
        "rollback_image_ref": "apitest-app:rollback-pre-deploy",
        "candidate": {"env": {"CANDIDATE_PORT": "8902"}, "keep": False},
    }
    if sandbox:
        raw["sandbox"] = dict(SANDBOX_BLOCK)
    raw.update(overrides)
    return parse_deploy_profile(raw)


def _params(runbook: Runbook, step_type: str) -> dict[str, Any]:
    for step in runbook.steps:
        if step.step_type == step_type:
            return dict(step.params)
    raise AssertionError(f"no {step_type} step in {runbook.runbook_id}")


# ---------------------------------------------------------------------------
# The block loads
# ---------------------------------------------------------------------------


class TestTheBlockLoads:
    def test_every_setting_is_read(self) -> None:
        profile = _profile()
        assert profile.sandbox == DeploySandbox(
            name="api-test-deploy",
            memory="6g",
            cpus=4,
            publish=("127.0.0.1:8901:8901", "127.0.0.1:8902:8902"),
            allow_network=(
                "deb.debian.org",
                "security.debian.org",
                "*.debian.org",
                "pypi.org",
                "files.pythonhosted.org",
            ),
        )

    def test_it_no_longer_lands_in_extra(self) -> None:
        # Before this work the whole block fell through to `extra`, where
        # nothing read it.
        assert "sandbox" not in _profile().extra

    def test_no_block_means_no_sandbox(self) -> None:
        assert _profile(sandbox=False).sandbox is None

    def test_only_the_name_is_required(self) -> None:
        profile = parse_deploy_profile(
            {
                "env_id": "local",
                "compose": {"file": "dc.yml"},
                "sandbox": {"name": "content-agent-py-deploy"},
            }
        )
        assert profile.sandbox is not None
        assert profile.sandbox.memory is None
        assert profile.sandbox.cpus is None
        assert profile.sandbox.publish == ()
        assert profile.sandbox.allow_network == ()

    def test_a_host_and_port_rule_is_allowed(self) -> None:
        # The model door on this box is named by address and port, because
        # host.docker.internal resolves to an address the policy blocks.
        profile = parse_deploy_profile(
            {
                "env_id": "local",
                "compose": {"file": "dc.yml"},
                "sandbox": {
                    "name": "content-agent-py-deploy",
                    "allow_network": ["172.30.1.253:4000"],
                },
            }
        )
        assert profile.sandbox is not None
        assert profile.sandbox.allow_network == ("172.30.1.253:4000",)

    @pytest.mark.parametrize("form", ["8901", "8901:8901", "127.0.0.1:8901:8901"])
    def test_all_three_port_forms_are_allowed(self, form: str) -> None:
        profile = parse_deploy_profile(
            {
                "env_id": "local",
                "compose": {"file": "dc.yml"},
                "sandbox": {"name": "a-deploy", "publish": [form]},
            }
        )
        assert profile.sandbox is not None
        assert profile.sandbox.publish == (form,)


# ---------------------------------------------------------------------------
# A bad setting is refused, with one plain sentence
# ---------------------------------------------------------------------------


class TestABadSettingIsRefused:
    @pytest.mark.parametrize(
        "name",
        ["API-Test-Deploy", "a", "-leading-hyphen", "has spaces", "a" * 64, ""],
    )
    def test_a_bad_name_is_refused(self, name: str) -> None:
        with pytest.raises(DeployProfileError, match="sandbox.name"):
            parse_deploy_profile(
                {
                    "env_id": "l",
                    "compose": {"file": "dc.yml"},
                    "sandbox": {"name": name},
                }
            )

    def test_a_missing_name_is_refused(self) -> None:
        with pytest.raises(DeployProfileError, match="sandbox.name"):
            parse_deploy_profile(
                {"env_id": "l", "compose": {"file": "dc.yml"}, "sandbox": {}}
            )

    @pytest.mark.parametrize(
        "rule",
        ["0:8901", "99999:8901", "8901:0", "a:8901", "1:2:3:4", "127.0.0.1:8901"],
    )
    def test_a_bad_port_rule_is_refused(self, rule: str) -> None:
        with pytest.raises(DeployProfileError, match="sandbox.publish"):
            parse_deploy_profile(
                {
                    "env_id": "l",
                    "compose": {"file": "dc.yml"},
                    "sandbox": {"name": "a-deploy", "publish": [rule]},
                }
            )

    @pytest.mark.parametrize(
        "rule",
        ["http://pypi.org", "pypi.org/simple", "pypi.org:0", "pypi org", ""],
    )
    def test_a_bad_network_rule_is_refused(self, rule: str) -> None:
        with pytest.raises(DeployProfileError, match="sandbox.allow_network"):
            parse_deploy_profile(
                {
                    "env_id": "l",
                    "compose": {"file": "dc.yml"},
                    "sandbox": {"name": "a-deploy", "allow_network": [rule]},
                }
            )

    def test_a_comma_in_a_rule_is_refused(self) -> None:
        # The rules are joined with commas on their way to the wrapper, so a
        # comma inside one would silently become two rules.
        with pytest.raises(DeployProfileError, match="comma"):
            parse_deploy_profile(
                {
                    "env_id": "l",
                    "compose": {"file": "dc.yml"},
                    "sandbox": {
                        "name": "a-deploy",
                        "allow_network": ["pypi.org,evil.example"],
                    },
                }
            )

    @pytest.mark.parametrize("cpus", [0, -1, "four", True, 2.5])
    def test_bad_processors_are_refused(self, cpus: Any) -> None:
        with pytest.raises(DeployProfileError, match="sandbox.cpus"):
            parse_deploy_profile(
                {
                    "env_id": "l",
                    "compose": {"file": "dc.yml"},
                    "sandbox": {"name": "a-deploy", "cpus": cpus},
                }
            )

    @pytest.mark.parametrize("memory", ["", "   ", 6, [], {"g": 6}])
    def test_bad_memory_is_refused(self, memory: Any) -> None:
        with pytest.raises(DeployProfileError, match="sandbox.memory"):
            parse_deploy_profile(
                {
                    "env_id": "l",
                    "compose": {"file": "dc.yml"},
                    "sandbox": {"name": "a-deploy", "memory": memory},
                }
            )

    def test_a_sandbox_that_is_not_a_block_is_refused(self) -> None:
        with pytest.raises(DeployProfileError, match="sandbox"):
            parse_deploy_profile(
                {
                    "env_id": "l",
                    "compose": {"file": "dc.yml"},
                    "sandbox": "api-test-deploy",
                }
            )


# ---------------------------------------------------------------------------
# The five settings reach every step that runs a script
# ---------------------------------------------------------------------------


class TestTheSettingsAreThreaded:
    def test_the_five_names_and_their_values(self) -> None:
        assert sandbox_env(_profile()) == {
            "SANDBOX_NAME": "api-test-deploy",
            "SANDBOX_MEMORY": "6g",
            "SANDBOX_CPUS": "4",
            "SANDBOX_PUBLISH": "127.0.0.1:8901:8901,127.0.0.1:8902:8902",
            "SANDBOX_ALLOW_NETWORK": (
                "deb.debian.org,security.debian.org,*.debian.org,pypi.org,"
                "files.pythonhosted.org"
            ),
        }

    def test_settings_left_out_come_through_empty(self) -> None:
        profile = parse_deploy_profile(
            {
                "env_id": "l",
                "compose": {"file": "dc.yml"},
                "sandbox": {"name": "a-deploy"},
            }
        )
        assert sandbox_env(profile) == {
            "SANDBOX_NAME": "a-deploy",
            "SANDBOX_MEMORY": "",
            "SANDBOX_CPUS": "",
            "SANDBOX_PUBLISH": "",
            "SANDBOX_ALLOW_NETWORK": "",
        }

    def test_the_deploy_runbook_carries_them_on_both_steps(self) -> None:
        runbook = build_deploy_runbook(
            _profile(), runbook_id="deploy-1", target="local", now=FIXED
        )
        for step_type in ("deploy_compose", "health_check"):
            env = _params(runbook, step_type)["extra_env"]
            assert all(name in env for name in FIVE_NAMES), step_type
            assert env["SANDBOX_NAME"] == "api-test-deploy"

    def test_the_candidate_leg_carries_both_the_mode_and_the_sandbox(self) -> None:
        # This is the shape the stage builds for the candidate leg: the mode
        # signal and the candidate's own addressing, plus the sandbox.
        runbook = build_deploy_runbook(
            _profile(),
            runbook_id="deploy-1",
            target="local",
            now=FIXED,
            compose_extra_env={"CANDIDATE": "1", "CANDIDATE_PORT": "8902"},
            check_extra_env={"CANDIDATE_PORT": "8902"},
        )
        compose = _params(runbook, "deploy_compose")["extra_env"]
        assert compose["CANDIDATE"] == "1"
        assert compose["CANDIDATE_PORT"] == "8902"
        assert compose["SANDBOX_NAME"] == "api-test-deploy"
        check = _params(runbook, "health_check")["extra_env"]
        assert check["CANDIDATE_PORT"] == "8902"
        assert check["SANDBOX_PUBLISH"] == "127.0.0.1:8901:8901,127.0.0.1:8902:8902"

    def test_the_promote_leg_carries_both(self) -> None:
        runbook = build_deploy_runbook(
            _profile(),
            runbook_id="deploy-1",
            target="local",
            now=FIXED,
            compose_extra_env={"PROMOTE": "1"},
        )
        compose = _params(runbook, "deploy_compose")["extra_env"]
        assert compose["PROMOTE"] == "1"
        assert compose["SANDBOX_NAME"] == "api-test-deploy"

    def test_the_revert_runbook_carries_them(self) -> None:
        # A revert must happen inside the same sandbox the deploy happened in.
        runbook = build_revert_runbook(
            _profile(),
            runbook_id="revert-1",
            target="local",
            rollback_image_ref="apitest-app:rollback-pre-deploy",
            now=FIXED,
        )
        env = _params(runbook, "deploy_compose")["extra_env"]
        assert all(name in env for name in FIVE_NAMES)

    def test_the_teardown_runbook_carries_them(self) -> None:
        runbook = build_candidate_teardown_runbook(
            _profile(),
            runbook_id="teardown-1",
            target="local",
            extra_env={"CANDIDATE_DOWN": "1", "CANDIDATE_PORT": "8902"},
            now=FIXED,
        )
        env = _params(runbook, "deploy_compose")["extra_env"]
        assert env["CANDIDATE_DOWN"] == "1"
        assert all(name in env for name in FIVE_NAMES)

    def test_the_caller_s_own_overlay_wins(self) -> None:
        # If a leg ever set one of these itself, the leg's value is the one
        # that goes through — the sandbox settings are the floor, not a lid.
        runbook = build_deploy_runbook(
            _profile(),
            runbook_id="deploy-1",
            target="local",
            now=FIXED,
            compose_extra_env={"SANDBOX_NAME": "somewhere-else"},
        )
        assert (
            _params(runbook, "deploy_compose")["extra_env"]["SANDBOX_NAME"]
            == "somewhere-else"
        )

    def test_they_reach_the_script_the_step_runs(self) -> None:
        # Driven through the real deploy_compose handler with a recording
        # runner in place of the subprocess: what the step params say is what
        # the script's environment gets.
        calls: list[dict[str, Any]] = []

        def runner(**kwargs: Any) -> tuple[int, str]:
            calls.append(kwargs)
            return 0, "ok"

        runbook = build_deploy_runbook(
            _profile(),
            runbook_id="deploy-1",
            target="local",
            now=FIXED,
            compose_extra_env={"CANDIDATE": "1"},
        )
        step = next(s for s in runbook.steps if s.step_type == "deploy_compose")
        handler = make_deploy_compose_handler(dry_run=False, script_runner=runner)
        outcome = handler(step)

        assert outcome.status.value == "passed"
        assert len(calls) == 1
        env = calls[0]["extra_env"]
        assert env["SANDBOX_NAME"] == "api-test-deploy"
        assert env["SANDBOX_MEMORY"] == "6g"
        assert env["SANDBOX_CPUS"] == "4"
        assert env["CANDIDATE"] == "1"
        assert calls[0]["script"] == "deploy/sandbox-deploy.sh"


# ---------------------------------------------------------------------------
# No block ⇒ nothing at all changes
# ---------------------------------------------------------------------------


class TestNoBlockChangesNothing:
    def test_no_settings_to_thread(self) -> None:
        assert sandbox_env(_profile(sandbox=False)) == {}

    def test_the_deploy_runbook_is_what_it_was(self) -> None:
        runbook = build_deploy_runbook(
            _profile(sandbox=False), runbook_id="deploy-1", target="local", now=FIXED
        )
        assert "extra_env" not in _params(runbook, "deploy_compose")
        assert "extra_env" not in _params(runbook, "health_check")

    def test_the_revert_runbook_is_what_it_was(self) -> None:
        runbook = build_revert_runbook(
            _profile(sandbox=False),
            runbook_id="revert-1",
            target="local",
            rollback_image_ref="apitest-app:rollback-pre-deploy",
            now=FIXED,
        )
        assert "extra_env" not in _params(runbook, "deploy_compose")

    def test_the_teardown_runbook_is_what_it_was(self) -> None:
        runbook = build_candidate_teardown_runbook(
            _profile(sandbox=False),
            runbook_id="teardown-1",
            target="local",
            extra_env={"CANDIDATE_DOWN": "1"},
            now=FIXED,
        )
        assert _params(runbook, "deploy_compose")["extra_env"] == {
            "CANDIDATE_DOWN": "1"
        }

    def test_no_sandbox_name_reaches_the_script(self) -> None:
        calls: list[dict[str, Any]] = []

        def runner(**kwargs: Any) -> tuple[int, str]:
            calls.append(kwargs)
            return 0, "ok"

        runbook = build_deploy_runbook(
            _profile(sandbox=False), runbook_id="deploy-1", target="local", now=FIXED
        )
        step = next(s for s in runbook.steps if s.step_type == "deploy_compose")
        make_deploy_compose_handler(dry_run=False, script_runner=runner)(step)
        assert calls[0]["extra_env"] is None


# ---------------------------------------------------------------------------
# The whole deploy stage, driven — not just the runbook builders
#
# One dry run of the real DeployStageRunner over a profile with a sandbox
# block: the candidate leg, the promote leg and the candidate teardown all
# happen, and every runbook the run left behind carries the five settings on
# the steps that run a script. Dry run means no script and no subprocess: the
# executor records what it would have run.
# ---------------------------------------------------------------------------


class TestTheWholeStageCarriesThem:
    @pytest.fixture
    def repository(self, tmp_path):
        import sqlite3

        from forge.persistence.migrations.runbook import apply
        from forge.persistence.repositories.runbook import RunbookRepository

        connection = sqlite3.connect(str(tmp_path / "deploy.db"))
        apply(connection)
        return RunbookRepository(connection=connection)

    @pytest.fixture
    def runner(self, repository, tmp_path):
        from unittest.mock import AsyncMock

        from forge.config.models import DeployStageConfig
        from forge.deploy.live_gate import DryRunBrokerInspector, DryRunLiveGateInvoker
        from forge.deploy.reservation import InProcessReservationLease
        from forge.deploy.stage import DeployStageRunner

        return DeployStageRunner(
            repository=repository,
            runbook_publisher=AsyncMock(),
            deploy_publisher=AsyncMock(),
            reservation=InProcessReservationLease(),
            live_gate_invoker=DryRunLiveGateInvoker(),
            broker_inspector=DryRunBrokerInspector(),
            config=DeployStageConfig(),
            deploy_record_root=str(tmp_path / "state"),
            dry_run=True,
            clock=lambda: FIXED,
        )

    @pytest.mark.asyncio
    async def test_every_runbook_of_a_real_run_carries_the_settings(
        self, runner, repository
    ) -> None:
        result = await runner.run_deploy(
            _profile(),
            correlation_id="corr-sandbox-1",
            deploy_run_id="run-1",
            feature="FEAT-SBX",
            feat_id="FEAT-SBX",
            task_id="TASK-SBX1",
        )
        assert result.outcome == "complete", result.failed_step

        for runbook_id in (
            "deploy-cand-run-1",  # the candidate leg
            "deploy-run-1",  # the promote leg
            "teardown-cand-run-1",  # tearing the candidate down again
        ):
            executed = repository.load_runbook(
                runbook_id, correlation_id="corr-sandbox-1"
            )
            assert executed is not None, runbook_id
            script_steps = [
                step
                for step in executed.steps
                if step.step_type in ("deploy_compose", "health_check")
            ]
            assert script_steps, runbook_id
            for step in script_steps:
                env = step.params.get("extra_env", {})
                assert all(name in env for name in FIVE_NAMES), (
                    f"{runbook_id}/{step.step_type}"
                )
                assert env["SANDBOX_NAME"] == "api-test-deploy"

    @pytest.mark.asyncio
    async def test_the_same_run_without_a_block_carries_nothing(
        self, runner, repository
    ) -> None:
        result = await runner.run_deploy(
            _profile(sandbox=False),
            correlation_id="corr-sandbox-2",
            deploy_run_id="run-2",
            feature="FEAT-SBX",
            feat_id="FEAT-SBX",
            task_id="TASK-SBX2",
        )
        assert result.outcome == "complete", result.failed_step

        executed = repository.load_runbook("deploy-run-2", correlation_id="corr-sandbox-2")
        assert executed is not None
        for step in executed.steps:
            if step.step_type in ("deploy_compose", "health_check"):
                for name in FIVE_NAMES:
                    assert name not in step.params.get("extra_env", {})
