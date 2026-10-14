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
                "private_path_prefixes": ["/home/example"],
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
    import subprocess as real_subprocess

    from forge import network_git
    from forge.publisher import git_work

    key = tmp_path / "key"
    key.write_text(THE_MADE_UP_KEY, encoding="utf-8")
    seen: list[dict] = []
    real_run = real_subprocess.run

    def capture(argv, *args, env=None, **kwargs):
        if "fetch" not in argv:
            return real_run(argv, *args, env=env, **kwargs)  # making the copy
        seen.append(env)
        raise OSError("not run in this test")

    monkeypatch.setattr(network_git.subprocess, "run", capture)
    commits = git_work.TheProjectsCommits(
        ProjectRoute(name="p", source="git://h/p", remote="git@example.org:o/p.git"),
        state_dir=tmp_path,
        credential=Credential(THE_MADE_UP_KEY, path=key),
        known_hosts="",
    )
    commits.where_the_remotes_branch_is("main")
    assert seen
    for env in seen:
        assert not env.get("GIT_ASKPASS")
        assert env["GIT_ALLOW_PROTOCOL"] == "ssh"
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
    # The ssh program by its full path (14 October 2026: git's PATH is fixed).
    assert command[0] == "ssh" or command[0].endswith("/ssh")
    assert command[1:3] == ["-F", "/dev/null"]
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
    assert not given["GIT_ASKPASS"]
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
                "private_path_prefixes": ["/home/example"],
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


# ---------------------------------------------------------------------------
# One key per route (13 October 2026)
#
# Two projects, each with a bare remote under one folder the stand-in ssh
# serves from, and each with its own key in the protected route-keys folder.
# The stand-in writes ONE FILE PER CALL, so calls made at the same moment by
# two sends cannot be mixed up in the log, and it refuses any key whose path
# it was told to refuse — the way a remote refuses a key it does not know.
# ---------------------------------------------------------------------------

#: The second project. Its build and record are its own.
SECOND = "bench/second-shop"
SECOND_BUILD = "build-FEAT-PUB2-1"
KEY_A = "-----BEGIN OPENSSH PRIVATE KEY-----\nTESTONLY-route-key-A\n"
KEY_B = "-----BEGIN OPENSSH PRIVATE KEY-----\nTESTONLY-route-key-B\n"


def _a_logging_stand_in_ssh(
    bin_dir: Path, remote_root: Path, calls: Path, *, refuse: tuple[Path, ...] = ()
) -> None:
    bin_dir.mkdir()
    calls.mkdir()
    refused = " ".join(shlex.quote(str(path)) for path in refuse)
    ssh = bin_dir / "ssh"
    ssh.write_text(
        "#!/bin/sh\n"
        f"log=$(mktemp {shlex.quote(str(calls))}/call.XXXXXX)\n"
        'printf "%s\\n" "$@" > "$log"\n'
        'key=""; previous=""\n'
        'for a; do [ "$previous" = "-i" ] && key="$a"; previous="$a"; done\n'
        f"for no in {refused}; do\n"
        '    if [ "$key" = "$no" ]; then echo "Permission denied (publickey)." >&2; exit 255; fi\n'
        "done\n"
        "for last; do :; done\n"
        f"cd {shlex.quote(str(remote_root))} && exec sh -c \"$last\"\n",
        encoding="utf-8",
    )
    ssh.chmod(0o755)


def _the_calls(calls: Path) -> list[tuple[str, str]]:
    """Each ssh call as (the key it was given, the repository it was for)."""
    found = []
    for one in sorted(calls.iterdir()):
        args = one.read_text(encoding="utf-8").splitlines()
        key = args[args.index("-i") + 1]
        found.append((key, args[-1]))
    return found


def _two_projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **ssh: object):
    from tests.forge.publisher.a_project_and_a_ledger import (
        a_route_key,
        a_route_keys_folder,
        the_credential_file,
    )

    root = tmp_path / "world"
    a = make_the_project(root / "a")
    b = make_the_project(root / "b")
    make_the_ledger(root / "forge.db", project=a)
    make_the_ledger(root / "forge.db", project=b, build_id=SECOND_BUILD, repo=SECOND)
    folder = a_route_keys_folder(tmp_path, monkeypatch)
    key_a = a_route_key(folder, "key-a", KEY_A)
    key_b = a_route_key(folder, "key-b", KEY_B)
    wide = the_credential_file(root)
    calls = tmp_path / "ssh-calls"
    _a_logging_stand_in_ssh(tmp_path / "bin", root, calls, **ssh)  # type: ignore[arg-type]
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    settings = settings_for(
        root,
        a,
        ledger=root / "forge.db",
        credential_file=wide,
        projects={
            PROJECT: ProjectRoute(
                name=PROJECT,
                source=str(a["copy"]),
                remote="git@example.invalid:a/remote.git",
                credential_name="key-a",
            ),
            SECOND: ProjectRoute(
                name=SECOND,
                source=str(b["copy"]),
                remote="git@example.invalid:b/remote.git",
                credential_name="key-b",
            ),
        },
    )
    settings = dataclasses.replace(settings, known_hosts_file="/pinned")
    return {
        "a": a,
        "b": b,
        "key_a": key_a,
        "key_b": key_b,
        "wide": wide,
        "calls": calls,
        "settings": settings,
    }


def _to_b(world: dict) -> dict:
    """A request for the second project's own build."""
    return {**a_request(world["b"], build_id=SECOND_BUILD), "project": SECOND}


def _for(calls: list[tuple[str, str]], folder: str) -> list[str]:
    return [key for key, command in calls if f"'{folder}/remote.git'" in command]


def test_each_route_sends_with_its_own_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _two_projects(tmp_path, monkeypatch)
    publisher = Publisher(world["settings"])

    to_a = publisher.publish(a_request(world["a"]))
    to_b = publisher.publish(
        _to_b(world)
    )

    assert to_a.published is True, to_a.refusal
    assert to_b.published is True, to_b.refusal
    assert what_the_remote_has(world["a"]["bare"]) == world["a"]["j"]
    assert what_the_remote_has(world["b"]["bare"]) == world["b"]["j"]
    calls = _the_calls(world["calls"])
    assert len(calls) == len(_for(calls, "a")) + len(_for(calls, "b"))
    assert _for(calls, "a") and set(_for(calls, "a")) == {str(world["key_a"])}
    assert _for(calls, "b") and set(_for(calls, "b")) == {str(world["key_b"])}


def test_a_refused_route_key_is_not_retried_with_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _two_projects(tmp_path, monkeypatch, refuse=(tmp_path / "route-keys" / "key-b",))
    before = what_the_remote_has(world["b"]["bare"])

    answer = Publisher(world["settings"]).publish(
        _to_b(world)
    )

    assert answer.published is False
    assert answer.refusal_kind == "the-remote-could-not-be-read"
    calls = _the_calls(world["calls"])
    # EXACTLY ONE call, with B's own key, and nothing else was tried: not the
    # publisher-wide key, not A's.
    assert calls == [(str(world["key_b"]), "git-upload-pack 'b/remote.git'")]
    assert what_the_remote_has(world["b"]["bare"]) == before
    assert every_push_the_remote_saw(world["b"]["bare"]) == []


def test_a_route_naming_no_file_uses_the_publisher_wide_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A regression guard: a route that names no key is sent as it always was."""
    root = tmp_path / "world"
    a = make_the_project(root / "a")
    make_the_ledger(root / "forge.db", project=a)
    wide = root / "deploy-key"
    wide.write_text(THE_MADE_UP_KEY, encoding="utf-8")
    wide.chmod(0o600)
    calls = tmp_path / "ssh-calls"
    _a_logging_stand_in_ssh(tmp_path / "bin", root, calls)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    settings = settings_for(
        root,
        a,
        ledger=root / "forge.db",
        credential_file=wide,
        projects={
            PROJECT: ProjectRoute(
                name=PROJECT,
                source=str(a["copy"]),
                remote="git@example.invalid:a/remote.git",
            )
        },
    )
    settings = dataclasses.replace(settings, known_hosts_file="/pinned")

    answer = Publisher(settings).publish(a_request(a))

    assert answer.published is True, answer.refusal
    keys = {key for key, _ in _the_calls(calls)}
    assert keys == {str(wide)}


def test_two_sends_at_the_same_time_each_use_only_their_own_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Different projects run at the same moment (their locks are per
    project). Neither send's git ever sees the other route's key."""
    import threading

    world = _two_projects(tmp_path, monkeypatch)
    publisher = Publisher(world["settings"])
    answers: dict[str, object] = {}

    def send(which: str, request: dict) -> None:
        answers[which] = publisher.publish(request)

    threads = [
        threading.Thread(target=send, args=("a", a_request(world["a"]))),
        threading.Thread(
            target=send,
            args=("b", _to_b(world)),
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert answers["a"].published is True, answers["a"].refusal  # type: ignore[attr-defined]
    assert answers["b"].published is True, answers["b"].refusal  # type: ignore[attr-defined]
    calls = _the_calls(world["calls"])
    assert set(_for(calls, "a")) == {str(world["key_a"])}
    assert set(_for(calls, "b")) == {str(world["key_b"])}


def test_a_key_swapped_after_the_health_check_is_refused_before_the_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from forge.publisher import __main__ as entry

    world = _two_projects(tmp_path, monkeypatch)
    publisher = Publisher(world["settings"])
    interfaces = tmp_path / "net"
    for name in ("lo", "eth0"):
        (interfaces / name).mkdir(parents=True)
    check = entry.the_self_check(world["settings"], publisher, interfaces=interfaces)
    assert check() is None
    before = what_the_remote_has(world["a"]["bare"])

    # A's key is replaced by a link to B's, after a passing health check.
    world["key_a"].unlink()
    world["key_a"].symlink_to(world["key_b"])

    answer = publisher.publish(a_request(world["a"]))

    assert answer.published is False
    assert answer.refusal_kind == "the-route-credential-changed"
    assert PROJECT in str(answer.refusal)
    assert "changed since the publisher started" in str(answer.refusal)
    assert "must be restarted" in str(answer.refusal)
    assert "TESTONLY" not in str(answer.refusal)
    # No ssh call at all for A: so none carrying B's key for A.
    assert _for(_the_calls(world["calls"]), "a") == []
    assert what_the_remote_has(world["a"]["bare"]) == before
    # And the health check now fails, naming A.
    said = check()
    assert said and PROJECT in said


def test_a_key_whose_permissions_changed_after_start_is_refused_until_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not only a swap: a chmod changes the file's change time, and the send
    is refused with the instruction to restart the publisher."""
    world = _two_projects(tmp_path, monkeypatch)
    publisher = Publisher(world["settings"])
    before = what_the_remote_has(world["a"]["bare"])

    world["key_a"].chmod(0o400)

    answer = publisher.publish(a_request(world["a"]))

    assert answer.published is False
    assert answer.refusal_kind == "the-route-credential-changed"
    assert "permissions or owner changed" in str(answer.refusal)
    assert "must be restarted" in str(answer.refusal)
    assert _for(_the_calls(world["calls"]), "a") == []
    assert what_the_remote_has(world["a"]["bare"]) == before
    # A restarted publisher reads it afresh and sends.
    assert Publisher(world["settings"]).publish(a_request(world["a"])).published is True
