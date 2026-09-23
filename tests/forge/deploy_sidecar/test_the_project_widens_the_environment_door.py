"""The environment door is the PROJECT'S to widen, never the REQUEST'S.

WHY THIS FILE EXISTS (23 September 2026, the coordinator's decision). The
helper service builds a child's environment from the factory's own short named
list plus the setting NAMES carried on the request under ``launch_settings``.
Until now a name on the request was admitted on its own say-so: anything of the
right shape that was not one of the names the factory keeps for itself went
straight in, and its value came out of the helper's own process. That is how an
independent probe passed its fake tools' settings to a real deploy, and it
means whoever can send this service a request can hand a build any setting the
service happens to be holding.

The rule now: a name the request presents is permitted ONLY if the project
itself declares it, in its own committed files —

* the ``launch: settings:`` block of its ``.guardkit/config.yaml``;
* the setting names its ``deploy/profile.yaml`` declares in its ``identity``
  block (the setting the identity is handed in, the setting the artifact is
  handed back in, and the setting its read-only question is asked with).

The helper reads those at a COMMIT (26 September 2026): the recorded commit the
work starts from, sent on the request as ``declared_at``, and with none, the
committed HEAD of the copy of the project it has. Never the working tree — a
line a build writes into the checkout the command is about to run out of is not
a declaration. A name the project does not declare is refused in plain words
and nothing starts. The reserved list still wins: a project that declares one
of the factory's own names gets nothing from it.

NOTHING LIVE IS TOUCHED HERE. The service is this same process's own test
server on 127.0.0.1, on a port the kernel picks; the project is a throwaway
directory; the "deploy script" is a child of the test that prints the NAMES of
the settings it was given and nothing else. No image is built, no sandbox is
made, no credential exists anywhere here — the planted values are strings
written in this file.
"""

from __future__ import annotations

import contextlib
import json
import stat
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml

from forge.config.models import ForgeConfig
from forge.deploy.profile import load_deploy_profile
from forge.deploy_sidecar.service import (
    COORDINATOR_OWNER_ENV,
    build_server,
    project_declared_settings,
)
from forge.pipeline.deployment_identity import declared_identity

REPO = "org/widget-shop"

#: What the project declares its own builds need, by name.
DECLARED_SETTING = "SOME_TOOL_CACHE"
DECLARED_VALUE = "/var/cache/some-tool"

#: A name of perfectly good shape that the project declares NOWHERE. The old
#: door admitted it; this one refuses it.
UNDECLARED_SETTING = "ANOTHER_TOOL_HOME"
UNDECLARED_VALUE = "/opt/another-tool"

#: One of the factory's own, planted in this process the way the real service
#: holds it. A project may declare it in its file all it likes.
A_RESERVED_SETTING = "FORGE_DB_PATH"

#: The script the profile names, and where its answer lands.
THE_SCRIPT = "deploy/say-what-i-was-given.sh"


def _child(root: Path) -> Path:
    """A child that PRINTS the names of the settings it was given.

    Names only, one per line, sorted. A value is never printed: the point of
    the door is which names arrive, and a printed value is a second place one
    could hide.
    """
    path = root / THE_SCRIPT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "for name in sorted(os.environ):\n"
        "    print('SETTING ' + name)\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _a_project(
    root: Path,
    *,
    declares: tuple[str, ...] = (),
    identity: dict[str, str] | None = None,
) -> Path:
    """A throwaway project: one deploy profile, one declaration, one script."""
    root.mkdir(parents=True, exist_ok=True)
    _child(root)
    profile: dict[str, Any] = {
        "env_id": "widgetshop",
        "compose": {"file": "docker-compose.yml", "script": THE_SCRIPT},
        "cwd": str(root),
    }
    if identity:
        profile["identity"] = identity
    (root / "deploy" / "profile.yaml").write_text(
        yaml.safe_dump(profile), encoding="utf-8"
    )
    declaration = root / ".guardkit" / "config.yaml"
    declaration.parent.mkdir(parents=True, exist_ok=True)
    if declares:
        declaration.write_text(
            "launch:\n  settings: [" + ", ".join(declares) + "]\n", encoding="utf-8"
        )
    else:
        declaration.write_text("toolchain:\n  test: qa/run\n", encoding="utf-8")
    # AND COMMITTED, because a declaration is a committed line. The helper
    # reads both files out of this history, never off the disk.
    _commit_everything(root)
    return root


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


def _commit_everything(root: Path, message: str = "the project as it is") -> str:
    """Put everything in ``root`` into a commit; answer that commit."""
    if not (root / ".git").exists():
        _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "--allow-empty", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def _config(repo: Path) -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": [str(repo.parent)]}},
            "planning": {"target_repo_paths": {REPO: str(repo)}},
        }
    )


@pytest.fixture(autouse=True)
def _plant(monkeypatch: pytest.MonkeyPatch) -> None:
    """The helper's own process holds the values these names would carry."""
    monkeypatch.setenv(DECLARED_SETTING, DECLARED_VALUE)
    monkeypatch.setenv(UNDECLARED_SETTING, UNDECLARED_VALUE)
    monkeypatch.setenv(A_RESERVED_SETTING, "/somewhere/the/ledger/lives")


def _serving(config: ForgeConfig):
    server = build_server(port=0, config_loader=lambda: config)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"


def _run(base_url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """POST ``/run`` to the REAL handler and read the answer back."""
    request = urllib.request.Request(
        base_url + "/run",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as answer:
            return answer.status, json.loads(answer.read().decode("utf-8"))
    except urllib.error.HTTPError as refused:  # a 4xx is an answer, not a fault
        return refused.code, json.loads(refused.read().decode("utf-8"))


def _ask(project: Path, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    server, base_url = _serving(_config(project))
    try:
        return _run(
            base_url,
            {
                "repo": REPO,
                "script": THE_SCRIPT,
                "timeout_seconds": 30,
                **body,
            },
        )
    finally:
        server.shutdown()
        server.server_close()


#: The build these requests say they are for.
THE_BUILD = "build-0001"


class _TheCoordinatorsAnswer(BaseHTTPRequestHandler):
    """A stand-in for the coordinator's own READ-ONLY answer.

    It answers one question — what commit did you record this build as
    starting from — out of a plain mapping written in the test. It is a child
    of this process on 127.0.0.1 on a port the kernel picks; no real
    coordinator, ledger or service is anywhere near it.
    """

    records: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 — the base class's spelling
        from urllib.parse import parse_qs, urlparse

        asked = parse_qs(urlparse(self.path).query)
        build = (asked.get("build") or [""])[0]
        answer: dict[str, Any] = {}
        if build in self.records:
            answer = {"build": build, "start_commit": self.records[build]}
        payload = json.dumps(answer).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:  # noqa: D102 — quiet in tests
        return


@contextlib.contextmanager
def _a_coordinator_that_recorded(
    records: dict[str, str], monkeypatch: pytest.MonkeyPatch
):
    """Point the helper at a stand-in coordinator holding ``records``."""
    handler = type("_Answer", (_TheCoordinatorsAnswer,), {"records": dict(records)})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    monkeypatch.setenv(COORDINATOR_OWNER_ENV, f"http://{host}:{port}/what-did-you-record")
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()


@contextlib.contextmanager
def _no_coordinator(monkeypatch: pytest.MonkeyPatch):
    """No route configured at all — the estate before the rollout."""
    monkeypatch.delenv(COORDINATOR_OWNER_ENV, raising=False)
    yield


def _names_the_child_was_given(body: dict[str, Any]) -> set[str]:
    return {
        line[len("SETTING ") :].strip()
        for line in str(body.get("output_tail") or "").splitlines()
        if line.startswith("SETTING ")
    }


class TestWhatTheProjectDeclares:
    """The reader itself: two files, one answer, markers left out."""

    def test_the_launch_block_and_the_profiles_identity_names(
        self, tmp_path: Path
    ) -> None:
        project = _a_project(
            tmp_path / "widget-shop",
            declares=(DECLARED_SETTING,),
            identity={
                "setting": "DEPLOY_IDENTITY",
                "reported_as": "DEPLOYED_IDENTITY",
                "checked_as": "CHECKED_ARTIFACT",
                "artifact_setting": "DEPLOY_ARTIFACT",
                "asked_with": "RUNNING_IDENTITY",
                "running_as": "RUNNING_IDENTITY",
            },
        )
        declared, note, where = project_declared_settings(project)
        assert note is None
        assert "committed HEAD" in where
        assert set(declared) == {
            DECLARED_SETTING,
            "DEPLOY_IDENTITY",
            "DEPLOY_ARTIFACT",
            "RUNNING_IDENTITY",
        }
        assert "CHECKED_ARTIFACT" not in declared, (
            "a marker names a line the step PRINTS, not a setting it is handed"
        )

    def test_a_project_that_declares_nothing_adds_nothing_of_its_own(
        self, tmp_path: Path
    ) -> None:
        """Only what the environment door already permitted such a project.

        A profile with no ``identity`` block reads as the factory's own two
        defaults there, which is exactly what ``allowed_env_keys`` has always
        permitted for it. Nothing the project itself said is added, because it
        said nothing.
        """
        project = _a_project(tmp_path / "widget-shop")
        declared, note, where = project_declared_settings(project)
        assert note is None
        assert DECLARED_SETTING not in declared
        identity = declared_identity(
            load_deploy_profile(project / "deploy" / "profile.yaml")
        )
        assert identity.declared is False
        assert set(declared) == {identity.setting, identity.artifact_setting}

    def test_the_factorys_own_names_are_never_taken_from_a_declaration(
        self, tmp_path: Path
    ) -> None:
        """A declaration naming one of them is unusable as a whole.

        The same answer the coordinator gets from the same file: a project
        whose ``launch:`` block names something this factory keeps for itself
        has not declared anything, and the note says so rather than the
        request being told the project "does not declare that name".
        """
        project = _a_project(
            tmp_path / "widget-shop", declares=(A_RESERVED_SETTING, DECLARED_SETTING)
        )
        declared, note, where = project_declared_settings(project)
        assert A_RESERVED_SETTING not in declared
        assert DECLARED_SETTING not in declared
        assert note is not None and "cannot use" in note


class TestADeclarationIsACommittedLine:
    """Read at a commit, never off the disk (26 September 2026).

    Both declaration files used to be read as they are in the working copy the
    helper was pointed at, so an uncommitted line counted as a declaration —
    including one a build had just written into the very checkout the command
    was about to run out of, which let a build widen its own door.
    """

    def test_a_name_only_in_the_working_tree_is_refused(self, tmp_path: Path) -> None:
        project = _a_project(tmp_path / "widget-shop", declares=(DECLARED_SETTING,))
        (project / ".guardkit" / "config.yaml").write_text(
            "launch:\n  settings: [" + DECLARED_SETTING + ", " + UNDECLARED_SETTING
            + "]\n",
            encoding="utf-8",
        )
        declared, _note, where = project_declared_settings(project)
        assert DECLARED_SETTING in declared
        assert UNDECLARED_SETTING not in declared
        assert "committed HEAD" in where
        status, body = _ask(project, {"launch_settings": [UNDECLARED_SETTING]})
        assert status == 400, body
        assert "an uncommitted line in a working copy is not a declaration" in (
            body["error"]
        )

    def test_the_same_name_committed_at_the_recorded_commit_is_admitted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _a_project(tmp_path / "widget-shop", declares=(DECLARED_SETTING,))
        (project / ".guardkit" / "config.yaml").write_text(
            "launch:\n  settings: [" + DECLARED_SETTING + ", " + UNDECLARED_SETTING
            + "]\n",
            encoding="utf-8",
        )
        started_from = _commit_everything(project, "and the second name")
        declared, note, where = project_declared_settings(
            project, commit=started_from
        )
        assert note is None
        assert UNDECLARED_SETTING in declared
        assert started_from in where
        with _a_coordinator_that_recorded({THE_BUILD: started_from}, monkeypatch):
            status, body = _ask(
                project,
                {
                    "launch_settings": [UNDECLARED_SETTING],
                    "build": THE_BUILD,
                    "declared_at": started_from,
                },
            )
        assert status == 200, body
        assert UNDECLARED_SETTING in _names_the_child_was_given(body)

    def test_a_commit_that_lacks_it_is_refused_even_though_HEAD_has_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The coordinator recorded THE OLDER commit, and the name is not there.

        This is the commit-binding working the other way round from the test
        below it: the request and the record agree, so the read is made — at a
        commit that simply does not carry the name, and the refusal says where
        it looked.
        """
        project = _a_project(tmp_path / "widget-shop", declares=(DECLARED_SETTING,))
        first = _git(project, "rev-parse", "HEAD")
        (project / ".guardkit" / "config.yaml").write_text(
            "launch:\n  settings: [" + DECLARED_SETTING + ", " + UNDECLARED_SETTING
            + "]\n",
            encoding="utf-8",
        )
        _commit_everything(project, "and the second name")
        with _a_coordinator_that_recorded({THE_BUILD: first}, monkeypatch):
            status, body = _ask(
                project,
                {
                    "launch_settings": [UNDECLARED_SETTING],
                    "build": THE_BUILD,
                    "declared_at": first,
                },
            )
        assert status == 400, body
        assert first in body["error"]
        assert "the commit this work starts from" in body["error"]
        # And at HEAD it is there, so the refusal is about the commit and not
        # about the name.
        at_head, _note, _where = project_declared_settings(project)
        assert UNDECLARED_SETTING in at_head

    def test_a_commit_of_the_wrong_shape_is_refused_before_git_is_started(
        self, tmp_path: Path
    ) -> None:
        project = _a_project(tmp_path / "widget-shop", declares=(DECLARED_SETTING,))
        status, body = _ask(
            project,
            {"launch_settings": [DECLARED_SETTING], "declared_at": "--upload-pack=x"},
        )
        assert status == 400, body
        assert "declared_at" in body["error"]


class TestTheRealRouteWithARealChild:
    """The whole way through: the HTTP handler, the runner and the child."""

    def test_a_name_the_project_declares_arrives(self, tmp_path: Path) -> None:
        project = _a_project(tmp_path / "widget-shop", declares=(DECLARED_SETTING,))
        status, body = _ask(project, {"launch_settings": [DECLARED_SETTING]})
        assert status == 200, body
        assert body["exit_code"] == 0, body
        given = _names_the_child_was_given(body)
        assert DECLARED_SETTING in given
        assert UNDECLARED_SETTING not in given

    def test_the_same_name_undeclared_is_refused_and_nothing_starts(
        self, tmp_path: Path
    ) -> None:
        project = _a_project(tmp_path / "widget-shop", declares=(DECLARED_SETTING,))
        status, body = _ask(project, {"launch_settings": [UNDECLARED_SETTING]})
        assert status == 400, body
        assert UNDECLARED_SETTING in body["error"]
        assert "does not declare that name" in body["error"]
        assert ".guardkit/config.yaml" in body["error"]
        assert "deploy/profile.yaml" in body["error"]
        assert "Nothing was started." in body["error"]
        # NOTHING STARTED: a refusal carries no exit code and no output at all.
        assert "exit_code" not in body and "output_tail" not in body

    def test_a_reserved_name_a_project_declares_is_still_refused(
        self, tmp_path: Path
    ) -> None:
        project = _a_project(
            tmp_path / "widget-shop", declares=(A_RESERVED_SETTING, DECLARED_SETTING)
        )
        status, body = _ask(project, {"launch_settings": [A_RESERVED_SETTING]})
        assert status == 400, body
        assert A_RESERVED_SETTING in body["error"]
        assert "keeps for itself" in body["error"]
        assert "exit_code" not in body

    def test_no_launch_settings_is_the_factorys_list_only(
        self, tmp_path: Path
    ) -> None:
        """The behaviour every caller written before these fields asks for."""
        project = _a_project(tmp_path / "widget-shop", declares=(DECLARED_SETTING,))
        status, body = _ask(project, {})
        assert status == 200, body
        given = _names_the_child_was_given(body)
        assert "PATH" in given, "the factory's own list is still handed over"
        for name in (DECLARED_SETTING, UNDECLARED_SETTING, A_RESERVED_SETTING):
            assert name not in given, (
                f"{name} reached the child although the request named nothing"
            )


class TestTheCommitIsBoundToTheRecord:
    """A request does not choose the commit its own declarations are read at.

    WHY THIS EXISTS (27 September 2026, Codex's requirement of the 23rd).
    Reading a project's declarations at a COMMIT rather than off the working
    copy closed the larger hole, and left a smaller one of the same shape: the
    commit came off the request. Whoever could send a request could name a
    commit at which the project declared a setting it does not declare now, and
    the door opened on that commit's own say-so.

    The helper cannot see the ledger, so it cannot look the answer up. What it
    can do is refuse to take it from the thing being checked: the request says
    which BUILD it is for, and the pair — that build, that commit — is
    confirmed with the coordinator's own read-only answer before a line is
    read. The coordinator here is a stand-in: a child of this process on
    127.0.0.1, on a port the kernel picks, answering out of a mapping written
    in this file.
    """

    @staticmethod
    def _two_commits(tmp_path: Path) -> tuple[Path, str, str]:
        """A project whose older commit lacks the declaration and whose HEAD has it."""
        project = _a_project(tmp_path / "widget-shop", declares=(DECLARED_SETTING,))
        older = _git(project, "rev-parse", "HEAD")
        (project / ".guardkit" / "config.yaml").write_text(
            "launch:\n  settings: [" + DECLARED_SETTING + ", " + UNDECLARED_SETTING
            + "]\n",
            encoding="utf-8",
        )
        head = _commit_everything(project, "and the second name")
        return project, older, head

    def test_bound_to_head_and_confirmed_is_admitted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project, _older, head = self._two_commits(tmp_path)
        with _a_coordinator_that_recorded({THE_BUILD: head}, monkeypatch):
            status, body = _ask(
                project,
                {
                    "launch_settings": [UNDECLARED_SETTING],
                    "build": THE_BUILD,
                    "declared_at": head,
                },
            )
        assert status == 200, body
        assert UNDECLARED_SETTING in _names_the_child_was_given(body)

    def test_the_older_commit_with_the_coordinator_saying_head_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The heart of it: the request names one commit, the record another."""
        project, older, head = self._two_commits(tmp_path)
        with _a_coordinator_that_recorded({THE_BUILD: head}, monkeypatch):
            status, body = _ask(
                project,
                {
                    "launch_settings": [UNDECLARED_SETTING],
                    "build": THE_BUILD,
                    "declared_at": older,
                },
            )
        assert status == 400, body
        # The sentence NAMES THE MISMATCH: both commits and the build.
        assert older in body["error"]
        assert head in body["error"]
        assert THE_BUILD in body["error"]
        assert "does not choose the commit" in body["error"]
        # NOTHING WAS READ and nothing was started.
        assert "exit_code" not in body and "output_tail" not in body

    def test_an_unrelated_commit_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project, _older, head = self._two_commits(tmp_path)
        unrelated = "0" * 40
        with _a_coordinator_that_recorded({THE_BUILD: head}, monkeypatch):
            status, body = _ask(
                project,
                {
                    "launch_settings": [UNDECLARED_SETTING],
                    "build": THE_BUILD,
                    "declared_at": unrelated,
                },
            )
        assert status == 400, body
        assert unrelated in body["error"]
        assert "exit_code" not in body

    def test_a_commit_with_no_build_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A request cannot establish its own authority."""
        project, _older, head = self._two_commits(tmp_path)
        with _a_coordinator_that_recorded({THE_BUILD: head}, monkeypatch):
            status, body = _ask(
                project,
                {"launch_settings": [UNDECLARED_SETTING], "declared_at": head},
            )
        assert status == 400, body
        assert "cannot establish its own authority" in body["error"]
        assert "exit_code" not in body

    def test_no_coordinator_route_and_a_commit_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project, _older, head = self._two_commits(tmp_path)
        with _no_coordinator(monkeypatch):
            status, body = _ask(
                project,
                {
                    "launch_settings": [UNDECLARED_SETTING],
                    "build": THE_BUILD,
                    "declared_at": head,
                },
            )
        assert status == 400, body
        assert "no way to ask the coordinator" in body["error"]
        assert COORDINATOR_OWNER_ENV in body["error"]
        assert "exit_code" not in body

    def test_a_coordinator_that_knows_nothing_of_the_build_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project, _older, head = self._two_commits(tmp_path)
        with _a_coordinator_that_recorded({"some-other-build": head}, monkeypatch):
            status, body = _ask(
                project,
                {
                    "launch_settings": [UNDECLARED_SETTING],
                    "build": THE_BUILD,
                    "declared_at": head,
                },
            )
        assert status == 400, body
        assert "did not say what commit it recorded" in body["error"]

    def test_a_build_with_no_commit_is_bound_to_what_the_coordinator_recorded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bound from the other end: the record decides, the request says nothing.

        The coordinator recorded the OLDER commit for this build, and the name
        is only at HEAD — so it is refused although the working copy, HEAD and
        every other reading would have admitted it.
        """
        project, older, _head = self._two_commits(tmp_path)
        with _a_coordinator_that_recorded({THE_BUILD: older}, monkeypatch):
            status, body = _ask(
                project,
                {"launch_settings": [UNDECLARED_SETTING], "build": THE_BUILD},
            )
        assert status == 400, body
        assert older in body["error"]
        assert "does not declare that name" in body["error"]

    def test_a_by_hand_run_reads_committed_head_and_says_so(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Neither a build nor a commit: this copy's committed HEAD, said out loud."""
        project, _older, _head = self._two_commits(tmp_path)
        with _no_coordinator(monkeypatch):
            status, body = _ask(project, {"launch_settings": [UNDECLARED_SETTING]})
        assert status == 200, body
        assert UNDECLARED_SETTING in _names_the_child_was_given(body)
        # And a name at neither commit is refused with HEAD named in words.
        with _no_coordinator(monkeypatch):
            status, refusal = _ask(project, {"launch_settings": ["NOT_ANYWHERE"]})
        assert status == 400, refusal
        assert "the committed HEAD of the copy of this project" in refusal["error"]


class TestBothFilesAreReadAtTheBoundCommit:
    """The profile's own names are committed lines too (the door's other half).

    Until now ``allowed_env_keys`` was handed the profile loaded off the
    WORKING COPY, so an uncommitted ``identity:`` line in the very checkout a
    build was about to run out of still widened which settings a request might
    carry. That was the last uncommitted reading left in this door.
    """

    #: Names of this project's own choosing, none of them one of the factory's
    #: defaults — so what is permitted here is permitted BECAUSE this project
    #: committed the line, and nothing else.
    IDENTITY = {
        "setting": "WIDGET_SHOP_IDENTITY",
        "reported_as": "WIDGET_SHOP_DEPLOYED",
        "asked_with": "WIDGET_SHOP_RUNNING",
        "running_as": "WIDGET_SHOP_RUNNING",
    }

    def _a_project_whose_profile_gains_an_identity(
        self, tmp_path: Path
    ) -> tuple[Path, str, str]:
        project = _a_project(tmp_path / "widget-shop")
        older = _git(project, "rev-parse", "HEAD")
        profile = yaml.safe_load(
            (project / "deploy" / "profile.yaml").read_text(encoding="utf-8")
        )
        profile["identity"] = dict(self.IDENTITY)
        (project / "deploy" / "profile.yaml").write_text(
            yaml.safe_dump(profile), encoding="utf-8"
        )
        head = _commit_everything(project, "the project declares an identity")
        return project, older, head

    def test_an_identity_only_in_the_working_tree_does_not_widen_the_door(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _a_project(tmp_path / "widget-shop")
        profile = yaml.safe_load(
            (project / "deploy" / "profile.yaml").read_text(encoding="utf-8")
        )
        # A NAME NOBODY COMMITTED, written into the checkout the command is
        # about to run out of — the shape of a build widening its own door.
        profile["identity"] = {**self.IDENTITY, "asked_with": UNDECLARED_SETTING}
        (project / "deploy" / "profile.yaml").write_text(
            yaml.safe_dump(profile), encoding="utf-8"
        )
        with _no_coordinator(monkeypatch):
            status, body = _ask(project, {"env": {UNDECLARED_SETTING: "anything"}})
        assert status == 400, body
        assert "not allowlisted" in body["error"]
        assert "exit_code" not in body

    def test_a_teardown_reads_the_profile_at_the_bound_commit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same binding on the leg that takes a candidate down.

        A teardown carries the identity of the candidate it may remove, in the
        setting the project declared for it. Bound to HEAD — where the project
        declares that setting — it is admitted; bound to the older commit,
        where it does not, the same request is refused and nothing runs.
        """
        project, older, head = self._a_project_whose_profile_gains_an_identity(
            tmp_path
        )
        teardown = {
            "env": {"WIDGET_SHOP_IDENTITY": "j-abc123@def456", "CANDIDATE_DOWN": "1"},
            "build": THE_BUILD,
        }
        with _a_coordinator_that_recorded({THE_BUILD: head}, monkeypatch):
            status, body = _ask(project, {**teardown, "declared_at": head})
        assert status == 200, body
        assert "WIDGET_SHOP_IDENTITY" in _names_the_child_was_given(body)

        with _a_coordinator_that_recorded({THE_BUILD: older}, monkeypatch):
            status, refused = _ask(project, {**teardown, "declared_at": older})
        assert status == 400, refused
        assert "not allowlisted" in refused["error"]
        assert "exit_code" not in refused


class TestACommitThisCopyDoesNotCarry:
    """The sentence says what is actually wrong (27 September 2026).

    When the coordinator records a commit this copy of the project does not
    have — a clone that has not fetched it yet, most plainly — the sentence a
    person read said the project's own ``.guardkit/config.yaml`` "says
    something this factory cannot use", and sent them to look at a file that is
    perfectly fine. The reason is a fact about the COPY, and it is said that
    way now.
    """

    def test_the_refusal_names_the_missing_commit_not_a_bad_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _a_project(tmp_path / "widget-shop", declares=(DECLARED_SETTING,))
        never_here = "0" * 40
        with _a_coordinator_that_recorded({THE_BUILD: never_here}, monkeypatch):
            status, body = _ask(
                project,
                {
                    "launch_settings": [DECLARED_SETTING],
                    "build": THE_BUILD,
                    "declared_at": never_here,
                },
            )
        assert status == 400, body
        assert "does not have the commit" in body["error"]
        assert never_here in body["error"]
        assert "says something this factory cannot use" not in body["error"]
        assert "exit_code" not in body
