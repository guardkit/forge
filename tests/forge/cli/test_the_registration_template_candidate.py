"""The deploy script a NEWLY REGISTERED repository is given, driven.

WHY THIS FILE EXISTS (27 September 2026, Codex's requirement of the 23rd, and
the reviewer's carry-forward of the 23rd before it).

The defect this cures was found and cured in one project's own deploy script,
and the template every new repository gets still had it. That template named
ONE candidate compose project for the whole repository — ``<project>-cand`` —
and its teardown took that name down. Two builds checking at once therefore
shared one compose project, one container and one built image; and one build's
cleanup removed another build's candidate and its database with it. That was
driven, not imagined: three builds, three databases, one cleanup. The profile
``forge register-repo`` wrote alongside it declared no ``identity:`` block at
all, so the factory had nothing to hand the script that could tell one build's
candidate from another's, and dispatched no cleanup at all.

WHAT IS PROVEN HERE. The template is rendered for a throwaway repository, and
the rendered script is driven twice — a candidate check and a teardown — with a
FAKE ``docker`` first on its PATH. The fake is a small program written by this
test: it records every ``compose`` call and answers ``image inspect`` out of a
file. Nothing real is anywhere near this: no image is built, no container runs,
no sandbox is made, no real docker is called, and every value used is a string
written in this file.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from forge.cli.register_repo import render_deploy_files
from forge.deploy.profile import parse_deploy_profile
from forge.pipeline.deployment_identity import declared_identity

#: The identity the factory hands the check and, later, the teardown.
AN_IDENTITY = "j-abcdef012345@0123456789abcdef"

#: What that identity reduces to inside a compose project name.
ITS_TOKEN = "j-abcdef012345-0123456789abcdef"


def _a_fake_docker(bin_dir: Path, log: Path) -> None:
    """A ``docker`` that records what it was asked and invents nothing.

    ``compose ... up``/``down`` are recorded and answered 0. ``image inspect``
    answers an id for any reference asked about, so the script's own
    "what did this build produce" reads succeed. Anything else is recorded and
    answered 0 as well — this program never reaches a container runtime.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    program = bin_dir / "docker"
    program.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, pathlib\n"
        f"log = pathlib.Path({str(log)!r})\n"
        "args = sys.argv[1:]\n"
        "with log.open('a') as handle:\n"
        "    handle.write(' '.join(args) + '\\n')\n"
        "if args[:2] == ['image', 'inspect']:\n"
        "    print('sha256:' + 'a' * 64)\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    program.chmod(program.stat().st_mode | stat.S_IXUSR)


def _a_fake_curl(bin_dir: Path) -> None:
    """A ``curl`` that always answers the health body the template waits for."""
    program = bin_dir / "curl"
    program.write_text(
        "#!/usr/bin/env python3\n"
        "print('{\"database\":\"connected\"}')\n",
        encoding="utf-8",
    )
    program.chmod(program.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def a_registered_repository(tmp_path: Path) -> tuple[Path, Path]:
    """A throwaway repository holding exactly what registration writes."""
    repo = tmp_path / "widget-shop"
    repo.mkdir()
    files = render_deploy_files(name="widget-shop", repo=repo, app_port=9401)
    for relative, text in files.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        if relative.endswith(".sh"):
            target.chmod(target.stat().st_mode | stat.S_IXUSR)
    (repo / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    log = tmp_path / "docker-calls.log"
    _a_fake_docker(tmp_path / "bin", log)
    _a_fake_curl(tmp_path / "bin")
    return repo, log


def _drive(repo: Path, tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    """Run the rendered script with the fake tools first on its PATH."""
    environment = {
        **os.environ,
        "PATH": f"{tmp_path / 'bin'}:{os.environ.get('PATH', '')}",
        **env,
    }
    return subprocess.run(
        [str(repo / "deploy" / "deploy.sh")],
        cwd=str(repo),
        env=environment,
        capture_output=True,
        text=True,
    )


class TestTheProfileRegistrationWrites:
    def test_it_declares_an_identity_block(self, tmp_path: Path) -> None:
        """Without it the factory has nothing to tell one candidate from another."""
        repo = tmp_path / "widget-shop"
        files = render_deploy_files(name="widget-shop", repo=repo, app_port=9401)
        profile = parse_deploy_profile(yaml.safe_load(files["deploy/profile.yaml"]))
        identity = declared_identity(profile)

        assert identity.declared is True, (
            "a newly registered repository that declares no identity gets no "
            "cleanup dispatched at all, and its candidates stand for ever"
        )
        assert identity.setting
        assert identity.marker
        assert identity.artifact_setting


class TestTheTemplateTakesTheIdentityItIsHanded:
    def test_the_check_makes_a_candidate_of_its_own(
        self, a_registered_repository: tuple[Path, Path], tmp_path: Path
    ) -> None:
        repo, log = a_registered_repository

        done = _drive(repo, tmp_path, CANDIDATE="1", DEPLOY_IDENTITY=AN_IDENTITY)

        assert done.returncode == 0, done.stdout + done.stderr
        calls = log.read_text(encoding="utf-8")
        assert f"-p widget-shop-cand-{ITS_TOKEN}" in calls, (
            "the candidate compose project has to be THIS check's own"
        )
        assert "-p widget-shop-cand " not in calls, (
            "the one shared candidate name is what two builds collided on"
        )

    def test_the_teardown_removes_only_its_own_candidate(
        self, a_registered_repository: tuple[Path, Path], tmp_path: Path
    ) -> None:
        repo, log = a_registered_repository

        done = _drive(repo, tmp_path, CANDIDATE_DOWN="1", DEPLOY_IDENTITY=AN_IDENTITY)

        assert done.returncode == 0, done.stdout + done.stderr
        removals = [
            line
            for line in log.read_text(encoding="utf-8").splitlines()
            if " down " in f" {line} "
        ]
        assert len(removals) == 1, removals
        assert f"-p widget-shop-cand-{ITS_TOKEN}" in removals[0]

    def test_a_teardown_handed_no_identity_removes_nothing(
        self, a_registered_repository: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """The whole cure: no name, no removal, and a sentence saying why."""
        repo, log = a_registered_repository

        done = _drive(repo, tmp_path, CANDIDATE_DOWN="1")

        assert done.returncode == 2, done.stdout + done.stderr
        assert "no identity and no token were handed to this teardown" in done.stdout
        assert "Nothing was removed" in done.stdout
        assert not log.exists() or "down" not in log.read_text(encoding="utf-8")

    def test_a_promote_handed_no_identity_refuses(
        self, a_registered_repository: tuple[Path, Path], tmp_path: Path
    ) -> None:
        repo, log = a_registered_repository

        done = _drive(repo, tmp_path, PROMOTE="1")

        assert done.returncode == 1, done.stdout + done.stderr
        assert "no DEPLOY_IDENTITY was handed to this promote" in done.stdout
        assert "LIVE name is untouched" in done.stdout
        assert not log.exists() or "up" not in log.read_text(encoding="utf-8")

    def test_the_factory_cannot_reach_the_by_hand_sweep(
        self, a_registered_repository: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """It needs two words on the command line, and the factory sends none."""
        repo, _log = a_registered_repository

        refused = subprocess.run(
            [str(repo / "deploy" / "deploy.sh"), "sweep-candidates"],
            cwd=str(repo),
            env={**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ.get('PATH', '')}"},
            capture_output=True,
            text=True,
        )
        assert refused.returncode == 2
        assert "--remove-every-candidate" in refused.stdout

        anything_else = subprocess.run(
            [str(repo / "deploy" / "deploy.sh"), "--do-something"],
            cwd=str(repo),
            env={**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ.get('PATH', '')}"},
            capture_output=True,
            text=True,
        )
        assert anything_else.returncode == 2
        assert "this script takes no arguments" in anything_else.stdout
