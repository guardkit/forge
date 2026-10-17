"""Asking the factory's own model whether a secret or code finding is real.

Public surface
==============

- :func:`build_prompt` — the question for one finding: its metadata, and for
  a code finding the redacted excerpt, asking for ``{"real": true|false}``.
- :func:`read_answer` — the model's reply reduced to one yes or no, or
  ``None`` when the reply is not a clean answer.
- :func:`excerpt_lines` / :func:`redacted_excerpt` — the code around a code
  finding, with every line that may hold a secret replaced.
- :func:`default_asker` — the coordinator's existing model route.

The redaction boundary (design step 3, Codex R3)
------------------------------------------------

- **A secret finding** is sent as metadata only: tool, rule, file, line and
  whether the path is a test, documentation or a lockfile. Its surrounding
  source is never read, stored or sent.
- **A code finding** is sent with its lines and 20 either side. Before that,
  the excerpt is passed through ``gitleaks stdin --redact``, and every line it
  flags, and every line the full-tree secret scan flagged in that file, is
  replaced with ``[line hidden: may hold a secret]``. If that check cannot
  run, no excerpt is sent at all. The excerpt exists only in memory, for one
  question, and is never stored or logged.
- **The model's own words never leave the process.** The answer is reduced to
  a yes or no here; nothing else of it is kept, logged or repeated.

The model route
---------------

The sweep asks the model the coordinator already asks during planning: the
stamp check's model, named by ``GUARDKIT_STAMP_MODEL`` and reached at
``GUARDKIT_STAMP_MODEL_URL`` (or ``OPENAI_BASE_URL``) with ``OPENAI_API_KEY``,
through GuardKit's own client (``guardkit.orchestrator.stamp_model_fallback``).
No new model client and no new credential. When that route is not configured,
nothing is asked and the findings stay ``pending``, which the Slack line says.
"""

from __future__ import annotations

import json
import os
import re
from typing import Callable, Iterable, Sequence

from .findings import KIND_CODE, Finding

#: The model address name the sweep reads (GuardKit's stamp-check client).
MODEL_URL_NAME = "GUARDKIT_STAMP_MODEL_URL"

#: Lines either side of a code finding in its excerpt.
EXCERPT_CONTEXT = 20

#: What a line that may hold a secret is replaced with.
HIDDEN_LINE = "[line hidden: may hold a secret]"

#: ``(prompt) -> answer text`` — the injected model call (synchronous, like
#: GuardKit's own).
Asker = Callable[[str], str]


def excerpt_lines(data: bytes, start: int, end: int) -> tuple[int, list[str]]:
    """``(first line number, lines)`` of the excerpt around lines start..end."""
    lines = data.split(b"\n")
    first = max(1, start - EXCERPT_CONTEXT)
    last = min(len(lines), max(end, start) + EXCERPT_CONTEXT)
    chosen = [
        line.rstrip(b"\r").decode("utf-8", errors="replace") for line in lines[first - 1 : last]
    ]
    return first, chosen


def redacted_excerpt(
    first: int, lines: Sequence[str], hidden: Iterable[int]
) -> str:
    """The excerpt as numbered text, with every line in ``hidden`` replaced.

    ``hidden`` holds file line numbers.
    """
    hide = set(hidden)
    out = []
    for offset, text in enumerate(lines):
        number = first + offset
        out.append(f"{number}: {HIDDEN_LINE if number in hide else text}")
    return "\n".join(out)


def build_prompt(finding: Finding, excerpt: str | None) -> str:
    """The question for one finding, built from allowlisted fields."""
    facts = [
        f"tool: {finding.tool}",
        f"rule: {finding.rule}",
        f"file: {finding.file}",
        f"line: {finding.line}",
    ]
    if finding.message:
        facts.append(f"what the rule looks for: {finding.message}")
    if finding.severity:
        facts.append(f"rule severity: {finding.severity}")
    kinds = ", ".join(finding.path_kinds) if finding.path_kinds else "ordinary source"
    facts.append(f"the path is: {kinds}")
    if finding.kind == KIND_CODE:
        question = (
            "A security code scanner matched this code. Decide whether it is a "
            "real security problem in this project, or a false alarm (for "
            "example a pattern that is safe in its context, test-only code, or "
            "documentation)."
        )
    else:
        question = (
            "A secret scanner matched a line in this project. The line itself is "
            "not shown. Decide from the rule and where the file sits whether it "
            "is likely a real credential, or a false alarm (for example a "
            "documentation example, a test fixture or a lockfile checksum)."
        )
    body = [question, "", *facts]
    if excerpt:
        body += [
            "",
            "The code around it (lines that may hold a secret are hidden):",
            excerpt,
        ]
    body += [
        "",
        'Answer with JSON only, exactly one object: {"real": true} or {"real": false}.',
    ]
    return "\n".join(body)


_THINKING = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_OBJECT = re.compile(r"\{[^{}]*\}")


def read_answer(text: object) -> bool | None:
    """The reply as True/False, or None when it is not one clean answer.

    The reply may wrap the JSON in thinking or a code fence; the answer is
    the last object that carries a boolean ``real``. Two objects that
    disagree, or none, is not an answer.
    """
    if not isinstance(text, str):
        return None
    plain = _THINKING.sub("", text)
    answers: list[bool] = []
    for match in _OBJECT.finditer(plain):
        try:
            loaded = json.loads(match.group(0))
        except ValueError:
            continue
        if isinstance(loaded, dict) and isinstance(loaded.get("real"), bool):
            answers.append(bool(loaded["real"]))
    if not answers or len(set(answers)) != 1:
        return None
    return answers[-1]


def default_asker() -> tuple[Asker | None, str]:
    """``(asker, why not)`` for the coordinator's existing model route.

    ``why not`` is plain words when there is no asker ("the model is not
    configured"); never the endpoint, never a key.
    """
    # An address name that is present but empty means "not configured", and
    # never falls through to OPENAI_BASE_URL. GuardKit's own rule says the
    # same (empty_env_disables, checked on 17 October 2026); it is said again
    # here so the sweep does not depend on that staying true. The estate
    # passes FORGE_SWEEP_MODEL_URL through as GUARDKIT_STAMP_MODEL_URL, empty
    # when it is not set.
    if os.environ.get(MODEL_URL_NAME) is not None and not os.environ[MODEL_URL_NAME].strip():
        return None, "the model is not configured"
    try:
        from guardkit.orchestrator.stamp_model_fallback import build_default_asker
    except Exception:  # noqa: BLE001 — an older or absent GuardKit means no model
        return None, "the model route is not available in this image"
    try:
        asker = build_default_asker()
    except Exception:  # noqa: BLE001 — a bad setting means no model, said plainly
        return None, "the model route's settings could not be read"
    if asker is None:
        return None, "the model is not configured"
    return asker, ""


__all__ = [
    "Asker",
    "EXCERPT_CONTEXT",
    "HIDDEN_LINE",
    "build_prompt",
    "default_asker",
    "excerpt_lines",
    "read_answer",
    "redacted_excerpt",
]
