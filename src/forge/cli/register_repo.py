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
     With ``--publish-credential-name NAME`` as well, that route names its
     own key: the bare name of a file in the publisher's route-keys folder.
     The key itself is never read, made or sent anywhere by this command;
     adding a deploy key to a repository is the owner's decision.

   Each YAML file gains exactly three entries — the allowlist path, the
   ``planning.target_repo_paths`` key and the ``planning.sandboxes`` entry
   (copying the sandbox name and addresses of the first repository already
   registered on the shared sandbox in the same file) — by
   surgical line insertion that keeps every comment, and must re-parse with
   :func:`forge.config.loader.load_config` or nothing is written.
3. **The factory's clone**, in the sandbox, as the sandbox user: refused when
   the projects folder is not git-ignored in the api_test clone, when the
   sandbox cannot read the repository (a private repository needs a read
   credential, which is an owner decision), or when a folder is already there
   with a different origin; reused when it is there with the same origin.
4. **Says what makes it live.** It prints the one-page procedure's own
   sequence — close intake, confirm drained, swap the staged files in,
   restart the sandbox supervisor, restart the publisher when its routes
   changed and then the coordinator (each after the drained check again),
   reopen intake, check — and restarts nothing itself. Every printed step
   acts on running containers by name and loads no secrets file.

``--slot <k>`` (design "One sandbox per project", 17 October 2026) gives the
project a sandbox of its own instead of the shared one, named after the
project (``--sandbox`` may say the same name, and no other). The sandbox is
made first, from the project's own clone on its machine, by the sandbox
recipe; its workspace clone (``--sandbox-clone``, the same path inside the
sandbox) IS the factory's clone of the project, and its own settings file
(its FORGE_CONFIG_PATH) is ``<that clone>/.guardkit/tmp/factory-runtime/forge.yaml``,
which this command writes when it is not there yet. So in this form nothing is
cloned: the command checks that the workspace clone is this project's and
keeps the runtime folder out of git, and the project's path in the sandbox's
settings is the workspace clone itself. The publisher's route fetches from the
sandbox's own git daemon, at its slot's port on the sandbox publish address. The
``planning.sandboxes`` entry it writes is not copied from another project's: it names
this sandbox, the helper and runner at slot *k*'s ports
(:mod:`forge.config.sandbox_slots`) on the sandbox publish address, and the
sandbox's own Git door port. The coordinator's copy of the entry is also
granted the project's declared memory name (``memory_projects: [<name>]``,
design section 2.4a, 18 October 2026): a build's approval card reads priors
only from a name its repository was granted, and the printed memory door line
names the same one. A project whose declared name the memory service would
refuse is refused here. A sandbox takes one project: a sandbox, or a
slot, that another project already has is refused. The shared sandbox
``api-test-deploy`` keeps the projects already in it and the old form, but it
has no slot and never a Git door.

``--via-git-door`` (17 October 2026) is for a private repository, and needs a
sandbox of its own: the sandbox reads it through its own port on the Git door
on the factory's own machine (its entry's ``git_door``), which adds a
read-only token, so the sandbox still holds no credential. The project's fetch
address is recorded as ``<git_door><org>/<name>.git`` and the sandbox's read
goes through it. The repository must already be on the door's allow-list,
keyed by that port; the command says so, and changes nothing about the door
itself.

``--sandbox-ssh <host>`` (Codex implementation review 1, R3) is for the
estate's two machines: the coordinator, its settings volume and the
publisher's file are on this machine, and the sandboxes on another (the
Spark). Run the command here, as before; every ``sbx`` command is then sent to
``<host>`` over ssh, each argument shell-quoted so it arrives as written, and
the printed activation lines say the same. Nothing else changes machine.

When the publisher's settings say ``private_only`` (17 October 2026), a
route is written only for a repository confirmed private
(:mod:`forge.publisher.private_remote`): its own route key reads it (the
check clone in step 1) and GitHub does not show it to anybody signed out.
That needs ``--publish-credential-name``.

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
import secrets
import shlex
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from typing import Any, Callable, Mapping, Sequence

import click
import yaml

from forge.config.sandbox_slots import LEGACY_SANDBOX, MAX_SLOT, SlotPorts, slot_ports
from forge.network_git import (
    NetworkRefused,
    git_door_address,
    proxy_environment,
    sandbox_git_command,
    sandbox_network_git,
    transport_of,
)
from forge.publisher.private_remote import GITHUB_HOST, anonymous_lookup, confirmed_private
from forge.project_git import FIXED_SETTINGS

__all__ = ["register_repo_cmd"]


# ---------------------------------------------------------------------------
# The estate's facts. Every one is a default for an option, so a different
# estate passes its own; none of them is a path on anybody's machine.
# ---------------------------------------------------------------------------

#: The one shared sandbox every project was built in (Rich, 5 October 2026).
#: Since one sandbox per project (17 October 2026) a project may be given a
#: sandbox of its own (``--slot <k>``, named after the project); this stays the default.
DEFAULT_SANDBOX: str = LEGACY_SANDBOX

#: The user the sandbox's helper and runner run as, and the owner of the clone.
SANDBOX_USER: str = "1000"

#: The coordinator's settings volume and the file in it, as the one-page
#: upgrade procedure names them.
DEFAULT_SETTINGS_VOLUME: str = "forge-estate_forge-settings"
SETTINGS_FILE_NAME: str = "forge.yaml"

#: The throwaway image the volume is read and written through, as the
#: procedure does (``docker run --rm -v <volume>:/s alpine ...``).
VOLUME_HELPER_IMAGE: str = "alpine"

#: How the printed activation names the estate's own start script: each
#: estate has its own (it hands compose the secrets, e.g. under ``sops
#: exec-env``), so it is named by its role, never by a file (S1 window, 18
#: October 2026; the plaintext secrets file it replaced was retired on 13
#: October).
ESTATE_START_SCRIPT: str = "<the estate's start script>"

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

#: The runtime folders a sandbox of its own's settings name beside its
#: settings file, as the shared sandbox's do (FORGE_AUTOBUILD_WORKTREE_BASE,
#: FORGE_DEPLOY_NOTES_DIR and TMPDIR in its env file). Its start-up script
#: uses them as they are and fails (mktemp) when one is missing, so this
#: command makes them with the settings file (S1 window, 18 October 2026).
SANDBOX_RUNTIME_FOLDERS: tuple[str, ...] = ("worktrees", "deploy-notes", "tmp")
PROJECTS_FOLDER: str = f"{RUNTIME_FOLDER}/projects"

#: Where the coordinator sees each project's own receipts folder (design
#: section 2.2a): the machine's receipts folders are mounted here, one folder
#: per sandbox, by the coordinator's project-receipts overlay
#: (deploy/compose/compose.project-receipts.yaml). A sandbox of its own's
#: entry names ``<this>/<sandbox name>``; the shared sandbox's never does.
COORDINATOR_PROJECT_RECEIPTS: str = "/var/lib/forge-project-receipts"

#: A new project in the shared sandbox copies the sandbox entry of a project
#: already registered there: the sandbox name and its two addresses are the
#: same for every project in it. Any such project serves (Codex design review
#: R9, 18 October 2026: it was api_test's alone, so unregistering api_test
#: would have stopped every later registration), so the reference is the first
#: entry, in file order, that names the shared sandbox: see
#: :func:`shared_sandbox_reference`.


def shared_sandbox_reference(sandboxes: Mapping[str, Any], key: str) -> tuple[str, Any] | None:
    """``(repository, entry)`` of the first registration on the shared sandbox,
    another repository's when there is one, else ``key``'s own; ``None`` when
    no registration is on the shared sandbox."""
    shared = [
        (name, entry) for name, entry in sandboxes.items()
        if isinstance(entry, dict) and entry.get("name") == LEGACY_SANDBOX
    ]
    others = [item for item in shared if item[0] != key]
    chosen = others or shared
    return chosen[0] if chosen else None

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
#: Where the coordinator reaches a sandbox's helper and runner (a sandbox of
#: its own's entry is written with this address and its slot's ports).
ESTATE_ENV_SANDBOX_ADDRESS: str = "SANDBOX_PUBLISH_ADDRESS"
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

#: The home a git READ is given where it needs none: a folder that does not
#: exist, so nothing can be read from it.
_NO_HOME: str = "/nonexistent-forge-register-home"

#: The estate settings that name the publisher's route keys, its pinned host
#: keys and its user (for the check clone of a private repository).
ESTATE_ENV_ROUTE_KEYS_DIR: str = "FORGE_PUBLISHER_ROUTE_KEYS_DIR"
ESTATE_ENV_KNOWN_HOSTS: str = "FORGE_PUBLISHER_KNOWN_HOSTS_FILE"
ESTATE_ENV_PUBLISHER_UID: str = "FORGE_PUBLISHER_UID"

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


def _entry_value(value: Any) -> str:
    """One sandbox entry value as YAML: a scalar, or a flow list of scalars."""
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_scalar(str(item)) for item in value) + "]"
    return _scalar(value)


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
    """Run one command, capturing its output. Never uses a shell.

    Bytes in and out, decoded without newline translation: a settings file
    read through here must come back byte for byte, or the activation's
    "is the live file still the one we staged from" check could never pass.
    """
    done = subprocess.run(  # noqa: S603 — fixed argv, no shell
        list(argv),
        input=None if input is None else input.encode("utf-8", "surrogateescape"),
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return subprocess.CompletedProcess(
        done.args,
        done.returncode,
        done.stdout.decode("utf-8", "surrogateescape"),
        done.stderr.decode("utf-8", "replace"),
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


#: Writes standard input (or, for the volume, ``/in``) to "$1" with nothing
#: but its owner able to read it, then gives it the mode of "$2", the live file.
_WRITE_LIKE: str = 'umask 077 && cat > "$1" && chmod "$(stat -c %a "$2")" "$1"'

#: Writes standard input to "$1", a file that must not be there yet, readable
#: by its owner only, making its folder first (a sandbox of its own's settings
#: file, 17 October 2026).
_CREATE_NEW: str = 'umask 077 && mkdir -p -- "${1%/*}" && set -C && cat > "$1"'

#: Makes each folder it is given ("$@"; kept if it is there), owner-only, and
#: then checks every one is a folder this user owns.
_MAKE_FOLDERS: str = (
    'umask 077 && mkdir -p -- "$@" && chmod 700 -- "$@" && '
    'for d; do test -d "$d" && test -O "$d" || exit 3; done'
)


@dataclass(frozen=True)
class VolumeStore:
    """Files in the coordinator's settings volume, through a throwaway container.

    Read: ``docker run --rm -v <volume>:/s:ro alpine cat /s/<file>``. Write: the
    text goes into a private temporary file and is copied in by ``docker run
    --rm --user 1000:1000 -v <volume>:/s -v <tmp>:/in:ro alpine`` (``cp``),
    keeping the owner as the one-page procedure does, and given the mode of
    the live file it stands beside.
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

    def write(self, name: str, text: str, *, like: str) -> None:
        with tempfile.TemporaryDirectory(prefix="forge-register-") as folder:
            local = Path(folder) / name
            local.write_bytes(text.encode("utf-8", "surrogateescape"))
            result = _call(
                self.run,
                ["docker", "run", "--rm", "--user", f"{SANDBOX_USER}:{SANDBOX_USER}",
                 "-v", f"{self.volume}:/s", "-v", f"{local}:/in:ro",
                 VOLUME_HELPER_IMAGE, "sh", "-c", _WRITE_LIKE.replace("cat >", "cp /in"),
                 "register-repo", f"/s/{name}", f"/s/{like}"],
            )
        if result.returncode != 0:
            raise Refused("coordinator", f"could not write {name} into {self.volume} ({_said(result)})")
        if self.read(name) != text:
            raise Refused("coordinator", f"{name} in {self.volume} did not read back as written")


@dataclass(frozen=True)
class SandboxStore:
    """Files and commands inside the shared sandbox, as its own user.

    Every call is ``sbx exec -u 1000 <sandbox> ...``; a write hands the text in
    on standard input (``-i``) to ``sh -c 'cat > "$1"'`` and gives the file
    the mode of the live file it stands beside.

    ``ssh_host`` (one sandbox per project, 17 October 2026; Codex
    implementation review 1, R3): the sandbox lives on another machine than
    the coordinator, as the estate's sandboxes live on the Spark. Every sbx
    command is then sent there as ``ssh -o BatchMode=yes <host> -- <the
    command, shell-quoted>``, so each argument arrives exactly as written (the
    remote shell reads the one quoted line back into the same words) and
    standard input still carries a write. Everything else this command does —
    the coordinator's settings volume, the publisher's file, the check clone —
    stays on this machine.
    """

    sandbox: str
    run: Runner
    ssh_host: str | None = None

    def _routed(self, command: Sequence[str]) -> list[str]:
        """``command``, sent to the sandbox's machine when it is another."""
        if not self.ssh_host:
            return list(command)
        return [*sandbox_ssh_prefix(self.ssh_host), shlex.join(list(command))]

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
        return _call(self.run, self._routed(command), input=input, timeout=timeout)

    @property
    def exec_prefix(self) -> list[str]:
        """``sbx exec -u <user> <sandbox>``, for a command line built whole."""
        return ["sbx", "exec", "-u", SANDBOX_USER, self.sandbox]

    def run_whole(
        self, command: Sequence[str], *, timeout: float = COMMAND_TIMEOUT
    ) -> "subprocess.CompletedProcess[str]":
        """Run a command line built whole (by :mod:`forge.network_git`)."""
        return _call(self.run, self._routed(command), timeout=timeout)

    def git_read(self, *args: str) -> "subprocess.CompletedProcess[str]":
        """A local git read inside the sandbox, isolated (14 October 2026):
        ``env -i``, no settings files, no transport, hooks and the file
        monitor off. No home is needed for a read."""
        return self.run_whole(
            sandbox_git_command(
                self.exec_prefix, list(args), transport="none",
                home=_NO_HOME, cwd="/", settings=FIXED_SETTINGS,
            )
        )

    def where(self, name: str) -> str:
        return f"{self.sandbox}:{name}"

    def read(self, name: str) -> str | None:
        result = self.exec("cat", name)
        return result.stdout if result.returncode == 0 else None

    def names(self, folder: str) -> list[str]:
        result = self.exec("ls", "-1", folder)
        return result.stdout.split() if result.returncode == 0 else []

    def create(self, name: str, text: str) -> None:
        """Write a new file ``name`` (refused if it is there), owner-only."""
        result = self.exec("sh", "-c", _CREATE_NEW, "register-repo", name, input=text)
        if result.returncode != 0:
            raise Refused("sandbox", f"could not create {name} in {self.sandbox} ({_said(result)})")
        if self.read(name) != text:
            raise Refused("sandbox", f"{name} in {self.sandbox} did not read back as written")

    def make_folders(self, folders: Sequence[str]) -> None:
        """Make each folder (kept if it is there), mode 0700, as the sandbox user."""
        result = self.exec("sh", "-c", _MAKE_FOLDERS, "register-repo", *folders)
        if result.returncode != 0:
            raise Refused(
                "sandbox",
                f"could not make the runtime folders {', '.join(folders)} in {self.sandbox}, "
                f"each owned by the sandbox user ({_said(result)})",
            )

    def write(self, name: str, text: str, *, like: str) -> None:
        result = self.exec("sh", "-c", _WRITE_LIKE, "register-repo", name, like, input=text)
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
            return Path(name).read_bytes().decode("utf-8", "surrogateescape")
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
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(text.encode("utf-8", "surrogateescape"))
        os.chmod(name, mode)


def sandbox_ssh_prefix(host: str) -> list[str]:
    """How a command reaches the sandbox's machine: plain ssh, no prompt."""
    return ["ssh", "-o", "BatchMode=yes", host, "--"]


#: What an ssh host for the sandbox's machine may be: a host name, an alias
#: from the ssh settings, or user@host. Never an option (no leading "-").
_SSH_HOST = re.compile(r"^(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9][A-Za-z0-9._:-]*$")


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


def _route_key_problem(
    name: str, keys_dir: str | None, known_hosts: str | None, owner: int | None
) -> tuple[Path | None, str | None]:
    """``(key file, None)`` when the publisher's route key may be used for the
    check clone, else ``(None, sentence)``. The same checks the publisher
    makes of its own route keys: an ordinary file, not a link, owned by the
    publisher's user, no access for group or others, not empty; read with
    ``O_NOFOLLOW``. The sentence names what is wrong, never the contents."""
    if not keys_dir:
        return None, (
            f"--publish-credential-name {name} names a route key, but no route-keys "
            f"folder is known: set {ESTATE_ENV_ROUTE_KEYS_DIR} in the estate env file "
            "(pass --estate-env-file). Nothing was run"
        )
    if not known_hosts:
        return None, (
            f"the route key {name} is for ssh, and ssh needs the pinned host keys: set "
            f"{ESTATE_ENV_KNOWN_HOSTS} in the estate env file. Nothing was run"
        )
    if not Path(known_hosts).is_file():
        return None, f"the pinned host keys file {known_hosts} is not there. Nothing was run"
    where = Path(keys_dir) / name
    said = f"the publisher's route key {where}"
    try:
        found = os.lstat(where)
    except OSError as exc:
        return None, f"{said} could not be looked at ({exc.strerror or type(exc).__name__}). Nothing was run"
    if stat.S_ISLNK(found.st_mode):
        return None, f"{said} is a symbolic link; a route's key has to be the file itself. Nothing was run"
    if not stat.S_ISREG(found.st_mode):
        return None, f"{said} is not a file. Nothing was run"
    expected = owner if owner is not None else os.geteuid()
    if found.st_uid != expected:
        return None, (
            f"{said} belongs to user {found.st_uid}, not to the publisher's user "
            f"{expected}. Nothing was run"
        )
    if found.st_mode & 0o077:
        return None, (
            f"{said} can be read or written by others (mode {stat.S_IMODE(found.st_mode):o}); "
            "it must be readable by the publisher's user alone. Nothing was run"
        )
    if found.st_size == 0:
        return None, f"{said} is empty. Nothing was run"
    try:
        descriptor = os.open(str(where), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        return None, f"{said} could not be opened ({exc.strerror or type(exc).__name__}). Nothing was run"
    os.close(descriptor)
    return where, None


def check_project(
    run: Runner,
    *,
    key: str,
    url: str,
    folder: Path,
    credential_name: str | None = None,
    route_keys_dir: str | None = None,
    known_hosts: str | None = None,
    publisher_uid: int | None = None,
) -> list[Step]:
    """Step 1's report lines (see :func:`check_project_and_memory`)."""
    steps, _memory = check_project_and_memory(
        run, key=key, url=url, folder=folder, credential_name=credential_name,
        route_keys_dir=route_keys_dir, known_hosts=known_hosts, publisher_uid=publisher_uid,
    )
    return steps


def check_project_and_memory(
    run: Runner,
    *,
    key: str,
    url: str,
    folder: Path,
    credential_name: str | None = None,
    route_keys_dir: str | None = None,
    known_hosts: str | None = None,
    publisher_uid: int | None = None,
) -> tuple[list[Step], str]:
    """Step 1: a shallow clone of the default branch, read, never written.

    Returns the report lines and the memory name the project declares
    (``memory: project:`` in its ``.guardkit/config.yaml``), which a sandbox
    of its own is granted (design section 2.4a).

    THE CLONE IS ISOLATED (Forge git hardening, 14 October 2026; there is no
    exception for register-repo). It runs through ``env -i`` with a fresh
    empty home folder, no personal or system settings file (so no helper,
    header, address rewrite or template hook of the operator's), the network
    settings and exactly one transport, and the credential is chosen
    explicitly, with no fallback between the two choices:

    * no key named (a public repository): no credential, ``https`` only, to
      the ``--github`` address;
    * ``--publish-credential-name NAME``: the publisher's route key of that
      name, by its path, with the publisher's own ssh command and pinned host
      keys, ``ssh`` only, to ``git@<host>:<org>/<name>.git``.

    Returns the report lines; raises :class:`Refused` naming every gap at once.
    """
    from forge.planning.declared_memory import DECLARATION_PATH, read_declared_memory

    checkout = folder / "project"
    credential_env: dict[str, str] = {}
    # The one transport the checked address uses: https (the --github check
    # takes nothing else).
    address, transport = url, transport_of(url) or "https"
    if credential_name is not None:
        from forge.publisher.credential import the_ssh_command_for

        key_file, problem = _route_key_problem(
            credential_name, route_keys_dir, known_hosts, publisher_uid
        )
        if problem or key_file is None:
            raise Refused("project", problem or "the route key could not be used")
        org, _, leaf = key.partition("/")
        host = urlsplit(url).hostname or ""
        address, transport = f"git@{host}:{org}/{leaf}.git", "ssh"
        credential_env["GIT_SSH_COMMAND"] = the_ssh_command_for(str(key_file), str(known_hosts))
    # This machine's proxy settings, when it has any and they carry no login
    # (one with a login is refused by name, before anything runs).
    try:
        proxies = proxy_environment()
    except NetworkRefused as refused:
        raise Refused("project", refused.sentence) from None
    # A fresh empty home folder for the clone, made after every check.
    home = Path(tempfile.mkdtemp(prefix="git-home-", dir=folder))
    cloned = _call(
        run,
        sandbox_git_command(
            [],
            ["clone", "--depth", "1", "--quiet", "--no-recurse-submodules", "--template=",
             "--", address, str(checkout)],
            transport=transport,
            home=str(home),
            credential_env=credential_env,
            proxy_env=proxies,
        ),
        timeout=CLONE_TIMEOUT,
    )
    if cloned.returncode != 0 or not checkout.is_dir():
        if credential_name is None:
            raise Refused(
                "project",
                f"Could not read {url} without a credential. If the repository is "
                "private, add a deploy key to it, put the private half in the "
                "publisher's route-keys folder, and run again with --publish "
                "--publish-credential-name <its name>.",
            )
        raise Refused(
            "project",
            f"Could not read {address} with the publisher's route key "
            f"{credential_name} (git clone stopped with exit code {cloned.returncode}). "
            "Check that the key is the repository's deploy key and that the pinned "
            "host keys are the host's.",
        )
    head = _call(
        run,
        sandbox_git_command(
            [], ["-C", str(checkout), "rev-parse", "HEAD"], transport="none",
            home=str(home), settings=FIXED_SETTINGS,
        ),
    )
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
    assert memory.project is not None  # memory.ok says so
    return [
        Step("project", "ok", f"{url} at {commit}: memory {memory.project}, tests `{test_command}`"),
    ], memory.project


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


def plan_settings_edit(
    text: str,
    *,
    which: str,
    key: str,
    path: str,
    fetch_address: str | None = None,
    entry: Mapping[str, str] | None = None,
) -> YamlEdit:
    """The entries ``key`` needs in one ``forge.yaml``, by line insertion.

    Three in every file (the allowlist, the repository map and the sandbox
    entry); with ``fetch_address`` (the coordinator's file only, 14 October
    2026) a fourth, ``planning.fetch_addresses``.

    ``path`` is the project's folder as that file's own world knows it. The
    sandbox entry copies the name and addresses of a project already on the
    shared sandbox (:func:`shared_sandbox_reference`) from the same file, as written (an address may be a ``${NAME}``); with ``entry``
    (a sandbox of its own, 17 October 2026) that entry is written instead, and
    a sandbox another project's entry already names, or another sandbox's
    addresses or door, is refused. Refuses — writing
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
    if entry is not None:
        # Every value is text, except a list of memory names (memory_projects,
        # design section 2.4a), which stays a list of text.
        entry = {
            k: [str(item) for item in v] if isinstance(v, (list, tuple)) else str(v)
            for k, v in entry.items()
        }
        wanted_keys = tuple(entry)
        _one_project_per_sandbox(which, sandboxes, key=key, entry=entry)
    else:
        found = shared_sandbox_reference(sandboxes, key)
        reference_key, reference = found if found is not None else (LEGACY_SANDBOX, None)
        wanted_keys = ("name", "sidecar_url", "runner_url")
        if not isinstance(reference, dict) or not all(
            isinstance(reference.get(k), str) and reference.get(k) for k in wanted_keys
        ):
            raise Refused(
                which,
                f"its forge.yaml has no complete planning.sandboxes entry on the shared "
                f"sandbox {LEGACY_SANDBOX} to copy the sandbox name and addresses from",
            )
        if reference.get("git_door"):
            raise Refused(
                which,
                f"its planning.sandboxes entry for {reference_key} names a Git door, "
                f"and the shared sandbox {reference.get('name')} never has one (one "
                "sandbox per project); take that line out by hand first",
            )
        entry = {k: str(reference[k]) for k in wanted_keys}
    filesystem = _mapping(_mapping(data.get("permissions")).get("filesystem"))
    allowlist = [str(item) for item in (filesystem.get("allowlist") or [])]
    repo_paths = _mapping(planning.get("target_repo_paths"))

    # A file whose every line ends in CRLF is edited as plain lines and given
    # CRLF back afterwards, so the inserted lines end the way the file's own
    # do. A file mixing the two is edited as it stands.
    crlf = "\n" in text and text.count("\r\n") == text.count("\n")
    plain = text.replace("\r\n", "\n") if crlf else text
    lines = plain.split("\n")
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
        granted_only = (
            isinstance(current_entry, dict)
            and "memory_projects" in entry
            and "memory_projects" not in current_entry
            and current_entry == {k: v for k, v in entry.items() if k != "memory_projects"}
        )
        if current_entry == entry:
            steps.append(Step(which, "unchanged", f"sandboxes has {key} in {entry['name']}"))
        elif granted_only:
            # A project given its sandbox before the memory grant existed (18
            # October 2026): its entry is as this run would write it, without
            # the grant, so the grant is added to it and nothing else changes.
            set_mapping_entry(
                lines, ("planning", "sandboxes", _scalar(key)), "memory_projects",
                _entry_value(entry["memory_projects"]),
            )
            expected["planning"]["sandboxes"][key] = dict(entry)
            steps.append(Step(
                which, "add",
                f"sandboxes {key} memory_projects {', '.join(entry['memory_projects'])}",
            ))
            changed = True
        elif current_entry is not None:
            raise Refused(
                which,
                f"planning.sandboxes already has an entry for {key} that differs from "
                "the one this run would write; fix it by hand",
            )
        else:
            _ensure_block(lines, ("planning", "sandboxes", _scalar(key)))
            for name in wanted_keys:
                set_mapping_entry(
                    lines, ("planning", "sandboxes", _scalar(key)), name, _entry_value(entry[name])
                )
            expected.setdefault("planning", {})
            expected["planning"]["sandboxes"] = {
                **(expected["planning"].get("sandboxes") or {}),
                key: dict(entry),
            }
            steps.append(Step(which, "add", f"sandboxes {key} in sandbox {entry['name']}"))
            changed = True

        if fetch_address is not None:
            addresses = _mapping(planning.get("fetch_addresses"))
            current_address = addresses.get(key)
            if current_address == fetch_address:
                steps.append(Step(which, "unchanged", f"fetch_addresses has {key}"))
            elif current_address is not None:
                raise Refused(
                    which,
                    f"planning.fetch_addresses already has a different address for {key}; "
                    "fix that line by hand",
                )
            else:
                set_mapping_entry(
                    lines, ("planning", "fetch_addresses"), _scalar(key), _scalar(fetch_address)
                )
                expected.setdefault("planning", {})
                expected["planning"]["fetch_addresses"] = {
                    **(expected["planning"].get("fetch_addresses") or {}),
                    key: fetch_address,
                }
                steps.append(Step(which, "add", f"fetch_addresses {key} -> {fetch_address}"))
                changed = True
    except YamlEditRefused as exc:
        raise Refused(which, str(exc)) from None

    if not changed:
        return YamlEdit(which, text, None, steps, entry)
    staged = "\n".join(lines)
    if crlf:
        staged = staged.replace("\n", "\r\n")
    try:
        reread = yaml.safe_load(staged)
    except yaml.YAMLError as exc:
        raise Refused(which, f"the edited forge.yaml would not parse ({exc.__class__.__name__})") from None
    if reread != expected:
        raise Refused(
            which,
            "the edited forge.yaml would change more than the project's own entries; "
            "nothing was written — edit it by hand",
        )
    if not _keeps_every_line(plain.split("\n"), lines):
        raise Refused(which, "the edit would lose a line of the file; nothing was written")
    why_not = _loads_as_forge_settings(staged)
    if why_not is not None:
        raise Refused(which, f"the edited forge.yaml would not load ({why_not}); nothing was written")
    return YamlEdit(which, text, staged, steps, entry)


def _one_project_per_sandbox(
    which: str, sandboxes: Mapping[str, Any], *, key: str, entry: Mapping[str, str]
) -> None:
    """A sandbox of its own takes one project (17 October 2026): refuse when
    another project's entry names this sandbox, or another sandbox has this
    entry's helper, runner or Git door, or (18 October 2026) another
    repository is already granted the memory name this entry is to be
    granted."""
    for other, current in sandboxes.items():
        if other == key or not isinstance(current, dict):
            continue
        if current.get("name") == entry["name"]:
            raise Refused(
                which,
                f"the sandbox {entry['name']} already belongs to {other}, and a sandbox "
                f"takes one project; give {key} a sandbox and a slot of its own",
            )
        # Compared by port, not by spelling: every sandbox is published on
        # the same machine, so one port is one sandbox's whatever the address
        # is written as (an address written as a ${NAME} has no port to read).
        theirs = {_port_of(current.get(f)) for f in ("sidecar_url", "runner_url", "git_door")} - {None}
        for field_name in ("sidecar_url", "runner_url", "git_door"):
            port = _port_of(entry.get(field_name))
            if port is not None and port in theirs:
                raise Refused(
                    which,
                    f"port {port} ({field_name}) of {entry['name']} is already the sandbox "
                    f"{current.get('name')}'s (for {other}); pick a slot no sandbox has",
                )
        if "receipts_root" in entry and current.get("receipts_root") == entry["receipts_root"]:
            raise Refused(
                which,
                f"the receipts_root of {entry['name']} is already the sandbox "
                f"{current.get('name')}'s (for {other}); pick a slot no sandbox has",
            )
        # A memory name belongs to one repository (design section 2.4a): the
        # card of each would otherwise read the other's records.
        theirs_memory = current.get("memory_projects") or []
        if not isinstance(theirs_memory, list):
            theirs_memory = [theirs_memory]
        for name in entry.get("memory_projects") or []:
            if name in [str(item) for item in theirs_memory]:
                raise Refused(
                    which,
                    f"{key} declares the memory name {name}, and {other} is already "
                    f"granted that name; a memory name belongs to one repository, so "
                    f"give {key} a memory name of its own in its .guardkit/config.yaml, "
                    "commit it to the default branch, and register again. Nothing was written",
                )


def _port_of(address: Any) -> int | None:
    """The port an http address names, or ``None``."""
    if not isinstance(address, str) or "${" in address:
        return None
    try:
        return urlsplit(address.strip()).port
    except ValueError:
        return None


@dataclass
class JsonEdit:
    """The publisher route: the new text (``None`` = nothing) and why."""

    original: str
    staged: str | None
    steps: list[Step] = field(default_factory=list)


def plan_publisher_edit(
    text: str,
    *,
    key: str,
    source: str,
    remote: str,
    credential_name: str | None = None,
) -> JsonEdit:
    """Add the one route ``projects[key] = {source, remote}``, and nothing else.

    With ``credential_name`` the route also names its own key, by bare name;
    without it the route is written exactly as it always was.
    """
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
    if credential_name is not None:
        route["credential_name"] = credential_name
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
    said = f"route {key}: {source} -> {remote}"
    if credential_name is not None:
        said += f", sent with its own key '{credential_name}'"
    return JsonEdit(text, staged, [Step("publisher", "add", said)])


def _backup_name(live: str, leaf: str, *, today: date | None = None, action: str = "register") -> str:
    stamp = (today or date.today()).strftime("%Y%m%d")
    return f"{live}.bak-{stamp}-pre-{action}-{leaf}"


def _pending_name(live: str, leaf: str, action: str = "register") -> str:
    """The staged copy: ``<live>.<leaf>-pending`` for register-repo, and
    ``<live>.<leaf>-unregister-pending`` for unregister-repo, so neither
    command's staged copy is ever taken for the other's."""
    return f"{live}.{leaf}-pending" if action == "register" else f"{live}.{leaf}-{action}-pending"


def _existing_backup(
    store: Any, live: str, leaf: str, original: str, folder: str | None, action: str = "register"
) -> str | None:
    """The newest backup of ``live`` for this project that holds ``original``."""
    base = PurePosixPath(live).name
    pattern = re.compile(rf"^{re.escape(base)}\.bak-(\d{{8}})-pre-{re.escape(action)}-{re.escape(leaf)}$")
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
    action: str = "register",
) -> tuple[Step, Staged | None]:
    """Write ``staged`` beside ``live`` as the pending copy, with a dated backup.

    Writes nothing when the same pending copy is already there (the backup it
    was made with is found and named), and nothing at all in a dry run.
    """
    pending = _pending_name(live, leaf, action)
    if store.read(pending) == staged:
        backup = _existing_backup(store, live, leaf, original, folder, action)
        detail = f"already staged as {store.where(pending)}"
        if backup is not None:
            detail += f" (backup {PurePosixPath(backup).name})"
        return Step(which, "unchanged", detail), Staged(live, pending, backup)
    if dry_run:
        return Step(which, "would stage", store.where(pending)), None
    backup = _backup_name(live, leaf, action=action)
    # Both copies take the live file's mode: the publisher's is 600, and a
    # sandbox or volume copy must not be readable more widely than its live file.
    store.write(backup, original, like=like or live)
    store.write(pending, staged, like=like or live)
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


def check_sandbox_clone(
    sandbox: SandboxStore, *, clone: str, leaf: str, url: str, door_note: str | None = None
) -> tuple[Step, bool]:
    """Every read step 3 needs, before anything is written.

    Returns the report line and whether a clone must be made. ``url`` is the
    https address, or the Git door's address for the project (``door_note``
    then says what the door needs, 17 October 2026).
    """
    folder = f"{PROJECTS_FOLDER}/{leaf}"
    ignored = sandbox.git_read("-C", clone, "check-ignore", "-q", folder)
    if ignored.returncode == 1:
        raise Refused(
            "clone",
            f"{PROJECTS_FOLDER} is not git-ignored in the api_test clone ({clone}), so a "
            "project cloned there would show up as api_test's own change; add it to "
            "api_test's .gitignore first",
        )
    if ignored.returncode != 0:
        raise Refused(
            "clone",
            f"could not ask git whether {PROJECTS_FOLDER} is ignored in {clone} "
            f"(git check-ignore stopped with exit code {ignored.returncode})",
        )

    try:
        readable = sandbox_network_git(
            sandbox, ["ls-remote", "--", url, "HEAD"], bare=True,
            transport=transport_of(url) or "https",
        )
    except NetworkRefused as refused:
        raise Refused("clone", f"{refused.sentence} Nothing was written") from None
    if (readable.returncode != 0 or not readable.stdout.strip()) and door_note is not None:
        raise Refused(
            "clone",
            f"the sandbox cannot read {url} through the Git door. Check that {door_note}, "
            "and that the sandbox may reach the door's port. Nothing was written",
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
    origin = sandbox.git_read("-C", target, "remote", "get-url", "origin")
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
    """``git clone <url> <projects folder>/<leaf>`` as the sandbox user, from a
    fresh empty folder, into the absolute target path, isolated."""
    target = f"{clone.rstrip('/')}/{PROJECTS_FOLDER}/{leaf}"
    try:
        made = sandbox_network_git(
            sandbox,
            ["clone", "--quiet", "--no-recurse-submodules", "--template=", "--", url, target],
            bare=False,
            transport=transport_of(url) or "https",
        )
    except NetworkRefused as refused:
        raise Refused("clone", refused.sentence) from None
    if made.returncode != 0:
        raise Refused(
            "clone",
            f"git clone {url} in the sandbox failed (git clone stopped with exit code "
            f"{made.returncode})",
        )
    origin = sandbox.git_read("-C", target, "remote", "get-url", "origin")
    if origin.returncode != 0 or not _same_repository(origin.stdout, url):
        raise Refused("clone", f"{target} was cloned but does not name {url} as its origin")
    return Step("clone", "done", f"{target} cloned from {url}")


def _names_repository(origin: str, org: str, leaf: str) -> bool | None:
    """Does a remote address name ``org/leaf``? ``None`` when it is not a
    repository address at all (a folder, as a sandbox's own clone may have)."""
    text = origin.strip().rstrip("/")
    match = re.match(r"^(?:[a-z][a-z0-9+.-]*://[^/]+/|[^/@:\s]+@[^:/\s]+:)(.+)$", text)
    if not match:
        return None
    path = match.group(1)
    path = path[:-4] if path.endswith(".git") else path
    parts = path.split("/")
    return len(parts) >= 2 and "/".join(parts[-2:]).lower() == f"{org}/{leaf}".lower()


def check_workspace_clone(
    sandbox: SandboxStore, *, clone: str, org: str, leaf: str, url: str, door_note: str | None = None
) -> Step:
    """Step 3 for a sandbox of its own (17 October 2026): nothing is cloned.

    The sandbox was made from the project's own clone, so its workspace clone
    is the factory's clone of the project. Checked, reading only: the factory's
    runtime folder (the sandbox's settings file and its staged copies) is
    git-ignored there; the sandbox can read the project at ``url`` (https, or
    its own Git door port); and the clone's origin, when it is a repository
    address, names this project.
    """
    ignored = sandbox.git_read("-C", clone, "check-ignore", "-q", RUNTIME_FOLDER)
    if ignored.returncode == 1:
        raise Refused(
            "clone",
            f"{RUNTIME_FOLDER} is not git-ignored in the sandbox's workspace clone ({clone}), so "
            "the sandbox's settings file there would show up as the project's own change; "
            "add .guardkit/tmp/ to the project's .gitignore first",
        )
    if ignored.returncode != 0:
        raise Refused(
            "clone",
            f"could not ask git whether {RUNTIME_FOLDER} is ignored in {clone} (is it the "
            f"sandbox's workspace clone? git check-ignore stopped with exit code {ignored.returncode})",
        )
    try:
        readable = sandbox_network_git(
            sandbox, ["ls-remote", "--", url, "HEAD"], bare=True,
            transport=transport_of(url) or "https",
        )
    except NetworkRefused as refused:
        raise Refused("clone", f"{refused.sentence} Nothing was written") from None
    if readable.returncode != 0 or not readable.stdout.strip():
        if door_note is not None:
            raise Refused(
                "clone",
                f"the sandbox cannot read {url} through its Git door port. Check that "
                f"{door_note}, and that the sandbox may reach that port. Nothing was written",
            )
        raise Refused(
            "clone",
            f"the sandbox cannot read {url}. It holds no GitHub credential, so it reads a "
            "public repository over https, or a private one through its own Git door port "
            "(--via-git-door). Nothing was written",
        )
    origin = sandbox.git_read("-C", clone, "remote", "get-url", "origin")
    if origin.returncode == 0 and _names_repository(origin.stdout, org, leaf) is False:
        raise Refused(
            "clone",
            f"the sandbox's workspace clone {clone} comes from {origin.stdout.strip()}, not "
            f"{org}/{leaf}; this sandbox was made for another project",
        )
    return Step(
        "clone", "unchanged",
        f"{clone} is the sandbox's own workspace clone; the factory builds {org}/{leaf} there",
    )


# ---------------------------------------------------------------------------
# Activation step (b) — is the factory drained?
# ---------------------------------------------------------------------------


#: What runs INSIDE the coordinator (``docker exec <coordinator> python -c``):
#: the coordinator holds the ledger and the bus connection, so it is asked.
#: Plain Python on purpose — the coordinator's image may be older than this
#: checkout, so nothing of this module may be imported there. It reads only:
#: ``consumer_info`` on the two durable consumers (the same read the
#: coordinator's own consumer-health check makes), and then the ledger, opened
#: read-only. The bus address carries a credential, so it is scrubbed out of
#: anything said.
#:
#: THE ORDER OF THE TWO OBSERVATIONS IS THE PROOF, and it is fixed here:
#:
#: 1. Intake is already closed (step a), so no new request can reach either
#:    consumer while this runs.
#: 2. The consumers are read FIRST. A request delivered and not yet finished
#:    shows as unacknowledged; one still waiting shows as pending. Both
#:    consumers ack only after their work is in the ledger (the planning
#:    consumer acks after the run or queue row is written; the build consumer
#:    acks on terminal completion), so a request acked before this read is
#:    already in the ledger.
#: 3. The ledger is then read as ONE snapshot — a single read transaction
#:    (BEGIN, every query, COMMIT) — so work moving between tables during the
#:    read (a queue row admitted into a planning run, a run handing over a
#:    build) is seen wholly before or wholly after the move, and every state
#:    on both sides of each move is counted.
#:
#: 4. The consumers are read AGAIN after the snapshot. With intake closed,
#:    every message that can still appear comes from an internal publisher
#:    reacting to work that was in flight at some moment — for example a
#:    planning run that finishes and publishes its build. If that work was in
#:    flight at the snapshot, the snapshot counts it. If it finished and
#:    published after the first consumer read and before the snapshot (the
#:    run terminal, its build not yet admitted, so in no table the snapshot
#:    counts), its message is on a consumer by the second read — pending, or
#:    unacknowledged, since the build consumer acks only when the build ends.
#:    DRAINED needs both consumer reads at zero and the snapshot empty.
#: 5. That whole look is taken TWICE, ``--settle-seconds`` apart (default 30;
#:    the host side does this, see :func:`check_drained`). A publication can
#:    sit in the coordinator's NATS client after ``publish`` has returned and
#:    before the broker has it, which the second consumer read would not see.
#:    The client flushes within milliseconds, far inside the settle interval,
#:    so such a message shows as pending or unacknowledged in the second look,
#:    or its effects change the ledger. DRAINED needs both looks drained AND
#:    the two snapshots identical — every count and a digest of every row's
#:    moving parts — because a changed snapshot means something moved.
#:
#: Reading the ledger first would leave a gap: a request processed and acked
#: between the two reads would be in neither. ``__name__`` guards the run, so
#: the tests can load these functions and move work between the observations.
DRAINED_READ_SCRIPT: str = r'''
import asyncio, hashlib, json, os, sqlite3, sys
from datetime import datetime, timezone
from urllib.parse import urlsplit

def scrubber(url):
    secrets = [s for s in (url, urlsplit(url).password if url else None) if s]
    def said(exc):
        text = f"{type(exc).__name__}: {exc}".splitlines()[0][:300]
        for s in secrets:
            text = text.replace(s, "***")
        return text
    return said

# nats-py retries a first connection for ever, printing each failure, unless
# told to stop after one attempt and given a quiet error callback.
async def quiet(exc):
    pass

async def _consumers(url, spec, said):
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

def read_consumers(url, spec, said):
    try:
        if not url:
            raise RuntimeError("the coordinator has no FORGE_NATS_URL")
        return asyncio.run(asyncio.wait_for(_consumers(url, spec, said), 30))
    except BaseException as exc:
        return {name: {"error": said(exc)} for name in spec["consumers"]}

def read_ledger(con, spec, between=None):
    # One read transaction: every query below sees the same moment.
    # ``between(name)`` is for the tests only: it runs after each query.
    step = between or (lambda name: None)
    con.isolation_level = None
    con.execute("BEGIN")
    try:
        def rows(sql, params=()):
            return con.execute(sql, params).fetchall()
        def one(sql, params=()):
            return int(rows(sql, params)[0][0])
        marks = lambda values: ",".join("?" for _ in values)
        builds = {str(k): int(v) for k, v in rows(
            "SELECT status, COUNT(*) FROM builds WHERE status IN (%s) GROUP BY status"
            % marks(spec["build_active"]), list(spec["build_active"]))}
        interrupted = one("SELECT COUNT(*) FROM builds WHERE status = 'INTERRUPTED'")
        step("builds")
        plans = {str(k): int(v) for k, v in rows(
            "SELECT state, COUNT(*) FROM planning_runs WHERE state NOT IN (%s) GROUP BY state"
            % marks(spec["planning_terminal"]), list(spec["planning_terminal"]))}
        step("planning_runs")
        free = one("SELECT COUNT(*) FROM work_queue WHERE status = 'QUEUED' AND after_id IS NULL")
        held = one("SELECT COUNT(*) FROM work_queue WHERE status = 'QUEUED' AND after_id IS NOT NULL")
        # The coordinator's own in-flight rule (work_queue_loop.count_in_flight):
        # an ADMITTED row is work in flight until the planning run or build it
        # starts has been written under its correlation id; once one exists,
        # that row speaks for itself (an INTERRUPTED build included).
        admitted = rows(
            "SELECT DISTINCT q.correlation_id FROM work_queue q WHERE q.status = 'ADMITTED'"
            " AND q.correlation_id IS NOT NULL")
        unwritten = 0
        admitted_interrupted = 0
        for (correlation_id,) in admitted:
            written = one("SELECT COUNT(*) FROM planning_runs WHERE correlation_id = ?", (correlation_id,)) \
                + one("SELECT COUNT(*) FROM builds WHERE correlation_id = ?", (correlation_id,))
            if not written:
                unwritten += 1
            elif one("SELECT COUNT(*) FROM builds WHERE correlation_id = ? AND status = 'INTERRUPTED'",
                     (correlation_id,)):
                admitted_interrupted += 1
        step("work_queue")
        # LIVE merge and deploy work only, the way the coordinator itself
        # decides it: held when it names a holder AND its expiry is later than
        # now (PublicationRecord.lease_is_live, DeploymentTarget.held_now). A
        # resting result is history nothing clears; it is counted for
        # information, never waited on.
        now = datetime.now(timezone.utc)
        def live(holder, expires):
            if not holder or not expires:
                return False
            try:
                expiry = datetime.fromisoformat(str(expires))
            except ValueError:
                return False
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            return expiry > now
        merges_live = merges_resting = 0
        for holder, expires, result in rows(
                "SELECT lease_holder, lease_expires_at, result FROM publication_records"):
            if live(holder, expires):
                merges_live += 1
            elif result not in spec["publication_finished"]:
                merges_resting += 1
        locks_live = locks_expired = 0
        for holder, expires in rows("SELECT holder_build, expires_at FROM deployment_targets"):
            if live(holder, expires):
                locks_live += 1
            elif holder:
                locks_expired += 1
        step("publication_records")
        # A digest of every row's moving parts, read in the same snapshot, so
        # two looks can tell "nothing moved" from "something moved and ended
        # up looking the same" (a run started and finished in between).
        digest = hashlib.sha256()
        for sql in (
            "SELECT build_id, status FROM builds ORDER BY build_id",
            "SELECT correlation_id, state FROM planning_runs ORDER BY correlation_id",
            "SELECT id, status FROM work_queue ORDER BY id",
            "SELECT build_id, result, lease_holder, lease_expires_at, turn FROM publication_records ORDER BY build_id",
            "SELECT target, counter, holder_build, expires_at FROM deployment_targets ORDER BY target",
        ):
            digest.update(sql.encode())
            for row in rows(sql):
                digest.update(json.dumps(row, default=str).encode())
    finally:
        con.execute("COMMIT")
    return {
        "builds": builds, "interrupted": interrupted, "planning_runs": plans,
        "queue_waiting": free, "queue_held": held,
        "admitted_unwritten": unwritten, "admitted_interrupted": admitted_interrupted,
        "merges_live": merges_live, "merges_resting": merges_resting,
        "deploy_locks_live": locks_live, "deploy_locks_expired": locks_expired,
        "state_digest": digest.hexdigest(),
    }

def observe(spec, environ, consumers=read_consumers, connect=None, between=None, recheck=True):
    # ``recheck=False`` is for the tests only: it shows the gap the second
    # consumer read closes, by repeating the first read instead.
    url = environ.get("FORGE_NATS_URL", "")
    said = scrubber(url)
    out = {"consumers": consumers(url, spec, said)}
    db = environ.get("FORGE_DB_PATH") or spec["db"]
    try:
        con = connect() if connect else sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
        try:
            out["ledger"] = read_ledger(con, spec, between)
        finally:
            con.close()
    except Exception as exc:
        out["ledger"] = {"error": said(exc)}
    out["consumers_after"] = consumers(url, spec, said) if recheck else out["consumers"]
    return out

if __name__ == "__main__":
    print(json.dumps(observe(json.loads(sys.argv[1]), os.environ)))
'''


def drained_read_spec() -> dict[str, Any]:
    """The states, stream and consumer names the read uses — Forge's own."""
    from forge.adapters.nats.planning_consumer import PLANNING_DURABLE_NAME
    from forge.cli._serve_config import DEFAULT_DURABLE_NAME
    from forge.pipeline.publication_record import (
        RESULT_MERGED_AND_RUNNING,
        RESULT_PUBLISHED_TO_MISSION_BRANCH,
    )
    from forge.planning.work_queue_loop import BUILD_ACTIVE_STATES, PLANNING_TERMINAL_STATES

    return {
        "db": COORDINATOR_LEDGER,
        "build_active": sorted(BUILD_ACTIVE_STATES),
        # Both are finished: a mission feature published to its mission's
        # branch owes no deploy (missions, 17 October 2026).
        "publication_finished": [
            RESULT_MERGED_AND_RUNNING,
            RESULT_PUBLISHED_TO_MISSION_BRANCH,
        ],
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


#: How long the drained check waits between its two looks. A publication
#: buffered in the coordinator's NATS client reaches the broker within the
#: client's flush cycle (milliseconds), far inside this.
DEFAULT_SETTLE_SECONDS: int = 30

#: The wait between the two looks. A seam: the tests replace it with a fake
#: clock, so no test sleeps.
sleep: Callable[[float], None] = time.sleep


def check_drained(
    run: Runner, container: str, settle_seconds: float
) -> tuple[list[str], list[str]]:
    """Two complete looks, ``settle_seconds`` apart: (reasons, notes).

    Each look is consumers, one ledger snapshot, consumers again (see
    :data:`DRAINED_READ_SCRIPT`). Drained only when both looks are drained and
    the two snapshots are identical; an empty reason list means drained.
    """
    consumers = drained_read_spec()["consumers"]
    first = read_drained_facts(run, container)
    sleep(settle_seconds)
    second = read_drained_facts(run, container)
    reasons = [f"first look: {r}" for r in judge_drained(first, consumers)]
    reasons += [f"second look: {r}" for r in judge_drained(second, consumers)]
    before, after = first.get("ledger"), second.get("ledger")
    if (
        isinstance(before, dict) and isinstance(after, dict)
        and not before.get("error") and not after.get("error")
        and before != after
    ):
        moved = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        reasons.append(
            f"the ledger changed between the two looks, {settle_seconds:g} seconds apart "
            f"({', '.join(moved)}): something moved, so the factory is not settled"
        )
    return reasons, drained_notes(second)


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
    in the work queue and no admitted item whose planning run or build is not
    written yet (``count_in_flight``'s rule); no merge and no deploy in
    progress (a publication record's lease or a deployment target's lock held
    and not expired — the coordinator's own ``lease_is_live`` and ``held_now``
    rule); and each durable consumer feeding the coordinator reads zero
    pending and zero awaiting acknowledgement both before and after the
    ledger read. Anything that could not be read is a reason too. An INTERRUPTED build is not one: a restart leaves it as it is, and one
    that can be relaunched holds an unacknowledged build request, which the
    consumer check already counts. Neither is a merge or a deploy that has
    stopped and holds nothing live (see :func:`drained_notes`).
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
            ("admitted_unwritten", lambda n: f"{n} admitted queue {_plural(n, 'item has', 'items have')} "
             "no planning run or build yet (being prepared)"),
            ("queue_held", lambda n: f"{n} {_plural(n, 'item waits', 'items wait')} in the work queue behind "
             "another item; wait for the item it waits on to finish, or withdraw it"),
            ("merges_live", lambda n: f"{n} {_plural(n, 'merge is', 'merges are')} in progress "
             "(a publication record's lease is held and has not expired)"),
            ("deploy_locks_live", lambda n: f"{n} {_plural(n, 'deploy is', 'deploys are')} in progress "
             "(a deployment target's lock is held and has not expired)"),
        ):
            n = _count(ledger.get(key))
            if n is None:
                reasons.append(f"the ledger's {key.replace('_', ' ')} could not be counted")
            elif n:
                reasons.append(say(n))
    for key, when in (("consumers", "before the ledger read"), ("consumers_after", "after the ledger read")):
        read = facts.get(key)
        read = read if isinstance(read, dict) else {}
        for name in consumers:
            row = read.get(name)
            if not isinstance(row, dict) or row.get("error"):
                why = row.get("error") if isinstance(row, dict) else "no answer"
                reasons.append(f"the bus consumer {name} could not be read {when} ({why})")
                continue
            pending, unacked = _count(row.get("pending")), _count(row.get("ack_pending"))
            if pending is None or unacked is None:
                reasons.append(f"the bus consumer {name} gave no counts {when}")
            elif pending or unacked:
                reasons.append(
                    f"the bus consumer {name} had {pending} pending and {unacked} awaiting "
                    f"acknowledgement {when} (a request on its way in)"
                )
    return reasons


def drained_notes(facts: Mapping[str, Any]) -> list[str]:
    """What the read found that does not stop activation, said for information.

    History nothing clears: INTERRUPTED builds, merges resting short of
    "merged into the remote and running" with no live lease (a refused merge,
    publication off, a press that stopped), and deployment locks whose holder
    stopped and whose expiry has passed.
    """
    ledger = facts.get("ledger")
    if not isinstance(ledger, dict):
        return []
    notes: list[str] = []
    n = _count(ledger.get("interrupted"))
    if n:
        notes.append(
            f"for information: {n} {_plural(n, 'build is', 'builds are')} INTERRUPTED. A restart "
            "leaves them as they are; one that can be relaunched holds an unacknowledged build "
            "request, which the consumer check above already counts"
        )
    n = _count(ledger.get("admitted_interrupted"))
    if n:
        notes.append(
            f"for information: {n} admitted queue {_plural(n, 'item stays', 'items stay')} ADMITTED "
            "with an INTERRUPTED build; the build speaks for it, as the coordinator's own count has it"
        )
    n = _count(ledger.get("merges_resting"))
    if n:
        notes.append(
            f"for information: {n} {_plural(n, 'merge rests', 'merges rest')} short of 'merged into "
            "the remote and running' with nobody holding it (refused, publication off, or a press "
            "that stopped); a restart does not touch it"
        )
    n = _count(ledger.get("deploy_locks_expired"))
    if n:
        notes.append(
            f"for information: {n} deployment {_plural(n, 'lock was', 'locks were')} left by a build "
            f"that stopped and {_plural(n, 'has', 'have')} expired; the next deploy takes it over"
        )
    return notes


# ---------------------------------------------------------------------------
# Step 7 — what makes it live, printed, never run
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Estate:
    """The names the printed sequence uses, concrete wherever they are known."""

    env_file: str
    estate_check: str
    sandbox: str
    volume: str
    coordinator: str
    settle_seconds: int = DEFAULT_SETTLE_SECONDS
    #: A sandbox of its own (17 October 2026): its slot's ports, said in (e).
    slot: SlotPorts | None = None
    #: The sandbox's machine, when it is another (--sandbox-ssh): the printed
    #: sbx commands are sent there the same way.
    sandbox_ssh: str | None = None


def activation_sequence(
    *,
    key: str,
    estate: Estate,
    coordinator: Staged | None,
    sandbox_file: Staged | None,
    publisher: Staged | None,
    helper_urls: tuple[str, str] | None,
    dry_run: bool,
    memory_project: str | None = None,
    removing: bool = False,
) -> list[str]:
    """The one-page procedure's own order, for this registration (step 7 a–h).

    ``removing`` (forge unregister-repo, 18 October 2026): the same order for
    taking a registration out; its last check is that the coordinator no
    longer lists the project's sandbox, and a sandbox whose settings did not
    change is not restarted.

    ``memory_project`` is the memory name a sandbox of its own is granted
    (design section 2.4a); its memory door line names the same one.

    Every printed step stops, starts or restarts a running container by name
    (S1 window, 18 October 2026): that needs no secrets, so nothing printed
    loads a secrets file, and a restart is explicit because ``docker compose
    up -d`` does not recreate a container when only its settings files
    changed. The coordinator stays up until its own restart, so the drained
    check can be asked again before the publisher and before the coordinator.
    """
    if coordinator is None and sandbox_file is None and publisher is None:
        if dry_run:
            return ["Activation: nothing would be staged, so there would be nothing to activate."]
        if removing:
            return [f"Activation: nothing is staged — {key} is already out of the live settings."]
        return [f"Activation: nothing is staged — {key} is already in the live settings."]
    command = "unregister-repo" if removing else "register-repo"

    def q(value: str) -> str:
        # A name still to be filled in ("$ESTATE_ENV") stays expandable.
        return f'"{value}"' if value.startswith("$") else shlex.quote(value)

    def container(service: str) -> str:
        return f"{COMPOSE_PROJECT}-{service}-1"

    intake = " ".join(container(service) for service in INTAKE_SERVICES)
    drained = (
        f"forge register-repo --check-drained --settle-seconds {estate.settle_seconds}"
        f" --coordinator-container {q(estate.coordinator)}"
    )

    # The sandbox's own machine, when it is another (--sandbox-ssh).
    sbx = (
        f"{' '.join(sandbox_ssh_prefix(shlex.quote(estate.sandbox_ssh)))} sbx"
        if estate.sandbox_ssh
        else "sbx"
    )

    lines = [
        ("What would make it live" if dry_run else "What makes it live")
        + " — the one-page stop/start procedure, run by the delivery owner once the factory's owner agrees."
        " This command restarted nothing.",
        "  Every step stops, starts or restarts a running container by name, which needs no secrets;"
        " nothing here loads a secrets file. If the estate has to be started afresh, use"
        f" {ESTATE_START_SCRIPT} (the one its README names), which hands compose its secrets itself."
        " Each restart is explicit: 'docker compose up -d' does not recreate a container when only"
        " its settings files changed.",
    ]
    if any(v.startswith("$") for v in (estate.env_file, estate.estate_check)):
        lines.append(
            "  Set these first (or pass --estate-env-file to have them filled in): ESTATE_ENV = the"
            " estate env file the factory runs on now; FORGE = the Forge checkout its compose files"
            " come from."
        )
    lines += [
        "(a) Close intake first, so no sentence, queue command or hand-over can arrive"
        " (sessions that publish on the bus directly are asked to hold):",
        f"    docker stop {intake}",
        "(b) Confirm the factory is drained (reads only):",
        f"    {drained}",
        f"    It looks twice, {estate.settle_seconds} seconds apart (consumers, one ledger snapshot,"
        " consumers again, each time), so a message still in a client's buffer is seen.",
        "    Go on only if it exits 0 (rely on the exit status, not the words: \"NOT DRAINED\""
        " contains \"DRAINED\"). Exit 0 means, in both looks: no build active, no planning run,"
        " merge or deploy unfinished, nothing queued, and zero pending and zero unacknowledged on"
        " both bus consumers; and nothing in the ledger moved between the looks. Any other exit — including anything it could not read — stops here: reopen"
        " intake and activate later:",
        f"    docker start {intake}",
        "(c) Put the staged settings in place, as ONE command: it first checks that every live file"
        " is still the one its staged copy was made from, and only then copies any. If a check"
        f" fails nothing is copied: reopen intake as in (b) and run {command} again. If a copy"
        " fails part-way, put the files already copied back from their backups, then restart as"
        " below:",
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
        exec_ = f"{sbx} exec -u {SANDBOX_USER} {q(estate.sandbox)}"
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
    # Taking a project out of a sandbox of its own leaves that sandbox serving
    # nothing, its settings as they were before the project was registered.
    quiet_sandbox = removing and (sandbox_file is None or estate.slot is not None)
    if removing and sandbox_file is None:
        lines.append("(d) The sandbox's settings did not change; its supervisor is not restarted.")
    elif quiet_sandbox:
        lines.append(
            f"(d) {estate.sandbox} now serves no project, and its settings are back as they were before"
            " the project was registered, so its supervisor is not restarted. Stopping or removing the"
            " sandbox is a separate decision."
        )
    elif estate.slot is None:
        lines += [
            "(d) Restart the sandbox supervisor and confirm the helper and runner are ready:",
            f"    docker restart {container(SANDBOX_SUPERVISOR_SERVICE)}",
        ]
    else:
        ports = estate.slot
        lines += [
            f"(d) Start or restart {estate.sandbox}'s own supervisor (its own Compose project on the"
            " sandbox's machine, one per project) and confirm the helper and runner are ready."
            f" Slot {ports.slot} publishes the runner on {ports.runner}, the helper on {ports.helper},"
            f" the git daemon on {ports.git_daemon} and previews on {ports.previews[0]} and"
            f" {ports.previews[1]}; its Git door port is {ports.git_door} and its memory door port"
            f" {ports.memory_door}:",
        ]
        if memory_project:
            lines.append(
                f"    Its memory door answers for the memory {memory_project} only: the estate env"
                f" file's FLEET_MEMORY_SANDBOX_DOORS has the entry {ports.memory_door}:{memory_project}"
                f" (and FLEET_MEMORY_SANDBOX_DOOR_PORTS includes {ports.memory_door}). It is the same"
                f" name the coordinator's entry for {key} is granted (memory_projects: [{memory_project}]),"
                " so the build's approval card reads that memory and no other."
            )
    if not quiet_sandbox:
        lines.append(
            f"    {sbx} exec {q(estate.sandbox)} docker ps --format '{{{{.Names}}}} {{{{.Status}}}}'"
            f"  (both {estate.sandbox}-helper and {estate.sandbox}-runner Up)"
        )
    if helper_urls is not None and not quiet_sandbox:
        sidecar, runner = helper_urls
        lines.append(
            f"    curl -sf {q(sidecar.rstrip('/') + '/healthz')} && curl -sf {q(runner.rstrip('/') + '/ok')}"
        )
    if publisher is not None:
        lines += [
            "(e) The publisher's routes changed: confirm the factory is still drained and restart"
            " the publisher, as ONE command, so it restarts only if the check exits 0 (otherwise"
            " reopen intake as in (b) and activate later); then confirm it lists the new route:",
            f"    {drained} \\",
            f"      && docker restart {container(PUBLISHER_SERVICE)}",
            f"    docker logs {container(PUBLISHER_SERVICE)} 2>&1 | grep 'publisher: settings'"
            f" | grep -F {q(key)}",
        ]
    else:
        lines.append("(e) The publisher's routes did not change; it is not restarted.")
    lines += [
        "(f) Confirm once more that the factory is drained and restart the coordinator, which"
        " reads the new settings when it starts, as ONE command, so it restarts only if the check"
        " exits 0 (otherwise reopen intake as in (b) and activate later):",
        f"    {drained} \\",
        f"      && docker restart {q(estate.coordinator)}",
        "(g) Reopen intake, the Slack-facing services last:",
        f"    docker start {intake}",
        "(h) Check the services, and that the coordinator "
        + ("no longer lists the project's sandbox:" if removing else "lists the new project's sandbox:"),
        f"    DOCKER_HOST=unix:///var/run/docker.sock {q(estate.estate_check)} --env-file"
        f" {q(estate.env_file)} --project {COMPOSE_PROJECT} services",
    ]
    listed = q(f"autobuild dispatch: {key} has a sandbox")
    if removing:
        lines.append(
            f"    docker logs --since 10m {q(estate.coordinator)} 2>&1 | grep -cF {listed}"
            "  (0: no longer listed since the restart)"
        )
    else:
        lines.append(f"    docker logs {q(estate.coordinator)} 2>&1 | grep -F {listed}")
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
    #: With --publish: where the publisher fetches the project's clone from.
    publish_source: str | None
    estate: Estate
    #: With --publish: the bare name of the route's own key, or None.
    publish_credential_name: str | None = None
    #: The publisher's route-keys folder, pinned host keys and user, from the
    #: estate env file (for the check clone with a route key).
    route_keys_dir: str | None = None
    known_hosts_file: str | None = None
    publisher_uid: int | None = None
    #: Read the repository through the Git door (17 October 2026).
    via_git_door: bool = False
    #: A sandbox of its own (17 October 2026): its slot, and the entry the
    #: settings files are given (name, helper, runner, Git door). ``None`` =
    #: the shared sandbox, whose entry is copied from api_test's.
    slot: SlotPorts | None = None
    sandbox_entry: dict[str, str] | None = None
    #: The coordinator's copy of that entry also names the project's own
    #: receipts folder as the coordinator sees it (design section 2.2a).
    coordinator_entry: dict[str, str] | None = None
    #: The sandbox's machine, when it is not this one (--sandbox-ssh).
    sandbox_ssh: str | None = None


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
    """The plain ``https://<host>/<org>/<name>`` address, or a refusal.

    No refusal here repeats the value: what was typed may hold a credential
    (``https://<token>@github.com/...``), so only the character or the shape
    that was wrong is named.
    """
    url = url.strip()
    plain = "give the plain https://github.com/<org>/<name> address (the value is not repeated here)"
    for char, called in (("@", "an @ (a user name or token before the host)"),
                         ("?", "a ? (a query)"), ("#", "a # (a fragment)")):
        if char in url:
            raise Refused("github", f"--github contains {called}; {plain}")
    if not url.startswith("https://") or url.startswith("https:///"):
        raise Refused("github", f"--github does not begin with https:// and a host; {plain}")
    if not urlsplit(url).hostname:
        raise Refused("github", f"--github names no host; {plain}")
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
    git_export_port: int | None,
    sandbox: str | None,
    volume: str,
    coordinator: str,
    settle_seconds: int = DEFAULT_SETTLE_SECONDS,
    publish_credential_name: str | None = None,
    via_git_door: bool = False,
    slot: int | None = None,
    sandbox_address: str | None = None,
    receipts_root: str | None = None,
    sandbox_ssh: str | None = None,
) -> Options:
    """The command's options with every default worked out, or a refusal."""
    org, leaf = _parse_name(name)
    if sandbox_ssh is not None and not _SSH_HOST.match(sandbox_ssh):
        raise Refused(
            "sandbox",
            "--sandbox-ssh is not an ssh host: a host name or alias, or user@host "
            "(the value is not repeated here)",
        )
    if sandbox is None:
        # A sandbox of its own is named after its project (the recipe's
        # convention); without --slot, the shared sandbox as before.
        sandbox = LEGACY_SANDBOX if slot is None else leaf
    shared = sandbox == LEGACY_SANDBOX
    ports: SlotPorts | None = None
    if receipts_root is not None and slot is None:
        raise Refused(
            "sandbox",
            f"--receipts-root is for a sandbox of its own (--slot); {LEGACY_SANDBOX} keeps "
            "the single receipts folder",
        )
    if shared and slot is not None:
        raise Refused(
            "sandbox",
            f"{LEGACY_SANDBOX} is the shared sandbox from before one sandbox per project: it "
            "has no slot and never a Git door. Give the project a sandbox of its own "
            "(--slot <k>, named after the project), or leave --slot out",
        )
    if shared and via_git_door:
        raise Refused(
            "git-door",
            f"the shared sandbox {LEGACY_SANDBOX} never reads through the Git door, so no "
            "private repository can be read where other projects build. Register it into "
            "a sandbox of its own: --slot <k> --via-git-door. Nothing was written",
        )
    if not shared:
        if slot is None:
            raise Refused(
                "sandbox",
                f"a sandbox of its own ({sandbox}) needs its slot, --slot <k> from 1 to "
                f"{MAX_SLOT}: its ports and its Git door port come from it",
            )
        try:
            ports = slot_ports(slot)
        except ValueError as exc:
            raise Refused("sandbox", f"--slot: {exc}") from None
        if receipts_root is None:
            receipts_root = f"{COORDINATOR_PROJECT_RECEIPTS}/{sandbox}"
        if not receipts_root.startswith("/") or ".." in receipts_root.split("/"):
            raise Refused(
                "sandbox",
                "a sandbox of its own keeps its receipts in a folder of its own (design "
                "section 2.2a): --receipts-root <that folder, as the coordinator sees it, "
                "an absolute path>",
            )
        if sandbox != leaf:
            raise Refused(
                "sandbox",
                f"a sandbox of its own is named after its project, {leaf}, and --sandbox "
                f"says {sandbox}; leave --sandbox out or name it {leaf}",
            )
        if git_export_port is not None and git_export_port != ports.git_daemon:
            raise Refused(
                "sandbox",
                f"--git-export-port {git_export_port} is not slot {slot}'s git daemon port "
                f"({ports.git_daemon}); leave it out for a sandbox of its own",
            )
    if publish_credential_name is not None:
        from forge.publisher.settings import WHAT_A_KEY_NAME_IS, a_route_key_name

        if not publish:
            raise Refused(
                "publisher",
                "--publish-credential-name needs --publish: it names the key of "
                "the publisher's route, and without --publish no route is written",
            )
        if not a_route_key_name(publish_credential_name):
            raise Refused(
                "publisher",
                "--publish-credential-name is not a key name: it must be "
                f"{WHAT_A_KEY_NAME_IS} (the value is not repeated here)",
            )
    if not github:
        raise Refused("github", "--github <https address of the repository> is needed")
    url = _check_url(github, leaf)

    estate_values: dict[str, str] = {}
    if estate_env_file is not None:
        try:
            estate_values = read_env_file(estate_env_file)
        except OSError as exc:
            raise Refused("estate", f"could not read the estate env file {estate_env_file} ({exc.strerror})") from None

    # The estate's sandbox env file is the shared sandbox's: a sandbox of its
    # own names its settings file itself.
    if shared and sandbox_env_file is None and estate_values.get(ESTATE_ENV_SANDBOX_FILE) and estate_env_file:
        sandbox_env_file = _resolve_beside(estate_values[ESTATE_ENV_SANDBOX_FILE], estate_env_file)
    if sandbox_settings is None and sandbox_env_file is not None:
        try:
            sandbox_values = read_env_file(sandbox_env_file)
        except OSError as exc:
            raise Refused("sandbox", f"could not read the sandbox env file {sandbox_env_file} ({exc.strerror})") from None
        sandbox_settings = sandbox_values.get(SANDBOX_ENV_CONFIG_PATH) or None
    if not sandbox_settings and ports is not None and sandbox_clone:
        # A sandbox of its own: its settings file is in its workspace clone's
        # runtime folder, as api_test's is in api_test's.
        sandbox_settings = f"{sandbox_clone.rstrip('/')}/{SANDBOX_SETTINGS_IN_CLONE}"
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

    publish_source: str | None = None
    gateway = ""
    if publish or ports is not None:
        gateway = (gateway_address or estate_values.get(ESTATE_ENV_GATEWAY, "")).strip()
        if not gateway:
            needs = "--publish" if ports is None else f"a sandbox of its own ({sandbox})"
            raise Refused(
                "publisher" if ports is None else "sandbox",
                f"{needs} needs the factory gateway address (where the publisher fetches from"
                + (")" if ports is None else ", and where the sandbox reaches its Git door port)")
                + f": pass --gateway-address, or --estate-env-file (its {ESTATE_ENV_GATEWAY} names it)",
            )
        if not _a_plain_address(gateway):
            raise Refused("publisher", f"the gateway address {gateway!r} is not a plain host name or address")
    clone_folder = PurePosixPath(sandbox_clone.rstrip("/")).name
    if publish and ports is None:
        port = DEFAULT_GIT_EXPORT_PORT if git_export_port is None else git_export_port
        publish_source = f"git://{gateway}:{port}/{clone_folder}/{PROJECTS_FOLDER}/{leaf}"

    sandbox_entry: dict[str, str] | None = None
    if ports is not None:
        address = (sandbox_address or _env_value(estate_values, ESTATE_ENV_SANDBOX_ADDRESS) or gateway).strip()
        if not _a_plain_address(address):
            raise Refused(
                "sandbox",
                f"the sandbox publish address {address!r} is not a plain host name or address: "
                f"pass --sandbox-address, or --estate-env-file (its {ESTATE_ENV_SANDBOX_ADDRESS} names it)",
            )
        if publish:
            # The sandbox's own git daemon serves its workspace clone's parent
            # folder, published with the helper and runner at its slot's port.
            publish_source = f"git://{address}:{ports.git_daemon}/{clone_folder}"
        sandbox_entry = {
            "name": sandbox,
            "sidecar_url": f"http://{address}:{ports.helper}",
            "runner_url": f"http://{address}:{ports.runner}",
            "git_door": f"http://{gateway}:{ports.git_door}/",
        }

    env_text, estate_check = estate_names(estate_values, estate_env_file)

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
        publish_source=publish_source,
        publish_credential_name=publish_credential_name,
        via_git_door=via_git_door,
        slot=ports,
        sandbox_entry=sandbox_entry,
        sandbox_ssh=sandbox_ssh,
        coordinator_entry=(
            {**sandbox_entry, "receipts_root": str(receipts_root).rstrip("/")}
            if sandbox_entry is not None
            else None
        ),
        route_keys_dir=estate_values.get(ESTATE_ENV_ROUTE_KEYS_DIR) or None,
        known_hosts_file=estate_values.get(ESTATE_ENV_KNOWN_HOSTS) or None,
        publisher_uid=(
            int(estate_values[ESTATE_ENV_PUBLISHER_UID])
            if str(estate_values.get(ESTATE_ENV_PUBLISHER_UID, "")).isdigit()
            else None
        ),
        estate=Estate(
            env_file=env_text,
            estate_check=estate_check,
            sandbox=sandbox,
            volume=volume,
            coordinator=coordinator,
            settle_seconds=settle_seconds,
            slot=ports,
            sandbox_ssh=sandbox_ssh,
        ),
    )


def estate_names(estate_values: Mapping[str, str], estate_env_file: Path | None) -> tuple[str, str]:
    """The estate env file and the estate check script, as the printed
    sequence names them (stand-ins to fill in when no env file was given)."""
    env_text = str(estate_env_file) if estate_env_file is not None else "$ESTATE_ENV"
    estate_check = "$FORGE/deploy/estate/estate-check"
    compose = estate_values.get(ESTATE_ENV_COMPOSE_FILE, "")
    first = next((part for part in re.split(r"[:,]", compose) if part.strip()), "")
    if first and estate_env_file is not None:
        estate_check = str(_resolve_beside(first.strip(), estate_env_file).parent / "estate-check")
    return env_text, estate_check


def _a_plain_address(value: str) -> bool:
    """A host name or address with nothing else in it (no ${NAME}, no port)."""
    return bool(re.fullmatch(r"[A-Za-z0-9.:\[\]-]+", value)) and "${" not in value


def _env_value(values: Mapping[str, str], name: str) -> str:
    """``values[name]`` with any ``${OTHER}`` in it filled from the same file
    (the example writes SANDBOX_PUBLISH_ADDRESS=${FACTORY_GATEWAY_ADDRESS})."""
    text = values.get(name, "")
    for _ in range(5):
        filled = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", lambda m: values.get(m.group(1), m.group(0)), text)
        if filled == text:
            break
        text = filled
    return text


#: The anonymous visibility lookup register-repo makes when the publisher is
#: private-only: one seam the tests replace, as :data:`run_command` is.
visibility_lookup: Callable[[str, str], int | None] = anonymous_lookup

#: The reminder about the Git door's allow-list (17 October 2026), keyed by
#: the sandbox's own door port (one sandbox per project).
GIT_DOOR_ALLOW_LIST_NOTE: str = (
    "the Git door reads only the repositories on its allow-list, each for one "
    "sandbox's port: the line {port}/{key} must be in the file the estate env "
    "file's GIT_DOOR_REPOS_FILE names, {port} in its GIT_DOOR_PORTS, and the door "
    "restarted after the change (deploy/estate/README.md, 'The Git door')"
)


def _publisher_is_private_only(publisher_text: str) -> bool:
    """Does the publisher's settings file say ``private_only: true``? A file
    that is not readable JSON is refused later, by the route's own check."""
    try:
        data = json.loads(publisher_text)
    except ValueError:
        return False
    return isinstance(data, dict) and data.get("private_only") is True


#: Where a sandbox of its own's settings file starts (17 October 2026): the
#: project's own entries are added to it as to any other settings file.
NEW_SANDBOX_SETTINGS: str = (
    "# THIS SANDBOX'S OWN SETTINGS FILE (FORGE_CONFIG_PATH), written by forge\n"
    "# register-repo: one project per sandbox; paths are this sandbox's own.\n"
    "planning:\n"
    "  enabled: true\n"
)


def check_confirmed_private(options: Options) -> Step:
    """With the publisher private-only: is the repository confirmed private?

    The same test the publisher makes (:func:`forge.publisher.private_remote.confirmed_private`).
    Its first half, the route's own key reading the repository, is step 1's
    check clone with that key, which has already passed by the time this
    runs (it refuses otherwise); so a route with no key of its own cannot be
    confirmed, and is refused.
    """
    if options.publish_credential_name is None:
        raise Refused(
            "private",
            "the publisher publishes only to private repositories (its settings "
            "say private_only), so the repository must be confirmed private, and "
            "that needs the route's own key: run again with "
            "--publish-credential-name <its key>. Nothing was written",
        )
    # Step 1's check clone read git@<the --github host>:<org>/<name>.git, so
    # the key read proves the GitHub repository only when that host is
    # github.com (coach review, 17 October 2026).
    if (urlsplit(options.url).hostname or "").lower() != GITHUB_HOST:
        raise Refused(
            "private",
            "the publisher publishes only to private repositories (its settings "
            f"say private_only), and only a {GITHUB_HOST} repository can be "
            "confirmed private; --github names another host. Nothing was written",
        )
    answer = confirmed_private(
        f"git@github.com:{options.org}/{options.leaf}.git",
        # Step 1's check clone read it with the route's own key.
        key_reads_the_remote=lambda: True,
        lookup=visibility_lookup,
    )
    if not answer.private:
        raise Refused("private", f"{answer.reason}. Nothing was written")
    return Step(
        "private", "ok",
        f"{options.key} is confirmed private: the route's own key read it, and "
        "GitHub does not show it to anybody signed out",
    )


def register(options: Options, run: Runner, steps: list[Step]) -> list[str]:
    """Steps 1–7: report lines go into ``steps``; returns the printed sequence.

    Every check and every read comes before the first write, so a refusal from
    a check leaves everything as it was. The writes come after, in order — the
    sandbox clone, then each staged copy with its backup — and none of them is
    a live file, so a write that fails part-way leaves at most a clone or a
    staged copy behind (said in the report), never a changed live setting.
    The project's own repository is never written.
    """
    with tempfile.TemporaryDirectory(prefix="forge-register-") as folder:
        project_steps, memory_name = check_project_and_memory(
            run,
            key=options.key,
            url=options.url,
            folder=Path(folder),
            credential_name=options.publish_credential_name,
            route_keys_dir=options.route_keys_dir,
            known_hosts=options.known_hosts_file,
            publisher_uid=options.publisher_uid,
        )
        steps += project_steps

    # A sandbox of its own is granted the project's declared memory name (design
    # section 2.4a): the coordinator's entry gets memory_projects, the same
    # name printed for the sandbox's memory door line below, so the card's
    # priors and the door answer for one name. The shared sandbox grants none.
    coordinator_entry = options.coordinator_entry
    granted_memory: str | None = None
    if options.slot is not None:
        granted_memory = _memory_name_to_grant(options.key, memory_name)
        coordinator_entry = {**(coordinator_entry or {}), "memory_projects": [granted_memory]}

    volume = VolumeStore(options.volume, run)
    sandbox = SandboxStore(options.sandbox, run, ssh_host=options.sandbox_ssh)
    host = HostStore()

    coordinator_text = volume.read(SETTINGS_FILE_NAME)
    if coordinator_text is None:
        raise Refused("coordinator", f"could not read {SETTINGS_FILE_NAME} from the volume {options.volume}")
    sandbox_text = sandbox.read(options.sandbox_settings)
    # A sandbox of its own is made without a settings file: this command
    # writes it (17 October 2026), from the few lines below plus the
    # project's own entries, before the sandbox's services first start.
    create_sandbox_file = sandbox_text is None and options.slot is not None
    if create_sandbox_file:
        sandbox_text = NEW_SANDBOX_SETTINGS
    if sandbox_text is None:
        raise Refused("sandbox", f"could not read {options.sandbox_settings} in the sandbox {options.sandbox}")

    coordinator_path = f"{COORDINATOR_PROJECTS_ROOT}/{options.leaf}"
    # A sandbox of its own builds the project in its workspace clone itself;
    # the shared sandbox in a clone in its projects folder.
    clone_path = (
        f"{options.sandbox_clone}/{PROJECTS_FOLDER}/{options.leaf}"
        if options.slot is None
        else options.sandbox_clone
    )
    # Where the sandbox reads the project: the https address, or (17 October
    # 2026) its own sandbox's Git door address for it, which is then also the
    # recorded fetch address.
    door = (options.sandbox_entry or {}).get("git_door")
    if options.via_git_door:
        assert door is not None  # resolve_options refuses the door without a sandbox of its own
        try:
            read_from = git_door_address(door, options.key)
        except ValueError as exc:
            raise Refused("git-door", f"{exc}. Nothing was written") from None
    else:
        read_from = options.url
    # The coordinator's settings also record the project's fetch address
    # (planning.fetch_addresses, 14 October 2026): the factory fetches a
    # starting point from it and never from a clone's own origin. The
    # sandbox's settings never carry it: that file sits inside a project clone.
    coordinator_edit = plan_settings_edit(
        coordinator_text, which="coordinator", key=options.key, path=coordinator_path,
        fetch_address=read_from, entry=coordinator_entry,
    )
    sandbox_edit = plan_settings_edit(
        sandbox_text, which="sandbox", key=options.key, path=clone_path, entry=options.sandbox_entry,
    )

    publisher_edit: JsonEdit | None = None
    if options.publish:
        assert options.publisher_settings is not None and options.publish_source is not None
        publisher_text = host.read(options.publisher_settings)
        if publisher_text is None:
            raise Refused("publisher", f"could not read the publisher's settings file {options.publisher_settings}")
        publisher_edit = plan_publisher_edit(
            publisher_text,
            key=options.key,
            source=options.publish_source,
            remote=f"git@github.com:{options.org}/{options.leaf}.git",
            credential_name=options.publish_credential_name,
        )
        if _publisher_is_private_only(publisher_text):
            steps.append(check_confirmed_private(options))

    door_note = (
        GIT_DOOR_ALLOW_LIST_NOTE.format(key=options.key, port=options.slot.git_door)
        if options.via_git_door and options.slot is not None
        else None
    )
    if options.slot is not None:
        clone_step, clone_needed = check_workspace_clone(
            sandbox, clone=options.sandbox_clone, org=options.org, leaf=options.leaf,
            url=read_from, door_note=door_note,
        ), False
    else:
        clone_step, clone_needed = check_sandbox_clone(
            sandbox, clone=options.sandbox_clone, leaf=options.leaf, url=read_from,
            door_note=door_note,
        )

    # Every read is done and nothing refused: now, and only now, write.
    if clone_needed and options.dry_run:
        steps.append(Step("clone", "would add", clone_step.detail))
    elif clone_needed:
        steps.append(make_sandbox_clone(sandbox, clone=options.sandbox_clone, leaf=options.leaf, url=read_from))
    else:
        steps.append(clone_step)
    if door_note is not None:
        steps.append(Step("git-door", "note", door_note))

    staged: dict[str, Staged | None] = {"coordinator": None, "sandbox": None, "publisher": None}
    edits = [(coordinator_edit, volume, SETTINGS_FILE_NAME, None)]
    if create_sandbox_file:
        steps += sandbox_edit.steps
        assert sandbox_edit.staged is not None
        if options.dry_run:
            steps.append(Step("sandbox", "would create", sandbox.where(options.sandbox_settings)))
        else:
            sandbox.create(options.sandbox_settings, sandbox_edit.staged)
            steps.append(Step(
                "sandbox", "created",
                f"{sandbox.where(options.sandbox_settings)}, the sandbox's own settings file "
                "(nothing reads it until the sandbox's services start)",
            ))
    if options.slot is not None:
        # The runtime folders beside the settings file, made sure of on every
        # registration of a sandbox of its own, the settings file new or not:
        # a run that wrote the file and then failed here, or a file written
        # before this step existed, is put right by running again (Codex
        # review of the S1 window fixes, R3).
        beside = PurePosixPath(options.sandbox_settings).parent
        runtime_folders = [str(beside / name) for name in SANDBOX_RUNTIME_FOLDERS]
        if options.dry_run:
            steps.append(Step(
                "sandbox", "would create",
                f"any missing runtime folder beside its settings file, owner-only: {', '.join(runtime_folders)}",
            ))
        else:
            sandbox.make_folders(runtime_folders)
            steps.append(Step(
                "sandbox", "ready",
                f"the runtime folders its start-up uses, owner-only: {', '.join(runtime_folders)}",
            ))
    if not create_sandbox_file:
        edits.append(
            (sandbox_edit, sandbox, options.sandbox_settings, str(PurePosixPath(options.sandbox_settings).parent))
        )
    for edit, store, live, folder_name in edits:
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
        memory_project=granted_memory,
    )
    return tail


#: The memory service's own rule for a project name (fleet-memory refuses any
#: other), which SandboxEntry.memory_projects holds to as well.
_MEMORY_PROJECT_NAME = re.compile(r"^[a-z0-9_]+$")


def _memory_name_to_grant(key: str, memory_name: str | None) -> str:
    """The declared memory name a sandbox of its own is granted, or refuse."""
    from forge.planning.declared_memory import THE_TWO_LINES

    name = str(memory_name or "").strip()
    if not name:
        raise Refused(
            "project",
            f"{key} declares no memory name in .guardkit/config.yaml, and a sandbox of "
            "its own is granted the name its project declares. Add these two lines, "
            f"commit them to the default branch, and register again:\n{THE_TWO_LINES}",
        )
    if not _MEMORY_PROJECT_NAME.match(name):
        raise Refused(
            "project",
            f"{key} declares the memory name {name!r}, and the memory service takes only "
            "lower-case letters, digits and underscores, so a sandbox of its own cannot "
            "be granted it. Correct the name in .guardkit/config.yaml, commit it to the "
            "default branch, and register again. Nothing was written",
        )
    return name


@click.command(name="register-repo")
@click.argument("name", required=False)
@click.option("--github", "github", default=None, help="The repository's https:// address on GitHub.")
@click.option("--publish", is_flag=True, default=False, help="Also add the publisher's route for it.")
@click.option("--publish-credential-name", "publish_credential_name", default=None,
              help="With --publish: the bare name of the route's own key in the publisher's "
                   "route-keys folder (default: the publisher-wide credential).")
@click.option("--via-git-door", "via_git_door", is_flag=True, default=False,
              help="Read a private repository through the Git door (the coordinator's "
                   "planning.git_door): record its fetch address through the door and "
                   "make the sandbox's clone through it. The repository must be on the "
                   "door's allow-list.")
@click.option("--dry-run", "dry_run", is_flag=True, default=False,
              help="Check the project and say what would change; write nothing anywhere.")
@click.option("--check-drained", "check_drained_only", is_flag=True, default=False,
              help="Activation step (b) only: say DRAINED, or why not. Reads only.")
@click.option("--settle-seconds", type=click.IntRange(min=0), default=DEFAULT_SETTLE_SECONDS,
              show_default=True,
              help="How far apart the drained check's two looks are.")
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
@click.option("--git-export-port", type=int, default=None,
              help=f"With --publish: the port the sandbox's git export is published on (default "
                   f"{DEFAULT_GIT_EXPORT_PORT} for the shared sandbox; a sandbox of its own uses its slot's).")
@click.option("--sandbox", "sandbox", default=None,
              help=f"The sandbox the project builds in: {DEFAULT_SANDBOX}, the shared one (the default), "
                   "or with --slot one of its own, named after the project (the default with --slot).")
@click.option("--slot", "slot", type=int, default=None,
              help=f"With --sandbox <its own>: the sandbox's slot, 1 to {MAX_SLOT}; its ports and "
                   "its Git door port come from it (one sandbox per project).")
@click.option("--sandbox-ssh", "sandbox_ssh", default=None, envvar="FORGE_SANDBOX_SSH",
              help="The sandbox's machine, when it is not this one: every sbx command is "
                   "sent there over ssh (the coordinator's and the publisher's files stay "
                   "on this machine).")
@click.option("--receipts-root", "receipts_root", default=None,
              help="With --slot: the project's own receipts folder, as the coordinator sees it "
                   f"(default {COORDINATOR_PROJECT_RECEIPTS}/<sandbox name>; design section 2.2a).")
@click.option("--sandbox-address", "sandbox_address", default=None,
              help=f"With --slot: where the coordinator reaches the sandbox's helper and runner "
                   f"(default: the estate env's {ESTATE_ENV_SANDBOX_ADDRESS}, else the gateway address).")
@click.option("--settings-volume", "volume", default=DEFAULT_SETTINGS_VOLUME, show_default=True,
              help="The coordinator's settings volume.")
@click.option("--coordinator-container", "coordinator", default=DEFAULT_COORDINATOR_CONTAINER,
              show_default=True, help="The running coordinator's container.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Print the report as JSON.")
def register_repo_cmd(
    name: str | None,
    github: str | None,
    publish: bool,
    publish_credential_name: str | None,
    via_git_door: bool,
    dry_run: bool,
    check_drained_only: bool,
    settle_seconds: int,
    estate_env_file: Path | None,
    sandbox_env_file: Path | None,
    sandbox_settings: str | None,
    sandbox_clone: str | None,
    publisher_settings: Path | None,
    gateway_address: str | None,
    git_export_port: int | None,
    sandbox: str | None,
    slot: int | None,
    sandbox_ssh: str | None,
    receipts_root: str | None,
    sandbox_address: str | None,
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

    if check_drained_only:
        consumers = drained_read_spec()["consumers"]
        reasons, found = check_drained(run, coordinator, settle_seconds)
        notes = [Step("drained", "note", note) for note in found]
        if not reasons:
            _emit(
                [Step("drained", "ok", "DRAINED: in two looks "
                      f"{settle_seconds:g} seconds apart, no build active, no planning run, merge "
                      "or deploy unfinished, nothing queued in the work queue, "
                      f"{' and '.join(consumers)} each read zero pending and zero unacknowledged "
                      "before and after the ledger read, and nothing in the ledger moved")] + notes,
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
            settle_seconds=settle_seconds,
            publish_credential_name=publish_credential_name,
            via_git_door=via_git_door,
            slot=slot,
            sandbox_address=sandbox_address,
            receipts_root=receipts_root,
            sandbox_ssh=sandbox_ssh,
        )
        tail = register(options, run, steps)
    except Refused as refusal:
        steps.append(Step(refusal.step, "refused", refusal.detail))
        _emit(steps, as_json=as_json)
        raise click.exceptions.Exit(1) from None
    _emit(steps, as_json=as_json, tail=tail)
