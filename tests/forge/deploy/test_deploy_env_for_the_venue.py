"""The deploy is sent the environment its venue can use (2026-09-09).

The first real merge press ended in 0.16 seconds. The candidate leg's first
step never started: the deploy sidecar inside the repository's sandbox refused
the request because it carried ``SANDBOX_SIDECAR_PUBLISH``, a key it does not
allow. The teardown that followed was refused for the same reason, so the log
said the candidate might still be up. The reason was written down in one
ledger row and nowhere a person would look.

Two halves are proved here, and one habit:

* a deploy that runs INSIDE the repository's own sandbox is not sent the
  settings that say how to CREATE that sandbox — there is nothing there to
  create, and the program that reads them (the host wrapper) is not what runs.
  What the deploy actually reads is untouched: the candidate's own
  environment, the live gate's, and the mode signals;
* a deploy that goes through the HOST wrapper is sent all of them, as it is
  today, and the sidecar's own allowlist accepts every one it should. The two
  are asserted against each other from the real code, so a new setting cannot
  be added on one side alone and break a deploy this way again;
* a repository with no sandbox block threads exactly what it threaded before,
  whichever venue is named.

Nothing here runs ``sbx``, creates a sandbox, or touches anything live.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from forge.deploy.profile import parse_deploy_profile
from forge.deploy.runbook_builder import (
    build_candidate_teardown_runbook,
    build_deploy_runbook,
    build_revert_runbook,
    sandbox_env,
)
from forge.deploy.stage import sidecar_refusal
from forge.deploy_sidecar.service import allowed_env_keys
from forge.persistence.repositories.runbook_models import (
    Runbook,
    Step,
    StepResult,
    StepStatus,
)

FIXED = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)

#: Every setting a profile's sandbox block can produce: the five every sandbox
#: has always had, and the six that make it carry the factory's own services.
ELEVEN = (
    "SANDBOX_NAME",
    "SANDBOX_MEMORY",
    "SANDBOX_CPUS",
    "SANDBOX_PUBLISH",
    "SANDBOX_ALLOW_NETWORK",
    "SANDBOX_SIDECAR_PUBLISH",
    "SANDBOX_RUNNER_PUBLISH",
    "SANDBOX_ENV_FILE",
    "SANDBOX_FORGE_PATH",
    "SANDBOX_GUARDKIT_PATH",
    "SANDBOX_RECEIPTS_PATH",
)

#: api_test's own shape on the day it broke: a sandbox that carries the
#: factory, with every setting the block allows.
SANDBOX_BLOCK: dict[str, Any] = {
    "name": "api-test-deploy",
    "memory": "6g",
    "cpus": 4,
    "publish": ["127.0.0.1:8901:8901", "127.0.0.1:8902:8902"],
    "allow_network": ["pypi.org", "*.debian.org"],
    "sidecar_publish": "127.0.0.1:8925:8125",
    "runner_publish": "127.0.0.1:8924:8124",
    "env_file": "/run/user/1000/forge-sandbox/api-test-deploy.env",
    "forge_path": "/home/rich/Projects/appmilla_github/forge",
    "guardkit_path": "/home/rich/Projects/appmilla_github/guardkit",
    "receipts_path": "/home/rich/forge-state/receipts",
}


def _profile(*, sandbox: bool = True):
    raw: dict[str, Any] = {
        "env_id": "local",
        "compose": {
            "file": "docker-compose.yml",
            "script": "deploy/sandbox-deploy.sh",
            "env_file": "deploy/.env",
        },
        "cwd": "/home/rich/Projects/appmilla_github/api_test",
        "health_checks": [{"cmd": "deploy/healthcheck.sh"}],
        "rollback_image_ref": "apitest-app:rollback-pre-deploy",
        "live_gate": {
            "driver": ["python3", "qa/gates/local_live_gate.py"],
            "env": {"API_TEST_BASE_URL": "http://localhost:8901"},
        },
        "candidate": {
            "env": {
                "CANDIDATE_PORT": "8902",
                "API_TEST_BASE_URL": "http://localhost:8902",
            },
            "keep": False,
        },
    }
    if sandbox:
        raw["sandbox"] = dict(SANDBOX_BLOCK)
    return parse_deploy_profile(raw)


#: The overlay the candidate leg puts on top — the mode signal and the
#: candidate's own addressing. This is what the deploy actually reads.
CANDIDATE_OVERLAY = {
    "CANDIDATE": "1",
    "CANDIDATE_PORT": "8902",
    "API_TEST_BASE_URL": "http://localhost:8902",
}
CHECK_OVERLAY = {
    "CANDIDATE_PORT": "8902",
    "API_TEST_BASE_URL": "http://localhost:8902",
}


def _params(runbook: Runbook, step_type: str) -> dict[str, Any]:
    for step in runbook.steps:
        if step.step_type == step_type:
            return dict(step.params)
    raise AssertionError(f"no {step_type} step in {runbook.runbook_id}")


def _candidate_deploy(*, inside: bool, sandbox: bool = True) -> Runbook:
    return build_deploy_runbook(
        _profile(sandbox=sandbox),
        runbook_id="deploy-cand-1",
        target="local",
        now=FIXED,
        compose_extra_env=dict(CANDIDATE_OVERLAY),
        check_extra_env=dict(CHECK_OVERLAY),
        inside_sandbox=inside,
    )


# ---------------------------------------------------------------------------
# Inside the repository's own sandbox
# ---------------------------------------------------------------------------


class TestInsideItsOwnSandbox:
    def test_the_settings_that_make_a_sandbox_are_not_sent(self) -> None:
        assert sandbox_env(_profile(), inside_sandbox=True) == {}

    def test_the_deploy_step_carries_none_of_the_eleven(self) -> None:
        env = _params(_candidate_deploy(inside=True), "deploy_compose")["extra_env"]
        assert [name for name in ELEVEN if name in env] == []

    def test_the_deploy_step_carries_everything_else_it_had(self) -> None:
        # The mode signal and the candidate's addressing — what the deploy
        # reads — are exactly what they are for a host-wrapper deploy.
        inside = _params(_candidate_deploy(inside=True), "deploy_compose")
        wrapper = _params(_candidate_deploy(inside=False), "deploy_compose")
        assert inside["extra_env"] == CANDIDATE_OVERLAY
        assert {
            k: v for k, v in wrapper["extra_env"].items() if k not in ELEVEN
        } == CANDIDATE_OVERLAY
        # And nothing else about the step moved.
        assert {k: v for k, v in inside.items() if k != "extra_env"} == {
            k: v for k, v in wrapper.items() if k != "extra_env"
        }

    def test_the_health_check_step_likewise(self) -> None:
        inside = _params(_candidate_deploy(inside=True), "health_check")
        assert inside["extra_env"] == CHECK_OVERLAY
        assert [name for name in ELEVEN if name in inside["extra_env"]] == []

    def test_the_teardown_step_likewise(self) -> None:
        # The step that takes the candidate down is built the same way as the
        # one that put it up, so it cannot be refused for a reason the deploy
        # was not.
        teardown = build_candidate_teardown_runbook(
            _profile(),
            runbook_id="teardown-cand-1",
            target="local",
            extra_env={"CANDIDATE_DOWN": "1", "CANDIDATE_PORT": "8902"},
            now=FIXED,
            inside_sandbox=True,
        )
        env = _params(teardown, "deploy_compose")["extra_env"]
        assert env == {"CANDIDATE_DOWN": "1", "CANDIDATE_PORT": "8902"}

    def test_the_revert_step_likewise(self) -> None:
        revert = build_revert_runbook(
            _profile(),
            runbook_id="revert-1",
            target="local",
            rollback_image_ref="apitest-app:rollback-pre-deploy",
            now=FIXED,
            inside_sandbox=True,
        )
        assert "extra_env" not in _params(revert, "deploy_compose")


# ---------------------------------------------------------------------------
# Through the host wrapper
# ---------------------------------------------------------------------------


class TestThroughTheHostWrapper:
    def test_all_eleven_ride_as_they_do_today(self) -> None:
        env = _params(_candidate_deploy(inside=False), "deploy_compose")["extra_env"]
        assert all(name in env for name in ELEVEN)
        assert env["SANDBOX_SIDECAR_PUBLISH"] == "127.0.0.1:8925:8125"
        assert env["SANDBOX_RECEIPTS_PATH"] == "/home/rich/forge-state/receipts"
        # The overlay still wins over the settings underneath it.
        assert env["CANDIDATE"] == "1"

    def test_the_default_is_the_host_wrapper(self) -> None:
        # Every caller that says nothing gets what it always got.
        assert sandbox_env(_profile()) == sandbox_env(
            _profile(), inside_sandbox=False
        )

    def test_the_sidecar_allows_every_key_the_profile_can_produce(self) -> None:
        """The two lists, asserted against each other from the real code.

        This is the break that cost the first merge press: the deploy stage
        grew six settings and the sidecar's allowlist did not, so the sidecar
        the deploy runs through refused the deploy's own environment. The
        assertion is between the real ``sandbox_env`` and the real
        ``allowed_env_keys``, so it fails the moment they drift again.
        """
        profile = _profile()
        produced = set(sandbox_env(profile))
        allowed = allowed_env_keys(profile)
        assert produced - allowed == {"SANDBOX_ENV_FILE"}

    def test_the_one_key_deliberately_left_off_and_why(self) -> None:
        """``SANDBOX_ENV_FILE`` is refused on purpose, and this says so.

        It is the only one of the eleven that names a file of secrets — the
        one the wrapper hands to ``sbx --env-file`` when it creates the
        sandbox. Values are not checked here, so allowing the key would let a
        request choose which file on this box becomes a sandbox's environment.
        Nothing sends it: a deploy inside the sandbox is sent none of the
        eleven, and creating a factory-carrying sandbox is an attended,
        host-side command. If a repository ever needs it, it is added by a
        decision rather than by accident.
        """
        allowed = allowed_env_keys(_profile())
        assert "SANDBOX_ENV_FILE" not in allowed
        for name in ELEVEN:
            if name != "SANDBOX_ENV_FILE":
                assert name in allowed, name


# ---------------------------------------------------------------------------
# No sandbox block at all
# ---------------------------------------------------------------------------


class TestNoSandboxBlock:
    @pytest.mark.parametrize("inside", [True, False])
    def test_nothing_to_thread_either_way(self, inside: bool) -> None:
        assert sandbox_env(_profile(sandbox=False), inside_sandbox=inside) == {}

    def test_the_deploy_runbook_is_the_same_either_way(self) -> None:
        inside = _candidate_deploy(inside=True, sandbox=False)
        wrapper = _candidate_deploy(inside=False, sandbox=False)
        assert [dict(s.params) for s in inside.steps] == [
            dict(s.params) for s in wrapper.steps
        ]
        assert _params(wrapper, "deploy_compose")["extra_env"] == CANDIDATE_OVERLAY


# ---------------------------------------------------------------------------
# A refusal is not silent
# ---------------------------------------------------------------------------

REFUSAL = (
    "sidecar refused (HTTP 400): env key 'SANDBOX_SIDECAR_PUBLISH' is not "
    "allowlisted — deny by default. Allowed: API_TEST_BASE_URL, CANDIDATE"
)


def _step_with(payload: dict[str, Any] | None) -> Step:
    return Step(
        step_type="deploy_compose",
        params={},
        status=StepStatus.failed,
        sequence_index=0,
        result=None
        if payload is None
        else StepResult(
            exit_code=1,
            captured_output="",
            started_at=FIXED,
            completed_at=FIXED,
            payload=payload,
        ),
    )


class TestTheSidecarsOwnSentenceIsReadable:
    def test_a_refused_deploy_step_hands_back_the_sentence(self) -> None:
        step = _step_with({"exit_code": 1, "captured_output": REFUSAL})
        assert sidecar_refusal(step) == REFUSAL

    def test_a_refused_health_check_hands_back_the_sentence(self) -> None:
        step = _step_with(
            {
                "ran": [
                    {"script": "a.sh", "exit_code": 0, "captured_output": "ok"},
                    {"script": "b.sh", "exit_code": 1, "captured_output": REFUSAL},
                ]
            }
        )
        assert sidecar_refusal(step) == REFUSAL

    def test_a_sidecar_that_could_not_be_reached_counts_too(self) -> None:
        step = _step_with(
            {
                "exit_code": 1,
                "captured_output": "sidecar unreachable at http://127.0.0.1:8925: x",
            }
        )
        assert sidecar_refusal(step) == (
            "sidecar unreachable at http://127.0.0.1:8925: x"
        )

    def test_a_scripts_own_failure_is_not_mistaken_for_one(self) -> None:
        step = _step_with(
            {"exit_code": 1, "captured_output": "docker compose: no such image"}
        )
        assert sidecar_refusal(step) is None

    @pytest.mark.parametrize("payload", [None, {}, {"captured_output": "  "}])
    def test_nothing_to_read_is_nothing_said(self, payload: Any) -> None:
        assert sidecar_refusal(_step_with(payload)) is None

    def test_a_very_long_sentence_is_trimmed_to_one_readable_line(self) -> None:
        long_refusal = "sidecar refused (HTTP 400): " + ("KEY, " * 200)
        said = sidecar_refusal(_step_with({"captured_output": long_refusal}))
        assert said is not None
        assert said.startswith("sidecar refused (HTTP 400): KEY,")
        assert len(said) <= 400
