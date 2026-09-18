"""The merge offer — the card that makes the merge word a mechanism.

Make-merge-work build spec (2026-08-24): when a routine build's terminal
publish succeeds and the build finished clean (``tasks_failed == 0``), this
module offers the owner a [Merge & deploy] card.

Since 2026-09-09 it offers the fix journey's card too. The merge-ready
checkpoint used to build its own card through the ordinary approval gate,
which mints a different request id and writes no offer row, so the merge
press — which matches on both — ignored it: the owner said approve and
nothing merged. :meth:`MergeOfferService.offer` is now the one publisher
behind both cards, and each caller supplies only its own words.

The offer is a DUAL envelope, published in order:

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
from typing import Any, Awaitable, Callable, Mapping

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
    "MERGE_BASE_REF",
    "MERGE_OFFER_DETAILS_KEY",
    "MERGE_OFFER_STAGE_LABEL",
    "MERGE_OFFER_TARGET_IDENTIFIER",
    "MergeOfferService",
    "approval_subject_for",
    "branch_to_merge",
    "default_merge_branch",
    "git_rev_parse_main",
    "merge_request_id",
    "read_baseline_failing",
    "request_behind_the_build",
    "run_the_scope_pass",
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

#: What a routine build's branch is held against when the scope pass asks
#: what it changed. The same branch the merge word merges into, and the same
#: one :func:`git_rev_parse_main` pins the offer to.
MERGE_BASE_REF: str = "main"

#: How the scope pass says where it found the person's own sentence, in
#: ordinary words, on the receipt.
REQUEST_FROM_THE_RUN: str = "planning_runs.request_text via builds.correlation_id"
REQUEST_FROM_THE_PARENT: str = (
    "planning_runs.request_text via the parent build's correlation_id"
)


def merge_request_id(build_id: str) -> str:
    """The offer's ``request_id`` — ``merge-{build_id}`` (spec-pinned)."""
    return f"merge-{build_id}"


def default_merge_branch(feature_id: str) -> str:
    """``autobuild/<feature id>`` — the branch every feature build is made on."""
    return f"autobuild/{feature_id}"


def branch_to_merge(feature_id: str, merge_branch: Any) -> str:
    """The branch the merge word merges for this build.

    Rewrite-on-refusal spec Part M, rule 54: the build row's ``merge_branch``
    when the conductor recorded one (a repair's commits land on the fix
    journey's own branch, ``fix/<task id>-<build8>``), else the feature's own
    ``autobuild/<feature id>`` — so every feature build, whose column is
    empty, behaves byte for byte as it always has. Every reader of the branch
    (the offer, the candidate check, the landed-merge detection, the merge
    command) goes through this one function so they can never disagree.
    """
    recorded = str(merge_branch or "").strip()
    return recorded or default_merge_branch(feature_id)


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


def request_behind_the_build(pool: Any, row: Any) -> tuple[str | None, str | None, str | None]:
    """The sentence this build was asked for — ``(request, where from, why not)``.

    A routine build's ``correlation_id`` is the planning run's own, so one
    read of ``planning_runs`` finds the sentence; measured read-only against
    the live ledger on 2026-09-15, that join lands 90 times out of 91.

    A REPAIR build's correlation id is the made-up ``fix-build-<parent build
    id>``, which joins nothing at all — 0 times out of 112 — so it resolves
    through its parent build's row in one hop instead. If neither finds a
    sentence, this says why in ordinary words and the scope pass then
    publishes "the sentence could not be found" rather than an empty request.

    Never raises.
    """
    from forge.pipeline.fix_row_producer import source_build_id_from_correlation_id

    correlation_id = str(getattr(row, "correlation_id", "") or "").strip()
    source = REQUEST_FROM_THE_RUN
    if not correlation_id:
        return None, None, "this build's row carries no correlation id"

    parent_build = source_build_id_from_correlation_id(correlation_id)
    if parent_build:
        source = REQUEST_FROM_THE_PARENT
        try:
            parent_row = pool.get_build_row(parent_build)
        except Exception as exc:  # noqa: BLE001 — a reader never stops a card
            return None, None, (
                f"the build this repair belongs to ({parent_build}) could not "
                f"be read ({type(exc).__name__})"
            )
        if parent_row is None:
            return None, None, (
                f"the build this repair belongs to ({parent_build}) is not in "
                "the record, so the sentence behind it could not be found"
            )
        correlation_id = str(getattr(parent_row, "correlation_id", "") or "").strip()
        if not correlation_id:
            return None, None, (
                f"the build this repair belongs to ({parent_build}) carries no "
                "correlation id"
            )

    try:
        text = _read_request_text(pool, correlation_id)
    except Exception as exc:  # noqa: BLE001 — a reader never stops a card
        return None, None, (
            f"the planning record for {correlation_id} could not be read "
            f"({type(exc).__name__})"
        )
    if not text:
        return None, None, (
            f"there is no planning record for {correlation_id}, so the "
            "sentence behind this build could not be found"
        )
    return text, source, None


def _read_request_text(pool: Any, correlation_id: str) -> str | None:
    """One read of ``planning_runs.request_text``, read-only where it can be.

    Opens a fresh read-only handle on the same database file the facade
    writes to — the pattern every other read side uses — and falls back to
    the facade's own connection for the in-memory databases tests run on.
    """
    from forge.adapters.sqlite.connect import read_only_connect

    db_path = getattr(pool, "db_path", None)
    statement = "SELECT request_text FROM planning_runs WHERE correlation_id = ?"
    if db_path is None or str(db_path) in ("", ":memory:"):
        cursor = pool.connection.execute(statement, (correlation_id,))
        found = cursor.fetchone()
    else:
        cx = read_only_connect(db_path)
        try:
            found = cx.execute(statement, (correlation_id,)).fetchone()
        finally:
            cx.close()
    if found is None:
        return None
    text = found[0] if not isinstance(found, dict) else found.get("request_text")
    text = str(text or "").strip()
    return text or None


def run_the_scope_pass(
    *,
    config: Any,
    pool: Any,
    build_id: str,
    feature_id: str,
    row: Any,
) -> Any | None:
    """Read the finished branch and hold it against the plan and the request.

    Returns the scope report, or ``None`` when the pass could not even be
    attempted — no repository path, say — which the card reads as "nobody
    counted" and says nothing about scope at all.

    Runs git, so it is called off the event loop. Never raises: every way
    this can fail is a sentence on the receipt, and none of them holds up a
    merge card.
    """
    from forge.config.sandboxes import sandbox_for
    from forge.pipeline.branch_scope import (
        read_branch_scope,
        read_branch_scope_in_sandbox,
    )
    from forge.pipeline.scope_report import (
        scope_of_the_build,
        unread_scope,
        write_scope_report,
    )

    repo_key = str(getattr(row, "repo", "") or "")
    repo_root_raw = config.planning.target_repo_paths.get(repo_key)
    if not repo_root_raw:
        logger.info(
            "the scope pass: %s has no entry in planning.target_repo_paths, "
            "so nothing was counted for %s and the card says nothing about "
            "scope",
            repo_key,
            build_id,
        )
        return None

    merge_branch = str(getattr(row, "merge_branch", None) or "").strip() or None
    head = branch_to_merge(feature_id, merge_branch)
    request, source, why_not = request_behind_the_build(pool, row)

    sandbox = sandbox_for(config, repo_key)
    try:
        if sandbox is not None:
            reading = read_branch_scope_in_sandbox(
                sandbox=sandbox,
                repo=repo_key,
                base=MERGE_BASE_REF,
                head=head,
                feature_id=feature_id,
            )
        else:
            reading = read_branch_scope(
                repo_root=Path(repo_root_raw),
                base=MERGE_BASE_REF,
                head=head,
                feature_id=feature_id,
            )
    except Exception as exc:  # noqa: BLE001 — a reader never stops a card
        report = unread_scope(
            f"what {head} changed against {MERGE_BASE_REF} could not be read "
            f"({type(exc).__name__}: {exc})"
        )
        write_scope_report(build_id, report)
        return report

    report = scope_of_the_build(
        reading=reading,
        request=request,
        request_source=source,
        request_why_not=why_not,
    )
    write_scope_report(build_id, report)
    return report


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
        scope_pass: Injectable ``(config, pool, build_id, feature_id, row) ->
            report | None`` seam; defaults to :func:`run_the_scope_pass`. It
            runs git, so it is called off the event loop.
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
        scope_pass: Callable[..., Any] = run_the_scope_pass,
        git_surface: Callable[[str, Path], Any | None] | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._config = config
        self._pool = pool
        self._publisher = pipeline_publisher
        self._raw_publish = raw_publish
        self._git_head = git_head
        self._baseline_reader = baseline_reader
        self._scope_pass = scope_pass
        self._git_surface = git_surface
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

        retained = getattr(event, "worktree_retention", None)
        if isinstance(retained, Mapping) and retained.get("ok"):
            path = str(retained.get("path") or "").strip()
            if (
                retained.get("build_id") != event.build_id
                or not path
                or Path(path).name != event.build_id
            ):
                logger.error(
                    "merge-offer: refusing %s — its retained worktree identity "
                    "does not bind the build id and path",
                    event.build_id,
                )
                return
            self._pool.record_worktree_path(event.build_id, path)

        # THE SCOPE PASS (the planner fix, 2026-09-15). Read the finished
        # branch once, here, before the card is built: what it changed that
        # the plan did not name, and what it built that the request never
        # asked for. It is a REPORT and never a refusal — anything it cannot
        # read is a sentence on its own receipt and the card simply says less.
        scope = await self._take_the_scope_pass(event)

        def _words(branch: str, merge_branch: str | None) -> str:
            from forge.cli._serve_gate_activation import card_line_about_scope

            # The card names the branch only when it is not the feature's own
            # (Part M, rule 55): a feature build's card reads exactly as before.
            named = (
                f"{event.feature_id} (branch {branch})"
                if merge_branch is not None
                else event.feature_id
            )
            sentences = [
                f"{named} built clean — {event.tasks_completed} of "
                f"{event.tasks_total} tasks passed."
            ]
            in_scope = card_line_about_scope(scope)
            if in_scope:
                sentences.append(in_scope)
            sentences.append(
                "Approve = merge into main, deploy to the sandbox and run the "
                "checks; the branch is kept either way. Reject = nothing "
                "changes."
            )
            return " ".join(sentences)

        details: dict[str, Any] = {
            "tasks_completed": event.tasks_completed,
            "tasks_total": event.tasks_total,
        }
        if isinstance(retained, Mapping) and retained.get("ok"):
            details["runner_worktree_retention"] = dict(retained)
        if scope is not None:
            details["scope_report"] = scope.to_dict()

        await self.offer(
            build_id=event.build_id,
            feature_id=event.feature_id,
            card_words=_words,
            extra_details=details,
        )

    async def _take_the_scope_pass(self, event: Any) -> Any | None:
        """Run the scope pass off the event loop; ``None`` if nobody counted.

        Never raises and never delays the card by more than the reading
        itself: a scope line is worth having and no merge card has ever
        waited on one before, so every way this can go wrong ends in a logged
        sentence and a card that says nothing about scope.
        """
        try:
            row = self._pool.get_build_row(event.build_id)
            if row is None:
                return None
            return await asyncio.to_thread(
                self._scope_pass,
                config=self._config,
                pool=self._pool,
                build_id=event.build_id,
                feature_id=event.feature_id,
                row=row,
            )
        except Exception as exc:  # noqa: BLE001 — a report never stops a card
            logger.warning(
                "the scope pass: nothing was counted for %s (%s: %s) — the "
                "merge card says nothing about scope",
                event.build_id,
                type(exc).__name__,
                exc,
            )
            return None

    async def offer(
        self,
        *,
        build_id: str,
        feature_id: str,
        card_words: Callable[[str, str | None], str],
        extra_details: Mapping[str, Any] | None = None,
    ) -> bool:
        """Latch the offer and put ONE merge card in front of the owner.

        This is the whole card — the durable row the merge press matches on,
        the approval request the press answers, and the ``build-paused``
        envelope jarvis renders — and it is the only place any of the three
        is built. Two callers reach it: the routine build's own hook
        (:meth:`maybe_offer`, whose words count the tasks that passed) and
        the fix journey's merge-ready checkpoint (whose words say what the
        checkpoint checked). They share this method precisely so the card the
        owner taps is always the card the merge press consumes; the fix
        journey's own card used to be built somewhere else, and the owner's
        merge word fell on the floor because nothing was listening for it
        (2026-09-09).

        Args:
            build_id: The build the card is for. The press reads it back out
                of the request id, so it is the join key of the whole press.
            feature_id: ``FEAT-XXXX`` — the approval subject and the card's
                synthetic join key are both built from it.
            card_words: ``(branch, merge_branch) -> str`` — the sentences on
                the face of the card. It is given the branch the merge word
                will merge and the row's own recorded branch (``None`` when
                the build never recorded one, which means the feature's own
                branch), so each caller can say what is true for it without
                either of them owning the card's plumbing.
            extra_details: Anything else this caller wants kept on the
                durable row and carried on the approval request. The press
                reads none of it; it is there so the record says who offered
                what.

        Returns:
            ``True`` when the offer was latched and the card publish was
            attempted — the state that must never be retried, whether or not
            the wire write itself raised. ``False`` when the offer refused
            before writing anything: the reason is always a logged sentence,
            and nothing reached the wire.
        """
        merge_cfg = getattr(self._config, "merge_executor", None)
        if merge_cfg is None or not merge_cfg.enabled:
            logger.info(
                "merge-offer: the merge press is switched off — no merge card "
                "is offered for %s",
                build_id,
            )
            return False

        # (a) Re-read the builds row — payload.repo is None BY DESIGN; the
        # durable row carries repo + correlation_id.
        row = self._pool.get_build_row(build_id)
        if row is None:
            logger.error(
                "merge-offer: no builds row for build_id=%s — cannot offer "
                "the merge card (the offer needs the row's repo and "
                "correlation_id)",
                build_id,
            )
            return False
        repo_root_raw = self._config.planning.target_repo_paths.get(row.repo)
        if not repo_root_raw:
            logger.error(
                "merge-offer: repo %r (build_id=%s) has no entry in "
                "planning.target_repo_paths — cannot offer the merge card",
                row.repo,
                build_id,
            )
            return False
        repo_root = Path(repo_root_raw)
        if not row.correlation_id:
            logger.error(
                "merge-offer: builds row %s carries an EMPTY correlation_id — "
                "jarvis drops empty-correlation cards, so no offer is made",
                build_id,
            )
            return False

        # (b) Pin main and the exact offered candidate now.
        merge_branch = str(getattr(row, "merge_branch", None) or "").strip() or None
        branch = branch_to_merge(feature_id, merge_branch)
        from forge.deploy.candidate_tree import InContainerCandidateGit

        git = (
            self._git_surface(row.repo, repo_root)
            if self._git_surface is not None
            else None
        ) or InContainerCandidateGit(repo_root)
        expect_main_sha = await self._git_head(repo_root)
        if expect_main_sha is None:
            logger.error(
                "merge-offer: could not read main's sha in %s — an offer "
                "without an expect-main-sha pin would not be honest; no card "
                "for %s",
                repo_root,
                build_id,
            )
            return False
        candidate_sha = await git.rev_parse(branch)
        candidate_tree = (
            await git.rev_parse(f"{candidate_sha}^{{tree}}")
            if candidate_sha
            else None
        )
        if not candidate_sha or not candidate_tree:
            logger.error(
                "merge-offer: could not pin the exact candidate %s for %s — "
                "no card is offered",
                branch,
                build_id,
            )
            return False

        retained_identity: dict[str, Any] | None = None
        runner_identity = (extra_details or {}).get("runner_worktree_retention")
        worktree_path = str(getattr(row, "worktree_path", None) or "").strip()
        if isinstance(runner_identity, Mapping):
            if not worktree_path:
                logger.error(
                    "merge-offer: refusing %s — the runner retained a worktree "
                    "but the build row has no recorded path",
                    build_id,
                )
                return False
            retained_identity = await git.inspect_autobuild_worktree(
                build_id, worktree_path
            )
            registrations = list(
                retained_identity.get("nested_registrations") or []
            )
            candidate_registration = [
                item
                for item in registrations
                if item.get("branch") == f"refs/heads/{branch}"
                and item.get("head") == candidate_sha
            ]
            if not retained_identity.get("ok") or len(candidate_registration) != 1:
                logger.error(
                    "merge-offer: refusing %s — the retained worktree does not "
                    "carry exactly one registered checkout of %s at %s (%s)",
                    build_id,
                    branch,
                    candidate_sha,
                    retained_identity.get("detail"),
                )
                return False
            retained_identity = dict(retained_identity)
            retained_identity["cleanup_registrations"] = candidate_registration

        baseline_failing = self._baseline_reader(build_id)

        # (c) DURABLE LATCH FIRST — probe, then write, BEFORE any wire.
        stages = self._pool.read_stages(build_id)
        if any(
            s.target_identifier == MERGE_OFFER_TARGET_IDENTIFIER for s in stages
        ):
            logger.info(
                "merge-offer: %s already has a merge card on record — not "
                "offering twice",
                build_id,
            )
            return False

        request_id = merge_request_id(build_id)
        subject = approval_subject_for(feature_id)
        details: dict[str, Any] = {
            "kind": "merge_deploy_offer",
            "build_id": build_id,
            "feature_id": feature_id,
            "repo": row.repo,
            "branch": branch,
            "merge_branch": merge_branch,
            "expect_main_sha": expect_main_sha,
            "candidate_identity_version": 1,
            "candidate_sha": candidate_sha,
            "candidate_tree": candidate_tree,
            **(
                {"worktree_retention": retained_identity}
                if retained_identity is not None
                else {}
            ),
            **dict(extra_details or {}),
            "baseline_failing": baseline_failing,
            "resume_options": ["approve", "reject"],
        }
        now = self._clock()
        self._pool.record_stage(
            StageLogEntry(
                build_id=build_id,
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
        rationale = card_words(branch, merge_branch)
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
                feature_id=feature_id,
                # Deliberately NOT the real build_id: merge-{feature_id} is
                # the join key jarvis uses, and the synthetic id keeps
                # jarvis's terminal registry from refusing the tap.
                build_id=f"merge-{feature_id}",
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
                build_id,
                exc,
            )
        return True
