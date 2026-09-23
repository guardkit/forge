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

The helper reads those from the copy of the project it has, because it cannot
see the coordinator's ledger. A name the project does not declare is refused in
plain words and nothing starts. The reserved list still wins: a project that
declares one of the factory's own names gets nothing from it.

NOTHING LIVE IS TOUCHED HERE. The service is this same process's own test
server on 127.0.0.1, on a port the kernel picks; the project is a throwaway
directory; the "deploy script" is a child of the test that prints the NAMES of
the settings it was given and nothing else. No image is built, no sandbox is
made, no credential exists anywhere here — the planted values are strings
written in this file.
"""

from __future__ import annotations

import json
import stat
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
import yaml

from forge.config.models import ForgeConfig
from forge.deploy.profile import load_deploy_profile
from forge.deploy_sidecar.service import build_server, project_declared_settings
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
    return root


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
        declared, note = project_declared_settings(project)
        assert note is None
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
        declared, note = project_declared_settings(project)
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
        declared, note = project_declared_settings(project)
        assert A_RESERVED_SETTING not in declared
        assert DECLARED_SETTING not in declared
        assert note is not None and "cannot use" in note


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
