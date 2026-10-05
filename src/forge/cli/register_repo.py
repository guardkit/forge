"""``forge register-repo`` — register a project on GitHub with the running factory.

The container set-up (design of 5 October 2026, "Registering more projects in
the factory", Part 2). The factory runs as containers; every project is built
in ONE shared Docker Sandbox (``api-test-deploy``), whose workspace is the
api_test clone. A registered project gets the factory's own clone of it in a
projects folder inside that clone's git-ignored runtime folder, so no sandbox
rebuild, mount, port or start-up script change is needed.

What the command does, in order, saying one plain line per step:

1. **Checks the project and authors nothing in it.** It shallow-clones the
   project's default branch into a temporary folder and reads
   ``.guardkit/config.yaml``: ``memory.project`` must be declared, a toolchain
   test command must be declared, and every declared binding document
   (``autobuild.player.required_documents``) must exist as an ordinary file.
   Anything missing is refused with the lines to add. It never writes to the
   project's repository.
2. **Prepares, and changes nothing live.** Every settings file it edits is
   written beside the live one as a staged copy named
   ``<file>.<leaf>-pending``, with a dated backup of the live file:

   * the coordinator's ``forge.yaml`` in the settings volume, read and written
     through a throwaway ``alpine`` container, as the one-page upgrade
     procedure does;
   * the sandbox's own ``forge.yaml`` inside the api_test clone, read and
     written through ``sbx exec``;
   * with ``--publish``, the publisher's settings file, with one new route.

   Each YAML file gains exactly three entries — the allowlist path, the
   ``planning.target_repo_paths`` key and the ``planning.sandboxes`` entry
   (copying api_test's sandbox name and addresses from the same file) — by
   surgical line insertion that keeps every comment, and must re-parse with
   :func:`forge.config.loader.load_config` or nothing is written.
3. **The factory's clone**, in the sandbox, as the sandbox user: refused when
   the projects folder is not git-ignored in the api_test clone, when the
   sandbox cannot read the repository (a private repository needs a read
   credential, which is an owner decision), or when a folder is already there
   with a different origin; reused when it is there with the same origin.
4. **Says what makes it live.** It prints the one-page procedure's own
   sequence — close intake, confirm drained, stop, swap the staged files in,
   restart the sandbox supervisor, start the publisher when its routes
   changed, start the rest, check — and restarts nothing itself.

``--check-drained`` is step (b) of that sequence: it reads the ledger, the work
queue and the two durable bus consumers that feed the coordinator, and says
DRAINED only when every one of them reads zero.

``--dry-run`` does step 1 and prints what the rest would do; it writes nothing
anywhere.

Everything that reaches outside this process — git, docker and sbx — goes
through :data:`run_command`, one small seam the tests replace with fakes.

Exit codes: 0 = registered, prepared, unchanged or drained; 1 = refused or not
drained, with a plain sentence saying which check said no.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

import click
import yaml

__all__ = ["register_repo_cmd"]


# ---------------------------------------------------------------------------
# The estate's facts. Every one is a default for an option, so a different
# estate passes its own; none of them is a path on anybody's machine.
# ---------------------------------------------------------------------------

#: The one shared sandbox every project is built in (Rich, 5 October 2026).
DEFAULT_SANDBOX: str = "api-test-deploy"

#: The user the sandbox's helper and runner run as, and the owner of the clone.
SANDBOX_USER: str = "1000"

#: The coordinator's settings volume and the file in it, as the one-page
#: upgrade procedure names them.
DEFAULT_SETTINGS_VOLUME: str = "forge-estate_forge-settings"
SETTINGS_FILE_NAME: str = "forge.yaml"

#: The throwaway image the volume is read and written through, as the
#: procedure does (``docker run --rm -v <volume>:/s alpine ...``).
VOLUME_HELPER_IMAGE: str = "alpine"

#: The Compose project the estate runs as, and its coordinator container.
COMPOSE_PROJECT: str = "forge-estate"
DEFAULT_COORDINATOR_CONTAINER: str = "forge-estate-coordinator-1"

#: Where the coordinator keeps its ledger (the ``forge-ledger`` volume,
#: ``deploy/compose/compose.yaml``); its own ``FORGE_DB_PATH`` wins when set.
COORDINATOR_LEDGER: str = "/var/lib/forge/forge.db"

#: Where the coordinator's settings name a project's folder. For a project
#: with a sandbox the coordinator never reads this folder: every read goes to
#: the sandbox helper by the project's key (see :data:`COORDINATOR_FOLDER_NOTE`).
COORDINATOR_PROJECTS_ROOT: str = "/var/lib/forge/projects"

#: The api_test clone's git-ignored runtime folder, the sandbox's own
#: settings file in it, and the projects folder registered projects live in.
RUNTIME_FOLDER: str = ".guardkit/tmp/factory-runtime"
SANDBOX_SETTINGS_IN_CLONE: str = f"{RUNTIME_FOLDER}/forge.yaml"
PROJECTS_FOLDER: str = f"{RUNTIME_FOLDER}/projects"

#: The project whose sandbox entry a new project copies: the sandbox name and
#: its two addresses are the same for every project in the shared sandbox.
REFERENCE_PROJECT: str = "guardkit/api_test"

#: The publisher fetches a project's clone from sbx's git daemon, which serves
#: anything inside the api_test clone (its base path is the clone's parent
#: folder) and is published on the factory gateway address at this port:
#: ``git://<gateway>:<port>/<clone folder name>/<projects folder>/<leaf>``. The
#: gateway address is this machine's, so it is never written here: it comes
#: from the estate env file's FACTORY_GATEWAY_ADDRESS or --gateway-address.
DEFAULT_GIT_EXPORT_PORT: int = 8918

#: The settings in the estate env file and the sandbox's bootstrap env file
#: this command takes its defaults from.
ESTATE_ENV_SANDBOX_FILE: str = "SANDBOX_PROJECT_ENV_FILE"
ESTATE_ENV_PUBLISHER_SETTINGS: str = "FORGE_PUBLISHER_SETTINGS_FILE"
ESTATE_ENV_COMPOSE_FILE: str = "COMPOSE_FILE"
ESTATE_ENV_GATEWAY: str = "FACTORY_GATEWAY_ADDRESS"
SANDBOX_ENV_CONFIG_PATH: str = "FORGE_CONFIG_PATH"

#: The three services that let work in: the two that receive Slack, and the
#: watch on the gateway (stopped with them so it does not raise an alarm about
#: a gateway that was stopped on purpose). The procedure starts them last.
INTAKE_SERVICES: tuple[str, ...] = ("front-door", "bus-gateway", "gateway-watch")

#: The publisher's Compose service and the sandbox supervisor's.
PUBLISHER_SERVICE: str = "forge-publisher"
SANDBOX_SUPERVISOR_SERVICE: str = "sandbox-runner"

#: The bus stream the coordinator's two durable consumers read.
BUS_STREAM: str = "PIPELINE"

#: How long one outside command may take. A clone gets longer.
COMMAND_TIMEOUT: float = 120.0
CLONE_TIMEOUT: float = 900.0

#: What a project's name and its last part may be: the repository map key is
#: ``org/name``, and the last part becomes a folder name.
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")

#: A YAML scalar written as it is; anything else is written double-quoted.
_PLAIN_SCALAR = re.compile(r"^[A-Za-z0-9_./-]+$")

#: What the coordinator does with its folder for a project that has a sandbox,
#: found 5 October 2026 by reading the code rather than assumed.
COORDINATOR_FOLDER_NOTE: str = (
    f"not created: for a project with a sandbox the coordinator never reads "
    f"its {COORDINATOR_PROJECTS_ROOT}/<leaf> folder — planning facts, worktrees, "
    f"repairs and the toolchain are read through the sandbox helper by the "
    f"project's key — and api_test's folder there does not exist either"
)


# ---------------------------------------------------------------------------
# The report — one plain line per step
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    """One reported line: ``<step> <status> <detail>``."""

    step: str
    status: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"step": self.step, "status": self.status, "detail": self.detail}


def _render(steps: Sequence[Step]) -> str:
    """Render the steps as aligned columns, one line each."""
    if not steps:
        return ""
    step_w = max(len(s.step) for s in steps)
    status_w = max(len(s.status) for s in steps)
    return "\n".join(
        f"{s.step.ljust(step_w)}  {s.status.ljust(status_w)}  {s.detail}".rstrip()
        for s in steps
    )


def _emit(steps: Sequence[Step], *, as_json: bool, tail: Sequence[str] = ()) -> None:
    if as_json:
        click.echo(
            json.dumps(
                {"steps": [s.as_dict() for s in steps], "activation": list(tail)},
                indent=2,
            )
        )
        return
    rendered = _render(steps)
    if rendered:
        click.echo(rendered)
    if tail:
        click.echo("")
        for line in tail:
            click.echo(line)


# ---------------------------------------------------------------------------
# Surgical YAML line insertion
#
# Everything below edits YAML as *lines of text*. It never round-trips a
# document through ``yaml.dump``, because a round trip drops every comment in
# the file and the comments in this estate's ``forge.yaml`` are the record of
# why entries exist. The functions locate a key by walking indentation, then
# insert one line in the right place.
# ---------------------------------------------------------------------------


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_ignorable(line: str) -> bool:
    """True for blank lines and whole-line comments."""
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


@dataclass(frozen=True)
class _Block:
    """Where a key lives and how far its children run."""

    key_line: int
    key_indent: int
    #: First line index at or after ``key_line + 1`` that is NOT a child.
    block_end: int
    #: Index of the last non-blank, non-comment child, or ``None``.
    last_content_child: int | None
    #: Indent of the first content child, or ``None`` when there are none.
    child_indent: int | None
    #: Text after the ``key:`` on the key line (``""`` when the value is a block).
    inline_value: str


def _block_extent(lines: Sequence[str], key_line: int, key_indent: int) -> _Block:
    """Measure the block a key at ``key_line`` owns."""
    block_end = len(lines)
    for i in range(key_line + 1, len(lines)):
        if _is_ignorable(lines[i]):
            continue
        indent = _indent_of(lines[i])
        is_seq_item = lines[i].lstrip().startswith("- ") or lines[i].strip() == "-"
        if indent > key_indent:
            continue
        if indent == key_indent and is_seq_item:
            # A block sequence's items sit at the same indent as its key.
            continue
        block_end = i
        break

    last_content: int | None = None
    child_indent: int | None = None
    for i in range(key_line + 1, block_end):
        if _is_ignorable(lines[i]):
            continue
        last_content = i
        if child_indent is None:
            child_indent = _indent_of(lines[i])

    _, _, after = lines[key_line].partition(":")
    return _Block(
        key_line=key_line,
        key_indent=key_indent,
        block_end=block_end,
        last_content_child=last_content,
        child_indent=child_indent,
        inline_value=after.strip(),
    )


def _find_key(
    lines: Sequence[str], key: str, *, start: int, end: int, indent: int
) -> int | None:
    """Index of the line declaring ``key:`` at ``indent`` within ``[start, end)``."""
    for i in range(start, end):
        line = lines[i]
        if _is_ignorable(line):
            continue
        if _indent_of(line) != indent:
            continue
        stripped = line.strip()
        if stripped.startswith("- "):
            continue
        name, sep, _ = stripped.partition(":")
        if sep and name.strip() == key:
            return i
    return None


def locate(lines: Sequence[str], path: Sequence[str]) -> _Block | None:
    """Find the block for a dotted key path, or ``None`` when a segment is absent."""
    start, end, indent = 0, len(lines), 0
    block: _Block | None = None
    for key in path:
        found = _find_key(lines, key, start=start, end=end, indent=indent)
        if found is None:
            return None
        block = _block_extent(lines, found, indent)
        start, end = found + 1, block.block_end
        indent = block.child_indent if block.child_indent is not None else indent + 2
    return block


class YamlEditRefused(Exception):
    """The file is shaped in a way this surgical editor will not touch."""


def _ensure_block(lines: list[str], path: Sequence[str]) -> _Block:
    """Return the block for ``path``, creating any missing levels as empty keys.

    A missing level is written as a bare ``key:`` line after the last content
    child of the deepest level that does exist (or at the end of the file when
    nothing in ``path`` exists at all).
    """
    existing: _Block | None = None
    depth = 0
    for depth in range(len(path), 0, -1):
        existing = locate(lines, path[:depth])
        if existing is not None:
            break
    else:
        depth = 0

    if existing is not None and depth == len(path):
        return existing

    if existing is None:
        insert_at = len(lines)
        indent = 0
        depth = 0
    else:
        if existing.inline_value and existing.inline_value not in ("{}", "[]"):
            raise YamlEditRefused(
                f"{'.'.join(path[:depth])} has a value on the same line, so this "
                "command cannot add to it safely — edit the file by hand"
            )
        insert_at = (
            existing.last_content_child + 1
            if existing.last_content_child is not None
            else existing.key_line + 1
        )
        indent = (
            existing.child_indent
            if existing.child_indent is not None
            else existing.key_indent + 2
        )

    new_lines: list[str] = []
    for offset, key in enumerate(path[depth:]):
        new_lines.append(f"{' ' * (indent + offset * 2)}{key}:")
    lines[insert_at:insert_at] = new_lines

    located = locate(lines, path)
    if located is None:  # pragma: no cover — defensive
        raise YamlEditRefused(f"could not create {'.'.join(path)} in the config")
    return located


def _insertion_point(block: _Block, *, sequence: bool) -> tuple[int, int]:
    """Where a new child line goes and at what indent."""
    if block.inline_value and block.inline_value not in ("{}", "[]"):
        raise YamlEditRefused(
            "the key has a value on the same line, so this command cannot add "
            "to it safely — edit the file by hand"
        )
    if block.last_content_child is not None:
        return block.last_content_child + 1, (
            block.child_indent
            if block.child_indent is not None
            else block.key_indent + (0 if sequence else 2)
        )
    # No children yet: a block sequence sits at the key's own indent, a block
    # mapping two spaces in.
    return block.key_line + 1, block.key_indent + (0 if sequence else 2)


def append_sequence_item(lines: list[str], path: Sequence[str], value: str) -> None:
    """Append ``- value`` to the block sequence at ``path``."""
    block = _ensure_block(lines, path)
    if block.inline_value == "[]":
        lines[block.key_line] = lines[block.key_line].split(":", 1)[0] + ":"
        block = _block_extent(lines, block.key_line, block.key_indent)
    at, indent = _insertion_point(block, sequence=True)
    lines.insert(at, f"{' ' * indent}- {value}")


def set_mapping_entry(
    lines: list[str], path: Sequence[str], key: str, value: str
) -> None:
    """Add ``key: value`` to the block mapping at ``path``."""
    block = _ensure_block(lines, path)
    if block.inline_value == "{}":
        lines[block.key_line] = lines[block.key_line].split(":", 1)[0] + ":"
        block = _block_extent(lines, block.key_line, block.key_indent)
    at, indent = _insertion_point(block, sequence=False)
    lines.insert(at, f"{' ' * indent}{key}: {value}")


def _scalar(value: str) -> str:
    """``value`` as a YAML scalar that reads back as exactly that text."""
    text = str(value)
    if _PLAIN_SCALAR.match(text):
        try:
            if yaml.safe_load(text) == text:
                return text
        except yaml.YAMLError:  # pragma: no cover — a plain match always parses
            pass
    return json.dumps(text)


def _without_empty_flow(line: str) -> str:
    """A ``key: []`` / ``key: {}`` line as the editor rewrites it (``key:``)."""
    stripped = line.rstrip()
    for empty in (" []", " {}"):
        if stripped.endswith(":" + empty):
            return stripped[: -len(empty)]
    return line


def _keeps_every_line(old: Sequence[str], new: Sequence[str]) -> bool:
    """True when every line of ``old`` is still in ``new``, in order.

    Comments included: the live files' comments are the record of why entries
    exist. The one rewrite the editor makes, ``key: []`` becoming ``key:``
    before its first item, is allowed for.
    """
    remaining = iter(_without_empty_flow(line) for line in new)
    return all(_without_empty_flow(line) in remaining for line in old)


# ---------------------------------------------------------------------------
# The seam — every outside command goes through here
# ---------------------------------------------------------------------------


#: ``run(argv, *, input=None, timeout=...)`` → a finished process. It is the
#: only way this module reaches git, docker or sbx, so the tests can answer for
#: all three without any of them being installed, running or touched.
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def _run_command(
    argv: Sequence[str], *, input: str | None = None, timeout: float = COMMAND_TIMEOUT
) -> "subprocess.CompletedProcess[str]":
    """Run one command, capturing its output. Never uses a shell."""
    return subprocess.run(  # noqa: S603 — fixed argv, no shell
        list(argv),
        input=input,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


#: The seam itself. Tests rebind this module attribute.
run_command: Runner = _run_command


def _call(
    run: Runner,
    argv: Sequence[str],
    *,
    input: str | None = None,
    timeout: float = COMMAND_TIMEOUT,
) -> "subprocess.CompletedProcess[str]":
    """Run through the seam; a command that could not start reads as a failure."""
    try:
        return run(list(argv), input=input, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(list(argv), 127, "", f"{type(exc).__name__}: {exc}")


def _said(result: "subprocess.CompletedProcess[str]") -> str:
    """The last line a failed command printed, or its exit code."""
    lines = (result.stderr or result.stdout or "").strip().splitlines()
    return lines[-1].strip() if lines else f"exit {result.returncode}"


class Refused(Exception):
    """One check said no. ``step`` names it; ``detail`` is the plain sentence."""

    def __init__(self, step: str, detail: str) -> None:
        super().__init__(detail)
        self.step = step
        self.detail = detail


# ---------------------------------------------------------------------------
# Where the three settings files live: three small stores, one shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VolumeStore:
    """Files in the coordinator's settings volume, through a throwaway container.

    Read: ``docker run --rm -v <volume>:/s:ro alpine cat /s/<file>``. Write: the
    text goes into a private temporary file and is copied in with ``docker run
    --rm --user 1000:1000 -v <volume>:/s -v <tmp>:/in:ro alpine cp /in /s/<file>``,
    keeping the owner, exactly as the one-page procedure puts a settings file in.
    """

    volume: str
    run: Runner

    def where(self, name: str) -> str:
        return f"{self.volume}:/{name}"

    def read(self, name: str) -> str | None:
        result = _call(
            self.run,
            ["docker", "run", "--rm", "-v", f"{self.volume}:/s:ro",
             VOLUME_HELPER_IMAGE, "cat", f"/s/{name}"],
        )
        return result.stdout if result.returncode == 0 else None

    def names(self) -> list[str]:
        result = _call(
            self.run,
            ["docker", "run", "--rm", "-v", f"{self.volume}:/s:ro",
             VOLUME_HELPER_IMAGE, "ls", "-1", "/s"],
        )
        return result.stdout.split() if result.returncode == 0 else []

    def write(self, name: str, text: str) -> None:
        with tempfile.TemporaryDirectory(prefix="forge-register-") as folder:
            local = Path(folder) / name
            local.write_text(text, encoding="utf-8")
            result = _call(
                self.run,
                ["docker", "run", "--rm", "--user", f"{SANDBOX_USER}:{SANDBOX_USER}",
                 "-v", f"{self.volume}:/s", "-v", f"{local}:/in:ro",
                 VOLUME_HELPER_IMAGE, "cp", "/in", f"/s/{name}"],
            )
        if result.returncode != 0:
            raise Refused("coordinator", f"could not write {name} into {self.volume} ({_said(result)})")
        if self.read(name) != text:
            raise Refused("coordinator", f"{name} in {self.volume} did not read back as written")


@dataclass(frozen=True)
class SandboxStore:
    """Files and commands inside the shared sandbox, as its own user.

    Every call is ``sbx exec -u 1000 <sandbox> ...``; a write hands the text in
    on standard input (``-i``) to ``sh -c 'cat > "$1"'``.
    """

    sandbox: str
    run: Runner

    def exec(
        self,
        *argv: str,
        input: str | None = None,
        env: Sequence[str] = (),
        timeout: float = COMMAND_TIMEOUT,
    ) -> "subprocess.CompletedProcess[str]":
        command = ["sbx", "exec"]
        if input is not None:
            command.append("-i")
        command += ["-u", SANDBOX_USER]
        for item in env:
            command += ["-e", item]
        command += [self.sandbox, *argv]
        return _call(self.run, command, input=input, timeout=timeout)

    def where(self, name: str) -> str:
        return f"{self.sandbox}:{name}"

    def read(self, name: str) -> str | None:
        result = self.exec("cat", name)
        return result.stdout if result.returncode == 0 else None

    def names(self, folder: str) -> list[str]:
        result = self.exec("ls", "-1", folder)
        return result.stdout.split() if result.returncode == 0 else []

    def write(self, name: str, text: str) -> None:
        result = self.exec("sh", "-c", 'cat > "$1"', "register-repo", name, input=text)
        if result.returncode != 0:
            raise Refused("sandbox", f"could not write {name} in {self.sandbox} ({_said(result)})")
        if self.read(name) != text:
            raise Refused("sandbox", f"{name} in {self.sandbox} did not read back as written")


@dataclass(frozen=True)
class HostStore:
    """The publisher's settings file on this machine. Mode is kept (it is 600)."""

    def where(self, name: str) -> str:
        return name

    def read(self, name: str) -> str | None:
        try:
            return Path(name).read_text(encoding="utf-8")
        except OSError:
            return None

    def names(self, folder: str) -> list[str]:
        try:
            return sorted(os.listdir(folder))
        except OSError:
            return []

    def write(self, name: str, text: str, *, like: str | None = None) -> None:
        mode = 0o600
        if like is not None:
            try:
                mode = Path(like).stat().st_mode & 0o777
            except OSError:
                pass
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(name, mode)


# ---------------------------------------------------------------------------
# Small file readers: env files
# ---------------------------------------------------------------------------


def read_env_file(path: Path) -> dict[str, str]:
    """``NAME=value`` lines of an env file. Comments and blank lines skipped.

    Only names are looked up here, never printed: the estate env file also
    holds addresses with credentials in them.
    """
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if name.startswith("export "):
            name = name[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[name] = value
    return values


def _resolve_beside(value: str, anchor: Path) -> Path:
    """A path an env file names, read relative to the env file's own folder."""
    candidate = Path(value).expanduser()
    return candidate if candidate.is_absolute() else (anchor.parent / candidate)


# ---------------------------------------------------------------------------
# Step 1 — check the project, author nothing
# ---------------------------------------------------------------------------


def _read_repo_config_dict(repo: Path) -> dict[str, Any]:
    """The project's guardkit config as a plain dict, or ``{}``."""
    path = repo / ".guardkit" / "config.yaml"
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — an unreadable config declares nothing
        return {}
    return data if isinstance(data, dict) else {}


def _guardkit_is_importable() -> bool:
    """Is guardkit's declaration loader importable in this interpreter?"""
    from importlib.util import find_spec

    try:
        return find_spec("guardkit.orchestrator.toolchain_declaration") is not None
    except Exception:  # noqa: BLE001 — an import machinery failure is "no"
        return False


def _declared_test_command(repo: Path) -> str | None:
    """The project's declared ``toolchain.test``, or ``None``.

    guardkit's own loader first, through the reader the factory's gates use
    (:func:`forge.cli._serve_conductor.load_declared_toolchain`), so the answer
    is the one the merge-ready check will get; the plain ``toolchain.test`` key
    otherwise, because that reader answers ``None`` both for "declares nothing"
    and for "guardkit is not importable here".
    """
    declaration = None
    if _guardkit_is_importable():
        try:
            from forge.cli._serve_conductor import load_declared_toolchain

            declaration = load_declared_toolchain(repo)
        except Exception:  # noqa: BLE001 — a reader defect is "not declared"
            declaration = None
    if declaration is not None:
        command = getattr(declaration, "test", None)
        if command:
            return str(command)
    raw = _read_repo_config_dict(repo).get("toolchain")
    if isinstance(raw, dict) and raw.get("test"):
        return str(raw["test"])
    return None


#: The lines a project adds to declare its test command.
THE_TOOLCHAIN_LINES: str = "  toolchain:\n    test: <the command that runs this project's tests>"


def _document_problems(repo: Path, config_text: str | None) -> list[str]:
    """One sentence per declared binding document that builds would refuse.

    Read with the same reader planning and admission use
    (:func:`forge.planning.declared_memory.read_declared_project_documents`).
    A document must be an ordinary file in the checkout: missing, a folder, a
    symbolic link, or reached through a linked folder is named.
    """
    from forge.planning.declared_memory import (
        BINDING_DOCUMENTS_FIELD,
        read_declared_project_documents,
    )

    declared, why = read_declared_project_documents(config_text or None)
    if why:
        return [f"the declared binding documents cannot be used: {why}"]
    problems: list[str] = []
    for path in (entry.path for entry in declared.documents):
        parts = PurePosixPath(path).parts
        linked = next(
            (
                "/".join(parts[:index])
                for index in range(1, len(parts) + 1)
                if repo.joinpath(*parts[:index]).is_symlink()
            ),
            None,
        )
        if linked == path:
            problems.append(f"the declared document {path} is a link; builds need an ordinary file")
        elif linked is not None:
            problems.append(
                f"the declared document {path} is reached through a link ({linked}); "
                "builds need an ordinary file"
            )
        elif not (repo / path).exists():
            problems.append(
                f"the declared document {path} ({BINDING_DOCUMENTS_FIELD}) is not in "
                "the project; commit it, or take it out of the list"
            )
        elif not (repo / path).is_file():
            problems.append(f"the declared document {path} is not an ordinary file")
    return problems


def check_project(run: Runner, *, key: str, url: str, folder: Path) -> list[Step]:
    """Step 1: a shallow clone of the default branch, read, never written.

    Returns the report lines; raises :class:`Refused` naming every gap at once.
    """
    from forge.planning.declared_memory import DECLARATION_PATH, read_declared_memory

    checkout = folder / "project"
    cloned = _call(
        run,
        ["env", "GIT_TERMINAL_PROMPT=0", "git", "clone", "--depth", "1", "--quiet",
         "--", url, str(checkout)],
        timeout=CLONE_TIMEOUT,
    )
    if cloned.returncode != 0 or not checkout.is_dir():
        raise Refused("project", f"could not read {url} ({_said(cloned)})")
    head = _call(run, ["git", "-C", str(checkout), "rev-parse", "HEAD"])
    commit = head.stdout.strip()[:12] if head.returncode == 0 and head.stdout.strip() else "its default branch"

    config_path = checkout / DECLARATION_PATH
    found = config_path.exists() or config_path.is_symlink()
    content: str | None = None
    unreadable: str | None = None
    if found:
        if config_path.is_symlink() or not config_path.is_file():
            unreadable = "it is not an ordinary file"
        else:
            try:
                content = config_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                unreadable = f"it could not be read ({type(exc).__name__})"

    problems: list[str] = []
    memory = read_declared_memory(
        repo=key, commit=commit, content=content, found=found, unreadable_because=unreadable
    )
    if not memory.ok:
        problems.append(str(memory.refusal))
    test_command = _declared_test_command(checkout) if content is not None else None
    if not test_command:
        problems.append(
            f"{key} declares no test command in {DECLARATION_PATH} at {commit}, so the "
            "merge-ready check would have nothing to run. Add these lines, commit "
            f"them to the default branch, and register again:\n{THE_TOOLCHAIN_LINES}"
        )
    problems += _document_problems(checkout, content)
    if problems:
        raise Refused("project", "\n".join(problems))
    return [
        Step("project", "ok", f"{url} at {commit}: memory {memory.project}, tests `{test_command}`"),
    ]


# ---------------------------------------------------------------------------
# Steps 2, 4 and 6 — the edits, computed and checked before anything is written
# ---------------------------------------------------------------------------


def _address_names_filled(text: str) -> dict[str, str]:
    """This process's environment, plus a stand-in for every ``${NAME}`` unset.

    The coordinator's settings name their addresses (``${FORGE_SANDBOX_...}``)
    and the coordinator fills them from its own environment at start. Here only
    the SHAPE is being checked, so an unset name gets an address that is plainly
    a stand-in; nothing is ever sent to it.
    """
    environ = dict(os.environ)
    for name in re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", text):
        if not str(environ.get(name, "")).strip():
            environ[name] = "http://register-repo-shape-check.invalid"
    return environ


def _loads_as_forge_settings(text: str) -> str | None:
    """``None`` when ``text`` loads with the factory's own loader; else why not."""
    from forge.config.loader import load_config

    with tempfile.TemporaryDirectory(prefix="forge-register-") as folder:
        path = Path(folder) / SETTINGS_FILE_NAME
        path.write_text(text, encoding="utf-8")
        try:
            load_config(path, environ=_address_names_filled(text))
        except Exception as exc:  # noqa: BLE001 — any refusal is the answer
            first = str(exc).strip().splitlines()
            return f"{type(exc).__name__}: {first[0] if first else 'no detail'}"
    return None


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


@dataclass
class YamlEdit:
    """What one settings file needs: the new text (``None`` = nothing) and why."""

    which: str
    original: str
    staged: str | None
    steps: list[Step] = field(default_factory=list)
    reference_entry: dict[str, str] = field(default_factory=dict)


def plan_settings_edit(text: str, *, which: str, key: str, path: str) -> YamlEdit:
    """The three entries ``key`` needs in one ``forge.yaml``, by line insertion.

    ``path`` is the project's folder as that file's own world knows it. The
    sandbox entry copies the reference project's name and addresses from the
    same file, as written (an address may be a ``${NAME}``). Refuses — writing
    nothing — when an entry already says something else, when the result would
    not load, when it would change anything but the three entries, or when a
    line of the original (a comment included) would be lost.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise Refused(which, f"its forge.yaml is not readable YAML ({exc.__class__.__name__})") from None
    if not isinstance(data, dict):
        raise Refused(which, "its forge.yaml is not a set of settings")
    planning = _mapping(data.get("planning"))
    sandboxes = _mapping(planning.get("sandboxes"))
    reference = sandboxes.get(REFERENCE_PROJECT)
    wanted_keys = ("name", "sidecar_url", "runner_url")
    if not isinstance(reference, dict) or not all(
        isinstance(reference.get(k), str) and reference.get(k) for k in wanted_keys
    ):
        raise Refused(
            which,
            f"its forge.yaml has no complete planning.sandboxes entry for "
            f"{REFERENCE_PROJECT} to copy the sandbox name and addresses from",
        )
    entry = {k: str(reference[k]) for k in wanted_keys}
    filesystem = _mapping(_mapping(data.get("permissions")).get("filesystem"))
    allowlist = [str(item) for item in (filesystem.get("allowlist") or [])]
    repo_paths = _mapping(planning.get("target_repo_paths"))

    lines = text.split("\n")
    expected = copy.deepcopy(data)
    steps: list[Step] = []
    changed = False
    try:
        if path in allowlist:
            steps.append(Step(which, "unchanged", f"allowlist has {path}"))
        else:
            append_sequence_item(lines, ("permissions", "filesystem", "allowlist"), _scalar(path))
            expected.setdefault("permissions", {}).setdefault("filesystem", {})
            target = expected["permissions"]["filesystem"]
            target["allowlist"] = [*(target.get("allowlist") or []), path]
            steps.append(Step(which, "add", f"allowlist {path}"))
            changed = True

        current = repo_paths.get(key)
        if current == path:
            steps.append(Step(which, "unchanged", f"target_repo_paths has {key} -> {path}"))
        elif current is not None:
            raise Refused(
                which,
                f"planning.target_repo_paths already has {key} -> {current}, not {path}; "
                "fix that line by hand or pick another name",
            )
        else:
            set_mapping_entry(lines, ("planning", "target_repo_paths"), _scalar(key), _scalar(path))
            expected.setdefault("planning", {})
            expected["planning"]["target_repo_paths"] = {
                **(expected["planning"].get("target_repo_paths") or {}),
                key: path,
            }
            steps.append(Step(which, "add", f"target_repo_paths {key} -> {path}"))
            changed = True

        current_entry = sandboxes.get(key)
        if current_entry == entry:
            steps.append(Step(which, "unchanged", f"sandboxes has {key} in {entry['name']}"))
        elif current_entry is not None:
            raise Refused(
                which,
                f"planning.sandboxes already has an entry for {key} that differs from "
                f"{REFERENCE_PROJECT}'s; fix it by hand",
            )
        else:
            _ensure_block(lines, ("planning", "sandboxes", _scalar(key)))
            for name in wanted_keys:
                set_mapping_entry(
                    lines, ("planning", "sandboxes", _scalar(key)), name, _scalar(entry[name])
                )
            expected["planning"]["sandboxes"][key] = dict(entry)
            steps.append(Step(which, "add", f"sandboxes {key} in sandbox {entry['name']}"))
            changed = True
    except YamlEditRefused as exc:
        raise Refused(which, str(exc)) from None

    if not changed:
        return YamlEdit(which, text, None, steps, entry)
    staged = "\n".join(lines)
    try:
        reread = yaml.safe_load(staged)
    except yaml.YAMLError as exc:
        raise Refused(which, f"the edited forge.yaml would not parse ({exc.__class__.__name__})") from None
    if reread != expected:
        raise Refused(
            which,
            "the edited forge.yaml would change more than the three entries; "
            "nothing was written — edit it by hand",
        )
    if not _keeps_every_line(text.split("\n"), lines):
        raise Refused(which, "the edit would lose a line of the file; nothing was written")
    why_not = _loads_as_forge_settings(staged)
    if why_not is not None:
        raise Refused(which, f"the edited forge.yaml would not load ({why_not}); nothing was written")
    return YamlEdit(which, text, staged, steps, entry)


@dataclass
class JsonEdit:
    """The publisher route: the new text (``None`` = nothing) and why."""

    original: str
    staged: str | None
    steps: list[Step] = field(default_factory=list)


def plan_publisher_edit(text: str, *, key: str, source: str, remote: str) -> JsonEdit:
    """Add the one route ``projects[key] = {source, remote}``, and nothing else."""
    from forge.publisher.settings import load_settings

    try:
        data = json.loads(text)
    except ValueError:
        raise Refused("publisher", "the publisher's settings file is not readable JSON") from None
    if not isinstance(data, dict):
        raise Refused("publisher", "the publisher's settings file must hold one object")
    projects = data.get("projects")
    if projects is not None and not isinstance(projects, dict):
        raise Refused("publisher", "'projects' in the publisher's settings is not an object")
    route = {"source": source, "remote": remote}
    current = (projects or {}).get(key)
    if current == route:
        return JsonEdit(text, None, [Step("publisher", "unchanged", f"route {key} is there")])
    if current is not None:
        raise Refused(
            "publisher",
            f"the publisher already has a different route for {key}; fix it by hand",
        )
    data["projects"] = {**(projects or {}), key: route}
    staged = json.dumps(data, indent=2) + "\n"
    with tempfile.TemporaryDirectory(prefix="forge-register-") as folder:
        probe = Path(folder) / "settings.json"
        probe.write_text(staged, encoding="utf-8")
        try:
            load_settings(probe)
        except Exception as exc:  # noqa: BLE001 — the loader's own sentence
            raise Refused("publisher", f"the edited publisher settings would not load: {exc}") from None
    return JsonEdit(text, staged, [Step("publisher", "add", f"route {key}: {source} -> {remote}")])


def _backup_name(live: str, leaf: str, *, today: date | None = None) -> str:
    stamp = (today or date.today()).strftime("%Y%m%d")
    return f"{live}.bak-{stamp}-pre-register-{leaf}"


def _pending_name(live: str, leaf: str) -> str:
    return f"{live}.{leaf}-pending"


def _existing_backup(store: Any, live: str, leaf: str, original: str, folder: str | None) -> str | None:
    """The newest backup of ``live`` for this project that holds ``original``."""
    base = PurePosixPath(live).name
    pattern = re.compile(rf"^{re.escape(base)}\.bak-(\d{{8}})-pre-register-{re.escape(leaf)}$")
    listed = store.names() if folder is None else store.names(folder)
    for name in sorted((n for n in listed if pattern.match(n)), reverse=True):
        full = name if folder is None else f"{folder.rstrip('/')}/{name}"
        if store.read(full) == original:
            return full
    return None


@dataclass
class Staged:
    """Where a staged copy and its backup are, once written or found."""

    live: str
    pending: str
    backup: str | None


def stage(
    store: Any,
    *,
    which: str,
    live: str,
    folder: str | None,
    leaf: str,
    original: str,
    staged: str,
    dry_run: bool,
    like: str | None = None,
) -> tuple[Step, Staged | None]:
    """Write ``staged`` beside ``live`` as the pending copy, with a dated backup.

    Writes nothing when the same pending copy is already there (the backup it
    was made with is found and named), and nothing at all in a dry run.
    """
    pending = _pending_name(live, leaf)
    if store.read(pending) == staged:
        backup = _existing_backup(store, live, leaf, original, folder)
        detail = f"already staged as {store.where(pending)}"
        if backup is not None:
            detail += f" (backup {PurePosixPath(backup).name})"
        return Step(which, "unchanged", detail), Staged(live, pending, backup)
    if dry_run:
        return Step(which, "would stage", store.where(pending)), None
    backup = _backup_name(live, leaf)
    if isinstance(store, HostStore):
        store.write(backup, original, like=like)
        store.write(pending, staged, like=like)
    else:
        store.write(backup, original)
        store.write(pending, staged)
    return (
        Step(which, "staged", f"{store.where(pending)} (backup {PurePosixPath(backup).name})"),
        Staged(live, pending, backup),
    )


# ---------------------------------------------------------------------------
# Step 3 — the factory's clone in the shared sandbox
# ---------------------------------------------------------------------------


def _same_repository(a: str, b: str) -> bool:
    """Two remote addresses for one repository (``.git`` and ``/`` aside)."""

    def norm(value: str) -> str:
        value = value.strip().rstrip("/")
        return value[:-4] if value.endswith(".git") else value

    return norm(a) == norm(b)


def check_sandbox_clone(sandbox: SandboxStore, *, clone: str, leaf: str, url: str) -> tuple[Step, bool]:
    """Every read step 3 needs, before anything is written.

    Returns the report line and whether a clone must be made.
    """
    folder = f"{PROJECTS_FOLDER}/{leaf}"
    ignored = sandbox.exec("git", "-C", clone, "check-ignore", "-q", folder)
    if ignored.returncode == 1:
        raise Refused(
            "clone",
            f"{PROJECTS_FOLDER} is not git-ignored in the api_test clone ({clone}), so a "
            "project cloned there would show up as api_test's own change; add it to "
            "api_test's .gitignore first",
        )
    if ignored.returncode != 0:
        raise Refused("clone", f"could not ask git whether {PROJECTS_FOLDER} is ignored in {clone} ({_said(ignored)})")

    readable = sandbox.exec(
        "git", "ls-remote", url, "HEAD", env=("GIT_TERMINAL_PROMPT=0",), timeout=CLONE_TIMEOUT
    )
    if readable.returncode != 0 or not readable.stdout.strip():
        raise Refused(
            "clone",
            f"the sandbox cannot read {url}. The sandbox holds no GitHub credential, so it "
            "can only read public repositories; a private one needs a read-only credential "
            "handed to the sandbox, which is an owner decision. Nothing was written",
        )

    target = f"{clone.rstrip('/')}/{folder}"
    exists = sandbox.exec("test", "-e", target)
    if exists.returncode == 1:
        return Step("clone", "add", f"git clone {url} {target}"), True
    if exists.returncode != 0:
        raise Refused("clone", f"could not look at {target} in the sandbox ({_said(exists)})")
    origin = sandbox.exec("git", "-C", target, "remote", "get-url", "origin")
    if origin.returncode != 0:
        raise Refused("clone", f"{target} is already there and is not a clone with an origin; move it away first")
    if not _same_repository(origin.stdout, url):
        raise Refused(
            "clone",
            f"{target} is already there, cloned from {origin.stdout.strip()}, not {url}; "
            "it is left alone — move it away or pick another name",
        )
    return Step("clone", "unchanged", f"{target} is already a clone of {url}"), False


def make_sandbox_clone(sandbox: SandboxStore, *, clone: str, leaf: str, url: str) -> Step:
    """``git clone <url> <projects folder>/<leaf>`` as the sandbox user."""
    target = f"{clone.rstrip('/')}/{PROJECTS_FOLDER}/{leaf}"
    made = sandbox.exec(
        "git", "clone", "--quiet", url, target, env=("GIT_TERMINAL_PROMPT=0",), timeout=CLONE_TIMEOUT
    )
    if made.returncode != 0:
        raise Refused("clone", f"git clone {url} in the sandbox failed ({_said(made)})")
    origin = sandbox.exec("git", "-C", target, "remote", "get-url", "origin")
    if origin.returncode != 0 or not _same_repository(origin.stdout, url):
        raise Refused("clone", f"{target} was cloned but does not name {url} as its origin")
    return Step("clone", "done", f"{target} cloned from {url}")


# ---------------------------------------------------------------------------
# Activation step (b) — is the factory drained?
# ---------------------------------------------------------------------------


#: What runs INSIDE the coordinator (``docker exec <coordinator> python -c``):
#: the coordinator holds the ledger and the bus connection, so it is asked.
#: Plain Python on purpose — the coordinator's image may be older than this
#: checkout, so nothing of this module may be imported there. It reads only:
#: the ledger opened read-only — active builds, unfinished planning runs,
#: queued work, and the merge/publish/deploy work rollout-quiesce also counts
#: (an unfinished publication record, a held deployment target) — and
#: ``consumer_info`` on the two durable consumers (the same read the
#: coordinator's own consumer-health check makes).
#: The bus address carries a credential, so it is scrubbed out of anything said.
DRAINED_READ_SCRIPT: str = r'''
import asyncio, json, os, sqlite3, sys
from urllib.parse import urlsplit
spec = json.loads(sys.argv[1])
url = os.environ.get("FORGE_NATS_URL", "")
secrets = [s for s in (url, urlsplit(url).password if url else None) if s]
def said(exc):
    text = f"{type(exc).__name__}: {exc}".splitlines()[0][:300]
    for s in secrets:
        text = text.replace(s, "***")
    return text
out = {}
db = os.environ.get("FORGE_DB_PATH") or spec["db"]
try:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    try:
        def counts(sql, done):
            marks = ",".join("?" for _ in done)
            return {str(k): int(v) for k, v in con.execute(sql % marks, list(done)).fetchall()}
        def one(sql, *params):
            return int(con.execute(sql, params).fetchone()[0])
        builds = {
            str(k): int(v)
            for k, v in con.execute(
                "SELECT status, COUNT(*) FROM builds WHERE status IN (%s) GROUP BY status"
                % ",".join("?" for _ in spec["build_active"]),
                list(spec["build_active"]),
            ).fetchall()
        }
        interrupted = one("SELECT COUNT(*) FROM builds WHERE status = 'INTERRUPTED'")
        plans = counts("SELECT state, COUNT(*) FROM planning_runs WHERE state NOT IN (%s) GROUP BY state", spec["planning_terminal"])
        free = one("SELECT COUNT(*) FROM work_queue WHERE status = 'QUEUED' AND after_id IS NULL")
        held = one("SELECT COUNT(*) FROM work_queue WHERE status = 'QUEUED' AND after_id IS NOT NULL")
        publications = one(
            "SELECT COUNT(*) FROM publication_records WHERE result IS NULL OR result != ?",
            spec["publication_finished"],
        )
        deploy_holds = one(
            "SELECT COUNT(*) FROM deployment_targets WHERE holder_build IS NOT NULL AND holder_build != ''"
        )
    finally:
        con.close()
    out["ledger"] = {
        "builds": builds, "interrupted": interrupted, "planning_runs": plans,
        "queue_waiting": free, "queue_held": held,
        "publications_unfinished": publications, "deploy_targets_held": deploy_holds,
    }
except Exception as exc:
    out["ledger"] = {"error": said(exc)}
# nats-py retries a first connection for ever, printing each failure, unless
# told to stop after one attempt and given a quiet error callback.
async def quiet(exc):
    pass
async def read_consumers():
    import nats
    nc = await nats.connect(
        url, connect_timeout=5, allow_reconnect=False, max_reconnect_attempts=1,
        reconnect_time_wait=0.5, error_cb=quiet,
    )
    try:
        js = nc.jetstream()
        got = {}
        for name in spec["consumers"]:
            try:
                info = await js.consumer_info(spec["stream"], name)
                got[name] = {"pending": int(info.num_pending), "ack_pending": int(info.num_ack_pending)}
            except Exception as exc:
                got[name] = {"error": said(exc)}
        return got
    finally:
        await nc.close()
try:
    if not url:
        raise RuntimeError("the coordinator has no FORGE_NATS_URL")
    out["consumers"] = asyncio.run(asyncio.wait_for(read_consumers(), 30))
except BaseException as exc:
    out["consumers"] = {name: {"error": said(exc)} for name in spec["consumers"]}
print(json.dumps(out))
'''


def drained_read_spec() -> dict[str, Any]:
    """The states, stream and consumer names the read uses — Forge's own."""
    from forge.adapters.nats.planning_consumer import PLANNING_DURABLE_NAME
    from forge.cli._serve_config import DEFAULT_DURABLE_NAME
    from forge.pipeline.publication_record import RESULT_MERGED_AND_RUNNING
    from forge.planning.work_queue_loop import BUILD_ACTIVE_STATES, PLANNING_TERMINAL_STATES

    return {
        "db": COORDINATOR_LEDGER,
        "build_active": sorted(BUILD_ACTIVE_STATES),
        "publication_finished": RESULT_MERGED_AND_RUNNING,
        "planning_terminal": sorted(PLANNING_TERMINAL_STATES),
        "stream": BUS_STREAM,
        "consumers": [PLANNING_DURABLE_NAME, DEFAULT_DURABLE_NAME],
    }


def read_drained_facts(run: Runner, container: str) -> dict[str, Any]:
    """Ask the coordinator. Anything that cannot be read is said, not assumed."""
    spec = drained_read_spec()
    result = _call(
        run,
        ["docker", "exec", container, "python", "-c", DRAINED_READ_SCRIPT, json.dumps(spec)],
        timeout=90,
    )
    if result.returncode != 0:
        return {"error": f"the coordinator ({container}) could not be asked ({_said(result)})"}
    lines = [line for line in (result.stdout or "").strip().splitlines() if line.strip()]
    try:
        facts = json.loads(lines[-1]) if lines else None
    except ValueError:
        facts = None
    if not isinstance(facts, dict):
        return {"error": f"the coordinator ({container}) answered something that is not the read's report"}
    return facts


def _by_state(counts: Mapping[str, Any]) -> str:
    return ", ".join(f"{state} {count}" for state, count in sorted(counts.items()))


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def judge_drained(facts: Mapping[str, Any], consumers: Sequence[str]) -> list[str]:
    """Why the factory is NOT drained; an empty list means drained.

    Drained means all of: no build in an active state (the coordinator's own
    list, ``BUILD_ACTIVE_STATES``); no planning run unfinished; nothing queued
    in the work queue; no publication record unfinished and no deployment
    target held (the merge, publish and deploy work rollout-quiesce counts);
    and each durable consumer feeding the coordinator reads zero pending and
    zero awaiting acknowledgement. Anything that could not be read is a reason
    too. An INTERRUPTED build is not one: a restart leaves it as it is, and one
    that can be relaunched holds an unacknowledged build request, which the
    consumer check already counts (see :func:`drained_notes`).
    """
    if facts.get("error"):
        return [str(facts["error"])]
    reasons: list[str] = []
    ledger = facts.get("ledger")
    if not isinstance(ledger, dict) or ledger.get("error"):
        why = ledger.get("error") if isinstance(ledger, dict) else "no answer"
        reasons.append(f"the ledger could not be read ({why})")
    else:
        for label, key, finished in (
            ("build", "builds", "running"),
            ("planning run", "planning_runs", "finished"),
        ):
            counts = ledger.get(key)
            if not isinstance(counts, dict) or any(_count(v) is None for v in counts.values()):
                reasons.append(f"the ledger's {label}s could not be counted")
                continue
            total = sum(counts.values())
            if total and finished == "running":
                reasons.append(f"{total} {_plural(total, 'build is', 'builds are')} active ({_by_state(counts)})")
            elif total:
                reasons.append(
                    f"{total} {_plural(total, 'planning run is', 'planning runs are')} not finished ({_by_state(counts)})"
                )
        for key, say in (
            ("queue_waiting", lambda n: f"{n} {_plural(n, 'item waits', 'items wait')} in the work queue, "
             "which the coordinator's automatic queue would admit within seconds"),
            ("queue_held", lambda n: f"{n} {_plural(n, 'item waits', 'items wait')} in the work queue behind "
             "another item; wait for the item it waits on to finish, or withdraw it"),
            ("publications_unfinished", lambda n: f"{n} {_plural(n, 'merge is', 'merges are')} not finished "
             "(a publication record short of 'merged into the remote and running')"),
            ("deploy_targets_held", lambda n: f"{n} deployment {_plural(n, 'target is', 'targets are')} held "
             "by a build that is deploying"),
        ):
            n = _count(ledger.get(key))
            if n is None:
                reasons.append(f"the ledger's {key.replace('_', ' ')} could not be counted")
            elif n:
                reasons.append(say(n))
    read = facts.get("consumers")
    read = read if isinstance(read, dict) else {}
    for name in consumers:
        row = read.get(name)
        if not isinstance(row, dict) or row.get("error"):
            why = row.get("error") if isinstance(row, dict) else "no answer"
            reasons.append(f"the bus consumer {name} could not be read ({why})")
            continue
        pending, unacked = _count(row.get("pending")), _count(row.get("ack_pending"))
        if pending is None or unacked is None:
            reasons.append(f"the bus consumer {name} gave no counts")
        elif pending or unacked:
            reasons.append(
                f"the bus consumer {name} has {pending} pending and {unacked} awaiting "
                "acknowledgement (a request on its way in)"
            )
    return reasons


def drained_notes(facts: Mapping[str, Any]) -> list[str]:
    """What the read found that does not stop activation, said for information."""
    ledger = facts.get("ledger")
    n = _count(ledger.get("interrupted")) if isinstance(ledger, dict) else None
    if not n:
        return []
    return [
        f"for information: {n} {_plural(n, 'build is', 'builds are')} INTERRUPTED. A restart leaves "
        "them as they are; one that can be relaunched holds an unacknowledged build request, "
        "which the consumer check above already counts"
    ]


# ---------------------------------------------------------------------------
# Step 7 — what makes it live, printed, never run
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Estate:
    """The names the printed sequence uses, concrete wherever they are known."""

    env_file: str
    #: The estate's secrets.env, loaded into compose's environment and never printed.
    loaded_env_file: str
    estate_check: str
    sandbox: str
    volume: str
    coordinator: str


def activation_sequence(
    *,
    key: str,
    estate: Estate,
    coordinator: Staged | None,
    sandbox_file: Staged | None,
    publisher: Staged | None,
    helper_urls: tuple[str, str] | None,
    dry_run: bool,
) -> list[str]:
    """The one-page procedure's own order, for this registration (step 7 a–h)."""
    if coordinator is None and sandbox_file is None and publisher is None:
        if dry_run:
            return ["Activation: nothing would be staged, so there would be nothing to activate."]
        return [f"Activation: nothing is staged — {key} is already in the live settings."]
    intake = " ".join(INTAKE_SERVICES)

    def q(value: str) -> str:
        # A name still to be filled in ("$ESTATE_ENV") stays expandable.
        return f'"{value}"' if value.startswith("$") else shlex.quote(value)

    dc = (
        f'dc() {{ ( set -a; . {q(estate.loaded_env_file)}; set +a; docker compose -p '
        f'{COMPOSE_PROJECT} --env-file {q(estate.env_file)} "$@" ); }}'
    )
    lines = [
        ("What would make it live" if dry_run else "What makes it live")
        + " — the one-page stop/start procedure, run by the delivery owner once the factory's owner agrees."
        " This command restarted nothing.",
        "  The compose form, with the existing secrets loaded and nothing printed:",
        f"    {dc}",
    ]
    if any(v.startswith("$") for v in (estate.env_file, estate.loaded_env_file, estate.estate_check)):
        lines.append(
            "  Set these first (or pass --estate-env-file to have them filled in): ESTATE_ENV = the"
            " estate env file the factory runs on now; RUN = the folder holding its secrets.env;"
            " FORGE = the Forge checkout its compose files come from."
        )
    lines += [
        "(a) Close intake first, so no sentence, queue command or hand-over can arrive"
        " (sessions that publish on the bus directly are asked to hold):",
        f"    dc stop {intake}",
        "(b) Confirm the factory is drained (reads only):",
        f"    forge register-repo --check-drained --coordinator-container {q(estate.coordinator)}",
        "    Go on only if it exits 0 (rely on the exit status, not the words: \"NOT DRAINED\""
        " contains \"DRAINED\"). Exit 0 means no build active, no planning run, merge or deploy"
        " unfinished, nothing queued, and zero pending and zero unacknowledged on both bus"
        " consumers. Any other exit — including anything it could not read — stops here: reopen"
        " intake and activate later:",
        f"    dc up -d {intake}",
        "(c) Stop the coordinator and the rest, intake still closed:",
        "    dc stop",
        f"    Check: docker ps --filter label=com.docker.compose.project={COMPOSE_PROJECT} -q"
        " prints nothing, and",
        f"    sbx exec {q(estate.sandbox)} docker ps --format '{{{{.Names}}}}' lists neither"
        f" {estate.sandbox}-helper nor {estate.sandbox}-runner.",
        "(d) Put the staged settings in place, as ONE command: it first checks that every live file"
        " is still the one its staged copy was made from, and only then copies any. If a check"
        " fails nothing is copied: start again as in (g) without swapping and run register-repo"
        " again. If a copy fails part-way, put the files already copied back from their backups"
        " before starting:",
    ]
    checks: list[str] = []
    copies: list[str] = []
    if coordinator is not None:
        volume = q(estate.volume)
        backup = coordinator.backup or "<its backup>"
        checks.append(
            f"docker run --rm -v {volume}:/s:ro {VOLUME_HELPER_IMAGE} cmp /s/{coordinator.live}"
            f" {q('/s/' + backup)}"
        )
        copies.append(
            f"docker run --rm --user {SANDBOX_USER}:{SANDBOX_USER} -v {volume}:/s"
            f" {VOLUME_HELPER_IMAGE} cp /s/{coordinator.pending} /s/{coordinator.live}"
        )
    if sandbox_file is not None:
        backup = sandbox_file.backup or "<its backup>"
        exec_ = f"sbx exec -u {SANDBOX_USER} {q(estate.sandbox)}"
        checks.append(f"{exec_} cmp {q(sandbox_file.live)} {q(backup)}")
        copies.append(f"{exec_} cp {q(sandbox_file.pending)} {q(sandbox_file.live)}")
    if publisher is not None:
        backup = publisher.backup or "<its backup>"
        checks.append(f"cmp {q(publisher.live)} {q(backup)}")
        copies.append(f"cp {q(publisher.pending)} {q(publisher.live)}")
    chain = checks + copies
    lines.append(f"    {chain[0]}" + (" \\" if len(chain) > 1 else ""))
    for index, command in enumerate(chain[1:], start=1):
        lines.append(f"      && {command}" + (" \\" if index < len(chain) - 1 else ""))
    lines += [
        "(e) Restart the sandbox supervisor and confirm the helper and runner are ready:",
        f"    dc up -d {SANDBOX_SUPERVISOR_SERVICE}",
        f"    sbx exec {q(estate.sandbox)} docker ps --format '{{{{.Names}}}} {{{{.Status}}}}'"
        f"  (both {estate.sandbox}-helper and {estate.sandbox}-runner Up)",
    ]
    if helper_urls is not None:
        sidecar, runner = helper_urls
        lines.append(
            f"    curl -sf {q(sidecar.rstrip('/') + '/healthz')} && curl -sf {q(runner.rstrip('/') + '/ok')}"
        )
    if publisher is not None:
        lines += [
            "(f) The publisher's routes changed: start it first and confirm it lists the new route:",
            f"    dc up -d --wait {PUBLISHER_SERVICE}",
            f"    docker logs {COMPOSE_PROJECT}-{PUBLISHER_SERVICE}-1 2>&1 | grep 'publisher: settings'"
            f" | grep -F {q(key)}",
        ]
    else:
        lines.append("(f) The publisher's routes did not change; it starts with the rest.")
    lines.append(
        "(g) Start the rest in the procedure's order — the publisher before the coordinator,"
        " the Slack-facing services last:"
    )
    if publisher is None:
        lines.append(f"    dc up -d --wait {PUBLISHER_SERVICE}")
    pattern = "|".join(INTAKE_SERVICES)
    lines += [
        f"    dc up -d $(dc config --services | grep -vxE '{pattern}')",
        f"    dc up -d {intake}",
        "(h) Check the services, and that the coordinator lists the new project's sandbox:",
        f"    DOCKER_HOST=unix:///var/run/docker.sock {q(estate.estate_check)} --env-file"
        f" {q(estate.env_file)} --project {COMPOSE_PROJECT} services",
        f"    docker logs {q(estate.coordinator)} 2>&1 | grep -F"
        f" {q(f'autobuild dispatch: {key} has a sandbox')}",
    ]
    return lines


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


@dataclass
class Options:
    """Every option, resolved: the estate's facts this run uses."""

    key: str
    org: str
    leaf: str
    url: str
    publish: bool
    dry_run: bool
    sandbox: str
    volume: str
    coordinator: str
    sandbox_settings: str
    sandbox_clone: str
    publisher_settings: str | None
    publish_source_base: str | None
    estate: Estate


def _parse_name(name: str) -> tuple[str, str]:
    if not _NAME_PATTERN.match(name) or ".." in name:
        raise Refused(
            "name",
            f"{name!r} is not a project name of the form org/name (letters, digits, "
            "dot, underscore and hyphen; the last part becomes a folder name)",
        )
    org, leaf = name.split("/")
    return org, leaf


def _check_url(url: str, leaf: str) -> str:
    url = url.strip()
    if not url.startswith("https://"):
        raise Refused("github", f"--github must be the repository's https:// address, not {url!r}")
    last = url.rstrip("/").rsplit("/", 1)[-1]
    last = last[:-4] if last.endswith(".git") else last
    if last != leaf:
        raise Refused("github", f"--github names the repository {last!r}, but the project name ends in {leaf!r}")
    return url


def resolve_options(
    *,
    name: str,
    github: str | None,
    publish: bool,
    dry_run: bool,
    estate_env_file: Path | None,
    sandbox_env_file: Path | None,
    sandbox_settings: str | None,
    sandbox_clone: str | None,
    publisher_settings: Path | None,
    gateway_address: str | None,
    git_export_port: int,
    sandbox: str,
    volume: str,
    coordinator: str,
) -> Options:
    """The command's options with every default worked out, or a refusal."""
    org, leaf = _parse_name(name)
    if not github:
        raise Refused("github", "--github <https address of the repository> is needed")
    url = _check_url(github, leaf)

    estate_values: dict[str, str] = {}
    if estate_env_file is not None:
        try:
            estate_values = read_env_file(estate_env_file)
        except OSError as exc:
            raise Refused("estate", f"could not read the estate env file {estate_env_file} ({exc.strerror})") from None

    if sandbox_env_file is None and estate_values.get(ESTATE_ENV_SANDBOX_FILE) and estate_env_file:
        sandbox_env_file = _resolve_beside(estate_values[ESTATE_ENV_SANDBOX_FILE], estate_env_file)
    if sandbox_settings is None and sandbox_env_file is not None:
        try:
            sandbox_values = read_env_file(sandbox_env_file)
        except OSError as exc:
            raise Refused("sandbox", f"could not read the sandbox env file {sandbox_env_file} ({exc.strerror})") from None
        sandbox_settings = sandbox_values.get(SANDBOX_ENV_CONFIG_PATH) or None
    if not sandbox_settings:
        raise Refused(
            "sandbox",
            "which file is the sandbox's own forge.yaml? Pass --sandbox-settings, or "
            f"--sandbox-env-file (its {SANDBOX_ENV_CONFIG_PATH} names it), or --estate-env-file "
            f"(its {ESTATE_ENV_SANDBOX_FILE} names that)",
        )
    if not sandbox_settings.startswith("/"):
        raise Refused("sandbox", f"the sandbox's settings path {sandbox_settings!r} is not a full path")
    if sandbox_clone is None:
        suffix = "/" + SANDBOX_SETTINGS_IN_CLONE
        if not sandbox_settings.endswith(suffix):
            raise Refused(
                "sandbox",
                f"the sandbox's settings file is not at <api_test clone>{suffix}, so the clone "
                "cannot be worked out from it; pass --sandbox-clone",
            )
        sandbox_clone = sandbox_settings[: -len(suffix)]
    if not sandbox_clone.startswith("/"):
        raise Refused("sandbox", f"the api_test clone path {sandbox_clone!r} is not a full path")

    if publish and publisher_settings is None and estate_values.get(ESTATE_ENV_PUBLISHER_SETTINGS) and estate_env_file:
        publisher_settings = _resolve_beside(estate_values[ESTATE_ENV_PUBLISHER_SETTINGS], estate_env_file)
    if publish and publisher_settings is None:
        raise Refused(
            "publisher",
            "--publish needs the publisher's settings file: pass --publisher-settings, or "
            f"--estate-env-file (its {ESTATE_ENV_PUBLISHER_SETTINGS} names it)",
        )

    publish_source_base: str | None = None
    if publish:
        gateway = (gateway_address or estate_values.get(ESTATE_ENV_GATEWAY, "")).strip()
        if not gateway:
            raise Refused(
                "publisher",
                "--publish needs the factory gateway address the publisher fetches from: pass "
                f"--gateway-address, or --estate-env-file (its {ESTATE_ENV_GATEWAY} names it)",
            )
        if not re.fullmatch(r"[A-Za-z0-9.:\[\]-]+", gateway) or "${" in gateway:
            raise Refused("publisher", f"the gateway address {gateway!r} is not a plain host name or address")
        clone_folder = PurePosixPath(sandbox_clone.rstrip("/")).name
        publish_source_base = f"git://{gateway}:{git_export_port}/{clone_folder}/{PROJECTS_FOLDER}"

    env_text = str(estate_env_file) if estate_env_file is not None else "$ESTATE_ENV"
    # The estate's secrets file sits beside its env file (the one-page
    # procedure's $RUN/secrets.env); it is loaded, never printed.
    loaded_env_text = (
        str(estate_env_file.parent / "secrets.env") if estate_env_file is not None else "$RUN/secrets.env"
    )
    estate_check = "$FORGE/deploy/estate/estate-check"
    compose = estate_values.get(ESTATE_ENV_COMPOSE_FILE, "")
    first = next((part for part in re.split(r"[:,]", compose) if part.strip()), "")
    if first and estate_env_file is not None:
        estate_check = str(_resolve_beside(first.strip(), estate_env_file).parent / "estate-check")

    return Options(
        key=f"{org}/{leaf}",
        org=org,
        leaf=leaf,
        url=url,
        publish=publish,
        dry_run=dry_run,
        sandbox=sandbox,
        volume=volume,
        coordinator=coordinator,
        sandbox_settings=sandbox_settings,
        sandbox_clone=sandbox_clone.rstrip("/"),
        publisher_settings=str(publisher_settings) if publisher_settings is not None else None,
        publish_source_base=publish_source_base,
        estate=Estate(
            env_file=env_text,
            loaded_env_file=loaded_env_text,
            estate_check=estate_check,
            sandbox=sandbox,
            volume=volume,
            coordinator=coordinator,
        ),
    )


def register(options: Options, run: Runner, steps: list[Step]) -> list[str]:
    """Steps 1–7: report lines go into ``steps``; returns the printed sequence.

    Every read comes before every write, so a refusal anywhere leaves
    everything — the project, the live settings and the sandbox — as it was.
    """
    with tempfile.TemporaryDirectory(prefix="forge-register-") as folder:
        steps += check_project(run, key=options.key, url=options.url, folder=Path(folder))

    volume = VolumeStore(options.volume, run)
    sandbox = SandboxStore(options.sandbox, run)
    host = HostStore()

    coordinator_text = volume.read(SETTINGS_FILE_NAME)
    if coordinator_text is None:
        raise Refused("coordinator", f"could not read {SETTINGS_FILE_NAME} from the volume {options.volume}")
    sandbox_text = sandbox.read(options.sandbox_settings)
    if sandbox_text is None:
        raise Refused("sandbox", f"could not read {options.sandbox_settings} in the sandbox {options.sandbox}")

    coordinator_path = f"{COORDINATOR_PROJECTS_ROOT}/{options.leaf}"
    clone_path = f"{options.sandbox_clone}/{PROJECTS_FOLDER}/{options.leaf}"
    coordinator_edit = plan_settings_edit(
        coordinator_text, which="coordinator", key=options.key, path=coordinator_path
    )
    sandbox_edit = plan_settings_edit(sandbox_text, which="sandbox", key=options.key, path=clone_path)

    publisher_edit: JsonEdit | None = None
    if options.publish:
        assert options.publisher_settings is not None and options.publish_source_base is not None
        publisher_text = host.read(options.publisher_settings)
        if publisher_text is None:
            raise Refused("publisher", f"could not read the publisher's settings file {options.publisher_settings}")
        publisher_edit = plan_publisher_edit(
            publisher_text,
            key=options.key,
            source=f"{options.publish_source_base.rstrip('/')}/{options.leaf}",
            remote=f"git@github.com:{options.org}/{options.leaf}.git",
        )

    clone_step, clone_needed = check_sandbox_clone(
        sandbox, clone=options.sandbox_clone, leaf=options.leaf, url=options.url
    )

    # Every read is done and nothing refused: now, and only now, write.
    if clone_needed and options.dry_run:
        steps.append(Step("clone", "would add", clone_step.detail))
    elif clone_needed:
        steps.append(make_sandbox_clone(sandbox, clone=options.sandbox_clone, leaf=options.leaf, url=options.url))
    else:
        steps.append(clone_step)

    staged: dict[str, Staged | None] = {"coordinator": None, "sandbox": None, "publisher": None}
    for edit, store, live, folder_name in (
        (coordinator_edit, volume, SETTINGS_FILE_NAME, None),
        (sandbox_edit, sandbox, options.sandbox_settings, str(PurePosixPath(options.sandbox_settings).parent)),
    ):
        steps += edit.steps
        if edit.staged is None:
            steps.append(Step(edit.which, "unchanged", "nothing to change, nothing staged"))
            continue
        line, where = stage(
            store, which=edit.which, live=live, folder=folder_name, leaf=options.leaf,
            original=edit.original, staged=edit.staged, dry_run=options.dry_run,
        )
        steps.append(line)
        staged[edit.which] = where if where is not None else Staged(
            live, _pending_name(live, options.leaf), _backup_name(live, options.leaf)
        )
    if publisher_edit is not None:
        assert options.publisher_settings is not None
        steps += publisher_edit.steps
        if publisher_edit.staged is None:
            steps.append(Step("publisher", "unchanged", "nothing to change, nothing staged"))
        else:
            live = options.publisher_settings
            line, where = stage(
                host, which="publisher", live=live, folder=str(Path(live).parent),
                leaf=options.leaf, original=publisher_edit.original,
                staged=publisher_edit.staged, dry_run=options.dry_run, like=live,
            )
            steps.append(line)
            staged["publisher"] = where if where is not None else Staged(
                live, _pending_name(live, options.leaf), _backup_name(live, options.leaf)
            )

    steps.append(Step("coordinator-folder", "unchanged", COORDINATOR_FOLDER_NOTE))

    entry = sandbox_edit.reference_entry
    urls = (entry.get("sidecar_url", ""), entry.get("runner_url", ""))
    helper_urls = urls if all(u and "${" not in u for u in urls) else None
    tail = activation_sequence(
        key=options.key,
        estate=options.estate,
        coordinator=staged["coordinator"],
        sandbox_file=staged["sandbox"],
        publisher=staged["publisher"],
        helper_urls=helper_urls,
        dry_run=options.dry_run,
    )
    return tail


@click.command(name="register-repo")
@click.argument("name", required=False)
@click.option("--github", "github", default=None, help="The repository's https:// address on GitHub.")
@click.option("--publish", is_flag=True, default=False, help="Also add the publisher's route for it.")
@click.option("--dry-run", "dry_run", is_flag=True, default=False,
              help="Check the project and say what would change; write nothing anywhere.")
@click.option("--check-drained", "check_drained", is_flag=True, default=False,
              help="Activation step (b) only: say DRAINED, or why not. Reads only.")
@click.option("--estate-env-file", type=click.Path(dir_okay=False, path_type=Path),
              envvar="FORGE_ESTATE_ENV_FILE", default=None,
              help="The estate env file the factory runs on now; the printed commands use it.")
@click.option("--sandbox-env-file", type=click.Path(dir_okay=False, path_type=Path), default=None,
              help=f"The sandbox's bootstrap env file (default: the estate env's {ESTATE_ENV_SANDBOX_FILE}).")
@click.option("--sandbox-settings", default=None,
              help=f"The sandbox's own forge.yaml (default: the bootstrap env's {SANDBOX_ENV_CONFIG_PATH}).")
@click.option("--sandbox-clone", default=None,
              help="The api_test clone in the sandbox (default: worked out from --sandbox-settings).")
@click.option("--publisher-settings", type=click.Path(dir_okay=False, path_type=Path), default=None,
              help=f"The publisher's settings file (default: the estate env's {ESTATE_ENV_PUBLISHER_SETTINGS}).")
@click.option("--gateway-address", default=None,
              help=f"With --publish: the factory gateway address (default: the estate env's {ESTATE_ENV_GATEWAY}).")
@click.option("--git-export-port", type=int, default=DEFAULT_GIT_EXPORT_PORT, show_default=True,
              help="With --publish: the port the sandbox's git export is published on.")
@click.option("--sandbox", "sandbox", default=DEFAULT_SANDBOX, show_default=True, help="The shared sandbox.")
@click.option("--settings-volume", "volume", default=DEFAULT_SETTINGS_VOLUME, show_default=True,
              help="The coordinator's settings volume.")
@click.option("--coordinator-container", "coordinator", default=DEFAULT_COORDINATOR_CONTAINER,
              show_default=True, help="The running coordinator's container.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Print the report as JSON.")
def register_repo_cmd(
    name: str | None,
    github: str | None,
    publish: bool,
    dry_run: bool,
    check_drained: bool,
    estate_env_file: Path | None,
    sandbox_env_file: Path | None,
    sandbox_settings: str | None,
    sandbox_clone: str | None,
    publisher_settings: Path | None,
    gateway_address: str | None,
    git_export_port: int,
    sandbox: str,
    volume: str,
    coordinator: str,
    as_json: bool,
) -> None:
    """Register a project on GitHub with the running factory.

    Checks the project (authoring nothing in it), makes the factory's own clone
    of it in the shared sandbox, stages the coordinator's, the sandbox's and
    (with --publish) the publisher's settings beside the live ones, and prints
    the stop/start sequence that makes it live. It restarts nothing.
    """
    run = run_command

    if check_drained:
        consumers = drained_read_spec()["consumers"]
        facts = read_drained_facts(run, coordinator)
        reasons = judge_drained(facts, consumers)
        notes = [Step("drained", "note", note) for note in drained_notes(facts)]
        if not reasons:
            _emit(
                [Step("drained", "ok", "DRAINED: no build active, no planning run, merge or deploy "
                      "unfinished, nothing queued in the work queue, and "
                      f"{' and '.join(consumers)} each read zero pending and zero unacknowledged")] + notes,
                as_json=as_json,
            )
            return
        _emit(
            [Step("drained", "no", reason) for reason in reasons]
            + notes
            + [Step("drained", "no", "NOT DRAINED — reopen intake and activate later")],
            as_json=as_json,
        )
        raise click.exceptions.Exit(1)

    steps: list[Step] = []
    try:
        if not name:
            raise Refused("name", "name the project: forge register-repo <org>/<name> --github <https address>")
        options = resolve_options(
            name=name,
            github=github,
            publish=publish,
            dry_run=dry_run,
            estate_env_file=estate_env_file,
            sandbox_env_file=sandbox_env_file,
            sandbox_settings=sandbox_settings,
            sandbox_clone=sandbox_clone,
            publisher_settings=publisher_settings,
            gateway_address=gateway_address,
            git_export_port=git_export_port,
            sandbox=sandbox,
            volume=volume,
            coordinator=coordinator,
        )
        tail = register(options, run, steps)
    except Refused as refusal:
        steps.append(Step(refusal.step, "refused", refusal.detail))
        _emit(steps, as_json=as_json)
        raise click.exceptions.Exit(1) from None
    _emit(steps, as_json=as_json, tail=tail)
