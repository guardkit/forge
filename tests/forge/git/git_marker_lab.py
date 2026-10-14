"""Real-git repositories whose own settings name marker programs.

Shared by Forge's git tests (``tests/forge/git/`` and
``tests/forge/integration/test_forge_git_runs_no_project_programs.py``). Ported
from GuardKit's ``tests/fixtures/git_marker_lab.py`` (the same fixture pins
GuardKit's helper), with Forge's network pieces added: credential programs
that hand over a password, a small smart-HTTP git server that records what it
was sent, and ``git daemon`` on 127.0.0.1. None of them needs the internet.

Every marker program appends one line to a marker file: its own name and
whether it could see :data:`SECRET_NAME` in its environment. A test runs a git
command and then reads :meth:`Lab.fired` to learn which project programs ran.

Edits are deliberately the SAME SIZE as the original content: git skips
reading a file (and so never runs its clean filter) when the size alone says
it changed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Sequence

SECRET_NAME = "FACTORY_TEST_SECRET"
SECRET_VALUE = "s3cret-value"

#: Hooks planted in the project's ``.git/hooks``.
HOOKS = (
    "pre-commit",
    "prepare-commit-msg",
    "commit-msg",
    "post-commit",
    "post-checkout",
    "post-merge",
    "pre-merge-commit",
    "reference-transaction",
    "post-index-change",
    "post-rewrite",
    "pre-rebase",
)

GIT = shutil.which("git") or "git"

#: Filter names the fixtures plant; set-up git switches them all off.
SETUP_SILENCED_FILTERS = ("mark", "proc", "late", "subf", "racer", "liar")


def git_version() -> str:
    return subprocess.run(
        [GIT, "--version"], capture_output=True, text=True, check=False
    ).stdout.strip()


class Lab:
    """Marker programs, a marker file and plain-git helpers in one folder."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir(parents=True, exist_ok=True)
        self.marks = root / "marks.txt"
        self.home = root / "home"
        self.home.mkdir(exist_ok=True)
        self._edits = 0
        for name, body in (
            ("clean", "cat\n"),
            ("smudge", "cat\n"),
            ("process", "exit 1\n"),
            ("textconv", 'cat "$1"\n'),
            ("extdiff", "exit 0\n"),
            ("mergedrv", "exit 1\n"),
            ("fsmonitor", "exit 1\n"),
            ("gpg", "exit 1\n"),
            ("sshcmd", "exit 1\n"),
            ("credhelper", "exit 0\n"),
            ("credhelper_url", "exit 0\n"),
            ("askpass", "exit 1\n"),
            ("uploadpack", 'exec git-upload-pack "$@"\n'),
            ("extmark", "exit 1\n"),
            ("gitproxy", "exit 1\n"),
            ("smudge_wt", "cat\n"),
            ("smudge_br", "cat\n"),
            ("smudge_co", "cat\n"),
            ("subclean", "cat\n"),
            ("subextdiff", "exit 0\n"),
            ("subtextconv", 'cat "$1"\n'),
            ("execmark", "exit 0\n"),
            ("late", "cat\n"),
            ("racer", "cat\n"),
            ("credhelper_pw", 'echo username=project; echo password=helper-password\n'),
            ("askpass_pw", "echo askpass-password\n"),
            ("envdump", "exit 1\n"),
        ):
            self.program(name, body)
        for hook in HOOKS:
            self.program("hook-" + hook, "exit 0\n")

    # -- programs and markers ------------------------------------------------

    def program(self, name: str, body: str = "exit 0\n") -> str:
        path = self.bin / name
        path.write_text(
            "#!/bin/sh\n"
            f'echo "{name} secret=${{{SECRET_NAME}:-absent}}" >> "{self.marks}"\n'
            + body
        )
        path.chmod(0o755)
        return str(path)

    def path(self, name: str) -> str:
        return str(self.bin / name)

    def fired(self) -> List[str]:
        """The marker lines written since the last call, then forget them."""
        if not self.marks.exists():
            return []
        lines = sorted({line for line in self.marks.read_text().splitlines() if line})
        self.marks.unlink()
        return lines

    @staticmethod
    def names(lines: Sequence[str]) -> List[str]:
        return sorted({line.split()[0] for line in lines})

    @staticmethod
    def saw_secret(lines: Sequence[str]) -> bool:
        return any(f"secret={SECRET_VALUE}" in line for line in lines)

    # -- plain git -----------------------------------------------------------

    def plain_env(self) -> Dict[str, str]:
        """The inherited environment, as the factory's git saw it before the fix."""
        return {**os.environ, SECRET_NAME: SECRET_VALUE, "GIT_TERMINAL_PROMPT": "0"}

    def plain(
        self, repo: Path, *args: str, input: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [GIT, *args], cwd=repo, capture_output=True, text=True, input=input,
            env=env or self.plain_env(), check=False,
        )

    def setup_git(self, repo: Path, *args: str) -> str:
        """Fixture set-up git: no project programs, no hooks, must succeed."""
        argv = [GIT, "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                "-c", "protocol.file.allow=always"]
        for name in SETUP_SILENCED_FILTERS:
            for key in ("clean", "smudge", "process"):
                argv += ["-c", f"filter.{name}.{key}="]
        argv += list(args)
        done = subprocess.run(
            argv, cwd=repo, capture_output=True, text=True, check=False,
            env={k: v for k, v in os.environ.items() if k != SECRET_NAME},
        )
        assert done.returncode == 0, (args, done.stderr)
        return done.stdout

    # -- edits ---------------------------------------------------------------

    def same_size_edit(self, path: Path) -> None:
        """Change the content but not the size, so git must read the file."""
        self._edits += 1
        old = path.read_text()
        letter = chr(ord("a") + self._edits % 26)
        if old[:1] == letter:
            letter = chr(ord("a") + (self._edits + 1) % 26)
        path.write_text(letter + old[1:])

    # -- repositories --------------------------------------------------------

    def base_repo(self, name: str, attributes: str = "") -> Path:
        """A committed repository on ``main`` with ``a.txt``; nothing planted."""
        repo = self.root / name
        repo.mkdir(parents=True)
        g = lambda *a: self.setup_git(repo, *a)  # noqa: E731
        g("init", "-q", "-b", "main")
        g("config", "user.name", "Lab")
        g("config", "user.email", "lab@example.invalid")
        if attributes:
            (repo / ".gitattributes").write_text(attributes)
        (repo / "a.txt").write_text("one\n")
        g("add", "-A")
        g("commit", "-qm", "base")
        return repo

    def marked_repo(self, name: str, edits: bool = True, process: bool = False) -> Path:
        """The shared fixture: every program-running setting planted.

        ``main`` and ``side`` both change ``a.txt`` (so a merge needs the
        merge program). With ``edits``, the working tree then has same-size
        edits to the filtered ``.txt`` and ``.dat`` files. With ``process``,
        ``.dat`` files also name a long-running "process" filter, which fails
        and so stops most commands early: that one has its own cases.
        """
        repo = self.root / name
        repo.mkdir(parents=True)
        g = lambda *a: self.setup_git(repo, *a)  # noqa: E731
        g("init", "-q", "-b", "main")
        g("config", "user.name", "Lab")
        g("config", "user.email", "lab@example.invalid")
        (repo / ".gitattributes").write_text(
            "*.txt filter=mark diff=mark merge=mark\n*.dat filter=proc\n"
        )
        (repo / "a.txt").write_text("1\n2\n3\n4\n5\n6\n")
        (repo / "b.txt").write_text("two\n")
        (repo / "c.dat").write_text("three\n")
        g("add", "-A")
        g("commit", "-qm", "base")
        g("branch", "side")
        g("checkout", "-q", "side")
        (repo / "a.txt").write_text("1\n2\n3\n4\n5\nsix\n")
        g("commit", "-qam", "side")
        g("checkout", "-q", "main")
        (repo / "a.txt").write_text("one\n2\n3\n4\n5\n6\n")
        g("commit", "-qam", "main")
        self.plant(repo, process=process)
        if edits:
            self.same_size_edit(repo / "a.txt")
            self.same_size_edit(repo / "b.txt")
            self.same_size_edit(repo / "c.dat")
        return repo

    def plant(self, repo: Path, process: bool = False) -> None:
        """Write the program-running settings and hooks into the project."""
        g = lambda *a: self.setup_git(repo, *a)  # noqa: E731
        if process:
            g("config", "filter.proc.process", self.path("process"))
        for key, value in (
            ("filter.mark.clean", self.path("clean")),
            ("filter.mark.smudge", self.path("smudge")),
            ("diff.mark.textconv", self.path("textconv")),
            ("diff.external", self.path("extdiff")),
            ("merge.mark.driver", self.path("mergedrv") + " %O %A %B"),
            ("core.fsmonitor", self.path("fsmonitor")),
            ("commit.gpgSign", "true"),
            ("gpg.program", self.path("gpg")),
        ):
            g("config", key, value)
        hooks = Path(g("rev-parse", "--absolute-git-dir").strip()) / "hooks"
        hooks.mkdir(exist_ok=True)
        for hook in HOOKS:
            shutil.copy(self.bin / ("hook-" + hook), hooks / hook)

    def merge_repo(self, name: str) -> Path:
        """``side`` carries a commit with a (fake) signature; ``gpg.program``
        names the marker. Nothing else is planted."""
        repo = self.base_repo(name)
        g = lambda *a: self.setup_git(repo, *a)  # noqa: E731
        g("branch", "side")
        g("checkout", "-q", "side")
        (repo / "b.md").write_text("b\n")
        g("add", "-A")
        tree = g("write-tree").strip()
        parent = g("rev-parse", "HEAD").strip()
        body = (
            f"tree {tree}\nparent {parent}\n"
            "author Lab <lab@example.invalid> 1 +0000\n"
            "committer Lab <lab@example.invalid> 1 +0000\n"
            "gpgsig -----BEGIN PGP SIGNATURE-----\n \n fake\n -----END PGP SIGNATURE-----\n"
            "\nsigned side\n"
        )
        commit = subprocess.run(
            [GIT, "hash-object", "-t", "commit", "-w", "--stdin"], cwd=repo,
            input=body, capture_output=True, text=True, check=True,
        ).stdout.strip()
        g("update-ref", "refs/heads/side", commit)
        g("checkout", "-qf", "main")
        g("reset", "-q", "--hard")
        g("config", "gpg.program", self.path("gpg"))
        return repo

    def include(self, repo: Path, condition: Optional[str], text: str, file_name: str) -> None:
        """Write ``text`` to a settings file in ``.git`` and include it."""
        git_dir = Path(self.setup_git(repo, "rev-parse", "--git-common-dir").strip())
        if not git_dir.is_absolute():
            git_dir = repo / git_dir
        (git_dir / file_name).write_text(text)
        key = "include.path" if condition is None else f"includeIf.{condition}.path"
        self.setup_git(repo, "config", key, file_name)

    def submodule_repo(self, name: str) -> Path:
        """A project with a registered submodule ``sub`` whose own settings
        name a clean filter (``subclean``). The submodule's tracked file has
        a same-size edit."""
        source = self.base_repo(name + "-src", attributes="*.txt filter=subf\n")
        repo = self.base_repo(name)
        self.setup_git(repo, "submodule", "add", "-q", str(source), "sub")
        self.setup_git(repo, "commit", "-qm", "add sub")
        self.setup_git(repo / "sub", "config", "filter.subf.clean", self.path("subclean"))
        self.same_size_edit(repo / "sub" / "a.txt")
        return repo

    def moved_submodule_repo(self, name: str, external: bool) -> Path:
        """A project whose submodule's recorded commit moved (not committed in
        the outer project), whose submodule names an external diff or a text
        converter, and whose own settings send reads into submodules."""
        source = self.base_repo(name + "-src", attributes="*.txt diff=tc\n")
        repo = self.base_repo(name)
        self.setup_git(repo, "submodule", "add", "-q", str(source), "sub")
        self.setup_git(repo, "commit", "-qm", "add sub")
        sub = repo / "sub"
        if external:
            self.setup_git(sub, "config", "diff.external", self.path("subextdiff"))
        else:
            self.setup_git(sub, "config", "diff.tc.textconv", self.path("subtextconv"))
        (sub / "a.txt").write_text("moved\n")
        self.setup_git(sub, "-c", "user.name=Lab", "-c", "user.email=lab@example.invalid",
                       "commit", "-qam", "moved in sub")
        for key, value in (
            ("diff.submodule", "diff"),
            ("status.submoduleSummary", "true"),
            ("submodule.recurse", "true"),
            ("diff.ignoreSubmodules", "none"),
            ("submodule.sub.ignore", "none"),
        ):
            self.setup_git(repo, "config", key, value)
        return repo

    def partial_clone(self, name: str) -> Path:
        """A blob-less partial clone with nothing checked out, so a checkout
        must fetch the missing file lazily from the promisor remote."""
        origin = self.root / (name + "-origin")
        origin.mkdir()
        g = lambda *a: self.setup_git(origin, *a)  # noqa: E731
        g("init", "-q", "-b", "main")
        (origin / "x.md").write_text("x\n")
        g("add", "-A")
        g("-c", "user.name=Lab", "-c", "user.email=lab@example.invalid", "commit", "-qm", "x")
        g("config", "uploadpack.allowFilter", "true")
        repo = self.root / name
        self.setup_git(
            self.root, "clone", "-q", "--no-checkout", "--filter=blob:none",
            f"file://{origin}", str(repo),
        )
        return repo


# ---------------------------------------------------------------------------
# Servers on 127.0.0.1 (no internet needed)

#: The password the smart-HTTP server's ``private/`` repositories accept.
PRIVATE_PASSWORD = "the-right-token"


class SmartHttp:
    """git's own ``http-backend`` behind a small Python server.

    Every request is recorded: its path, the password in its ``Authorization``
    header (decoded, empty when none) and every header it carried. Paths under
    ``/private/`` need Basic auth with :data:`PRIVATE_PASSWORD`.
    """

    def __init__(self, root: Path) -> None:
        import base64
        import http.server
        import threading

        self.root = root
        self.seen: List[Dict[str, object]] = []
        seen = self.seen
        backend_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GIT_PROJECT_ROOT": str(root),
            "GIT_HTTP_EXPORT_ALL": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "HOME": str(root),
            "REMOTE_ADDR": "127.0.0.1",
        }

        class Handler(http.server.BaseHTTPRequestHandler):
            def _go(self) -> None:
                path, _, query = self.path.partition("?")
                auth = self.headers.get("Authorization", "")
                given = ""
                if auth.startswith("Basic "):
                    decoded = base64.b64decode(auth.split()[1]).decode("utf-8", "replace")
                    given = decoded.split(":", 1)[1] if ":" in decoded else ""
                seen.append({
                    "path": path,
                    "password": given,
                    "authorization": auth,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                })
                if path.startswith("/private/") and given != PRIVATE_PASSWORD:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="lab"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if self.command == "POST" else b""
                env = dict(backend_env)
                env.update({
                    "PATH_INFO": path, "QUERY_STRING": query,
                    "REQUEST_METHOD": self.command,
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    "CONTENT_LENGTH": str(len(body)),
                })
                if self.headers.get("Git-Protocol"):
                    env["GIT_PROTOCOL"] = self.headers["Git-Protocol"]
                out = subprocess.run(
                    [GIT, "http-backend"], input=body, env=env, capture_output=True,
                    check=False,
                ).stdout
                head, _, rest = out.partition(b"\r\n\r\n")
                status = 200
                headers = []
                for line in head.decode("latin-1").split("\r\n"):
                    key, _, value = line.partition(":")
                    if key.lower() == "status":
                        status = int(value.split()[0])
                    elif key:
                        headers.append((key, value.strip()))
                self.send_response(status)
                for key, value in headers:
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(rest)))
                self.end_headers()
                self.wfile.write(rest)

            do_GET = do_POST = _go

            def log_message(self, *args: object) -> None:
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self._server.server_address[1]}"

    def url(self, name: str) -> str:
        return f"{self.base}/{name}"

    def passwords(self) -> List[str]:
        return [str(r["password"]) for r in self.seen if r["password"]]

    def header_values(self, name: str) -> List[str]:
        out = []
        for record in self.seen:
            headers = record["headers"]
            assert isinstance(headers, dict)
            if name.lower() in headers:
                out.append(str(headers[name.lower()]))
        return out

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class GitDaemon:
    """``git daemon`` on 127.0.0.1 serving every repository under ``root``."""

    def __init__(self, root: Path) -> None:
        import socket
        import time

        self.port = free_port()
        self._process = subprocess.Popen(
            [GIT, "daemon", "--reuseaddr", "--export-all", f"--base-path={root}",
             "--listen=127.0.0.1", f"--port={self.port}", str(root)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={k: v for k, v in os.environ.items() if k != SECRET_NAME},
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", self.port)) == 0:
                    return
            time.sleep(0.05)
        raise RuntimeError("git daemon did not start")

    def url(self, name: str) -> str:
        return f"git://127.0.0.1:{self.port}/{name}"

    def close(self) -> None:
        self._process.terminate()
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._process.kill()


class Listener:
    """A TCP port that records whether anything connected to it."""

    def __init__(self) -> None:
        import socket
        import threading

        self.connections = 0
        self._socket = socket.socket()
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(5)
        self.port = int(self._socket.getsockname()[1])

        def accept() -> None:
            while True:
                try:
                    conn, _ = self._socket.accept()
                except OSError:
                    return
                self.connections += 1
                conn.close()

        threading.Thread(target=accept, daemon=True).start()

    def close(self) -> None:
        self._socket.close()


def bare_copy_of(lab: "Lab", repo: Path, where: Path) -> Path:
    """A bare clone of ``repo`` at ``where`` that serves over HTTP (and git)."""
    where.parent.mkdir(parents=True, exist_ok=True)
    lab.setup_git(lab.root, "clone", "-q", "--bare", str(repo), str(where))
    lab.setup_git(where, "config", "http.receivepack", "true")
    lab.setup_git(where, "update-server-info")
    return where
