"""The merge offer — the card that makes the merge word a mechanism.

Make-merge-work build spec (2026-08-24): when a routine build's terminal
publish succeeds and the build finished clean (``tasks_failed == 0``), this
module offers the owner a [Merge & deploy] card. The offer is a DUAL
envelope, published in order:

1. The AGENTS :class:`~nats_core.events.ApprovalRequestPayload` on
   ``agents.approval.forge.merge-{feature_id}`` — the same approval-response
   plumbing the build gate's tap uses, so the press comes back on the
   ``.response`` mirror subject that :mod:`forge.pipeline.merge_executor`
   consumes.
2. The pipeline ``build-paused`` envelope — the card jarvis renders. Its
   ``build_id`` is deliberately ``merge-{feature_id}`` (NOT the real
   build_id): that is the join key jarvis uses, and the synthetic id keeps
   jarvis's terminal registry from refusing the tap on an already-terminal
   build.

Ordering laws (all load-bearing):

* **Durable latch FIRST.** The offer's stage row (target_identifier
  ``merge_deploy_offer``) is written via the same ``record_stage`` path the
  gate uses BEFORE any wire write — so a crash between latch and publish
  leaves an honest "offered" record and the offer is never doubled.
* **ONE publish attempt ever.** A raise mid-publish is an honest terminal
  log ("the card may be on the wire") — never retried; retrying could put
  two cards on the wire against one latch.
* **Fire-and-forget.** The wireup invokes :meth:`MergeOfferService.maybe_offer`
  via ``asyncio.create_task`` after the terminal publish + build-state
  write-back succeed; nothing here may delay ``_on_terminal``'s ack.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from nats_core.envelope import EventType, MessageEnvelope
from nats_core.events import (
    ApprovalRequestPayload,
    BuildCompletePayload,
    BuildPausedPayload,
)

from forge.lifecycle.persistence import StageLogEntry
from forge.receipts import receipts_root

logger = logging.getLogger(__name__)

__all__ = [
    "MERGE_AGENT_ID",
    "MERGE_OFFER_DETAILS_KEY",
    "MERGE_OFFER_STAGE_LABEL",
    "MERGE_OFFER_TARGET_IDENTIFIER",
    "MergeOfferService",
    "approval_subject_for",
    "git_rev_parse_main",
    "merge_request_id",
    "read_baseline_failing",
]

#: ``stage_log.target_identifier`` of the durable offer latch.
MERGE_OFFER_TARGET_IDENTIFIER: str = "merge_deploy_offer"

#: The card's stage label — plain words, per the estate's no-jargon law.
MERGE_OFFER_STAGE_LABEL: str = "the merge word"

#: ``stage_log.details_json`` key holding the offer snapshot.
MERGE_OFFER_DETAILS_KEY: str = "merge_offer"

#: ``agent_id`` stamped on the approval request payload.
MERGE_AGENT_ID: str = "merge-deploy-executor"

#: ``source_id`` on every envelope this module emits (the forge identity).
SOURCE_ID: str = "forge"


def merge_request_id(build_id: str) -> str:
    """The offer's ``request_id`` — ``merge-{build_id}`` (spec-pinned)."""
    return f"merge-{build_id}"


def approval_subject_for(feature_id: str) -> str:
    """The AGENTS subject the card's press answers on (spec-pinned)."""
    return f"agents.approval.forge.merge-{feature_id}"


async def git_rev_parse_main(repo_root: Path) -> str | None:
    """Read ``main``'s sha in ``repo_root`` — the merge's expect-main-sha pin.

    Returns ``None`` on any failure (missing repo, no ``main``, git absent):
    the caller refuses to make an offer it cannot pin, loudly.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "rev-parse",
            "main",
            cwd=str(repo_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await proc.communicate()
    except Exception as exc:  # noqa: BLE001 — best-effort probe, honest None
        logger.warning(
            "merge-offer: git rev-parse main failed to run in %s (%s)",
            repo_root,
            exc,
        )
        return None
    if proc.returncode != 0:
        logger.warning(
            "merge-offer: git rev-parse main exited %s in %s (%s)",
            proc.returncode,
            repo_root,
            stderr_b.decode("utf-8", errors="replace").strip(),
        )
        return None
    sha = stdout_b.decode("utf-8", errors="replace").strip()
    return sha or None


def read_baseline_failing(build_id: str) -> list[str] | None:
    """Best-effort pre-merge baseline failing set — fail-open ``None``.

    Globs ``receipts_root()/<build_id>/**/baseline.json`` and accepts either
    a bare list of test names or a dict carrying a ``failing`` list. Any
    read/parse trouble reads as "no baseline recorded" — the merge verb then
    runs without a ``--baseline-json`` and compares against its own record.
    """
    try:
        root = receipts_root() / build_id
        for path in sorted(root.glob("**/baseline.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, list) and all(isinstance(x, str) for x in data):
                return list(data)
            if isinstance(data, dict):
                failing = data.get("failing")
                if isinstance(failing, list) and all(
                    isinstance(x, str) for x in failing
                ):
                    return list(failing)
    except Exception as exc:  # noqa: BLE001 — fail-open by contract
        logger.debug(
            "merge-offer: baseline read failed for %s (%s); proceeding without",
            build_id,
            exc,
        )
    return None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MergeOfferService:
    """Offers the [Merge & deploy] card after a clean routine build.

    Args:
        config: The validated ``ForgeConfig`` — reads
            ``merge_executor.enabled``, ``merge_executor.response_wait_seconds``
            and ``planning.target_repo_paths``.
        pool: The shared ``SqliteLifecyclePersistence`` facade (builds row
            re-read, offer latch probe + write).
        pipeline_publisher: The shared
            :class:`~forge.adapters.nats.pipeline_publisher.PipelinePublisher`
            (the ``build-paused`` mirror rides the existing publisher).
        raw_publish: ``async (subject, body_bytes)`` — the raw NATS publish
            for the AGENTS approval envelope (its subject is not in the
            pipeline family). Production binds the daemon's shared client's
            ``publish``.
        git_head: Injectable ``async (repo_root) -> sha | None`` seam;
            defaults to :func:`git_rev_parse_main`.
        baseline_reader: Injectable ``(build_id) -> list[str] | None`` seam;
            defaults to :func:`read_baseline_failing`.
        clock: Wall-clock seam for the stage row / paused_at stamps.
    """

    def __init__(
        self,
        *,
        config: Any,
        pool: Any,
        pipeline_publisher: Any,
        raw_publish: Callable[[str, bytes], Awaitable[Any]],
        git_head: Callable[[Path], Awaitable[str | None]] = git_rev_parse_main,
        baseline_reader: Callable[[str], list[str] | None] = read_baseline_failing,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._config = config
        self._pool = pool
        self._publisher = pipeline_publisher
        self._raw_publish = raw_publish
        self._git_head = git_head
        self._baseline_reader = baseline_reader
        self._clock = clock

    async def maybe_offer(self, event: Any) -> None:
        """The wireup's fire-and-forget hook — never raises past itself."""
        try:
            await self._maybe_offer(event)
        except Exception as exc:  # noqa: BLE001 — hook must never propagate
            logger.error(
                "merge-offer: offer pass raised (%s) for payload_type=%s — "
                "if the raise was mid-publish the card may be on the wire; "
                "the offer is NOT retried",
                exc,
                type(event).__name__,
            )

    async def _maybe_offer(self, event: Any) -> None:
        merge_cfg = getattr(self._config, "merge_executor", None)
        if merge_cfg is None or not merge_cfg.enabled:
            return
        if not isinstance(event, BuildCompletePayload):
            return
        if event.tasks_failed != 0:
            logger.info(
                "merge-offer: %s finished with %d failed task(s) — no merge "
                "card is offered for a build that is not clean",
                event.build_id,
                event.tasks_failed,
            )
            return

        # (a) Re-read the builds row — payload.repo is None BY DESIGN; the
        # durable row carries repo + correlation_id.
        row = self._pool.get_build_row(event.build_id)
        if row is None:
            logger.error(
                "merge-offer: no builds row for build_id=%s — cannot offer "
                "the merge card (the offer needs the row's repo and "
                "correlation_id)",
                event.build_id,
            )
            return
        repo_root_raw = self._config.planning.target_repo_paths.get(row.repo)
        if not repo_root_raw:
            logger.error(
                "merge-offer: repo %r (build_id=%s) has no entry in "
                "planning.target_repo_paths — cannot offer the merge card",
                row.repo,
                event.build_id,
            )
            return
        repo_root = Path(repo_root_raw)
        if not row.correlation_id:
            logger.error(
                "merge-offer: builds row %s carries an EMPTY correlation_id — "
                "jarvis drops empty-correlation cards, so no offer is made",
                event.build_id,
            )
            return

        # (b) Pin main's sha now — the merge later refuses if main moved.
        expect_main_sha = await self._git_head(repo_root)
        if expect_main_sha is None:
            logger.error(
                "merge-offer: could not read main's sha in %s — an offer "
                "without an expect-main-sha pin would not be honest; no card "
                "for %s",
                repo_root,
                event.build_id,
            )
            return
        baseline_failing = self._baseline_reader(event.build_id)

        # (c) DURABLE LATCH FIRST — probe, then write, BEFORE any wire.
        stages = self._pool.read_stages(event.build_id)
        if any(
            s.target_identifier == MERGE_OFFER_TARGET_IDENTIFIER for s in stages
        ):
            logger.info(
                "merge-offer: %s already has a merge card on record — not "
                "offering twice",
                event.build_id,
            )
            return

        request_id = merge_request_id(event.build_id)
        subject = approval_subject_for(event.feature_id)
        details: dict[str, Any] = {
            "kind": "merge_deploy_offer",
            "build_id": event.build_id,
            "feature_id": event.feature_id,
            "repo": row.repo,
            "branch": f"autobuild/{event.feature_id}",
            "expect_main_sha": expect_main_sha,
            "tasks_completed": event.tasks_completed,
            "tasks_total": event.tasks_total,
            "baseline_failing": baseline_failing,
            "resume_options": ["approve", "reject"],
        }
        now = self._clock()
        self._pool.record_stage(
            StageLogEntry(
                build_id=event.build_id,
                stage_label=MERGE_OFFER_STAGE_LABEL,
                target_kind="local_tool",
                target_identifier=MERGE_OFFER_TARGET_IDENTIFIER,
                status="GATED",
                gate_mode="MANDATORY_HUMAN_APPROVAL",
                started_at=now,
                completed_at=now,
                duration_secs=0.0,
                details={
                    MERGE_OFFER_DETAILS_KEY: {
                        "request_id": request_id,
                        "correlation_id": row.correlation_id,
                        "approval_subject": subject,
                        **details,
                    }
                },
            )
        )

        # (d) ONE publish attempt ever — dual envelope, approval FIRST.
        rationale = (
            f"{event.feature_id} built clean — {event.tasks_completed} of "
            f"{event.tasks_total} tasks passed. Approve = merge into main, "
            "deploy to the sandbox and run the checks; the branch is kept "
            "either way. Reject = nothing changes."
        )
        try:
            approval = ApprovalRequestPayload(
                request_id=request_id,
                agent_id=MERGE_AGENT_ID,
                action_description=rationale,
                risk_level="high",
                timeout_seconds=merge_cfg.response_wait_seconds,
                details=details,
            )
            envelope = MessageEnvelope(
                source_id=SOURCE_ID,
                event_type=EventType.APPROVAL_REQUEST,
                correlation_id=row.correlation_id,
                payload=approval.model_dump(mode="json"),
            )
            await self._raw_publish(
                subject, envelope.model_dump_json().encode("utf-8")
            )
            paused = BuildPausedPayload(
                feature_id=event.feature_id,
                # Deliberately NOT the real build_id: merge-{feature_id} is
                # the join key jarvis uses, and the synthetic id keeps
                # jarvis's terminal registry from refusing the tap.
                build_id=f"merge-{event.feature_id}",
                stage_label=MERGE_OFFER_STAGE_LABEL,
                gate_mode="MANDATORY_HUMAN_APPROVAL",
                coach_score=None,
                rationale=rationale,
                approval_subject=subject,
                paused_at=now.isoformat(),
                correlation_id=row.correlation_id,
            )
            await self._publisher.publish_build_paused(paused)
        except Exception as exc:  # noqa: BLE001 — one attempt, honest terminal log
            logger.error(
                "merge-offer: publish attempt for %s raised (%s) — the card "
                "may be on the wire; the offer is latched and will NOT be "
                "retried (forge merge-deploy is the attended fallback)",
                event.build_id,
                exc,
            )
