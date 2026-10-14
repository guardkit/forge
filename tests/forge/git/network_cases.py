"""The network rows of the Forge design's "What I proved" table, as cases.

Each case plants settings in a project's clone that plain git (``git fetch``
inside the clone with the inherited environment, which is how Forge fetched a
starting point before the change) obeys: a program runs, or a credential or a
header reaches the server. ``test_planted_settings.py`` runs them with plain
git as positive controls; ``test_network_git.py`` runs Forge's two-step fetch
from the recorded address against the same clone and expects nothing.

The recorded address is a public repository on a small smart-HTTP server on
127.0.0.1 (:class:`~tests.forge.git.git_marker_lab.SmartHttp`).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, List, Optional

import pytest

from tests.forge.git.git_marker_lab import (
    HOOKS,
    SECRET_NAME,
    SECRET_VALUE,
    Lab,
    SmartHttp,
    bare_copy_of,
)


@dataclass
class NetLab:
    """A marker lab, a smart-HTTP server and the repositories it serves."""

    lab: Lab
    http: SmartHttp
    good: Path
    good_commit: str
    other_commit: str
    copies: Path

    @property
    def recorded(self) -> str:
        """The address the factory recorded: the public good repository."""
        return self.http.url("good.git")

    @property
    def private(self) -> str:
        """A repository the server serves only with the right password."""
        return self.http.url("private/good.git")

    @property
    def other(self) -> str:
        """Another public repository with a different commit."""
        return self.http.url("other.git")

    def clone(self, name: str = "clone") -> Path:
        """The project's clone: a repository with nothing of the good one."""
        return self.lab.base_repo(name)


def make_netlab(root: Path) -> NetLab:
    lab = Lab(root / "lab")
    served = root / "served"
    good = lab.base_repo("good-src")
    (good / "good.md").write_text("good\n")
    lab.setup_git(good, "add", "-A")
    lab.setup_git(good, "commit", "-qm", "good")
    other = lab.base_repo("other-src")
    (other / "other.md").write_text("other\n")
    lab.setup_git(other, "add", "-A")
    lab.setup_git(other, "commit", "-qm", "other")
    bare_copy_of(lab, good, served / "good.git")
    bare_copy_of(lab, good, served / "private" / "good.git")
    bare_copy_of(lab, other, served / "other.git")
    return NetLab(
        lab=lab,
        http=SmartHttp(served),
        good=served / "good.git",
        good_commit=lab.setup_git(good, "rev-parse", "HEAD").strip(),
        other_commit=lab.setup_git(other, "rev-parse", "HEAD").strip(),
        copies=root / "factory-copies",
    )


@pytest.fixture()
def netlab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[NetLab]:
    made = make_netlab(tmp_path)
    monkeypatch.setenv("HOME", str(made.lab.home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv(SECRET_NAME, SECRET_VALUE)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG_PARAMETERS",
                 "GIT_CONFIG_COUNT", "GIT_EXTERNAL_DIFF", "GIT_ASKPASS", "SSH_ASKPASS",
                 "GIT_SSH_COMMAND", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
                 "GIT_ALLOW_PROTOCOL", "GIT_PROTOCOL_FROM_USER"):
        monkeypatch.delenv(name, raising=False)
    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                 "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FORGE_FETCH_COPIES_DIR", str(made.copies))
    # The lab's server speaks plain http on 127.0.0.1; production records
    # https only. The tests allow http as well, and nothing else.
    # (The positive controls use this fixture too, and must run without the
    # module.)
    try:
        from forge import network_git
    except ImportError:
        network_git = None
    if network_git is not None:
        monkeypatch.setattr(network_git, "FETCH_TRANSPORTS", ("https", "http"))
    try:
        yield made
    finally:
        made.http.close()


@dataclass(frozen=True)
class NetCase:
    id: str
    #: Plants settings in the clone (and points origin somewhere).
    plant: Callable[[NetLab, Path], None]
    #: The plain-git command that obeys them today.
    plain: Callable[[NetLab], List[str]]
    #: Markers plain git runs (each with the secret visible).
    expect: frozenset
    #: For the http cases: what the server must have received under plain git.
    server_receives: Optional[str] = None
    server_header: Optional[str] = None


def _origin(n: NetLab, clone: Path, url: str) -> None:
    n.lab.setup_git(clone, "remote", "add", "origin", url)


def _ssh_command(n: NetLab, clone: Path) -> None:
    _origin(n, clone, "ssh://example.invalid/x.git")
    n.lab.setup_git(clone, "config", "core.sshCommand", n.lab.path("sshcmd"))


def _credential_helper(n: NetLab, clone: Path) -> None:
    _origin(n, clone, n.private)
    n.lab.setup_git(clone, "config", f"credential.{n.http.base}.helper",
                    n.lab.path("credhelper_url"))
    n.lab.setup_git(clone, "config", "credential.helper", n.lab.path("credhelper_pw"))


def _askpass(n: NetLab, clone: Path) -> None:
    _origin(n, clone, n.private)
    n.lab.setup_git(clone, "config", "core.askPass", n.lab.path("askpass_pw"))


def _extra_header(n: NetLab, clone: Path) -> None:
    _origin(n, clone, n.recorded)
    n.lab.setup_git(clone, "config", "http.extraHeader", "X-Project-Header: planted")
    n.lab.setup_git(clone, "config", f"http.{n.http.base}/.extraHeader",
                    "Authorization: Bearer planted-token")


def _ext_origin(n: NetLab, clone: Path) -> None:
    _origin(n, clone, f"ext::{n.lab.path('extmark')} %S")
    n.lab.setup_git(clone, "config", "protocol.ext.allow", "always")


def _instead_of(n: NetLab, clone: Path) -> None:
    n.lab.setup_git(clone, "config", f"url.ext::{n.lab.path('extmark')} %S.insteadOf",
                    n.recorded)
    n.lab.setup_git(clone, "config", "protocol.ext.allow", "always")


def _upload_pack(n: NetLab, clone: Path) -> None:
    _origin(n, clone, str(n.good))
    n.lab.setup_git(clone, "config", "remote.origin.uploadpack", n.lab.path("uploadpack"))


def _git_proxy(n: NetLab, clone: Path) -> None:
    _origin(n, clone, "git://127.0.0.1:9/x.git")
    n.lab.setup_git(clone, "config", "protocol.git.allow", "always")
    n.lab.setup_git(clone, "config", "core.gitProxy", n.lab.path("gitproxy"))


def _hook(n: NetLab, clone: Path) -> None:
    _origin(n, clone, str(n.good))
    hooks = clone / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    shutil.copy(n.lab.bin / "hook-reference-transaction", hooks / "reference-transaction")


def _fetch_origin(n: NetLab) -> List[str]:
    return ["fetch", "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main"]


NETWORK_CASES = [
    NetCase("sshCommand", _ssh_command, _fetch_origin, frozenset({"sshcmd"})),
    NetCase("credential helper", _credential_helper, _fetch_origin,
            frozenset({"credhelper_pw"}), server_receives="helper-password"),
    NetCase("askPass", _askpass, _fetch_origin, frozenset({"askpass_pw"}),
            server_receives="askpass-password"),
    NetCase("extraHeader", _extra_header, _fetch_origin, frozenset(),
            server_header="planted"),
    NetCase("ext origin", _ext_origin, _fetch_origin, frozenset({"extmark"})),
    NetCase("insteadOf on an explicit address", _instead_of,
            lambda n: ["fetch", "--no-tags", n.recorded, "+refs/heads/main:refs/heads/fetched"],
            frozenset({"extmark"})),
    NetCase("uploadpack", _upload_pack, _fetch_origin, frozenset({"uploadpack"})),
    NetCase("gitProxy", _git_proxy, _fetch_origin, frozenset({"gitproxy"})),
    NetCase("reference-transaction hook", _hook, _fetch_origin,
            frozenset({"hook-reference-transaction"})),
]

__all__ = ["HOOKS", "NETWORK_CASES", "NetCase", "NetLab", "make_netlab", "netlab"]
