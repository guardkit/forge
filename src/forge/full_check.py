"""The project's full check, run the way the merge runs it (1 November 2026).

The merge checks the joined tree with the project's FULL check: the root
``test`` plus every declared component and every folder a ``parts:`` pattern
matches, each in its own folder, with its own install first and its own time
limit. The merge card's step used to run only the root command, so a card
could be shown for work the join then failed. Both now run the same thing.

WHAT THE PARTS ARE is GuardKit's answer, never Forge's: GuardKit's own
``toolchain_declaration.full_check_parts`` is asked, from the module the
build system installs. Where it is not importable (an older GuardKit, or a
test run without GuardKit's own dependencies), the answer is "could not be
read", and the caller stops; nothing here guesses a command.

HOW EACH PART IS RUN is the caller's runner, so the coordinator and the
sandbox's helper each run a project's commands the way they already do (an
environment built from nothing, the project's declared names). The verdict
is the same as the merge's: any part that failed makes the whole check
failed (a test that runs out of its time is a failed part, as at the join);
otherwise any part that could not run (nothing declared for it, an install
that failed, a command that would not start) makes it "could not run"; only
every part passing is a pass.

WHETHER A PART'S RUN COUNTS is GuardKit's answer too (31 October 2026, from
Codex's second check of the after-task check redesign). A project may
declare, for any part, what "no tests ran" looks like in its output
(``absent_substrings``, ``ran_marker_regex``, ``requires_ran_marker``).
The join applies those with GuardKit's own ``declared_no_tests_ran`` and
``ran_marker_problem``; this check used to drop them and call every exit 0 a
pass, so a card could be offered for a part that ran no test and the join
then refused. Each part now carries those fields, and its verdict is read
with the same two GuardKit functions, taken from the same module as
``full_check_parts``: a part whose declaration says nothing ran FAILED, in
GuardKit's words, and goes back for repair before the card; a declaration
that requires a marker and names none could not run. Where those functions
are missing, the check could not be read, as for the reader.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Any, Callable, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "TimedOut",
    "RanOutput",
    "VerdictRules",
    "CheckPart",
    "FullCheck",
    "PASSED",
    "FAILED",
    "COULD_NOT_RUN",
    "full_check_parts_of",
    "run_full_check",
]

PASSED = "passed"
FAILED = "failed"
COULD_NOT_RUN = "could not run"

#: Where GuardKit keeps its declaration reader (the same module Forge's
#: declaration loaders ask).
TOOLCHAIN_MODULE = "guardkit.orchestrator.toolchain_declaration"


class TimedOut(str):
    """A runner's sentence for a command stopped at its time limit.

    It IS the sentence (an ordinary string); what it adds is that it says so,
    because a runner answers a timeout with no exit code, exactly as it
    answers a command that would not start. A part's test that runs out of
    its time is a FAILED part, as GuardKit's join counts it (1 November
    2026), and goes back for repair; one that would not start could not run.
    """

    timed_out = True


class RanOutput(str):
    """A runner's sentence with the run's whole output kept on it.

    It IS the sentence; ``output`` is what the command printed, standard
    output then a line break then standard error, as GuardKit's join reads
    it, so GuardKit's ``declared_no_tests_ran`` sees the same text here
    (31 October 2026). A runner whose answer has no ``output`` kept none.
    """

    output: str

    def __new__(cls, sentence: str, *, output: str) -> "RanOutput":
        said = super().__new__(cls, sentence)
        said.output = output
        return said


@dataclass(frozen=True)
class VerdictRules:
    """GuardKit's own rules for reading one part's run (31 October 2026).

    ``no_tests_ran`` is ``toolchain_declaration.declared_no_tests_ran``
    ``(commands, returncode, output) -> why | None`` and ``marker_problem``
    is ``toolchain_declaration.ran_marker_problem`` ``(commands) -> why |
    None``, taken from the module that listed the parts, so the merge card
    and the join can never read a run differently.
    """

    no_tests_ran: Callable[[Any, int | None, str | None], str | None]
    marker_problem: Callable[[Any], str | None]


@dataclass(frozen=True)
class CheckPart:
    """One part of the full check: where it runs and what it runs.

    The three "no tests ran" fields are the part's own declaration (the top
    level for the root, the component's or ``parts:`` entry's otherwise), in
    the shape GuardKit's rules read; ``rules`` are those rules. A part with
    no ``rules`` is never run: its result could not be read.
    """

    label: str
    cwd: str
    test: str | None
    install: str | None = None
    install_timeout: int = 300
    test_timeout: int = 300
    name: str = ""
    absent_substrings: tuple[str, ...] | None = None
    ran_marker_regex: str | None = None
    requires_ran_marker: bool | None = None
    rules: VerdictRules | None = field(default=None, compare=False, repr=False)

    @property
    def declares_what_no_tests_looks_like(self) -> bool:
        return (
            self.absent_substrings is not None
            or self.ran_marker_regex is not None
            or self.requires_ran_marker is not None
        )


@dataclass(frozen=True)
class FullCheck:
    """What the full check said, part by part and as a whole."""

    status: str
    detail: str
    failed: tuple[str, ...] = ()
    could_not_run: tuple[str, ...] = ()
    failing_cases: tuple[str, ...] = ()
    evidence: str = ""
    parts: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    #: The runner's own answer when there was exactly one part, kept whole
    #: (it may carry the command that ran and its exit code).
    only_detail: Any = None

    @property
    def exit_code(self) -> int | None:
        """``0`` passed, ``1`` failed, ``None`` could not run."""
        if self.status == PASSED:
            return 0
        if self.status == FAILED:
            return 1
        return None


def _as_check_part(
    part: Any, declaration: Any, rules: VerdictRules | None = None
) -> CheckPart:
    name = str(getattr(part, "name", "") or "")
    cwd = str(getattr(part, "cwd", ".") or ".")
    toolchain = getattr(part, "toolchain", None)
    limits = toolchain if toolchain is not None else declaration
    absent = getattr(limits, "absent_substrings", None)
    requires = getattr(limits, "requires_ran_marker", None)
    return CheckPart(
        label=name if cwd in (".", name) else f"{name} (in {cwd})",
        cwd=cwd,
        test=getattr(part, "test", None) or None,
        # The root's install is the build's own bootstrap, as in GuardKit's
        # full check; only a component's or a pattern part's install runs here.
        install=(getattr(toolchain, "install", None) or None) if toolchain else None,
        install_timeout=int(getattr(limits, "install_timeout", 300) or 300),
        test_timeout=int(getattr(limits, "test_timeout", 300) or 300),
        name=name,
        absent_substrings=None if absent is None else tuple(str(a) for a in absent),
        ran_marker_regex=getattr(limits, "ran_marker_regex", None),
        requires_ran_marker=None if requires is None else bool(requires),
        rules=rules,
    )


#: The names of GuardKit's own verdict rules in its toolchain module.
VERDICT_RULE_NAMES = ("declared_no_tests_ran", "ran_marker_problem")


def full_check_parts_of(
    declaration: Any,
    tree: Path | str,
    *,
    module_candidates: Sequence[str] = (TOOLCHAIN_MODULE,),
) -> list[CheckPart] | None:
    """GuardKit's own list of what the full check runs, found in ``tree``.

    Each part carries GuardKit's own verdict rules (:class:`VerdictRules`)
    from the same module. ``None`` when GuardKit's reader is not importable
    here, or it has no ``full_check_parts``, ``declared_no_tests_ran`` or
    ``ran_marker_problem`` (an older GuardKit), or when it raised; the
    caller says the check could not be read. Never raises.
    """
    if declaration is None:
        return []
    for candidate in module_candidates:
        try:
            import_module(candidate)
        except (ImportError, ModuleNotFoundError, ValueError):
            continue
        module = import_module(candidate)
        reader = getattr(module, "full_check_parts", None)
        no_tests_ran = getattr(module, VERDICT_RULE_NAMES[0], None)
        marker_problem = getattr(module, VERDICT_RULE_NAMES[1], None)
        if reader is None or no_tests_ran is None or marker_problem is None:
            return None
        rules = VerdictRules(no_tests_ran=no_tests_ran, marker_problem=marker_problem)
        try:
            return [
                _as_check_part(p, declaration, rules)
                for p in reader(declaration, Path(tree))
            ]
        except Exception as exc:  # noqa: BLE001 — a reader defect is not a pass
            logger.error(
                "full check: GuardKit's full_check_parts raised %s: %s in %s",
                type(exc).__name__,
                exc,
                tree,
            )
            return None
    return None


#: ``(*, command, cwd, timeout_seconds) -> (exit_code | None, detail)``: the
#: exit code is the verdict, and ``None`` means the command could not be run
#: or did not finish.
Runner = Callable[..., tuple[int | None, Any]]


def _inside(tree: Path, cwd: str) -> Path | None:
    base = Path(tree)
    folder = base / cwd
    try:
        folder.resolve().relative_to(base.resolve())
    except (OSError, ValueError):
        return None
    return folder


def run_full_check(
    parts: Sequence[CheckPart], *, tree: Path | str, run: Runner
) -> FullCheck:
    """Run every part in its own folder, its install first; never raises."""
    if not parts:
        return FullCheck(
            status=COULD_NOT_RUN,
            detail="the project declares no test command anywhere, so nothing was checked",
            could_not_run=("nothing declared",),
        )
    results: list[dict[str, Any]] = []
    for part in parts:
        status, detail, evidence, cases = _run_part(part, tree=Path(tree), run=run)
        results.append(
            {
                "label": part.label,
                "status": status,
                "detail": detail,
                "evidence": evidence,
                "failing_cases": cases,
            }
        )
    statuses = [r["status"] for r in results]
    status = (
        FAILED if FAILED in statuses else COULD_NOT_RUN if COULD_NOT_RUN in statuses else PASSED
    )
    shown = [r for r in results if r["status"] == status]
    detail = (
        str(shown[0]["detail"])
        if len(results) == 1
        else "; ".join(f"{r['label']}: {r['detail']}" for r in shown)
    )
    evidence = "\n".join(
        f"=== {r['label']}: {r['status']}\n{r['evidence']}".rstrip()
        for r in results
        if r["evidence"] or r["status"] != PASSED
    )
    return FullCheck(
        status=status,
        detail=detail,
        failed=tuple(r["label"] for r in results if r["status"] == FAILED),
        could_not_run=tuple(r["label"] for r in results if r["status"] == COULD_NOT_RUN),
        failing_cases=tuple(c for r in results if r["status"] == FAILED for c in r["failing_cases"]),
        evidence=evidence,
        parts=tuple(
            {"label": r["label"], "status": r["status"], "detail": str(r["detail"])}
            for r in results
        ),
        only_detail=results[0]["detail"] if len(results) == 1 else None,
    )


def _run_part(
    part: CheckPart, *, tree: Path, run: Runner
) -> tuple[str, Any, str, tuple[str, ...]]:
    if not part.test:
        return COULD_NOT_RUN, "nothing is declared for it", "", ()
    if part.rules is None:
        return (
            COULD_NOT_RUN,
            "GuardKit's own rules for reading a part's run "
            "(declared_no_tests_ran, ran_marker_problem) were not given, so "
            "its result could not be read",
            "",
            (),
        )
    folder = _inside(tree, part.cwd)
    if folder is None:
        return COULD_NOT_RUN, f"its folder {part.cwd} is outside the tree", "", ()
    if part.install:
        try:
            code, said = run(
                command=part.install, cwd=folder, timeout_seconds=part.install_timeout
            )
        except Exception as exc:  # noqa: BLE001 — a runner defect is not a pass
            code, said = None, f"raised {type(exc).__name__}: {exc}"
        if code != 0:
            return (
                COULD_NOT_RUN,
                f"the declared install (`{part.install}`) did not succeed: {said}",
                str(getattr(said, "evidence", "") or ""),
                (),
            )
    # As at the join: after the install, before the test, a declaration that
    # requires a marker and names none is a fault in the declaration.
    try:
        problem = part.rules.marker_problem(part)
    except Exception as exc:  # noqa: BLE001 — a rule defect is not a pass
        problem = f"could not be read: {type(exc).__name__}: {exc}"
    if problem is not None:
        return COULD_NOT_RUN, f"the declaration of part {part.label} {problem}", "", ()
    try:
        code, said = run(command=part.test, cwd=folder, timeout_seconds=part.test_timeout)
    except Exception as exc:  # noqa: BLE001 — a runner defect is not a pass
        return COULD_NOT_RUN, f"running it raised {type(exc).__name__}: {exc}", "", ()
    evidence = str(getattr(said, "evidence", "") or "")
    if code is None and getattr(said, "timed_out", False) is True:
        return (
            FAILED,
            f"its test `{part.test}` did not finish within its limit of "
            f"{part.test_timeout}s, which counts as failed, as at the join "
            f"({said})",
            evidence,
            (),
        )
    if code is None:
        return COULD_NOT_RUN, said, evidence, ()
    # The run finished: GuardKit's own rule says whether the part's own
    # declaration says no tests ran, in GuardKit's own sentence.
    output = getattr(said, "output", None)
    if output is None and part.declares_what_no_tests_looks_like:
        return (
            COULD_NOT_RUN,
            f"its test `{part.test}` exited {code}, but the run's output was "
            "not kept, so the part's declared \"no tests ran\" text could not "
            "be looked for",
            evidence,
            (),
        )
    try:
        why = part.rules.no_tests_ran(part, code, output)
    except Exception as exc:  # noqa: BLE001 — a rule defect is not a pass
        return (
            COULD_NOT_RUN,
            f"reading its run raised {type(exc).__name__}: {exc}",
            evidence,
            (),
        )
    if why is not None:
        return (
            FAILED,
            f"no tests ran in part {part.name or part.label} ({part.test}): {why}",
            evidence,
            (),
        )
    if code == 0:
        return PASSED, said, evidence, ()
    cases = tuple(str(c) for c in (getattr(said, "failing_cases", ()) or ()))
    return FAILED, said, evidence, cases
