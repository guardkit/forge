"""The starter client speaks the starter's own interface (deploy/compose/
sandbox-starter/starter.py) and never raises."""

from __future__ import annotations

from typing import Any

from forge.sandbox_gate.starter_client import (
    HOLD_DEPLOYED_TIMEOUT_SECONDS,
    STOP_TIMEOUT_SECONDS,
    NO_DEPLOYMENT,
    StarterClient,
)


class Transport:
    def __init__(self, answers: list[tuple[int, Any]] | None = None, *, fail: bool = False) -> None:
        self.answers = answers or [(200, {"ok": True})]
        self.asked: list[tuple[str, str, float]] = []
        self.fail = fail

    def __call__(self, method: str, url: str, body: Any, timeout: float) -> tuple[int, Any]:
        self.asked.append((method, url, timeout))
        if self.fail:
            raise ConnectionRefusedError("refused")
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


BASE = "http://169.254.0.2:8930"


def test_a_stop_says_none_and_waits_long_enough() -> None:
    t = Transport()
    answer = StarterClient(BASE, transport=t).stop_sync("notes-app-sbx", deployment=NO_DEPLOYMENT)
    assert answer.ok
    method, url, timeout = t.asked[0]
    assert (method, url) == ("POST", f"{BASE}/stop/notes-app-sbx?deployment=none")
    assert timeout == STOP_TIMEOUT_SECONDS >= 7 * 60


def test_a_start_passes_the_recorded_deployment() -> None:
    t = Transport()
    StarterClient(BASE, transport=t).start_sync(
        "notes-app-sbx",
        deployment={"compose_project": "notes", "identity_label": "app.identity=notes@abc"},
    )
    assert t.asked[0][1] == (
        f"{BASE}/start/notes-app-sbx?compose_project=notes&identity_label=app.identity%3Dnotes%40abc"
    )


def test_unsaid_leaves_the_starters_settings_to_decide() -> None:
    t = Transport()
    StarterClient(BASE, transport=t).start_sync("notes-app-sbx")
    assert t.asked[0][1] == f"{BASE}/start/notes-app-sbx"


def test_a_refusal_is_read_with_its_sentence() -> None:
    t = Transport([(409, {"ok": False, "message": "notes-app-sbx is held open (h1 by register-repo)"})])
    answer = StarterClient(BASE, transport=t).stop_sync("notes-app-sbx")
    assert not answer.ok and answer.refused and "held open" in answer.sentence


def test_a_failure_is_not_a_refusal() -> None:
    t = Transport([(503, {"ok": False, "message": "docker stop did not finish"})])
    answer = StarterClient(BASE, transport=t).stop_sync("notes-app-sbx")
    assert not answer.ok and not answer.refused


def test_suspend_carries_its_reason_in_the_query() -> None:
    t = Transport()
    StarterClient(BASE, transport=t).suspend_sync("notes-app-sbx", "the proxy did not answer")
    assert t.asked[0][1] == f"{BASE}/suspend/notes-app-sbx?reason=the+proxy+did+not+answer"


def test_a_deployed_hold_waits_up_to_fifteen_minutes() -> None:
    t = Transport([(200, {"ok": True, "hold_id": "hold-abc", "readiness": "ready"})])
    answer = StarterClient(BASE, transport=t).hold_sync(
        "notes-app-sbx", owner="merge-deploy", until="deployed"
    )
    assert answer.hold_id == "hold-abc"
    assert t.asked[0][1] == f"{BASE}/hold/notes-app-sbx?owner=merge-deploy&until=deployed"
    assert t.asked[0][2] == HOLD_DEPLOYED_TIMEOUT_SECONDS > 15 * 60


def test_a_hold_the_starter_ended_has_nothing_to_release() -> None:
    t = Transport([(503, {"ok": False, "message": "the coordinator did not record ready", "hold_id": "hold-x", "hold_ended": True})])
    answer = StarterClient(BASE, transport=t).hold_sync("notes-app-sbx", owner="merge-deploy", until="deployed")
    assert answer.hold_id is None and "did not record ready" in answer.sentence


def test_an_unreachable_starter_is_said_never_raised() -> None:
    client = StarterClient(BASE, transport=Transport(fail=True))
    assert client.status_sync("notes-app-sbx").reached is False
    assert client.stop_sync("notes-app-sbx").ok is False
    assert client.hold_sync("notes-app-sbx", owner="x", until="services").hold_id is None
    assert client.release_sync("hold-x") is False
