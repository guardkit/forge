"""Bring a sandbox's recorded deployment back after a start (one sandbox per
project, stage S2, design section 2.3a, Codex S2 round 1, R1, R2 and R6;
implementation review 1, R4 and R5).

A project's own sandbox is a factory workspace, and its deployment is the
factory's check that the merged result runs. When a stopped sandbox is started
again its deployed app does not come back by itself (its containers have
restart policy ``no``), but nothing is lost: the images the merge checked, the
named volumes and the app's settings survive on the sandbox's own disks. So
the coordinator redeploys the recorded artifact through the factory's own
route, with the parts the merge press already uses and nothing else:

1. the deployment record (``deployment_targets``) names the commit, the
   identity and the build running on each of the sandbox's targets;
2. the publication record of that build names the artifact the merge checked
   (``checked_json``);
3. the project's own deploy profile at that commit says how the identity and
   the artifact are handed over (the identity declaration);
4. the deployment lock is taken, and the record is READ AGAIN under it: if
   another holder recorded something newer meanwhile, this attempt is
   abandoned and the current record is redeployed instead (R4);
5. only then is the commit's tree laid out, in a restoration folder of its own
   for this attempt — never the original feature's candidate folder — so the
   project's own steps are the ones that shipped with the commit (R5);
6. the promote leg re-tags and starts those exact images, with no rebuild, and
   runs the project's own health checks and live gate;
7. what is running must report the very identity on record, and only then is
   it written down again and the lock put down.

A restoration folder is removed only after an answer: when the promote's
answer was lost (it raised, or the stage did not confirm it), the folder is
kept and written down (``sandbox_restorations``). Before any later attempt,
the helper's ``GET /busy`` is asked: while it is executing anything, the
redeploy waits; once it is not, the kept folders are removed and the attempt
goes on (R5).

No join, no check and no publish. Any failure is a failed redeploy, in one
plain sentence: the gate then tells the starter to suspend automatic
management, and the sandbox stays up. A lock somebody else holds, a command a
takeover stopped, or a promote that may still be running is not a failure:
the redeploy is asked for again.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from forge.sandbox_gate.gate import RedeployOutcome

logger = logging.getLogger(__name__)

__all__ = [
    "compose_redeploy",
    "outstanding_restorations",
    "recorded_targets",
    "redeploy_recorded",
]

#: Who the promote leg is told decided this deploy.
DECIDED_BY = "the sandbox gate (bringing the recorded deployment back after a start)"

#: How many times one redeploy follows a record that changed under it.
MAX_RECORD_CHANGES = 3

_RESTORATIONS_DDL = """
CREATE TABLE IF NOT EXISTS sandbox_restorations (
    restoration TEXT PRIMARY KEY,
    repo        TEXT NOT NULL,
    target      TEXT NOT NULL,
    path        TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    settled_at  TEXT
)
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_restorations(connection: sqlite3.Connection) -> None:
    connection.execute(_RESTORATIONS_DDL)
    connection.commit()


def outstanding_restorations(
    connection: sqlite3.Connection, repos: tuple[str, ...]
) -> list[dict[str, str]]:
    """Restoration folders kept after an uncertain promote, not yet removed."""
    _ensure_restorations(connection)
    marks = ", ".join("?" for _ in repos)
    rows = connection.execute(
        f"SELECT restoration, repo, target, path FROM sandbox_restorations "
        f"WHERE settled_at IS NULL AND repo IN ({marks}) ORDER BY started_at",
        tuple(repos),
    ).fetchall()
    return [
        {"restoration": str(r[0]), "repo": str(r[1]), "target": str(r[2]), "path": str(r[3])}
        for r in rows
    ]


def recorded_targets(connection: sqlite3.Connection, repos: tuple[str, ...]) -> list[dict[str, Any]]:
    """Every deployment target of ``repos`` with something recorded as running."""
    found: list[dict[str, Any]] = []
    for repo in repos:
        try:
            rows = connection.execute(
                "SELECT target FROM deployment_targets WHERE (target = ? OR substr(target, 1, ?) = ?) "
                "AND running_commit IS NOT NULL AND trim(running_commit) <> '' "
                "ORDER BY target",
                (repo, len(repo) + 2, f"{repo}::"),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return []
            raise
        for row in rows:
            record = _the_record(connection, repo, str(row[0]))
            if record is not None:
                found.append(record)
    return found


def _the_record(connection: sqlite3.Connection, repo: str, target: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT running_commit, running_identity, running_build FROM deployment_targets "
        "WHERE target = ?",
        (target,),
    ).fetchone()
    if row is None or not row[0]:
        return None
    return {
        "repo": repo,
        "target": target,
        "commit": str(row[0]),
        "identity": str(row[1] or ""),
        "build": str(row[2] or ""),
    }


def _the_checked_artifact(connection: sqlite3.Connection, build_id: str) -> tuple[str | None, str | None]:
    """``(artifact, joined commit)`` the build's publication record holds."""
    row = connection.execute(
        "SELECT checked_json, j_commit FROM publication_records WHERE build_id = ?",
        (build_id,),
    ).fetchone()
    if row is None:
        return None, None
    try:
        checked = json.loads(row[0]) if row[0] else {}
    except ValueError:
        checked = {}
    artifact = str(checked.get("artifact") or "").strip() if isinstance(checked, dict) else ""
    return (artifact or None), (str(row[1]) if row[1] else None)


def _helper_get(url: str, timeout: float) -> tuple[int, str]:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return int(response.status), response.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), ""


async def _the_helper_is_idle(deps: Any, repo: str) -> bool:
    """True only when the repository's helper answers ``GET /busy`` with an
    empty list; any request executing, or no usable answer, is False."""
    from forge.config.sandboxes import sandbox_for

    entry = sandbox_for(deps.config, repo)
    if entry is None:
        return False
    get = getattr(deps, "http_get", None) or _helper_get
    try:
        status, body = await asyncio.to_thread(get, f"{str(entry.sidecar_url).rstrip('/')}/busy", 10.0)
        data = json.loads(body) if status == 200 else None
    except Exception:  # noqa: BLE001 — no usable answer
        return False
    executing = data.get("executing") if isinstance(data, dict) else data
    return isinstance(executing, list) and not executing


async def _settle_kept_folders(deps: Any, connection: sqlite3.Connection, repos: tuple[str, ...], clock: Any) -> str | None:
    """Remove the restoration folders an earlier uncertain promote left, once
    the helper shows nothing executing. ``None`` when none is left; else why
    the redeploy must wait."""
    kept = outstanding_restorations(connection, repos)
    if not kept:
        return None
    for repo in sorted({k["repo"] for k in kept}):
        if not await _the_helper_is_idle(deps, repo):
            return (
                f"an earlier redeploy's promote for {repo} may still be running (its "
                "answer was lost and the helper does not show itself idle), so its "
                "restoration folder is kept and the redeploy is asked for again"
            )
    for item in kept:
        repo_root = Path(deps.config.planning.target_repo_paths.get(item["repo"]) or ".")
        git = deps.git_surface(item["repo"], repo_root) if deps.git_surface is not None else None
        removed = await git.remove_candidate_tree(item["restoration"], item["path"]) if git else None
        if removed is not True:
            return f"the kept restoration folder {item['restoration']} could not be removed yet"
        connection.execute(
            "UPDATE sandbox_restorations SET settled_at = ? WHERE restoration = ?",
            (clock().isoformat(), item["restoration"]),
        )
        connection.commit()
    return None


async def redeploy_recorded(
    deps: Any, sandbox: str, repos: tuple[str, ...], *, clock: Any = None
) -> RedeployOutcome:
    """Redeploy every recorded deployment of ``repos`` (one sandbox's)."""
    connection = getattr(deps.pool, "connection", None)
    if connection is None:
        return RedeployOutcome(ok=False, sentence="the ledger could not be read")
    clock = clock or _utcnow
    waiting = await _settle_kept_folders(deps, connection, tuple(repos), clock)
    if waiting is not None:
        return RedeployOutcome(ok=False, retry=True, sentence=waiting)
    targets = recorded_targets(connection, repos)
    if not targets:
        return RedeployOutcome(ok=True, sentence="no deployment is recorded")
    for first in targets:
        recorded = first
        for _ in range(MAX_RECORD_CHANGES):
            outcome = await _redeploy_one(deps, connection, recorded, clock)
            if not isinstance(outcome, dict):
                break
            recorded = outcome  # the record changed under the lock: follow it
        else:
            return RedeployOutcome(
                ok=False,
                retry=True,
                sentence=f"{first['target']}'s record kept changing; the redeploy is asked for again",
            )
        if not outcome.ok:
            return outcome
        logger.info(
            "sandbox redeploy: %s is running %s again on %s",
            recorded["target"],
            recorded["identity"],
            sandbox,
        )
    return RedeployOutcome(ok=True, sentence="every recorded deployment is back")


async def _redeploy_one(
    deps: Any, connection: sqlite3.Connection, recorded: dict[str, Any], clock: Any
) -> RedeployOutcome | dict[str, Any]:
    """One attempt. A dict back means the record changed under the lock: that
    is the current record, to redeploy instead."""
    from forge.pipeline.deployment_identity import (
        declared_identity,
        identity_reported_by,
        the_identities_differ,
    )
    from forge.pipeline.deployment_lock import DeploymentLockStore, deployment_target_name
    from forge.pipeline.merge_executor import (
        NOTHING_WAS_STARTED,
        _deploy_task_id,
        _the_profile_where_it_lives,
    )

    repo, target = recorded["repo"], recorded["target"]
    commit, identity, build_id = recorded["commit"], recorded["identity"], recorded["build"]

    def failed(why: str) -> RedeployOutcome:
        return RedeployOutcome(ok=False, sentence=f"{target} was not brought back: {why}")

    if not identity or not build_id:
        return failed("its record names no identity or no build, so there is nothing to check it by")
    repo_root_raw = deps.config.planning.target_repo_paths.get(repo)
    if not repo_root_raw:
        return failed(f"{repo} is not in planning.target_repo_paths")
    repo_root = Path(repo_root_raw)
    build = connection.execute(
        "SELECT feature_id, correlation_id FROM builds WHERE build_id = ?", (build_id,)
    ).fetchone()
    if build is None:
        return failed(f"the build {build_id} it records is not in the ledger")
    feature_id, correlation_id = str(build[0]), str(build[1] or "")
    artifact, joined = _the_checked_artifact(connection, build_id)
    if not artifact:
        return failed(f"the publication record of {build_id} names no checked artifact")
    if joined and joined != commit:
        return failed(
            f"the publication record of {build_id} was checked at {joined[:10]}, not at "
            f"the recorded {commit[:10]}"
        )
    git = deps.git_surface(repo, repo_root) if deps.git_surface is not None else None
    if git is None:
        return failed(f"{repo} has no sandbox venue to lay the commit's tree out in")
    profile, why_not = await _the_profile_where_it_lives(git, repo_root, commit)
    if profile is None:
        return failed(f"its deploy profile could not be read at {commit[:10]} ({why_not})")
    if deployment_target_name(repo, getattr(profile, "env_id", None)) != target:
        return failed(f"its deploy profile at {commit[:10]} no longer names this target")
    declaration = declared_identity(profile)
    if not getattr(declaration, "declared", False):
        return failed("its deploy profile does not declare how the identity is handed over")

    lock = DeploymentLockStore(connection)
    grant = lock.grant(
        target=target,
        build_id=build_id,
        turn=0,
        holder=f"sandbox-redeploy:{os.getpid()}:{uuid.uuid4().hex[:8]}",
        now=clock(),
    )
    if grant is None:
        current = lock.read(target)
        return RedeployOutcome(
            ok=False,
            retry=True,
            sentence=(
                f"another build ({current.holder_build or 'unnamed'}) holds the deployment "
                f"lock on {target}; the redeploy is asked for again"
            ),
        )
    restoration: str | None = None
    tree: str | None = None
    keep_the_folder = False
    try:
        # R4: THE RECORD IS READ AGAIN UNDER THE LOCK. Another holder may have
        # deployed and recorded something newer while this attempt read the
        # profile; then this attempt is abandoned for the current record.
        now_recorded = _the_record(connection, repo, target)
        if now_recorded is None:
            return RedeployOutcome(ok=True, sentence=f"{target} no longer records a deployment")
        if (now_recorded["commit"], now_recorded["identity"], now_recorded["build"]) != (
            commit,
            identity,
            build_id,
        ):
            logger.info(
                "sandbox redeploy: %s's record changed to %s while this attempt "
                "prepared; following the current record",
                target,
                now_recorded["identity"],
            )
            return now_recorded

        # R5: A RESTORATION FOLDER OF ITS OWN, laid out only under the lock.
        restoration = f"FEAT-R{uuid.uuid4().hex[:8].upper()}"
        try:
            await git.ensure_candidate_trees_excluded()
            laid_out = await git.materialise_candidate_tree(restoration, commit)
        except Exception as exc:  # noqa: BLE001 — a lay-out that failed is a redeploy not made
            return failed(f"the tree of {commit[:10]} could not be laid out ({exc})")
        tree = str(laid_out.path)
        _ensure_restorations(connection)
        connection.execute(
            "INSERT INTO sandbox_restorations (restoration, repo, target, path, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (restoration, repo, target, tree, clock().isoformat()),
        )
        connection.commit()

        ownership = {
            "target": target,
            "target_counter": grant.counter,
            "build": build_id,
            "identity": identity,
            "identity_setting": declaration.setting,
            "artifact": artifact,
            "artifact_setting": declaration.artifact_setting,
        }
        # R5 (Codex S2 implementation review 2): FROM HERE THE FOLDER IS KEPT
        # unless a definite answer comes back. A cancellation (a coordinator
        # stopping) or any exception leaves the row and the folder as
        # outstanding, and is raised on; only a confirmed finish — this
        # answer, or a later empty /busy — removes them.
        keep_the_folder = True
        try:
            deployed = await deps.deploy_dispatcher(
                repo=repo,
                repo_root=repo_root,
                feature_id=feature_id,
                build_id=build_id,
                correlation_id=correlation_id,
                decided_by=DECIDED_BY,
                dry_run=False,
                leg="promote",
                deploy_run_id=str(uuid.uuid4()),
                task_id=_deploy_task_id(feature_id),
                deploy_ownership=ownership,
                candidate_cwd=tree,
            )
        except Exception as exc:  # noqa: BLE001 — no answer: the promote may still run
            return RedeployOutcome(
                ok=False,
                retry=True,
                sentence=(
                    f"the promote of {target} got no answer ({type(exc).__name__}: {exc}); "
                    "its restoration folder is kept until the helper shows it finished"
                ),
            )
        detail = (getattr(deployed, "detail", None) or {}) if deployed is not None else {}
        if deployed is None or detail.get("answer_lost") or getattr(deployed, "confirmed", False) is not True:
            return RedeployOutcome(
                ok=False,
                retry=True,
                sentence=(
                    f"the promote of {target} was not confirmed as finished; its "
                    "restoration folder is kept until the helper shows it finished"
                ),
            )
        keep_the_folder = False  # a definite answer: the promote has finished
        word = getattr(deployed, "outcome", None)
        said = str(detail.get("deploy_output") or "")
        stopped = next((w for w in NOTHING_WAS_STARTED if w in said), None)
        if stopped is not None:
            return RedeployOutcome(
                ok=False,
                retry=True,
                sentence=f"the deploy command on {target} did not run ({stopped}); asked for again",
            )
        if word != "complete":
            return failed(
                "the project's own deploy step, with its health checks and live gate, did "
                f"not finish ({word or 'the deploy stage answered nothing'})"
            )
        reported = identity_reported_by(said, marker=declaration.marker)
        if the_identities_differ(identity, reported):
            return failed(
                f"the deploy step was handed {identity} and reported "
                + (f"{reported}" if reported else "no identity at all")
            )
        if not lock.record_running(
            target=target,
            counter=grant.counter,
            now=clock(),
            commit=commit,
            identity=identity,
            build_id=build_id,
        ):
            return RedeployOutcome(
                ok=False,
                retry=True,
                sentence=f"the deployment lock on {target} was taken over while it redeployed",
            )
        return RedeployOutcome(ok=True, sentence=f"{target} is running {identity} again")
    finally:
        try:
            lock.release(target=target, counter=grant.counter, now=clock())
        except Exception:  # noqa: BLE001 — never costs a result
            logger.warning("sandbox redeploy: the lock on %s was not put down", target)
        if restoration is not None and tree is not None and not keep_the_folder:
            if await git.remove_candidate_tree(restoration, tree) is True:
                connection.execute(
                    "UPDATE sandbox_restorations SET settled_at = ? WHERE restoration = ?",
                    (clock().isoformat(), restoration),
                )
                connection.commit()


def compose_redeploy(
    *, forge_config: Any, sqlite_pool: Any, nats_client: Any, db_path: Any
) -> Any:
    """The gate's redeploy route, from the coordinator's parts: the merge
    press's own deploy dispatch and git venue."""
    from types import SimpleNamespace

    from forge.cli.serve import compose_merge_git_surface
    from forge.pipeline.merge_executor import build_in_daemon_deploy_dispatcher

    deps = SimpleNamespace(
        config=forge_config,
        pool=sqlite_pool,
        git_surface=compose_merge_git_surface(forge_config),
        deploy_dispatcher=build_in_daemon_deploy_dispatcher(
            config=forge_config, nats_client=nats_client, db_path=db_path
        ),
        http_get=None,
    )

    async def redeploy(sandbox: str, repos: tuple[str, ...]) -> RedeployOutcome:
        return await redeploy_recorded(deps, sandbox, repos)

    return redeploy
