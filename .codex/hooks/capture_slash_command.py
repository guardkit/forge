#!/usr/bin/env python3
"""Capture GuardKit slash-command invocations to docs/history/.

Wired to two Claude Code hook events via .claude/settings.json:

  UserPromptSubmit -> stash state for this session
  Stop             -> read transcript, write docs/history/ files

The script dispatches on the hook_event_name field of the JSON payload it
receives on stdin, so the same script handles both events.

Allowlisted commands (others ignored): system-arch, system-design,
system-plan, arch-refine, design-refine, feature-spec, feature-plan,
feature-build, feature-complete, task-create, task-work, task-review,
task-refine, task-complete, agent-validate, agent-format, agent-enhance,
review.

Output:
  docs/history/command-history.md             (chronological master log)
  docs/history/<command>-<slug>-history.md    (one per invocation)

Slug derivation: first quoted string -> slugified, else first FEAT-XXX /
TASK-XXX / TASK-REV-XXX token, else no slug (single shared file like
system-arch-history.md).

The script never raises; failures are logged to .claude/hooks/.state/capture.log.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

CAPTURE_COMMANDS = {
    "system-arch", "system-design", "system-plan",
    "arch-refine", "design-refine",
    "feature-spec", "feature-plan", "feature-build", "feature-complete",
    "task-create", "task-work", "task-review", "task-refine", "task-complete",
    "agent-validate", "agent-format", "agent-enhance",
    "review",
}

STATE_DIR_REL = ".claude/hooks/.state"
LOG_REL = ".claude/hooks/.state/capture.log"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _log(cwd: str, msg: str) -> None:
    try:
        p = Path(cwd) / LOG_REL
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(f"{_utc_iso()} {msg}\n")
    except Exception:
        pass


def _slugify(text: str, max_len: int = 60) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9-]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text[:max_len] or "untitled"


def _derive_slug(prompt: str) -> str | None:
    m = re.search(r'"([^"]+)"', prompt)
    if m:
        return _slugify(m.group(1))
    m = re.search(r"\b(TASK-REV-[A-Z0-9-]+|FEAT-[A-Z0-9-]+|TASK-[A-Z0-9-]+)\b", prompt)
    if m:
        return m.group(1)
    return None


def _parse_slash(prompt: str) -> tuple[str, str | None] | None:
    if not prompt:
        return None
    m = re.match(r"^/([\w-]+)\b", prompt.lstrip())
    if not m:
        return None
    cmd = m.group(1)
    if cmd not in CAPTURE_COMMANDS:
        return None
    return cmd, _derive_slug(prompt)


def _state_path(cwd: str, session_id: str) -> Path:
    return Path(cwd) / STATE_DIR_REL / f"{session_id}.json"


def _cleanup_stale(cwd: str, max_age_hours: int = 48) -> None:
    state_dir = Path(cwd) / STATE_DIR_REL
    if not state_dir.exists():
        return
    cutoff = time.time() - (max_age_hours * 3600)
    for f in state_dir.glob("*.json"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except Exception:
            pass


def handle_user_prompt(payload: dict) -> None:
    cwd = payload.get("cwd") or os.getcwd()
    session_id = payload.get("session_id") or "unknown"
    prompt = payload.get("prompt") or ""
    parsed = _parse_slash(prompt)
    if parsed is None:
        return
    command, slug = parsed
    _cleanup_stale(cwd)
    state = {
        "session_id": session_id,
        "command": command,
        "slug": slug,
        "prompt": prompt,
        "transcript_path": payload.get("transcript_path"),
        "started_at": _utc_iso(),
        "started_at_epoch": time.time(),
        "completion_written": False,
    }
    sp = _state_path(cwd, session_id)
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps(state, indent=2))
    _log(cwd, f"queued: /{command} ({slug or '-'}) session={session_id}")


def _extract_assistant_text(transcript_path: str, started_at_epoch: float) -> str:
    p = Path(transcript_path)
    if not p.exists():
        return ""
    chunks: list[str] = []
    with p.open() as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("type") != "assistant":
                continue
            ts = d.get("timestamp")
            if ts:
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if dt.timestamp() + 1.0 < started_at_epoch:
                        continue
                except Exception:
                    pass
            msg = d.get("message") or {}
            content = msg.get("content")
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text":
                        text = c.get("text") or ""
                        if text.strip():
                            chunks.append(text)
            elif isinstance(content, str) and content.strip():
                chunks.append(content)
    return "\n\n".join(chunks).strip()


def _format_block(command: str, slug: str | None, prompt: str, response: str, started_at: str) -> str:
    title = f"/{command}" + (f" {slug}" if slug else "")
    return (
        f"\n\n---\n\n"
        f"## {title} — {started_at}\n\n"
        f"### Prompt\n\n"
        f"```\n{prompt}\n```\n\n"
        f"### Assistant response\n\n"
        f"{response}\n"
    )


def _append_master(cwd: str, command: str, slug: str | None, prompt: str, response: str, started_at: str) -> None:
    history_dir = Path(cwd) / "docs" / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    master = history_dir / "command-history.md"
    if not master.exists():
        master.write_text(
            "# Command History\n\n"
            "Master chronological log of GuardKit slash-command invocations.\n"
            "Captured automatically by `.claude/hooks/capture_slash_command.py`.\n"
        )
    with master.open("a") as f:
        f.write(_format_block(command, slug, prompt, response, started_at))


def _write_per_command(cwd: str, command: str, slug: str | None, prompt: str, response: str, started_at: str) -> None:
    history_dir = Path(cwd) / "docs" / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    name = f"{command}-{slug}-history.md" if slug else f"{command}-history.md"
    path = history_dir / name
    if not path.exists():
        header = (
            f"# /{command}" + (f" — {slug}" if slug else "") + " — history\n\n"
            "Captured automatically by `.claude/hooks/capture_slash_command.py`.\n"
        )
        path.write_text(header)
    with path.open("a") as f:
        f.write(_format_block(command, slug, prompt, response, started_at))


def handle_stop(payload: dict) -> None:
    cwd = payload.get("cwd") or os.getcwd()
    session_id = payload.get("session_id") or "unknown"
    sp = _state_path(cwd, session_id)
    if not sp.exists():
        return
    try:
        state = json.loads(sp.read_text())
    except Exception:
        _log(cwd, f"corrupt state file: {sp}")
        return
    if state.get("completion_written"):
        return
    transcript_path = payload.get("transcript_path") or state.get("transcript_path")
    if not transcript_path:
        _log(cwd, f"no transcript path for session={session_id}")
        return
    response = _extract_assistant_text(transcript_path, state.get("started_at_epoch") or 0.0)
    if not response.strip():
        _log(cwd, f"empty response for /{state.get('command')} ({state.get('slug') or '-'})")
        return
    try:
        _append_master(cwd, state["command"], state.get("slug"), state["prompt"], response, state["started_at"])
        _write_per_command(cwd, state["command"], state.get("slug"), state["prompt"], response, state["started_at"])
        state["completion_written"] = True
        state["completed_at"] = _utc_iso()
        sp.write_text(json.dumps(state, indent=2))
        _log(cwd, f"captured: /{state['command']} ({state.get('slug') or '-'}) -> {len(response)} chars")
    except Exception as e:
        _log(cwd, f"error writing history: {type(e).__name__}: {e}")


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except Exception:
        return 0
    cwd = payload.get("cwd") or os.getcwd()
    event = payload.get("hook_event_name") or ""
    try:
        if event == "UserPromptSubmit":
            handle_user_prompt(payload)
        elif event == "Stop":
            handle_stop(payload)
    except Exception as e:
        _log(cwd, f"unhandled in {event}: {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
