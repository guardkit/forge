"""Pre-run resource-headroom preflight (O-27 / O-29 — E2-S4 stretch).

A run must fail CLEANLY *before* it starts when the box is already under its
memory or disk floor — never mid-run with a kernel OOM-kill (O-27, run-11
journal verbatim: "killed by the OOM killer" at ~19 min of sustained Coach
reasoning) or a JetStream/worktree write wedged by ENOSPC (O-29). This module is
the small, side-effect-light helper the run-entry paths consult: it READS
available system memory + working-filesystem free space, compares each against a
configurable floor, and returns a structured verdict. *Refusing* the run and
*notifying* (route-and-notify, DDR-007) is the caller's job — this module never
imports NATS, so it stays a pure, trivially-testable unit.

Defaults are conservative and ON. The co-resident 4-model seat stack sits at a
steady-state ~14 GB headroom (gap-analysis O-27), so an 8 GB memory floor
refuses only a genuinely starved box; the 20 GB disk floor keeps clear of the
10 GB JetStream store plus rollback-image churn (O-29). The check only ever
refuses BEFORE work starts, so leaving it enabled is safe — it can never kill a
run in flight.

An *unreadable* resource (e.g. ``/proc/meminfo`` absent on a non-Linux dev host,
or ``disk_usage`` raising) is treated as UNCHECKED, never a fabricated breach:
the preflight fails open per-resource so a reading quirk can never wedge the
factory. The one thing it will not do is let a *readably-starved* box start.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

logger = logging.getLogger(__name__)

__all__ = [
    "GIB",
    "MEMORY_RESOURCE",
    "DISK_RESOURCE",
    "ResourceBreach",
    "ResourcePreflightResult",
    "ResourcePreflightConfigLike",
    "read_available_memory_bytes",
    "read_available_disk_bytes",
    "evaluate_resource_preflight",
    "run_resource_preflight",
]

#: One gibibyte in bytes — floors are expressed in GiB for operator readability.
GIB = 1024 ** 3

MEMORY_RESOURCE = "memory"
DISK_RESOURCE = "disk"

#: ``/proc/meminfo`` key carrying the kernel's own estimate of allocatable
#: memory without swapping — the right signal for "can a heavy turn start".
_MEMINFO_PATH = Path("/proc/meminfo")
_MEMAVAILABLE_KEY = "MemAvailable:"

#: Reader seams — zero-argument (memory) / one-argument (disk) callables the
#: caller may substitute in tests. Prod binds the real ``/proc`` + ``shutil``
#: readers below.
MemoryReader = Callable[[], "int | None"]
DiskReader = Callable[[Path], "int | None"]


class ResourcePreflightConfigLike(Protocol):
    """Structural view of the config the preflight consults.

    Kept as a Protocol (not an import of ``forge.config.models``) so this module
    stays dependency-light and import-cycle-free; the real
    ``ResourcePreflightConfig`` pydantic model satisfies it structurally.
    """

    enabled: bool
    min_available_memory_gb: float
    min_available_disk_gb: float
    working_path: str | None


@dataclass(frozen=True)
class ResourceBreach:
    """One resource found below its configured floor at run start."""

    resource: str
    available_gb: float
    floor_gb: float

    @property
    def detail(self) -> str:
        return (
            f"{self.resource} {self.available_gb:.1f} GB "
            f"< {self.floor_gb:.1f} GB floor"
        )


@dataclass(frozen=True)
class ResourcePreflightResult:
    """Verdict of a pre-run resource check.

    ``ok`` is True when nothing *readable* is below its floor (an unchecked
    resource never fails the run). ``breaches`` is the loud specific detail the
    caller routes-and-notifies on; ``unchecked`` names any resource whose
    reading was unavailable (surfaced for diagnostics, never a refusal).
    """

    ok: bool
    breaches: tuple[ResourceBreach, ...] = ()
    checked: tuple[str, ...] = ()
    unchecked: tuple[str, ...] = ()

    @property
    def summary(self) -> str:
        """A single loud, specific line for the FAILED terminal + notification."""
        if self.ok:
            checked = ", ".join(self.checked) or "nothing"
            return f"Resource preflight OK (checked: {checked})."
        breaches = "; ".join(b.detail for b in self.breaches)
        return (
            f"Resource preflight FAILED: {breaches}. Refusing the run before it "
            "starts (O-27/O-29) rather than risk a mid-run OOM-kill / ENOSPC."
        )


def read_available_memory_bytes() -> int | None:
    """Available system memory in bytes from ``/proc/meminfo`` (Linux).

    Returns ``None`` when the file is absent or the ``MemAvailable`` line cannot
    be parsed (non-Linux host, unexpected format) — the caller treats that as
    UNCHECKED, never a breach.
    """
    try:
        text = _MEMINFO_PATH.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith(_MEMAVAILABLE_KEY):
            parts = line.split()
            # Format: "MemAvailable:   12345678 kB"
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1]) * 1024
            return None
    return None


def read_available_disk_bytes(path: Path) -> int | None:
    """Free bytes on the filesystem holding ``path`` (``shutil.disk_usage``).

    Returns ``None`` when the path is unreadable — treated as UNCHECKED.
    """
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def evaluate_resource_preflight(
    *,
    enabled: bool,
    min_memory_gb: float,
    min_disk_gb: float,
    available_memory_bytes: int | None,
    available_disk_bytes: int | None,
) -> ResourcePreflightResult:
    """Pure verdict from already-taken readings — the testable core.

    ``enabled=False`` short-circuits to OK with nothing checked (byte-no-op). A
    ``None`` reading marks that resource UNCHECKED (never a breach). A reading at
    or above its floor passes; strictly below fails loudly.
    """
    if not enabled:
        return ResourcePreflightResult(ok=True)

    breaches: list[ResourceBreach] = []
    checked: list[str] = []
    unchecked: list[str] = []

    for resource, reading, floor_gb in (
        (MEMORY_RESOURCE, available_memory_bytes, min_memory_gb),
        (DISK_RESOURCE, available_disk_bytes, min_disk_gb),
    ):
        if reading is None:
            unchecked.append(resource)
            logger.warning(
                "resource preflight: %s reading unavailable — skipping (UNCHECKED)",
                resource,
            )
            continue
        checked.append(resource)
        available_gb = reading / GIB
        if available_gb < floor_gb:
            breaches.append(
                ResourceBreach(
                    resource=resource,
                    available_gb=available_gb,
                    floor_gb=floor_gb,
                )
            )

    return ResourcePreflightResult(
        ok=not breaches,
        breaches=tuple(breaches),
        checked=tuple(checked),
        unchecked=tuple(unchecked),
    )


def run_resource_preflight(
    config: ResourcePreflightConfigLike,
    *,
    working_path: Path | None = None,
    read_memory: MemoryReader = read_available_memory_bytes,
    read_disk: DiskReader = read_available_disk_bytes,
) -> ResourcePreflightResult:
    """Read the box and return a verdict — the run-entry seam.

    Bound (e.g. via ``functools.partial(run_resource_preflight, cfg)``) into the
    driver as a zero-argument callable so the driver stays ignorant of ``/proc``
    and ``shutil``. When ``config.enabled`` is False no reading is taken at all.

    Args:
        config: The resource-preflight config (floors + working path).
        working_path: Filesystem whose free space is checked; defaults to
            ``config.working_path`` and finally the process CWD.
        read_memory / read_disk: Injectable reader seams (tests).
    """
    if not config.enabled:
        return ResourcePreflightResult(ok=True)

    target = working_path
    if target is None:
        configured = (config.working_path or "").strip()
        target = Path(configured) if configured else Path.cwd()

    return evaluate_resource_preflight(
        enabled=True,
        min_memory_gb=config.min_available_memory_gb,
        min_disk_gb=config.min_available_disk_gb,
        available_memory_bytes=read_memory(),
        available_disk_bytes=read_disk(target),
    )
