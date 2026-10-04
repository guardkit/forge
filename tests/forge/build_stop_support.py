"""Real processes and real runners for the build-stopping checks (3 October 2026).

Nothing here is a stand-in for the runner: the runners are real
``langgraph dev`` servers serving the real ``autobuild_runner`` graph, and a
build's processes are real processes. The one
stand-in is GuardKit itself — a small script, named by ``FORGE_GUARDKIT_PATH``,
that starts the kinds of process a real build starts:

* a grandchild in the child's own group, which writes a heartbeat file into
  the build's worktree every 0.2 s (so "keeps running and keeps its files"
  can be checked);
* a process in a session of its own that ignores SIGTERM, as GuardKit's serve
  probe starts one;

all carrying the build's owner marker. It records every process's number and
start time, and — for a build started behind another — whether the other
build's processes were still alive at the moment it started.

Every process and container a check starts is removed by the check.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import textwrap
import time
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"

FAKE_GUARDKIT = textwrap.dedent(
    """\
    #!{python}
    import json, os, signal, subprocess, sys, time
    HERE = os.path.dirname(os.path.abspath(__file__))
    plan = json.load(open(os.path.join(HERE, "plan.json")))
    feature = sys.argv[sys.argv.index("feature") + 1]
    spec = plan[feature]
    records = plan["_records"]
    os.environ["GUARDKIT_RUN_OWNER"] = spec["build_id"]

    def starttime(pid):
        raw = open("/proc/%d/stat" % pid).read()
        rest = raw.rpartition(")")[2].split()
        return rest[0], int(rest[19])

    def alive(pid, start):
        try:
            state, now = starttime(pid)
        except OSError:
            return False
        return now == start and state not in ("Z", "X")

    # A second launch of the same build (a relaunch) records under its own
    # name, and first says whether the first launch's processes were alive.
    relaunch = os.path.exists(os.path.join(records, feature + ".pids"))
    name = feature + (".relaunch" if relaunch else "")
    must_be_gone = spec.get("must_be_gone", []) + ([feature] if relaunch else [])
    others = {{}}
    for other in must_be_gone:
        try:
            recorded = json.load(open(os.path.join(records, other + ".pids")))
        except OSError:
            others[other] = "no record"
            continue
        others[other] = [p for p, s in recorded if alive(p, s)]

    fixtures = {{}}
    for other in spec.get("fixtures_must_be_gone", []):
        listed = subprocess.run(
            ["docker", "ps", "--quiet", "--filter",
             "label=guardkit.fixture.owner=" + other],
            capture_output=True, text=True,
        )
        fixtures[other] = listed.stdout.split() if listed.returncode == 0 else "unknown"

    beat = (
        "import os, time\\n"
        "while True:\\n"
        "    open(os.path.join(os.getcwd(), 'beat'), 'w').write(str(time.time()))\\n"
        "    time.sleep(0.2)\\n"
    )
    stubborn = (
        "import signal, time\\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n"
        "while True:\\n"
        "    time.sleep(1)\\n"
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", beat],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ),
        subprocess.Popen(
            [sys.executable, "-c", stubborn],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ),
    ]
    time.sleep(0.3)
    pids = [os.getpid()] + [p.pid for p in procs]
    tmp = os.path.join(records, name + ".pids.tmp")
    json.dump([[p, starttime(p)[1]] for p in pids], open(tmp, "w"))
    os.replace(tmp, os.path.join(records, name + ".pids"))
    tmp = os.path.join(records, name + ".started.tmp")
    json.dump(
        {{"at": time.time(), "others_alive": others, "fixtures_alive": fixtures}},
        open(tmp, "w"),
    )
    os.replace(tmp, os.path.join(records, name + ".started"))
    print("== guardkit autobuild start ==", flush=True)
    time.sleep(
        spec.get("relaunch_run_seconds", 600) if relaunch else spec.get("run_seconds", 600)
    )
    if not spec.get("leave_behind"):
        for p in procs:
            p.kill()
    sys.exit(spec.get("exit", 0))
    """
)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        },
    )


@dataclass
class Estate:
    """A temporary estate: a repository, a fake GuardKit and folders."""

    root: Path
    repos: Path
    repo: Path
    records: Path
    guardkit: Path
    plan: dict[str, Any] = field(default_factory=dict)

    def add_build(
        self, feature_id: str, build_id: str, *, branch: str, **spec: Any
    ) -> None:
        if branch != "main":
            _git(self.repo, "branch", branch)
        self.plan[feature_id] = {"build_id": build_id, **spec}
        self.plan["_records"] = str(self.records)
        (self.guardkit.parent / "plan.json").write_text(json.dumps(self.plan))

    def env(self) -> dict[str, str]:
        return {
            "FORGE_GUARDKIT_PATH": str(self.guardkit),
            "FORGE_REPO_BASE": str(self.repos),
            "FORGE_CONFIG_PATH": str(self.root / "no-forge.yaml"),
            "FORGE_RECEIPTS_DIR": str(self.root / "receipts"),
            "FORGE_AUTOBUILD_WORKTREE_BASE": str(self.root / "worktrees"),
            "FORGE_AUTOBUILD_MIN_AVAILABLE_BYTES": "1",
            "FORGE_BUILD_STOP_GRACE_SECONDS": "1",
            "FORGE_AUTOBUILD_TIMEOUT_SECONDS": "600",
        }

    def pids(self, feature_id: str, timeout: float = 60.0) -> list[tuple[int, int]]:
        path = self.records / f"{feature_id}.pids"
        wait_for(path.exists, timeout, f"{feature_id} never started its processes")
        return [tuple(item) for item in json.loads(path.read_text())]

    def started(self, feature_id: str) -> dict[str, Any] | None:
        path = self.records / f"{feature_id}.started"
        return json.loads(path.read_text()) if path.exists() else None


def make_estate(root: Path) -> Estate:
    repos = root / "repos"
    repo = repos / "example"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("example\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    records = root / "records"
    records.mkdir()
    tools = root / "tools"
    tools.mkdir()
    guardkit = tools / "guardkit"
    guardkit.write_text(FAKE_GUARDKIT.format(python=sys.executable))
    guardkit.chmod(0o755)
    (root / "worktrees").mkdir()
    (root / "receipts").mkdir()
    return Estate(
        root=root, repos=repos, repo=repo, records=records, guardkit=guardkit
    )


def launch_message(feature_id: str, build_id: str, branch: str) -> str:
    payload = {
        "build_id": build_id,
        "feature_id": feature_id,
        "repo": "example/example",
        "branch": branch,
        "correlation_id": f"corr-{feature_id}",
    }
    return (
        "RUN_AUTOBUILD subagent=autobuild_runner payload=" + json.dumps(payload)
    )


def proc_alive(pid: int, start: int) -> bool:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    rest = raw.rpartition(")")[2].split()
    return int(rest[19]) == start and rest[0] not in ("Z", "X")


def wait_for(predicate: Any, timeout: float, message: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError(message)


def kill_recorded(estate: Estate) -> None:
    """Kill every process any build recorded (cleanup, whatever happened)."""
    for path in estate.records.glob("*.pids"):
        for pid, start in json.loads(path.read_text()):
            if proc_alive(pid, start):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass


def kill_marked(build_ids: list[str]) -> None:
    """Kill any process carrying one of these owner markers (cleanup)."""
    needles = {f"GUARDKIT_RUN_OWNER={b}".encode() for b in build_ids}
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) == os.getpid():
            continue
        try:
            entries = set(Path(f"/proc/{name}/environ").read_bytes().split(b"\0"))
        except OSError:
            continue
        if entries & needles:
            try:
                os.kill(int(name), signal.SIGKILL)
            except OSError:
                pass


@dataclass
class Runner:
    url: str
    process: subprocess.Popen[bytes]
    log: Path


@contextmanager
def real_runner(
    estate: Estate,
    name: str,
    *,
    jobs: int = 1,
    app: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> Iterator[Runner]:
    """A real ``langgraph dev`` runner with ``jobs`` job slots."""
    home = estate.root / f"runner-{name}"
    home.mkdir()
    config: dict[str, Any] = {
        "dependencies": ["."],
        "graphs": {"autobuild_runner": "forge.subagents.autobuild_runner:graph"},
    }
    if app is not None:
        config["http"] = {"app": app}
    (home / "langgraph.json").write_text(json.dumps(config))
    port = free_port()
    log = home / "runner.log"
    langgraph = Path(sys.executable).parent / "langgraph"
    env = {
        **os.environ,
        **estate.env(),
        "PYTHONPATH": f"{SRC}{os.pathsep}{REPO_ROOT}",
        "LANGGRAPH_NO_ANALYTICS": "1",
        **(extra_env or {}),
    }
    with open(log, "wb") as out:
        process = subprocess.Popen(
            [
                str(langgraph),
                "dev",
                "--config",
                str(home / "langgraph.json"),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-browser",
                "--no-reload",
                "--n-jobs-per-worker",
                str(jobs),
                "--allow-blocking",
            ],
            cwd=home,
            env=env,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    url = f"http://127.0.0.1:{port}"
    try:
        def _up() -> bool:
            if process.poll() is not None:
                raise AssertionError(
                    f"runner {name} exited: {log.read_text()[-3000:]}"
                )
            try:
                with urllib.request.urlopen(url + "/ok", timeout=1) as reply:
                    return reply.status == 200
            except OSError:
                return False

        wait_for(_up, 120, f"runner {name} never answered: {log}")
        yield Runner(url=url, process=process, log=log)
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        process.wait(timeout=30)


def start_fixture_container(build_id: str) -> str:
    """A real labelled container standing in for a build's test fixture."""
    done = subprocess.run(
        [
            "docker",
            "run",
            "--detach",
            "--label",
            f"guardkit.fixture.owner={build_id}",
            "--label",
            "forge.test=concurrent-builds",
            "busybox:1.36",
            "sleep",
            "600",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return done.stdout.strip()


def remove_test_containers(build_ids: list[str]) -> None:
    for build_id in build_ids:
        ids = subprocess.run(
            [
                "docker",
                "ps",
                "--all",
                "--quiet",
                "--filter",
                "label=forge.test=concurrent-builds",
                "--filter",
                f"label=guardkit.fixture.owner={build_id}",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout.split()
        if ids:
            subprocess.run(
                ["docker", "rm", "--force", *ids], capture_output=True, timeout=60
            )


def container_running(container_id: str) -> bool:
    done = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Running}}", container_id],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return done.returncode == 0 and done.stdout.strip() == "true"


def docker_available() -> bool:
    try:
        return (
            subprocess.run(
                ["docker", "image", "inspect", "busybox:1.36"],
                capture_output=True,
                timeout=30,
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


def refusing_engine(root: Path) -> tuple[Path, Path]:
    """A real engine client whose ``rm`` is refused while a file exists."""
    refuse = root / "refuse-rm"
    refuse.write_text("on")
    wrapper = root / "engine-refusing-rm"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "rm" ] && [ -e "{refuse}" ]; then\n'
        "  echo 'removal refused' >&2; exit 1\n"
        "fi\n"
        'exec docker "$@"\n'
    )
    wrapper.chmod(0o755)
    return wrapper, refuse


def marked_alive(build_id: str) -> list[int]:
    """Live processes carrying this build's owner marker."""
    needle = f"GUARDKIT_RUN_OWNER={build_id}".encode()
    found = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            entries = Path(f"/proc/{name}/environ").read_bytes().split(b"\0")
            state = Path(f"/proc/{name}/stat").read_text().rpartition(")")[2].split()[0]
        except OSError:
            continue
        if needle in entries and state not in ("Z", "X"):
            found.append(int(name))
    return found
