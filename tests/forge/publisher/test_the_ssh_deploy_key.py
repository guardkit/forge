"""A remote reached over SSH is pushed to with the deploy key, and nothing else.

When a project's remote is an SSH address, git runs ssh with that one key,
``IdentitiesOnly=yes``, no agent, no person's configuration, and the host
checked against a pinned known-hosts FILE (never fetched at run time). An
https remote still gets the token through ``GIT_ASKPASS``.

The end-to-end test puts a stand-in ``ssh`` first on the PATH: it records how
it was called and runs the git command it was asked for against a bare
repository on disk. No real host is contacted.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shlex
from pathlib import Path

import pytest

from forge.publisher.credential import (
    Credential,
    an_ssh_address,
    the_environment_git_is_given,
)
from forge.publisher.service import Publisher
from forge.publisher.settings import ProjectRoute, SettingsRefused, load_settings
from tests.forge.publisher.a_project_and_a_ledger import (
    PROJECT,
    a_request,
    every_push_the_remote_saw,
    make_the_ledger,
    make_the_project,
    settings_for,
    what_the_remote_has,
)

THE_MADE_UP_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nTESTONLY-not-a-key\n"


@pytest.mark.parametrize(
    "address, ssh",
    [
        ("git@example.invalid:owner/repo.git", True),
        ("git@build_host.example:owner/repo.git", True),
        ("ssh://git@example.invalid/owner/repo.git", True),
        ("ssh://git@example.invalid:2222/owner/repo.git", True),
        ("https://example.invalid/owner/repo.git", False),
        ("git://10.0.0.1:9418/repo", False),
        ("/srv/remote.git", False),
    ],
)
def test_which_addresses_are_ssh(address: str, ssh: bool) -> None:
    assert an_ssh_address(address) is ssh


def _settings_with_remote(tmp_path: Path, remote: str) -> Path:
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "credential_file": "/k",
                "ledger": "/l",
                "state_dir": "/s",
                "known_hosts_file": "/etc/forge-publisher/known_hosts",
                "projects": {"p": {"source": "git://10.0.0.1:9418/p", "remote": remote}},
            }
        ),
        encoding="utf-8",
    )
    return settings


@pytest.mark.parametrize(
    "remote",
    [
        "example.org:owner/repo.git",  # SSH without a user: not a form it takes
        "git+ssh://git@example.org/owner/repo.git",
        "ssh+git://git@example.org/owner/repo.git",
        "SSH://git@example.org/owner/repo.git",
        "ssh://example.org/owner/repo.git",
        "file:///srv/repo.git",
        "git://example.org/owner/repo.git",
        "http://example.org/owner/repo.git",
        "/srv/repo.git",
    ],
)
def test_any_other_remote_form_is_refused(tmp_path: Path, remote: str) -> None:
    with pytest.raises(SettingsRefused, match="https:// address .* or an SSH address"):
        load_settings(_settings_with_remote(tmp_path, remote))


@pytest.mark.parametrize(
    "remote",
    [
        "https://example.org/owner/repo.git",
        "git@example.org:owner/repo.git",
        "ssh://git@example.org/owner/repo.git",
    ],
)
def test_the_two_kinds_it_takes(tmp_path: Path, remote: str) -> None:
    loaded = load_settings(_settings_with_remote(tmp_path, remote))
    assert loaded.projects["p"].source == "git://10.0.0.1:9418/p"


def test_a_recognised_ssh_remote_always_takes_the_pinned_host_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even with no known-hosts file named, git never gets the token path."""
    from forge.publisher import git_work

    key = tmp_path / "key"
    key.write_text(THE_MADE_UP_KEY, encoding="utf-8")
    seen: list[dict] = []

    def capture(*_args, env=None, **_kwargs):
        seen.append(env)
        raise OSError("not run in this test")

    monkeypatch.setattr(git_work.subprocess, "run", capture)
    commits = git_work.TheProjectsCommits(
        ProjectRoute(name="p", source="git://h/p", remote="git@example.org:o/p.git"),
        state_dir=tmp_path,
        credential=Credential(THE_MADE_UP_KEY, path=key),
        known_hosts="",
    )
    commits.where_the_remotes_branch_is("main")
    assert seen
    for env in seen:
        assert "GIT_ASKPASS" not in env
        assert "UserKnownHostsFile=/dev/null" in env["GIT_SSH_COMMAND"]
        assert "StrictHostKeyChecking=yes" in env["GIT_SSH_COMMAND"]


def test_the_environment_over_ssh(tmp_path: Path) -> None:
    key = tmp_path / "deploy key"
    key.write_text(THE_MADE_UP_KEY, encoding="utf-8")
    given = the_environment_git_is_given(
        Credential(THE_MADE_UP_KEY, path=key),
        state_dir=tmp_path,
        home=tmp_path,
        known_hosts="/etc/forge-publisher/known_hosts",
    )
    command = shlex.split(given["GIT_SSH_COMMAND"])
    assert command[:3] == ["ssh", "-F", "/dev/null"]
    assert command[command.index("-i") + 1] == str(key)
    for option in (
        "IdentitiesOnly=yes",
        "IdentityAgent=none",
        "BatchMode=yes",
        "StrictHostKeyChecking=yes",
        "UserKnownHostsFile=/etc/forge-publisher/known_hosts",
        "GlobalKnownHostsFile=/dev/null",
    ):
        assert option in command
    assert "GIT_ASKPASS" not in given
    assert "SSH_AUTH_SOCK" not in given
    assert "TESTONLY" not in json.dumps(given)


def test_the_environment_over_https_is_unchanged(tmp_path: Path) -> None:
    key = tmp_path / "token"
    key.write_text("TESTONLY-token", encoding="utf-8")
    given = the_environment_git_is_given(
        Credential("TESTONLY-token", path=key), state_dir=tmp_path, home=tmp_path
    )
    assert "GIT_ASKPASS" in given
    assert "GIT_SSH_COMMAND" not in given


def test_an_ssh_remote_without_pinned_host_keys_is_refused(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "credential_file": "/k",
                "ledger": "/l",
                "state_dir": "/s",
                "projects": {
                    "p": {"source": "git://h/p", "remote": "git@example.invalid:o/p.git"}
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(SettingsRefused, match="known_hosts_file"):
        load_settings(settings)
    data = json.loads(settings.read_text())
    data["known_hosts_file"] = "/etc/forge-publisher/known_hosts"
    settings.write_text(json.dumps(data), encoding="utf-8")
    assert load_settings(settings).known_hosts_file == "/etc/forge-publisher/known_hosts"


def _a_stand_in_ssh(bin_dir: Path, remote_root: Path, log: Path) -> None:
    """``ssh`` that logs its call and runs the asked-for git command locally."""
    bin_dir.mkdir()
    ssh = bin_dir / "ssh"
    ssh.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" >> {shlex.quote(str(log))}\n'
        f'printf "ASKPASS=%s AGENT=%s\\n" "$GIT_ASKPASS" "$SSH_AUTH_SOCK" >> {shlex.quote(str(log))}\n'
        'for last; do :; done\n'
        f"cd {shlex.quote(str(remote_root))} && exec sh -c \"$last\"\n",
        encoding="utf-8",
    )
    ssh.chmod(0o755)


def test_a_whole_send_goes_through_ssh_with_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "world"
    project = make_the_project(root)
    make_the_ledger(root / "forge.db", project=project)
    key = root / "deploy-key"
    key.write_text(THE_MADE_UP_KEY, encoding="utf-8")
    key.chmod(0o600)
    log = tmp_path / "ssh-calls"
    _a_stand_in_ssh(tmp_path / "bin", root, log)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/an-agent-that-must-not-be-used")

    settings = settings_for(
        root,
        project,
        ledger=root / "forge.db",
        credential_file=key,
        projects={
            PROJECT: ProjectRoute(
                name=PROJECT,
                source=str(project["copy"]),
                remote="git@example.invalid:remote.git",
            )
        },
    )
    settings = dataclasses.replace(settings, known_hosts_file="/pinned")

    answer = Publisher(settings).publish(a_request(project))

    assert answer.published is True, answer.refusal
    assert what_the_remote_has(project["bare"]) == project["j"]
    assert len(every_push_the_remote_saw(project["bare"])) == 1
    called = log.read_text(encoding="utf-8")
    assert f"-i\n{key}\n" in called
    assert "IdentitiesOnly=yes" in called
    assert "StrictHostKeyChecking=yes" in called
    assert "UserKnownHostsFile=/pinned" in called
    assert "git-receive-pack" in called
    assert "ASKPASS= AGENT=\n" in called
    assert "TESTONLY" not in called
