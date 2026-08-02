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

**The cap law** (leg-invocation stage-2 design §4) lives here too, for
exactly the same reason the activation flag does: two surfaces — ``forge
queue`` and the daemon's conductor router — must never disagree about
whether a fix journey may open. A fix journey with no review-cycle cap is
the runaway this law exists to make impossible: the 2026-08-02 crossing
ran ~200 legs because the build resolved a profile whose every cap was
``None`` and the budget guard is, by design, a strict no-op for such a
profile. So an unset cap is REFUSED — never silently read as "unlimited".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from forge.lifecycle.modes import BuildMode

__all__ = [
    "CONDUCTOR_FLAG_PATH",
    "MODE_B_RETIRED_MESSAGE",
    "MODE_C_MIN_REVIEW_CYCLES",
    "MODE_C_NOT_ACTIVATED_MESSAGE",
    "ModeCCapRefusal",
    "UNCAPPED_ESCAPE_PROFILE_NAME",
    "conductor_enabled",
    "mode_c_cap_refusal",
    "mode_c_cap_refusal_from_config",
    "mode_refusal_reason",
    "uncapped_escape_applies",
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


#: THE CAP FLOOR (stage-2 design §4, resting on the cap-mapping law in
#: ``config/models.py``). ``max_review_cycles`` counts EVERY review a fix
#: journey runs, and a bounded journey runs two: the initial review that
#: finds the fix tasks, and the ONE follow-up that confirms they landed.
#: So ``1`` is not "tighter", it is a trap — it breaches at the mandatory
#: follow-up and the journey can never finish. Two is the floor.
MODE_C_MIN_REVIEW_CYCLES: int = 2


#: The ONE deliberately-named uncapped profile (stage-2 design §4, "the
#: escape stays honest"). The reserved ``attended`` profile is NOT it —
#: its reservation stands, and an uncapped fix journey has to be asked for
#: by name AND acknowledged in the same invocation. The daemon's router
#: refuses the escape outright regardless: it is a sandbox-drive door, not
#: a production one.
UNCAPPED_ESCAPE_PROFILE_NAME: str = "sandbox-uncapped"


@dataclass(frozen=True)
class ModeCCapRefusal:
    """One refusal of the cap law, in the two lengths its readers need.

    Attributes:
        profile: The effective profile name the refusal is about, when it
            could be determined (``None`` when the profile could not even
            be resolved — the name is then quoted inside ``detail``).
        summary: One line, safe for a database column or a log line.
        message: The full readable refusal, for a human at a terminal.
    """

    profile: str | None
    summary: str
    message: str


def _cap_refusal(
    profile: str | None,
    headline: str,
    detail: str,
) -> ModeCCapRefusal:
    """Compose a :class:`ModeCCapRefusal` from its one variable part."""
    summary = f"the fix journey is refused: {headline}"
    message = (
        f"{summary}\n"
        f"{detail}\n"
        "A fix journey with no review-cycle cap is the runaway: the build "
        "keeps re-reviewing, re-minting the same findings under fresh ids, "
        "and nothing ever stops it. An unset cap is therefore refused — it "
        "is never read as 'unlimited'.\n"
        "The fix: give the profile a review-cycle cap of at least "
        f"{MODE_C_MIN_REVIEW_CYCLES} in forge.yaml, e.g.\n"
        "    budget:\n"
        "      profiles:\n"
        "        fix-journey:\n"
        f"          max_review_cycles: {MODE_C_MIN_REVIEW_CYCLES}\n"
        "          max_build_wallclock_seconds: 3600\n"
        "then queue the fix journey with '--profile fix-journey'."
    )
    return ModeCCapRefusal(profile=profile, summary=summary, message=message)


def uncapped_escape_applies(
    profile_name: str | None,
    uncapped_acknowledged: bool,
) -> bool:
    """Return ``True`` iff the honest sandbox escape is in play.

    Stated once, here, so the belt that enforces it and the surface that
    narrates it can never drift apart. BOTH halves are required: the
    profile is named :data:`UNCAPPED_ESCAPE_PROFILE_NAME` *and* the same
    invocation acknowledges what it is asking for.
    """
    return bool(uncapped_acknowledged) and profile_name == (
        UNCAPPED_ESCAPE_PROFILE_NAME
    )


def mode_c_cap_refusal(
    *,
    profile_name: str | None,
    guards: Any,
    resolve_error: str | None = None,
    uncapped_acknowledged: bool = False,
) -> ModeCCapRefusal | None:
    """THE cap law, stated once: may a fix journey open under these caps?

    A mode-c build may not open unless the profile it resolved carries
    ``max_review_cycles`` as a positive int of at least
    :data:`MODE_C_MIN_REVIEW_CYCLES`.

    Args:
        profile_name: The EFFECTIVE profile name (the requested one, or
            the config default when none was requested). ``None`` when it
            could not be determined.
        guards: The resolved :class:`~forge.config.models.BudgetGuards`
            (anything exposing ``max_review_cycles``). Ignored when
            ``resolve_error`` is set.
        resolve_error: Set when the profile could not be resolved at all —
            the "profile absent" arm, which is what
            ``BudgetConfig.resolve``'s ``KeyError`` means. It surfaces as
            the same readable refusal, never as a crash.
        uncapped_acknowledged: The same-invocation acknowledgment half of
            the honest escape (see :func:`uncapped_escape_applies`). The
            daemon belt never passes ``True``.

    Returns:
        ``None`` when the journey may open; otherwise the refusal.
    """
    if resolve_error is not None:
        return _cap_refusal(
            profile_name,
            "its budget profile could not be resolved, so no cap can be "
            "proven",
            str(resolve_error),
        )

    if uncapped_escape_applies(profile_name, uncapped_acknowledged):
        return None

    named = repr(profile_name) if profile_name is not None else "it resolved"
    cap = getattr(guards, "max_review_cycles", None)

    if cap is None:
        detail = (
            f"Budget profile {named} sets no review-cycle cap at all "
            "(max_review_cycles is unset), so the budget guard would be a "
            "strict no-op for the whole journey."
        )
        if uncapped_acknowledged:
            detail += (
                "\nThe uncapped acknowledgment was given, but it only opens "
                f"the profile named {UNCAPPED_ESCAPE_PROFILE_NAME!r} — the "
                "reserved 'attended' profile is never an escape hatch."
            )
        return _cap_refusal(profile_name, "its profile sets no cap", detail)

    # A ``bool`` is deliberately NOT special-cased: it *is* an int, and
    # both of its values sit below the floor, so it lands on the
    # below-the-floor refusal below rather than needing a clause of its
    # own. A clause that can never change an outcome is a future lie.
    if not isinstance(cap, int):
        return _cap_refusal(
            profile_name,
            "its profile's review-cycle cap is not a whole number",
            f"Budget profile {named} sets max_review_cycles={cap!r}, which "
            "is not an integer, so no cap can be enforced from it.",
        )

    if cap < MODE_C_MIN_REVIEW_CYCLES:
        return _cap_refusal(
            profile_name,
            f"its review-cycle cap of {cap} is below the floor of "
            f"{MODE_C_MIN_REVIEW_CYCLES}",
            f"Budget profile {named} sets max_review_cycles={cap}. The count "
            "includes the INITIAL review, so a bounded journey needs "
            f"{MODE_C_MIN_REVIEW_CYCLES}: the review that finds the fix "
            "tasks, and the one follow-up that confirms they landed. A cap "
            f"of {cap} breaches at that mandatory follow-up, so the journey "
            "could never finish.",
        )

    return None


def mode_c_cap_refusal_from_config(
    config: Any,
    profile_name: str | None,
    *,
    uncapped_acknowledged: bool = False,
) -> ModeCCapRefusal | None:
    """Resolve ``profile_name`` off ``config`` and apply :func:`mode_c_cap_refusal`.

    The convenience form for callers holding a config and a requested
    profile name (``forge queue``). It owns the resolution failures —
    an absent ``budget`` section, ``resolve()``'s ``KeyError`` for an
    unknown profile, or any other resolver fault — and turns each one into
    the SAME readable refusal rather than a traceback. The rule itself is
    not restated here; it is applied by delegating to
    :func:`mode_c_cap_refusal`.
    """
    budget = getattr(config, "budget", None)
    if budget is None:
        return mode_c_cap_refusal(
            profile_name=profile_name,
            guards=None,
            resolve_error=(
                "This configuration carries no readable 'budget' section, so "
                "no review-cycle cap can be read from it."
            ),
        )

    effective = profile_name
    if effective is None:
        default = getattr(budget, "default_profile", None)
        effective = default if isinstance(default, str) else None

    try:
        guards = budget.resolve(profile_name)
    except KeyError as exc:
        detail = exc.args[0] if exc.args else str(exc)
        return mode_c_cap_refusal(
            profile_name=effective,
            guards=None,
            resolve_error=str(detail),
        )
    except Exception as exc:  # noqa: BLE001 — a resolver fault is a refusal
        return mode_c_cap_refusal(
            profile_name=effective,
            guards=None,
            resolve_error=f"{type(exc).__name__}: {exc}",
        )

    return mode_c_cap_refusal(
        profile_name=effective,
        guards=guards,
        uncapped_acknowledged=uncapped_acknowledged,
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
