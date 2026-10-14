"""The one way Forge runs git on this machine's own repositories.

Git reads the repository's own settings (``.git/config``, includes, the
project's attribute files), and some settings name a program for git to run:
a filter, a file-system monitor, a diff converter, a merge program, a signing
program, a credential helper, an ssh command, hooks. The model writing the
code, and the project's own scripts, can change those settings, and every
project clone Forge works in is shared with the build's model worktrees. So
every local git command Forge runs goes through :func:`run_git` (or
:func:`run_git_async`, or :func:`open_git` for the two streaming readers),
which:

1. gives git a fixed environment instead of Forge's own, so no factory secret
   (the model service key, the memory database address, the bus password)
   reaches git or any program it starts, and no git command can reach another
   repository (``GIT_ALLOW_PROTOCOL=none``, ``GIT_NO_LAZY_FETCH=1``);
2. adds fixed settings on the command line (:data:`FIXED_SETTINGS`), which
   beat every settings file: no file monitor, no hooks, no signing, no
   submodule recursion, a rebase to-do list that cannot carry an ``exec`` line;
3. reads the setting names just before each command and switches off every
   filter, custom merge program and branch merge option named there;
4. adds fixed options per command (:data:`COMMAND_OPTIONS`, and Forge's own
   :data:`FORGE_COMMAND_OPTIONS`);
5. refuses writes in a project that holds a submodule or would take in a
   nested repository, because no setting keeps ``git add`` out of one;
6. adds a new worktree in two steps (``--no-checkout``, then the names read
   and ``reset --hard`` inside it), because a new worktree's checkout reads
   its settings again where an include can switch a filter on;
7. refuses every command that reaches another repository. The one exception
   is :mod:`forge.network_git`'s local-only step, which brings commits in
   from the factory's own copy through :func:`run_from_local_copy`;
8. treats file names literally, and replaces git's own error text with a
   fixed sentence, so nothing git prints on failure reaches a log, a card, a
   record or a model. stdout is still returned whole.

THE SAME RULES AS GUARDKIT. GuardKit's ``guardkit/lib/project_git.py`` holds
the same rules. Forge does not import GuardKit as a library, so they are
copied, and ``tests/forge/git/test_project_git_matches_guardkit.py`` pins this
copy to GuardKit's named constants (the release image check compares the two
live). Forge's additions are kept apart in :data:`FORGE_COMMAND_OPTIONS`,
:data:`CALLER_SETTINGS` and :data:`FORGE_REFUSED_OPTIONS`.

The threat model is "honest but fallible": the code being built and the model
building it are not trying to race the factory. A program that changes git's
settings in the few milliseconds between the name read and the command could
still run, but only with the fixed environment, so it gets no secret.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

logger = logging.getLogger(__name__)

__all__ = [
    "BASE_SETTINGS",
    "CALLER_ENVIRONMENT_NAMES",
    "CALLER_SETTINGS",
    "COMMAND_OPTIONS",
    "FIXED_ENVIRONMENT",
    "FIXED_SETTINGS",
    "FORGE_COMMAND_OPTIONS",
    "FORGE_REFUSED_OPTIONS",
    "GitRefused",
    "IDENTITY_ENVIRONMENT_NAMES",
    "KEPT_ENVIRONMENT_NAMES",
    "MERGE_PROGRAM",
    "NETWORK_COMMANDS",
    "NETWORK_REFUSAL",
    "NETWORK_REMOTE_COMMANDS",
    "REBASE_OPTIONS",
    "REBASE_REFUSAL",
    "REBASE_SETTINGS",
    "REFUSED_EXIT_CODE",
    "SETTINGS_REFUSAL",
    "SUBMODULE_REFUSAL",
    "UNSAFE_NAME_REFUSAL",
    "WORKTREE_REFUSAL",
    "WRITE_COMMANDS",
    "failure_sentence",
    "git_exec_folder",
    "git_program",
    "open_git",
    "pattern_pathspec",
    "prepare_git",
    "run_from_local_copy",
    "run_git",
    "run_git_async",
    "timeout_sentence",
]

# ---------------------------------------------------------------------------
# The fixed rules, as named constants (pinned to GuardKit's by a test)

#: Settings given on every git command line. Command-line settings beat every
#: settings file, includes too.
BASE_SETTINGS: Tuple[str, ...] = (
    # File monitor and hooks.
    "core.fsmonitor=false",
    "core.hooksPath=/dev/null",
    # Signing: never sign, never check a signature, and the signing programs
    # are the harmless system program ``false``.
    "commit.gpgSign=false",
    "tag.gpgSign=false",
    "log.showSignature=false",
    "merge.verifySignatures=false",
    "gpg.program=false",
    "gpg.ssh.program=false",
    "gpg.x509.program=false",
    # Submodules: never recurse, never look inside, never summarise.
    "submodule.recurse=false",
    "diff.ignoreSubmodules=dirty",
    "diff.submodule=short",
    "status.submoduleSummary=false",
    # Background work and other programs.
    "gc.auto=0",
    "maintenance.auto=false",
    "core.alternateRefsCommand=",
    "merge.default=text",
    "core.editor=:",
    "sequence.editor=:",
    "core.pager=cat",
)

#: A rebase writes a to-do list before it replays commits, and these settings
#: decide what is in it. ``rebase.instructionFormat`` could add an ``exec``
#: line; the rest are fixed so nothing about the list depends on the project.
#: Git versions that do not know a setting ignore it.
REBASE_SETTINGS: Tuple[str, ...] = (
    "rebase.instructionFormat=%s",
    "rebase.rebaseMerges=false",
    "rebase.updateRefs=false",
    "rebase.autoSquash=false",
    "rebase.backend=merge",
    "rebase.missingCommitsCheck=ignore",
)

#: Every fixed setting, in the order it is given.
FIXED_SETTINGS: Tuple[str, ...] = BASE_SETTINGS + REBASE_SETTINGS

#: Environment values git always gets.
FIXED_ENVIRONMENT: Dict[str, str] = {
    "LC_ALL": "C",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ALLOW_PROTOCOL": "none",
    "GIT_NO_LAZY_FETCH": "1",
}

#: Commit identity values a caller may pass; Forge's own are kept.
IDENTITY_ENVIRONMENT_NAMES: Tuple[str, ...] = (
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_AUTHOR_DATE",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
    "GIT_COMMITTER_DATE",
)

#: Names copied from Forge's own environment when set. The three
#: ``GIT_CONFIG_*`` names only say WHICH system and personal settings files
#: git reads (those files are read either way: the containers' commit identity
#: lives in ``/etc/gitconfig``); every program-running setting in them is
#: switched off on the command line like the project's own.
KEPT_ENVIRONMENT_NAMES: Tuple[str, ...] = (
    "HOME",
    "XDG_CONFIG_HOME",
    "TZ",
    "TMPDIR",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_NOSYSTEM",
) + IDENTITY_ENVIRONMENT_NAMES

#: Names a caller may add for one command.
CALLER_ENVIRONMENT_NAMES: Tuple[str, ...] = (
    "GIT_INDEX_FILE",
    "GIT_OPTIONAL_LOCKS",
) + IDENTITY_ENVIRONMENT_NAMES

#: Commands that reach another repository. Refused outright.
NETWORK_COMMANDS: Tuple[str, ...] = (
    "fetch",
    "pull",
    "push",
    "clone",
    "ls-remote",
    "fetch-pack",
    "send-pack",
    "credential",
    "submodule",
)

#: ``git remote`` sub-commands that contact the remote. Refused.
NETWORK_REMOTE_COMMANDS: Tuple[str, ...] = ("update", "show", "prune", "set-head")

#: Options added right after the command name.
COMMAND_OPTIONS: Dict[str, Tuple[str, ...]] = {
    "diff": ("--no-ext-diff", "--no-textconv", "--submodule=short", "--ignore-submodules=dirty"),
    "log": ("--no-ext-diff", "--no-textconv", "--submodule=short"),
    "show": ("--no-ext-diff", "--no-textconv", "--submodule=short"),
    "status": ("--ignore-submodules=dirty",),
    "merge": ("--no-gpg-sign", "--no-verify-signatures"),
    "commit": ("--no-gpg-sign",),
    "rebase": ("--no-gpg-sign", "--no-rebase-merges", "--no-update-refs", "--no-autosquash"),
}

#: The only options a factory ``git rebase`` may carry (the fixed ones in
#: :data:`COMMAND_OPTIONS` are added after this check).
REBASE_OPTIONS: Tuple[str, ...] = ("--autostash", "--abort")

#: ``merge`` and ``rebase`` actions that take no other option.
_ACTION_OPTIONS = frozenset(
    {"--abort", "--continue", "--skip", "--quit", "--edit-todo", "--show-current-patch"}
)

#: Commands refused when the project holds a submodule (``worktree add`` too).
WRITE_COMMANDS: Tuple[str, ...] = (
    "add",
    "commit",
    "checkout",
    "switch",
    "restore",
    "reset",
    "merge",
    "rebase",
    "stash",
    "apply",
    "cherry-pick",
    "revert",
    "read-tree",
)

#: The merge program every custom one is replaced by: git's own line merge.
MERGE_PROGRAM = "git merge-file --marker-size=%L %A %O %B"

# ---------------------------------------------------------------------------
# Forge's own additions (the only allowed differences from GuardKit's rules)

#: Options Forge adds for commands GuardKit never runs.
FORGE_COMMAND_OPTIONS: Dict[str, Tuple[str, ...]] = {
    "commit-tree": ("--no-gpg-sign",),
}

#: Command options refused because they run a program: ``grep -O`` opens the
#: matching files in a program named on the command line.
FORGE_REFUSED_OPTIONS: Dict[str, Tuple[str, ...]] = {
    "grep": ("-O", "--open-files-in-pager"),
}

#: The only settings a caller may add with ``-c``: a folder-ownership
#: exception, plain file names in output, and the repair branch's fallback
#: commit identity. Anything else could switch a hook or a filter back on.
CALLER_SETTINGS: Tuple[str, ...] = (
    "safe.directory",
    "core.quotepath",
    "user.name",
    "user.email",
)

# ---------------------------------------------------------------------------
# Fixed sentences

NETWORK_REFUSAL = "The factory does not run git commands that reach another repository."
SUBMODULE_REFUSAL = (
    "This project contains another git repository inside it (a submodule or a "
    "nested repository). The factory does not build projects like that yet, so "
    "it stopped before running git {command}."
)
REBASE_REFUSAL = (
    "The factory runs git rebase only with the options it uses itself "
    "(--autostash, --abort), because other rebase options can run programs "
    "from the to-do list."
)
WORKTREE_REFUSAL = (
    "git worktree add could not finish filling the new worktree, so the "
    "half-made worktree was removed."
)
SETTINGS_REFUSAL = (
    "git {command} was not run because the project's git settings could not be "
    "read, so nothing they name could be switched off."
)
UNSAFE_NAME_REFUSAL = (
    "git {command} was not run because the project's git settings use a name "
    "the factory cannot switch off."
)
PAGER_REFUSAL = (
    "The factory does not run git grep with --open-files-in-pager, because it "
    "starts a program."
)

#: The exit code a refusal reports (git's own code for a fatal error).
REFUSED_EXIT_CODE = 128

#: How long a name read or a refusal check may take when the caller set no
#: time limit of its own.
_READ_TIMEOUT_S = 60.0


class GitRefused(OSError):
    """A git command the helper would not start. ``str()`` is the sentence.

    An ``OSError``, so a caller that already treats "git could not be run" as
    the command's failure treats a refusal the same way.
    """

    def __init__(self, sentence: str) -> None:
        super().__init__(sentence)
        self.sentence = sentence

    def __str__(self) -> str:
        return self.sentence


def failure_sentence(command: str, returncode: Any) -> str:
    """What a failed command's ``stderr`` says instead of git's own text."""
    return f"git {command} stopped with exit code {returncode}."


def timeout_sentence(command: str, timeout: Any) -> str:
    """What a timed-out command's ``stderr`` says."""
    try:
        shown = f"{float(timeout):g}"
    except (TypeError, ValueError):
        shown = str(timeout)
    return f"git {command} did not finish within {shown} seconds."


# ---------------------------------------------------------------------------
# File names


class PatternPathspec(str):
    """A pathspec the factory wrote as a PATTERN (``:(exclude,glob)**/x/**``).

    A command carrying one runs without ``--literal-pathspecs``. Only Forge's
    own constants are ever marked this way; a project's file name never is.
    """


def pattern_pathspec(text: str) -> PatternPathspec:
    return PatternPathspec(text)


# ---------------------------------------------------------------------------
# The git program and the environment


_git_program_cache: List[str] = []


def git_program() -> str:
    """The full path of the git program, found once from Forge's own PATH."""
    if not _git_program_cache:
        found = shutil.which("git")
        if not found:
            raise FileNotFoundError(2, "git is not installed on this machine", "git")
        _git_program_cache.append(os.path.realpath(found) if os.path.islink(found) else found)
    return _git_program_cache[0]


def _child_environment(extra: Mapping[str, str]) -> Dict[str, str]:
    """The environment git gets: fixed values, a few kept names, the caller's."""
    unknown = sorted(set(extra) - set(CALLER_ENVIRONMENT_NAMES))
    if unknown:
        raise ValueError(f"run_git does not pass these environment names: {unknown}")
    folder = os.path.dirname(git_program())
    env: Dict[str, str] = {"PATH": f"{folder}:/usr/bin:/bin"}
    for name in KEPT_ENVIRONMENT_NAMES:
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    env.update(FIXED_ENVIRONMENT)
    env.update({k: str(v) for k, v in extra.items()})
    return env


# ---------------------------------------------------------------------------
# Splitting a command line


@dataclass
class _Command:
    dirs: List[str]
    settings: List[str]
    rest: List[str]
    literal: Optional[bool]

    @property
    def name(self) -> str:
        return self.rest[0]

    @property
    def label(self) -> str:
        """The command for a sentence: ``status``, ``worktree add``, ``stash push``."""
        if (
            self.name in ("worktree", "remote", "stash", "submodule")
            and len(self.rest) > 1
            and re.fullmatch(r"[a-z][a-z-]*", self.rest[1])
        ):
            return f"{self.name} {self.rest[1]}"
        return self.name


def _caller_setting_allowed(setting: str) -> bool:
    key = setting.partition("=")[0].strip().lower()
    return key in CALLER_SETTINGS


def _split(args: Sequence[Any]) -> _Command:
    items = [os.fspath(a) if isinstance(a, os.PathLike) else a for a in args]
    if items and items[0] == "git":
        items = items[1:]
    dirs: List[str] = []
    settings: List[str] = []
    literal: Optional[bool] = None
    index = 0
    while index < len(items) and str(items[index]).startswith("-"):
        option = items[index]
        if option in ("-C", "-c"):
            if index + 1 >= len(items):
                raise ValueError(f"run_git: {option} needs a value")
            value = str(items[index + 1])
            if option == "-c" and not _caller_setting_allowed(value):
                # Forge's caller allow-list: a caller cannot switch a hook,
                # a filter or a program back on.
                raise ValueError(
                    "run_git takes only these settings from a caller: "
                    + ", ".join(CALLER_SETTINGS)
                )
            (dirs if option == "-C" else settings).append(value)
            index += 2
        elif option == "--literal-pathspecs":
            literal = True
            index += 1
        elif option == "--no-pager":
            index += 1
        else:
            raise ValueError(f"run_git does not take the git option {option!r}")
    rest = [str(a) if not isinstance(a, str) else a for a in items[index:]]
    if not rest:
        raise ValueError("run_git needs a git command")
    return _Command(dirs, settings, rest, literal)


def _base_argv(dirs: Sequence[str], settings: Sequence[str], literal: bool) -> List[str]:
    argv = [git_program(), "--no-pager"]
    if literal:
        argv.append("--literal-pathspecs")
    for folder in dirs:
        argv += ["-C", folder]
    for setting in list(settings) + list(FIXED_SETTINGS):
        argv += ["-c", setting]
    return argv


def _with_command_options(rest: List[str]) -> List[str]:
    name = rest[0]
    added = COMMAND_OPTIONS.get(name) or FORGE_COMMAND_OPTIONS.get(name)
    if not added:
        return list(rest)
    if name in ("merge", "rebase") and any(a in _ACTION_OPTIONS for a in rest[1:]):
        return list(rest)
    return [name, *added, *rest[1:]]


# ---------------------------------------------------------------------------
# The helper's own reads (never through ``subprocess.run``)


class _ReadTimedOut(Exception):
    pass


def _spawn(
    argv: Sequence[str], cwd: Optional[str], env: Mapping[str, str], timeout: float
) -> Optional[Tuple[int, bytes]]:
    """Run one of the helper's own reads. ``None`` when it could not start.

    Built on ``Popen`` so the helper's reads never take a place in a test's
    scripted ``subprocess.run``.
    """
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            list(argv),
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None
    try:
        out, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise _ReadTimedOut()
    return process.returncode, out or b""


def _read_setting_names(
    prefix: Sequence[str], cwd: Optional[str], timeout: float
) -> Optional[List[str]]:
    """Every setting name git sees in this place (includes followed).

    Names are decoded with the file-system encoding's ``surrogateescape``, so
    a name that is not valid UTF-8 comes back byte for byte when it is put on
    git's command line. ``None`` when the read did not succeed: it could not
    start, exited non-zero, or listed none of the repository's own settings
    although a repository is there. The caller then runs nothing. Raises
    :class:`_ReadTimedOut` on a timeout.
    """
    env = _child_environment({})
    done = _spawn([*prefix, "config", "--list", "--name-only", "-z", "--show-scope"],
                  cwd, env, timeout)
    if done is None or done[0] != 0:
        return None
    fields = done[1].split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    if len(fields) % 2:
        return None
    scopes = [os.fsdecode(f) for f in fields[0::2]]
    names = [os.fsdecode(f) for f in fields[1::2]]
    if not any(scope in ("local", "worktree") for scope in scopes):
        # None of a repository's own settings were listed. Outside a
        # repository that is expected; inside one, the read missed them.
        inside = _spawn([*prefix, "rev-parse", "--git-dir"], cwd, env, timeout)
        if inside is None or inside[0] == 0:
            return None
    return names


def _switch_offs(names: Sequence[str]) -> Optional[List[str]]:
    """Settings that switch off every named filter, custom merge program and
    branch merge option. ``None`` when a name cannot be given on git's command
    line (a sub-section name with ``=`` in it)."""
    out: List[str] = []
    seen = set()
    for name in names:
        parts = name.split(".")
        if len(parts) < 3:
            continue
        section, sub, key = parts[0].lower(), ".".join(parts[1:-1]), parts[-1].lower()
        if section not in ("filter", "merge", "branch"):
            continue
        if section == "branch" and key != "mergeoptions":
            continue
        if "=" in sub:
            return None
        if (section, sub) in seen:
            continue
        seen.add((section, sub))
        if section == "filter":
            out += [f"filter.{sub}.clean=", f"filter.{sub}.smudge=",
                    f"filter.{sub}.process=", f"filter.{sub}.required=false"]
        elif section == "merge":
            out.append(f"merge.{sub}.driver={MERGE_PROGRAM}")
        else:
            out.append(f"branch.{sub}.mergeOptions=")
    return out


def _add_pathspecs(rest: Sequence[str]) -> Tuple[List[str], bool]:
    """The pathspecs a ``git add`` would take, and whether it takes untracked
    files at all (``-u`` does not)."""
    specs: List[str] = []
    takes_untracked = True
    after_dashes = False
    for arg in rest[1:]:
        if after_dashes:
            specs.append(arg)
        elif arg == "--":
            after_dashes = True
        elif arg.startswith("-"):
            if arg in ("-u", "--update"):
                takes_untracked = False
        else:
            specs.append(arg)
    return specs, takes_untracked


def _nested_repositories(
    command: _Command, env_extra: Mapping[str, str], cwd: Optional[str],
    literal: bool, timeout: float,
) -> Optional[List[str]]:
    """Submodules in the index, and (before ``add``) nested repositories the
    add would take in. ``None`` when git could not say; the caller then runs
    nothing."""
    env = _child_environment(env_extra)
    prefix = _base_argv(command.dirs, command.settings, literal=False)
    done = _spawn([*prefix, "ls-files", "-s", "-z"], cwd, env, timeout)
    if done is None or done[0] != 0:
        return None
    found: List[str] = []
    for record in done[1].split(b"\0"):
        if record.startswith(b"160000 "):
            found.append(os.fsdecode(record.partition(b"\t")[2]))
    if command.name == "add" and not found:
        specs, takes_untracked = _add_pathspecs(command.rest)
        if takes_untracked:
            listing_literal = literal
            if not specs:
                specs, listing_literal = [":/"], False
            prefix = _base_argv(command.dirs, command.settings, literal=listing_literal)
            done = _spawn(
                [*prefix, "ls-files", "--others", "--exclude-standard", "-z", "--", *specs],
                cwd, env, timeout,
            )
            if done is None or done[0] != 0:
                return None
            found += [os.fsdecode(p) for p in done[1].split(b"\0") if p.endswith(b"/")]
    return found


# ---------------------------------------------------------------------------
# Preparing one command


@dataclass
class PreparedGit:
    """One git command, ready to run: the full argv and environment, or the
    sentence it was refused with."""

    argv: Optional[List[str]]
    env: Dict[str, str]
    label: str
    refusal: Optional[str] = None


#: ``git grep`` options that take the next argument as their value.
_GREP_VALUE_OPTIONS = frozenset(
    {"-e", "-f", "-A", "-B", "-C", "-m", "--max-count", "--max-depth",
     "--threads", "--after-context", "--before-context", "--context"}
)


def _opens_a_pager(rest: Sequence[str]) -> bool:
    """True when a ``git grep`` would start the program ``-O`` names."""
    skip = False
    for arg in rest[1:]:
        if skip:
            skip = False
            continue
        if arg == "--":
            return False
        if arg in _GREP_VALUE_OPTIONS:
            skip = True
            continue
        if arg.startswith("--"):
            # git accepts any unique prefix of a long option.
            if len(arg) > 3 and "--open-files-in-pager".startswith(arg.split("=", 1)[0]):
                return True
            continue
        if arg.startswith("-") and len(arg) > 1:
            # A bundle of short options: ``-nO`` is ``-n -O``. A value-taking
            # option inside the bundle swallows the rest of it.
            for index, letter in enumerate(arg[1:]):
                if letter == "O":
                    return True
                if f"-{letter}" in _GREP_VALUE_OPTIONS:
                    if index == len(arg) - 2:
                        skip = True
                    break
    return False


def _refusal_for(command: _Command) -> Optional[str]:
    name = command.name
    rest = command.rest
    if name in NETWORK_COMMANDS:
        return NETWORK_REFUSAL
    if name == "remote" and len(rest) > 1 and rest[1] in NETWORK_REMOTE_COMMANDS:
        return NETWORK_REFUSAL
    if name == "archive" and any(a == "--remote" or a.startswith("--remote=") for a in rest[1:]):
        return NETWORK_REFUSAL
    if name == "rebase":
        for arg in rest[1:]:
            if arg == "--":
                break
            # Only the options the factory itself uses: git accepts any unique
            # prefix of a long option (``--exe=``, ``--interac``), so a
            # refusal list could never be complete.
            if arg.startswith("-") and arg not in REBASE_OPTIONS:
                return REBASE_REFUSAL
    if name == "grep" and _opens_a_pager(rest):
        return PAGER_REFUSAL
    return None


def prepare_git(
    args: Sequence[Any],
    *,
    cwd: Optional[Union[str, Path]] = None,
    env: Optional[Mapping[str, str]] = None,
    literal_pathspecs: Optional[bool] = None,
    timeout: Optional[float] = None,
) -> PreparedGit:
    """Build one protected git command without running it.

    ``worktree add`` is not supported here: use :func:`run_git`, which adds a
    worktree in two steps.
    """
    command = _split(args)
    env_extra = dict(env or {})
    child_env = _child_environment(env_extra)
    label = command.label
    if command.name == "worktree" and len(command.rest) > 1 and command.rest[1] == "add":
        raise ValueError("prepare_git does not add worktrees; use run_git")
    refusal = _refusal_for(command)
    if refusal:
        return PreparedGit(None, child_env, label, refusal)
    if literal_pathspecs is None:
        literal_pathspecs = command.literal if command.literal is not None else True
        if any(isinstance(a, PatternPathspec) for a in command.rest):
            literal_pathspecs = False
    if command.name == "check-ignore":
        literal_pathspecs = False  # git refuses the option, and takes plain paths anyway
    workdir = os.fspath(cwd) if cwd is not None else None
    read_timeout = min(float(timeout), _READ_TIMEOUT_S) if timeout else _READ_TIMEOUT_S
    try:
        if command.name in WRITE_COMMANDS:
            nested = _nested_repositories(
                command, env_extra, workdir, literal_pathspecs, read_timeout
            )
            if nested is None:
                return PreparedGit(
                    None, child_env, label, SETTINGS_REFUSAL.format(command=label)
                )
            if nested:
                # Named only here, in the local log; never in the sentence.
                logger.warning(
                    "git %s refused: another git repository inside the project at %s",
                    label, ", ".join(repr(n) for n in nested[:5]),
                )
                return PreparedGit(
                    None, child_env, label, SUBMODULE_REFUSAL.format(command=label)
                )
        names = _read_setting_names(
            _base_argv(command.dirs, command.settings, literal=False), workdir, read_timeout
        )
    except _ReadTimedOut:
        return PreparedGit(None, child_env, label, SETTINGS_REFUSAL.format(command=label))
    if names is None:
        # Fail closed: without the names nothing can be switched off.
        return PreparedGit(None, child_env, label, SETTINGS_REFUSAL.format(command=label))
    switch_offs = _switch_offs(names)
    if switch_offs is None:
        return PreparedGit(None, child_env, label, UNSAFE_NAME_REFUSAL.format(command=label))
    argv = _base_argv(command.dirs, command.settings, literal_pathspecs)
    for setting in switch_offs:
        argv += ["-c", setting]
    argv += [str(a) for a in _with_command_options(command.rest)]
    return PreparedGit(argv, child_env, label)


# ---------------------------------------------------------------------------
# Running one command


def _finish(
    label: str, argv_shown: List[str], returncode: Any, stdout: Any, text_mode: bool,
    check: bool,
) -> subprocess.CompletedProcess:
    if returncode == 0:
        stderr: Union[str, bytes] = ""
    else:
        stderr = failure_sentence(label, returncode)
    if not text_mode:
        stderr = stderr.encode()
    if check and returncode != 0:
        raise subprocess.CalledProcessError(returncode, f"git {label}", stdout, stderr)
    return subprocess.CompletedProcess(argv_shown, returncode, stdout, stderr)


def _refused(
    label: str, argv_shown: List[str], sentence: str, text_mode: bool, check: bool
) -> subprocess.CompletedProcess:
    stdout: Union[str, bytes] = "" if text_mode else b""
    stderr: Union[str, bytes] = sentence if text_mode else sentence.encode()
    if check:
        raise subprocess.CalledProcessError(
            REFUSED_EXIT_CODE, f"git {label}", stdout, stderr
        )
    return subprocess.CompletedProcess(argv_shown, REFUSED_EXIT_CODE, stdout, stderr)


def run_git(
    args: Sequence[Any],
    *,
    cwd: Optional[Union[str, Path]] = None,
    check: bool = False,
    timeout: Optional[float] = None,
    input: Optional[Union[str, bytes]] = None,
    text: bool = True,
    encoding: Optional[str] = None,
    errors: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    literal_pathspecs: Optional[bool] = None,
) -> subprocess.CompletedProcess:
    """Run one git command, under the rules in this module.

    ``args`` is the command without ``git`` (a leading ``"git"`` is accepted),
    optionally starting with ``-C <folder>`` and ``-c <setting>`` (only the
    settings in :data:`CALLER_SETTINGS`). Returns the ``CompletedProcess``
    shape callers already use; ``stderr`` is empty on success and a fixed
    sentence otherwise. ``check=True`` raises ``CalledProcessError`` carrying
    the same sentence; a timeout raises ``TimeoutExpired`` whose text names
    only the command. ``env`` may carry only :data:`CALLER_ENVIRONMENT_NAMES`.
    """
    text_mode = bool(text or encoding or errors)
    command = _split(args)
    shown = ["git", *[str(a) for a in command.rest]]
    if command.name == "worktree" and len(command.rest) > 1 and command.rest[1] == "add":
        return _add_worktree(
            command, cwd=cwd, check=check, timeout=timeout, text_mode=text_mode,
            encoding=encoding, errors=errors, env=env, shown=shown,
        )
    prepared = prepare_git(
        args, cwd=cwd, env=env, literal_pathspecs=literal_pathspecs, timeout=timeout
    )
    if prepared.refusal:
        return _refused(prepared.label, shown, prepared.refusal, text_mode, check)
    return _launch(
        prepared, shown, cwd=cwd, check=check, timeout=timeout, input=input,
        text_mode=text_mode, encoding=encoding, errors=errors,
    )


async def run_git_async(args: Sequence[Any], **kwargs: Any) -> subprocess.CompletedProcess:
    """:func:`run_git` off the event loop, for Forge's ``asyncio`` callers.

    The same command line, environment and sentences: it runs
    :func:`run_git` in a worker thread. Give a ``timeout`` to bound it; a
    cancelled caller stops waiting, and git finishes or meets its timeout.
    """
    return await asyncio.to_thread(run_git, args, **kwargs)


def open_git(
    args: Sequence[Any],
    *,
    cwd: Optional[Union[str, Path]] = None,
    env: Optional[Mapping[str, str]] = None,
    stdin: Any = None,
    literal_pathspecs: Optional[bool] = None,
    timeout: Optional[float] = None,
) -> "subprocess.Popen[bytes]":
    """Start one git command whose output the caller streams.

    For the two streaming readers (``archive`` into ``tarfile``, and the
    long-lived ``cat-file --batch``). The command line and environment are
    :func:`run_git`'s. stdout is a pipe of bytes; stdin is ``DEVNULL`` unless
    the caller gives ``subprocess.PIPE``; git's stderr is discarded, so a
    caller that sees a non-zero exit reports :func:`failure_sentence`.
    ``timeout`` bounds only the helper's own setting-name read. Raises
    :class:`GitRefused` (an ``OSError``) with the fixed sentence instead of
    starting a command the rules refuse.
    """
    prepared = prepare_git(
        args, cwd=cwd, env=env, literal_pathspecs=literal_pathspecs, timeout=timeout
    )
    if prepared.refusal or prepared.argv is None:
        raise GitRefused(prepared.refusal or SETTINGS_REFUSAL.format(command=prepared.label))
    return subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        prepared.argv,
        cwd=os.fspath(cwd) if cwd is not None else None,
        env=prepared.env,
        stdin=subprocess.DEVNULL if stdin is None else stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )


def _launch(
    prepared: PreparedGit, shown: List[str], *, cwd, check, timeout, input,
    text_mode, encoding, errors,
) -> subprocess.CompletedProcess:
    kwargs: Dict[str, Any] = {
        "cwd": os.fspath(cwd) if cwd is not None else None,
        "env": prepared.env,
        "capture_output": True,
        "timeout": timeout,
    }
    if text_mode:
        kwargs["text"] = True
        if encoding:
            kwargs["encoding"] = encoding
        if errors:
            kwargs["errors"] = errors
    if input is not None:
        kwargs["input"] = input
    else:
        kwargs["stdin"] = subprocess.DEVNULL
    label = prepared.label
    try:
        done = subprocess.run(prepared.argv, **kwargs)  # noqa: S603 - fixed argv
    except subprocess.TimeoutExpired:
        sentence = timeout_sentence(label, timeout)
        raise subprocess.TimeoutExpired(
            f"git {label}", timeout if timeout is not None else 0, output=None,
            stderr=sentence if text_mode else sentence.encode(),
        ) from None
    except subprocess.CalledProcessError as exc:  # only from a scripted stand-in
        return _finish(label, shown, exc.returncode, exc.output, text_mode, check)
    return _finish(label, shown, done.returncode, done.stdout, text_mode, check)


def _worktree_add_destination(rest: Sequence[str]) -> Optional[str]:
    """The ``<path>`` of ``worktree add [options] <path> [<commit-ish>]``."""
    takes_value = {"-b", "-B", "--reason"}
    index = 2
    after_dashes = False
    while index < len(rest):
        arg = str(rest[index])
        if not after_dashes and arg == "--":
            after_dashes = True
        elif not after_dashes and arg in takes_value:
            index += 1
        elif not after_dashes and arg.startswith("-"):
            pass
        else:
            return arg
        index += 1
    return None


def _add_worktree(
    command: _Command, *, cwd, check, timeout, text_mode, encoding, errors, env, shown,
) -> subprocess.CompletedProcess:
    """``worktree add`` in two steps: never let git check the new worktree out.

    1. ``worktree add --no-checkout`` with the caller's own arguments (its
       ``--lock``, ``-b`` and ``--detach`` kept);
    2. the setting names read inside the new worktree, where an include can
       name its git folder (``gitdir:``) or its new branch (``onbranch:``);
    3. ``reset --hard HEAD`` there, with those names switched off.

    If step 2 or 3 fails, the half-made worktree is removed.
    """
    rest = list(command.rest)
    destination = _worktree_add_destination(rest)
    caller_said_no_checkout = "--no-checkout" in rest
    rest = [a for a in rest if a != "--checkout"]
    if not caller_said_no_checkout:
        rest = [rest[0], rest[1], "--no-checkout", *rest[2:]]
    first_args: List[str] = []
    for folder in command.dirs:
        first_args += ["-C", folder]
    for setting in command.settings:
        first_args += ["-c", setting]
    # The source repository's index is checked like any other write.
    source = _Command(command.dirs, command.settings, ["checkout"], None)
    workdir = os.fspath(cwd) if cwd is not None else None
    read_timeout = min(float(timeout), _READ_TIMEOUT_S) if timeout else _READ_TIMEOUT_S
    try:
        nested = _nested_repositories(source, dict(env or {}), workdir, True, read_timeout)
        names = (
            _read_setting_names(
                _base_argv(command.dirs, command.settings, literal=False), workdir,
                read_timeout,
            )
            if nested == []
            else None
        )
    except _ReadTimedOut:
        nested, names = None, None
    if nested:
        logger.warning("git worktree add refused: the project holds a submodule")
        return _refused("worktree add", shown,
                        SUBMODULE_REFUSAL.format(command="worktree add"), text_mode, check)
    if names is None:
        return _refused("worktree add", shown,
                        SETTINGS_REFUSAL.format(command="worktree add"), text_mode, check)
    switch_offs = _switch_offs(names)
    if switch_offs is None:
        return _refused("worktree add", shown,
                        UNSAFE_NAME_REFUSAL.format(command="worktree add"), text_mode, check)
    argv = _base_argv(command.dirs, command.settings, literal=True)
    for setting in switch_offs:
        argv += ["-c", setting]
    argv += [str(a) for a in rest]
    prepared = PreparedGit(argv, _child_environment(dict(env or {})), "worktree add")
    added = _launch(prepared, shown, cwd=cwd, check=False, timeout=timeout, input=None,
                    text_mode=text_mode, encoding=encoding, errors=errors)
    if added.returncode != 0 or caller_said_no_checkout or destination is None:
        if check and added.returncode != 0:
            raise subprocess.CalledProcessError(
                added.returncode, "git worktree add", added.stdout, added.stderr
            )
        return added
    base = Path(workdir) if workdir is not None else Path.cwd()
    for folder in command.dirs:
        base = base / folder
    target = str(base / destination)
    filled = None
    try:
        filled = _fill_new_worktree(target, settings=command.settings, env=env,
                                    timeout=timeout)
    finally:
        if filled is None or filled.returncode != 0:
            removal = prepare_git([*first_args, "worktree", "remove", "--force", target],
                                  cwd=cwd, timeout=timeout)
            if removal.argv:
                _spawn(removal.argv, workdir, removal.env,
                       float(timeout) if timeout else _READ_TIMEOUT_S)
    if filled is None or filled.returncode != 0:
        return _refused("worktree add", shown, WORKTREE_REFUSAL, text_mode, check)
    return added


def _fill_new_worktree(
    target: str, *, settings: Sequence[str], env: Optional[Mapping[str, str]],
    timeout: Optional[float],
) -> Optional[subprocess.CompletedProcess]:
    """Steps 2 and 3 of :func:`_add_worktree`. ``None`` when refused."""
    caller: List[str] = []
    for setting in settings:
        caller += ["-c", setting]
    prepared = prepare_git([*caller, "-C", target, "reset", "--hard", "-q", "HEAD"], env=env,
                           timeout=timeout)
    if prepared.refusal or prepared.argv is None:
        return None
    try:
        return _launch(prepared, ["git", "reset", "--hard", "-q", "HEAD"], cwd=None,
                       check=False, timeout=timeout, input=None, text_mode=True,
                       encoding=None, errors=None)
    except subprocess.TimeoutExpired:
        return None


# ---------------------------------------------------------------------------
# The one network-shaped command run under these rules: forge.network_git's
# local-only step, which brings commits in from the factory's own copy


def git_exec_folder() -> str:
    """Git's own exec folder (where ``git-upload-pack`` lives)."""
    if not _exec_folder_cache:
        done = _spawn([*_base_argv([], [], literal=False), "--exec-path"], None,
                      _child_environment({}), _READ_TIMEOUT_S)
        folder = done[1].decode("utf-8", "replace").strip() if done and done[0] == 0 else ""
        _exec_folder_cache.append(folder or os.path.dirname(git_program()))
    return _exec_folder_cache[0]


_exec_folder_cache: List[str] = []


def run_from_local_copy(
    clone: Union[str, Path], rest: Sequence[str], *, timeout: Optional[float] = None,
) -> subprocess.CompletedProcess:
    """Run ``rest`` in ``clone`` under every rule here, with only the ``file``
    transport allowed instead of none.

    For :func:`forge.network_git.bring_in_from_copy` alone: the one command
    that reads another repository through this module, and only a local
    folder the factory owns. The network refusal in :func:`run_git` is not
    applied, because ``rest`` is that module's fixed ``fetch`` from a local
    folder; everything else is: the fixed settings, the names read just
    before and switched off, the fixed environment and the fixed sentence.
    """
    dirs = [os.fspath(clone)]
    items = [str(a) for a in rest]
    label = items[0] if items else "git"
    shown = ["git", *items]
    read_timeout = min(float(timeout), _READ_TIMEOUT_S) if timeout else _READ_TIMEOUT_S
    try:
        names = _read_setting_names(_base_argv(dirs, [], literal=False), None, read_timeout)
    except _ReadTimedOut:
        names = None
    if names is None:
        return _refused(label, shown, SETTINGS_REFUSAL.format(command=label), True, False)
    switch_offs = _switch_offs(names)
    if switch_offs is None:
        return _refused(label, shown, UNSAFE_NAME_REFUSAL.format(command=label), True, False)
    argv = _base_argv(dirs, [], literal=False)
    for setting in switch_offs:
        argv += ["-c", setting]
    argv += items
    env = _child_environment({})
    env["GIT_ALLOW_PROTOCOL"] = "file"
    prepared = PreparedGit(argv, env, label)
    return _launch(prepared, shown, cwd=None, check=False, timeout=timeout, input=None,
                   text_mode=True, encoding=None, errors="replace")
