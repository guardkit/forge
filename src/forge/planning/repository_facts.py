"""What the repository already does, for the words a request uses.

WHY (2026-09-13). The spec seat is blind to the repository by design — no
scan, no memory, no file reading — and its coach was shown only the draft. So
when a sentence asked for ``GET /users/created-per-day``, nothing could tell
the coach that ``/users/count-today`` and ``/users/count-by-domain`` already
existed beside it, took no token, and returned their data bare. The seat
assumed authentication "as common practice", the coder built it, and the
gate refused it.

This is the fact sheet the coach is shown (design
``planning-coach-design-2026-09-13.md`` §4a): for each route path the request
names, the sibling routes in the same file, whether each declares an
authentication dependency, and what shape it returns. Deterministic —
``git grep`` for the path's first segment, then Python's own ``ast`` for the
file it lands in. It says only what it can prove: a file that is not Python
is named with its routes and the sentence "whether those routes require
authentication was not read", never a guess.

The stack question, answered in one line (Rich's rule): on a non-Python
repository this returns the sibling route literals ``git grep`` finds and
says the authentication and shape facts were not read. It never claims
them.

Never raises: like the inventory and the spec-words finder beside it, this
must never be able to stop a planning run. ``None`` when there is nothing to
say, so the dispatch is byte for byte what it is today.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path
from typing import Any

__all__ = ["RouteFact", "routes_in_python_file", "what_the_repository_already_does"]

_ROUTE_PATH = re.compile(r"(?<![\w/])/(?:[A-Za-z0-9_{}-]+/)*[A-Za-z0-9_{}-]+")
_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "api_route", "route"})
_AUTH_NAME = re.compile(
    r"auth|token|current_user|require|verify|secur|permission|api_key|logged", re.IGNORECASE
)
_MAX_FILES = 3
_MAX_ROUTES_PER_FILE = 12


class RouteFact:
    """One route as the file declares it."""

    __slots__ = ("method", "path", "auth", "returns")

    def __init__(self, method: str, path: str, auth: str | None, returns: str) -> None:
        self.method = method
        self.path = path
        self.auth = auth  # the dependency's name when one guards the route
        self.returns = returns

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"RouteFact({self.method} {self.path}, auth={self.auth!r}, returns={self.returns!r})"


def _route_paths_in(request_text: str) -> list[str]:
    paths: list[str] = []
    for match in _ROUTE_PATH.findall(request_text or ""):
        if len(match) > 3 and re.search(r"[A-Za-z]", match) and match not in paths:
            paths.append(match)
    return paths


def _git_grep_files(repo_path: str, needle: str) -> list[str]:
    try:
        completed = subprocess.run(
            ["git", "-c", "safe.directory=*", "-C", str(repo_path), "grep", "-l", "-F", needle],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0:
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


#: Extensions that are documentation or configuration, never a route table.
#: A markdown file that MENTIONS a route is not a file that DEFINES one, and
#: putting it on the fact sheet is noise in front of the coach.
_NOT_SOURCE = (
    ".md", ".markdown", ".rst", ".txt", ".adoc", ".json", ".lock",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".csv", ".log",
)

#: The words that name a file as a place routes live, matched as WHOLE words
#: inside the path's own segments. Substring matching put
#: ``.claude/agents/fastapi-specialist-ext.md`` on the first live fact sheet
#: (2026-09-13), because "api" is inside "fastapi".
_ROUTE_WORDS = frozenset(
    {"router", "routers", "route", "routes", "api", "apis", "endpoint",
     "endpoints", "handler", "handlers", "controller", "controllers",
     "view", "views", "resource", "resources"}
)


def _looks_like_routes(path: str) -> bool:
    """True when this path is a source file where routes plausibly live."""
    lowered = path.lower()
    if lowered.endswith(_NOT_SOURCE):
        return False
    words = {word for word in re.split(r"[^a-z0-9]+", lowered) if word}
    return bool(words & _ROUTE_WORDS)


def _dependency_names(node: ast.AST) -> list[str]:
    """Every ``Depends(<name>)`` under ``node``, by the name it names."""
    names: list[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and getattr(sub.func, "id", getattr(sub.func, "attr", "")) == "Depends":
            for arg in sub.args:
                name = getattr(arg, "id", None) or getattr(arg, "attr", None)
                if name:
                    names.append(str(name))
    return names


def _returns_of(decorator: ast.Call, function: ast.AST) -> str:
    for keyword in decorator.keywords:
        if keyword.arg == "response_model":
            return _shape(keyword.value)
    annotation = getattr(function, "returns", None)
    if annotation is not None:
        return _shape(annotation)
    return "no declared response model"


def _shape(node: ast.AST) -> str:
    text = ast.unparse(node) if hasattr(ast, "unparse") else "a value"
    if isinstance(node, ast.Subscript):
        head = ast.unparse(node.value).lower() if hasattr(ast, "unparse") else ""
        if head in ("list", "typing.list", "sequence"):
            return f"an unwrapped list ({text})"
    if isinstance(node, ast.Constant) and node.value is None:
        return "nothing"
    return f"an object ({text})"


def routes_in_python_file(source: str) -> list[RouteFact]:
    """The routes a FastAPI/Starlette-style Python module declares."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    prefixes: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            callee = getattr(node.value.func, "id", None) or getattr(node.value.func, "attr", None)
            if callee == "APIRouter":
                for keyword in node.value.keywords:
                    if keyword.arg == "prefix" and isinstance(keyword.value, ast.Constant):
                        for target in node.targets:
                            if isinstance(target, ast.Name):
                                prefixes[target.id] = str(keyword.value.value)
    facts: list[RouteFact] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            method = decorator.func.attr
            if method not in _HTTP_METHODS or not decorator.args:
                continue
            first = decorator.args[0]
            if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
                continue
            owner = getattr(decorator.func.value, "id", "")
            path = prefixes.get(owner, "") + first.value
            guards = [n for n in _dependency_names(decorator) + _dependency_names(node.args) if _AUTH_NAME.search(n)]
            for arg in node.args.args + node.args.kwonlyargs:
                if _AUTH_NAME.search(arg.arg) and arg.arg not in ("db",):
                    guards.append(arg.arg)
            facts.append(
                RouteFact(
                    method.upper() if method not in ("api_route", "route") else "ROUTE",
                    path,
                    guards[0] if guards else None,
                    _returns_of(decorator, node),
                )
            )
            if len(facts) >= _MAX_ROUTES_PER_FILE:
                return facts
    return facts


def _sentences_for(file: str, facts: list[RouteFact]) -> str:
    listed = ", ".join(f"{f.method} {f.path}" for f in facts)
    parts = [f"`{file}` defines {listed}."]
    guarded = [f for f in facts if f.auth]
    if not guarded:
        parts.append("None of them declares an authentication dependency.")
    elif len(guarded) == len(facts):
        parts.append(
            "Every one of them requires authentication ("
            + ", ".join(sorted({str(f.auth) for f in guarded}))
            + ")."
        )
    else:
        parts.append(
            "Requires authentication: "
            + ", ".join(f"{f.method} {f.path} ({f.auth})" for f in guarded)
            + ". The others declare no authentication dependency."
        )
    returns = sorted({f.returns for f in facts})
    if len(returns) == 1:
        parts.append(f"Each returns {returns[0]}.")
    else:
        parts.append(" ".join(f"{f.method} {f.path} returns {f.returns}." for f in facts))
    return " ".join(parts)


def what_the_repository_already_does(repo_path: str, request_text: str) -> str | None:
    """The fact sheet, or ``None`` when the request names no route the
    repository has anything to say about."""
    try:
        paths = _route_paths_in(request_text)
        if not paths:
            return None
        root = Path(repo_path)
        sheets: list[str] = []
        seen_files: set[str] = set()
        for path in paths:
            first = "/" + path.lstrip("/").split("/")[0]
            if len(first) < 3:
                continue
            files = [f for f in _git_grep_files(str(root), f'"{first}') if _looks_like_routes(f)]
            if not files:
                # Same filter on the wider search: a documentation file that
                # mentions the path is not a file that defines it.
                files = [
                    f for f in _git_grep_files(str(root), f'"{first}/')
                    if _looks_like_routes(f)
                ]
            for file in files[:_MAX_FILES]:
                if file in seen_files:
                    continue
                seen_files.add(file)
                try:
                    source = (root / file).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if file.endswith(".py"):
                    facts = routes_in_python_file(source)
                    if facts:
                        sheets.append(_sentences_for(file, facts))
                else:
                    literals = sorted(
                        {m for m in _ROUTE_PATH.findall(source) if m.startswith(first)}
                    )[:_MAX_ROUTES_PER_FILE]
                    if literals:
                        sheets.append(
                            f"`{file}` mentions {', '.join(literals)}. This file is not "
                            "Python, so whether those routes require authentication "
                            "and what they return was not read."
                        )
        return "\n".join(sheets) if sheets else None
    except Exception:  # noqa: BLE001 — a fact sheet must never stop a planning run
        return None
