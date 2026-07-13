#!/usr/bin/env python3
"""scripts/restart_policy.py — the O-30 forge-prod restart-policy switch (Phase E2 / E2-S3).

ONE receipted, reversible operator step that gives the un-supervised **forge-prod**
container a host-reboot / power-loss auto-recovery policy and can take it back off:

    apply       docker update --restart unless-stopped forge-prod
    --rollback  docker update --restart no          forge-prod   (docker's default)

**Why this exists (O-30).** forge-prod (the planning engine, the factory heart) is
started with ``docker run -d --name forge-prod --network host … forge:latest … serve``
and NO ``--restart`` policy — docker's default is ``no``, so an overnight power blip /
kernel-update reboot / docker-daemon restart leaves the planning engine down with no
restart. Every *other* factory service auto-recovers (nats ``restart: unless-stopped``,
specialists dual-role restart, llama-swap systemd). This closes that asymmetry.

**Scope / safety (rule 3).** This script runs ``docker update`` — a metadata-only
change to an EXISTING container's ``HostConfig.RestartPolicy``. ``docker update
--restart`` does **NOT** stop, restart, or recreate the container (verified against
the docker CLI reference and demonstrated on a scratch container beside this script);
the running process is untouched and the new policy takes effect on the *next* daemon
start / reboot. It therefore needs **no Ack-Pending-0 / worker-free drain** — that
gate guards a *recreate* (a real restart), and no restart occurs here. The preflight
says so explicitly in the receipt.

**This is a coordinator tool, not a self-service one (rule 3).** Applying the policy
to the LIVE ``forge-prod`` is the coordinator's attended step. Authoring +
demonstrating this pass targets a *throwaway scratch container* via ``--container``;
the live application is left for the coordinator. The default ``--container`` is
``forge-prod`` because that is who the coordinator ultimately runs it against.

**Idempotent.** Re-running ``apply`` when the policy is already ``unless-stopped``
(or ``--rollback`` when already ``no``) changes nothing and still emits a receipt;
``changed`` is False.

Usage
-----
    # Preflight only (read-only inspect; runs no `docker update`):
    python scripts/restart_policy.py --dry-run

    # Give forge-prod the restart policy (the coordinator's live step):
    python scripts/restart_policy.py

    # Take it back off (docker's default `no`):
    python scripts/restart_policy.py --rollback

    # Hermetic rehearsal / demonstration against a scratch container:
    python scripts/restart_policy.py --container forge-prod-restart-demo --dry-run
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: The container the coordinator ultimately targets (O-30).
DEFAULT_CONTAINER = "forge-prod"
#: The auto-recovery policy — matches nats' `restart: unless-stopped` (compose:25).
APPLY_POLICY = "unless-stopped"
#: docker's own default; what `--rollback` restores.
ROLLBACK_POLICY = "no"


# ---------------------------------------------------------------------------
# docker seam (the ONE place a subprocess is spawned — monkeypatched in tests)
# ---------------------------------------------------------------------------


def _run_docker(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run ``docker <args>`` and capture output.

    The single seam through which this script touches docker. Hermetic tests
    monkeypatch this with an in-memory fake, so the test-suite never needs a
    real docker daemon or a real container.
    """
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def inspect_restart_policy(container: str) -> dict[str, Any] | None:
    """Return ``.HostConfig.RestartPolicy`` for ``container`` (None if absent).

    ``None`` means ``docker inspect`` failed — almost always "no such container";
    the caller turns that into a clean error, never a stack trace.
    """
    proc = _run_docker(
        ["inspect", "-f", "{{json .HostConfig.RestartPolicy}}", container]
    )
    if proc.returncode != 0:
        return None
    raw = proc.stdout.strip()
    if not raw or raw == "null":
        # A container with no policy set at all serialises as an empty object;
        # normalise to docker's documented default.
        return {"Name": "no", "MaximumRetryCount": 0}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):  # pragma: no cover - defensive
        raise ValueError(f"unexpected RestartPolicy shape for {container!r}: {parsed!r}")
    # An unset policy round-trips as {"Name":"","MaximumRetryCount":0}; treat "" as "no".
    if not parsed.get("Name"):
        parsed = {"Name": "no", "MaximumRetryCount": parsed.get("MaximumRetryCount", 0)}
    return parsed


def set_restart_policy(container: str, policy: str) -> subprocess.CompletedProcess[str]:
    """``docker update --restart <policy> <container>`` (metadata-only; no restart)."""
    return _run_docker(["update", "--restart", policy, container])


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def preflight_checklist(container: str, *, target_policy: str) -> list[dict[str, Any]]:
    """The O-30 preconditions — most importantly the Ack-Pending-0 non-requirement."""
    checks: list[dict[str, Any]] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    add(
        "no_restart_occurs",
        "ok",
        "`docker update --restart` is a metadata-only change to "
        f"{container!r}'s HostConfig.RestartPolicy — it does NOT stop, restart, "
        "or recreate the container; the running process is untouched and the "
        "new policy takes effect on the next daemon start / reboot.",
    )
    add(
        "ack_pending_zero_not_required",
        "ok",
        "Ack-Pending-0 / worker-free drain is NOT needed for this operation: "
        "that gate guards a container *recreate* (a real restart that would "
        "drop an in-flight build); no restart occurs here, so no drain is "
        "required. (Contrast the forge-prod RECREATE at the B4 window, which "
        "does gate on Ack-Pending-0.)",
    )
    add(
        "target_policy",
        "ok",
        f"target HostConfig.RestartPolicy.Name = {target_policy!r} "
        f"({'auto-recovery on' if target_policy != 'no' else 'docker default / off'}).",
    )
    return checks


# ---------------------------------------------------------------------------
# Receipt + write
# ---------------------------------------------------------------------------


def build_receipt(
    *,
    mode: str,
    dry_run: bool,
    container: str,
    target_policy: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    changed: bool,
    checklist: list[dict[str, Any]],
    docker_update_cmd: list[str],
) -> dict[str, Any]:
    return {
        "script": "scripts/restart_policy.py",
        "purpose": "O-30 forge-prod restart-policy switch (Phase E2 / E2-S3)",
        "mode": mode,
        "dry_run": dry_run,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "container": container,
        "target_policy": target_policy,
        "docker_update_cmd": " ".join(docker_update_cmd),
        "restart_policy_before": before,
        "restart_policy_after": after,
        "changed": changed,
        "preflight_checklist": checklist,
        "operator_next_action": (
            "none — the policy is live in the daemon immediately and persists "
            "across reboots; no container restart/recreate is needed or performed"
            if not dry_run
            else "none (dry run — no `docker update` executed)"
        ),
    }


def write_receipt(receipt: dict[str, Any], receipt_dir: Path) -> Path:
    receipt_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    tag = "dry-run" if receipt["dry_run"] else "applied"
    dest = receipt_dir / f"restart-policy-{receipt['mode']}-{tag}-{stamp}.json"
    dest.write_text(json.dumps(receipt, indent=2, default=str) + "\n", encoding="utf-8")
    return dest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="restart_policy.py",
        description="O-30 restart-policy switch: give forge-prod host-reboot auto-recovery.",
    )
    p.add_argument(
        "--container",
        default=DEFAULT_CONTAINER,
        help=f"container to update (default {DEFAULT_CONTAINER})",
    )
    p.add_argument(
        "--rollback",
        action="store_true",
        help=f"restore docker's default restart policy ({ROLLBACK_POLICY!r})",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="inspect + print the change and receipt; run NO `docker update`",
    )
    p.add_argument(
        "--receipt-dir",
        type=Path,
        default=None,
        help="directory for the receipt JSON (default: ops/receipts beside the repo)",
    )
    return p.parse_args(argv)


def _default_receipt_dir() -> Path:
    # ops/receipts/ beside the repo root (scripts/ -> repo root -> ops/receipts).
    return Path(__file__).resolve().parent.parent / "ops" / "receipts"


def run(argv: list[str]) -> int:
    args = _parse_args(argv)
    mode = "rollback" if args.rollback else "apply"
    target_policy = ROLLBACK_POLICY if args.rollback else APPLY_POLICY
    container: str = args.container
    docker_update_cmd = ["docker", "update", "--restart", target_policy, container]

    before = inspect_restart_policy(container)
    if before is None:
        print(
            f"ERROR: container not found (or docker unreachable): {container!r}",
            file=sys.stderr,
        )
        return 2

    already = (before.get("Name") or "no") == target_policy

    print(f"=== restart_policy.py [{mode}{' · DRY RUN' if args.dry_run else ''}] ===")
    print(f"container: {container}")
    print(f"restart policy (before): {before.get('Name')!r}")
    print(f"target policy: {target_policy!r}")
    print("preflight checklist:")
    checklist = preflight_checklist(container, target_policy=target_policy)
    for c in checklist:
        print(f"  [{c['status']:>7}] {c['check']}: {c['detail']}")

    after: dict[str, Any] | None
    changed: bool
    if args.dry_run:
        after = before if already else {"Name": target_policy, "MaximumRetryCount": 0}
        changed = not already
        print(f"would change: {changed} ({'nothing' if not changed else target_policy})")
    else:
        if already:
            after = before
            changed = False
            print(f"no change — {container} already {target_policy!r} (idempotent)")
        else:
            proc = set_restart_policy(container, target_policy)
            if proc.returncode != 0:
                print(
                    f"ERROR: `docker update` failed (rc={proc.returncode}):\n"
                    f"{proc.stderr.strip()}",
                    file=sys.stderr,
                )
                return 3
            after = inspect_restart_policy(container)
            changed = (before.get("Name") or "no") != (
                (after or {}).get("Name") or "no"
            )
            print(f"restart policy (after): {(after or {}).get('Name')!r}")
            print(f"changed: {changed}")

    receipt = build_receipt(
        mode=mode,
        dry_run=args.dry_run,
        container=container,
        target_policy=target_policy,
        before=before,
        after=after,
        changed=changed,
        checklist=checklist,
        docker_update_cmd=docker_update_cmd,
    )
    print(f"operator next action: {receipt['operator_next_action']}")

    receipt_dir = args.receipt_dir or _default_receipt_dir()
    receipt_path = write_receipt(receipt, receipt_dir)
    print(f"receipt: {receipt_path}")
    return 0


def main() -> None:  # pragma: no cover - thin entrypoint
    raise SystemExit(run(sys.argv[1:]))


if __name__ == "__main__":  # pragma: no cover
    main()
