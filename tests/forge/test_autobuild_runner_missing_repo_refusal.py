"""The LOUD missing-repo refusal (second-repo readiness, 2026-07-31).

A launch that arrives with no ``repo`` used to fall through to
``FORGE_DEFAULT_REPO`` unconditionally, at INFO, as a TASK-ABW-002 hotfix. With
exactly one registered target that was survivable. With a second target it is a
WRONG-REPO BUILD THAT LOOKS GREEN: the daemon builds repo #1, every gate passes
against repo #1's tests, and the feature that belonged to repo #2 is reported
delivered.

These tests pin the cure:

* a repo-less launch is REFUSED — ``_resolve_repo_path`` returns ``None`` and
  the terminal reason is the plain-words :data:`MISSING_REPO_REFUSAL`, never a
  silent default and never the generic ``repo=None`` wording;
* ``FORGE_DEFAULT_REPO`` is honoured ONLY when ``FORGE_DEFAULT_REPO_OPT_IN``
  explicitly rides with it, and then it announces itself;
* the opt-in fails CLOSED on anything that is not an affirmative value, so a
  typo in the systemd unit is a refusal rather than a wrong-repo build.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from forge.subagents.autobuild_runner import (
    FORGE_DEFAULT_REPO_ENV,
    FORGE_DEFAULT_REPO_OPT_IN_ENV,
    FORGE_REPO_BASE_ENV,
    MISSING_DEFAULT_REPO_REFUSAL,
    MISSING_REPO_REFUSAL,
    _default_repo_opt_in_enabled,
    _resolve_repo_path,
    repo_resolution_failure_reason,
)


@pytest.fixture(autouse=True)
def _clean_repo_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test states its own environment — none inherits the host's."""
    monkeypatch.delenv(FORGE_DEFAULT_REPO_ENV, raising=False)
    monkeypatch.delenv(FORGE_DEFAULT_REPO_OPT_IN_ENV, raising=False)
    monkeypatch.delenv(FORGE_REPO_BASE_ENV, raising=False)


def _make_checkout(base: Path, name: str) -> Path:
    """Create ``base/name`` as a real git repo (the resolver checks ``.git``)."""
    repo = base / name
    repo.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-q", str(repo)], check=True, capture_output=True
    )
    return repo


# ---------------------------------------------------------------------------
# The refusal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"repo": None},
        {"repo": ""},
        {"repo": "   "},
        {"repo": 42},
    ],
    ids=["absent", "none", "empty", "blank", "not-a-string"],
)
def test_repoless_launch_is_refused_even_with_a_default_repo_set(
    payload: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The default exists, is resolvable, and is STILL not used without opt-in."""
    _make_checkout(tmp_path, "api_test")
    monkeypatch.setenv(FORGE_REPO_BASE_ENV, str(tmp_path))
    monkeypatch.setenv(FORGE_DEFAULT_REPO_ENV, "appmilla/api_test")

    with caplog.at_level("ERROR"):
        assert _resolve_repo_path(payload) is None

    # LOUD: an ERROR naming the defect, not an INFO shrug.
    assert any(
        "refusing to build" in record.getMessage() for record in caplog.records
    ), caplog.text
    assert repo_resolution_failure_reason(payload) == MISSING_REPO_REFUSAL


def test_the_refusal_names_the_defect_and_both_cures() -> None:
    """The terminal speaks plainly — no internal ids, no 'repo=None'."""
    assert "no repo" in MISSING_REPO_REFUSAL
    assert "refusing to build" in MISSING_REPO_REFUSAL
    # Names the consequence.
    assert "DIFFERENT repository" in MISSING_REPO_REFUSAL
    assert "looks green" in MISSING_REPO_REFUSAL
    # Names both cures.
    assert "Send 'repo' in the launch payload" in MISSING_REPO_REFUSAL
    assert "FORGE_DEFAULT_REPO_OPT_IN=1" in MISSING_REPO_REFUSAL


def test_a_resolvable_payload_repo_keeps_the_historical_reason_wording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repo that IS named but cannot be resolved is a different failure."""
    monkeypatch.setenv(FORGE_REPO_BASE_ENV, str(tmp_path))
    payload = {"repo": "appmilla/not_cloned"}
    assert _resolve_repo_path(payload) is None
    reason = repo_resolution_failure_reason(payload)
    assert reason == "unable to resolve repo path for repo='appmilla/not_cloned'"
    assert reason != MISSING_REPO_REFUSAL


# ---------------------------------------------------------------------------
# The opt-in
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on", " 1 "])
def test_opt_in_accepts_affirmative_values(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(FORGE_DEFAULT_REPO_OPT_IN_ENV, value)
    assert _default_repo_opt_in_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "yes please", "y"])
def test_opt_in_fails_closed_on_anything_else(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo in the unit file must produce a REFUSAL, never a wrong-repo build."""
    monkeypatch.setenv(FORGE_DEFAULT_REPO_OPT_IN_ENV, value)
    assert _default_repo_opt_in_enabled() is False


def test_default_repo_is_used_under_an_explicit_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    repo = _make_checkout(tmp_path, "api_test")
    monkeypatch.setenv(FORGE_REPO_BASE_ENV, str(tmp_path))
    monkeypatch.setenv(FORGE_DEFAULT_REPO_ENV, "appmilla/api_test")
    monkeypatch.setenv(FORGE_DEFAULT_REPO_OPT_IN_ENV, "1")

    with caplog.at_level("WARNING"):
        resolved = _resolve_repo_path({})

    assert resolved == repo.resolve()
    # Honoured, but never silently: a WARNING naming the repo and the opt-in.
    assert any(
        "appmilla/api_test" in record.getMessage()
        and FORGE_DEFAULT_REPO_OPT_IN_ENV in record.getMessage()
        for record in caplog.records
    ), caplog.text


def test_opt_in_without_a_default_repo_is_its_own_refusal(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(FORGE_DEFAULT_REPO_OPT_IN_ENV, "1")
    with caplog.at_level("ERROR"):
        assert _resolve_repo_path({}) is None
    assert repo_resolution_failure_reason({}) == MISSING_DEFAULT_REPO_REFUSAL
    assert "FORGE_DEFAULT_REPO is empty" in MISSING_DEFAULT_REPO_REFUSAL


def test_an_explicit_payload_repo_never_consults_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second-repo geometry: the payload wins, the default is not even read."""
    _make_checkout(tmp_path, "api_test")
    ts_repo = _make_checkout(tmp_path, "ts-api-test")
    monkeypatch.setenv(FORGE_REPO_BASE_ENV, str(tmp_path))
    monkeypatch.setenv(FORGE_DEFAULT_REPO_ENV, "appmilla/api_test")
    monkeypatch.setenv(FORGE_DEFAULT_REPO_OPT_IN_ENV, "1")

    resolved = _resolve_repo_path({"repo": "appmilla/ts-api-test"})
    assert resolved == ts_repo.resolve()


# ---------------------------------------------------------------------------
# The terminal the operator actually sees
# ---------------------------------------------------------------------------


def test_the_refusal_rides_the_failed_snapshot_as_error_message() -> None:
    """``error_message`` is the field the lifecycle bridge puts on the wire as
    ``failure_reason`` — the refusal must land there, not in a log only."""
    from forge.subagents.autobuild_runner import _build_failed_snapshot

    payload = {"feature_id": "FEAT-TS1", "build_id": "build-1"}
    snapshot = _build_failed_snapshot(
        payload, reason=repo_resolution_failure_reason(payload)
    )
    assert snapshot["error_message"] == MISSING_REPO_REFUSAL
