"""``forge register-repo`` — one command takes a git checkout to "the factory
can build in it" (Lane A, stage one; binding spec 2026-09-05).

Registering a repository used to be thirteen manual steps across four files.
This command does the twelve that are mechanical and *prints* the one that is
not: the forge-prod recreate, which a human runs when they are ready. Nothing
here starts, stops or recreates a container, and nothing here touches a model.

What it does, in order:

1. Refuses early and writes nothing at all when ``--name`` carries a path
   separator, when the checkout is not a thing the build side could ever
   resolve (missing, not a directory, no ``.git``, not owned by uid 1000, not
   directly under ``FORGE_REPO_BASE``), when a repository map key for this name
   already points somewhere else, or when there is no ``test:`` command for the
   merge-ready gate to run.
2. Warns and carries on for the things that are merely thin: no git remote, no
   test roots, no ``deploy/profile.yaml``, no ``docs/architecture-rules.yaml``.
   With ``--deploy-port`` it writes the four deploy files instead of warning:
   a ``deploy/profile.yaml`` naming this repository's Docker Sandbox, the
   wrapper that brings that sandbox up, the deploy script that runs inside it,
   and the candidate overlay that puts the throwaway copy on its own port.
   Without the flag nothing about deploys is written and the warning stands.
3. Scaffolds guardkit in the repository when it has none, writes the minimal
   ``toolchain:`` block when the repository declares none (it NEVER overwrites a
   declaration that is already there), and records the fleet-memory project id.
4. Adds the checkout to ``permissions.filesystem.allowlist`` and both key
   spellings — ``guardkit/<name>`` and ``appmilla_github/<name>`` — to
   ``planning.target_repo_paths``, by **surgical line insertion**. The live
   ``forge.yaml``'s comment blocks are load-bearing prose written by the people
   who run this estate; dumping the parsed model back to YAML would erase them,
   so this module never does that. One dated backup is taken before the first
   change, and :func:`forge.config.loader.load_config` must re-parse the result
   or the backup goes back and the command exits non-zero.
5. Checks that no build is in flight — asking a running forge-prod container
   first and ``FORGE_DB_PATH`` second, the way the recreate script's own gate
   reads the ledger, and never creating a ledger of its own — and prints the
   recreate command.

Exit codes: 0 = registered (or nothing to do, or a warn-only run); 1 = refused,
with a plain sentence saying which check said no.
"""

from __future__ import annotations

import importlib.resources
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import click
import yaml

__all__ = ["register_repo_cmd"]


# ---------------------------------------------------------------------------
# Constants — every one of them mirrors a seam named in the spec
# ---------------------------------------------------------------------------

#: The uid the estate's checkouts belong to. A checkout owned by anyone else
#: cannot be written by the build container's bind, so registering it would
#: mint a claim the build side cannot honour.
EXPECTED_OWNER_UID: int = 1000

#: Environment variable and default for the directory the build side resolves
#: repositories under by basename (``autobuild_runner.py:1110`` and
#: ``:1210``; the resolution itself is ``autobuild_runner.py:1463-1479``).
FORGE_REPO_BASE_ENV: str = "FORGE_REPO_BASE"
DEFAULT_FORGE_REPO_BASE: str = "~/Projects/appmilla_github"

#: The two key spellings the estate uses for the same repository. Builds are
#: queued with ``appmilla_github/<name>``; the planning flows use
#: ``guardkit/<name>``. Both are minted from this one loop so they cannot drift.
REPO_MAP_NAMESPACES: tuple[str, ...] = ("guardkit", "appmilla_github")

#: The command a human runs after this one, printed and never run.
RECREATE_COMMAND: str = "bash ops/forge-prod-recreate.sh"

#: The container the estate runs the forge in, and the config path inside it.
#: The recreate script's own gate reads the ledger through this container
#: (``ops/forge-prod-recreate.sh``), because the ledger the *estate* builds
#: against lives in the container's bind, not beside whatever directory a human
#: happened to run this command from. This gate reads it the same way.
FORGE_PROD_CONTAINER: str = "forge-prod"
FORGE_PROD_CONFIG: str = "/var/forge/forge.yaml"

#: The environment variable that names a ledger to read when there is no
#: forge-prod container (``cli/status.py:108``).
FORGE_DB_PATH_ENV: str = "FORGE_DB_PATH"

#: How long the two docker reads may take before the gate gives up and warns.
DOCKER_READ_TIMEOUT: int = 30

#: fleet-memory's identifier contract (``fleet-memory``
#: ``src/fleet_memory/payloads/base.py:16``), enforced by guardkit's own
#: sanitiser (``guardkit/knowledge/fleet_memory_payloads.py:38-53``).
_IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z0-9_]+$")
_IDENTIFIER_SANITISER = re.compile(r"[^A-Za-z0-9_]+")

#: The default test timeout guardkit's declaration schema itself uses
#: (``guardkit/orchestrator/toolchain_declaration.py:205``).
DEFAULT_TEST_TIMEOUT: int = 300

#: Where the shipped files for a new repository's deploy live: two shell
#: scripts and the candidate overlay. They are real files, not Python strings,
#: so they can be read and diffed as what they are (the same reasoning as the
#: lifecycle schema's ``.sql``).
DEPLOY_TEMPLATE_PACKAGE: str = "forge.cli.deploy_templates"

#: The hosts every repository's sandbox is allowed to reach: the Debian
#: mirrors it installs system packages from and the Python index it installs
#: Python packages from. A sandbox reaches nothing off its own network without
#: a rule, so without these an image build inside it cannot fetch anything.
DEFAULT_SANDBOX_ALLOW_NETWORK: tuple[str, ...] = (
    "deb.debian.org",
    "security.debian.org",
    "*.debian.org",
    "pypi.org",
    "files.pythonhosted.org",
)

#: How much memory and how many processors a new repository's sandbox gets.
#: The same settings the first sandbox on this box was proven with.
DEFAULT_SANDBOX_MEMORY: str = "6g"
DEFAULT_SANDBOX_CPUS: int = 4

#: The four files ``--deploy-port`` writes, in the order they are reported.
DEPLOY_FILES: tuple[str, ...] = (
    "deploy/profile.yaml",
    "deploy/sandbox-deploy.sh",
    "deploy/deploy.sh",
    "deploy/docker-compose.candidate.yml",
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


def _tail(name: str) -> list[Step]:
    """The two closing lines.

    They are NOT part of the aligned table: they are the sentences a human
    copies, so they print exactly as the spec writes them (``next`` and
    ``slack`` both pad to six characters, so their values still line up).
    """
    return [
        Step("next", "ok", f"run: {RECREATE_COMMAND}"),
        Step("slack", "ok", f"target: {name}  <your first feature>"),
    ]


def _emit(
    steps: Sequence[Step], *, as_json: bool, tail: Sequence[Step] = ()
) -> None:
    if as_json:
        click.echo(json.dumps([s.as_dict() for s in (*steps, *tail)], indent=2))
        return
    rendered = _render(steps)
    if rendered:
        click.echo(rendered)
    for line in tail:
        click.echo(f"{line.step.ljust(5)} {line.detail}")


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


def _split_lines(text: str) -> list[str]:
    return text.split("\n")


def _join_lines(lines: Sequence[str]) -> str:
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Seams other modules own — module-level so tests can rebind them
# ---------------------------------------------------------------------------


def _run_guardkit_init(repo: Path, template: str) -> subprocess.CompletedProcess[str]:
    """Shell out to ``guardkit init <template>`` from the repository."""
    return subprocess.run(  # noqa: S603 — fixed argv, no shell
        ["guardkit", "init", template],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


#: Directory names that are never test roots, guardkit's own list verbatim
#: (``installer/core/commands/lib/smoke_gates_nudge.py:32``).
_TEST_ROOT_SKIP_NAMES = frozenset({"__pycache__", ".pytest_cache", "node_modules"})


def _shallow_test_roots(repo: Path) -> list[str]:
    """``tests/<name>`` for every immediate subdirectory of ``tests/``.

    guardkit's own rule, rewritten here rather than imported: one level deep,
    directories only, skipping names that begin with a dot and the three cache
    and build directories guardkit skips, sorted
    (``smoke_gates_nudge.py:40-82``). It is used only when guardkit is not
    importable, which is the ordinary case on the surface Rich runs this
    command from: forge's venv carries the guardkit CLI on PATH, not the
    guardkit package.
    """
    tests_dir = Path(repo) / "tests"
    if not tests_dir.is_dir():
        return []
    try:
        return sorted(
            f"tests/{child.name}"
            for child in tests_dir.iterdir()
            if child.is_dir()
            and not child.name.startswith(".")
            and child.name not in _TEST_ROOT_SKIP_NAMES
        )
    except OSError:
        return []


def _discover_test_roots(repo: Path) -> list[str]:
    """Forge's own ``discover_target_test_roots`` (``target_terminal_tools.py:487``).

    That function imports guardkit's discovery and raises
    ``TargetTestRootsUnresolved`` when guardkit is not importable — which is
    what happens every time this command is run the way the spec says Rich runs
    it, from the forge checkout, whose venv has the guardkit CLI on PATH but no
    guardkit package. Reporting "not available here" there would be useless to
    the reader, so the plain directory scan above answers instead: it is the
    same rule guardkit applies, and it gives the same answer for the shapes
    this check exists to catch.
    """
    try:
        from forge.planning.target_terminal_tools import discover_target_test_roots

        return list(discover_target_test_roots(repo))
    except Exception:  # noqa: BLE001 — no guardkit here; scan the tree instead
        return _shallow_test_roots(repo)


#: A callable that runs a command and hands back the finished process. It is a
#: parameter, not a hard-wired ``subprocess.run``, so the tests can answer for
#: docker without docker being installed, running, or touched.
Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def _run_command(argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    """Run a command and capture its output. Reads only; changes nothing."""
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        timeout=DOCKER_READ_TIMEOUT,
        check=False,
    )


def _forge_prod_is_running(run: Runner) -> bool:
    """Is there a container called forge-prod, and is it up?

    ``docker inspect`` answers both questions in one read: a missing container
    is a non-zero exit, a stopped one prints ``false``.
    """
    try:
        result = run(
            [
                "docker",
                "inspect",
                "-f",
                "{{.State.Running}}",
                FORGE_PROD_CONTAINER,
            ]
        )
    except (OSError, subprocess.SubprocessError):
        # No docker on this machine, or it did not answer. Not an error: the
        # next branch reads the ledger a different way.
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def _terminal_status_values() -> set[str]:
    """The terminal build states, by name (``cli/status.py:95-100``)."""
    from forge.cli.status import _TERMINAL_STATES

    return {str(state.value) for state in _TERMINAL_STATES}


def _not_terminal_in_container(run: Runner) -> int:
    """How many builds the forge-prod ledger shows as not terminal.

    The read is exactly the recreate script's gate of record, in its JSON
    spelling: ``docker exec forge-prod forge --config /var/forge/forge.yaml
    status --json``. It runs inside the container, so it reads the ledger the
    estate actually builds against, and it starts, stops and changes nothing.
    """
    result = run(
        [
            "docker",
            "exec",
            FORGE_PROD_CONTAINER,
            "forge",
            "--config",
            FORGE_PROD_CONFIG,
            "status",
            "--json",
        ]
    )
    if result.returncode != 0:
        said = (result.stderr or result.stdout or "").strip().splitlines()
        raise RuntimeError(
            said[-1]
            if said
            else f"'forge status' in {FORGE_PROD_CONTAINER} exited {result.returncode}"
        )
    rows = json.loads(result.stdout)
    if not isinstance(rows, list):
        raise RuntimeError("'forge status --json' did not print a list of builds")
    terminal = _terminal_status_values()
    return sum(1 for row in rows if str(row.get("status", "")) not in terminal)


def _read_ledger_views(db_path: Path) -> list[Any]:
    """The status projection from a ledger file, opened read-only.

    ``read_only_connect`` is a ``mode=ro`` handle: a path that is not already a
    database is an error here, never a new empty one. This command must never
    bring a ledger into being — an invented ledger would show no builds and the
    gate would say "all terminal" about an estate it had not read.
    """
    from forge.cli.status import _read_status_views

    return list(_read_status_views(db_path, None))


def _all_terminal(views: Iterable[Any]) -> bool:
    """The existing terminal test (``cli/status.py:416-425``) — not re-implemented."""
    from forge.cli.status import _all_terminal as existing

    return existing(views)


def _waiting_step(waiting: int) -> Step:
    """``estate ok`` when nothing is in flight, ``estate wait <n>`` otherwise."""
    if waiting <= 0:
        return Step("estate", "ok", "all builds terminal")
    subject = "build is" if waiting == 1 else "builds are"
    return Step("estate", "wait", f"{waiting} {subject} not terminal")


def _estate_step(runner: Runner | None = None) -> Step:
    """The estate gate's one line. Reads; never starts, stops or creates anything.

    In the order the recreate gate of record uses:

    1. a running ``forge-prod`` container — ask it, the way the recreate script
       asks it;
    2. otherwise ``FORGE_DB_PATH``, read-only;
    3. otherwise say plainly that the ledger could not be read.

    Any failure along the way is a warn, not a refusal: registering a
    repository is not made wrong by a gate that could not see the queue, and
    the human still gets the recreate command with the warning above it.
    """
    run = runner or _run_command
    try:
        if _forge_prod_is_running(run):
            return _waiting_step(_not_terminal_in_container(run))
        raw = os.environ.get(FORGE_DB_PATH_ENV, "").strip()
        if raw:
            views = _read_ledger_views(Path(raw).expanduser())
            return _waiting_step(sum(1 for v in views if not _all_terminal([v])))
        return Step(
            "estate",
            "warn",
            "could not read the build ledger (no forge-prod container and "
            "FORGE_DB_PATH unset)",
        )
    except Exception as exc:  # noqa: BLE001 — an unreadable ledger is a warn
        return Step("estate", "warn", f"could not read the build ledger: {exc}")


# ---------------------------------------------------------------------------
# Reading what the repository already declares
# ---------------------------------------------------------------------------


def _repo_config_path(repo: Path) -> Path:
    """``<repo>/.guardkit/config.yaml`` (``toolchain_declaration.py:144``)."""
    return repo / ".guardkit" / "config.yaml"


def _read_repo_config_dict(repo: Path) -> dict[str, Any]:
    """The repository's guardkit config as a plain dict, or ``{}``.

    guardkit reads this file the same way — ``yaml.safe_load`` into a plain
    dict, keys pulled by name (``guardkit/orchestrator/security_config.py:84-86``,
    ``guardkit/orchestrator/coach_grammar.py:88-93``,
    ``guardkit/planning/context_switch.py:76-82``). There is no whole-file
    schema, so a key guardkit does not know about is inert.
    """
    path = _repo_config_path(repo)
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — an unreadable config is "declares nothing"
        return {}
    return data if isinstance(data, dict) else {}


def _guardkit_is_importable() -> bool:
    """Is guardkit's declaration loader importable in this interpreter?

    Asked BEFORE calling forge's wrapper, because the wrapper logs a loud
    warning aimed at the forge image when guardkit is missing — a true sentence
    in that context, and pure noise in this one, where the raw-dict fallback
    below answers the same question correctly.
    """
    from importlib.util import find_spec

    try:
        return find_spec("guardkit.orchestrator.toolchain_declaration") is not None
    except Exception:  # noqa: BLE001 — an import machinery failure is "no"
        return False


def _declared_test_command(repo: Path) -> str | None:
    """The repository's declared ``toolchain.test``, or ``None``.

    Asks guardkit's own loader first, through forge's existing wrapper
    (``cli/_serve_conductor.py:367-410``), so the answer is the one the
    merge-ready checkpoint will get. That wrapper returns ``None`` both when the
    declaration is absent AND when guardkit is not importable in this
    interpreter, and those two are very different for a writer: the second must
    never be read as "declares nothing" or this command would append a second
    ``toolchain:`` block over a real one. So the raw dict is consulted too, and
    a declaration found either way counts.
    """
    declaration = None
    if _guardkit_is_importable():
        try:
            from forge.cli._serve_conductor import load_declared_toolchain

            declaration = load_declared_toolchain(repo)
        except Exception:  # noqa: BLE001 — a reader defect is no licence to write
            declaration = None
    if declaration is not None:
        command = getattr(declaration, "test", None)
        if command:
            return str(command)
    raw = _read_repo_config_dict(repo).get("toolchain")
    if isinstance(raw, dict) and raw.get("test"):
        return str(raw["test"])
    return None


def project_id_for(name: str) -> str:
    """The fleet-memory project id for a repository name.

    The existing sanitiser rule, verbatim
    (``guardkit/knowledge/fleet_memory_payloads.py:38-53``): every run of
    characters outside ``[A-Za-z0-9_]`` becomes one underscore, and leading or
    trailing underscores are trimmed.
    """
    if not name:
        return "unknown"
    cleaned = _IDENTIFIER_SANITISER.sub("_", name).strip("_")
    return cleaned or "unknown"


def _git_remote(repo: Path) -> str | None:
    """The first configured git remote's name, or ``None``."""
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", "remote"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception:  # noqa: BLE001 — no git binary reads as "no remote"
        return None
    if result.returncode != 0:
        return None
    remotes = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return remotes[0] if remotes else None


# ---------------------------------------------------------------------------
# Config path resolution
# ---------------------------------------------------------------------------


def _resolve_config_path(ctx: click.Context) -> Path:
    """The ``forge.yaml`` this command edits: ``--config``, else ``./forge.yaml``.

    Never hard-coded. The group already loaded whichever of these it found
    (``cli/main.py:62-80``); this reads the same decision back so the file that
    gets edited is the file that got loaded.
    """
    parent = ctx.parent
    explicit = parent.params.get("config_path") if parent is not None else None
    if explicit is not None:
        return Path(explicit)
    default = Path("forge.yaml")
    if default.exists():
        return default
    raise click.ClickException(
        "no forge.yaml to register into — pass --config /path/to/forge.yaml "
        "before the subcommand, or run from a directory that has one"
    )


def _has_path_separator(name: str) -> bool:
    """Does this name carry a separator any filesystem here would act on?

    ``/`` is checked outright rather than only through :data:`os.sep`, because
    the repository-map keys are built with a literal ``/`` whatever platform
    this runs on, and a backslash is checked because it is a separator on one
    of them.
    """
    separators = {"/", "\\", os.sep}
    if os.altsep:
        separators.add(os.altsep)
    return any(sep in name for sep in separators)


def _repo_base() -> Path:
    raw = os.environ.get(FORGE_REPO_BASE_ENV, "").strip() or DEFAULT_FORGE_REPO_BASE
    return Path(raw).expanduser().resolve()


# ---------------------------------------------------------------------------
# The deploy files (--deploy-port) — the Docker Sandbox this repository
# deploys into, the wrapper that brings it up, the script that runs inside,
# and the overlay that puts the candidate copy on its own port
# ---------------------------------------------------------------------------


def compose_project_for(name: str) -> str:
    """The compose project name for a repository, from its own name.

    Lower case, with anything that is not a letter or a digit becoming a
    hyphen, because that is what a compose project name and a sandbox name may
    both contain. Returns an empty string when nothing usable is left.
    """
    cleaned = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return re.sub(r"-{2,}", "-", cleaned)


def sandbox_name_for(name: str) -> str:
    """The sandbox name for a repository: its compose project plus ``-deploy``."""
    project = compose_project_for(name)
    return f"{project}-deploy" if project else ""


def _read_deploy_template(filename: str) -> str:
    """Read one of the deploy files shipped beside this module."""
    return (
        importlib.resources.files(DEPLOY_TEMPLATE_PACKAGE)
        .joinpath(filename)
        .read_text(encoding="utf-8")
    )


def render_deploy_files(
    *,
    name: str,
    repo: Path,
    app_port: int,
    allow_extra: str | None = None,
) -> dict[str, str]:
    """Render the four deploy files for a repository, as {path: text}.

    Args:
        name: The repository's registered name.
        repo: The checkout's absolute path — the profile's ``cwd``, and the
            path the sandbox bind-mounts, so it reads the same inside and out.
        app_port: The port the app is published on. The candidate copy is
            published on the next port up.
        allow_extra: One more host the sandbox may reach, written ``host:port``
            — the model door on this box for a repository whose app talks to a
            model. None ⇒ only the Debian and Python rules.
    """
    project = compose_project_for(name)
    sandbox = sandbox_name_for(name)
    candidate_port = app_port + 1
    allow = list(DEFAULT_SANDBOX_ALLOW_NETWORK)
    if allow_extra:
        allow.append(allow_extra)
    allow_yaml = ", ".join(f'"{host}"' for host in allow)

    profile = f"""\
# How the factory deploys {name}.
#
# Every merge deploys this repository into its own Docker Sandbox — a small
# virtual machine with its own kernel and its own Docker engine. The sandbox
# bind-mounts this checkout at the path below, so the path is the same inside
# it and out, and publishes the app port and the candidate port back to the
# host, so the health checks and the live gate keep running from the host.
#
# Written by `forge register-repo --deploy-port {app_port}`. Edit it freely; the
# command never rewrites a profile that is already here.
format_version: "1.0"
env_id: local
compose:
  file: docker-compose.yml
  # The only script the deploy step runs. It makes sure the sandbox below is
  # up and awake, then runs deploy/deploy.sh inside it and returns that
  # script's exit code unchanged.
  script: deploy/sandbox-deploy.sh
hosts:
  - host: localhost
    role: app
secret_injection: []
models_required: []
# Add a health check when this repository has one — a script path, never a
# command line, because the script's exit code is the verdict:
# health_checks:
#   - cmd: "deploy/healthcheck.sh"
# The image kept so a failed deploy can be put back. deploy/deploy.sh keeps it.
rollback_image_ref: "{project}-app:rollback-pre-deploy"
# This checkout's absolute path. The deploy step resolves the script above
# relative to it, and the sandbox mounts this same path inside itself.
cwd: "{repo}"
# The candidate copy: the build stands up on the port below first and is
# checked there, and only a pass takes the live port. `keep: false` tears the
# candidate down again afterwards.
candidate:
  env:
    CANDIDATE_PORT: "{candidate_port}"
  keep: false
# This repository's Docker Sandbox.
sandbox:
  name: {sandbox}
  memory: {DEFAULT_SANDBOX_MEMORY}
  cpus: {DEFAULT_SANDBOX_CPUS}
  publish: ["127.0.0.1:{app_port}:{app_port}", "127.0.0.1:{candidate_port}:{candidate_port}"]
  allow_network: [{allow_yaml}]
"""

    replacements = {
        "@@NAME@@": name,
        "@@PROJECT@@": project,
        "@@SANDBOX@@": sandbox,
        "@@APP_PORT@@": str(app_port),
        "@@CANDIDATE_PORT@@": str(candidate_port),
    }
    rendered: dict[str, str] = {"deploy/profile.yaml": profile}
    # The wrapper is copied exactly as it is shipped. It is the same file
    # api_test deploys with, byte for byte: every value it needs — the sandbox
    # name, its memory, its processors, its ports and the hosts it may reach —
    # arrives in its environment from the profile above, so there is nothing in
    # it to fill in for this repository.
    rendered["deploy/sandbox-deploy.sh"] = _read_deploy_template("sandbox-deploy.sh")
    # These two do carry this repository's own names and ports.
    for path, template in (
        ("deploy/deploy.sh", "deploy.sh"),
        ("deploy/docker-compose.candidate.yml", "docker-compose.candidate.yml"),
    ):
        text = _read_deploy_template(template)
        for token, value in replacements.items():
            text = text.replace(token, value)
        rendered[path] = text
    return rendered


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


@click.command(name="register-repo")
@click.argument("repo_path", type=click.Path(path_type=Path))
@click.option(
    "--name",
    "name_opt",
    default=None,
    help="Name the repository is registered under. Defaults to the folder name.",
)
@click.option(
    "--template",
    "template",
    default="default",
    show_default=True,
    help="Template passed to `guardkit init` when the repository has no guardkit.",
)
@click.option(
    "--toolchain-test",
    "toolchain_test",
    default=None,
    help=(
        "The test command the merge-ready gate runs. Required when the "
        "repository does not already declare one."
    ),
)
@click.option(
    "--deploy-port",
    "deploy_port",
    type=int,
    default=None,
    help=(
        "Write the repository's deploy files, publishing the app on this port "
        "and its candidate copy on the next one up. Without this nothing "
        "about deploys is written."
    ),
)
@click.option(
    "--deploy-allow",
    "deploy_allow",
    default=None,
    help=(
        "One more host the deployment sandbox may reach, written host:port — "
        "the model door on this box for a repository whose app talks to a "
        "model. Needs --deploy-port."
    ),
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Say what would change and change nothing.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the same report as a JSON list of {step, status, detail}.",
)
@click.pass_obj
def register_repo_cmd(
    config: Any,
    repo_path: Path,
    name_opt: str | None,
    template: str,
    toolchain_test: str | None,
    deploy_port: int | None,
    deploy_allow: str | None,
    dry_run: bool,
    as_json: bool,
) -> None:
    """Register a git checkout so the factory can build in it.

    Checks the checkout, adds it to the forge's filesystem allowlist and to the
    repository map under both key spellings, writes the toolchain block into the
    repository's guardkit config when it has none, derives and prints the
    fleet-memory project id, reports what the repository has and lacks, checks
    that no build is running, and prints the one command left for a human.

    With ``--deploy-port`` it also writes the four files a repository needs to
    be deployed into its own Docker Sandbox: the profile that names the
    sandbox, the wrapper that brings it up, the deploy script that runs inside
    it, and the candidate overlay that puts the throwaway copy on its own
    port.
    """
    ctx = click.get_current_context()
    steps: list[Step] = []

    #: Undo actions for everything already written, newest first. A refusal is
    #: "nothing was registered", so it must also be "nothing was left changed":
    #: the repository's config goes back to what it said and the dated backup,
    #: now standing behind no change at all, is removed.
    rollbacks: list[Callable[[], None]] = []

    def refuse(step: str, detail: str) -> None:
        for undo in reversed(rollbacks):
            try:
                undo()
            except OSError:  # pragma: no cover — a failed undo must not mask why
                pass
        steps.append(Step(step, "refused", detail))
        _emit(steps, as_json=as_json)
        raise click.ClickException(detail)

    config_path = _resolve_config_path(ctx)
    if config is None:
        config = _load(config_path)

    # ---- the name is checked first, before anything at all is read from disk
    # or written to it. A name is not free text: it becomes two repository-map
    # keys, part of the backup file's name, and the folder name the build side
    # resolves under the base. A separator in it would mint the key
    # ``guardkit/a/b``, which nothing looks up, and would send the dated backup
    # into some other directory — so it is refused here, where nothing has yet
    # been written and there is nothing to undo.
    if name_opt is not None and _has_path_separator(name_opt):
        refuse(
            "name",
            f"--name {name_opt!r} contains a path separator — the name becomes "
            "a folder name and two repository-map keys, so it must be a plain "
            "name with no slashes in it",
        )

    # ---- the deploy flags are checked here too, before anything is written.
    # A port that cannot be a port, or a name that cannot be a sandbox name,
    # would only surface later as a file this repository could not deploy with.
    if deploy_allow is not None and deploy_port is None:
        refuse(
            "deploy-files",
            "--deploy-allow says what else the deployment sandbox may reach, "
            "so it only means something with --deploy-port, which is what "
            "writes the sandbox — pass both, or neither",
        )
    if deploy_port is not None and not (1 <= deploy_port <= 65534):
        refuse(
            "deploy-files",
            f"--deploy-port {deploy_port} is not a port the app and its "
            "candidate copy can share — pass a whole number from 1 to 65534, "
            "because the candidate copy takes the next port up",
        )

    # ---- rule 2: the checks that refuse (nothing is written when any fails)
    repo = Path(repo_path).expanduser()
    if not repo.exists():
        refuse("path", f"{repo} does not exist")
    if not repo.is_dir():
        refuse("path", f"{repo} is not a directory")
    repo = repo.resolve()
    steps.append(Step("path", "ok", str(repo)))

    if not (repo / ".git").exists():
        refuse("git", f"{repo} is not a git checkout — it has no .git")
    steps.append(Step("git", "ok", "checkout has .git"))

    owner_uid = repo.stat().st_uid
    if owner_uid != EXPECTED_OWNER_UID:
        refuse(
            "owner",
            f"{repo} is owned by uid {owner_uid}, not uid {EXPECTED_OWNER_UID} — "
            "the build container could not write to it",
        )
    steps.append(Step("owner", "ok", f"uid {EXPECTED_OWNER_UID}"))

    base = _repo_base()
    if repo.parent != base:
        refuse(
            "base",
            f"{repo} is not directly under {base} — the build side resolves a "
            "repository by folder name under that directory, so a checkout "
            "anywhere else can never be found",
        )
    steps.append(Step("base", "ok", f"directly under {base}"))

    name = name_opt or repo.name
    map_keys = [f"{namespace}/{name}" for namespace in REPO_MAP_NAMESPACES]
    existing_map: dict[str, str] = dict(
        getattr(getattr(config, "planning", None), "target_repo_paths", {}) or {}
    )
    for key in map_keys:
        current = existing_map.get(key)
        if current is not None and Path(current).expanduser() != repo:
            refuse(
                "repo-map",
                f"{key} already points at {current}, not {repo} — pick another "
                "--name or fix the map by hand",
            )

    if deploy_port is not None and not sandbox_name_for(name):
        refuse(
            "deploy-files",
            f"{name} does not reduce to a name a Docker Sandbox can carry — a "
            "sandbox name is lower-case letters, digits and hyphens, so pick "
            "another --name",
        )

    declared_test = _declared_test_command(repo)
    if declared_test is None and not toolchain_test:
        refuse(
            "toolchain",
            f"{name} declares no test command and none was given — pass "
            "--toolchain-test 'the command that runs the tests'; without it the "
            "merge-ready gate has nothing to run",
        )

    # ---- rule 4, first half: measure what forge.yaml needs and take the dated
    # backup. This happens HERE, before the guardkit scaffold and before the
    # repository's own config is written, because the spec says the backup is
    # taken before the first mutation and the repository-side write is the
    # first mutation. (The guardkit scaffold, when one is created, is left in
    # place on a refusal: deleting a whole scaffold is a bigger act than the
    # one that failed. Everything else undoes.)
    original_text = config_path.read_text(encoding="utf-8")
    lines = _split_lines(original_text)

    allowlist = [
        str(Path(entry).expanduser())
        for entry in getattr(
            getattr(getattr(config, "permissions", None), "filesystem", None),
            "allowlist",
            [],
        )
        or []
    ]
    allowlist_needed = str(repo) not in allowlist
    map_needed = [key for key in map_keys if key not in existing_map]

    if not allowlist_needed and not map_needed:
        backup_step = Step("backup", "unchanged", "nothing to change, no backup taken")
    elif dry_run:
        backup_step = Step("backup", "would-add", _backup_path(config_path, name).name)
    else:
        backup_file = _backup_path(config_path, name)
        backup_file.write_text(original_text, encoding="utf-8")
        rollbacks.append(lambda: backup_file.unlink(missing_ok=True))
        backup_step = Step("backup", "ok", backup_file.name)

    # ---- rule 3: the checks that warn and carry on
    remote = _git_remote(repo)
    if remote is None:
        steps.append(
            Step(
                "remote",
                "warn",
                "no git remote — branches stay local; the merge-ready push leg "
                "will report not-pushed",
            )
        )
    else:
        steps.append(Step("remote", "ok", f"git remote {remote}"))

    # ---- rule 5: the repository's own guardkit config
    already_initialised = (repo / ".guardkit").is_dir() or (repo / ".claude").is_dir()
    if already_initialised:
        steps.append(Step("guardkit", "ok", "already initialised"))
    elif dry_run:
        steps.append(Step("guardkit", "would-add", f"guardkit init {template}"))
    else:
        result = _run_guardkit_init(repo, template)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            reason = detail[-1] if detail else f"exit {result.returncode}"
            refuse(
                "guardkit",
                f"guardkit init {template} failed in {repo} ({reason}) — nothing "
                "was written to the forge config",
            )
        steps.append(Step("guardkit", "added", f"guardkit init {template}"))

    identifier = project_id_for(name)
    if not _IDENTIFIER_PATTERN.match(identifier):  # pragma: no cover — defensive
        refuse(
            "project-id",
            f"{name} does not reduce to a usable fleet-memory project id",
        )

    repo_config = _repo_config_path(repo)
    repo_text = (
        repo_config.read_text(encoding="utf-8") if repo_config.is_file() else ""
    )
    repo_lines = _split_lines(repo_text) if repo_text else []
    repo_changed = False

    if declared_test is not None:
        steps.append(Step("toolchain", "unchanged", f"declares test: {declared_test}"))
    elif dry_run:
        steps.append(Step("toolchain", "would-add", f"test: {toolchain_test}"))
    else:
        repo_lines = _write_toolchain_block(
            repo_lines, str(toolchain_test), DEFAULT_TEST_TIMEOUT
        )
        repo_changed = True
        steps.append(Step("toolchain", "added", f"test: {toolchain_test}"))

    memory_block = _read_repo_config_dict(repo).get("memory")
    has_project = isinstance(memory_block, dict) and memory_block.get("project")
    if has_project:
        steps.append(Step("memory", "unchanged", f"project: {memory_block['project']}"))
    elif dry_run:
        steps.append(Step("memory", "would-add", f"project: {identifier}"))
    else:
        repo_lines = _write_memory_project(repo_lines, identifier)
        repo_changed = True
        steps.append(Step("memory", "added", f"project: {identifier}"))

    if repo_changed and not dry_run:
        repo_config.parent.mkdir(parents=True, exist_ok=True)
        existed_before = repo_config.is_file()
        previous_text = repo_text if existed_before else None
        text = _join_lines(repo_lines)
        if text and not text.endswith("\n"):
            text += "\n"
        repo_config.write_text(text, encoding="utf-8")

        def _restore_repo_config(
            path: Path = repo_config, previous: str | None = previous_text
        ) -> None:
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(previous, encoding="utf-8")

        rollbacks.append(_restore_repo_config)

    steps.append(Step("project-id", "ok", identifier))

    # ---- the deploy files. Only written when --deploy-port asks for them,
    # and never over a file that is already there: a profile someone wrote by
    # hand is the record of how this repository actually deploys.
    if deploy_port is not None:
        rendered = render_deploy_files(
            name=name, repo=repo, app_port=deploy_port, allow_extra=deploy_allow
        )
        try:
            _parse_written_profile(rendered["deploy/profile.yaml"])
        except Exception as exc:  # noqa: BLE001 — the parser's own sentence
            refuse("deploy-files", f"the deploy profile would not load: {exc}")
        added_any = False
        for relative in DEPLOY_FILES:
            target = repo / relative
            if target.exists():
                steps.append(
                    Step("deploy-files", "unchanged", f"{relative} is already there")
                )
                continue
            if dry_run:
                steps.append(Step("deploy-files", "would-add", relative))
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(rendered[relative], encoding="utf-8")
            if relative.endswith(".sh"):
                target.chmod(0o755)
            rollbacks.append(
                lambda path=target: path.unlink(missing_ok=True)  # type: ignore[misc]
            )
            steps.append(Step("deploy-files", "added", relative))
            added_any = True
        # The closing line reports the profile that is now ON DISK, never the
        # ports this run asked for: a profile someone wrote by hand is left
        # alone, and saying "on ports 8911 and 8912" about a repository whose
        # own profile deploys on 9000 would be a plain lie.
        if not dry_run:
            steps.append(
                Step(
                    "deploy-files",
                    "ok",
                    _deploy_profile_sentence(repo, added_any=added_any),
                )
            )

    # ---- rule 6: what the repository has and lacks
    try:
        roots = _discover_test_roots(repo)
    except Exception as exc:  # noqa: BLE001 — a failure here warns, never stops
        steps.append(
            Step(
                "test-roots",
                "warn",
                f"the repository's test roots could not be listed ({exc})",
            )
        )
    else:
        if roots:
            steps.append(Step("test-roots", "ok", ", ".join(roots)))
        else:
            steps.append(
                Step(
                    "test-roots",
                    "warn",
                    "tests/ holds no subdirectory, so plans that name smoke "
                    "gates will fail plan-containment; add tests/<area>/",
                )
            )

    for step_name, relative, missing_sentence in (
        ("qa-gates", "qa/gates/registry.yaml", "no qa/gates/registry.yaml"),
        ("deploy", "deploy/profile.yaml", "no deploy/profile.yaml"),
        (
            "arch-rules",
            "docs/architecture-rules.yaml",
            "no docs/architecture-rules.yaml",
        ),
    ):
        if (repo / relative).exists():
            steps.append(Step(step_name, "ok", relative))
        else:
            steps.append(Step(step_name, "warn", missing_sentence))

    # ---- rule 4, second half: the forge config, by surgical line insertion.
    # The file was read and the backup taken above, before the first mutation.
    steps.append(backup_step)

    allowlist_status = (
        "unchanged" if not allowlist_needed else ("would-add" if dry_run else "added")
    )
    if allowlist_needed and not dry_run:
        try:
            append_sequence_item(
                lines, ("permissions", "filesystem", "allowlist"), str(repo)
            )
        except YamlEditRefused as exc:
            refuse("allowlist", str(exc))
    steps.append(Step("allowlist", allowlist_status, str(repo)))

    for key in map_keys:
        needed = key in map_needed
        status = "unchanged" if not needed else ("would-add" if dry_run else "added")
        if needed and not dry_run:
            try:
                set_mapping_entry(
                    lines, ("planning", "target_repo_paths"), key, str(repo)
                )
            except YamlEditRefused as exc:
                refuse("repo-map", str(exc))
        steps.append(Step("repo-map", status, f"{key} -> {repo}"))

    if (allowlist_needed or map_needed) and not dry_run:
        config_path.write_text(_join_lines(lines), encoding="utf-8")
        try:
            _load(config_path)
        except Exception as exc:  # noqa: BLE001 — any parse failure restores
            config_path.write_text(original_text, encoding="utf-8")
            refuse(
                "config",
                f"the edited {config_path.name} no longer parses "
                f"({exc.__class__.__name__}) — the backup has been restored and "
                "nothing was registered",
            )
        steps.append(Step("config", "ok", f"{config_path.name} re-parses"))
    else:
        steps.append(Step("config", "unchanged", f"{config_path.name} untouched"))

    # ---- rule 7: the estate gate. Prints the recreate command, never runs it.
    steps.append(_estate_step())

    _emit(steps, as_json=as_json, tail=_tail(name))


# ---------------------------------------------------------------------------
# Small helpers used by the command
# ---------------------------------------------------------------------------


def _parse_written_profile(text: str) -> Any:
    """Prove the profile just rendered is one the deploy step can read.

    Uses the deploy step's own loader, so the file this command writes is held
    to exactly the rules the deploy step will hold it to.
    """
    from forge.deploy.profile import parse_deploy_profile

    return parse_deploy_profile(yaml.safe_load(text))


def _deploy_profile_sentence(repo: Path, *, added_any: bool) -> str:
    """One plain sentence about the deploy profile this repository now has.

    Read back from disk after the files are written, so what is reported is
    what the repository really carries. A profile that was already there keeps
    its own sandbox and its own ports, and those are the ones named.

    Args:
        repo: The checkout.
        added_any: Whether this run wrote any deploy file at all.
    """
    path = repo / "deploy" / "profile.yaml"
    sandbox = None
    try:
        profile = _parse_written_profile(path.read_text(encoding="utf-8"))
        sandbox = profile.sandbox
    except Exception:  # noqa: BLE001 — an unreadable profile is reported, not raised
        sandbox = None

    ports = _host_ports_of(sandbox.publish) if sandbox is not None else []
    if sandbox is not None and ports:
        return f"sandbox {sandbox.name} on {_ports_phrase(ports)}"
    if not added_any:
        return "deploy files already present, unchanged"
    if sandbox is not None:
        return f"sandbox {sandbox.name}, whose profile publishes no port"
    return "the deploy profile already here names no sandbox"


def _host_ports_of(publish: tuple[str, ...]) -> list[str]:
    """The host-side port of each publish rule, in the order they are written.

    A rule is written ``PORT``, ``HOST_PORT:SANDBOX_PORT`` or
    ``HOST_ADDRESS:HOST_PORT:SANDBOX_PORT``; the host port is the last part
    but one, except in the one-part form where it is the only part.
    """
    ports: list[str] = []
    for rule in publish:
        parts = rule.split(":")
        port = parts[0] if len(parts) == 1 else parts[-2]
        if port and port not in ports:
            ports.append(port)
    return ports


def _ports_phrase(ports: list[str]) -> str:
    """``port 8911`` / ``ports 8911 and 8912`` / ``ports 1, 2 and 3``."""
    if len(ports) == 1:
        return f"port {ports[0]}"
    return f"ports {', '.join(ports[:-1])} and {ports[-1]}"


def _load(config_path: Path) -> Any:
    from forge.config.loader import load_config

    return load_config(config_path)


def _backup_path(config_path: Path, name: str) -> Path:
    """``forge.yaml.bak-YYYYMMDD-pre-register-<name>`` beside the config."""
    stamp = date.today().strftime("%Y%m%d")
    return config_path.with_name(f"{config_path.name}.bak-{stamp}-pre-register-{name}")


def _write_toolchain_block(lines: list[str], command: str, timeout: int) -> list[str]:
    """Add ``test:`` to the repository's toolchain declaration without overwriting.

    When there is no ``toolchain:`` key the minimal block is appended whole.
    When there is one that simply has no ``test:``, only the keys the block does
    not already declare are inserted into it. Nothing that is already declared
    is rewritten, and no key is ever written twice.
    """
    lines = list(lines)
    if locate(lines, ("toolchain",)) is None:
        while lines and not lines[-1].strip():
            lines.pop()
        if lines:
            lines.append("")
        lines.extend(["toolchain:", f"  test: {command}", f"  test_timeout: {timeout}"])
        return lines
    # The block is there but declares no ``test``. Insert that — and insert
    # ``test_timeout`` ONLY when the block declares none. Writing it
    # unconditionally appends a SECOND ``test_timeout:`` beside the
    # repository's own; PyYAML takes the last key, so a repository that had
    # declared 900 would silently be run at 300, and stricter parsers reject a
    # file carrying duplicate keys at all. Never overwrite a declaration.
    declares_timeout = locate(lines, ("toolchain", "test_timeout")) is not None
    set_mapping_entry(lines, ("toolchain",), "test", command)
    if not declares_timeout:
        set_mapping_entry(lines, ("toolchain",), "test_timeout", str(timeout))
    return lines


def _write_memory_project(lines: list[str], identifier: str) -> list[str]:
    """Add ``memory.project`` when it is absent, leaving anything else alone."""
    lines = list(lines)
    if locate(lines, ("memory",)) is None:
        while lines and not lines[-1].strip():
            lines.pop()
        if lines:
            lines.append("")
        lines.extend(["memory:", f"  project: {identifier}"])
        return lines
    set_mapping_entry(lines, ("memory",), "project", identifier)
    return lines
