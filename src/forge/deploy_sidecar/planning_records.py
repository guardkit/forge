"""Keep a copy of every planning step the sandbox helper runs (1 November 2026).

WHY. Since planning moved into each project's sandbox (28 October 2026), the
helper writes Forge's request to a file, runs one planner container, reads its
``result.json`` and then removes the request file and the planner's output
folder, the planner's trace included. Forge records neither the request nor
the reply. Before that move a request could be read back off the message bus
for a day, and those copies are what the planner replays are built from. On
1 November 2026 three real plans could not be replayed because nothing of
their requests was left.

WHAT IS KEPT. One folder per step, under the project's receipts folder, which
inside a sandbox is the folder the Dell's own receipts for that project live
in (``FORGE_RECEIPTS_DIR``, bound read-write into the sandbox at the same
path), so it can be read from the Dell with nothing else running::

    <receipts>/planning/<UTC time>-<run id>-<step>-<8 hex>/
        request-body.json   the helper request, byte for byte as Forge posted it
        request.json        the file the planner read (Forge's command)
        output/             the planner's whole output folder: result.json, trace
        reply.json          what the helper answered Forge
        step.json           a short summary: who, what, when, how it ended

The folder is written under a ``.partial`` name and renamed when complete, so
a reader never sees half of one.

HOW LONG. Folders older than ``FORGE_PLANNING_RECORDS_DAYS`` days (default 14)
are removed each time a new one is kept. Only folders this module named are
ever removed.

NO SECRETS. Forge's request carries the run's own text: the request, the
project's documents, the spec and plan facts, ids and a time budget. It carries
no credential by design, and none of the helper's settings (the planner's own
settings file is never kept). As a guard, any field whose NAME says it is a
credential, and any value of an unmistakable credential shape (a provider key,
a GitHub or Slack token, a private key, a password inside an address), is
replaced before anything is written, and ``step.json`` lists each replacement
so a replay knows its input was changed. The planner's output files get the
same check: JSON and JSON-per-line files are read and checked value by value,
other text by shape. A file that needed nothing keeps its original bytes. Nothing here refuses or stops a step: it only keeps
records, and a record that cannot be kept is logged and the step goes on.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import shutil
import stat
from pathlib import Path
from typing import Any, Mapping

from forge.receipts import RECEIPTS_DIR_ENV

logger = logging.getLogger(__name__)

#: The folder under the receipts root that holds the planning records.
PLANNING_RECORDS_DIRNAME: str = "planning"

#: The one setting: how many days a planning record is kept.
PLANNING_RECORDS_DAYS_ENV: str = "FORGE_PLANNING_RECORDS_DAYS"
PLANNING_RECORDS_DAYS_DEFAULT: int = 14

#: How a record folder's name starts: its UTC time, to the second.
_STAMP_FORMAT: str = "%Y%m%dT%H%M%SZ"
_RECORD_NAME = re.compile(r"^(\d{8}T\d{6}Z)-[A-Za-z0-9._-]+$")
_PARTIAL_SUFFIX: str = ".partial"

#: Field names that say the field is a credential: one of these words as a
#: whole part of the name (split on ``_``, ``-``, ``.`` and case changes), so
#: ``api_key`` and ``authToken`` match and ``max_tokens`` does not.
_CREDENTIAL_NAME_WORDS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "apikey",
        "authorization",
        "credential",
        "credentials",
        "cookie",
        "dsn",
    }
)
_CREDENTIAL_NAME_PAIRS: frozenset[tuple[str, str]] = frozenset(
    {("api", "key"), ("private", "key"), ("access", "key"), ("secret", "key")}
)

#: Values of an unmistakable credential shape. Kept narrow on purpose: the
#: request is mostly a project's own documents, and replacing ordinary text
#: (a commit id, the word "Bearer" in a README) would spoil the replay this
#: record exists for.
#:
#: Where a shape starts: not inside a word, or straight after a written escape
#: such as ``\n`` or ``\t``. A plain word boundary is not enough, because in
#: serialized JSON a token at the start of a line follows the letter ``n`` of
#: ``\n`` (review finding, 1 November 2026).
_START: str = r"(?:(?<=\\[A-Za-z])|(?<![A-Za-z0-9_]))"
_CREDENTIAL_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(_START + r"sk-(?:proj-)?[A-Za-z0-9_\-]{32,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(_START + r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(_START + r"glpat-[A-Za-z0-9_\-]{20,}"),
    re.compile(_START + r"xox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(_START + r"AKIA[0-9A-Z]{16}(?![0-9A-Z])"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)",
        re.S,
    ),
    re.compile(_START + r"Bearer [A-Za-z0-9._\-]{20,}"),
)
#: A password inside an address (``scheme://user:password@host``): the scheme
#: and user are kept, the password replaced. The scheme may follow a written
#: escape, which then stays in front of it untouched.
_URL_WITH_LOGIN = re.compile(
    _START + r"([A-Za-z][A-Za-z0-9+.\-]*://[^\s:/?#@\"'\\]*):[^\s/?#@\"'\\]+@"
)
REDACTED: str = "***REDACTED***"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def planning_records_root(env: Mapping[str, str]) -> Path | None:
    """``<receipts>/planning``, or ``None`` when no receipts folder is named.

    Inside a sandbox the runner script always names one (it exports
    ``FORGE_RECEIPTS_DIR``), so ``None`` means a helper started some other way.
    """
    named = str(env.get(RECEIPTS_DIR_ENV) or "").strip()
    if not named:
        return None
    return Path(named).expanduser() / PLANNING_RECORDS_DIRNAME


def planning_records_days(env: Mapping[str, str]) -> int:
    """The days a record is kept: the setting when it is a positive whole
    number, otherwise the default (said in the log, never refused)."""
    raw = str(env.get(PLANNING_RECORDS_DAYS_ENV) or "").strip()
    if not raw:
        return PLANNING_RECORDS_DAYS_DEFAULT
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    logger.warning(
        "forge-deploy-sidecar: %s=%r is not a positive whole number of days; "
        "planning records are kept for %d days",
        PLANNING_RECORDS_DAYS_ENV,
        raw,
        PLANNING_RECORDS_DAYS_DEFAULT,
    )
    return PLANNING_RECORDS_DAYS_DEFAULT


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


def _name_says_secret(name: str) -> bool:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    words = [word for word in re.split(r"[_\-.\s]+", spaced.lower()) if word]
    if any(word in _CREDENTIAL_NAME_WORDS for word in words):
        return True
    return any(pair in _CREDENTIAL_NAME_PAIRS for pair in zip(words, words[1:]))


def redact_text(text: str) -> tuple[str, int]:
    """``text`` with credential-shaped values replaced, and how many were."""
    count = 0
    for pattern in _CREDENTIAL_VALUE_PATTERNS:
        text, found = pattern.subn(REDACTED, text)
        count += found
    text, found = _URL_WITH_LOGIN.subn(lambda m: f"{m.group(1)}:{REDACTED}@", text)
    return text, count + found


def redact_json(value: Any, where: str = "") -> tuple[Any, list[str]]:
    """A copy of ``value`` with secret-named fields and credential-shaped
    strings replaced, and where each replacement was made."""
    found: list[str] = []
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            here = f"{where}/{key}"
            if _name_says_secret(str(key)) and item not in (None, "", [], {}):
                out[key] = REDACTED
                found.append(here)
                continue
            out[key], inner = redact_json(item, here)
            found.extend(inner)
        return out, found
    if isinstance(value, list):
        items: list[Any] = []
        for index, item in enumerate(value):
            clean, inner = redact_json(item, f"{where}[{index}]")
            items.append(clean)
            found.extend(inner)
        return items, found
    if isinstance(value, str):
        clean, count = redact_text(value)
        if count:
            found.append(where or "/")
        return clean, found
    return value, found


# ---------------------------------------------------------------------------
# Keeping and removing
# ---------------------------------------------------------------------------


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def record_folder_name(now: _dt.datetime, container: str) -> str:
    """``<UTC time>-<run id>-<step>-<8 hex>``, from the container's name."""
    stem = container.removeprefix("forge-planning-")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-.") or "step"
    return f"{now.astimezone(_dt.timezone.utc).strftime(_STAMP_FORMAT)}-{stem}"


def _write_json_or_bytes(
    path: Path, *, exact: bytes | None, value: Any
) -> tuple[list[str], bool]:
    """Write ``exact`` when nothing in ``value`` needs replacing, otherwise the
    redacted ``value``. Answers the replacements and whether it was exact."""
    clean, found = redact_json(value)
    if exact is not None and not found:
        path.write_bytes(exact)
        return found, True
    path.write_text(json.dumps(clean, ensure_ascii=False), encoding="utf-8")
    return found, False


def _redact_json_text(text: str) -> str | None:
    """``text`` as JSON with :func:`redact_json` applied: the original text
    when nothing needed replacing, the re-written JSON when something did, or
    ``None`` when ``text`` is not one JSON document."""
    try:
        value = json.loads(text)
    except ValueError:
        return None
    clean, found = redact_json(value)
    if not found:
        return text
    ending = "\n" if text.endswith("\n") else ""
    return json.dumps(clean, ensure_ascii=False) + ending


def _redact_json_lines(text: str) -> str | None:
    """``text`` as one JSON document per line, each line through
    :func:`redact_json`; a line that needed nothing is kept as it was.
    ``None`` when any non-blank line is not JSON, or there are none."""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    seen = False
    for line in lines:
        body = line.rstrip("\r\n")
        if not body.strip():
            out.append(line)
            continue
        seen = True
        try:
            value = json.loads(body)
        except ValueError:
            return None
        clean, found = redact_json(value)
        if found:
            out.append(json.dumps(clean, ensure_ascii=False) + line[len(body):])
        else:
            out.append(line)
    return "".join(out) if seen else None


def redact_output_text(text: str) -> str:
    """One planner output file's text with credentials replaced.

    JSON, or JSON with one document per line, is read and every value checked
    the way the request is (credential-named fields and credential-shaped
    strings, on the decoded text); anything else goes through
    :func:`redact_text`. Text that needed nothing comes back unchanged, byte
    for byte."""
    try:
        for reader in (_redact_json_text, _redact_json_lines):
            answer = reader(text)
            if answer is not None:
                return answer
    except (RecursionError, ValueError, TypeError):
        pass
    return redact_text(text)[0]


def _copy_output(source: Path, dest: Path) -> dict[str, Any]:
    """Copy the planner's output folder: regular files only (a link is named,
    not followed), each text file through :func:`redact_output_text`."""
    copied = 0
    total = 0
    skipped: list[str] = []
    redacted: list[str] = []
    dest.mkdir(parents=True, exist_ok=True)
    if not source.is_dir():
        return {"files": 0, "bytes": 0, "skipped": [], "redacted": []}
    for folder, dirs, files in os.walk(source, followlinks=False):
        here = Path(folder)
        relative_folder = here.relative_to(source)
        for name in sorted(dirs):
            if (here / name).is_symlink():
                skipped.append(str(relative_folder / name))
        for name in sorted(files):
            path = here / name
            relative = relative_folder / name
            try:
                info = path.lstat()
            except OSError:
                skipped.append(str(relative))
                continue
            if not stat.S_ISREG(info.st_mode):
                skipped.append(str(relative))
                continue
            try:
                data = path.read_bytes()
            except OSError:
                skipped.append(str(relative))
                continue
            target = dest / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = None
            if text is not None:
                clean = redact_output_text(text)
                if clean != text:
                    redacted.append(str(relative))
                    data = clean.encode("utf-8")
            target.write_bytes(data)
            copied += 1
            total += len(data)
    return {"files": copied, "bytes": total, "skipped": skipped, "redacted": redacted}


def prune_planning_records(
    root: Path, *, days: int, now: _dt.datetime | None = None
) -> list[str]:
    """Remove the record folders under ``root`` older than ``days`` days, by
    the time in their names; anything else under ``root`` is left alone."""
    cutoff = (now or _now()) - _dt.timedelta(days=days)
    removed: list[str] = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return removed
    for entry in entries:
        name = entry.name.removesuffix(_PARTIAL_SUFFIX)
        match = _RECORD_NAME.match(name)
        if match is None or entry.is_symlink() or not entry.is_dir():
            continue
        try:
            stamp = _dt.datetime.strptime(match.group(1), _STAMP_FORMAT).replace(
                tzinfo=_dt.timezone.utc
            )
        except ValueError:
            continue
        if stamp < cutoff:
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry.name)
    return removed


def keep_planning_step(
    *,
    env: Mapping[str, str],
    container: str,
    payload: Mapping[str, Any],
    raw_body: bytes | None,
    request_path: Path,
    output_path: Path,
    status: int,
    reply: Mapping[str, Any],
    now: _dt.datetime | None = None,
) -> Path | None:
    """Keep one planning step; answer where, or ``None``. Never raises.

    Called after the step ends and before its request file and output folder
    are removed.
    """
    root = planning_records_root(env)
    if root is None:
        logger.warning(
            "forge-deploy-sidecar: no %s is set, so the planning step %s is "
            "not kept",
            RECEIPTS_DIR_ENV,
            container,
        )
        return None
    at = now or _now()
    final = root / record_folder_name(at, container)
    partial = final.with_name(final.name + _PARTIAL_SUFFIX)
    try:
        partial.mkdir(parents=True, exist_ok=False)
        body_redacted, body_exact = _write_json_or_bytes(
            partial / "request-body.json", exact=raw_body, value=dict(payload)
        )
        request = payload.get("request")
        try:
            planner_read = request_path.read_bytes()
        except OSError:
            planner_read = None
        request_redacted, _ = _write_json_or_bytes(
            partial / "request.json", exact=planner_read, value=request
        )
        output = _copy_output(output_path, partial / "output")
        reply_redacted, _ = _write_json_or_bytes(
            partial / "reply.json", exact=None, value=dict(reply)
        )
        args = request.get("args") if isinstance(request, Mapping) else None
        step = {
            "kept_at": at.astimezone(_dt.timezone.utc).isoformat(),
            "repo": payload.get("repo"),
            "build": payload.get("build"),
            "role": payload.get("role"),
            "command": request.get("command") if isinstance(request, Mapping) else None,
            "feature_id": args.get("feature_id") if isinstance(args, Mapping) else None,
            "start_commit": payload.get("start_commit"),
            "memory_project": payload.get("memory_project"),
            "container": container,
            "status": status,
            "exit_code": reply.get("exit_code"),
            "timed_out": reply.get("timed_out"),
            "cancelled": bool(reply.get("cancelled")),
            "has_result": isinstance(reply.get("result"), Mapping),
            "request_body_as_posted": body_exact,
            "redacted": {
                "request-body.json": body_redacted,
                "request.json": request_redacted,
                "reply.json": reply_redacted,
                "output": output["redacted"],
            },
            "output": {
                "files": output["files"],
                "bytes": output["bytes"],
                "not_copied": output["skipped"],
            },
        }
        (partial / "step.json").write_text(
            json.dumps(step, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        partial.rename(final)
    except Exception as exc:  # noqa: BLE001 — a record never fails the step
        logger.warning(
            "forge-deploy-sidecar: the planning step %s could not be kept under "
            "%s: %s: %s",
            container,
            root,
            type(exc).__name__,
            exc,
        )
        shutil.rmtree(partial, ignore_errors=True)
        return None
    try:
        removed = prune_planning_records(root, days=planning_records_days(env), now=at)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "forge-deploy-sidecar: old planning records under %s could not be "
            "removed: %s: %s",
            root,
            type(exc).__name__,
            exc,
        )
        removed = []
    if removed:
        logger.info(
            "forge-deploy-sidecar: removed %d planning record(s) older than %d "
            "days under %s",
            len(removed),
            planning_records_days(env),
            root,
        )
    logger.info("forge-deploy-sidecar: kept the planning step %s at %s", container, final)
    return final


__all__ = [
    "PLANNING_RECORDS_DAYS_DEFAULT",
    "PLANNING_RECORDS_DAYS_ENV",
    "PLANNING_RECORDS_DIRNAME",
    "REDACTED",
    "keep_planning_step",
    "planning_records_days",
    "planning_records_root",
    "prune_planning_records",
    "record_folder_name",
    "redact_json",
    "redact_output_text",
    "redact_text",
]
