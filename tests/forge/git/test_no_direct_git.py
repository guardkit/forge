"""Every git command Forge runs goes through its two helpers.

Reads the source of ``src/forge/`` and fails on:

* a ``subprocess``, ``asyncio`` subprocess or ``os`` process call whose
  command is git, anywhere other than ``forge/project_git.py`` and
  ``forge/network_git.py``. A command counts as git when it is a literal list
  or tuple starting with ``"git"``, a ``["git"] + args`` sum, a name assigned
  one of those, a string starting with ``"git "``, or (for
  ``create_subprocess_exec`` and the ``os.exec*``/``os.spawn*`` family) the
  program ``"git"`` given as the first argument;
* a sandbox command whose program is git (``sandbox.exec("git", ...)``,
  ``sbx exec ... git``) built outside the helpers;
* a git command that reaches another repository (``fetch``, ``pull``,
  ``push``, ``clone``, ``ls-remote``, ``archive --remote`` and the rest),
  written as a ``["git", <network command>]`` literal, handed to a sandbox's
  ``exec``, or handed to any git runner other than
  :mod:`forge.network_git`'s own (``run_network_git``,
  ``sandbox_git_command``, ``sandbox_network_git``), which applies the
  isolated network policy;
* a production executor that does not hand its command to the helper;
* any ``git `` command in ``cli/deploy_templates/*.sh``.

There are no named exceptions. The publisher still writes its own fetch and
send argument lists (its one non-forcing send, ``the_send_argv``, is pinned
by its own tests), but they run only through ``network_git.run_network_git``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[3]
PACKAGE = ROOT / "src" / "forge"
HELPERS = (Path("project_git.py"), Path("network_git.py"))
TEMPLATES = PACKAGE / "cli" / "deploy_templates"

NETWORK = {"fetch", "pull", "push", "clone", "ls-remote", "fetch-pack", "send-pack",
           "credential", "submodule"}
NETWORK_REMOTE = {"update", "show", "prune", "set-head"}

#: The runners that apply the isolated network policy.
NETWORK_BOUNDARY = {"run_network_git", "sandbox_git_command", "sandbox_network_git"}

PROCESS_CALLS = {
    ("subprocess", "run"), ("subprocess", "Popen"), ("subprocess", "call"),
    ("subprocess", "check_call"), ("subprocess", "check_output"),
    ("subprocess", "getoutput"), ("subprocess", "getstatusoutput"),
    ("asyncio", "create_subprocess_exec"), ("asyncio", "create_subprocess_shell"),
    ("os", "system"), ("os", "popen"), ("os", "execv"), ("os", "execvp"),
    ("os", "execvpe"), ("os", "execl"), ("os", "execlp"), ("os", "spawnv"),
    ("os", "spawnvp"), ("os", "posix_spawn"), ("os", "posix_spawnp"),
}

#: Process calls whose FIRST argument is the program, not a command list.
PROGRAM_FIRST = {"create_subprocess_exec", "execl", "execlp", "execv", "execvp", "execvpe",
                 "spawnv", "spawnvp", "posix_spawn", "posix_spawnp"}

#: Production executors, and what each must hand its command to.
EXECUTORS: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    ("adapters/git/operations.py", "_default_execute", ("run_git", "run_git_async")),
    ("subagents/autobuild_runner.py", "_run_git", ("run_git", "run_git_async")),
    ("subagents/autobuild_worktree_lifecycle.py", "_git", ("run_git",)),
    ("deploy/review_preview.py", "_run_command", ("run_git",)),
    ("deploy/candidate_tree.py", "_run_git", ("run_git",)),
    ("deploy/candidate_tree.py", "_git", ("run_git",)),
    ("publisher/git_work.py", "TheProjectsCommits._git", ("run_network_git",)),
    ("pipeline/repair_branch.py", "_git", ("run_git",)),
    ("pipeline/branch_scope.py", "_git", ("run_git",)),
)


def _python_files() -> Iterator[Path]:
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE)
        if "__pycache__" in rel.parts:
            continue
        yield rel


def _const(node: ast.AST) -> Optional[str]:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _list_items(node: ast.AST) -> Optional[List[ast.AST]]:
    if isinstance(node, (ast.List, ast.Tuple)):
        return list(node.elts)
    return None


def _callee_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


class _Scanner(ast.NodeVisitor):
    def __init__(self) -> None:
        self.process_aliases: Dict[str, Tuple[str, str]] = {}
        self.module_aliases: Dict[str, str] = {}
        self.assigned: Dict[str, ast.AST] = {}
        self.git_collections: set = set()
        self.direct: List[int] = []
        self.network: List[int] = []
        self.sandbox: List[int] = []

    # -- what names mean --------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.module_aliases[alias.asname or alias.name] = alias.name
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            key = (node.module or "", alias.name)
            if key in PROCESS_CALLS:
                self.process_aliases[alias.asname or alias.name] = key
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if isinstance(target, ast.Name):
                self.assigned[target.id] = node.value
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name) and node.value is not None:
            self.assigned[node.target.id] = node.value
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        source = node.iter
        holds_git = isinstance(source, ast.Name) and source.id in self.git_collections
        items = _list_items(source)
        if items and any(self._is_git_command(i) for i in items):
            holds_git = True
        if holds_git and isinstance(node.target, ast.Name):
            self.assigned[node.target.id] = ast.List(elts=[ast.Constant("git")])
        self.generic_visit(node)

    # -- is this a git command? -------------------------------------------

    def _is_git_word(self, node: ast.AST) -> bool:
        value = _const(node)
        if value is None and isinstance(node, ast.Name):
            bound = self.assigned.get(node.id)
            value = _const(bound) if bound is not None else None
        return value is not None and (value == "git" or value.endswith("/git"))

    def _is_git_command(self, node: ast.AST, depth: int = 0) -> bool:
        if depth > 5:
            return False
        if isinstance(node, ast.Starred):
            return self._is_git_command(node.value, depth + 1)
        items = _list_items(node)
        if items:
            return self._is_git_word(items[0])
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return self._is_git_command(node.left, depth + 1)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in (
            "list", "tuple"
        ) and node.args:
            return self._is_git_command(node.args[0], depth + 1)
        if isinstance(node, ast.Name) and node.id in self.assigned:
            return self._is_git_command(self.assigned[node.id], depth + 1)
        text = _const(node)
        if text is not None:
            return text.startswith("git ")
        if isinstance(node, ast.JoinedStr) and node.values:
            first = _const(node.values[0])
            return first is not None and first.startswith("git ")
        return False

    def _process_call(self, node: ast.Call) -> Optional[str]:
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            module = self.module_aliases.get(func.value.id, func.value.id)
            if (module, func.attr) in PROCESS_CALLS:
                return func.attr
        if isinstance(func, ast.Attribute) and func.attr in ("create_subprocess_exec",
                                                              "create_subprocess_shell"):
            return func.attr
        if isinstance(func, ast.Name) and func.id in self.process_aliases:
            return self.process_aliases[func.id][1]
        return None

    def _network_words(self, items: List[ast.AST]) -> bool:
        words = [_const(i) for i in items[:2]]
        if not words or words[0] is None:
            return False
        if words[0] in NETWORK:
            return True
        if words[0] == "remote" and len(words) > 1 and words[1] in NETWORK_REMOTE:
            return True
        if words[0] == "archive" and any(
            (_const(i) or "").startswith("--remote") for i in items[1:]
        ):
            return True
        return False

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = _callee_name(node)
        if (isinstance(func, ast.Attribute) and func.attr in ("append", "extend", "insert")
                and isinstance(func.value, ast.Name) and node.args
                and any(self._is_git_command(a) for a in node.args)):
            self.git_collections.add(func.value.id)
        kind = self._process_call(node)
        if kind:
            command = node.args[0] if node.args else next(
                (k.value for k in node.keywords if k.arg in ("args", "cmd", "command")), None
            )
            if command is not None and (
                self._is_git_command(command)
                or (kind in PROGRAM_FIRST and self._is_git_word(command))
            ):
                self.direct.append(node.lineno)
        # A sandbox (or any other) ``exec`` whose program is git.
        if name == "exec" and node.args and self._is_git_word(node.args[0]):
            self.sandbox.append(node.lineno)
            if self._network_words(list(node.args[1:3])):
                self.network.append(node.lineno)
        # A network command handed to a git runner that is not the boundary.
        if "git" in name.lower() and name not in NETWORK_BOUNDARY:
            for arg in node.args:
                items = _list_items(arg)
                if items and self._network_words(items):
                    self.network.append(node.lineno)
        self.generic_visit(node)

    def visit_List(self, node: ast.List) -> None:
        self._check_network_literal(node)
        self.generic_visit(node)

    def visit_Tuple(self, node: ast.Tuple) -> None:
        self._check_network_literal(node)
        self.generic_visit(node)

    def _check_network_literal(self, node: ast.AST) -> None:
        items = _list_items(node) or []
        if len(items) >= 2 and _const(items[0]) == "git":
            # ``git -C <dir> -c <setting> fetch``: skip the options and their values.
            words = [_const(i) for i in items[1:]]
            index = 0
            while index < len(words) and (words[index] or "").startswith("-"):
                index += 2 if words[index] in ("-C", "-c", "--git-dir") else 1
            if self._network_words(list(items[1 + index:1 + index + 2])):
                self.network.append(node.lineno)


def _scan_source(source: str, filename: str = "<sample>") -> _Scanner:
    scanner = _Scanner()
    scanner.visit(ast.parse(source, filename=filename))
    return scanner


def _scan(rel: Path) -> _Scanner:
    return _scan_source((PACKAGE / rel).read_text(encoding="utf-8"), str(rel))


def test_no_direct_git_process_calls() -> None:
    found = []
    for rel in _python_files():
        if rel in HELPERS:
            continue
        scanner = _scan(rel)
        for line in scanner.direct + scanner.sandbox:
            found.append(f"{rel}:{line}")
    assert found == [], (
        "git is run directly instead of through forge.project_git or "
        "forge.network_git: " + ", ".join(found)
    )


def test_no_network_git_commands_outside_the_network_boundary() -> None:
    found = []
    for rel in _python_files():
        if rel == Path("network_git.py"):
            continue
        for line in _scan(rel).network:
            found.append(f"{rel}:{line}")
    assert found == [], (
        "git commands that reach another repository outside forge.network_git: "
        + ", ".join(found)
    )


#: ``git`` in command position: at the start of a command, after a separator,
#: a pipe, ``$(``, a backtick, or a shell keyword.
_SHELL_GIT = re.compile(
    r"(?:^|[;&|(`]|\$\(|\b(?:then|do|else|exec|command)\b)\s*git(?:\s|$)"
)


def _shell_git_commands(line: str) -> bool:
    """True when a shell line runs git (quoted prose about git does not count;
    a ``$(git ...)`` inside double quotes does)."""
    code = line.split(" #", 1)[0] if not line.lstrip().startswith("#") else ""
    code = re.sub(r"'[^']*'", " ", code)
    inner = re.findall(r"\$\(([^)]*)\)", code)
    code = re.sub(r'"[^"]*"', " ", code)
    return any(_SHELL_GIT.search(part) for part in [code, *inner])


def test_no_git_in_the_deploy_templates() -> None:
    found = []
    for path in sorted(TEMPLATES.glob("*.sh")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _shell_git_commands(line):
                found.append(f"{path.name}:{number}")
    assert found == [], "git commands in the deploy templates: " + ", ".join(found)


def test_the_template_check_recognises_a_git_command() -> None:
    assert _shell_git_commands("git status")
    assert _shell_git_commands("  x=$(git rev-parse HEAD)")
    assert _shell_git_commands('echo "$(git log -1)"')
    assert _shell_git_commands("true && git fetch origin")
    assert not _shell_git_commands('refuse "the clone borrows git objects from x"')
    assert not _shell_git_commands("# git is not run here")
    assert not _shell_git_commands("git_settings_value() {")


def _function_source(rel: str, qualified: str) -> Optional[str]:
    source = (PACKAGE / rel).read_text(encoding="utf-8")
    tree = ast.parse(source)
    body = tree.body
    found = None
    for part in qualified.split("."):
        found = next(
            (n for n in body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                               ast.ClassDef)) and n.name == part),
            None,
        )
        if found is None:
            return None
        body = found.body
    return ast.get_source_segment(source, found)


def test_every_production_executor_hands_its_command_to_the_helper() -> None:
    missing = []
    for rel, qualified, helpers in EXECUTORS:
        body = _function_source(rel, qualified)
        if body is None or not any(f"{h}(" in body for h in helpers):
            missing.append(f"{rel}::{qualified}")
    assert missing == [], "executors that do not hand git to the helper: " + ", ".join(missing)


def test_the_source_check_recognises_each_shape() -> None:
    """The scanner itself: each recognised shape, and the ones it must not flag."""
    sample = '''
import subprocess, os, asyncio
from subprocess import run as sprun
GIT = "git"
CMD = ["git", "status"]
def f(args, sandbox, run_git, run_network_git):
    subprocess.run(["git", "status"])
    subprocess.run(["git"] + args)
    subprocess.run(CMD)
    os.system("git status")
    sprun([GIT, "diff"])
    asyncio.create_subprocess_exec(*["git", "log"])
    asyncio.create_subprocess_exec("git", "log")
    subprocess.run(["ls"])
    run_git(["fetch", "origin"])
    x = ["git", "push", "origin"]
    sandbox.exec("git", "ls-remote", "u")
    sandbox.exec("ls", "-1")
    run_network_git(["fetch", "--", "u"], transport="https")
    y = ["git", "-C", "here", "fetch", "origin"]
    cmds = []
    cmds.append(["git", "rev-parse", "HEAD"])
    for c in cmds:
        subprocess.run(c)
'''
    scanner = _scan_source(sample)
    assert scanner.direct == [7, 8, 9, 10, 11, 12, 13, 24]
    assert scanner.sandbox == [17]
    assert sorted(scanner.network) == [15, 16, 17, 20]
