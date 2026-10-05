"""The deploy wrapper every sandbox deploys with, driven.

``deploy/sandbox-deploy.sh`` is shipped in ``forge.cli.deploy_templates``. It
brings a project's Docker Sandbox up, runs the project's own deploy script
inside it, and hands back that script's exit code unchanged. These tests used
to live with ``forge register-repo``, which wrote the wrapper into a
repository; the container set-up's register-repo (5 October 2026) writes
nothing into a project, so they drive the shipped file directly.

A fake ``sbx``, ``systemctl`` and ``docker`` first on PATH record every
argument and answer as told. No real sandbox, unit or daemon is touched.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import forge.cli.deploy_templates as templates

TEMPLATES = Path(templates.__file__).resolve().parent


# ---------------------------------------------------------------------------
# The wrapper, driven — with a fake sbx and a fake systemctl first on PATH
#
# The wrapper is the only script the deploy step runs. It brings the sandbox
# up, runs the repository's own deploy script inside it, and hands back that
# script's exit code unchanged. Every test below drives the real file the
# command writes; the two fakes record every argument they are given and
# answer as the test tells them to. No real sbx, no real systemctl, no
# sandbox, no daemon.
# ---------------------------------------------------------------------------


FAKE_SBX = """#!/usr/bin/env bash
# A stand-in for Docker's `sbx`, put first on PATH by the test. It writes down
# every argument it is given and answers the way the test told it to. It never
# creates, starts, stops or looks at a real sandbox, and it never runs the real
# tool: no test in this file goes anywhere near the sandbox daemon.
#
# It models the three forms the real 0.39.0 tool actually has:
#   sbx ls                                                  lists the sandboxes
#   sbx policy check network --sandbox NAME TARGET           read-only question
#   sbx policy allow network --sandbox NAME RULES            adds the rules
# and `sbx create shell ...` and `sbx exec ...`.
#
# THE ANSWER TO THE QUESTION IS THE EXIT CODE: 0 means the target is allowed,
# anything else means it is not. The test names the allowed targets in
# SBX_ALLOWED (comma separated); SBX_POLICY_CHECK_STATUS forces one answer for
# every target, which is how "the tool could not answer at all" is played.
printf '%s\\n' "$*" >> "$SBX_LOG"
case "$1" in
  ls)
    printf '%s\\n' "${SBX_LS:-}"
    ;;
  policy)
    if [ "$2 $3" = "check network" ]; then
      if [ -n "${SBX_POLICY_CHECK_STATUS:-}" ]; then
        exit "${SBX_POLICY_CHECK_STATUS}"
      fi
      target="${@: -1}"
      case ",${SBX_ALLOWED:-}," in
        *",${target},"*) exit 0 ;;
        *) exit 1 ;;
      esac
    fi
    ;;
  exec)
    exit "${SBX_EXEC_STATUS:-0}"
    ;;
esac
exit 0
"""

FAKE_SYSTEMCTL = """#!/usr/bin/env bash
# A stand-in for systemctl. It writes down what it was asked to do and does
# nothing: no unit is started, stopped or reloaded by any test in this file.
# The wrapper's question whether a unit is masked is answered "disabled" and
# not written down, so the log holds only what the wrapper did.
if [ "$2" = "show" ]; then
  printf 'disabled\\n'
  exit 0
fi
printf '%s\\n' "$*" >> "$SYSTEMCTL_LOG"
exit 0
"""

FAKE_DOCKER = """#!/usr/bin/env bash
# A stand-in for the machine's docker: the wrapper only asks whether a
# Compose sandbox supervisor exists, and here none does.
exit 0
"""


@pytest.fixture
def wrapper_repo(tmp_path):
    """A repository carrying the shipped wrapper, plus the three fakes."""
    repo = tmp_path / "bench-one"
    (repo / "deploy").mkdir(parents=True)
    shutil.copy(TEMPLATES / "sandbox-deploy.sh", repo / "deploy" / "sandbox-deploy.sh")
    (repo / "deploy" / "sandbox-deploy.sh").chmod(0o755)

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    (fake_bin / "sbx").write_text(FAKE_SBX, encoding="utf-8")
    (fake_bin / "systemctl").write_text(FAKE_SYSTEMCTL, encoding="utf-8")
    (fake_bin / "docker").write_text(FAKE_DOCKER, encoding="utf-8")
    for name in ("sbx", "systemctl", "docker"):
        (fake_bin / name).chmod(0o755)
    return repo, fake_bin


def _drive_wrapper(wrapper_repo, tmp_path, **env):
    """Run the wrapper with the fakes first on PATH; return (result, sbx, systemctl)."""
    repo, fake_bin = wrapper_repo
    sbx_log = tmp_path / "sbx.log"
    systemctl_log = tmp_path / "systemctl.log"
    sbx_log.write_text("", encoding="utf-8")
    systemctl_log.write_text("", encoding="utf-8")
    run_env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "SBX_LOG": str(sbx_log),
        "SYSTEMCTL_LOG": str(systemctl_log),
        "SANDBOX_NAME": "bench-one-deploy",
        "SANDBOX_MEMORY": "6g",
        "SANDBOX_CPUS": "4",
        "SANDBOX_PUBLISH": "127.0.0.1:8911:8911,127.0.0.1:8912:8912",
        "SANDBOX_ALLOW_NETWORK": "pypi.org,*.debian.org",
    }
    run_env.update({k: str(v) for k, v in env.items()})
    result = subprocess.run(
        [str(repo / "deploy" / "sandbox-deploy.sh")],
        cwd=repo,
        env=run_env,
        capture_output=True,
        text=True,
    )
    return (
        result,
        [line for line in sbx_log.read_text().splitlines() if line.strip()],
        [line for line in systemctl_log.read_text().splitlines() if line.strip()],
    )


def test_the_wrapper_creates_the_sandbox_when_it_is_not_there(wrapper_repo, tmp_path):
    result, sbx, _ = _drive_wrapper(wrapper_repo, tmp_path, SBX_LS="")

    assert result.returncode == 0, result.stderr
    created = [line for line in sbx if line.startswith("create ")]
    assert len(created) == 1
    assert "create shell" in created[0]
    assert "--name bench-one-deploy" in created[0]
    assert "--memory 6g" in created[0]
    assert "--cpus 4" in created[0]
    assert "--publish 127.0.0.1:8911:8911" in created[0]
    assert "--publish 127.0.0.1:8912:8912" in created[0]


def test_the_wrapper_does_not_create_a_sandbox_that_is_already_there(
    wrapper_repo, tmp_path
):
    result, sbx, _ = _drive_wrapper(
        wrapper_repo, tmp_path, SBX_LS="bench-one-deploy   running"
    )

    assert result.returncode == 0, result.stderr
    assert [line for line in sbx if line.startswith("create ")] == []


def test_a_similar_name_is_not_mistaken_for_this_sandbox(wrapper_repo, tmp_path):
    result, sbx, _ = _drive_wrapper(
        wrapper_repo, tmp_path, SBX_LS="bench-one-deploy-old   running"
    )

    assert result.returncode == 0, result.stderr
    assert len([line for line in sbx if line.startswith("create ")]) == 1


def test_the_network_rules_are_added_once_in_one_call(wrapper_repo, tmp_path):
    result, sbx, _ = _drive_wrapper(wrapper_repo, tmp_path, SBX_LS="")

    assert result.returncode == 0, result.stderr
    allowed = [line for line in sbx if line.startswith("policy allow ")]
    assert allowed == [
        "policy allow network --sandbox bench-one-deploy pypi.org,*.debian.org"
    ]


def test_each_address_is_asked_about_one_at_a_time(wrapper_repo, tmp_path):
    # The real tool judges a bare host name as if it were being reached over
    # HTTPS on port 443, but the Debian mirrors are fetched over plain HTTP, so
    # a bare host has to be asked about as an http:// address. An entry that
    # already names a port is asked about exactly as it is written.
    result, sbx, _ = _drive_wrapper(
        wrapper_repo,
        tmp_path,
        SBX_LS="bench-one-deploy   running",
        SANDBOX_ALLOW_NETWORK="pypi.org,192.0.2.53:4000",
        SBX_ALLOWED="http://pypi.org,192.0.2.53:4000",
    )

    assert result.returncode == 0, result.stderr
    assert [line for line in sbx if line.startswith("policy check ")] == [
        "policy check network --sandbox bench-one-deploy http://pypi.org",
        "policy check network --sandbox bench-one-deploy 192.0.2.53:4000",
    ]
    assert [line for line in sbx if line.startswith("policy allow ")] == []


def test_the_rules_are_not_added_again_when_they_are_already_allowed(
    wrapper_repo, tmp_path
):
    result, sbx, _ = _drive_wrapper(
        wrapper_repo,
        tmp_path,
        SBX_LS="bench-one-deploy   running",
        SBX_ALLOWED="http://pypi.org,http://*.debian.org",
    )

    assert result.returncode == 0, result.stderr
    assert [line for line in sbx if line.startswith("policy allow ")] == []


def test_a_missing_rule_means_the_whole_set_is_added(wrapper_repo, tmp_path):
    result, sbx, _ = _drive_wrapper(
        wrapper_repo,
        tmp_path,
        SBX_LS="bench-one-deploy   running",
        SBX_ALLOWED="http://pypi.org",
    )

    assert result.returncode == 0, result.stderr
    assert len([line for line in sbx if line.startswith("policy allow ")]) == 1


def test_rules_are_added_when_the_question_cannot_be_answered(
    wrapper_repo, tmp_path
):
    # A tool that cannot answer the question must not leave a sandbox walled
    # off — adding a rule that is already there changes nothing.
    result, sbx, _ = _drive_wrapper(
        wrapper_repo,
        tmp_path,
        SBX_LS="bench-one-deploy   running",
        SBX_POLICY_CHECK_STATUS="2",
    )

    assert result.returncode == 0, result.stderr
    assert len([line for line in sbx if line.startswith("policy allow ")]) == 1


def test_the_keeper_is_started_for_this_sandbox(wrapper_repo, tmp_path):
    result, _, systemctl = _drive_wrapper(wrapper_repo, tmp_path, SBX_LS="")

    assert result.returncode == 0, result.stderr
    assert systemctl == ["--user start forge-sandbox-keeper@bench-one-deploy"]


def test_the_deploy_script_runs_inside_with_exactly_the_named_settings(
    wrapper_repo, tmp_path
):
    repo, _ = wrapper_repo
    result, sbx, _ = _drive_wrapper(wrapper_repo, tmp_path, SBX_LS="")

    assert result.returncode == 0, result.stderr
    ran = [line for line in sbx if line.startswith("exec ")]
    assert len(ran) == 1
    assert ran[0] == (
        f"exec -w {repo} "
        "-e CANDIDATE -e PROMOTE -e REVERT -e CANDIDATE_DOWN "
        "-e CANDIDATE_PORT -e ROLLBACK_IMAGE_REF -e ENV_FILE "
        "bench-one-deploy deploy/deploy.sh"
    )


@pytest.mark.parametrize("status", ["0", "1", "2", "7"])
def test_the_inner_exit_code_comes_back_unchanged(wrapper_repo, tmp_path, status):
    result, _, _ = _drive_wrapper(
        wrapper_repo, tmp_path, SBX_LS="", SBX_EXEC_STATUS=status
    )

    assert result.returncode == int(status), result.stderr
    assert f"exited {status}" in result.stdout


def test_no_sandbox_name_means_it_refuses_and_touches_nothing(wrapper_repo, tmp_path):
    result, sbx, systemctl = _drive_wrapper(
        wrapper_repo, tmp_path, SBX_LS="", SANDBOX_NAME=""
    )

    assert result.returncode == 2
    assert "SANDBOX_NAME is not set" in result.stdout
    assert sbx == []
    assert systemctl == []


def test_no_network_rules_means_no_policy_call_at_all(wrapper_repo, tmp_path):
    result, sbx, _ = _drive_wrapper(
        wrapper_repo, tmp_path, SBX_LS="", SANDBOX_ALLOW_NETWORK=""
    )

    assert result.returncode == 0, result.stderr
    assert [line for line in sbx if line.startswith("policy ")] == []


def test_settings_left_empty_are_left_off_the_create(wrapper_repo, tmp_path):
    result, sbx, _ = _drive_wrapper(
        wrapper_repo,
        tmp_path,
        SBX_LS="",
        SANDBOX_MEMORY="",
        SANDBOX_CPUS="",
        SANDBOX_PUBLISH="",
    )

    assert result.returncode == 0, result.stderr
    created = [line for line in sbx if line.startswith("create ")][0]
    assert created == f"create shell {wrapper_repo[0]} --name bench-one-deploy"


# ---------------------------------------------------------------------------
# The wrapper is ONE file (rule 13 of the 15:10Z amendment)
#
# api_test and every repository born by this command deploy with the same
# wrapper, byte for byte. It holds no value belonging to any one repository:
# the sandbox's name, size, ports and rules all reach it in its environment.
#
# 2026-09-07: the lane that makes a sandbox carry the factory's own services
# (the deploy sidecar and the build runner, the spec's Part O rule 68) changes
# the shipped wrapper. forge ships it; a repository's copy is refreshed by
# copying the shipped file over it, and api_test's is refreshed at the
# attended go-live, because nothing in a build lane writes into another
# repository's checkout. So until that copy is made there are exactly two
# states this check accepts: the same bytes, or api_test still carrying the
# wrapper as it stood before this lane (the sha256 below). Anything else is
# real drift and fails.
# ---------------------------------------------------------------------------

#: api_test's wrapper as it stood before the sandbox carried the factory
#: (forge commit 74690a4's shipped file, byte for byte). This constant goes
#: when api_test's copy is refreshed at the go-live.
WRAPPER_BEFORE_THE_FACTORY_SHA256 = (
    "e40bb550b8531d0c98f4f9b0a0c81d9dea5cba1e766ecca10196dd46d13d8fd0"
)


def _api_test_wrapper() -> Path | None:
    """api_test's own wrapper, if a checkout of it sits beside this one."""
    estate = Path(__file__).resolve().parents[3].parent
    for checkout in ("api_test-wt-sandbox", "api_test"):
        candidate = estate / checkout / "deploy" / "sandbox-deploy.sh"
        if candidate.is_file():
            return candidate
    return None


def test_the_shipped_wrapper_is_api_test_s_wrapper_byte_for_byte():
    """The same bytes, or api_test still one refresh behind (see above)."""
    theirs = _api_test_wrapper()
    if theirs is None:
        pytest.skip(
            "no api_test checkout beside this one, so there is nothing to "
            "compare the shipped wrapper with"
        )
    ours = TEMPLATES / "sandbox-deploy.sh"
    if ours.read_bytes() == theirs.read_bytes():
        return
    behind = hashlib.sha256(theirs.read_bytes()).hexdigest()
    assert behind == WRAPPER_BEFORE_THE_FACTORY_SHA256, (
        f"{ours} and {theirs} have drifted apart; they are meant to be one "
        "file, and this is not the one refresh that is expected to be "
        f"outstanding — copy {ours} over {theirs}, or copy the other way if "
        "it is api_test's copy that moved on purpose"
    )
