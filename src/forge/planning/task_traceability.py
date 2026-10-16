"""Hold every task in a plan against the sentence the person actually sent.

WHY (2026-09-15, the twelve-plan measurement). One sentence went through the
factory twelve times. The specification the person approved was right twelve
times out of twelve; the plan behind it was different every time — three to
five tasks, five different names for one endpoint, the web address moved in
three of the twelve, and something nobody asked for in ten of the twelve. Every
refusal in the whole experiment came from the plan, never from the coder, which
built exactly what its task said each time. So the leak is one stage wide, and
this is the deterministic half of the cure: the plan is read against the
request before anything is committed.

THE RULE, in one sentence: **a plan is sent back to be rewritten, once, when
any of its tasks names a web address the request did not name or asks for a
capability the request never mentioned.**

Two flags, both real additions to what the person asked for:

* ``CONTRADICTED_PATH`` — the task names a web address the request does not
  name. This is what got one run refused at the live gate an hour later.
* ``UNASKED_CAPABILITY`` — the task asks for something the request never
  mentioned: a login requirement, a database migration, a service module,
  error handling, logging. This is what got two more runs refused.

NO EXACT WORDING (16 October 2026). There was a third flag, ``CANNOT_CITE``:
a task whose quote was not three consecutive words of the request. It is
gone. Rich, in his words: "insists on exact wording from an LLM generated text
is just never going to be reliable so I don't think insisting on exact wording
makes sense". A task may say what it serves in the request's words or its
own; nothing here compares a task's wording with the request's any more. The
two flags above never depended on quotes: they read every web address and
capability word in the whole task document. (Design of record: ai-transition
``docs/designs/planning-remove-exact-quote-rule-2026-10-16.md``.)

Every question is asked of the WHOLE task document — its front matter, its
title, its body sentence and its acceptance criteria — because that is where
the evidence turned out to be: in six of the twelve plans the web address
appears nowhere except inside an acceptance criterion, and the plan that moved
the endpoint said so only in a criterion.

Pure: no input, no output, no model, never raises on bad input (a task document
that cannot be read is a task the review says it could not read, not a failed
planning run).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import yaml

from forge.planning.assumption_review import _CAPABILITIES
from forge.planning.repository_facts import _ROUTE_PATH

__all__ = [
    "CONTRADICTED_PATH",
    "UNASKED_CAPABILITY",
    "TaskFinding",
    "TraceabilityReview",
    "review_task_traceability",
]

#: The task names a web address the request does not name — a contradiction of
#: something the person actually said.
CONTRADICTED_PATH = "CONTRADICTED_PATH"

#: The task asks for a capability the request never mentions — an addition.
UNASKED_CAPABILITY = "UNASKED_CAPABILITY"

#: Boilerplate, taken out before a task is read: it says nothing about what
#: the task builds. This sentence appears in forty-nine of the fifty-eight task
#: documents measured.
_LINT_BOILERPLATE = re.compile(
    r"^.*all modified files pass project-configured lint/format checks.*$",
    re.IGNORECASE | re.MULTILINE,
)

#: The capability classes a plan may not add on its own, beyond the ones the
#: spec-side reviewer already knows (authentication, permissions, rate
#: limiting, pagination, caching, a required header, a response wrapper — the
#: table imported above). These four are what the twelve-plan measurement used.
#:
#: "metrics" is deliberately NOT here. In the twelve measured plans it fired on
#: three tasks of one plan purely because that plan chose "metrics" as its
#: naming family, and a list that flags a plan for its choice of names is a
#: list a person learns to ignore.
_PLAN_ONLY_CAPABILITIES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "a database migration",
        re.compile(
            r"\bmigrations?\b|\balembic\b|\bschema (change|upgrade|version)\b|"
            r"\bdowngrade\b",
            re.IGNORECASE,
        ),
    ),
    (
        "a service layer or module",
        re.compile(
            r"\bservice (layer|module|class|logic)\b|\bbusiness logic layer\b|"
            r"\bservice[- ]oriented\b|\b(new|separate|dedicated) package\b",
            re.IGNORECASE,
        ),
    ),
    (
        "error handling",
        re.compile(
            r"\berror handling\b|\bexception handler\b|\berror handler\b|"
            r"\bglobal (error|exception)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "logging",
        re.compile(
            r"\blogging\b|\blog (entries|records|statements)\b|"
            r"\bstructured logs?\b",
            re.IGNORECASE,
        ),
    ),
)

#: Every capability class the guard asks about: the spec side's table, then the
#: four above. One list, so the two halves of the planning coach cannot drift.
_ALL_CAPABILITIES: tuple[tuple[str, re.Pattern[str]], ...] = (
    *_CAPABILITIES,
    *_PLAN_ONLY_CAPABILITIES,
)

#: Folders whose files are not web addresses even when they are written with a
#: leading slash. A path under a test root or a documentation folder is a place
#: on disk, and reading it as a route the request never named would flag the
#: cleanest plans in the measurement.
_DEFAULT_TEST_ROOTS: tuple[str, ...] = ("tests", "test", "spec", "specs")
_DOC_FOLDERS: tuple[str, ...] = ("docs", "doc", "documentation")

#: Top-level folders of the operating system's own disk. A slash-led path that
#: starts with one of these is a file, never a web address: a Keycloak setup
#: script writing ``/tmp/kcadm-`` and ``/dev/null`` put "TASK-xxx answers at
#: /tmp/kcadm-" on an approval card on 15 October 2026. A blunt list on
#: purpose. Names a web application might really use as its own address are
#: left off it: ``home``, ``users``, ``private``, ``root``, ``run``, ``sys``
#: (``/sys/user`` in an admin console) and ``dev`` (``/dev/seed``, the
#: unrequested extra a card exists to catch) — so ``/users/created-per-day``
#: is still read as the web address it is.
_SYSTEM_FOLDERS: tuple[str, ...] = (
    "tmp", "proc", "etc", "var", "usr", "opt",
    "bin", "sbin", "lib", "lib64", "mnt", "srv", "nix", "boot",
)

#: The device files a script really writes to or reads from. Only these, and
#: anything under them, count as places on disk inside ``/dev``.
_DEVICE_FILES: tuple[str, ...] = (
    "/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr", "/dev/zero",
    "/dev/random", "/dev/urandom", "/dev/tty", "/dev/shm",
    # bash's own network files: a readiness probe writing to /dev/tcp/host/port
    # put "/dev/tcp" on FEAT-0A2A's merge card on 15 October 2026.
    "/dev/tcp", "/dev/udp",
)


@dataclass(frozen=True)
class TaskFinding:
    """One thing wrong with one task, said plainly."""

    task_id: str
    path: str
    flag: str
    what: str
    sentence: str


@dataclass
class TraceabilityReview:
    """The plan tree as read, and everything found wrong with it."""

    findings: list[TaskFinding] = field(default_factory=list)
    tasks_read: int = 0
    unreadable: list[str] = field(default_factory=list)

    @property
    def contradictions(self) -> list[TaskFinding]:
        """The findings that can stop a run: a moved web address, or a
        capability nobody asked for."""
        return [
            finding
            for finding in self.findings
            if finding.flag in (CONTRADICTED_PATH, UNASKED_CAPABILITY)
        ]

    @property
    def flagged_tasks(self) -> list[str]:
        """Every task with a finding against it, in the order first found."""
        seen: list[str] = []
        for finding in self.findings:
            if finding.task_id not in seen:
                seen.append(finding.task_id)
        return seen

    @property
    def sends_it_back(self) -> bool:
        """True when this plan should be rewritten once: a task contradicts
        the request (a web address it did not name, or a capability it never
        mentioned). How a task words what it serves never sends a plan back
        (16 October 2026)."""
        return bool(self.contradictions)

    @property
    def stops_the_run(self) -> bool:
        """True when a plan that has ALREADY been rewritten once still
        contradicts the request."""
        return bool(self.contradictions)

    def note(self) -> str:
        """The machine's note to the plan writer: every finding, word for
        word, and the one ask."""
        lines = [
            f"The machine read this plan against the request and found "
            f"{len(self.findings)} task(s) that do not follow it:"
        ]
        for finding in self.findings:
            lines.append(f"- {finding.sentence}")
        lines.append(
            "Rewrite the plan so that every task answers at the web address "
            "the request names and no other, and adds no capability the "
            "request does not mention. A plan may choose between readings of "
            "what was asked; it may not add. Change nothing else."
        )
        return "\n".join(lines)

    def stop_sentences(self) -> list[str]:
        """The findings that stopped the run, in ordinary words."""
        return [finding.sentence for finding in self.contradictions]

    def receipt(self) -> dict[str, Any]:
        return {
            "tasks_read": self.tasks_read,
            "flagged": list(self.flagged_tasks),
            "findings": [
                {
                    "task_id": finding.task_id,
                    "path": finding.path,
                    "flag": finding.flag,
                    "what": finding.what,
                    "sentence": finding.sentence,
                }
                for finding in self.findings
            ],
            "unreadable": list(self.unreadable),
        }


def _is_task_document(path: str) -> bool:
    """True for a task document in a plan tree: ``TASK-....md``."""
    name = str(path).replace("\\", "/").rsplit("/", 1)[-1]
    return name.startswith("TASK-") and name.lower().endswith(".md")


def _front_matter(text: str) -> dict[str, Any]:
    """The task document's front matter, tolerant of anything at all."""
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return {}
    body = stripped[3:]
    end = body.find("\n---")
    if end < 0:
        return {}
    try:
        loaded = yaml.safe_load(body[:end])
    except yaml.YAMLError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _task_id_of(front: Mapping[str, Any], path: str) -> str:
    declared = str(front.get("id") or "").strip()
    if declared:
        return declared
    name = str(path).replace("\\", "/").rsplit("/", 1)[-1]
    return name[:-3] if name.lower().endswith(".md") else name


def _route_shaped(text: str) -> list[str]:
    """Every route-shaped string in ``text``, by the route reader's own rule:
    a slash-led path longer than three characters with a letter in it.

    A closing markup tag (``</span>``, ``</time>``) is not one: the slash right
    after ``<`` put "/span" and "/time" on FEAT-7F19's card (15 October 2026)."""
    found: list[str] = []
    source = str(text or "")
    for hit in _ROUTE_PATH.finditer(source):
        match = hit.group(0)
        if hit.start() > 0 and source[hit.start() - 1] == "<":
            continue
        if len(match) > 3 and re.search(r"[A-Za-z]", match) and match not in found:
            found.append(match)
    return found


def _is_a_place_on_disk(path: str, test_roots: Sequence[str]) -> bool:
    """True when this slash-led string is a file under a test root, a
    documentation folder or one of the operating system's own folders, which
    is a place on disk and not a web address.

    The repository declares its test roots as folders — ``tests/health``,
    ``tests/users`` — so the folder they all sit in is what a path is compared
    against: ``/tests/users/test_analytics.py`` is a file, not an endpoint.
    ``/tmp/kcadm-`` and ``/dev/null`` in a setup script are files too.
    """
    # A shell default such as ``${TMPDIR:-/tmp}/kc-readiness-XXXXXX`` arrives
    # as ``/tmp}/...``: the closing brace is shell syntax, not part of the
    # folder name (FEAT-6AC9's card, 15 October 2026).
    first = path.lstrip("/").split("/", 1)[0].lower().rstrip("}")
    roots = {
        str(root).strip("/ ").lower().split("/", 1)[0]
        for root in test_roots
        if str(root).strip("/ ")
    }
    if first in roots or first in _DOC_FOLDERS or first in _SYSTEM_FOLDERS:
        return True
    lowered = "/" + path.strip("/").lower()
    return any(
        lowered == device or lowered.startswith(device + "/")
        for device in _DEVICE_FILES
    )


def review_task_traceability(
    files: Mapping[str, str],
    *,
    request_text: str,
    repository_facts: str | None = None,
    test_roots: Sequence[str] | None = None,
) -> TraceabilityReview:
    """Read every task in the plan tree against the request.

    ``files`` is the plan tree as it stands before anything is committed: a
    mapping of repository-relative path to content. ``request_text`` is the
    sentence the person sent, word for word. ``repository_facts`` is the short
    sheet of what the repository already does — a web address that is already
    there is not a web address this plan invented. ``test_roots`` are the
    repository's own declared test folders.

    Never raises. A document that cannot be read is named in ``unreadable``
    and the rest of the plan is still read.
    """
    review = TraceabilityReview()
    request = str(request_text or "")
    facts = str(repository_facts or "")
    request_routes = {route.rstrip("/").lower() for route in _route_shaped(request)}
    fact_routes = {route.rstrip("/").lower() for route in _route_shaped(facts)}
    roots = tuple(test_roots) if test_roots else _DEFAULT_TEST_ROOTS

    for path in sorted(files):
        if not _is_task_document(path):
            continue
        try:
            document = _LINT_BOILERPLATE.sub("", str(files[path] or ""))
            front = _front_matter(document)
            task_id = _task_id_of(front, path)
            review.tasks_read += 1
            _review_one_task(
                review,
                task_id=task_id,
                path=str(path),
                document=document,
                request=request,
                request_routes=request_routes,
                fact_routes=fact_routes,
                test_roots=roots,
            )
        except Exception as exc:  # noqa: BLE001 — a reader must never stop a run
            review.unreadable.append(f"{path} could not be read ({type(exc).__name__})")
    return review


def _review_one_task(
    review: TraceabilityReview,
    *,
    task_id: str,
    path: str,
    document: str,
    request: str,
    request_routes: set[str],
    fact_routes: set[str],
    test_roots: Sequence[str],
) -> None:
    """The two questions, asked of one whole task document."""
    # 1. Does it name a web address the request did not name?
    for route in _route_shaped(document):
        key = route.rstrip("/").lower()
        if key in request_routes or key in fact_routes:
            continue
        if _is_a_place_on_disk(route, test_roots):
            continue
        review.findings.append(
            TaskFinding(
                task_id=task_id,
                path=path,
                flag=CONTRADICTED_PATH,
                what=route,
                sentence=(
                    f"{task_id} answers at {route}, which the request does not "
                    "name"
                ),
            )
        )

    # 2. Does it ask for a capability the request never mentioned?
    for capability, pattern in _ALL_CAPABILITIES:
        if not pattern.search(document):
            continue
        if pattern.search(request):
            continue  # the person asked for it; a reading, not an addition
        review.findings.append(
            TaskFinding(
                task_id=task_id,
                path=path,
                flag=UNASKED_CAPABILITY,
                what=capability,
                sentence=(
                    f"{task_id} asks for {capability}, which the request never "
                    "mentions"
                ),
            )
        )
