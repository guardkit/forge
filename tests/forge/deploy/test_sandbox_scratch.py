"""sandbox-scratch, and estate-check's item 9b (release -3, TC7).

The sandbox is played by a stand-in 'sbx' that runs what it is asked to run on
this machine, so the code the tool sends into a sandbox is really executed -
against a real git clone with real registered worktrees - and only the
transport is pretended. One real throwaway sandbox drive is recorded in the
release's evidence, not here.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[3]
TOOL = ROOT / "deploy" / "estate" / "sandbox-scratch"
CHECK = ROOT / "deploy" / "estate" / "estate-check"
loader = importlib.machinery.SourceFileLoader("sandbox_scratch", str(TOOL))
spec = importlib.util.spec_from_loader(loader.name, loader)
scratch = importlib.util.module_from_spec(spec)
loader.exec_module(scratch)

GIB = 1024 ** 3

FAKE_SBX = r"""#!/usr/bin/env python3
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ['FAKE_SBX_LOG'], 'a') as log:
    log.write(json.dumps(args) + '\n')
name = os.environ.get('FAKE_SBX_NAME', 'fake-sandbox')
status = os.environ.get('FAKE_SBX_STATUS', 'running')
if args[:2] == ['ls', '--json']:
    print(json.dumps({'sandboxes': [{'name': name, 'status': status}]}))
    raise SystemExit(0)
if args[:1] == ['ls']:
    print('SANDBOX   AGENT   STATUS   PORTS   WORKSPACE')
    print(name + '   shell   ' + status + '      /somewhere')
    raise SystemExit(0)
if args[:1] != ['exec']:
    raise SystemExit(1)
rest = args[1:]
if rest[:1] == ['-i']:
    rest = rest[1:]
if rest[0] != name:
    print('no such sandbox', file=sys.stderr)
    raise SystemExit(1)
rest = rest[1:]
if rest[:1] == ['--']:
    rest = rest[1:]
mutate = os.environ.get('FAKE_SBX_MUTATE')
if mutate and '"action": "stream"' in ' '.join(rest):
    # Another writer, part-way through: the file changes between the record
    # of the source and the copy of it.
    with open(mutate, 'ab') as stream:
        stream.write(b'written by somebody else during the copy')
if rest[:2] == ['docker', 'exec']:
    # The build runner's own container: run its code here, with the runner's
    # environment as the test sets it.
    if os.environ.get('FAKE_RUNNER_DOWN'):
        print('Error: No such container', file=sys.stderr)
        raise SystemExit(1)
    code = rest[rest.index('-c') + 1]
    env = dict(os.environ, PYTHONPATH=os.environ['FAKE_RUNNER_PYTHONPATH'])
    os.execve(os.environ['FAKE_RUNNER_PYTHON'], [os.environ['FAKE_RUNNER_PYTHON'], '-c', code], env)
os.execvp(rest[0], rest)
"""


def _executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd, check=True, capture_output=True,
    )


@pytest.fixture
def sandbox(tmp_path: Path):
    """A clone, the sandbox's two scratch places, and a stand-in sbx."""
    tools = tmp_path / "tools"
    tools.mkdir()
    _executable(tools / "sbx", FAKE_SBX)
    clone = tmp_path / "clone"
    clone.mkdir()
    _git("init", "-q", "-b", "main", cwd=clone)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=clone)
    scratch_root = clone / ".guardkit" / "tmp"
    scratch_root.mkdir(parents=True)
    sandbox_tmp = tmp_path / "sandbox-tmp"
    sandbox_tmp.mkdir()
    host = tmp_path / "host"
    host.mkdir()
    log = tmp_path / "sbx-log.jsonl"
    env = dict(os.environ, PATH=f"{tools}:{os.environ['PATH']}", FAKE_SBX_LOG=str(log))

    def run(*args: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(TOOL), "--sandbox", "fake-sandbox", "--clone", str(clone),
             "--sandbox-tmp", str(sandbox_tmp), *args],
            text=True, capture_output=True, env=env | extra, timeout=120,
        )

    def asked() -> list[list[str]]:
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    return {"run": run, "clone": clone, "scratch": scratch_root, "tmp": sandbox_tmp,
            "host": host, "asked": asked, "tmp_path": tmp_path}


def _plant(folder: Path) -> None:
    (folder / "sub" / "deeper").mkdir(parents=True)
    (folder / "big.bin").write_bytes(os.urandom(300_000))
    (folder / "sub" / "hello.txt").write_text("hello\n")
    (folder / "sub" / "deeper" / "empty").write_bytes(b"")
    (folder / "sub" / "link-to-big").symlink_to("../big.bin")
    (folder / "sub" / "absolute-link").symlink_to("/usr/bin/env")
    (folder / os.fsdecode(b"not-utf8-\xff.txt")).write_bytes(b"odd name")
    (folder / "sub").chmod(0o751)
    (folder / "sub" / "hello.txt").chmod(0o640)


def _snapshot(folder: Path) -> dict[str, tuple]:
    """Everything a copy could change: bytes, modes, times and link text."""
    seen = {}
    for root, dirs, files in os.walk(folder):
        for name in dirs + files + ["."]:
            path = Path(root) / name
            info = os.lstat(path)
            content = None
            if stat.S_ISREG(info.st_mode):
                content = path.read_bytes()
            elif stat.S_ISLNK(info.st_mode):
                content = os.readlink(path)
            seen[str(path)] = (info.st_mode, info.st_size, info.st_mtime_ns, content)
    return seen


# ---------------------------------------------------------------------------
# REFUSALS, before anything is created anywhere
# ---------------------------------------------------------------------------


def test_a_folder_holding_a_registered_worktree_is_refused(sandbox) -> None:
    holder = sandbox["scratch"] / "holds-a-worktree"
    holder.mkdir()
    _git("worktree", "add", "-q", "--detach", str(holder / "wt"), "HEAD", cwd=sandbox["clone"])
    to = sandbox["host"] / "copy"
    done = sandbox["run"]("--copy", "holds-a-worktree", "--to", str(to))
    assert done.returncode == 2, done.stdout + done.stderr
    assert "registered git worktree" in done.stderr
    assert not to.exists() and not list(sandbox["host"].iterdir())


def test_a_worktree_of_another_repository_is_refused_too(sandbox, tmp_path) -> None:
    other = tmp_path / "other-repo"
    other.mkdir()
    _git("init", "-q", "-b", "main", cwd=other)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=other)
    holder = sandbox["tmp"] / "old-runner-worktrees"
    holder.mkdir()
    _git("worktree", "add", "-q", "--detach", str(holder / "build-1"), "HEAD", cwd=other)
    done = sandbox["run"]("--copy", str(holder), "--to", str(sandbox["host"] / "copy"))
    assert done.returncode == 2, done.stdout + done.stderr
    assert "registered git worktree" in done.stderr
    assert "build-1" in done.stderr


def test_factory_runtime_is_refused(sandbox) -> None:
    runtime = sandbox["scratch"] / "factory-runtime"
    (runtime / "receipts").mkdir(parents=True)
    (runtime / "receipts" / "r.json").write_text("{}")
    before = _snapshot(runtime)
    done = sandbox["run"]("--copy", "factory-runtime", "--to", str(sandbox["host"] / "copy"))
    assert done.returncode == 2, done.stdout + done.stderr
    assert "factory-runtime" in done.stderr and "never copied" in done.stderr
    assert not (sandbox["host"] / "copy").exists()
    assert _snapshot(runtime) == before


def test_an_existing_destination_is_refused_and_the_source_is_unchanged(sandbox) -> None:
    folder = sandbox["scratch"] / "september"
    folder.mkdir()
    _plant(folder)
    before = _snapshot(folder)
    to = sandbox["host"] / "already-here"
    to.mkdir()
    (to / "keep.txt").write_text("mine")
    done = sandbox["run"]("--copy", "september", "--to", str(to))
    assert done.returncode == 2, done.stdout + done.stderr
    assert "already exists" in done.stderr
    assert [p.name for p in to.iterdir()] == ["keep.txt"], "an existing folder was merged into"
    assert _snapshot(folder) == before
    # Refused before the sandbox was asked anything at all.
    assert sandbox["asked"]() == []


@pytest.mark.parametrize("where", ["outside", "nested", "symlink", "the-root", "both-roots"])
def test_anything_but_a_top_level_folder_of_the_two_places_is_refused(sandbox, where) -> None:
    scratch_root, tmp = sandbox["scratch"], sandbox["tmp"]
    if where == "outside":
        elsewhere = sandbox["tmp_path"] / "elsewhere"
        elsewhere.mkdir()
        asked = str(elsewhere)
    elif where == "nested":
        (scratch_root / "a" / "b").mkdir(parents=True)
        asked = str(scratch_root / "a" / "b")
    elif where == "symlink":
        (sandbox["tmp_path"] / "real").mkdir()
        (tmp / "pointer").symlink_to(sandbox["tmp_path"] / "real")
        asked = "pointer"
    elif where == "the-root":
        asked = str(scratch_root)
    else:
        (scratch_root / "twice").mkdir()
        (tmp / "twice").mkdir()
        asked = "twice"
    done = sandbox["run"]("--copy", asked, "--to", str(sandbox["host"] / "copy"))
    assert done.returncode == 2, done.stdout + done.stderr
    assert done.stderr.startswith("Refusing: ")
    assert not (sandbox["host"] / "copy").exists()


def test_a_folder_holding_a_pipe_is_refused(sandbox) -> None:
    folder = sandbox["tmp"] / "has-a-pipe"
    folder.mkdir()
    os.mkfifo(folder / "pipe")
    done = sandbox["run"]("--copy", "has-a-pipe", "--to", str(sandbox["host"] / "copy"))
    assert done.returncode == 2
    assert "not a file, a folder or a symbolic link" in done.stderr


def test_a_stopped_sandbox_is_never_asked_because_asking_would_start_it(sandbox) -> None:
    (sandbox["scratch"] / "september").mkdir()
    for args in (["--report"], ["--copy", "september", "--to", str(sandbox["host"] / "copy")]):
        done = sandbox["run"](*args, FAKE_SBX_STATUS="stopped")
        assert done.returncode == 2, done.stdout + done.stderr
        assert "would start it" in done.stderr
    assert all(call[0] != "exec" for call in sandbox["asked"]())


# ---------------------------------------------------------------------------
# THE COPY
# ---------------------------------------------------------------------------


def test_a_copy_is_verified_and_the_source_is_unchanged(sandbox) -> None:
    folder = sandbox["tmp"] / "b9-corrections"
    folder.mkdir()
    _plant(folder)
    before = _snapshot(folder)
    to = sandbox["host"] / "archive" / "stamp" / "b9-corrections"
    done = sandbox["run"]("--copy", "b9-corrections", "--to", str(to))
    assert done.returncode == 0, done.stdout + done.stderr
    assert "verified copy: 8 entries" in done.stdout
    assert _snapshot(folder) == before, "the copy changed the source"
    assert stat.S_IMODE(to.stat().st_mode) == 0o700
    assert (to / "big.bin").read_bytes() == (folder / "big.bin").read_bytes()
    assert os.readlink(to / "sub" / "absolute-link") == "/usr/bin/env"
    assert os.readlink(to / "sub" / "link-to-big") == "../big.bin"
    assert (to / os.fsdecode(b"not-utf8-\xff.txt")).read_bytes() == b"odd name"
    assert stat.S_IMODE((to / "sub" / "hello.txt").stat().st_mode) == 0o640
    source = json.loads(Path(str(to) + ".source-manifest.json").read_text())
    copy = json.loads(Path(str(to) + ".copy-manifest.json").read_text())
    assert copy["verdict"] == "verified copy" and copy["differences"] == []
    files = {row["path"]: row for row in source["entries"]}
    assert files["big.bin"]["size"] == 300_000 and len(files["big.bin"]["sha256"]) == 64
    assert [(r["path"], r.get("sha256")) for r in source["entries"]] == [
        (r["path"], r.get("sha256")) for r in copy["entries"]]


def test_a_source_that_changes_during_the_copy_is_reported_and_both_are_kept(sandbox) -> None:
    folder = sandbox["scratch"] / "still-written"
    folder.mkdir()
    _plant(folder)
    to = sandbox["host"] / "copy"
    done = sandbox["run"]("--copy", "still-written", "--to", str(to),
                          FAKE_SBX_MUTATE=str(folder / "sub" / "hello.txt"))
    assert done.returncode == 1, done.stdout + done.stderr
    assert "copy does not match" in done.stdout
    assert "sub/hello.txt: size was 6" in done.stdout
    assert "Both are kept" in done.stdout
    # Nothing deleted on either side.
    assert (folder / "sub" / "hello.txt").read_bytes().startswith(b"hello\n")
    assert (folder / "big.bin").exists()
    assert (to / "sub" / "hello.txt").exists() and (to / "big.bin").exists()
    record = json.loads(Path(str(to) + ".copy-manifest.json").read_text())
    assert record["verdict"] == "copy does not match"
    assert any("hello.txt" in line for line in record["differences"])


def test_the_tool_has_no_way_to_remove_or_change_anything_in_the_sandbox(sandbox) -> None:
    parser_help = subprocess.run([sys.executable, str(TOOL), "--help"], text=True,
                                 capture_output=True, check=True).stdout
    options = {word.strip(",[]:") for word in parser_help.split() if word.startswith("--")}
    assert options == {"--help", "--sandbox", "--clone", "--sandbox-tmp", "--report", "--copy", "--to"}

    folder = sandbox["tmp"] / "september"
    folder.mkdir()
    _plant(folder)
    assert sandbox["run"]("--report").returncode == 0
    assert sandbox["run"]("--copy", "september", "--to", str(sandbox["host"] / "c")).returncode == 0
    calls = sandbox["asked"]()
    assert calls, "nothing was recorded"
    for call in calls:
        assert call[:1] in (["ls"], ["exec"])
        assert not {"rm", "mv", "chmod", "unlink", "rmdir", "chown"} & set(call)
        if call[0] == "exec":
            assert call[2:4] == ["python3", "-c"] and call[4] == scratch.SANDBOX
    # And the code it sends only reads.
    for token in ("unlink", "os.remove", "rmdir", "rmtree", "os.rename", "os.replace", "chmod",
                  "chown", "truncate", "O_WRONLY", "O_TRUNC", "shutil", "utime"):
        assert token not in scratch.SANDBOX, token


def test_the_report_names_sizes_disks_and_worktrees(sandbox) -> None:
    (sandbox["scratch"] / "factory-runtime").mkdir()
    holder = sandbox["scratch"] / "forge-worktrees"
    holder.mkdir()
    _git("worktree", "add", "-q", "--detach", str(holder / "wt"), "HEAD", cwd=sandbox["clone"])
    september = sandbox["tmp"] / "september"
    september.mkdir()
    (september / "f").write_bytes(b"x" * 2048)
    done = sandbox["run"]("--report")
    assert done.returncode == 0, done.stdout + done.stderr
    out = done.stdout
    assert "the clone's disk" in out and "the sandbox's root disk (/)" in out and "Docker's disk" in out
    assert "nothing was changed" in out
    lines = out.splitlines()
    assert any("2.0 KiB" in line and "september" in line for line in lines)
    assert "never copied" in out.split("factory-runtime", 1)[1].splitlines()[1]
    assert "holds a registered worktree" in out.split("forge-worktrees", 1)[1].splitlines()[1]


# ---------------------------------------------------------------------------
# THE HOST SIDE NEVER WRITES OUTSIDE THE NEW FOLDER, whatever the stream says
# ---------------------------------------------------------------------------


def _stream(*members: tuple[str, str, bytes | str]) -> tarfile.TarFile:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as out:
        for name, kind, payload in members:
            info = tarfile.TarInfo(name)
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                out.addfile(info)
            elif kind == "link":
                info.type, info.linkname = tarfile.SYMTYPE, payload
                out.addfile(info)
            elif kind == "hard":
                info.type, info.linkname = tarfile.LNKTYPE, payload
                out.addfile(info)
            else:
                info.size = len(payload)
                out.addfile(info, io.BytesIO(payload))
    buffer.seek(0)
    return tarfile.open(fileobj=buffer, mode="r|")


@pytest.mark.parametrize("members", [
    [("../escape", "file", b"x")],
    [("/etc/escape", "file", b"x")],
    [("a/../../escape", "file", b"x")],
    [("out", "link", "/"), ("out/escape", "file", b"x")],
    [("hard", "hard", "/etc/passwd")],
    [("missing-parent/file", "file", b"x")],
    [("same", "file", b"x"), ("same", "file", b"y")],
])
def test_a_stream_that_would_land_outside_the_copy_is_refused(tmp_path, members) -> None:
    destination = tmp_path / "copy"
    destination.mkdir()
    with _stream(*members) as stream:
        problem = scratch.unpack(stream, destination)
    assert problem and "the copy stopped there" in problem
    assert sorted(p.name for p in tmp_path.iterdir()) == ["copy"]


# ---------------------------------------------------------------------------
# estate-check item 9b: the build disk inside the sandbox, against its floor
# ---------------------------------------------------------------------------


@pytest.fixture
def disk_check(tmp_path: Path):
    tools = tmp_path / "tools"
    tools.mkdir()
    _executable(tools / "sbx", FAKE_SBX)
    # Everything else the services check asks answers 'nothing here', so every
    # other item fails quickly; only 9b is looked at.
    _executable(tools / "docker", "#!/bin/sh\nexit 0\n")
    _executable(tools / "sudo", "#!/bin/sh\nexit 1\n")
    _executable(tools / "curl", "#!/bin/sh\nprintf '000'\nexit 28\n")
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    settings = tmp_path / "sandbox-forge.yaml"
    env_file = tmp_path / "estate.env"
    env_file.write_text("SANDBOX_NAME=fake-sandbox\nFACTORY_GATEWAY_ADDRESS=127.0.0.1\nFORGE_ANSWER_PORT=8126\n")
    env = dict(os.environ, PATH=f"{tools}:{os.environ['PATH']}",
               FAKE_SBX_LOG=str(tmp_path / "sbx-log.jsonl"),
               FAKE_RUNNER_PYTHON=sys.executable, FAKE_RUNNER_PYTHONPATH=str(ROOT / "src"),
               FORGE_CONFIG_PATH=str(settings), FORGE_AUTOBUILD_WORKTREE_BASE=str(worktrees))
    env.pop("FORGE_AUTOBUILD_MIN_AVAILABLE_BYTES", None)
    free = os.statvfs(worktrees).f_bavail * os.statvfs(worktrees).f_frsize

    def run(floor_gb: float | None, **extra: str) -> str:
        if floor_gb is not None:
            settings.write_text("permissions:\n  filesystem:\n    allowlist: [/tmp]\n"
                                f"resource_preflight:\n  min_available_disk_gb: {floor_gb}\n")
        done = subprocess.run(["bash", str(CHECK), "--env-file", str(env_file), "services"],
                              text=True, capture_output=True, env=env | extra, timeout=120)
        rows = [line for line in done.stdout.splitlines() if " 9b " in line]
        assert len(rows) == 1, done.stdout + done.stderr
        return rows[0]

    return run, free


def test_the_disk_item_fails_below_the_floor(disk_check) -> None:
    run, free = disk_check
    row = run(free / GIB + 50)
    assert row.lstrip().startswith("NOT PASSED"), row
    assert "BELOW the floor in the sandbox's settings file" in row
    assert "refuse the next build" in row


def test_the_disk_item_warns_within_ten_gib_above_the_floor(disk_check) -> None:
    run, free = disk_check
    if free < 6 * GIB:
        pytest.skip("this machine has too little free space to place a floor 5 GiB below it")
    row = run((free - 5 * GIB) / GIB)
    assert row.lstrip().startswith("ok, WARNING"), row
    assert "by less than 10 GiB" in row


def test_the_disk_item_passes_well_above_the_floor(disk_check) -> None:
    run, free = disk_check
    if free < 12 * GIB:
        pytest.skip("this machine has less than 12 GiB free")
    row = run(0.5)
    assert row.lstrip().startswith("ok "), row
    assert "more than 10 GiB above the floor in the sandbox's settings file" in row


def test_the_disk_item_fails_when_the_runner_cannot_read_its_floor(disk_check, tmp_path) -> None:
    run, _ = disk_check
    (tmp_path / "sandbox-forge.yaml").write_text("resource_preflight: [this is not a mapping\n")
    row = run(None)
    assert row.lstrip().startswith("NOT PASSED"), row
    assert "refuses every build" in row


def test_the_disk_item_is_not_a_pass_when_it_could_not_ask(disk_check) -> None:
    run, _ = disk_check
    assert run(1, FAKE_RUNNER_DOWN="1").lstrip().startswith("not checked")
    stopped = run(1, FAKE_SBX_STATUS="stopped")
    assert stopped.lstrip().startswith("not checked") and "asking would start it" in stopped
