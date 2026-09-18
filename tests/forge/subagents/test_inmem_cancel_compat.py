from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from forge.subagents.inmem_cancel_compat import (
    _AFFECTED_SOURCE_SHA256,
    _persistent_listener,
    install_inmem_cancel_listener_compat,
)


@pytest.mark.asyncio
async def test_wrapper_rearms_after_idle_return_then_preserves_interrupt() -> None:
    calls = 0
    done = asyncio.Event()

    async def original(queue, run_id, thread_id, event) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            event.set()

    wrapped = _persistent_listener(original)
    await wrapped(object(), "run", "thread", done)

    assert calls == 2
    assert done.is_set()


@pytest.mark.asyncio
async def test_wrapper_preserves_normal_done_without_rearming() -> None:
    calls = 0
    done = asyncio.Event()

    async def original(queue, run_id, thread_id, event) -> None:
        nonlocal calls
        calls += 1
        event.set()

    await _persistent_listener(original)(object(), "run", "thread", done)

    assert calls == 1


@pytest.mark.asyncio
async def test_wrapper_propagates_task_cancellation() -> None:
    entered = asyncio.Event()

    async def original(queue, run_id, thread_id, event) -> None:
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        _persistent_listener(original)(object(), "run", "thread", asyncio.Event())
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_wrapper_preserves_rollback_value_and_stops() -> None:
    rollback = RuntimeError("rollback sentinel")

    class Done:
        value = None

        def is_set(self) -> bool:
            return self.value is not None

        def set(self, value=True) -> None:
            self.value = value

    done = Done()
    calls = 0

    async def original(queue, run_id, thread_id, event) -> None:
        nonlocal calls
        calls += 1
        event.set(rollback)

    await _persistent_listener(original)(object(), "run", "thread", done)

    assert calls == 1
    assert done.value is rollback


@pytest.mark.asyncio
async def test_wrapper_propagates_listener_exception() -> None:
    async def original(queue, run_id, thread_id, event) -> None:
        raise LookupError("listener failed")

    with pytest.raises(LookupError, match="listener failed"):
        await _persistent_listener(original)(object(), "run", "thread", asyncio.Event())


def test_installer_refuses_unreviewed_source_without_replacing_listener() -> None:
    async def original(queue, run_id, thread_id, done) -> None:
        return None

    ops = SimpleNamespace(listen_for_cancellation=original)
    with pytest.raises(RuntimeError, match="refusing autobuild graph startup"):
        install_inmem_cancel_listener_compat(
            _ops=ops,
            _runtime_version="0.34.1",
            _source_text="different source",
        )

    assert ops.listen_for_cancellation is original


def test_installer_wraps_exact_reviewed_identity(monkeypatch) -> None:
    async def original(queue, run_id, thread_id, done) -> None:
        done.set()

    ops = SimpleNamespace(listen_for_cancellation=original)
    monkeypatch.setattr(
        "forge.subagents.inmem_cancel_compat.hashlib.sha256",
        lambda _value: SimpleNamespace(hexdigest=lambda: _AFFECTED_SOURCE_SHA256),
    )

    assert (
        install_inmem_cancel_listener_compat(
            _ops=ops,
            _runtime_version="0.34.1",
            _source_text="reviewed source",
        )
        == "installed"
    )
    assert ops.listen_for_cancellation is not original
    assert (
        install_inmem_cancel_listener_compat(
            _ops=ops,
            _runtime_version="0.34.1",
            _source_text="reviewed source",
        )
        == "already-installed"
    )


def test_installer_leaves_other_runtime_versions_unchanged() -> None:
    async def original(queue, run_id, thread_id, done) -> None:
        done.set()

    ops = SimpleNamespace(listen_for_cancellation=original)
    result = install_inmem_cancel_listener_compat(
        _ops=ops,
        _runtime_version="0.35.0",
        _source_text="not inspected for another version",
    )

    assert result == "version-mismatch"
    assert ops.listen_for_cancellation is original
