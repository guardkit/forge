"""Which model writes this factory's code, decided at daemon start-up.

With ``routine.seat`` named in ``forge.yaml`` the routine dispatch is bound
to that seat and every routine build carries it as ``--model <seat>``. With
no seat named the composition root hands back the dispatch function itself,
untouched, so a routine build is dispatched exactly as it is today and the
build system's own default still applies.

The point of the second half is the one that matters in production: every
``forge.yaml`` deployed today names no routine seat, and none of them may
change behaviour because this lever exists.
"""

from __future__ import annotations

import asyncio
from typing import Any

from forge.cli.serve import compose_routine_subprocess_dispatcher
from forge.config.models import ForgeConfig
from forge.pipeline.dispatchers import subprocess_dispatcher
from forge.pipeline.dispatchers.subprocess_dispatcher import dispatch_subprocess_stage


def _config(seat: object = None) -> ForgeConfig:
    body: dict[str, object] = {
        "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
    }
    if seat is not None:
        body["routine"] = {"seat": seat}
    return ForgeConfig.model_validate(body)


def _what_the_dispatch_passes_on(monkeypatch, seat: str) -> dict[str, Any]:
    """Compose with ``seat`` named, call the dispatch once, and return the
    keyword arguments that reached the real dispatch function.

    Since 9 October 2026 (42554fb9) a named seat is carried by a small
    wrapper that also forwards the feature-routing fields, not by a
    ``functools.partial``, so the binding is checked by what the wrapped
    function is actually given.
    """
    seen: dict[str, Any] = {}

    async def recorder(*args: Any, **kwargs: Any) -> str:
        seen["args"] = args
        seen["kwargs"] = kwargs
        return "dispatched"

    monkeypatch.setattr(subprocess_dispatcher, "dispatch_subprocess_stage", recorder)
    dispatch = compose_routine_subprocess_dispatcher(_config(seat))
    assert asyncio.run(dispatch(correlation_id="corr-1")) == "dispatched"
    return seen


def test_with_no_seat_named_the_dispatch_is_handed_back_untouched(
    caplog,
) -> None:
    with caplog.at_level("INFO"):
        dispatch = compose_routine_subprocess_dispatcher(_config())

    assert dispatch is dispatch_subprocess_stage
    lines = [record.getMessage() for record in caplog.records]
    assert any("no routine seat is named" in line for line in lines)


def test_a_named_seat_is_bound_onto_the_routine_dispatch(caplog, monkeypatch) -> None:
    with caplog.at_level("INFO"):
        seen = _what_the_dispatch_passes_on(monkeypatch, "qwen3-coder-30b")

    assert seen["args"] == ()
    assert seen["kwargs"]["routine_seat"] == "qwen3-coder-30b"
    assert seen["kwargs"]["correlation_id"] == "corr-1"
    lines = [record.getMessage() for record in caplog.records]
    assert any("routine builds run on" in line for line in lines)


def test_the_composition_root_passes_the_configured_value_through(monkeypatch) -> None:
    """It reads the operator's seat; it never invents one of its own.

    A default chosen here would be exactly the silent decision this lever
    exists to end — so whatever the config says, character for character
    (after the strip config load already did), is what gets bound.
    """
    for seat in ("qwen3-coder-30b", "gpt-oss-120b", "some-other-seat"):
        seen = _what_the_dispatch_passes_on(monkeypatch, seat)
        assert seen["kwargs"]["routine_seat"] == seat


def test_a_blank_seat_composes_as_no_seat_at_all() -> None:
    """Config load reads blank as absent; the composition root agrees."""
    dispatch = compose_routine_subprocess_dispatcher(_config("   "))

    assert dispatch is dispatch_subprocess_stage


def test_settings_with_no_routine_section_object_still_compose() -> None:
    """A settings object from an older shape must not break the boot.

    The langgraph sidecar and a couple of test paths hand this function
    whatever config object they hold; a missing section reads as "no seat
    named", which is the same as today.
    """

    class _OldSettings:
        pass

    assert (
        compose_routine_subprocess_dispatcher(_OldSettings())
        is dispatch_subprocess_stage
    )
