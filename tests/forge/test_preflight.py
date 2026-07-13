"""O-27 / O-29 (E2-S4) — pre-run resource-headroom preflight.

The pure helper that lets a run fail CLEANLY before it starts instead of a
mid-run kernel OOM-kill (O-27) / ENOSPC-wedged write (O-29). Every path is
exercised with MOCKED readings (no real ``/proc`` or ``shutil`` dependency), so
the breach / no-breach verdict is deterministic on any host.
"""

from __future__ import annotations

from pathlib import Path

from forge.config.models import ResourcePreflightConfig
from forge.preflight import (
    DISK_RESOURCE,
    GIB,
    MEMORY_RESOURCE,
    ResourceBreach,
    ResourcePreflightResult,
    evaluate_resource_preflight,
    read_available_disk_bytes,
    read_available_memory_bytes,
    run_resource_preflight,
)


# --------------------------------------------------------------------------- #
# evaluate_resource_preflight — the pure verdict core (mocked readings)
# --------------------------------------------------------------------------- #


def _evaluate(mem_gb: float | None, disk_gb: float | None) -> ResourcePreflightResult:
    return evaluate_resource_preflight(
        enabled=True,
        min_memory_gb=8.0,
        min_disk_gb=20.0,
        available_memory_bytes=None if mem_gb is None else int(mem_gb * GIB),
        available_disk_bytes=None if disk_gb is None else int(disk_gb * GIB),
    )


def test_no_breach_when_both_above_floor() -> None:
    result = _evaluate(mem_gb=14.0, disk_gb=100.0)
    assert result.ok is True
    assert result.breaches == ()
    assert set(result.checked) == {MEMORY_RESOURCE, DISK_RESOURCE}
    assert result.unchecked == ()


def test_memory_breach_is_loud_and_specific() -> None:
    result = _evaluate(mem_gb=3.2, disk_gb=100.0)
    assert result.ok is False
    assert len(result.breaches) == 1
    breach = result.breaches[0]
    assert breach.resource == MEMORY_RESOURCE
    assert breach.floor_gb == 8.0
    assert "memory 3.2 GB < 8.0 GB floor" in breach.detail
    assert "FAILED" in result.summary
    assert "O-27/O-29" in result.summary


def test_disk_breach_detected() -> None:
    result = _evaluate(mem_gb=14.0, disk_gb=12.0)
    assert result.ok is False
    assert [b.resource for b in result.breaches] == [DISK_RESOURCE]
    assert "disk 12.0 GB < 20.0 GB floor" in result.summary


def test_both_breaching_are_both_reported() -> None:
    result = _evaluate(mem_gb=1.0, disk_gb=1.0)
    assert result.ok is False
    assert {b.resource for b in result.breaches} == {MEMORY_RESOURCE, DISK_RESOURCE}
    assert "memory" in result.summary and "disk" in result.summary


def test_reading_exactly_at_floor_passes() -> None:
    # >= floor passes; only STRICTLY below refuses.
    result = _evaluate(mem_gb=8.0, disk_gb=20.0)
    assert result.ok is True
    assert result.breaches == ()


def test_unreadable_resource_is_unchecked_never_a_breach() -> None:
    # A None memory reading (non-Linux host) fails OPEN — never a fabricated
    # breach — while disk is still checked.
    result = _evaluate(mem_gb=None, disk_gb=100.0)
    assert result.ok is True
    assert result.unchecked == (MEMORY_RESOURCE,)
    assert result.checked == (DISK_RESOURCE,)


def test_disabled_is_a_byte_no_op() -> None:
    result = evaluate_resource_preflight(
        enabled=False,
        min_memory_gb=8.0,
        min_disk_gb=20.0,
        available_memory_bytes=0,  # would breach hard if consulted
        available_disk_bytes=0,
    )
    assert result.ok is True
    assert result.breaches == ()
    assert result.checked == ()


# --------------------------------------------------------------------------- #
# run_resource_preflight — the seam with injectable readers
# --------------------------------------------------------------------------- #


def _cfg(**overrides: object) -> ResourcePreflightConfig:
    base: dict[str, object] = {
        "enabled": True,
        "min_available_memory_gb": 8.0,
        "min_available_disk_gb": 20.0,
    }
    base.update(overrides)
    return ResourcePreflightConfig(**base)  # type: ignore[arg-type]


def test_run_uses_injected_readers_and_passes() -> None:
    seen: list[Path] = []

    def read_mem() -> int:
        return int(14 * GIB)

    def read_disk(path: Path) -> int:
        seen.append(path)
        return int(100 * GIB)

    result = run_resource_preflight(
        _cfg(),
        working_path=Path("/srv/factory"),
        read_memory=read_mem,
        read_disk=read_disk,
    )
    assert result.ok is True
    assert seen == [Path("/srv/factory")]  # disk checked on the given path


def test_run_refuses_on_injected_breach() -> None:
    result = run_resource_preflight(
        _cfg(),
        working_path=Path("/srv/factory"),
        read_memory=lambda: int(2 * GIB),
        read_disk=lambda _p: int(100 * GIB),
    )
    assert result.ok is False
    assert result.breaches[0].resource == MEMORY_RESOURCE


def test_run_disabled_takes_no_reading() -> None:
    def boom() -> int:  # pragma: no cover - must never be called
        raise AssertionError("readers must not run when preflight disabled")

    result = run_resource_preflight(
        _cfg(enabled=False),
        read_memory=boom,
        read_disk=lambda _p: boom(),
    )
    assert result.ok is True


def test_run_defaults_working_path_to_config_then_cwd() -> None:
    captured: list[Path] = []

    run_resource_preflight(
        _cfg(working_path="/data/jetstream"),
        read_memory=lambda: int(14 * GIB),
        read_disk=lambda p: captured.append(p) or int(100 * GIB),  # type: ignore[func-returns-value,return-value]
    )
    assert captured == [Path("/data/jetstream")]

    captured.clear()
    run_resource_preflight(
        _cfg(working_path=None),
        read_memory=lambda: int(14 * GIB),
        read_disk=lambda p: captured.append(p) or int(100 * GIB),  # type: ignore[func-returns-value,return-value]
    )
    assert captured == [Path.cwd()]


# --------------------------------------------------------------------------- #
# The real readers — smoke only (never assert host-specific values)
# --------------------------------------------------------------------------- #


def test_real_memory_reader_returns_positive_or_none() -> None:
    value = read_available_memory_bytes()
    assert value is None or value > 0


def test_real_disk_reader_on_tmp_path(tmp_path: Path) -> None:
    value = read_available_disk_bytes(tmp_path)
    assert value is not None and value > 0


def test_breach_detail_formatting() -> None:
    breach = ResourceBreach(resource=MEMORY_RESOURCE, available_gb=3.25, floor_gb=8.0)
    assert breach.detail == "memory 3.2 GB < 8.0 GB floor"
