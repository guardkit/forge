"""EVERY request this stage sends the helper carries the build and the commit.

The far side reads a project's own declarations at a commit rather than off the
working copy it runs the project's scripts out of, and it will not take that
commit on a request's own word: the request has to name the BUILD, and the
helper asks the coordinator what commit it recorded that build as starting
from. So the pair has to be on every request this stage sends — the candidate
check, both of the promote's, the teardown and the read-only "what are you
running" question.

WHY THIS FILE EXISTS (28 September 2026, the seventh review). Nothing proved
it. A reviewer deleted ``build=``/``start_commit=`` from where the stage makes
its script runner and ran the four named suites: all still green. The helper's
own refusals were covered; the coordinator's half of the same rule was covered
by nothing at all, so a future edit could drop the stamp and no test would say
so.

Two kinds of proof here, and they fail in different ways:

* every request's recorded body is read and the pair asserted on it, so a
  missing stamp is named directly;
* the stand-in helper REFUSES an unstamped request, so the refusal comes back
  as a red leg. A stamp dropped anywhere between the dispatcher and the wire
  turns these legs red even if somebody deletes the assertions.

WHAT THE STAND-IN IS, EXACTLY (corrected 23 September 2026, the eighth
review). The second class's stand-in refuses EVERY request that carries
neither the build nor the commit. The real helper is not that strict and this
file used to say it was. What the real helper does, since the same review, is
refuse an unstamped request that asks for the project's own declarations to be
READ — a deploy of the live thing always asks (its two setting names are
committed lines), and so does any request naming a memory or a setting of the
project's own. A request that asks for nothing declared needs no binding and
is served with the factory's own list, as it always was; a person running one
by hand says ``by_hand: true`` and is served at the helper's committed HEAD.
So for the promote's deploy step this stand-in mirrors production, and for the
plainer legs it is deliberately stricter — a tighter net around the stamp, not
a claim about the far side. The real helper's own refusal of an unstamped
deploy is proven against the real route in
``tests/forge/deploy_sidecar/test_the_project_widens_the_environment_door.py``
(``test_a_deploy_with_no_build_at_all_is_refused_and_nothing_is_deployed``).

Nothing real is contacted. The stand-in helper is an HTTP server in this
process, bound to loopback on a port the kernel picks.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from forge.config.models import DeployStageConfig
from forge.deploy.composition import dispatch_deploy_stage
from forge.deploy.live_gate import DryRunBrokerInspector, DryRunLiveGateInvoker
from forge.deploy.profile import parse_deploy_profile
from forge.persistence.repositories.runbook import RunbookRepository

FIXED = datetime(2026, 9, 28, 9, 0, 0, tzinfo=UTC)

#: The build, and the commit the coordinator's ledger records it as starting
#: from. Both are the coordinator's own facts; this file only asks that they
#: travel.
THE_BUILD = "b-stamp-7f21"
THE_RECORDED_COMMIT = "9f1c2d3e4b5a69788796a5b4c3d2e1f00112233"

#: A repository key and a target repo path the stand-in never resolves — it is
#: a recorder, not the helper, and it answers without looking at anything.
REPO = "acme/widget-shop"

#: Which candidate a teardown means. Without it the teardown leg refuses and
#: sends nothing at all, which would make this file prove nothing.
WHICH_CANDIDATE = {"DEPLOY_IDENTITY": "j-7f21beef@feedfacecafe"}

#: What a deploy of the live thing owns. Present on the promote only.
OWNERSHIP = {
    "target": "acme/widget-shop::live",
    "target_counter": 4,
    "build": THE_BUILD,
    "identity": "j-7f21beef@feedfacecafe",
    "identity_setting": "DEPLOY_IDENTITY",
    "artifact": "the-artifact-the-check-reported",
    "artifact_setting": "DEPLOY_ARTIFACT",
}


class _ARecordingHelper:
    """A stand-in for the helper: it records every request and answers.

    It also REFUSES a request that carries neither the build nor the commit,
    in the same SHAPE the real helper refuses one — an HTTP 400 carrying one
    plain sentence, which the client relays as a non-zero exit and the step
    records as a failure. That is what makes a dropped stamp show up as a red
    leg rather than as a quietly weaker request.

    It is stricter than the real helper on purpose, and this file's own
    docstring says exactly where: the real helper refuses an unstamped request
    that asks for the project's declarations to be READ (every deploy of the
    live thing does) and serves one that asks for nothing declared. This
    stand-in refuses both, so no leg of this stage can drop the pair unnoticed.
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        recorder = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 — http.server's name
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                except Exception:  # noqa: BLE001 — a stand-in, never a crash
                    body = {}
                recorder.requests.append(body)
                build = str(body.get("build") or "").strip()
                commit = str(body.get("declared_at") or "").strip()
                if not build or not commit:
                    self._answer(
                        400,
                        {
                            "error": (
                                "this request names no build and no commit, so "
                                "there is nothing to read this project's own "
                                "declarations at and nothing was run"
                            )
                        },
                    )
                    return
                self._answer(
                    200,
                    {
                        "exit_code": 0,
                        "output_tail": "DEPLOYED_IDENTITY=j-7f21beef@feedfacecafe\n",
                        "cwd": str(body.get("cwd") or ""),
                    },
                )

            def _answer(self, status: int, payload: dict[str, Any]) -> None:
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args: Any) -> None:  # noqa: ANN401
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def stamps(self) -> list[tuple[str, str]]:
        """``(build, commit)`` off every request, in the order they arrived."""
        return [
            (str(r.get("build") or ""), str(r.get("declared_at") or ""))
            for r in self.requests
        ]


@pytest.fixture
def helper() -> Any:
    stand_in = _ARecordingHelper()
    try:
        yield stand_in
    finally:
        stand_in.close()


@pytest.fixture
def repository(tmp_path: Path) -> RunbookRepository:
    from forge.persistence.migrations.runbook import apply

    connection = sqlite3.connect(str(tmp_path / "deploy.db"))
    apply(connection)
    return RunbookRepository(connection=connection)


@pytest.fixture
def runbook_publisher() -> AsyncMock:
    publisher = AsyncMock()
    publisher.publish_runbook_started = AsyncMock()
    publisher.publish_step_started = AsyncMock()
    publisher.publish_step_result = AsyncMock()
    publisher.publish_runbook_complete = AsyncMock()
    publisher.publish_escalated = AsyncMock()
    return publisher


def _profile() -> Any:
    return parse_deploy_profile(
        {
            "env_id": "widget-shop-local",
            "compose": {"file": "compose.yaml", "script": "deploy/deploy.sh"},
            "health_checks": [{"cmd": "qa/health.sh"}],
            "cwd": "/somewhere/widget-shop",
            "candidate": {"env": {"CANDIDATE_PORT": "8902"}},
        }
    )


async def _drive(
    *,
    leg: str,
    helper: Any,
    repository: RunbookRepository,
    runbook_publisher: AsyncMock,
    tmp_path: Path,
    build_id: str | None = THE_BUILD,
    declared_at: str | None = THE_RECORDED_COMMIT,
) -> Any:
    """One leg, through the REAL dispatcher, onto the stand-in helper."""
    return await dispatch_deploy_stage(
        DeployStageConfig(
            enabled=True, execution_surface="sidecar", sidecar_url=helper.url
        ),
        _profile(),
        correlation_id=f"corr-{leg}",
        deploy_run_id=f"run-{leg}",
        repository=repository,
        runbook_publisher=runbook_publisher,
        deploy_publisher=_QuietDeployPublisher(),
        live_gate_invoker=DryRunLiveGateInvoker(),
        broker_inspector=DryRunBrokerInspector(),
        deploy_record_root=str(tmp_path / "state"),
        dry_run=False,
        clock=lambda: FIXED,
        target_repo=REPO,
        target_repo_root=str(tmp_path / "widget-shop"),
        feature="FEAT-7F21",
        feat_id="FEAT-7F21",
        leg=leg,
        candidate_cwd=None,
        prior_events=("DeployQueued",) if leg == "promote" else (),
        deploy_ownership=dict(OWNERSHIP) if leg == "promote" else None,
        identity_env=dict(WHICH_CANDIDATE),
        ask_env={"RUNNING_IDENTITY": "1"},
        build_id=build_id,
        declared_at=declared_at,
    )


class _QuietDeployPublisher:
    """Every deploy event, dropped. This file is about what goes to the helper."""

    def __getattr__(self, _name: str) -> Any:
        async def _publish(*_args: Any, **_kwargs: Any) -> None:
            return None

        return _publish


# ---------------------------------------------------------------------------
# Every leg, every request
# ---------------------------------------------------------------------------


class TestEveryRequestCarriesTheBuildAndTheRecordedCommit:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("leg", "at_least"),
        [
            ("candidate_check", 2),
            ("promote", 2),
            ("candidate_down", 1),
            ("what_is_running", 1),
        ],
    )
    async def test_the_pair_is_on_every_request_the_leg_sends(
        self, leg, at_least, helper, repository, runbook_publisher, tmp_path
    ) -> None:
        await _drive(
            leg=leg,
            helper=helper,
            repository=repository,
            runbook_publisher=runbook_publisher,
            tmp_path=tmp_path,
        )
        stamps = helper.stamps()
        assert len(stamps) >= at_least, (
            f"the {leg} leg sent {len(stamps)} requests; this leg is the one "
            f"place the pair could be dropped, so it must send at least "
            f"{at_least}"
        )
        assert stamps == [(THE_BUILD, THE_RECORDED_COMMIT)] * len(stamps)

    @pytest.mark.asyncio
    async def test_the_promote_stamps_the_teardown_inside_it_too(
        self, helper, repository, runbook_publisher, tmp_path
    ) -> None:
        """The promote takes the candidate down on its way past.

        That teardown is a SECOND request, made further in than the one the
        promote's own deploy step sends, and it used to carry nothing at all —
        so it read at whatever the working copy's HEAD happened to be.
        """
        await _drive(
            leg="promote",
            helper=helper,
            repository=repository,
            runbook_publisher=runbook_publisher,
            tmp_path=tmp_path,
        )
        scripts = [str(r.get("script") or "") for r in helper.requests]
        assert len(scripts) >= 2
        # Both of them, whatever the project called its scripts.
        assert helper.stamps() == [(THE_BUILD, THE_RECORDED_COMMIT)] * len(scripts)

    @pytest.mark.asyncio
    async def test_the_deploy_of_the_live_thing_carries_its_ownership_too(
        self, helper, repository, runbook_publisher, tmp_path
    ) -> None:
        """The stamp does not displace what the promote already carried."""
        await _drive(
            leg="promote",
            helper=helper,
            repository=repository,
            runbook_publisher=runbook_publisher,
            tmp_path=tmp_path,
        )
        owning = [r for r in helper.requests if r.get("deploy")]
        assert owning, "the promote's deploy step owns the target and must say so"
        for request in owning:
            assert request["deploy"]["build"] == THE_BUILD
            assert request["build"] == THE_BUILD
            assert request["declared_at"] == THE_RECORDED_COMMIT


# ---------------------------------------------------------------------------
# The mutation guard: an unstamped request is refused, so a dropped stamp is red
# ---------------------------------------------------------------------------


class TestADroppedStampIsARedLeg:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "leg", ["candidate_check", "promote", "candidate_down", "what_is_running"]
    )
    async def test_a_leg_whose_requests_carry_no_stamp_fails(
        self, leg, helper, repository, runbook_publisher, tmp_path
    ) -> None:
        """Drop the pair at the dispatcher — the shape of the mutation — and
        every leg goes red against a helper that will not serve an unstamped
        request.

        This is the test that bites when somebody deletes the stamp from where
        the stage makes its script runner: the assertions above name it, and
        this one fails even with those assertions gone.

        The stand-in's refusal is stricter than the real helper's on the
        plainer legs; the class docstring says so, and the real helper's own
        refusal of an unstamped deploy is proven against the real route in the
        door tests rather than claimed here.
        """
        result = await _drive(
            leg=leg,
            helper=helper,
            repository=repository,
            runbook_publisher=runbook_publisher,
            tmp_path=tmp_path,
            build_id=None,
            declared_at=None,
        )
        assert result is not None
        assert result.outcome == "failed", (
            f"the {leg} leg sent requests with no build and no commit and the "
            "helper refused them; the leg must be red"
        )
        assert helper.requests, "nothing reached the helper at all"
        assert all(
            not r.get("build") and not r.get("declared_at") for r in helper.requests
        )

    @pytest.mark.asyncio
    async def test_the_same_legs_are_green_when_the_pair_is_there(
        self, helper, repository, runbook_publisher, tmp_path
    ) -> None:
        """The other half of the mutation: with the stamp, the same legs pass.

        Without this, a leg that was red for some unrelated reason would make
        the test above pass for the wrong reason.
        """
        for leg in ("candidate_check", "promote", "candidate_down", "what_is_running"):
            result = await _drive(
                leg=leg,
                helper=helper,
                repository=repository,
                runbook_publisher=runbook_publisher,
                tmp_path=tmp_path,
            )
            assert result is not None and result.outcome == "complete", leg
