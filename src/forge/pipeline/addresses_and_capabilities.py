"""The web addresses and capability words the merge card's scope report reads.

The scope report (:mod:`forge.pipeline.scope_report`) asks of a finished
build: does it answer at a web address the request never named, or add a
capability the request never asked for? These are the readers it uses for
both questions — what counts as a web address, which slash-led strings are
places on disk instead, and the list of capability classes.

WHERE THEY CAME FROM. They were written on 15 September 2026 for a check that
read a plan's task documents against the request before the plan was
committed (``forge.planning.task_traceability``). Nothing called that check
after plan review moved to the specialist seat's own semantic review on
17 September 2026, and it was deleted on 16 October 2026: a release gate that
day showed its two flags were false alarms most of the time. The scope report
was always the live reader of these helpers, so they moved here unchanged.

Pure: no input, no output, no model, never raises.
"""

from __future__ import annotations

import re
from typing import Sequence

from forge.planning.assumption_review import _CAPABILITIES
from forge.planning.repository_facts import _ROUTE_PATH

#: The capability classes a plan may not add on its own, beyond the ones the
#: spec-side reviewer already knows (authentication, permissions, rate
#: limiting, pagination, caching, a required header, a response wrapper — the
#: table imported above). These four are what the twelve-plan measurement
#: (15 September 2026) used.
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

#: Every capability class the scope report asks about: the spec side's table,
#: then the four above. One list, so the spec side and the merge card cannot
#: drift apart.
_ALL_CAPABILITIES: tuple[tuple[str, re.Pattern[str]], ...] = (
    *_CAPABILITIES,
    *_PLAN_ONLY_CAPABILITIES,
)

#: Folders whose files are not web addresses even when they are written with a
#: leading slash. A path under a documentation folder is a place on disk.
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


#: The characters that open a string literal in code, in every language.
_STRING_OPENERS: str = "\"'`"


def _route_shaped(text: str, *, opening_a_string: bool = False) -> list[str]:
    """Every route-shaped string in ``text``, by the route reader's own rule:
    a slash-led path longer than three characters with a letter in it.

    A closing markup tag (``</span>``, ``</time>``) is not one: the slash right
    after ``<`` put "/span" and "/time" on FEAT-7F19's card (15 October 2026).

    ``opening_a_string`` is for code a build wrote: a web address that code
    declares opens a string literal (``"/health"``, ``'/version'``), in any
    language. A slash in the middle of a string is a path being built, not an
    address: ``"${folder}/node_modules"`` in a check script and the import
    ``"./config/settings"`` were both read as addresses the build answers at,
    and the first cost FEAT-8AD7 its merge (step 1 measurement, 26 October
    2026)."""
    found: list[str] = []
    source = str(text or "")
    for hit in _ROUTE_PATH.finditer(source):
        match = hit.group(0)
        before = source[hit.start() - 1] if hit.start() > 0 else ""
        if before == "<":
            continue
        if opening_a_string and (not before or before not in _STRING_OPENERS):
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
