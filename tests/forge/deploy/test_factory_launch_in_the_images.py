"""Every GuardKit started in a factory container is a factory launch (19 Oct 2026).

GuardKit's frontier-model fence, and its rule that a factory build uses only
the memory name it is handed, both key on ``GUARDKIT_FACTORY_LAUNCH`` being
non-empty. Forge's ``build_launch_env`` sets it on every launch it builds;
these pin the belt and braces:

* the release image's runtime environment sets it (coordinator, answer
  service, and both containers in a project's sandbox run that image);
* the sandbox start-up script hands it to both containers itself, after the
  named settings, so nothing handed in can clear it;
* a project's own programs still never see it: their environment is built
  from nothing.

With the stand-in ``docker`` and ``sbx`` the other sandbox tests use. No
sandbox, image, container or service of the estate is touched.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from forge.launch_environment import (
    GUARDKIT_FACTORY_LAUNCH_ENV,
    build_launch_env,
    build_project_command_env,
)
from tests.forge.deploy.test_sandbox_bootstrap_from_the_release_image import (  # noqa: F401
    _the_two_starts,
    sandbox,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = REPO_ROOT / "Dockerfile"
FIXED = f"--env {GUARDKIT_FACTORY_LAUNCH_ENV}=1"


def _runtime_stage(text: str) -> str:
    match = re.search(r"^FROM\s+\S+\s+AS\s+runtime\b(?P<body>.*)\Z", text, re.M | re.S)
    assert match, "no runtime stage in the Dockerfile"
    return match.group("body")


class TestTheReleaseImage:
    def test_the_runtime_environment_sets_it(self) -> None:
        runtime = _runtime_stage(DOCKERFILE.read_text(encoding="utf-8"))
        assert re.search(
            rf"^ENV\s+{GUARDKIT_FACTORY_LAUNCH_ENV}=1\s*$", runtime, re.MULTILINE
        )

    def test_only_in_the_runtime_stage_and_after_the_players_self_check(self) -> None:
        text = DOCKERFILE.read_text(encoding="utf-8")
        runtime = _runtime_stage(text)
        # Not in the builder stage: nothing there runs GuardKit as the factory.
        assert text.count(f"ENV {GUARDKIT_FACTORY_LAUNCH_ENV}=") == 1
        at = runtime.index(f"ENV {GUARDKIT_FACTORY_LAUNCH_ENV}=1")
        # After the self-check that runs in the Player's minimal environment,
        # and before the per-commit cache barrier, so the layer stays cached.
        assert runtime.index('RUN check_home="$(mktemp -d)"') < at
        assert at < runtime.index("ARG FORGE_GIT_SHA")


class TestTheSandboxStartUp:
    def test_both_containers_are_given_it(self, sandbox) -> None:  # noqa: F811
        helper, runner = _the_two_starts(sandbox)
        for call in (helper, runner):
            assert FIXED in call, call

    def test_nothing_handed_in_can_clear_it(self, sandbox) -> None:  # noqa: F811
        """A project's own extra setting of that name, if one were ever let
        through, is handed in BEFORE the fixed value, so the fixed one wins."""
        helper, runner = _the_two_starts(
            sandbox,
            SANDBOX_CONTAINER_ENV_NAMES=GUARDKIT_FACTORY_LAUNCH_ENV,
            **{GUARDKIT_FACTORY_LAUNCH_ENV: "0"},
        )
        for call in (helper, runner):
            # The name really was handed in, so the order below is tested.
            assert f"--env {GUARDKIT_FACTORY_LAUNCH_ENV} " in call, call
            words = call.split()
            fixed_at = max(
                i
                for i, word in enumerate(words[:-1])
                if word == "--env" and words[i + 1] == f"{GUARDKIT_FACTORY_LAUNCH_ENV}=1"
            )
            for i, word in enumerate(words[:-1]):
                if word == "--env" and words[i + 1].split("=", 1)[0] == (
                    GUARDKIT_FACTORY_LAUNCH_ENV
                ):
                    assert i <= fixed_at, call
            # The fixed value is a Docker option: it comes before the image.
            image_at = next(i for i, word in enumerate(words) if word.startswith("sha256:"))
            assert fixed_at < image_at, call


class TestAProjectsProgramNeverSeesIt:
    def test_built_from_nothing_even_when_the_container_has_it(
        self, tmp_path: Path
    ) -> None:
        project = tmp_path / "project"
        project.mkdir()
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        parent = {"PATH": "/usr/bin:/bin", GUARDKIT_FACTORY_LAUNCH_ENV: "1"}

        env = build_project_command_env(
            project, parent=parent, declared=[GUARDKIT_FACTORY_LAUNCH_ENV]
        )

        assert GUARDKIT_FACTORY_LAUNCH_ENV not in env

    def _project(self, tmp_path: Path) -> Path:
        project = tmp_path / "project"
        project.mkdir()
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        return project

    def test_a_project_value_equal_to_a_public_marker_is_passed(
        self, tmp_path: Path
    ) -> None:
        """Codex round 1 (20 October 2026): with the flag in every factory
        container, a deploy addition valued ``1`` (a release version) was
        refused as "the value of a setting the factory keeps for itself"."""
        parent = {
            "PATH": "/usr/bin:/bin",
            GUARDKIT_FACTORY_LAUNCH_ENV: "1",
            "FORGE_SIDECAR_IN_SANDBOX": "1",
            "FLEET_MEMORY_ENABLED": "1",
            "GUARDKIT_SDK_TIMEOUT": "1800",
        }

        env = build_project_command_env(
            self._project(tmp_path),
            parent=parent,
            additions={"RELEASE_VERSION": "1", "TIMEOUT": "1800"},
        )

        assert env["RELEASE_VERSION"] == "1"
        assert env["TIMEOUT"] == "1800"
        assert GUARDKIT_FACTORY_LAUNCH_ENV not in env

    @pytest.mark.parametrize(
        "marker", [GUARDKIT_FACTORY_LAUNCH_ENV, "FORGE_SIDECAR_IN_SANDBOX"]
    )
    def test_a_marker_is_public_whatever_word_it_holds(
        self, tmp_path: Path, marker: str
    ) -> None:
        """GuardKit counts any non-empty value as set, so a marker may hold a
        word rather than 1; it is still public, never a secret."""
        parent = {"PATH": "/usr/bin:/bin", marker: "yes"}

        env = build_project_command_env(
            self._project(tmp_path), parent=parent, additions={"CONFIRM": "yes"}
        )

        assert env["CONFIRM"] == "yes"
        assert marker not in env

    @pytest.mark.parametrize(
        ("held", "value"),
        [
            ("FLEET_MEMORY_PG_DSN", "postgresql://u:not-a-real-one@db:5432/m"),
            ("OPENAI_BASE_URL", "http://192.0.2.7:8000/v1"),
            # A plain number is still guarded under a credential-shaped name.
            ("GUARDKIT_NATS_PASSWORD", "424242"),
            ("FORGE_GIT_SHA", "0123456789abcdef0123456789abcdef01234567"),
            # A word under a reserved name that is not a marker is still guarded.
            ("GUARDKIT_HARNESS", "langgraph"),
        ],
    )
    def test_a_value_that_could_be_a_secret_is_still_refused(
        self, tmp_path: Path, held: str, value: str
    ) -> None:
        from forge.launch_environment import ProjectCommandEnvironmentError

        parent = {"PATH": "/usr/bin:/bin", GUARDKIT_FACTORY_LAUNCH_ENV: "1", held: value}

        with pytest.raises(ProjectCommandEnvironmentError) as refused:
            build_project_command_env(
                self._project(tmp_path), parent=parent, additions={"X": value}
            )
        assert held in str(refused.value)
        assert value not in str(refused.value)


class TestAFactoryLaunchStillSetsItWhateverTheContainerHolds:
    @pytest.mark.parametrize("held", [None, "", "0", "1"])
    def test_it_is_one(self, held: str | None) -> None:
        parent = {} if held is None else {GUARDKIT_FACTORY_LAUNCH_ENV: held}
        assert build_launch_env(parent=parent)[GUARDKIT_FACTORY_LAUNCH_ENV] == "1"
