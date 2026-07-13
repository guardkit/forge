#!/usr/bin/env python3
"""scripts/activate_planning.py — the G-09 activation bundle (Lane B / Phase E1 B4-prep).

ONE receipted, reversible operator step that turns **live planning** on for the
B4 window and can turn it back off. It flips two forge config flags together and
ensures the member-id approver + default target repo the live loop needs:

    apply     planning.enabled = true
              planning.target_terminal.enabled = true
              planning.escalation_approver / approval.expected_approver = <approver>
              planning.default_target_repo = <target-repo>
    --rollback  planning.enabled = false
                planning.target_terminal.enabled = false     (resting state)

**Scope / safety (rule 8).** This script ONLY reads and rewrites the forge config
file (default ``~/forge-state/forge.yaml``) and writes a before/after receipt. It
NEVER touches forge-prod, the live NATS bus, or any container. The forge-prod
recreate that makes the flip take effect is the coordinator's attended step at the
B4 window — this script prints it as the operator's NEXT ACTION, it does not
perform it. Every write is validated against :class:`ForgeConfig` first, so an
invalid config is never written; the write is atomic (temp + ``os.replace``).

**Idempotent.** Re-running ``apply`` when already on (or ``--rollback`` when
already off) changes nothing and still emits a receipt; ``changed`` is False.

**Out-of-band prerequisites (J04 / MP-010).** The broker notification ACL grant
(``forge`` → ``jarvis.notification.slack`` + the planning subjects), the Slack app
scopes/invite, and the fleet-watcher ``nats_url`` were established LIVE in MP-010 /
J04 and live in nats-infra + the container env, NOT in this config file. They are
surfaced as a PRECONDITION CHECKLIST in the receipt (the coordinator confirms them
at the B4 window); this script owns the config-side flip only. YAML comments are
not preserved by the rewrite (values and structure are).

Usage
-----
    # Preflight only (no write): show current + would-be state.
    python scripts/activate_planning.py --dry-run

    # Turn live planning ON (writes the config + a receipt), then the operator
    # recreates forge-prod (printed as the next action).
    python scripts/activate_planning.py

    # Roll back to the resting state (both flags OFF).
    python scripts/activate_planning.py --rollback

    # Hermetic rehearsal against a config COPY (never forge-prod):
    python scripts/activate_planning.py --config /tmp/forge.copy.yaml --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Make the script runnable outside an installed venv by putting the repo's
# ``src`` on the path (harmless when forge is already importable).
_REPO_SRC = Path(__file__).resolve().parent.parent / "src"
if _REPO_SRC.is_dir() and str(_REPO_SRC) not in sys.path:
    sys.path.insert(0, str(_REPO_SRC))

import yaml  # noqa: E402 — after the sys.path shim

from forge.config.models import ForgeConfig  # noqa: E402

#: The resting-state config on the GB10 (MP-010 deploy verification).
DEFAULT_CONFIG_PATH = Path.home() / "forge-state" / "forge.yaml"
#: Rich's Slack member id — the pinned approver / originator (MP-010 / J04).
DEFAULT_APPROVER = "U03QR8WKT29"
#: The B4 target repo (three-lanes §3 B4; MP-010 default_target_repo).
DEFAULT_TARGET_REPO = "guardkit/api_test"


# ---------------------------------------------------------------------------
# Config read / state projection
# ---------------------------------------------------------------------------


def load_raw(path: Path) -> dict[str, Any]:
    """Read ``path`` as a plain YAML mapping (empty file → empty dict)."""
    text = path.read_text(encoding="utf-8")
    raw = yaml.safe_load(text) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: top-level YAML must be a mapping, got {type(raw)}")
    return raw


def project_state(raw: dict[str, Any]) -> dict[str, Any]:
    """Extract just the activation-relevant keys for a before/after dump."""
    planning = raw.get("planning") or {}
    approval = raw.get("approval") or {}
    target_terminal = planning.get("target_terminal") or {}
    return {
        "planning.enabled": planning.get("enabled", False),
        "planning.target_terminal.enabled": target_terminal.get("enabled", False),
        "planning.escalation_approver": planning.get("escalation_approver"),
        "planning.default_target_repo": planning.get("default_target_repo"),
        "planning.originator_wait_seconds": planning.get("originator_wait_seconds"),
        "planning.escalated_wait_seconds": planning.get("escalated_wait_seconds"),
        "planning.target_repo_paths": planning.get("target_repo_paths") or {},
        "approval.expected_approver": approval.get("expected_approver"),
    }


# ---------------------------------------------------------------------------
# Mutation (pure — returns a new dict, never writes)
# ---------------------------------------------------------------------------


def _set(raw: dict[str, Any], section: str, key: str, value: Any) -> None:
    node = raw.setdefault(section, {})
    if not isinstance(node, dict):  # pragma: no cover - defensive
        raise ValueError(f"config section {section!r} is not a mapping")
    node[key] = value


def apply_activation(
    raw: dict[str, Any],
    *,
    approver: str,
    target_repo: str,
    target_repo_path: str | None,
) -> dict[str, Any]:
    """Return a COPY of ``raw`` with live planning turned ON (idempotent).

    Sets both flags ON and ensures the approver + default target repo the live
    loop needs. Wait windows are only filled when absent (never overwritten).
    """
    import copy

    new = copy.deepcopy(raw)
    planning = new.setdefault("planning", {})
    planning["enabled"] = True
    tt = planning.setdefault("target_terminal", {})
    tt["enabled"] = True
    planning["escalation_approver"] = approver
    planning["default_target_repo"] = target_repo
    planning.setdefault("originator_wait_seconds", 3600)
    planning.setdefault("escalated_wait_seconds", 14400)
    if target_repo_path is not None:
        paths = planning.setdefault("target_repo_paths", {})
        if isinstance(paths, dict):
            paths[target_repo] = target_repo_path
    _set(new, "approval", "expected_approver", approver)
    return new


def apply_rollback(raw: dict[str, Any]) -> dict[str, Any]:
    """Return a COPY of ``raw`` restored to the resting state (both flags OFF).

    Only the two switches are reverted — the approver / target-repo / wait
    values are harmless config and are left in place (MP-010 reverted ``enabled``
    alone). Rollback is the B4 kill switch: flag OFF, PLANNED_HANDOFF is the
    terminal again and Mode P intake is rejected at the boundary.
    """
    import copy

    new = copy.deepcopy(raw)
    planning = new.setdefault("planning", {})
    planning["enabled"] = False
    tt = planning.setdefault("target_terminal", {})
    tt["enabled"] = False
    return new


def validate(raw: dict[str, Any]) -> None:
    """Validate the mutated raw config against :class:`ForgeConfig`.

    Raises ``pydantic.ValidationError`` on an invalid result so the caller
    aborts WITHOUT writing (we never write a config forge would reject at boot).
    """
    ForgeConfig.model_validate(raw)


# ---------------------------------------------------------------------------
# Preflight checklist (the J04 / MP-010 out-of-band prerequisites)
# ---------------------------------------------------------------------------


def preflight_checklist(
    raw: dict[str, Any], *, target_repo: str, approver: str
) -> list[dict[str, Any]]:
    """Config-side preconditions + the out-of-band items the operator confirms.

    Config-side items are checked here; out-of-band items (broker ACL, Slack
    scopes, fleet-watcher nats_url) are un-checkable from this script — they are
    listed with ``status="confirm"`` for the coordinator to tick at the window.
    """
    planning = raw.get("planning") or {}
    approval = raw.get("approval") or {}
    paths = planning.get("target_repo_paths") or {}
    checks: list[dict[str, Any]] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    # Config-side (checkable).
    repo_mapped = isinstance(paths, dict) and target_repo in paths
    add(
        "target_repo_path_mapped",
        "ok" if repo_mapped else "warn",
        f"planning.target_repo_paths[{target_repo!r}] = "
        f"{paths.get(target_repo) if isinstance(paths, dict) else None!r} "
        "(the target terminal cannot resolve the repo without a local path — "
        "pass --target-repo-path to set it)",
    )
    approver_set = bool(approval.get("expected_approver")) or bool(
        planning.get("escalation_approver")
    )
    add(
        "member_id_approver_present",
        "ok" if approver_set else "warn",
        f"pinned approver member-id = {approver!r} (apply will set it)",
    )

    # Out-of-band (confirm at the window — established live in MP-010 / J04).
    add(
        "broker_notification_acl",
        "confirm",
        "nats-infra: `forge` may publish jarvis.notification.slack + the "
        "planning subjects (granted live in MP-010 follow-up addendum 2)",
    )
    add(
        "slack_app_scopes_and_invite",
        "confirm",
        "Slack app has message.channels/message.groups + bot is /invite'd to "
        "the planning channel; originator id matches the approver (J04)",
    )
    add(
        "fleet_watcher_nats_url",
        "confirm",
        "forge-prod env threads nats_url so the fleet watcher populates "
        "specialist discovery (else every PO dispatch degrades)",
    )
    return checks


# ---------------------------------------------------------------------------
# Receipt + atomic write
# ---------------------------------------------------------------------------


def build_receipt(
    *,
    mode: str,
    dry_run: bool,
    config_path: Path,
    before: dict[str, Any],
    after: dict[str, Any],
    checklist: list[dict[str, Any]],
) -> dict[str, Any]:
    changed_keys = sorted(k for k in after if before.get(k) != after.get(k))
    return {
        "script": "scripts/activate_planning.py",
        "purpose": "Lane B / Phase E1 (G-09) activation bundle — live planning switch",
        "mode": mode,
        "dry_run": dry_run,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "config_path": str(config_path),
        "before": before,
        "after": after,
        "changed": bool(changed_keys),
        "changed_keys": changed_keys,
        "preflight_checklist": checklist,
        "operator_next_action": (
            "recreate forge-prod so the flip takes effect (Ack-Pending-0 + "
            "worker-free checked, rollback tag minted first — the attended "
            "deploy step; NOT performed by this script)"
        )
        if mode == "apply" and not dry_run
        else (
            "recreate forge-prod to return to the resting state"
            if mode == "rollback" and not dry_run
            else "none (dry run — no config written)"
        ),
    }


def write_atomic(path: Path, raw: dict[str, Any]) -> None:
    """Validate-then-write the config atomically (temp + os.replace)."""
    validate(raw)  # never write an invalid config
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".yaml.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            yaml.safe_dump(raw, fh, sort_keys=False, default_flow_style=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_receipt(receipt: dict[str, Any], receipt_dir: Path) -> Path:
    receipt_dir.mkdir(parents=True, exist_ok=True)
    # Microsecond precision so a rapid apply→re-apply→rollback sequence never
    # collides two receipts onto the same filename.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    tag = "dry-run" if receipt["dry_run"] else "applied"
    dest = receipt_dir / f"activation-{receipt['mode']}-{tag}-{stamp}.json"
    dest.write_text(json.dumps(receipt, indent=2, default=str) + "\n", encoding="utf-8")
    return dest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="activate_planning.py",
        description="G-09 activation bundle: the reversible live-planning switch.",
    )
    p.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"forge config file (default {DEFAULT_CONFIG_PATH})",
    )
    p.add_argument(
        "--rollback",
        action="store_true",
        help="restore the resting state (planning + target_terminal OFF)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="compute + validate + print the change and receipt, write NOTHING",
    )
    p.add_argument("--approver", default=DEFAULT_APPROVER, help="pinned approver member-id")
    p.add_argument(
        "--target-repo", default=DEFAULT_TARGET_REPO, help="default target repo (org/name)"
    )
    p.add_argument(
        "--target-repo-path",
        default=None,
        help="absolute local working-copy path for the target repo (sets "
        "planning.target_repo_paths[<target-repo>])",
    )
    p.add_argument(
        "--receipt-dir",
        type=Path,
        default=None,
        help="directory for the receipt JSON (default: beside the config)",
    )
    return p.parse_args(argv)


def run(argv: list[str]) -> int:
    args = _parse_args(argv)
    mode = "rollback" if args.rollback else "apply"
    config_path: Path = args.config

    if not config_path.exists():
        print(f"ERROR: config not found: {config_path}", file=sys.stderr)
        return 2
    try:
        raw = load_raw(config_path)
    except (ValueError, yaml.YAMLError) as exc:
        print(f"ERROR: cannot parse {config_path}: {exc}", file=sys.stderr)
        return 2

    before = project_state(raw)

    if mode == "rollback":
        new_raw = apply_rollback(raw)
    else:
        new_raw = apply_activation(
            raw,
            approver=args.approver,
            target_repo=args.target_repo,
            target_repo_path=args.target_repo_path,
        )

    # Validate the *result* before we ever consider writing.
    try:
        validate(new_raw)
    except Exception as exc:  # noqa: BLE001 — surface validation loudly, write nothing
        print(
            f"ERROR: the resulting config is invalid; writing NOTHING.\n{exc}",
            file=sys.stderr,
        )
        return 3

    after = project_state(new_raw)
    checklist = preflight_checklist(
        new_raw, target_repo=args.target_repo, approver=args.approver
    )
    receipt = build_receipt(
        mode=mode,
        dry_run=args.dry_run,
        config_path=config_path,
        before=before,
        after=after,
        checklist=checklist,
    )

    # Human-readable summary.
    print(f"=== activate_planning.py [{mode}{' · DRY RUN' if args.dry_run else ''}] ===")
    print(f"config: {config_path}")
    print("state (before → after):")
    for key in after:
        mark = "  " if before.get(key) == after.get(key) else "* "
        print(f"  {mark}{key}: {before.get(key)!r} -> {after.get(key)!r}")
    print(f"changed: {receipt['changed']} ({', '.join(receipt['changed_keys']) or 'nothing'})")
    print("preflight checklist:")
    for c in checklist:
        print(f"  [{c['status']:>7}] {c['check']}: {c['detail']}")
    print(f"operator next action: {receipt['operator_next_action']}")

    if not args.dry_run:
        if receipt["changed"]:
            write_atomic(config_path, new_raw)
            print(f"WROTE {config_path}")
        else:
            print(f"no change — {config_path} left as-is (idempotent)")

    receipt_dir = args.receipt_dir or config_path.parent
    receipt_path = write_receipt(receipt, receipt_dir)
    print(f"receipt: {receipt_path}")
    return 0


def main() -> None:  # pragma: no cover - thin entrypoint
    raise SystemExit(run(sys.argv[1:]))


if __name__ == "__main__":  # pragma: no cover
    main()
