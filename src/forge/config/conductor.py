"""The conductor's activation seam — one flag, one place to read it.

Revival design pass §a.5 / §h.8 (``supervisor-revival-design-pass-2026-07-31``).

Two surfaces need the same answer to the same question and must never
disagree about it:

1. ``forge queue`` — refuse a build in a mode nothing will ever drive,
   rather than writing a build row that sits silently stuck (risk h.8).
2. the daemon's driver loop (Stage 1c) — decide whether a dequeued build
   is handed to the conductor at all.

So the read lives here, once. :func:`conductor_enabled` is the accessor
Stage 1c reuses verbatim; it defaults **OFF** and degrades to OFF for any
config shape it does not recognise. There is deliberately no way to turn
the conductor on by accident: an absent section, an absent field, a
``None`` config, or a config object of the wrong shape all answer
``False``.

Mode vocabulary (plain names — the phrase-book is law on user surfaces):

* **the routine build** (codename Mode A) — always available. The
  pipeline picks up one queued build, runs it, records the outcome.
* **the full journey** (codename Mode B) — RETIRED as a production
  destination by Rich's 2026-07-31 ruling: it is superseded by the
  spec-writer chain (codename Mode P), which has been the live path
  since 2026-07-16. Refused at queue time, always, flag or no flag.
* **the fix journey** (codename Mode C) — review a failed build, work
  through bounded fixes, hand back a gates-green branch. Driven by the
  conductor, so it is refused until the conductor is switched on.
"""

from __future__ import annotations

from typing import Any

from forge.lifecycle.modes import BuildMode

__all__ = [
    "CONDUCTOR_FLAG_PATH",
    "MODE_B_RETIRED_MESSAGE",
    "MODE_C_NOT_ACTIVATED_MESSAGE",
    "conductor_enabled",
    "mode_refusal_reason",
]


#: Dotted path of the activation flag inside ``forge.yaml``. Quoted in
#: operator-facing messages so nobody has to grep the source to find the
#: switch.
CONDUCTOR_FLAG_PATH: str = "conductor.enabled"


#: Plain-language refusal for the retired full journey. No flag turns
#: this back on — the mechanism it duplicated is live and better.
MODE_B_RETIRED_MESSAGE: str = (
    "forge queue --mode b is refused: that mode is retired.\n"
    "The full journey — one plain sentence through to a merged feature — is "
    "handled by the spec-writer chain, which has been the live path since "
    "2026-07-16. Nothing in production drives --mode b, so a queued row "
    "would sit stuck forever.\n"
    "Nothing was queued. Use --mode a for a routine build, or start the "
    "journey through the spec-writer chain."
)


#: Plain-language refusal for the fix journey while the conductor is off.
MODE_C_NOT_ACTIVATED_MESSAGE: str = (
    "forge queue --mode c is refused: the conductor is not switched on.\n"
    "The fix journey — review a failed build, work through bounded fixes, "
    "hand back a gates-green branch — is driven by the conductor, and the "
    "conductor is inert in this configuration. Nothing would pick this "
    "build up; the row would sit stuck forever.\n"
    f"Nothing was queued. Switch it on by setting '{CONDUCTOR_FLAG_PATH}: "
    "true' in forge.yaml once the fix journey has been activated."
)


def conductor_enabled(config: Any) -> bool:
    """Return ``True`` iff the conductor is switched on in ``config``.

    The accessor is deliberately tolerant on the way *down* and strict on
    the way *up*: anything it cannot read as an explicit ``True`` answers
    ``False``. That is the safe direction — a misread config leaves the
    conductor inert and the tree byte-for-byte today's behaviour.

    Args:
        config: A :class:`forge.config.models.ForgeConfig` (or any object
            exposing a ``conductor`` attribute with an ``enabled`` field).
            ``None`` and unrecognised shapes both answer ``False``.

    Returns:
        ``True`` only when ``conductor.enabled`` reads as a true value.
    """
    if config is None:
        return False
    conductor = getattr(config, "conductor", None)
    if conductor is None:
        return False
    return bool(getattr(conductor, "enabled", False))


def mode_refusal_reason(mode: BuildMode, config: Any) -> str | None:
    """Return the plain-language refusal for ``mode``, or ``None`` to allow.

    This is the single decision point for "may a build be queued in this
    mode at all?". It reads the same flag the driver loop reads, so the
    queue can never mint a row the daemon would refuse to drive.

    Args:
        mode: The resolved :class:`BuildMode` for the queue attempt.
        config: The loaded config, forwarded to :func:`conductor_enabled`.

    Returns:
        ``None`` when the mode is activated and the build may be queued;
        otherwise the operator-facing refusal message. The caller is
        responsible for printing it and exiting nonzero *before* any
        build row is written.
    """
    if mode is BuildMode.MODE_B:
        return MODE_B_RETIRED_MESSAGE
    if mode is BuildMode.MODE_C and not conductor_enabled(config):
        return MODE_C_NOT_ACTIVATED_MESSAGE
    return None
