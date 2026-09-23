"""The build system's own checks on a join this press did not make.

THE PROBLEM, in plain words. Two different things check a joined result: the
build system's own checks, which run INSIDE the build system's merge command,
and the factory's own live check on the joined tree. A press that picks up a
join an earlier press already made does not run the merge command again — that
would try to merge something that is already merged — so the first kind of
check has never run on that joined result. One kind of check is not "checked",
so such a build is never published and stops, GATED, saying exactly that.

WHAT WOULD CLOSE IT is a command of the build system's own that checks an
already-joined commit WITHOUT merging it. That is a change to the build
system's own command line, with its own tests, and not a change to this
factory — and the factory runs that build system from a frozen container
image, so a command written today would not exist for the factory until that
image is rebuilt. So this module does not implement it. What it does is ASK:

    <the build system's own command> check-join <FEATURE> --joined <COMMIT>

If the installed build system has that sub-command, its answer is used and the
GATED case closes by itself. If it does not — which is the case today, and the
case inside the frozen image — nothing is invented: the build stays GATED and
the sentence names the sub-command that would close it, so that whoever adds
it knows precisely what to add and nothing here has to change again.

THE CONTRACT this expects of that sub-command is written down beside this
stage's evidence (``stage-4c-executor/evidence/NOTES.md``), because it is a
promise between two repositories and not an implementation detail.

NOTHING HERE NAMES A LANGUAGE, A TEST RUNNER OR A PACKAGE MANAGER. What the
checks ARE belongs to the project and its build system; this module carries a
question and reads an answer.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

__all__ = [
    "CHECK_JOIN_SUBCOMMAND",
    "CHECK_JOIN_VERB",
    "CheckJoinAnswer",
    "ask_the_build_system_to_check_the_join",
    "the_gated_sentence",
    "the_sub_command_is_not_there",
]

#: The command the question is put to — the same one the merge word's own join
#: is put to, so a project that has one has both.
CHECK_JOIN_SUBCOMMAND: str = "autobuild"

#: The verb this factory asks for. It does not exist on the installed build
#: system today, and the refusal says so by name.
CHECK_JOIN_VERB: str = "check-join"

#: How a command line says "I have never heard of that verb". No exit code is
#: reserved for it across command lines, so both are read: the conventional
#: exit code for a usage error, and the words command lines print.
_USAGE_EXIT_CODES: tuple[int, ...] = (2, 64, 127)
_NOT_THERE: tuple[str, ...] = (
    "no such command",
    "unknown command",
    "unrecognized command",
    "unrecognised command",
    "invalid choice",
    "unknown argument",
    "unrecognized arguments",
    "command not found",
    "is not a",
)


@dataclass(frozen=True)
class CheckJoinAnswer:
    """What came back, and what it means for publication.

    ``ran`` — the build system's own checks ran on this joined commit.
    ``passed`` — and they were green. ``passed`` is meaningless unless ``ran``.
    ``sentence`` — plain words for the person reading the result.
    """

    ran: bool
    passed: bool
    sentence: str
    #: True when the installed build system does not have the sub-command at
    #: all, which is the GATED case this stage leaves for rollout.
    not_installed: bool = False
    checks_passed: int | None = None
    checks_total: int | None = None
    verify_status: str | None = None
    report: dict[str, Any] | None = None


def the_sub_command_is_not_there(
    *, exit_code: int | None, said: str
) -> bool:
    """Does this answer mean "I have never heard of that verb"?"""
    spoken = (said or "").lower()
    if any(phrase in spoken for phrase in _NOT_THERE) and CHECK_JOIN_VERB in spoken:
        return True
    if any(phrase in spoken for phrase in _NOT_THERE) and "usage" in spoken:
        return True
    return exit_code in _USAGE_EXIT_CODES and CHECK_JOIN_VERB in spoken


def _report_in(said: str) -> dict[str, Any] | None:
    """The one-line JSON report the sub-command prints, if it printed one."""
    start = said.find("{")
    while start >= 0:
        end = said.rfind("}")
        if end > start:
            try:
                decoded = json.loads(said[start : end + 1])
            except ValueError:
                decoded = None
            if isinstance(decoded, dict):
                return decoded
        start = said.find("{", start + 1)
    return None


def _gated(why: str) -> CheckJoinAnswer:
    return CheckJoinAnswer(
        ran=False,
        passed=False,
        not_installed=True,
        sentence=why,
    )


#: The sentence a GATED build carries when the sub-command is not there. It
#: names the sub-command exactly, because that name is the whole of what has
#: to be built for this ending to stop happening.
def the_gated_sentence(feature_id: str, j_commit: str, why: str = "") -> str:
    return (
        "the build system's own checks after a join have not run on "
        f"{j_commit[:10]}, because this press picked up a join an earlier one "
        "had already made and does not run the merge command again. The one "
        "thing that would run them on an already-joined commit is a "
        f"sub-command of the build system's own — `{CHECK_JOIN_SUBCOMMAND} "
        f"{CHECK_JOIN_VERB} {feature_id} --joined {j_commit[:10]}` — and the "
        "installed build system does not have it"
        + (f" ({why})" if why else "")
    )


async def ask_the_build_system_to_check_the_join(
    run: Callable[..., Awaitable[Any]],
    *,
    repo_root: Path,
    feature_id: str,
    j_commit: str,
    timeout_seconds: float,
) -> CheckJoinAnswer:
    """Ask for the checks on an already-joined commit. Never raises.

    ``ran`` false with ``not_installed`` true is the ordinary answer today:
    the sub-command does not exist, the build stays GATED, and the sentence
    names what would close it.
    """
    args = [CHECK_JOIN_VERB, feature_id, "--joined", j_commit, "--json"]
    try:
        result = await run(
            subcommand=CHECK_JOIN_SUBCOMMAND,
            args=args,
            repo_path=repo_root,
            read_allowlist=[repo_root],
            timeout_seconds=timeout_seconds,
            with_nats_streaming=False,
        )
    except Exception as exc:  # noqa: BLE001 — a GATED ending, never a crash
        # The door the factory runs the build system through carries exactly
        # the commands it was built to carry and refuses anything else, and
        # that refusal arrives here as an exception. It is the same fact as
        # "the installed build system does not have it": this factory cannot
        # get that question answered where the build happens.
        logger.info(
            "merge-executor: the build system could not be asked to check the "
            "join for %s (%s: %s) — the build stays gated and says so",
            feature_id,
            type(exc).__name__,
            exc,
        )
        return _gated(
            the_gated_sentence(
                feature_id, j_commit, f"{type(exc).__name__}: {exc}"
            )
        )

    exit_code = getattr(result, "exit_code", None)
    said = "\n".join(
        part
        for part in (
            getattr(result, "stdout_tail", "") or "",
            getattr(result, "stderr", None) or "",
        )
        if part
    )
    if the_sub_command_is_not_there(exit_code=exit_code, said=said):
        return _gated(the_gated_sentence(feature_id, j_commit))

    report = _report_in(said) or {}
    status = getattr(result, "status", "failed")
    ran = bool(report.get("verify_ran")) or status == "success"
    if report.get("verify_ran") is False:
        ran = False
    if not ran:
        # It exists, and it could not run the checks. That is "unverified",
        # not a pass and not a red set of checks.
        return CheckJoinAnswer(
            ran=False,
            passed=False,
            sentence=(
                f"the build system's own checks on the joined result "
                f"{j_commit[:10]} could not run: "
                + str(
                    report.get("verify_detail")
                    or report.get("verify_status")
                    or f"the check-join command answered {status}"
                )
            ),
            verify_status=str(report.get("verify_status") or "unverified"),
            report=report or None,
        )
    passed = (
        report.get("verify_ok") is True
        if "verify_ok" in report
        else (status == "success" and exit_code == 0)
    )
    counted = report.get("checks_passed")
    total = report.get("checks_total")
    return CheckJoinAnswer(
        ran=True,
        passed=bool(passed),
        sentence=(
            f"the build system's own checks ran on the joined result "
            f"{j_commit[:10]} and "
            + (
                "passed"
                if passed
                else "did not pass: "
                + str(
                    report.get("verify_detail")
                    or report.get("verify_status")
                    or "they went red"
                )
            )
        ),
        checks_passed=counted if isinstance(counted, int) else None,
        checks_total=total if isinstance(total, int) else None,
        verify_status=str(report.get("verify_status") or ("passed" if passed else "failed")),
        report=report or None,
    )
