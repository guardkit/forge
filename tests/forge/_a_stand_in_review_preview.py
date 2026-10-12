"""The review-preview service, stood in for, offline.

The merge card for a project with a ``preview`` section carries a preview of
the candidate joined onto main, and the merge press refuses an approval whose
preview is not usable (web-readiness W7). The real service
(``forge.deploy.review_preview``) deploys into a sandbox and runs a browser
journey; none of that belongs in a unit test. This stand-in answers the same
six calls out of plain fields the test sets, and writes down every call so a
test can say what was asked and when.

The evidence it returns is a real ``nats_core`` ``ReviewEvidence``, so the
shapes the offer puts on the wire are the shapes Jarvis will read.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from nats_core.events import ReviewEvidence

from forge.deploy.review_preview import PreviewRequest

__all__ = ["PREVIEW_TREE", "StandInReviewPreview", "an_evidence"]

#: The tree the stand-in says it previewed: the candidate joined onto main.
PREVIEW_TREE = "e" * 40

#: When every stand-in preview was prepared.
PREPARED_AT = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


def an_evidence(
    state: str = "ready",
    *,
    candidate_sha: str,
    candidate_tree: str,
    preview_tree: str = PREVIEW_TREE,
    failure_reason: str = "the journey did not finish",
) -> ReviewEvidence:
    """One review-evidence record, ready or failed, for the given candidate."""
    base = "https://preview.example.invalid"
    common: dict[str, Any] = {
        "candidate_sha": candidate_sha,
        "candidate_tree": candidate_tree,
        "preview_tree": preview_tree,
        "run_id": "run-1",
        "created_at": PREPARED_AT,
        "app_expires_at": PREPARED_AT + timedelta(hours=24),
        "evidence_expires_at": PREPARED_AT + timedelta(days=14),
        "outcome": {"passed": 3, "failed": 0, "skipped": None, "flaky": None},
    }
    if state == "ready":
        return ReviewEvidence(
            state="ready",
            app_url=f"{base}:8950/?preview=token",
            report_url=f"{base}/report/index.html",
            media_urls=[f"{base}/media/journey.webm"],
            **common,
        )
    return ReviewEvidence(
        state="failed",
        failure_reason=failure_reason,
        diagnostics_url=f"{base}/diagnostics/trace.zip",
        **common,
    )


class StandInReviewPreview:
    """Answers the review-preview interface out of plain fields.

    Args:
        enabled: What ``enabled_for`` answers for every project.
        state: ``ready`` or ``failed`` — the evidence ``prepare`` returns.
        prepare_raises: When set, ``prepare`` raises it instead.
        refusal: What ``approval_refusal`` answers (``None`` = may approve).
        release_raises: When set, ``release`` raises it after writing the
            call down.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        state: str = "ready",
        preview_tree: str = PREVIEW_TREE,
        prepare_raises: BaseException | None = None,
        refusal: str | None = None,
        release_raises: BaseException | None = None,
    ) -> None:
        self.enabled = enabled
        self.state = state
        self.preview_tree = preview_tree
        self.prepare_raises = prepare_raises
        self.refusal = refusal
        self.release_raises = release_raises
        self.asked_about: list[str] = []
        self.requests: list[Any] = []
        self.released: list[dict[str, str]] = []
        self.refusal_questions: list[tuple[Any, datetime]] = []
        self.sweeps: list[datetime] = []
        self.reconciles = 0

    def enabled_for(self, project: str) -> bool:
        self.asked_about.append(project)
        return self.enabled

    async def prepare(self, req: PreviewRequest) -> ReviewEvidence:
        self.requests.append(req)
        if self.prepare_raises is not None:
            raise self.prepare_raises
        return an_evidence(
            self.state,
            candidate_sha=req.candidate_sha,
            candidate_tree=req.candidate_tree,
            preview_tree=self.preview_tree,
        )

    async def release(self, *, feature_id: str, build_id: str, reason: str) -> None:
        self.released.append(
            {"feature_id": feature_id, "build_id": build_id, "reason": reason}
        )
        if self.release_raises is not None:
            raise self.release_raises

    async def sweep(self, *, now: datetime) -> None:
        self.sweeps.append(now)

    async def reconcile_on_boot(self) -> None:
        self.reconciles += 1

    def approval_refusal(
        self, review_evidence: Mapping[str, Any] | None, *, now: datetime
    ) -> str | None:
        self.refusal_questions.append((review_evidence, now))
        return self.refusal
