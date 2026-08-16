"""THE ROUTING LAW's close-side check — ``verifier:`` stamps at the merge card.

Card Q8/A.2 (``ai-transition/docs/bdd-replacement-options-card-2026-08-09.md``),
Rich's ruling of 2026-08-14. Guardkit's half landed first
(``guardkit/orchestrator/verifier_stamp.py`` @ ``80464e16``): every approved
scenario in a feature YAML carries a ``verifier:`` stamp from a closed list of
homes, and an unstamped scenario fails the plan load. That is the *plan-load*
half. This module is the *close-side* half the same lane deferred to forge:

    **a stamped verifier that did not run is ABSENT, and ABSENT is UNKNOWN,
    and UNKNOWN publishes no merge card.**

The merge-ready checkpoint (``forge.cli._serve_conductor.make_gates_green_reader``)
already answers GREEN/RED/UNKNOWN from the repo's declared ``toolchain.test``
command. This module adds the ``stamps_satisfied`` leg beside it: it reads the
feature's per-scenario stamps and asks, home by home, whether the home that
was PROMISED at planning time actually RAN GREEN for this branch. Where the
answer is "no evidence", the leg says so in plain language — naming the
scenario and the missing home — and the checkpoint reads UNKNOWN.

How each home is satisfied (the A.2 home table, close-side)
-----------------------------------------------------------
``toolchain``
    Satisfied by the existing toolchain leg being GREEN — the declared suite
    ran, exit 0, in the fix branch's worktree. Nothing new is run here.
``hurl``
    Satisfied when the NEWEST F4 results envelope under
    ``<worktree>/qa/gates/history/`` (the ``guardkit qa live-gate`` runner's
    own receipt, ``ResultsEnvelope`` in ``guardkit.qa.formats.gate_registry``)
    has ``verdict: pass``, names a gate ``hurl-twins`` with ``exit_code: 0``,
    and STARTED after the branch's last code commit. A stale envelope — one
    that ran before the last change — proves nothing about the code being
    merged and reads ABSENT.
``exam`` · ``probe:bus`` · ``probe:process`` · ``flutter`` · ``playwright``
    ABSENT unless that same newest, fresh, green envelope names the home as a
    ``gate_id`` (``exam`` / ``probe:bus`` or ``probe-bus`` / ...) with exit 0.
    These homes have no forge-side runner today; the rule keeps them from
    silently passing, which is the whole point of the closed list.
``operator``
    Satisfied BY DECLARATION — attended verification is a human act, not a
    receipt the machinery can read — but every operator-stamped scenario is
    LISTED BY NAME on the card's gate detail and in the log as *attended*, so
    a green card never hides which of its scenarios a human, not a machine,
    vouched for.

No stamps at all
    The leg reports ``NOT_ENFORCED`` and the checkpoint is exactly what it
    was the day before this module existed. Every feature in the estate that
    predates the routing law is unaffected (the same opt-in law guardkit's
    plan-load half ships).

Why the stamps are read from the CANONICAL repo and the envelope from the
WORKTREE: the stamps are the plan of record — what was promised at planning
time — and, like the ``toolchain:`` declaration the neighbouring leg reads,
they are read from the canonical checkout so the fix journey's own agent
(which edits the worktree) cannot un-stamp its way to a card. The envelope is
a RECEIPT of a run against the branch's code, and the runner writes it beside
the code it ran; the branch's code lives in the worktree. (An envelope in the
canonical checkout was produced from main, not from this branch, and must not
green it.)

Freshness is measured against the branch's last CODE commit — the newest
commit that touched anything outside ``qa/gates/history/`` and
``qa/gates/evidence/`` — so a receipt-only commit that lands the envelope
itself does not make the envelope it just landed look stale. Falls back to
the plain last commit when the branch has nothing else.

**No guardkit dependency.** Forge does not install guardkit; this module
reads the feature YAML with the stdlib + PyYAML and mirrors the closed
vocabulary as a constant. If guardkit's list ever changes, the mirror is a
one-line change and the loud UNREADABLE path (an unknown home) catches the
gap in the meantime rather than passing it.

The routing law's ENFORCEMENT (coordinator condition 5, 2026-08-16)
-------------------------------------------------------------------
:func:`resolve_routing_law` / :func:`resolve_routing_law_enforcement` read
the SAME opt-in flag guardkit's plan-load half reads — the feature YAML's own
``routing_law:`` wins, then the repo's ``.guardkit/config.yaml``, else off —
so forge's plan-commit hook (THE STAMP NORMALIZER) stops a run over an
undecidable stamp only where the law is actually flipped on. Forge only
READS the flag; nothing in forge writes ``routing_law`` anywhere.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

logger = logging.getLogger(__name__)

__all__ = [
    "VERIFIER_HOMES",
    "HOME_GATE_IDS",
    "HISTORY_RELATIVE_PATH",
    "FEATURES_RELATIVE_PATH",
    "CONFIG_RELATIVE_PATH",
    "ROUTING_LAW_KEY",
    "ROUTING_LAW_VALUES",
    "RECEIPT_ONLY_PATHS",
    "RoutingLawResolution",
    "read_repo_routing_law",
    "resolve_routing_law",
    "resolve_routing_law_enforcement",
    "ScenarioStamp",
    "StampsRead",
    "Envelope",
    "StampsStatus",
    "StampsVerdict",
    "read_scenario_stamps",
    "read_newest_envelope",
    "read_last_code_commit_time",
    "evaluate_stamps",
    "make_stamps_leg",
]


#: The closed list — a MIRROR of ``guardkit.orchestrator.verifier_stamp
#: .VERIFIER_HOMES`` (card A.2's home table). Kept as a copy on purpose:
#: forge does not depend on guardkit, and the mirror is checked against the
#: original by a test that runs whenever guardkit is importable.
VERIFIER_HOMES: tuple[str, ...] = (
    "toolchain",
    "hurl",
    "exam",
    "probe:bus",
    "probe:process",
    "flutter",
    "playwright",
    "operator",
)

#: Which envelope ``gate_id`` values count as "this home ran". ``hurl`` is
#: the api_test pilot's ``hurl-twins`` gate (``qa/gates/hurl_twin_gate.py``,
#: ``GATE_ID = "hurl-twins"``). The other envelope-backed homes have no
#: runner in the estate yet; they are named here so an envelope that DOES
#: name them can satisfy them, and nothing else can.
HOME_GATE_IDS: Mapping[str, tuple[str, ...]] = {
    "hurl": ("hurl-twins",),
    "exam": ("exam",),
    "probe:bus": ("probe:bus", "probe-bus"),
    "probe:process": ("probe:process", "probe-process"),
    "flutter": ("flutter",),
    "playwright": ("playwright",),
}

#: Where the ``guardkit qa live-gate`` runner writes its F4 envelopes,
#: relative to the tree it ran in (``live_gate/runner.py HISTORY_DIRNAME``).
HISTORY_RELATIVE_PATH: Path = Path("qa") / "gates" / "history"

#: Where guardkit keeps the feature YAML (``FeatureLoader.FEATURES_DIR``).
FEATURES_RELATIVE_PATH: Path = Path(".guardkit") / "features"

#: The per-repo routing-law flag lives in the same file as the ``toolchain:``
#: declaration — a MIRROR of ``guardkit.orchestrator.verifier_stamp
#: .CONFIG_RELATIVE_PATH`` / ``ROUTING_LAW_KEY`` / ``ROUTING_LAW_VALUES``
#: (checked against the original by the same guardkit-importable test as
#: ``VERIFIER_HOMES``).
CONFIG_RELATIVE_PATH: Path = Path(".guardkit") / "config.yaml"
ROUTING_LAW_KEY: str = "routing_law"
ROUTING_LAW_VALUES: tuple[str, ...] = ("enforced", "off")

#: Paths a commit may touch and still NOT count as a code change for the
#: freshness rule (module docstring, "Freshness").
RECEIPT_ONLY_PATHS: tuple[str, ...] = ("qa/gates/history", "qa/gates/evidence")

_GIT_TIMEOUT_SECONDS = 30


# ---------------------------------------------------------------------------
# The stamps (plan of record)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScenarioStamp:
    """One scenario's ``verifier:`` stamp, as read from the feature YAML."""

    title: str
    verifier: str
    test_ref: str | None = None


@dataclass(frozen=True, slots=True)
class StampsRead:
    """What the feature YAML said about its scenarios.

    ``present`` — the file existed and parsed. ``stamps`` — the per-scenario
    stamps in file order. ``error`` — a plain-language reason the stamps
    could not be trusted (unparseable file, malformed map, unknown home);
    when set, the close-side leg reads UNREADABLE → UNKNOWN, never green.
    """

    path: Path
    present: bool
    stamps: tuple[ScenarioStamp, ...] = ()
    routing_law: str | None = None
    error: str | None = None


def read_scenario_stamps(feature_yaml_path: "Path | str") -> StampsRead:
    """Read ``scenarios:`` from a feature YAML with the stdlib + PyYAML.

    Mirrors the shapes guardkit's ``Feature`` model accepts: a mapping of
    scenario title → either a bare home name (``"toolchain"``) or a mapping
    with a ``verifier:`` key (and optional ``test_ref`` / ``test_paths``).
    Never raises; every failure is a plain-language ``error``.
    """
    path = Path(feature_yaml_path)
    if not path.is_file():
        return StampsRead(path=path, present=False)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — an unreadable plan is said, not hidden
        return StampsRead(
            path=path,
            present=True,
            error=f"{path} could not be parsed as YAML ({type(exc).__name__}: {exc})",
        )
    if not isinstance(data, dict):
        return StampsRead(
            path=path,
            present=True,
            error=f"{path} is not a mapping at the top level",
        )

    routing_law_raw = data.get("routing_law")
    routing_law: str | None
    if routing_law_raw is None:
        routing_law = None
    elif routing_law_raw is False:  # YAML 1.1 `off` → False (guardkit's own trap)
        routing_law = "off"
    else:
        routing_law = str(routing_law_raw)

    raw_scenarios = data.get("scenarios")
    if raw_scenarios in (None, {}, []):
        return StampsRead(path=path, present=True, routing_law=routing_law)
    if not isinstance(raw_scenarios, dict):
        return StampsRead(
            path=path,
            present=True,
            routing_law=routing_law,
            error=(
                f"{path}: `scenarios:` must be a mapping of scenario title → "
                f"verifier stamp, got {type(raw_scenarios).__name__}"
            ),
        )

    stamps: list[ScenarioStamp] = []
    for title, raw in raw_scenarios.items():
        if not isinstance(title, str) or not title.strip():
            return StampsRead(
                path=path,
                present=True,
                routing_law=routing_law,
                error=f"{path}: `scenarios:` has a non-string or empty title {title!r}",
            )
        if isinstance(raw, str):
            raw = {"verifier": raw}
        if not isinstance(raw, dict) or not isinstance(raw.get("verifier"), str):
            return StampsRead(
                path=path,
                present=True,
                routing_law=routing_law,
                error=(
                    f"{path}: scenario {title!r} has no `verifier:` home "
                    f"(got {raw!r})"
                ),
            )
        verifier = raw["verifier"].strip()
        if verifier not in VERIFIER_HOMES:
            return StampsRead(
                path=path,
                present=True,
                routing_law=routing_law,
                error=(
                    f"{path}: scenario {title!r} is stamped `verifier: "
                    f"{verifier}`, which is not in the closed list "
                    f"({', '.join(VERIFIER_HOMES)}) — an unknown home cannot "
                    "be satisfied and is never assumed"
                ),
            )
        test_ref = raw.get("test_ref")
        stamps.append(
            ScenarioStamp(
                title=title,
                verifier=verifier,
                test_ref=test_ref if isinstance(test_ref, str) and test_ref else None,
            )
        )
    return StampsRead(
        path=path, present=True, stamps=tuple(stamps), routing_law=routing_law
    )


# ---------------------------------------------------------------------------
# The routing law's ENFORCEMENT (coordinator review condition 5, 2026-08-16)
# ---------------------------------------------------------------------------
#
# Guardkit's plan-load half is OPT-IN (``verifier_stamp.py``, "The opt-in
# flag"): the law bites only where a repo says ``routing_law: enforced`` in
# ``.guardkit/config.yaml``, and a feature YAML's own ``routing_law:`` WINS
# over the repo flag (the escape hatch). Forge's plan-commit hook (THE STAMP
# NORMALIZER, ``forge.planning.driver``) reads the SAME two places with the
# SAME precedence, so that a repo the law is not flipped on never has its
# plans killed at plan-commit over an undecidable stamp — that would be
# enforcement through the back door. Forge only READS the flag; nothing in
# forge ever writes ``routing_law`` anywhere (pinned by test).


@dataclass(frozen=True, slots=True)
class RoutingLawResolution:
    """Where the routing law's enforcement for one feature came from.

    ``enforcement`` — ``"enforced"`` or ``"off"``. ``source`` — ``"feature"``
    (the feature YAML's own flag), ``"repo"`` (``.guardkit/config.yaml``), or
    ``"default"`` (neither says anything → off). ``detail`` — the plain
    sentence a receipt carries. ``invalid`` — a PRESENT flag with a value
    outside the closed list (guardkit's plan load will reject it); forge
    reads that as ENFORCED, never as silently off, and says so.
    """

    enforcement: str
    source: str
    detail: str
    invalid: bool = False

    @property
    def enforced(self) -> bool:
        return self.enforcement == "enforced"


def _normalize_routing_law(raw: Any) -> "tuple[str | None, bool]":
    """``(value, invalid)`` — mirrors guardkit's ``normalize_routing_law_flag``
    without raising: ``None`` → absent; ``False`` (YAML 1.1 ``off``) →
    ``"off"``; ``"enforced"``/``"off"`` → themselves; anything else (``True``
    from ``on``/``yes``, a typo like ``enforce``) → ``("enforced", True)``
    because a law flag must never be silently mis-read as off."""
    if raw is None:
        return None, False
    if raw is False:
        return "off", False
    if isinstance(raw, str) and raw.strip() in ROUTING_LAW_VALUES:
        return raw.strip(), False
    return "enforced", True


def read_repo_routing_law(repo_root: "Path | str") -> "tuple[str | None, bool]":
    """``(value, invalid)`` from ``<repo_root>/.guardkit/config.yaml``'s
    top-level ``routing_law:``; ``(None, False)`` when the file, the key, or a
    readable mapping is absent (guardkit's ``load_repo_routing_law`` posture
    for file-level rot: warn, treat as unset)."""
    config_path = Path(repo_root) / CONFIG_RELATIVE_PATH
    if not config_path.is_file():
        return None, False
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — a broken config never crashes the hook
        logger.warning(
            "routing law: %s could not be read (%s: %s) — repo flag treated as unset",
            config_path,
            type(exc).__name__,
            exc,
        )
        return None, False
    if not isinstance(data, dict):
        return None, False
    return _normalize_routing_law(data.get(ROUTING_LAW_KEY))


def resolve_routing_law(worktree: "Path | str", feature_id: str) -> RoutingLawResolution:
    """Feature-level ``routing_law:`` wins → repo ``.guardkit/config.yaml`` →
    ``off``. Reads ``<worktree>/.guardkit/features/<feature_id>.yaml`` with
    :func:`read_scenario_stamps` (which already absorbs the YAML ``off`` →
    ``False`` trap) and the repo flag with :func:`read_repo_routing_law`.
    Never raises; a flag forge cannot read is a flag that is not set.
    """
    root = Path(worktree)
    feature_yaml = _feature_yaml_path(root, feature_id)
    feature_flag: str | None = None
    feature_invalid = False
    try:
        read = read_scenario_stamps(feature_yaml)
    except Exception as exc:  # noqa: BLE001 — a reader defect is "unset", said aloud
        logger.warning(
            "routing law: reading %s raised %s: %s — feature-level flag treated as unset",
            feature_yaml,
            type(exc).__name__,
            exc,
        )
        read = None
    if read is not None and read.present and read.routing_law is not None:
        feature_flag, feature_invalid = _normalize_routing_law(read.routing_law)
    if feature_flag is not None:
        if feature_invalid:
            logger.warning(
                "routing law: %s carries an invalid `%s:` value (%r) — read as "
                "ENFORCED, never as silently off (guardkit's plan load will "
                "reject it; the values are %s)",
                feature_yaml,
                ROUTING_LAW_KEY,
                read.routing_law if read is not None else None,
                " / ".join(ROUTING_LAW_VALUES),
            )
            return RoutingLawResolution(
                enforcement="enforced",
                source="feature",
                detail=(
                    f"routing law {ROUTING_LAW_KEY}: in the feature YAML is "
                    f"{read.routing_law!r}, not one of "
                    f"{' / '.join(ROUTING_LAW_VALUES)} — read as enforced, "
                    "never as silently off"
                ),
                invalid=True,
            )
        return RoutingLawResolution(
            enforcement=feature_flag,
            source="feature",
            detail=(
                f"routing law {feature_flag}: the feature YAML "
                f"({feature_yaml.name}) says so, which wins over the repo flag"
            ),
        )
    repo_flag, repo_invalid = read_repo_routing_law(root)
    if repo_flag is not None:
        if repo_invalid:
            logger.warning(
                "routing law: %s carries an invalid `%s:` value — read as "
                "ENFORCED, never as silently off (the values are %s)",
                root / CONFIG_RELATIVE_PATH,
                ROUTING_LAW_KEY,
                " / ".join(ROUTING_LAW_VALUES),
            )
            return RoutingLawResolution(
                enforcement="enforced",
                source="repo",
                detail=(
                    f"routing law {ROUTING_LAW_KEY}: in "
                    f"{CONFIG_RELATIVE_PATH.as_posix()} is not one of "
                    f"{' / '.join(ROUTING_LAW_VALUES)} — read as enforced, "
                    "never as silently off"
                ),
                invalid=True,
            )
        return RoutingLawResolution(
            enforcement=repo_flag,
            source="repo",
            detail=(
                f"routing law {repo_flag}: {CONFIG_RELATIVE_PATH.as_posix()} "
                "says so (no feature-level flag)"
            ),
        )
    return RoutingLawResolution(
        enforcement="off",
        source="default",
        detail=(
            f"routing law off: neither the feature YAML nor "
            f"{CONFIG_RELATIVE_PATH.as_posix()} carries a {ROUTING_LAW_KEY}: "
            "flag (this repo does not enforce the routing law yet)"
        ),
    )


def resolve_routing_law_enforcement(worktree: "Path | str", feature_id: str) -> str:
    """``"enforced"`` | ``"off"`` — the string form of :func:`resolve_routing_law`."""
    return resolve_routing_law(worktree, feature_id).enforcement


# ---------------------------------------------------------------------------
# The envelope (receipt of a run)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Envelope:
    """The slice of an F4 results envelope the close-side check reads."""

    path: Path
    run_id: str
    verdict: str
    started: datetime | None
    gates: Mapping[str, int | None]
    feature_id: str | None = None

    def gate_exit(self, gate_ids: Sequence[str]) -> "tuple[str, int | None] | None":
        """``(gate_id, exit_code)`` for the first of ``gate_ids`` present."""
        for gate_id in gate_ids:
            if gate_id in self.gates:
                return gate_id, self.gates[gate_id]
        return None


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _load_envelope(path: Path) -> Envelope | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — a broken receipt is not a green one
        logger.warning(
            "routing law: envelope %s could not be read (%s: %s) — ignored",
            path,
            type(exc).__name__,
            exc,
        )
        return None
    if not isinstance(data, dict):
        return None
    started = _parse_iso(data.get("started"))
    if started is None:
        # The runner always writes `started`; a hand-written or damaged file
        # falls back to the file clock, which is still a real time.
        try:
            started = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            started = None
    gates: dict[str, int | None] = {}
    for gate in data.get("gates") or ():
        if isinstance(gate, dict) and isinstance(gate.get("gate_id"), str):
            code = gate.get("exit_code")
            gates[gate["gate_id"]] = code if isinstance(code, int) else None
    return Envelope(
        path=path,
        run_id=str(data.get("run_id") or path.stem),
        verdict=str(data.get("verdict") or "").strip().lower(),
        started=started,
        gates=gates,
        feature_id=(
            data.get("feature_id") if isinstance(data.get("feature_id"), str) else None
        ),
    )


def read_newest_envelope(history_dir: "Path | str") -> "Envelope | None":
    """The newest F4 envelope under ``history_dir`` (by ``started``), or None.

    Missing directory → ``None``. Envelopes that will not parse are ignored
    with a warning; a directory of only broken files also answers ``None``.
    """
    root = Path(history_dir)
    if not root.is_dir():
        return None
    envelopes = [
        env
        for env in (_load_envelope(p) for p in sorted(root.glob("*.json")))
        if env is not None
    ]
    if not envelopes:
        return None
    return max(
        envelopes,
        key=lambda e: (e.started or datetime.min.replace(tzinfo=timezone.utc)),
    )


def read_last_code_commit_time(
    worktree: "Path | str",
    branch: str | None = None,
    *,
    receipt_only_paths: Sequence[str] = RECEIPT_ONLY_PATHS,
) -> datetime | None:
    """Committer time of the branch's last CODE commit (module docstring).

    ``git log -1 --format=%ct <ref> -- . ':(exclude)qa/gates/history' ...``
    in the worktree; falls back to the plain last commit when every commit
    on the branch is receipt-only. The ref is ``branch`` when given (the
    build row's fix branch), then ``HEAD`` — the worktree's checkout, which
    is the code the declared suite just ran against — if the branch name
    does not resolve there. ``None`` on any git failure, which the caller
    reads as "freshness cannot be proven", i.e. ABSENT.
    """
    refs = [branch, "HEAD"] if branch and branch != "HEAD" else ["HEAD"]
    excludes = [f":(exclude){p}" for p in receipt_only_paths]
    for ref in refs:
        base = ["git", "log", "-1", "--format=%ct", ref, "--", "."]
        ref_failed = False
        for argv in (base + excludes, base):
            try:
                completed = subprocess.run(  # noqa: S603 — fixed argv, no shell
                    argv,
                    cwd=str(worktree),
                    capture_output=True,
                    text=True,
                    timeout=_GIT_TIMEOUT_SECONDS,
                    check=False,
                )
            except Exception as exc:  # noqa: BLE001 — could not read is not fresh
                logger.warning(
                    "routing law: git log in %s raised %s: %s — the branch's "
                    "last commit time cannot be read",
                    worktree,
                    type(exc).__name__,
                    exc,
                )
                return None
            if completed.returncode != 0:
                logger.warning(
                    "routing law: git log %s exited %s in %s: %s",
                    ref,
                    completed.returncode,
                    worktree,
                    (completed.stderr or "").strip().splitlines()[-1:]
                    or "(no stderr)",
                )
                ref_failed = True
                break
            text = (completed.stdout or "").strip().splitlines()
            if text and text[-1].strip().isdigit():
                return datetime.fromtimestamp(int(text[-1].strip()), tz=timezone.utc)
        if not ref_failed:
            # The ref resolved but has no commits at all — nothing to be fresh
            # against; do not try HEAD, it would be the same empty history.
            return None
    return None


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------


class StampsStatus(StrEnum):
    """What the ``stamps_satisfied`` leg concluded.

    NOT_ENFORCED — no stamps on the feature; the leg has no effect.
    SATISFIED    — every stamped scenario's home is proven (or, for
                   ``operator``, declared and listed).
    ABSENT       — at least one stamped verifier did not run green for this
                   branch. The checkpoint reads UNKNOWN: no card.
    UNREADABLE   — the stamps themselves could not be trusted (unparseable
                   feature YAML, unknown home). Also UNKNOWN: no card.
    """

    NOT_ENFORCED = "not-enforced"
    SATISFIED = "satisfied"
    ABSENT = "absent"
    UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class StampsVerdict:
    """The leg's answer, with the plain-language lines a human reads.

    ``lines`` — one sentence per finding, in the order a reader wants them:
    every missing home first (scenario named, home named, why), then the
    attended list, then the satisfied summary. ``missing`` — the
    ``(scenario, home)`` pairs that are ABSENT, for ``failed_gates``.
    ``attended`` — the operator-stamped scenario titles (LISTED, never
    silently passed).
    """

    status: StampsStatus
    lines: tuple[str, ...] = ()
    missing: tuple[tuple[str, str], ...] = ()
    attended: tuple[str, ...] = ()
    satisfied_by_home: Mapping[str, int] = field(default_factory=dict)

    @property
    def blocks_card(self) -> bool:
        return self.status in (StampsStatus.ABSENT, StampsStatus.UNREADABLE)

    @property
    def detail(self) -> str:
        return " ".join(self.lines)


def _fmt(ts: datetime | None) -> str:
    return ts.isoformat(timespec="seconds") if ts is not None else "unknown"


def _envelope_status_for_home(
    home: str,
    *,
    envelope: Envelope | None,
    code_commit_time: datetime | None,
    history_dir: Path,
) -> "str | None":
    """``None`` when the home is proven by the envelope; else the reason not."""
    gate_ids = HOME_GATE_IDS[home]
    wanted = " or ".join(f"`{g}`" for g in gate_ids)
    if envelope is None:
        return (
            f"no results envelope exists under {history_dir} (the "
            f"`guardkit qa live-gate` run that would carry a {wanted} gate "
            "never wrote a receipt here)"
        )
    where = f"newest envelope {envelope.run_id}"
    if envelope.verdict != "pass":
        return (
            f"the {where} has verdict `{envelope.verdict or 'missing'}`, not "
            "`pass`"
        )
    found = envelope.gate_exit(gate_ids)
    if found is None:
        return (
            f"the {where} is green but names no {wanted} gate (gates present: "
            f"{', '.join(sorted(envelope.gates)) or 'none'})"
        )
    gate_id, exit_code = found
    if exit_code != 0:
        return f"the {where} names `{gate_id}` but its exit code is {exit_code!r}, not 0"
    if code_commit_time is None:
        return (
            f"the {where} names `{gate_id}` exit 0, but the branch's last "
            "commit time could not be read from git, so its freshness cannot "
            "be proven"
        )
    if envelope.started is None:
        return (
            f"the {where} names `{gate_id}` exit 0, but carries no readable "
            "`started` time, so its freshness cannot be proven"
        )
    if envelope.started < code_commit_time:
        return (
            f"the {where} names `{gate_id}` exit 0 but is STALE — it started "
            f"{_fmt(envelope.started)}, before the branch's last code commit "
            f"at {_fmt(code_commit_time)}; the verified code is not the code "
            "being merged"
        )
    return None


def evaluate_stamps(
    stamps_read: StampsRead,
    *,
    toolchain_green: bool,
    envelope: Envelope | None,
    code_commit_time: datetime | None,
    history_dir: "Path | str",
    feature_id: str = "",
) -> StampsVerdict:
    """The pure decision: stamps + evidence → :class:`StampsVerdict`.

    No I/O. Every input is something a caller (or a test) already read.
    """
    history_dir = Path(history_dir)
    feature = feature_id or stamps_read.path.stem
    if stamps_read.error:
        return StampsVerdict(
            status=StampsStatus.UNREADABLE,
            lines=(
                f"the routing law (card A.2) blocks the merge card: feature "
                f"{feature}'s scenario stamps could not be read — "
                f"{stamps_read.error}. A plan whose promised verifiers cannot "
                "be read cannot prove they ran, so no merge card is published.",
            ),
        )
    if not stamps_read.stamps:
        return StampsVerdict(
            status=StampsStatus.NOT_ENFORCED,
            lines=(
                (
                    f"routing law: feature {feature} carries no scenario stamps "
                    "(no `scenarios:` map in its feature YAML) — the "
                    "stamped-verifier check is not enforced for this build."
                    if stamps_read.present
                    else f"routing law: no feature YAML at {stamps_read.path} — "
                    "the stamped-verifier check is not enforced for this build."
                ),
            ),
        )

    missing: list[tuple[str, str]] = []
    reasons: list[str] = []
    attended: list[str] = []
    satisfied: dict[str, int] = {}
    envelope_reason_cache: dict[str, str | None] = {}

    for stamp in stamps_read.stamps:
        home = stamp.verifier
        if home == "toolchain":
            if toolchain_green:
                satisfied[home] = satisfied.get(home, 0) + 1
            else:
                missing.append((stamp.title, home))
                reasons.append(
                    f"scenario {stamp.title!r} is stamped `verifier: toolchain` "
                    "but the declared toolchain suite is not green"
                )
        elif home == "operator":
            attended.append(stamp.title)
            satisfied[home] = satisfied.get(home, 0) + 1
        elif home in HOME_GATE_IDS:
            if home not in envelope_reason_cache:
                envelope_reason_cache[home] = _envelope_status_for_home(
                    home,
                    envelope=envelope,
                    code_commit_time=code_commit_time,
                    history_dir=history_dir,
                )
            reason = envelope_reason_cache[home]
            if reason is None:
                satisfied[home] = satisfied.get(home, 0) + 1
            else:
                missing.append((stamp.title, home))
                reasons.append(
                    f"scenario {stamp.title!r} is stamped `verifier: {home}` "
                    f"but {reason}"
                )
        else:  # pragma: no cover — read_scenario_stamps refuses unknown homes
            missing.append((stamp.title, home))
            reasons.append(
                f"scenario {stamp.title!r} is stamped `verifier: {home}`, "
                "which this check has no way to prove"
            )

    lines: list[str] = []
    if missing:
        lines.append(
            f"the routing law (card A.2) blocks the merge card for feature "
            f"{feature}: "
            + "; ".join(reasons)
            + ". A stamped verifier that did not run green for this branch is "
            "ABSENT, ABSENT is UNKNOWN, and UNKNOWN publishes no merge card."
        )
    if not missing:
        summary = " · ".join(f"{home}: {count}" for home, count in satisfied.items())
        proof = ""
        if envelope is not None and any(h in HOME_GATE_IDS for h in satisfied):
            proof = (
                f" (envelope {envelope.run_id}, started {_fmt(envelope.started)}, "
                f"after the branch's last code commit at {_fmt(code_commit_time)})"
            )
        lines.append(
            f"routing law: all {len(stamps_read.stamps)} stamped scenario(s) of "
            f"feature {feature} satisfied — {summary}{proof}."
        )
    if attended:
        # LISTED on both the green card and the blocked close: a human, not
        # the machinery, vouched for these, and the reader is told which.
        lines.append(
            "ATTENDED (operator-stamped, verified by a human, not by the "
            "machinery — listed by name, never silently passed): "
            + "; ".join(repr(t) for t in attended)
            + "."
        )
    return StampsVerdict(
        status=StampsStatus.ABSENT if missing else StampsStatus.SATISFIED,
        lines=tuple(lines),
        missing=tuple(missing),
        attended=tuple(attended),
        satisfied_by_home=dict(satisfied),
    )


# ---------------------------------------------------------------------------
# The production leg (I/O composed, every reader injectable)
# ---------------------------------------------------------------------------


def make_stamps_leg(
    *,
    stamps_reader: Callable[[Path], StampsRead] | None = None,
    envelope_reader: Callable[[Path], Envelope | None] | None = None,
    commit_time_reader: Callable[..., datetime | None] | None = None,
) -> Callable[..., StampsVerdict]:
    """Build the ``stamps_satisfied`` leg the merge-ready checkpoint calls.

    Returns ``(*, feature_id, repo_root, worktree, branch, toolchain_green)
    -> StampsVerdict``. Reads the stamps from
    ``<repo_root>/.guardkit/features/<feature_id>.yaml`` (canonical — the
    plan of record), the newest envelope from
    ``<worktree>/qa/gates/history/`` (the branch's own receipts), and the
    branch's last code commit time from git in the worktree. Never raises:
    a reader that raises answers UNREADABLE, which is UNKNOWN, which is no
    card.
    """
    _stamps = stamps_reader or read_scenario_stamps
    _envelope = envelope_reader or read_newest_envelope
    _commit = commit_time_reader or read_last_code_commit_time

    def stamps_leg(
        *,
        feature_id: str,
        repo_root: "Path | str",
        worktree: "Path | str",
        branch: str | None,
        toolchain_green: bool,
    ) -> StampsVerdict:
        history_dir = Path(worktree) / HISTORY_RELATIVE_PATH
        feature_yaml = _feature_yaml_path(Path(repo_root), feature_id)
        try:
            stamps_read = _stamps(feature_yaml)
        except Exception as exc:  # noqa: BLE001 — a reader defect is not green
            stamps_read = StampsRead(
                path=feature_yaml,
                present=True,
                error=f"reading the stamps raised {type(exc).__name__}: {exc}",
            )
        envelope: Envelope | None = None
        commit_time: datetime | None = None
        if any(s.verifier in HOME_GATE_IDS for s in stamps_read.stamps):
            try:
                envelope = _envelope(history_dir)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "routing law: envelope reader raised %s: %s for %s — "
                    "treated as no envelope",
                    type(exc).__name__,
                    exc,
                    history_dir,
                )
                envelope = None
            try:
                commit_time = _commit(worktree, branch)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "routing law: commit-time reader raised %s: %s for %s — "
                    "freshness cannot be proven",
                    type(exc).__name__,
                    exc,
                    worktree,
                )
                commit_time = None
        return evaluate_stamps(
            stamps_read,
            toolchain_green=toolchain_green,
            envelope=envelope,
            code_commit_time=commit_time,
            history_dir=history_dir,
            feature_id=feature_id,
        )

    return stamps_leg


def _feature_yaml_path(repo_root: Path, feature_id: str) -> Path:
    """``<repo_root>/.guardkit/features/<id>.yaml``, or ``.yml`` if that exists.

    The same two-suffix rule as guardkit's ``FeatureLoader.load_feature``.
    """
    base = repo_root / FEATURES_RELATIVE_PATH
    yaml_path = base / f"{feature_id}.yaml"
    if yaml_path.exists():
        return yaml_path
    yml_path = base / f"{feature_id}.yml"
    if yml_path.exists():
        return yml_path
    return yaml_path
