"""Forge's network boundary: the recorded address, the factory's credential
(or none), one transport, and nothing from the project.

These fail today because ``forge.network_git`` does not exist. Each case has a
positive control in ``test_planted_settings.py`` (plain git, the way Forge
fetched before) or in the test itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.forge.git.git_marker_lab import SECRET_NAME, Lab, Listener
from tests.forge.git.network_cases import NETWORK_CASES, NetCase, NetLab, netlab  # noqa: F401


def _ng():
    from forge import network_git

    return network_git


def _pg():
    from forge import project_git

    return project_git


def _origin_main(n: NetLab, clone: Path) -> str:
    return n.lab.setup_git(clone, "rev-parse", "refs/remotes/origin/main").strip()


# ---------------------------------------------------------------------------
# The two-step fetch runs nothing the clone names


@pytest.mark.parametrize("case", NETWORK_CASES, ids=[c.id for c in NETWORK_CASES])
def test_fetch_from_recorded_address_runs_no_project_program(
    netlab: NetLab, case: NetCase
) -> None:
    clone = netlab.clone()
    case.plant(netlab, clone)
    netlab.lab.fired()
    start = _ng().fetch_start_point(clone, netlab.recorded, repo="org/good")
    assert netlab.lab.fired() == []
    assert start.refusal is None, start.refusal
    assert (start.branch, start.commit) == ("main", netlab.good_commit)
    assert _origin_main(netlab, clone) == netlab.good_commit
    assert netlab.http.passwords() == []
    assert netlab.http.header_values("x-project-header") == []
    assert netlab.http.header_values("authorization") == []


@pytest.mark.parametrize(
    "plant",
    ["helper", "askPass", "extraHeader", "per-address extraHeader"],
)
def test_fetch_from_recorded_address_sends_no_project_credential(
    netlab: NetLab, plant: str
) -> None:
    """The recorded address is the private repository: plain git sends the
    project's credential or header; the factory's fetch sends nothing, and
    so is refused by the server."""
    clone = netlab.clone()
    lab = netlab.lab
    if plant == "helper":
        lab.setup_git(clone, "config", "credential.helper", lab.path("credhelper_pw"))
    elif plant == "askPass":
        lab.setup_git(clone, "config", "core.askPass", lab.path("askpass_pw"))
    elif plant == "extraHeader":
        lab.setup_git(clone, "config", "http.extraHeader", "Authorization: Bearer planted-token")
    else:
        lab.setup_git(clone, "config", f"http.{netlab.private}.extraHeader",
                      "Authorization: Bearer planted-token")
    lab.plain(clone, "fetch", "--no-tags", netlab.private, "+refs/heads/main:refs/heads/x")
    sent = netlab.http.passwords() + netlab.http.header_values("authorization")
    assert sent, "positive control: plain git sent the project's credential"
    netlab.http.seen.clear()
    lab.fired()
    start = _ng().fetch_start_point(clone, netlab.private, repo="org/private")
    assert start.refusal and "IGNORE" not in start.refusal
    assert netlab.http.passwords() == []
    assert netlab.http.header_values("authorization") == []
    assert lab.fired() == []


# ---------------------------------------------------------------------------
# One transport


@pytest.mark.parametrize("kind", ["git", "http", "ext", "ssh"])
def test_network_transport_is_the_recorded_one(netlab: NetLab, kind: str) -> None:
    ng = _ng()
    listener = Listener()
    lab = netlab.lab
    try:
        address = {
            "git": f"git://127.0.0.1:{listener.port}/x.git",
            "http": netlab.recorded,
            "ext": f"ext::{lab.path('extmark')} %S",
            "ssh": f"ssh://lab@127.0.0.1:{listener.port}/x.git",
        }[kind]
        credential = {"GIT_SSH_COMMAND": lab.path("sshcmd")} if kind == "ssh" else None

        def ask(transport: str):
            return ng.run_network_git(
                ["ls-remote", "--", address, "HEAD"], transport=transport,
                credential_env=credential, timeout=20,
            )

        # Positive control: with that transport named, git does reach out.
        lab.fired()
        ask({"git": "git", "http": "http", "ext": "ext", "ssh": "ssh"}[kind])
        reached = (listener.connections or netlab.http.seen or lab.fired())
        assert reached, "positive control: the transport was used"
        listener.connections = 0
        netlab.http.seen.clear()
        done = ask("https")
        assert not done.ok
        assert listener.connections == 0
        assert netlab.http.seen == []
        assert lab.fired() == []
        assert "fatal" not in done.said.lower()
    finally:
        listener.close()


def test_transport_of_names_one_transport_per_form() -> None:
    ng = _ng()
    assert ng.transport_of("https://example.invalid/o/r.git") == "https"
    assert ng.transport_of("git://127.0.0.1/r") == "git"
    assert ng.transport_of("git@example.invalid:o/r.git") == "ssh"
    assert ng.transport_of("ssh://git@example.invalid/o/r.git") == "ssh"
    assert ng.transport_of("/srv/r.git") == "file"
    for refused in ("file:///srv/r.git", "ext::sh -c x", "relative/r.git", ""):
        assert ng.transport_of(refused) is None, refused


def test_the_fetch_address_must_be_plain_https(monkeypatch: pytest.MonkeyPatch) -> None:
    ng = _ng()
    assert ng.check_fetch_address("https://example.invalid/o/r.git") is None
    for bad in ("http://example.invalid/o/r", "git://example.invalid/o/r",
                "ssh://git@example.invalid/o/r", "ext::sh", "/srv/r.git",
                "https://token@example.invalid/o/r", "https://example.invalid/o/r?x=1",
                "https://example.invalid/", "https:///o/r"):
        sentence = ng.check_fetch_address(bad)
        assert sentence, bad
        assert bad not in sentence and "example.invalid" not in sentence


# ---------------------------------------------------------------------------
# Bringing the commit into the clone


@pytest.mark.parametrize("redirect", ["insteadOf to ext", "insteadOf to another repository"])
def test_bringing_in_refuses_a_redirected_copy(netlab: NetLab, redirect: str) -> None:
    ng = _ng()
    lab = netlab.lab
    clone = netlab.clone()
    copy = ng.factory_copy_for(netlab.recorded)
    if redirect == "insteadOf to ext":
        target = f"ext::{lab.path('extmark')} %S"
        lab.setup_git(clone, "config", "protocol.ext.allow", "always")
    else:
        target = str(netlab.good.parent / "other.git")
    lab.setup_git(clone, "config", f"url.{target}.insteadOf", str(copy))
    # Positive control: plain git follows the rewrite of the copy's folder.
    first = ng.fetch_start_point(netlab.clone("warm"), netlab.recorded)
    assert first.refusal is None, first.refusal
    lab.fired()
    lab.plain(clone, "fetch", "--no-tags", str(copy), "+refs/heads/main:refs/heads/plain")
    if redirect == "insteadOf to ext":
        assert "extmark" in Lab.names(lab.fired())
    else:
        assert lab.setup_git(clone, "rev-parse", "refs/heads/plain").strip() == netlab.other_commit
    start = ng.fetch_start_point(clone, netlab.recorded, repo="org/good")
    assert lab.fired() == []
    assert start.refusal, "the redirected copy was refused"
    assert not start.commit
    assert "IGNORE" not in start.refusal and str(copy) not in start.refusal


def test_bringing_in_takes_only_an_absolute_local_folder(netlab: NetLab) -> None:
    ng = _ng()
    clone = netlab.clone()
    for copy in ("relative/copy.git", netlab.recorded, f"file://{netlab.good}"):
        done = ng.bring_in_from_copy(clone, copy, ["+refs/heads/main:refs/remotes/origin/main"])
        assert done.returncode != 0
        assert done.stderr == ng.COPY_REFUSAL


def test_a_project_without_a_recorded_address_is_refused(netlab: NetLab) -> None:
    ng = _ng()
    clone = netlab.clone()
    netlab.lab.setup_git(clone, "remote", "add", "origin", str(netlab.good))
    start = ng.fetch_start_point(clone, None, repo="org/good")
    assert start.refusal == ng.no_fetch_address_sentence("org/good")
    assert "planning.fetch_addresses" in start.refusal
    assert netlab.lab.setup_git(clone, "for-each-ref", "refs/remotes/") == ""


def test_fetch_all_branches_brings_every_branch(netlab: NetLab) -> None:
    ng = _ng()
    lab = netlab.lab
    side = netlab.lab.root / "side-src"
    lab.setup_git(netlab.lab.root, "clone", "-q", str(netlab.good), str(side))
    lab.setup_git(side, "checkout", "-q", "-b", "feature/x")
    (side / "f.md").write_text("f\n")
    lab.setup_git(side, "add", "-A")
    lab.setup_git(side, "-c", "user.name=Lab", "-c", "user.email=lab@example.invalid",
                  "commit", "-qm", "f")
    lab.setup_git(side, "push", "-q", str(netlab.good), "feature/x")
    lab.setup_git(netlab.good, "update-server-info")
    clone = netlab.clone()
    got = ng.fetch_all_branches(clone, netlab.recorded, repo="org/good")
    assert got.refusal is None, got.refusal
    assert set(got.branches) == {"main", "feature/x"}
    assert lab.setup_git(clone, "rev-parse", "refs/remotes/origin/feature/x").strip() == (
        got.branches["feature/x"]
    )


# ---------------------------------------------------------------------------
# The environment: no secret, no personal or system settings


def test_network_environment_has_no_secret(
    netlab: NetLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    ng = _ng()
    lab = netlab.lab
    for name in ("OPENAI_API_KEY", "FLEET_MEMORY_PG_DSN", "GUARDKIT_NATS_PASSWORD",
                 "SSH_AUTH_SOCK"):
        monkeypatch.setenv(name, "not-a-real-value")
    home = lab.root / "some-home"
    home.mkdir()
    env = ng.isolated_environment("https", home)
    for name in (SECRET_NAME, "OPENAI_API_KEY", "FLEET_MEMORY_PG_DSN",
                 "GUARDKIT_NATS_PASSWORD", "SSH_AUTH_SOCK"):
        assert name not in env
    assert env["GIT_ALLOW_PROTOCOL"] == "https"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ASKPASS"] == "" and env["SSH_ASKPASS"] == ""
    assert env["GIT_NO_LAZY_FETCH"] == "1"
    assert env["HOME"] == str(home) and env["XDG_CONFIG_HOME"] == str(home / ".config")
    # And a program git does start (the factory's own ssh command, here a
    # marker that dumps what it was given) sees none of it.
    dump = lab.root / "ssh-environment.txt"
    lab.program("envdump", f'env > "{dump}"\nexit 1\n')
    ng.run_network_git(
        ["ls-remote", "--", "ssh://lab@127.0.0.1:9/x.git"], transport="ssh",
        credential_env={"GIT_SSH_COMMAND": lab.path("envdump")}, timeout=20,
    )
    seen = dict(line.split("=", 1) for line in dump.read_text().splitlines() if "=" in line)
    for name in (SECRET_NAME, "OPENAI_API_KEY", "FLEET_MEMORY_PG_DSN",
                 "GUARDKIT_NATS_PASSWORD", "SSH_AUTH_SOCK"):
        assert name not in seen
    assert not Path(seen["HOME"]).exists(), "the fresh home folder was removed after"
    with pytest.raises(ValueError):
        ng.isolated_environment("https", home, {"OPENAI_API_KEY": "x"})


def test_a_proxy_carrying_a_password_is_refused(
    netlab: NetLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    ng = _ng()
    monkeypatch.setenv("HTTPS_PROXY", "http://user:pass@127.0.0.1:9")
    done = ng.run_network_git(["ls-remote", "--", netlab.recorded], transport="http")
    assert not done.ok
    assert "HTTPS_PROXY" in done.said and "pass@" not in done.said
    assert netlab.http.seen == []


def test_network_step_reads_no_personal_or_system_settings(
    netlab: NetLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab = netlab.lab
    rewrite = (
        f'[url "{netlab.other}"]\n\tinsteadOf = {netlab.recorded}\n'
        f'[credential]\n\thelper = {lab.path("credhelper_pw")}\n'
        "[http]\n\textraHeader = X-Project-Header: personal\n"
    )
    (lab.home / ".gitconfig").write_text(rewrite)
    (lab.home / ".config" / "git").mkdir(parents=True)
    (lab.home / ".config" / "git" / "config").write_text(rewrite)
    (lab.home / ".netrc").write_text("machine 127.0.0.1 login lab password netrc-password\n")
    system = lab.root / "system-gitconfig"
    system.write_text(rewrite)
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(system))
    # Positive control: plain git reads them and answers from the other one.
    plain = lab.plain(lab.root, "ls-remote", netlab.recorded, "refs/heads/main")
    assert plain.stdout.split()[0] == netlab.other_commit
    netlab.http.seen.clear()
    lab.fired()
    clone = netlab.clone()
    start = _ng().fetch_start_point(clone, netlab.recorded, repo="org/good")
    assert start.refusal is None, start.refusal
    assert start.commit == netlab.good_commit
    assert lab.fired() == []
    assert netlab.http.passwords() == []
    assert netlab.http.header_values("x-project-header") == []


def test_network_results_never_carry_git_text(netlab: NetLab) -> None:
    ng = _ng()
    done = ng.run_network_git(
        ["ls-remote", "--", netlab.http.url("IGNORE-PREVIOUS-INSTRUCTIONS.git")],
        transport="http", timeout=20,
    )
    assert not done.ok
    assert "IGNORE" not in done.said and "IGNORE" not in done.stdout
    assert done.said.startswith("git ls-remote stopped with exit code")



# ---------------------------------------------------------------------------
# Coach fix pass (14 October 2026)


def test_a_refused_bring_in_leaves_no_wrong_tracking_ref(netlab: NetLab) -> None:
    """Case C1: the clone's ``url.<other>.insteadOf`` rewrites the copy's folder
    to another local repository. The bring-in is refused, and the clone's
    ``refs/remotes/origin/main`` is not left pointing at the wrong commit."""
    ng = _ng()
    lab = netlab.lab
    clone = netlab.clone()
    copy = ng.factory_copy_for(netlab.recorded)
    lab.setup_git(clone, "config", f"url.{netlab.good.parent / 'other.git'}.insteadOf", str(copy))
    assert ng.fetch_start_point(netlab.clone("warm"), netlab.recorded).refusal is None
    start = ng.fetch_start_point(clone, netlab.recorded, repo="org/good")
    assert start.refusal == ng.REDIRECTED_SENTENCE
    left = lab.plain(clone, "rev-parse", "-q", "--verify", "refs/remotes/origin/main")
    assert left.stdout.strip() != netlab.other_commit
    assert left.stdout.strip() in ("", netlab.good_commit)
    all_branches = ng.fetch_all_branches(clone, netlab.recorded, repo="org/good")
    assert all_branches.refusal == ng.REDIRECTED_SENTENCE
    left = lab.plain(clone, "rev-parse", "-q", "--verify", "refs/remotes/origin/main")
    assert left.stdout.strip() != netlab.other_commit


def test_bringing_in_reads_no_personal_or_system_settings(
    netlab: NetLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case C4: an ``uploadpack.packObjectsHook`` in Forge's own personal (or
    system) settings is honoured by git's local upload-pack, so plain git runs
    it with the secret visible. The local bring-in reads neither file."""
    lab = netlab.lab
    hook = f"[uploadpack]\n\tpackObjectsHook = {lab.path('envdump')}\n"
    (lab.home / ".gitconfig").write_text(hook)
    system = lab.root / "system-gitconfig"
    system.write_text(hook)
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(system))
    ng = _ng()
    assert ng.fetch_start_point(netlab.clone("warm"), netlab.recorded).refusal is None
    copy = ng.factory_copy_for(netlab.recorded)
    clone = netlab.clone()
    lab.fired()
    lab.plain(clone, "fetch", "--no-tags", "--", str(copy), "+refs/heads/main:refs/heads/plain")
    fired = lab.fired()
    assert "envdump" in Lab.names(fired)  # positive control
    start = ng.fetch_start_point(clone, netlab.recorded, repo="org/good")
    assert lab.fired() == []
    assert start.refusal is None, start.refusal
    assert start.commit == netlab.good_commit


def test_a_credentialed_proxy_refuses_only_network_commands(
    netlab: NetLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proxy with a login is refused by name for a command that reaches a
    remote; local commands at the boundary (making the copy, reading its refs)
    never look at proxies, and the sentence says it is about the proxy."""
    ng = _ng()
    monkeypatch.setenv("HTTPS_PROXY", "http://user:pass@127.0.0.1:9")
    made = netlab.lab.root / "local-only.git"
    local = ng.run_network_git(["init", "--bare", "-q", str(made)], transport="none")
    assert local.ok, local.said
    reads = ng.run_network_git(["for-each-ref"], transport="none", git_dir=made)
    assert reads.ok, reads.said
    start = ng.fetch_start_point(netlab.clone(), netlab.recorded, repo="org/good")
    assert start.refusal and "HTTPS_PROXY" in start.refusal
    assert "could not be made" not in start.refusal
    assert "pass@" not in start.refusal
    assert netlab.http.seen == []


def test_a_login_free_proxy_is_passed_to_network_commands(
    netlab: NetLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    ng = _ng()
    lab = netlab.lab
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    dump = lab.root / "ssh-environment.txt"
    lab.program("envdump", f'env > "{dump}"\nexit 1\n')
    ng.run_network_git(["ls-remote", "--", "ssh://lab@127.0.0.1:9/x.git"], transport="ssh",
                       credential_env={"GIT_SSH_COMMAND": lab.path("envdump")}, timeout=20)
    seen = dict(line.split("=", 1) for line in dump.read_text().splitlines() if "=" in line)
    assert seen.get("HTTPS_PROXY") == "http://proxy.invalid:3128"
    assert seen.get("no_proxy") == "127.0.0.1,localhost"
    dump.unlink()
    ng.run_network_git(["ls-remote", "--", "ssh://lab@127.0.0.1:9/x.git"], transport="none",
                       credential_env={"GIT_SSH_COMMAND": lab.path("envdump")}, timeout=20)
    assert not dump.exists() or "HTTPS_PROXY" not in dump.read_text()


def test_the_sandbox_command_line_carries_login_free_proxies_only() -> None:
    ng = _ng()
    line = ng.sandbox_git_command(
        ["sbx", "exec", "-u", "1000", "s"], ["ls-remote", "--", "https://example.invalid/o/r"],
        transport="https", home="/tmp/h",
        proxy_env={"HTTPS_PROXY": "http://proxy.invalid:3128", "NO_PROXY": "localhost"},
    )
    assert "HTTPS_PROXY=http://proxy.invalid:3128" in line
    assert "NO_PROXY=localhost" in line
    assert line.index("HTTPS_PROXY=http://proxy.invalid:3128") < line.index("git")
    with pytest.raises(ng.NetworkRefused) as refused:
        ng.sandbox_git_command(
            ["sbx", "exec", "-u", "1000", "s"], ["ls-remote", "--", "https://example.invalid/o/r"],
            transport="https", home="/tmp/h", proxy_env={"HTTPS_PROXY": "http://u:p@proxy:1"},
        )
    assert "HTTPS_PROXY" in str(refused.value) and "u:p" not in str(refused.value)


# ---------------------------------------------------------------------------
# Implementation review round 1 (14 October 2026): the bring-in takes only the
# copy's own branches, and a failed bring-in leaves no unverified ref


def _push_branch(netlab: NetLab, repo: Path, branch: str, text: str) -> str:
    """Add ``branch`` with one more commit to the bare ``repo``; its commit."""
    lab = netlab.lab
    work = lab.root / f"work-{repo.name}-{branch.replace('/', '-')}"
    lab.setup_git(lab.root, "clone", "-q", str(repo), str(work))
    lab.setup_git(work, "checkout", "-q", "-b", branch)
    (work / f"{branch.replace('/', '-')}.md").write_text(text)
    lab.setup_git(work, "add", "-A")
    lab.setup_git(work, "-c", "user.name=Lab", "-c", "user.email=lab@example.invalid",
                  "commit", "-qm", text)
    lab.setup_git(work, "push", "-q", str(repo), branch)
    lab.setup_git(repo, "update-server-info")
    return lab.setup_git(work, "rev-parse", "HEAD").strip()


def _tracking(netlab: NetLab, clone: Path) -> dict:
    out = netlab.lab.setup_git(clone, "for-each-ref", "--format=%(objectname) %(refname)",
                               "refs/remotes/origin/")
    return {ref: sha for sha, _, ref in (line.partition(" ") for line in out.splitlines()) if ref}


def test_all_branches_takes_only_the_copys_branches(netlab: NetLab) -> None:
    """R1: the copy's folder is rewritten to another local repository that
    shares ``main`` (same commit) and adds an extra branch and a branch named
    like an existing tracking ref. Nothing but the copy's own branches, at the
    copy's commits, is left under refs/remotes/origin/."""
    ng = _ng()
    lab = netlab.lab
    assert ng.fetch_start_point(netlab.clone("warm"), netlab.recorded).refusal is None
    copy = ng.factory_copy_for(netlab.recorded)
    evil = lab.root / "evil.git"
    lab.setup_git(lab.root, "clone", "-q", "--bare", str(netlab.good), str(evil))
    extra = _push_branch(netlab, evil, "extra", "not the recorded repository")
    overwrite = _push_branch(netlab, evil, "old", "an overwrite")
    clone = netlab.clone()
    # A tracking ref the clone already has, which the copy does not.
    lab.setup_git(clone, "update-ref", "refs/remotes/origin/old",
                  lab.setup_git(clone, "rev-parse", "HEAD").strip())
    lab.setup_git(clone, "config", f"url.{evil}.insteadOf", str(copy))
    # Positive control: a wildcard fetch of the copy's folder brings both in.
    lab.plain(clone, "fetch", "--no-tags", "--", str(copy), "+refs/heads/*:refs/plain/*")
    plain = lab.setup_git(clone, "for-each-ref", "--format=%(refname)", "refs/plain/")
    assert "refs/plain/extra" in plain and "refs/plain/old" in plain
    got = ng.fetch_all_branches(clone, netlab.recorded, repo="org/good")
    held = _tracking(netlab, clone)
    assert extra not in held.values() and overwrite not in held.values()
    assert set(held) <= {"refs/remotes/origin/main", "refs/remotes/origin/HEAD"}
    if got.refusal is None:
        assert held.get("refs/remotes/origin/main") == netlab.good_commit
        assert set(got.branches) == {"main"}


def test_a_failed_bring_in_leaves_no_unverified_ref(netlab: NetLab) -> None:
    """R2: two requested branches, the copy's folder rewritten to another
    local repository, and a lock on one destination ref: the fetch fails part
    way, and no destination ref is left at a commit nobody verified."""
    ng = _ng()
    lab = netlab.lab
    _push_branch(netlab, netlab.good, "feature/x", "the recorded feature")
    assert ng.fetch_start_point(netlab.clone("warm"), netlab.recorded,
                                branch="feature/x").refusal is None
    copy = ng.factory_copy_for(netlab.recorded)
    other = lab.root / "other-two.git"
    lab.setup_git(lab.root, "clone", "-q", "--bare", str(netlab.good.parent / "other.git"),
                  str(other))
    wrong = _push_branch(netlab, other, "feature/x", "not the recorded feature")
    clone = netlab.clone()
    lab.setup_git(clone, "config", f"url.{other}.insteadOf", str(copy))
    locks = clone / ".git" / "refs" / "remotes" / "origin" / "feature"
    locks.mkdir(parents=True)
    (locks / "x.lock").write_text("")
    start = ng.fetch_start_point(clone, netlab.recorded, branch="feature/x", repo="org/good")
    assert start.refusal, "refused"
    held = _tracking(netlab, clone)
    assert netlab.other_commit not in held.values()
    assert wrong not in held.values()
    (locks / "x.lock").unlink()
    (locks / "x.lock").write_text("")
    got = ng.fetch_all_branches(clone, netlab.recorded, repo="org/good")
    assert got.refusal
    held = _tracking(netlab, clone)
    assert netlab.other_commit not in held.values() and wrong not in held.values()


def test_the_bring_in_is_atomic(netlab: NetLab) -> None:
    """``--atomic`` on the local bring-in: one ref that cannot be updated
    means none is."""
    ng = _ng()
    lab = netlab.lab
    _push_branch(netlab, netlab.good, "feature/y", "y")
    clone = netlab.clone()
    assert ng.fetch_start_point(netlab.clone("warm"), netlab.recorded,
                                branch="feature/y").refusal is None
    copy = ng.factory_copy_for(netlab.recorded)
    locks = clone / ".git" / "refs" / "remotes" / "origin" / "feature"
    locks.mkdir(parents=True)
    (locks / "y.lock").write_text("")
    done = ng.bring_in_from_copy(clone, copy, ["+refs/heads/main:refs/remotes/origin/main",
                                               "+refs/heads/feature/y:refs/remotes/origin/feature/y"])
    assert done.returncode != 0
    assert "refs/remotes/origin/main" not in _tracking(netlab, clone)
