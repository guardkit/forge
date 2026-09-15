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
any of its tasks names a web address the request did not name, asks for a
capability the request never mentioned, or cannot point at the words of the
request it serves without being one of the four scaffolding kinds.**

Three flags, and they are not treated alike:

* ``CONTRADICTED_PATH`` — the task names a web address the request does not
  name. This is what got one run refused at the live gate an hour later.
* ``UNASKED_CAPABILITY`` — the task asks for something the request never
  mentioned: a login requirement, a database migration, a service module,
  error handling, logging. This is what got two more runs refused.
* ``CANNOT_CITE`` — the task cannot quote the request at all and claims no
  scaffolding kind. Nine of fifty-eight tasks drew this flag alone in the
  measurement and three or four of them were innocent, so **it never stops a
  run on its own**; it is said in ordinary words to the person instead.

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
    "CANNOT_CITE",
    "CONTRADICTED_PATH",
    "UNASKED_CAPABILITY",
    "SCAFFOLDING_KINDS",
    "TaskFinding",
    "TraceabilityReview",
    "excused_task_line",
    "review_task_traceability",
]

#: The task names a web address the request does not name — a contradiction of
#: something the person actually said.
CONTRADICTED_PATH = "CONTRADICTED_PATH"

#: The task asks for a capability the request never mentions — an addition.
UNASKED_CAPABILITY = "UNASKED_CAPABILITY"

#: The task cannot point at the words of the request it serves, and claims no
#: scaffolding kind. Never stops a run on its own.
CANNOT_CITE = "CANNOT_CITE"

#: The four kinds of task that are excused from quoting the request, and
#: nothing else ever is. They are excused from QUOTING only: a scaffolding task
#: that names a web address the request did not name, or asks for a capability
#: nobody asked for, is flagged exactly like any other task.
#:
#: ``tests`` and ``documentation`` are the fourteen test tasks and seven
#: documentation tasks of the fifty-eight measured, not one of which could
#: quote the sentence. ``response-shape`` is a task that only defines the shape
#: of the answer for the endpoint the sentence names — the sentence says
#: "returns the number of users created", it does not say "Pydantic".
#: ``data-access`` is the database query behind the named endpoint and nothing
#: else. There is deliberately no error-handling kind: the task that produced
#: an application-wide rewrite said "for the daily counts endpoint" in its own
#: body, so it was already scoped in words and an allowlist keyed on wording
#: would have waved it straight through. Error handling is on the capability
#: list below instead.
SCAFFOLDING_KINDS: tuple[str, ...] = ("tests", "documentation", "response-shape", "data-access")

#: What a task document written under the new shape says, under the quote
#: heading, when it genuinely cannot quote the request. The heading is never
#: omitted; this line, and nothing else, goes under it.
_EXCUSED_TASK_LINE = "_none — this is a {kind} task_"


def excused_task_line(kind: str) -> str:
    """The one line an excused task writes under the quote heading."""
    return _EXCUSED_TASK_LINE.format(kind=kind)


#: Task documents written before ``scaffolding_kind`` existed — every plan on
#: every planning branch today — say the same thing in the older
#: ``task_type:`` key the plan writer has always written. A test task is a test
#: task whichever key names it, so the two older values are read as the two
#: matching kinds. A document carrying ``scaffolding_kind`` is read from that
#: key alone.
_TASK_TYPE_AS_KIND: dict[str, str] = {"testing": "tests", "documentation": "documentation"}

#: The heading a task writes the words of the request under.
_QUOTE_HEADING = "## The words of the request this task serves"

#: Boilerplate, and it must go before anyone asks whether a task quoted the
#: request — otherwise every task looks as though it said something. This
#: sentence appears in forty-nine of the fifty-eight task documents measured.
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

#: How many consecutive words of the request count as quoting it.
_QUOTE_WINDOW = 3


#: Small numbers written the way a person writes them. A card that opens
#: "1 task(s)" is machine talk; one that opens "One task" is a sentence.
_IN_WORDS: tuple[str, ...] = (
    "Zero",
    "One",
    "Two",
    "Three",
    "Four",
    "Five",
    "Six",
    "Seven",
    "Eight",
    "Nine",
    "Ten",
)


def _in_words(how_many: int) -> str:
    """``3`` as ``"Three"``. Anything past ten keeps its digits, because
    "Seventeen" on a card reads worse than "17"."""
    if 0 <= how_many < len(_IN_WORDS):
        return _IN_WORDS[how_many]
    return str(how_many)


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
    def cannot_cite(self) -> list[TaskFinding]:
        return [finding for finding in self.findings if finding.flag == CANNOT_CITE]

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
        """True when this plan should be rewritten once: any finding at all,
        including a task that cannot quote the request — the writer is asked
        to fix everything it can in the one round it gets."""
        return bool(self.findings)

    @property
    def stops_the_run(self) -> bool:
        """True when a plan that has ALREADY been rewritten once still
        contradicts the request. A task that merely cannot quote the request
        never stops a run."""
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
            "Rewrite the plan so that every task points at the words of the "
            "request it serves, answers at the web address the request names "
            "and no other, and adds no capability the request does not "
            "mention. A plan may choose between readings of what was asked; "
            "it may not add. Change nothing else."
        )
        return "\n".join(lines)

    def cannot_cite_line(self) -> str | None:
        """The one plain line a person reads when tasks quote nothing from the
        request. ``None`` when every task could quote it.

        Written the way a person writes it: "One task ... does" or "Three
        tasks ... do", and the plain truth about what happened, which is that
        the run carried on. The earlier wording said the plan "was not sent
        back for that on its own", and that was machine talk and untrue as
        well: any finding at all opens the one note round the plan writer
        gets. What is true, and what a person needs to know, is that the run
        was not STOPPED for it.
        """
        findings = self.cannot_cite
        if not findings:
            return None
        names = ", ".join(finding.task_id for finding in findings)
        how_many = _in_words(len(findings))
        if len(findings) == 1:
            said = (
                f"{how_many} task in this plan does not quote any of the "
                "words of the request it serves"
            )
        else:
            said = (
                f"{how_many} tasks in this plan do not quote any of the "
                "words of the request they serve"
            )
        return f"{said}: {names}. The run was not stopped for that on its own."

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


def _scaffolding_kind_of(front: Mapping[str, Any]) -> str | None:
    """The kind this task claims, or ``None``.

    The new ``scaffolding_kind`` key when the document carries one; otherwise
    the older ``task_type`` where it names a test or documentation task, which
    is how every plan written before this rule existed says the same thing.
    Anything outside the four kinds is not a kind — it is never invented, and
    a task claiming one the list does not have is simply not excused.
    """
    declared = str(front.get("scaffolding_kind") or "").strip().lower()
    if declared:
        return declared if declared in SCAFFOLDING_KINDS else None
    older = str(front.get("task_type") or "").strip().lower()
    return _TASK_TYPE_AS_KIND.get(older)


def _section(text: str, heading: str) -> str:
    """The body of one ``##`` section, or the empty string when absent."""
    lowered = text.lower()
    at = lowered.find(heading.lower())
    if at < 0:
        return ""
    rest = text[at + len(heading) :]
    nxt = re.search(r"^#{1,6} ", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def _the_words_quoted(text: str) -> str:
    """The block quote the task wrote under the quote heading, its ``>`` lines
    joined — or the empty string when it wrote no quote there at all.

    The excused line — ``_none — this is a tests task_`` — is a declaration
    that there is nothing to quote, not a quote, so it yields nothing here;
    whether that task is excused is the scaffolding question, asked next.
    """
    body = _section(text, _QUOTE_HEADING).strip()
    if not body:
        return ""
    for kind in SCAFFOLDING_KINDS:
        if body == excused_task_line(kind):
            return ""
    if re.fullmatch(r"_none.*_", body, re.IGNORECASE | re.DOTALL):
        return ""
    quoted = [
        line.strip().lstrip(">").strip()
        for line in body.splitlines()
        if line.strip().startswith(">")
    ]
    return "\n".join(part for part in quoted if part)


def _quotes_the_request_in_its_own_section(
    text: str, request_words: Sequence[str]
) -> bool:
    """True when the words the task quoted under the heading really are the
    request's own words.

    Rule 1 of the task document's shape says that section holds "words copied
    from the request". As first built, this counted ANY block quote under the
    heading, so a task that paraphrased the request in its own words passed
    the check while quoting nothing — which is the one thing the section
    exists to prevent. Found in the coordinator's own review of the build and
    fixed here: the quoted words count only when at least three consecutive
    words of them appear in the request, read with this module's own
    normalisation, so punctuation and capitals never decide it.

    A quote that is not the request's words is no quote at all: the task then
    falls through to the scaffolding-kind and three-consecutive-words
    questions exactly as if it had written no section.
    """
    quoted = _the_words_quoted(text)
    if not quoted:
        return False
    return _quotes_three_consecutive_words(quoted, request_words)


def _words(text: str) -> list[str]:
    """The text as plain lower-case words, punctuation gone.

    ``GET /users/created-per-day`` and ``get users created per day`` are the
    same words here on purpose: a task that repeats the web address has
    repeated the request's words, however it punctuated them.
    """
    return re.findall(r"[a-z0-9]+", str(text or "").lower())


def _quotes_three_consecutive_words(document: str, request_words: Sequence[str]) -> bool:
    """True when three consecutive words of the request appear in the task."""
    if len(request_words) < _QUOTE_WINDOW:
        return False
    task_words = _words(document)
    if len(task_words) < _QUOTE_WINDOW:
        return False
    windows = {
        tuple(task_words[i : i + _QUOTE_WINDOW])
        for i in range(len(task_words) - _QUOTE_WINDOW + 1)
    }
    for i in range(len(request_words) - _QUOTE_WINDOW + 1):
        if tuple(request_words[i : i + _QUOTE_WINDOW]) in windows:
            return True
    return False


def _route_shaped(text: str) -> list[str]:
    """Every route-shaped string in ``text``, by the route reader's own rule:
    a slash-led path longer than three characters with a letter in it."""
    found: list[str] = []
    for match in _ROUTE_PATH.findall(str(text or "")):
        if len(match) > 3 and re.search(r"[A-Za-z]", match) and match not in found:
            found.append(match)
    return found


def _without(document: str, routes: Sequence[str]) -> str:
    """The task document with the web addresses it invented taken out.

    Only the addresses this task was flagged for naming — a web address the
    request does not name and the repository does not already have. Asked
    whether the task quotes the request, a document keeping them would answer
    yes on its own invention: the plan that moved the endpoint to
    ``/stats/users-created-per-day`` shares the words "users created per" with
    the request only because it renamed the request's own address, and those
    are not the request's words. Nothing else is removed.
    """
    trimmed = str(document or "")
    for route in routes:
        trimmed = trimmed.replace(route, " ")
    return trimmed


def _is_a_place_on_disk(path: str, test_roots: Sequence[str]) -> bool:
    """True when this slash-led string is a file under a test root or a
    documentation folder, which is a place on disk and not a web address.

    The repository declares its test roots as folders — ``tests/health``,
    ``tests/users`` — so the folder they all sit in is what a path is compared
    against: ``/tests/users/test_analytics.py`` is a file, not an endpoint.
    """
    first = path.lstrip("/").split("/", 1)[0].lower()
    roots = {
        str(root).strip("/ ").lower().split("/", 1)[0]
        for root in test_roots
        if str(root).strip("/ ")
    }
    return first in roots or first in _DOC_FOLDERS


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
    request_words = _words(request)
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
                front=front,
                request=request,
                request_words=request_words,
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
    front: Mapping[str, Any],
    request: str,
    request_words: Sequence[str],
    request_routes: set[str],
    fact_routes: set[str],
    test_roots: Sequence[str],
) -> None:
    """The three questions, asked of one whole task document."""
    # 1. Does it name a web address the request did not name?
    contradicted: list[str] = []
    for route in _route_shaped(document):
        key = route.rstrip("/").lower()
        if key in request_routes or key in fact_routes:
            continue
        if _is_a_place_on_disk(route, test_roots):
            continue
        contradicted.append(route)
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

    # 3. Can it point at the words of the request it serves?
    if _quotes_the_request_in_its_own_section(document, request_words):
        return
    kind = _scaffolding_kind_of(front)
    if kind is not None:
        return
    if _quotes_three_consecutive_words(
        _without(document, contradicted), request_words
    ):
        return
    # A task that wrote a quote which is not the request's words is told
    # exactly that, because it is a different mistake from writing nothing and
    # a different thing to put right.
    wrote_a_quote = bool(_the_words_quoted(document))
    kinds = ", ".join(SCAFFOLDING_KINDS)
    sentence = (
        f"{task_id} quotes words that are not in the request, and claims no "
        f"scaffolding kind (one of: {kinds})"
        if wrote_a_quote
        else (
            f"{task_id} quotes nothing from the request and claims no "
            f"scaffolding kind (one of: {kinds})"
        )
    )
    review.findings.append(
        TaskFinding(
            task_id=task_id,
            path=path,
            flag=CANNOT_CITE,
            what="",
            sentence=sentence,
        )
    )
