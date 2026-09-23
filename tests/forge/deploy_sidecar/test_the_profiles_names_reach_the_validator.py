"""The names a project declares are the names the helper permits.

WHY THIS FILE EXISTS (25 September 2026, the third review of the executor
stage). A project declares, in its own ``deploy/profile.yaml``, the setting the
identity of what was checked is handed to its deploy step in, the setting that
artifact is handed back in, and the setting its read-only "what are you
running" step is asked with. The deploy stage read those names and put them in
the request. The helper service's environment door did not know about them at
all, so through the REAL route, with a real committed profile:

* the candidate check was refused ``400 — env key 'DEPLOY_IDENTITY' is not
  allowlisted``;
* the read-only question was refused ``400 — env key 'RUNNING_IDENTITY' is not
  allowlisted``.

Neither command started. Nothing had caught it because every drive of the
deploy had gone round that route rather than through it.

So these tests do two things. They check the rule — the permitted names are the
factory's own list plus the ones this project declared, each checked for shape
and against what the factory keeps for itself. And they check it AGAINST THE
COMMITTED PROFILE OF A REAL PROJECT, so that if the profile and the validator
ever disagree again, a test goes red here rather than a deploy going nowhere in
the factory.

Nothing here runs a process. The route is called with a runner that records
what it was asked to run and returns, so what is proved is the door's verdict.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from forge.config.models import ForgeConfig
from forge.deploy.profile import load_deploy_profile
from forge.deploy_sidecar.deploy_executor import ExecutorAnswer
from forge.deploy_sidecar.service import allowed_env_keys, process_run_request

#: A real project's committed profile, in the estate this factory serves. It is
#: READ, never written and never run: what is wanted from it is the names it
#: declares, exactly as they are committed. Where it lives is this machine's
#: business, not this repository's: the path comes from one setting, and with
#: no setting the test skips. (A default path used to be written here; this
#: repository is public, and a machine's own layout does not belong in it.)
import os

A_COMMITTED_PROFILE = Path(os.environ.get("FORGE_TEST_COMMITTED_PROFILE", ""))


def _the_committed_profile() -> Path:
    """The profile's path, or a skip: every reader of it goes through here."""
    if not str(A_COMMITTED_PROFILE) or not A_COMMITTED_PROFILE.is_file():
        pytest.skip(
            "set FORGE_TEST_COMMITTED_PROFILE to a project's committed "
            "deploy/profile.yaml to run this against a real one"
        )
    return A_COMMITTED_PROFILE


def _profile_text() -> str:
    return _the_committed_profile().read_text(encoding="utf-8")


def _git(where: Path, *args: str) -> str:
    done = subprocess.run(
        [
            "git",
            "-c", "user.email=tests@example.invalid",
            "-c", "user.name=tests",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        cwd=str(where),
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def _a_project(
    tmp_path: Path,
    profile_text: str,
    *,
    declares: tuple[str, ...] = (),
    committed: bool = True,
) -> Path:
    """A repository whose ``deploy/profile.yaml`` is the given text.

    ``declares`` is what that project says its own builds need, written into
    its own ``.guardkit/config.yaml``. Since 23 September 2026 that file and
    the profile's identity block are the ONLY things that widen the helper's
    environment door: a request presenting a name the project has not declared
    is refused and nothing starts.

    AND SINCE THE LAST CHANGE A DECLARATION IS A COMMITTED LINE, so this writes
    the project's history as well as its files: the helper reads both of them
    out of a commit and never off the disk, and a copy laid out as a plain
    directory declares nothing at all. ``committed=False`` builds exactly that
    plain directory, for the one test below that is about it.
    """
    repo = tmp_path / "a-project"
    (repo / "deploy").mkdir(parents=True)
    (repo / "deploy" / "profile.yaml").write_text(profile_text, encoding="utf-8")
    if declares:
        (repo / ".guardkit").mkdir(parents=True, exist_ok=True)
        (repo / ".guardkit" / "config.yaml").write_text(
            "launch:\n  settings: [" + ", ".join(declares) + "]\n", encoding="utf-8"
        )
    if committed:
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "the project as it is")
    return repo


def _config(repo: Path) -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {"target_repo_paths": {"bench/a-project": str(repo)}},
        }
    )


class _Ran:
    """A script runner that records instead of running anything."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> tuple[int, str]:
        self.calls.append(dict(kwargs))
        return 0, "the script would have run here"


class TestTheCommittedProfilesOwnNames:
    """Every setting this project declares is permitted, by name."""

    def test_the_identity_settings_are_permitted(self) -> None:
        profile = load_deploy_profile(_the_committed_profile())
        declared = yaml.safe_load(_profile_text()).get("identity") or {}
        permitted = allowed_env_keys(profile)
        for key in ("setting", "artifact_setting", "asked_with"):
            name = str(declared.get(key) or "").strip()
            assert name, f"this profile declares no {key}"
            assert name in permitted, (
                f"the profile declares {key}={name!r} and the helper does not "
                "permit it — the two halves of the factory disagree"
            )

    def test_the_markers_are_NOT_permitted_as_settings(self) -> None:
        """A marker names a line the step PRINTS; it is never handed to one.

        Two of this project's names happen to be the same word in both roles
        (``RUNNING_IDENTITY`` is both what the step is asked with and what it
        answers under), so the one that proves the rule is the check's marker.
        """
        profile = load_deploy_profile(_the_committed_profile())
        declared = yaml.safe_load(_profile_text()).get("identity") or {}
        settings = {
            str(declared.get(k) or "")
            for k in ("setting", "artifact_setting", "asked_with")
        }
        permitted = allowed_env_keys(profile)
        for key in ("reported_as", "checked_as", "running_as"):
            name = str(declared.get(key) or "").strip()
            if not name or name in settings:
                continue
            assert name not in permitted, (
                f"{name} is a marker the step prints, not a setting it is "
                "handed, and permitting it would widen the door for nothing"
            )


class TestTheRealRouteWithTheCommittedProfile:
    """``process_run_request`` itself, with the project's own names on it."""

    def _ask(
        self, tmp_path: Path, env: dict[str, str], *, script: str = "deploy/deploy.sh"
    ) -> tuple[int, dict]:
        repo = _a_project(tmp_path, _profile_text())
        ran = _Ran()
        return process_run_request(
            {
                "repo": "bench/a-project",
                "script": script,
                "env": env,
                "timeout_seconds": 5,
            },
            config=_config(repo),
            script_runner=ran,
            inside_sandbox=True,
        )

    def test_the_candidate_check_is_not_refused_for_its_identity_setting(
        self, tmp_path: Path
    ) -> None:
        declared = yaml.safe_load(_profile_text()).get("identity") or {}
        status, body = self._ask(
            tmp_path,
            {
                "CANDIDATE": "1",
                str(declared["setting"]): "j-0123456789ab@ffff",
            },
        )
        assert status == 200, body
        assert "error" not in body

    def test_the_promote_is_not_refused_for_the_artifact_setting(
        self, tmp_path: Path
    ) -> None:
        declared = yaml.safe_load(_profile_text()).get("identity") or {}
        status, body = self._ask(
            tmp_path,
            {
                "PROMOTE": "1",
                str(declared["setting"]): "j-0123456789ab@ffff",
                str(declared["artifact_setting"]): "an-artifact-of-its-own",
            },
        )
        assert status == 200, body
        assert "error" not in body

    def test_the_read_only_question_is_not_refused(self, tmp_path: Path) -> None:
        declared = yaml.safe_load(_profile_text()).get("identity") or {}
        status, body = self._ask(tmp_path, {str(declared["asked_with"]): "1"})
        assert status == 200, body
        assert "error" not in body

    def test_a_name_the_project_did_not_declare_is_still_refused(
        self, tmp_path: Path
    ) -> None:
        status, body = self._ask(tmp_path, {"SOMETHING_NOBODY_DECLARED": "1"})
        assert status == 400
        assert "not allowlisted" in body["error"]

    def test_a_project_cannot_declare_one_of_the_factorys_own_names(
        self, tmp_path: Path
    ) -> None:
        """Declaring a reserved name does not open the door onto it."""
        text = _profile_text().replace(
            "  setting: DEPLOY_IDENTITY", "  setting: FORGE_LEDGER_PATH"
        )
        assert "FORGE_LEDGER_PATH" in text
        repo = _a_project(tmp_path, text)
        assert "FORGE_LEDGER_PATH" not in allowed_env_keys(
            load_deploy_profile(repo / "deploy" / "profile.yaml")
        )
        status, body = process_run_request(
            {
                "repo": "bench/a-project",
                "script": "deploy/deploy.sh",
                "env": {"FORGE_LEDGER_PATH": "/somewhere/of/my/own"},
                "timeout_seconds": 5,
            },
            config=_config(repo),
            script_runner=_Ran(),
            inside_sandbox=True,
        )
        assert status == 400
        assert "not allowlisted" in body["error"]


class _WouldHaveDeployed:
    """An executor that records the request instead of starting anything."""

    def __init__(self) -> None:
        self.asked: list[object] = []

    def run(self, request: object) -> ExecutorAnswer:
        self.asked.append(request)
        return ExecutorAnswer(
            accepted=True,
            word="the-deploy-command-ran",
            sentence="the command would have run here",
            exit_code=0,
        )


class TestTheDeployBlocksSettingNamesToo:
    """The other half of the same rule (26 September, the fourth review).

    The env door above was connected to this project's declaration and the
    ``deploy`` block was not, so a name the door refused was accepted through
    the block and went into the environment of the one command that deploys
    the live thing. Both halves read the SAME declaration now, and this is
    the test that goes red if they ever drift apart again.
    """

    def _deploy(
        self, tmp_path: Path, block: dict
    ) -> tuple[int, dict, _WouldHaveDeployed]:
        repo = _a_project(tmp_path, _profile_text())
        executor = _WouldHaveDeployed()
        status, body = process_run_request(
            {
                "repo": "bench/a-project",
                "script": "deploy/deploy.sh",
                "env": {"PROMOTE": "1"},
                "timeout_seconds": 5,
                "deploy": {
                    "target": "a-project::live",
                    "target_counter": 1,
                    "build": "build-a",
                    **block,
                },
            },
            config=_config(repo),
            script_runner=_Ran(),
            deploy_executor=executor,
            inside_sandbox=True,
        )
        return status, body, executor

    def test_the_names_this_project_declares_are_accepted(
        self, tmp_path: Path
    ) -> None:
        declared = yaml.safe_load(_profile_text()).get("identity") or {}
        status, body, executor = self._deploy(
            tmp_path,
            {
                "identity": "j-0123456789ab@ffff",
                "identity_setting": str(declared["setting"]),
                "artifact": "an-artifact-of-its-own",
                "artifact_setting": str(declared["artifact_setting"]),
            },
        )
        assert status == 200, body
        assert executor.asked, "the deploy never reached the executor"

    def test_a_name_this_project_never_declared_is_refused(
        self, tmp_path: Path
    ) -> None:
        status, body, executor = self._deploy(
            tmp_path,
            {
                "identity": "j-0123456789ab@ffff",
                "identity_setting": "SOMETHING_NOBODY_DECLARED",
            },
        )
        assert status == 400
        assert "does not declare that name" in body["error"]
        assert not executor.asked, "nothing may start on a refused request"

    def test_one_of_the_factorys_own_names_is_refused(self, tmp_path: Path) -> None:
        status, body, executor = self._deploy(
            tmp_path,
            {"identity": "j-0123456789ab@ffff", "identity_setting": "PATH"},
        )
        assert status == 400
        assert "is one of the settings this factory sets itself" in body["error"]
        assert not executor.asked

    def test_a_marker_is_refused_here_as_well(self, tmp_path: Path) -> None:
        """A marker names a line the step prints; it is never handed to one."""
        declared = yaml.safe_load(_profile_text()).get("identity") or {}
        marker = str(declared["checked_as"])
        status, body, executor = self._deploy(
            tmp_path,
            {"identity": "j-0123456789ab@ffff", "identity_setting": marker},
        )
        assert status == 400
        assert marker in body["error"]
        assert not executor.asked


class TestADeclaredLaunchSettingOnTheRequest:
    """And the request cannot widen the door on its own say-so.

    Rewritten 23 September 2026. The first of these used to pass with no
    declaration anywhere: a name of the right shape on the request was enough.
    It is not any more — the project's own file has to name it.

    Rewritten again 23 September 2026, and this is the fault the review of the
    last change found. The rule became "a declaration is a COMMITTED line",
    and the project these tests build was a plain directory with no history,
    so the declaration written into it was no declaration at all and the first
    test below went red. The project is committed now, which is what the rule
    asks of a real one; the plain directory has its own test, because what
    happens to a copy with no history is worth pinning down rather than
    leaving as a surprise.
    """

    def _ask(self, repo: Path, body: dict) -> tuple[int, dict]:
        ran = _Ran()
        status, answer = process_run_request(
            {
                "repo": "bench/a-project",
                "script": "deploy/deploy.sh",
                "env": {"A_PROJECT_TOOL_HOME": "somewhere"},
                "timeout_seconds": 5,
                **body,
            },
            config=_config(repo),
            script_runner=ran,
            inside_sandbox=True,
        )
        return status, {**answer, "_started": bool(ran.calls)}

    def test_it_is_permitted_as_an_env_key_too(self, tmp_path: Path) -> None:
        repo = _a_project(
            tmp_path, _profile_text(), declares=("A_PROJECT_TOOL_HOME",)
        )
        status, body = self._ask(repo, {"launch_settings": ["A_PROJECT_TOOL_HOME"]})
        assert status == 200, body
        assert body["_started"]

    def test_a_copy_with_no_history_declares_nothing_and_says_so(
        self, tmp_path: Path
    ) -> None:
        """The same declaration, in a copy that has no commits in it.

        A declaration is a committed line, so a copy with nowhere to commit to
        has declared nothing, and every request naming a setting is refused
        with nothing started. It is the refusal SENTENCE that matters here: it
        names the copy and says it carries no committed history, rather than
        telling a person to commit two lines to a history that is not there.
        """
        repo = _a_project(
            tmp_path,
            _profile_text(),
            declares=("A_PROJECT_TOOL_HOME",),
            committed=False,
        )
        status, body = self._ask(repo, {"launch_settings": ["A_PROJECT_TOOL_HOME"]})
        assert status == 400, body
        assert "carries no committed history" in body["error"], body["error"]
        assert "a declaration is a committed line" in body["error"], body["error"]
        assert str(repo) in body["error"]
        assert not body["_started"], "nothing may start on a refused request"

    def test_the_request_cannot_declare_it_on_the_projects_behalf(
        self, tmp_path: Path
    ) -> None:
        repo = _a_project(tmp_path, _profile_text())
        status, body = self._ask(repo, {"launch_settings": ["A_PROJECT_TOOL_HOME"]})
        assert status == 400
        assert "A_PROJECT_TOOL_HOME" in body["error"]
        assert "does not declare that name" in body["error"]
        assert not body["_started"], "nothing may start on a refused request"

    def test_and_is_refused_without_the_declaration(self, tmp_path: Path) -> None:
        repo = _a_project(tmp_path, _profile_text())
        status, body = self._ask(repo, {})
        assert status == 400
        assert "not allowlisted" in body["error"]
        assert not body["_started"]
