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

The sweep asks the factory's own model through GuardKit's own client
(``guardkit.orchestrator.stamp_model_fallback.ConfiguredAsker``), built from
the sweep's own three settings, ``FORGE_SWEEP_MODEL_URL``,
``FORGE_SWEEP_MODEL`` and ``FORGE_SWEEP_MODEL_EXTRA_BODY``, passed as
arguments (:func:`default_asker`). No new model client and no new
credential; the process environment is never changed. When the address or
the name is empty, nothing is asked and the findings stay ``pending``,
which the Slack line says.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Callable, Iterable, Mapping, Sequence

from .findings import KIND_CODE, Finding

logger = logging.getLogger(__name__)

#: The sweep's own model settings (fix pass, 17 October 2026). They are the
#: sweep's alone: GuardKit's GUARDKIT_STAMP_MODEL* names would also switch on
#: the plan's stamp check in the coordinator, so the sweep never uses them.
MODEL_URL_NAME = "FORGE_SWEEP_MODEL_URL"
MODEL_NAME = "FORGE_SWEEP_MODEL"
MODEL_EXTRA_BODY_NAME = "FORGE_SWEEP_MODEL_EXTRA_BODY"

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


def _extra_body(raw: str) -> dict[str, object]:
    """The optional extra request fields, or ``{}`` when unset or not a JSON object."""
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        value = None
    if not isinstance(value, dict):
        # The value is never logged: an operator could put something private in it.
        logger.warning("security sweep: %s is not a JSON object; the model is asked without it", MODEL_EXTRA_BODY_NAME)
        return {}
    return value


def default_asker(environ: Mapping[str, str] | None = None) -> tuple[Asker | None, str]:
    """``(asker, why not)`` from the sweep's own three settings.

    ``FORGE_SWEEP_MODEL_URL`` (the OpenAI-compatible ``/v1`` root),
    ``FORGE_SWEEP_MODEL`` (the name the router serves) and the optional
    ``FORGE_SWEEP_MODEL_EXTRA_BODY`` (a JSON object). They are handed to
    GuardKit's own client as constructor arguments; the process environment
    is read, never changed, so nothing else in the coordinator — the plan's
    stamp check above all — starts asking a model because the sweep was
    configured. An empty or missing address or name means not configured:
    nothing is asked and nothing falls back to ``OPENAI_BASE_URL``.

    ``why not`` is plain words; never the address, never a key.
    """
    env = os.environ if environ is None else environ
    url = str(env.get(MODEL_URL_NAME) or "").strip()
    model = str(env.get(MODEL_NAME) or "").strip()
    if not url or not model:
        return None, "the model is not configured"
    try:
        from guardkit.orchestrator import stamp_model_fallback as client
    except Exception:  # noqa: BLE001 — an older or absent GuardKit means no model
        return None, "the model route is not available in this image"
    try:
        asker = client.ConfiguredAsker(
            url,
            model,
            float(getattr(client, "DEFAULT_TIMEOUT_S", 60.0)),
            extra_body=_extra_body(str(env.get(MODEL_EXTRA_BODY_NAME) or "")),
        )
    except Exception:  # noqa: BLE001 — a bad setting means no model, said plainly
        return None, "the model route's settings could not be read"
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
