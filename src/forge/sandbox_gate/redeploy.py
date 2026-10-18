"""Bring a sandbox's recorded deployment back after a start (one sandbox per
project, stage S2, design section 2.3a, Codex S2 round 1, R1, R2 and R6).

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
4. the commit's tree is laid out again where the repository lives, so the
   project's own steps are the ones that shipped with the commit;
5. under the deployment lock, the promote leg re-tags and starts those exact
   images, with no rebuild, and runs the project's own health checks and live
   gate;
6. what is running must report the very identity on record, and only then is
   it written down again and the lock put down.

No join, no check and no publish: it is the same deploy steps the merge press
runs after its join. Any failure is a failed redeploy, in one plain sentence:
the gate then tells the starter to suspend automatic management, and the
sandbox stays up. A lock somebody else holds, or a command a takeover stopped,
is not a failure: the redeploy is asked for again.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from forge.sandbox_gate.gate import RedeployOutcome

logger = logging.getLogger(__name__)

__all__ = ["compose_redeploy", "recorded_targets", "redeploy_recorded"]

#: Who the promote leg is told decided this deploy.
DECIDED_BY = "the sandbox gate (bringing the recorded deployment back after a start)"


def recorded_targets(connection: sqlite3.Connection, repos: tuple[str, ...]) -> list[dict[str, Any]]:
    """Every deployment target of ``repos`` with something recorded as running."""
    found: list[dict[str, Any]] = []
    for repo in repos:
        try:
            rows = connection.execute(
                "SELECT target, running_commit, running_identity, running_build "
                "FROM deployment_targets WHERE (target = ? OR substr(target, 1, ?) = ?) "
                "AND running_commit IS NOT NULL AND trim(running_commit) <> '' "
                "ORDER BY target",
                (repo, len(repo) + 2, f"{repo}::"),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return []
            raise
        for row in rows:
            found.append(
                {
                    "repo": repo,
                    "target": str(row[0]),
                    "commit": str(row[1]),
                    "identity": str(row[2] or ""),
                    "build": str(row[3] or ""),
                }
            )
    return found


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


async def redeploy_recorded(
    deps: Any, sandbox: str, repos: tuple[str, ...], *, clock: Any = None
) -> RedeployOutcome:
    """Redeploy every recorded deployment of ``repos`` (one sandbox's)."""
    connection = getattr(deps.pool, "connection", None)
    if connection is None:
        return RedeployOutcome(ok=False, sentence="the ledger could not be read")
    targets = recorded_targets(connection, repos)
    if not targets:
        return RedeployOutcome(ok=True, sentence="no deployment is recorded")
    for recorded in targets:
        outcome = await _redeploy_one(deps, connection, recorded, clock or _utcnow)
        if not outcome.ok:
            return outcome
        logger.info(
            "sandbox redeploy: %s is running %s again on %s",
            recorded["target"],
            recorded["identity"],
            sandbox,
        )
    return RedeployOutcome(ok=True, sentence="every recorded deployment is back")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _redeploy_one(
    deps: Any, connection: sqlite3.Connection, recorded: dict[str, Any], clock: Any
) -> RedeployOutcome:
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
    try:
        await git.ensure_candidate_trees_excluded()
        laid_out = await git.materialise_candidate_tree(feature_id, commit)
    except Exception as exc:  # noqa: BLE001 — a lay-out that failed is a redeploy not made
        return failed(f"the tree of {commit[:10]} could not be laid out ({exc})")
    tree = str(laid_out.path)

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
        await git.remove_candidate_tree(feature_id, tree)
        return RedeployOutcome(
            ok=False,
            retry=True,
            sentence=(
                f"another build ({current.holder_build or 'unnamed'}) holds the deployment "
                f"lock on {target}; the redeploy is asked for again"
            ),
        )
    try:
        ownership = {
            "target": target,
            "target_counter": grant.counter,
            "build": build_id,
            "identity": identity,
            "identity_setting": declaration.setting,
            "artifact": artifact,
            "artifact_setting": declaration.artifact_setting,
        }
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
        except Exception as exc:  # noqa: BLE001 — a raise is a failed redeploy
            return failed(f"its promote raised ({type(exc).__name__}: {exc})")
        word = getattr(deployed, "outcome", None) if deployed is not None else None
        detail = (getattr(deployed, "detail", None) or {}) if deployed is not None else {}
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
        await git.remove_candidate_tree(feature_id, tree)


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
    )

    async def redeploy(sandbox: str, repos: tuple[str, ...]) -> RedeployOutcome:
        return await redeploy_recorded(deps, sandbox, repos)

    return redeploy
