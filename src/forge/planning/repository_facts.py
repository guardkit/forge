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
authentication dependency, and what shape it returns. Deterministic — a
literal search for the path's first segment, then Python's own ``ast`` for
the file it lands in. It says only what it can prove: a file that is not
Python is named with its routes and the sentence "whether those routes
require authentication was not read", never a guess.

WIDENED (1 October 2026, release -3 item 10). On 1 October the planner's
checker let through an invented ``is_deleted`` column and its migration,
although the repository already soft-deletes its users with ``deleted_at``.
Routes alone could never have said so. The sheet now also names the data
models and migrations the request's words touch:

* a DATA-MODEL file is one the project declares as such, or — when it
  declares nothing — one whose path carries a model word (``model``,
  ``entity``, ``schema``, ``table``…) as a whole word. In a Python file the
  classes whose name, or whose own string setting, is one of the request's
  words are read with ``ast`` and their own fields listed exactly as written.
  A file in any other language is named, and the sheet says its fields were
  not read;
* a MIGRATION file is one the project declares as such, or one whose path
  carries a migration word. Only file names are reported — the ones whose
  names carry one of the request's words — and how many there are.

Where the project says where those live: an optional ``repository_facts:``
block in its own ``.guardkit/config.yaml`` (the file the factory already
reads for ``toolchain:``, ``memory:`` and ``specification:``)::

    repository_facts:
      data_models: ["src/**/models.py"]
      migrations: ["db/changes"]

Patterns read exactly as the ``specification: paths:`` ones do. Nothing here
knows any framework's or tool's name; the path words are plain English.

WHERE THE REPOSITORY IS READ (1 October 2026). It used to be ``git grep`` and
a file read at the coordinator's ``repo_path``. In the containerised
coordinator that path does not exist, and the function, built never to raise,
quietly said nothing, so the checker ran with no facts at all. Now every read
goes through a :class:`RepositoryReader`: :class:`LocalCheckoutReader` for a
repository the coordinator holds itself, and the sandbox helper's read-only
``/code`` routes for a repository that has a sandbox (built by
:meth:`forge.planning.sidecar_git_runner.SidecarGitRunner.code_reader`). The
driver picks the reader exactly the way its git runner routes every other
call for that repository.

SILENCE IS THE BUG. A repository that cannot be read is no longer "nothing to
say": :func:`read_repository_facts` answers with an explicit unavailable state
and its reason, and its :attr:`RepositoryFacts.text` — what the coach is shown
— begins ``Repository facts unavailable:``. The driver puts the same reason
on the card the person approves.

Still never raises: a fact sheet must never be able to stop a planning run.
``None`` text when the request has no words to look for, or the repository
has nothing to say about them.
"""

from __future__ import annotations

import ast
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence

__all__ = [
    "DECLARATION_KEY",
    "LocalCheckoutReader",
    "ModelFact",
    "RepositoryFacts",
    "RepositoryReader",
    "RepositoryUnreadable",
    "RouteFact",
    "UNAVAILABLE_PREFIX",
    "models_in_python_file",
    "read_repository_facts",
    "routes_in_python_file",
    "unavailable_text",
    "what_the_repository_already_does",
]

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


#: How many of the request's route paths the sheet looks at. A sentence
#: names one or two; past this a pasted list would only cost searches.
_MAX_ROUTE_PATHS = 3


def _route_paths_in(request_text: str) -> list[str]:
    paths: list[str] = []
    for match in _ROUTE_PATH.findall(request_text or ""):
        if len(match) > 3 and re.search(r"[A-Za-z]", match) and match not in paths:
            paths.append(match)
            if len(paths) >= _MAX_ROUTE_PATHS:
                break
    return paths



# ---------------------------------------------------------------------------
# Reading the repository
# ---------------------------------------------------------------------------


class RepositoryUnreadable(Exception):
    """The repository could not be read. The message is one plain clause
    saying why, written for the card a person reads."""


class RepositoryReader(Protocol):
    """The reads the planner makes, wherever the repository is."""

    #: Plain words naming where the repository is read from.
    where: str

    def list_files(self) -> list[str]:
        """Every file the repository tracks. Raises
        :class:`RepositoryUnreadable` when the repository cannot be read."""

    def files_mentioning(
        self, text: str, *, ignore_case: bool = False, relevant: Any = None
    ) -> list[str]:
        """The tracked files whose text contains ``text`` literally.
        ``relevant`` (optional) names the files the caller can use, so a
        reader that must recover a cut answer recovers only those. Raises
        :class:`RepositoryUnreadable` when the repository cannot be read."""

    def places_mentioning(self, text: str) -> list[str]:
        """Every ``path:line`` whose text contains ``text`` literally. Raises
        :class:`RepositoryUnreadable` when the repository cannot be read."""

    def read_text(self, path: str) -> str | None:
        """One tracked file's text; ``None`` when that one file cannot be
        served (too large, not text) — and then the reason is kept in the
        reader's ``refused`` mapping, by path, so a caller can say it.
        Raises :class:`RepositoryUnreadable` when the repository itself
        cannot be read."""


class LocalCheckoutReader:
    """A checkout the coordinator holds itself, read with ``git``.

    ``timeout_s`` bounds the listing, ``search_timeout_s`` each search. A
    directory without its own ``.git`` is refused rather than read: ``git -C``
    walks UP to find a repository, so a plain directory inside somebody
    else's checkout would otherwise answer with that checkout's files.
    """

    def __init__(
        self, repo_path: str, *, timeout_s: float = 10.0, search_timeout_s: float = 30.0
    ) -> None:
        self._root = Path(str(repo_path))
        self._timeout_s = timeout_s
        self._search_timeout_s = search_timeout_s
        self.where = f"the checkout at {self._root}"
        #: Why each file that could not be served was refused, by path.
        self.refused: dict[str, str] = {}

    def _check_root(self) -> None:
        if not self._root.is_dir():
            raise RepositoryUnreadable(
                f"there is no checkout at {self._root} where the planner runs"
            )
        if not (self._root / ".git").exists():
            raise RepositoryUnreadable(f"{self._root} is not a git repository (no .git)")

    def _git(self, *args: str, timeout: float) -> subprocess.CompletedProcess[str]:
        self._check_root()
        try:
            return subprocess.run(
                ["git", "-c", "safe.directory=*", "-C", str(self._root), *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RepositoryUnreadable(
                f"git could not read {self.where} ({type(exc).__name__})"
            ) from exc

    @staticmethod
    def _first_line(stderr: str) -> str:
        return (" ".join((stderr or "").split()) or "no reason given")[:200]

    def list_files(self) -> list[str]:
        completed = self._git("ls-files", "-z", timeout=self._timeout_s)
        if completed.returncode != 0:
            raise RepositoryUnreadable(
                f"listing the tracked files of {self.where} failed (git exited "
                f"{completed.returncode}: {self._first_line(completed.stderr)})"
            )
        return [p for p in completed.stdout.split("\0") if p]

    def _grep(self, *args: str) -> list[str]:
        completed = self._git("grep", *args, timeout=self._search_timeout_s)
        # git grep answers 1 for "no match": that is an answer, not a failure.
        if completed.returncode == 1 and not completed.stderr.strip():
            return []
        if completed.returncode != 0:
            raise RepositoryUnreadable(
                f"searching {self.where} failed (git exited "
                f"{completed.returncode}: {self._first_line(completed.stderr)})"
            )
        return [line for line in completed.stdout.splitlines() if line.strip()]

    def files_mentioning(
        self, text: str, *, ignore_case: bool = False, relevant: Any = None
    ) -> list[str]:
        args = ["-l", "-F"] + (["-i"] if ignore_case else []) + ["-e", text]
        return [line.strip() for line in self._grep(*args)]

    def places_mentioning(self, text: str) -> list[str]:
        return [
            ":".join(line.split(":", 2)[:2])
            for line in self._grep("-n", "--fixed-strings", "--", text)
        ]

    def read_text(self, path: str) -> str | None:
        self._check_root()
        try:
            data = (self._root / path).read_bytes()
        except OSError as exc:
            self.refused[path] = f"it could not be opened ({type(exc).__name__})"
            return None
        if len(data) > _MAX_READ_BYTES:
            self.refused[path] = (
                f"it is {len(data)} bytes, over the {_MAX_READ_BYTES}-byte limit for one file"
            )
            return None
        if b"\0" in data[:8192]:
            self.refused[path] = "it is not text"
            return None
        return data.decode("utf-8", errors="replace")


def _not_read(reader: Any, path: str) -> str:
    """One plain sentence for a file the reader would not serve, with its
    reason when the reader kept one."""
    why = (getattr(reader, "refused", None) or {}).get(path)
    return f"`{path}` could not be read ({why or 'it was not served'})"


# ---------------------------------------------------------------------------
# The answer
# ---------------------------------------------------------------------------

#: How the coach's sheet begins when the repository could not be read. The
#: card line, the receipt and the coach all carry the same reason.
UNAVAILABLE_PREFIX: str = "Repository facts unavailable: "

#: What follows the reason on the coach's sheet. Absence of facts is not a
#: fact: a reader must not take "nothing said" as "nothing there".
_UNAVAILABLE_TAIL: str = (
    " Nothing on this sheet says what the repository already has, and that "
    "is not evidence that it has nothing: do not add tables, columns, routes "
    "or files the request does not name on the strength of this silence."
)


@dataclass(frozen=True)
class RepositoryFacts:
    """The fact sheet, or the plain reason there is none.

    ``sheet`` is ``None`` when there was nothing to say. ``unavailable`` is the
    reason the repository could not be read, and is ``None`` when it was.
    """

    sheet: str | None
    unavailable: str | None = None
    where: str = ""
    #: Set when the repository was read in part: what was read is on the
    #: sheet, and the sheet's notes say what was not.
    partial: str | None = None

    @property
    def text(self) -> str | None:
        """What the coach and the deterministic reviewers are given."""
        if self.unavailable:
            return unavailable_text(self.unavailable)
        return self.sheet


def unavailable_text(reason: str) -> str:
    """The sentence a coach or plan-writer is given in place of facts it could
    not have, with the reason the repository could not be read."""
    return f"{UNAVAILABLE_PREFIX}{reason}.{_UNAVAILABLE_TAIL}"


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




# ---------------------------------------------------------------------------
# The words a request uses
# ---------------------------------------------------------------------------

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")

#: Words that say nothing about WHAT the repository holds: the request's
#: grammar and the vocabulary of asking for an endpoint. A model named after
#: one of these would still be a true fact; leaving them out only keeps the
#: search short.
_STOP_WORDS = frozenset(
    {
        "the", "and", "for", "that", "this", "with", "from", "into", "onto",
        "each", "per", "all", "any", "are", "was", "were", "has", "have",
        "had", "not", "its", "their", "them", "they", "then", "than", "when",
        "which", "who", "what", "where", "how", "why", "should", "would",
        "could", "can", "will", "shall", "must", "may", "might", "new",
        "add", "adds", "get", "post", "put", "patch", "make", "use", "using",
        "only", "also", "last", "first", "oldest", "newest", "one", "two",
        "three", "four", "five", "six", "seven", "eight", "nine", "ten",
        "endpoint", "endpoints", "route", "routes", "api", "return",
        "returns", "returned", "returning", "give", "gives", "show", "shows",
        "list", "lists", "number", "count", "please", "want", "need", "our",
        "your", "you", "there", "here", "about", "like", "such", "other",
        "same", "some", "more", "most", "less", "least", "very", "just",
        "but", "nor", "yet", "both", "either", "neither", "via", "over",
        "under", "after", "before", "between", "within", "without", "http",
        "https", "json", "response", "request", "field", "fields",
    }
)

_MAX_REQUEST_WORDS = 20


def _request_words(request_text: str) -> list[str]:
    """The request's content words, lower case, in order, without repeats."""
    words: list[str] = []
    for match in _WORD.findall(request_text or ""):
        for part in re.split(r"_+", match.lower()):
            if len(part) >= 3 and part not in _STOP_WORDS and part not in words:
                words.append(part)
    return words[:_MAX_REQUEST_WORDS]


def _forms(word: str) -> set[str]:
    """A word, and its plain singular and plural, so ``users`` finds ``User``
    and ``user`` finds ``users``. Spelling only — no dictionary, no guess."""
    word = word.lower()
    forms = {word, word + "s"}
    if word.endswith("ies") and len(word) > 4:
        forms.add(word[:-3] + "y")
    elif word.endswith(("ses", "xes", "ches", "shes")) and len(word) > 4:
        forms.add(word[:-2])
    elif word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        forms.add(word[:-1])
    if word.endswith("y") and len(word) > 3:
        forms.add(word[:-1] + "ies")
    return forms


def _path_words(path: str) -> set[str]:
    return {word for word in re.split(r"[^a-z0-9]+", path.lower()) if word}


# ---------------------------------------------------------------------------
# Data models and migrations
# ---------------------------------------------------------------------------

#: Whole path words that name a file as a data model, best first. Plain
#: English, the same in every stack.
#: ("table" is left out on purpose: it is too often a plain word in a file
#: name — ``add-deleted-at-column-to-users-table.feature`` — to name a model.)
_MODEL_WORDS_FIRST = frozenset({"model", "models", "entity", "entities", "orm"})
_MODEL_WORDS_THEN = frozenset({"schema", "schemas"})
#: Whole path words that name a file as a migration.
_MIGRATION_WORDS = frozenset({"migration", "migrations", "migrate"})
#: Path words that name a file as a test or a fixture — never the model itself.
_TEST_WORDS = frozenset({"test", "tests", "spec", "specs", "fixture", "fixtures", "mock", "mocks"})
#: A migration may be written in a data format; only prose is ruled out.
_PROSE = (".md", ".markdown", ".rst", ".txt", ".adoc", ".log")

_MAX_MODEL_FILES = 5
_MAX_MODEL_CLASSES = 6
_MAX_FIELDS_PER_CLASS = 40
_MAX_FIELD_CHARS = 110
_MAX_MIGRATION_NAMES = 10
_MAX_SHEET_CHARS = 6000
#: Each section's share of the sheet, so one long section can never push
#: another out: the routes cannot crowd out the models that would have shown
#: ``deleted_at``. They add up to the whole.
_ROUTE_SECTION_CHARS = 2400
_MODEL_SECTION_CHARS = 2400
_MIGRATION_SECTION_CHARS = 600
_NOTE_SECTION_CHARS = 600
_MAX_READ_BYTES = 262_144
_MAX_NOUN_SEARCHES = 2

#: Where a project declares its own data-model and migration paths: the file
#: it already declares its toolchain, memory and specification in.
DECLARATION_KEY = "repository_facts"


@dataclass(frozen=True)
class ModelFact:
    """One data-model class, as its own body writes it."""

    name: str
    bases: tuple[str, ...]
    settings: tuple[str, ...]
    fields: tuple[str, ...]


def _one_line(node: ast.AST) -> str:
    text = " ".join(ast.unparse(node).split())
    return text if len(text) <= _MAX_FIELD_CHARS else text[: _MAX_FIELD_CHARS - 1] + "…"


def models_in_python_file(source: str, words: Sequence[str]) -> list[ModelFact]:
    """The classes in ``source`` that the request's ``words`` name.

    A class is named when its own name, or a string its own body sets (a
    table or collection name, whatever the framework calls it), is one of the
    words in singular or plural. Its fields are the assignments written in
    its own body, verbatim; methods and inherited fields are not listed.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    wanted: set[str] = set()
    for word in words:
        wanted |= _forms(word)
    found: list[ModelFact] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        settings: list[str] = []
        fields: list[str] = []
        named = node.name.lower() in wanted
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                target = stmt.target.id
                value = stmt.value
            elif (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
            ):
                target = stmt.targets[0].id
                value = stmt.value
            else:
                continue
            if target.startswith("__") and target.endswith("__"):
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    settings.append(_one_line(stmt))
                    if value.value.lower() in wanted:
                        named = True
                continue
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                if value.value.lower() in wanted:
                    named = True
            fields.append(_one_line(stmt))
        if named and fields:
            found.append(
                ModelFact(
                    name=node.name,
                    bases=tuple(_one_line(base) for base in node.bases),
                    settings=tuple(settings[:5]),
                    fields=tuple(fields[:_MAX_FIELDS_PER_CLASS]),
                )
            )
            if len(found) >= _MAX_MODEL_CLASSES:
                break
    return found


def _model_sentence(file: str, fact: ModelFact) -> str:
    head = f"`{file}` declares class {fact.name}"
    if fact.bases:
        head += f"({', '.join(fact.bases)})"
    parts = [head + "."]
    if fact.settings:
        parts.append("Its own settings: " + "; ".join(fact.settings) + ".")
    parts.append(
        "The fields written in its own body: " + "; ".join(fact.fields) + "."
    )
    parts.append("Fields it inherits were not read.")
    return " ".join(parts)


def _declared_paths(
    reader: RepositoryReader, tracked: Sequence[str], unread: list[str] | None = None
) -> tuple[tuple[str, ...] | None, tuple[str, ...] | None, str | None]:
    """``(data_models, migrations, note)`` from the project's own declaration.

    ``None`` for a list the project does not declare. ``note`` is one sentence
    when the declaration is there and could not be read.
    """
    from forge.planning.declared_memory import DECLARATION_PATH, _parse

    if DECLARATION_PATH not in set(tracked):
        return None, None, None
    text = reader.read_text(DECLARATION_PATH)
    if text is None:
        if unread is not None:
            unread.append(_not_read(reader, DECLARATION_PATH))
        return None, None, (
            f"The project's declarations could not be read: "
            f"{_not_read(reader, DECLARATION_PATH)}, so data-model and "
            "migration files were found by their path words."
        )
    data, why = _parse(text)
    if data is None:
        return None, None, (
            f"The project's `{DECLARATION_PATH}` could not be read ({why}), so "
            "data-model and migration files were found by their path words."
        )
    block = data.get(DECLARATION_KEY)
    if block is None:
        return None, None, None
    if not isinstance(block, dict):
        return None, None, (
            f"The project's `{DECLARATION_PATH}` has a `{DECLARATION_KEY}` "
            "block that is not a mapping, so data-model and migration files "
            "were found by their path words."
        )

    def _patterns(key: str) -> tuple[str, ...] | None:
        value = block.get(key)
        if isinstance(value, str) and value.strip():
            return (value.strip(),)
        if isinstance(value, list):
            cleaned = tuple(str(v).strip() for v in value if isinstance(v, str) and v.strip())
            return cleaned or None
        return None

    return _patterns("data_models"), _patterns("migrations"), None


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    from forge.pipeline.merge_ready_checkpoint import path_is_specification

    # The one place a path is compared with a project's declared patterns;
    # the specification fence reads its own declaration through it.
    return path_is_specification(path, patterns)


def _model_sheets(
    reader: RepositoryReader,
    tracked: Sequence[str],
    declared: tuple[str, ...] | None,
    words: Sequence[str],
    route_nouns: Sequence[str],
    test_folders: tuple[str, ...] = (),
    into: list[str] | None = None,
    unread: list[str] | None = None,
) -> list[str]:
    sheets: list[str] = into if into is not None else []
    if declared is not None:
        candidates = [f for f in tracked if _matches(f, declared)]
        if not candidates:
            sheets.append(
                "The project declares its data models at "
                + ", ".join(f"`{p}`" for p in declared)
                + ", and no tracked file is there, so no model was read."
            )
            return sheets
    else:
        candidates = []
        for f in tracked:
            if f.lower().endswith(_NOT_SOURCE):
                continue
            if f.startswith(test_folders):
                continue
            pw = _path_words(f)
            if pw & _TEST_WORDS:
                continue
            if pw & (_MODEL_WORDS_FIRST | _MODEL_WORDS_THEN):
                candidates.append(f)
    if not candidates:
        return sheets
    all_forms: set[str] = set()
    for word in words:
        all_forms |= _forms(word)
    route_forms: set[str] = set()
    for noun in route_nouns:
        route_forms |= _forms(noun)
    mentioned: dict[str, str] = {}
    candidate_set = set(candidates)
    for noun in list(route_nouns)[:_MAX_NOUN_SEARCHES]:
        for f in reader.files_mentioning(
            noun, ignore_case=True, relevant=candidate_set.__contains__
        ):
            mentioned.setdefault(f, noun)

    def _rank(f: str) -> tuple[int, int, str]:
        pw = _path_words(f)
        if pw & route_forms:
            first = 0
        elif pw & all_forms:
            first = 1
        elif f in mentioned:
            first = 2
        else:
            first = 3
        return (first, 0 if pw & _MODEL_WORDS_FIRST else 1, f)

    for f in sorted(candidates, key=_rank)[:_MAX_MODEL_FILES]:
        if f.endswith((".py", ".pyi")):
            source = reader.read_text(f)
            if source is None:
                if unread is not None:
                    unread.append(_not_read(reader, f))
                continue
            for fact in models_in_python_file(source, words):
                sheets.append(_model_sentence(f, fact))
            continue
        pw = _path_words(f)
        named_by_path = sorted(pw & all_forms)
        if named_by_path:
            why = f"its path names {', '.join(named_by_path)}"
        elif f in mentioned:
            why = f"it mentions {mentioned[f]}"
        else:
            continue
        sheets.append(
            f"`{f}` looks like a data-model file by its path, and {why}. It is "
            "not Python, so what it declares was not read."
        )
    return sheets


def _migration_sheet(
    tracked: Sequence[str], declared: tuple[str, ...] | None, words: Sequence[str]
) -> str | None:
    if declared is not None:
        migrations = [f for f in tracked if _matches(f, declared)]
        if not migrations:
            return (
                "The project declares its migrations at "
                + ", ".join(f"`{p}`" for p in declared)
                + ", and no tracked file is there."
            )
        how = "declared by the project at " + ", ".join(f"`{p}`" for p in declared)
    else:
        migrations = [
            f
            for f in tracked
            if not f.lower().endswith(_PROSE)
            and _path_words(f) & _MIGRATION_WORDS
            and not _path_words(f) & _TEST_WORDS
        ]
        if not migrations:
            return None
        folders = sorted({f.rsplit("/", 1)[0] if "/" in f else "." for f in migrations})
        how = "found by their path, under " + ", ".join(f"`{d}`" for d in folders[:3])
        if len(folders) > 3:
            how += f" and {len(folders) - 3} more folder(s)"
    all_forms: set[str] = set()
    for word in words:
        all_forms |= _forms(word)
    named = sorted(f for f in migrations if _path_words(f.rsplit("/", 1)[-1]) & all_forms)
    sentence = f"The repository has {len(migrations)} migration file(s), {how}."
    if named:
        shown = ", ".join(f"`{f}`" for f in named[:_MAX_MIGRATION_NAMES])
        more = f" and {len(named) - _MAX_MIGRATION_NAMES} more" if len(named) > _MAX_MIGRATION_NAMES else ""
        sentence += f" Those whose names carry the request's words: {shown}{more}."
    else:
        sentence += " None of their names carries the request's words."
    sentence += " Their contents were not read."
    return sentence


# ---------------------------------------------------------------------------
# The sheet
# ---------------------------------------------------------------------------


def _route_sheets(
    reader: RepositoryReader,
    paths: Sequence[str],
    test_folders: tuple[str, ...] = (),
    into: list[str] | None = None,
    unread: list[str] | None = None,
) -> list[str]:
    """Route facts for the request's paths. One search per first segment —
    ``/users/a`` and ``/users/b`` are the same search — and never a file
    in the repository's own test folders, which mention routes without
    defining them."""

    def _candidate(f: str) -> bool:
        return _looks_like_routes(f) and not f.startswith(test_folders)

    sheets: list[str] = into if into is not None else []
    seen_files: set[str] = set()
    seen_firsts: set[str] = set()
    for path in paths:
        first = "/" + path.lstrip("/").split("/")[0]
        if len(first) < 3 or first in seen_firsts:
            continue
        seen_firsts.add(first)
        files = [
            f for f in reader.files_mentioning(f'"{first}', relevant=_candidate) if _candidate(f)
        ]
        if not files:
            # Same filter on the wider search: a documentation file that
            # mentions the path is not a file that defines it.
            files = [
                f
                for f in reader.files_mentioning(f'"{first}/', relevant=_candidate)
                if _candidate(f)
            ]
        for file in files[:_MAX_FILES]:
            if file in seen_files:
                continue
            seen_files.add(file)
            source = reader.read_text(file)
            if source is None:
                if unread is not None:
                    unread.append(_not_read(reader, file))
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
    return sheets


def _test_folders(tracked: Sequence[str]) -> tuple[str, ...]:
    """The repository's own test folders, found by the factory's existing
    test-root discovery run over the listing — never a list of names here."""
    try:
        from forge.planning.target_terminal_tools import (
            discover_test_roots_from_listing,
            folders_holding_tests,
        )

        return folders_holding_tests(discover_test_roots_from_listing(tracked))
    except Exception:  # noqa: BLE001 — a fact sheet must never stop a planning run
        return ()


def _section(lines: Sequence[str], cap: int, what: str) -> str:
    """``lines`` joined, held to ``cap`` characters by whole lines; what was
    left out is said, never silently dropped."""
    kept: list[str] = []
    used = 0
    for index, line in enumerate(lines):
        extra = len(line) + (1 if kept else 0)
        if used + extra > cap - 120:
            left = len(lines) - index
            if not kept:
                kept.append(line[: cap - 140].rstrip() + "…")
                left -= 1
            if left:
                kept.append(f"({left} more line(s) about {what} were left out to keep the sheet short.)")
            break
        kept.append(line)
        used += extra
    return "\n".join(kept)


def _bounded(text: str) -> str:
    """The last wall: the sections' own caps add up to this already."""
    if len(text) <= _MAX_SHEET_CHARS:
        return text
    return text[: _MAX_SHEET_CHARS - 60].rstrip() + f"\n(The sheet was cut at {_MAX_SHEET_CHARS} characters.)"


def read_repository_facts(reader: RepositoryReader, request_text: str) -> RepositoryFacts:
    """The fact sheet for ``request_text``, read through ``reader``.

    Never raises. A repository that cannot be read answers with
    :attr:`RepositoryFacts.unavailable` set to the reason, never with an
    empty sheet that looks like "nothing there".
    """
    where = str(getattr(reader, "where", "") or "the repository")
    try:
        paths = _route_paths_in(request_text)
        words = _request_words(request_text)
        if not paths and not words:
            return RepositoryFacts(None, where=where)
        begin = getattr(reader, "begin", None)
        if callable(begin):
            begin()
        tracked = reader.list_files()
        test_folders = _test_folders(tracked)
        route_nouns: list[str] = []
        for path in paths:
            noun = path.lstrip("/").split("/")[0].lower()
            if re.search(r"[a-z]", noun) and "{" not in noun and noun not in route_nouns:
                route_nouns.append(noun)
        all_words = list(dict.fromkeys([*route_nouns, *words]))
        # What is read is KEPT: a helper that stops answering part-way leaves
        # every fact already read on the sheet, and the notes say the rest
        # could not be read.
        route_lines: list[str] = []
        model_lines: list[str] = []
        migration: str | None = None
        notes: list[str] = []
        # Files the sheet chose to read and the reader would not serve: each
        # is said by name and reason, never skipped as if it were not there.
        unread: list[str] = []
        stopped: str | None = None
        try:
            _route_sheets(reader, paths, test_folders, into=route_lines, unread=unread)
            models, migrations, note = _declared_paths(reader, tracked, unread)
            if note:
                notes.append(note)
            _model_sheets(
                reader, tracked, models, all_words, route_nouns, test_folders,
                into=model_lines, unread=unread,
            )
            migration = _migration_sheet(tracked, migrations, all_words)
        except RepositoryUnreadable as exc:
            stopped = str(exc) or f"{where} could not be read"
        cuts = list(getattr(reader, "cuts", None) or [])
        if cuts:
            # A cut-short answer is never passed off as a whole one.
            notes.append(
                "Some of what this sheet read was incomplete, so it may be "
                "missing facts: " + "; ".join(dict.fromkeys(cuts)) + "."
            )
        unread = list(dict.fromkeys(unread))
        if unread and not route_lines and not model_lines and not migration and not stopped:
            # The files that mattered could not be read and nothing else was
            # learned: that is "could not read", not "nothing there".
            return RepositoryFacts(
                None,
                unavailable="the files that matter for this request could not be read: "
                + "; ".join(unread),
                where=where,
            )
        if unread:
            notes.insert(0, "Not read, so this sheet is incomplete: " + "; ".join(unread) + ".")
        if stopped:
            if not route_lines and not model_lines and not migration:
                return RepositoryFacts(None, unavailable=stopped, where=where)
            notes.append(
                f"The rest of the repository could not be read ({stopped}), so "
                "this sheet is incomplete."
            )
        sections = [
            _section(route_lines, _ROUTE_SECTION_CHARS, "routes"),
            _section(model_lines, _MODEL_SECTION_CHARS, "data models"),
            _section([migration] if migration else [], _MIGRATION_SECTION_CHARS, "migrations"),
            _section(notes, _NOTE_SECTION_CHARS, "what was incomplete"),
        ]
        sheet = "\n".join(section for section in sections if section)
        partial = "; ".join([*([stopped] if stopped else []), *unread, *dict.fromkeys(cuts)]) or None
        return RepositoryFacts(_bounded(sheet) if sheet else None, where=where, partial=partial)
    except RepositoryUnreadable as exc:
        return RepositoryFacts(None, unavailable=str(exc) or f"{where} could not be read", where=where)
    except Exception as exc:  # noqa: BLE001 — a fact sheet must never stop a planning run
        # Even a fault in this reader is said, never swallowed into silence.
        return RepositoryFacts(
            None,
            unavailable=f"reading {where} failed ({type(exc).__name__}: {str(exc)[:160]})",
            where=where,
        )


def what_the_repository_already_does(repo_path: str, request_text: str) -> str | None:
    """The coach's sheet for a checkout the coordinator holds itself: the
    facts, the unavailable sentence when the checkout cannot be read, or
    ``None`` when there is nothing to say."""
    return read_repository_facts(LocalCheckoutReader(repo_path), request_text).text
