"""One run of the nightly security sweep, from fetch to the Slack line.

Public surface
==============

- :class:`SweepDeps` — everything a run needs, each piece injectable so the
  tests run with no network, no bus, no model and no scanner.
- :func:`run_sweep` — one run over every project.
- :class:`RunReport` / :class:`ProjectResult` — what happened, in allowlisted
  fields only.
- :func:`slack_line` — the one line Rich reads, or ``None`` for silence.

A run, in the order the design gives (piece 3, steps 1 to 9)
-----------------------------------------------------------

For each distinct fetch address, one project at a time:

1. fetch the default branch into the sweep's own mirror;
2. write out its committed files from the object store (Codex R12) and scan
   them, one scanner at a time: OSV-Scanner, Gitleaks over the tree,
   Gitleaks over the history (counted only), Opengrep;
3. keep only allowlisted fields (Codex R3);
4. name each finding by its fingerprint and update its status (Codex R11);
5. ask the model about secret and code findings still ``pending``
   (Codex R5) — only while the factory is idle;
6. pick out the serious ones;
7. file at most one repair row for the project (Codex R4, R10);
9. save the state.

Then one Slack line (step 8) and one history line. If a build starts while
the sweep runs, it finishes the project it is on and stops (Codex R6); the
rest are left for the next night and named in the line.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from forge.planning.work_queue_store import OPEN_STATUSES, WorkQueueStore

from . import drafting
from .findings import (
    KIND_CODE,
    KIND_DEPENDENCY,
    KIND_SECRET,
    TOOL_GITLEAKS,
    TOOL_OPENGREP,
    TOOL_OSV,
    Finding,
    dependency_fingerprint,
    is_serious,
    line_fingerprint,
    path_kinds,
    seriousness_order,
    shown,
)
from .repository import (
    ExportFailed,
    ExportReport,
    Mirror,
    export_committed_files,
    fetch_mirror,
)
from .scanners import (
    GITLEAKS_STDIN_TIMEOUT_S,
    GITLEAKS_TIMEOUT_S,
    OPENGREP_TIMEOUT_S,
    OSV_TIMEOUT_S,
    RuleCatalogue,
    Runner,
    ScannerSetup,
    gitleaks_history_argv,
    gitleaks_stdin_argv,
    gitleaks_tree_argv,
    opengrep_argv,
    osv_argv,
    read_gitleaks,
    read_opengrep,
    read_osv,
    run_tool,
    write_factory_ignore,
)
from .sorting import Asker, build_prompt, excerpt_lines, read_answer, redacted_excerpt
from .state import (
    MIRRORS_FOLDER,
    WORK_FOLDER,
    SweepLock,
    SweepState,
    append_history,
    fingerprint_key,
    load_state,
    make_folder,
    save_state,
)

logger = logging.getLogger(__name__)

#: How long a drafted finding may stay before the line calls it still open.
STILL_OPEN_AFTER = timedelta(days=7)

#: The most model questions one run asks; the rest wait for the next night.
MAX_MODEL_QUESTIONS = 200

#: How long one model question may take, beyond the route's own limit.
MODEL_TIMEOUT_S = 180.0

#: The parts of a project the line can say were not checked.
PART_DEPENDENCIES = "dependencies"
PART_SECRETS = "secrets"
PART_HISTORY = "secret history"
PART_CODE = "code patterns"


@dataclass
class SweepDeps:
    """Everything one run needs."""

    #: The projects, by ``forge.yaml`` key, with their recorded fetch address.
    projects: Mapping[str, str]
    #: The sweep's own folder (see :mod:`.state`).
    folder: Path
    queue: WorkQueueStore
    #: Who a repair request is from, so its spec card goes to them.
    approver: str | None
    serious_score: float
    draft_repairs: bool
    weekly_all_clear: str | None
    setup: ScannerSetup
    #: ``async (text) -> None`` — the one Slack line.
    publish: Callable[[str], Awaitable[None]]
    #: Whether the factory is idle right now (nothing in flight).
    is_idle: Callable[[], bool]
    asker: Asker | None = None
    #: Plain words for why there is no asker.
    asker_missing: str = "the model is not configured"
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))
    runner: Runner = run_tool
    fetch: Callable[[str, Path], Mirror] = fetch_mirror
    export: Callable[[Path, str, Path], ExportReport] = export_committed_files
    save: Callable[[Path, SweepState], None] = save_state
    catalogue: RuleCatalogue | None = None
    max_model_questions: int = MAX_MODEL_QUESTIONS
    model_timeout: float = MODEL_TIMEOUT_S


@dataclass
class ProjectResult:
    """What the sweep did for one project. Allowlisted fields only."""

    project: str
    fetched: bool = False
    fetch_problem: bool = False
    #: The mirror was fetched but its files could not be written out.
    unreadable: bool = False
    not_checked: dict[str, str] = field(default_factory=dict)
    no_lockfile: bool = False
    new: int = 0
    new_serious: int = 0
    serious_eligible: int = 0
    drafted_row: int | None = None
    drafted_count: int = 0
    waiting_for_row: int | None = None
    not_drafted_reason: str | None = None
    pending: int = 0
    pending_reason: str | None = None
    still_open: dict[int, int] = field(default_factory=dict)
    history_secret_matches: int | None = None
    symlinks_skipped: list[str] = field(default_factory=list)
    submodules_skipped: list[str] = field(default_factory=list)
    settings_left_out: list[str] = field(default_factory=list)
    #: Files over the export's size limit: not checked, said so.
    too_large: list[str] = field(default_factory=list)
    #: Repair rows whose findings are still present, and the status of each
    #: one that is already closed.
    still_open_closed: dict[int, str] = field(default_factory=dict)

    @property
    def has_news(self) -> bool:
        """Whether this project alone is reason to post the line."""
        return bool(
            self.fetch_problem
            or self.unreadable
            or self.not_checked
            or self.new
            or self.drafted_row is not None
            or self.pending
        )

    def history(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "fetched": self.fetched,
            "unreadable": self.unreadable,
            "new": self.new,
            "new_serious": self.new_serious,
            "serious_eligible": self.serious_eligible,
            "pending": self.pending,
        }
        for name in ("drafted_row", "waiting_for_row", "history_secret_matches"):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        if self.drafted_count:
            out["drafted_count"] = self.drafted_count
        if self.not_checked:
            out["not_checked"] = dict(self.not_checked)
        if self.no_lockfile:
            out["no_lockfile"] = True
        if self.still_open:
            out["still_open"] = {str(k): v for k, v in self.still_open.items()}
        if self.still_open_closed:
            out["still_open_closed"] = {str(k): v for k, v in self.still_open_closed.items()}
        for name in ("symlinks_skipped", "submodules_skipped", "settings_left_out", "too_large"):
            values = getattr(self, name)
            if values:
                out[name] = values[:20]
        return out


@dataclass
class RunReport:
    """One run's outcome, the line it posted, and every project's result."""

    started_at: datetime
    outcome: str = "finished"
    projects: list[ProjectResult] = field(default_factory=list)
    left_for_next: list[str] = field(default_factory=list)
    line: str | None = None
    failure: str | None = None


# ---------------------------------------------------------------------------
# The line


def display_names(projects: list[str]) -> dict[str, str]:
    """Each project's short name (``org/name`` -> ``name``) when unambiguous."""
    short = {p: p.rsplit("/", 1)[-1] for p in projects}
    counts: dict[str, int] = {}
    for name in short.values():
        counts[name] = counts.get(name, 0) + 1
    return {p: (name if counts[name] == 1 else p) for p, name in short.items()}


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _project_phrase(result: ProjectResult, name: str) -> str:
    if result.fetch_problem:
        return f"{name} — could not be fetched, so it was not checked"
    if result.unreadable:
        return f"{name} — its files could not be read from the fetched copy, so it was not checked"
    parts: list[str] = []
    if result.drafted_row is not None:
        text = (
            f"{_plural(result.drafted_count, 'serious problem', 'serious problems')}, "
            "repair request sent (its spec card follows)"
        )
        rest = result.serious_eligible - result.drafted_count
        if rest > 0:
            text += f"; {rest} more wait for it"
        parts.append(text)
    elif result.waiting_for_row is not None:
        parts.append(
            f"{_plural(result.serious_eligible, 'serious problem waits', 'serious problems wait')} "
            f"for repair request #{result.waiting_for_row} to finish"
        )
    elif result.serious_eligible:
        reason = f" ({result.not_drafted_reason})" if result.not_drafted_reason else ""
        parts.append(
            f"{_plural(result.serious_eligible, 'serious problem', 'serious problems')}, "
            f"no repair request drafted{reason}"
        )
    other_new = result.new - result.new_serious
    if other_new > 0:
        parts.append(f"{_plural(other_new, 'new finding', 'new findings')}, not serious")
    if result.pending:
        reason = f" ({result.pending_reason})" if result.pending_reason else ""
        parts.append(f"{result.pending} waiting to be sorted{reason}")
    for part, reason in result.not_checked.items():
        parts.append(f"{part} not checked ({reason})")
    if result.no_lockfile and PART_DEPENDENCIES not in result.not_checked:
        parts.append("dependencies can't be checked (no lockfile)")
    if result.too_large:
        parts.append(
            f"{_plural(len(result.too_large), 'file', 'files')} not checked: too large"
        )
    for row, count in sorted(result.still_open.items()):
        text = (
            f"findings from repair request #{row} are still present after 7 days "
            f"({count} of them)"
        )
        closed = result.still_open_closed.get(row)
        if closed:
            text += f"; that request is closed ({closed.lower()})"
        parts.append(text)
    if not parts:
        parts.append("nothing new")
    return f"{name} — " + ", ".join(parts)


def slack_line(report: RunReport, *, all_clear_day: bool) -> str | None:
    """The one line for this run, or None for silence.

    Posted when anything is new, a repair was drafted, a project could not be
    checked, findings wait to be sorted, projects were left for the next
    night, or the run failed. Otherwise silent, except on the weekly
    all-clear day. Built from allowlisted fields only.
    """
    if report.outcome == "failed":
        return (
            "Nightly security check: the run failed "
            f"({report.failure or 'an unexpected error'}), so tonight's check is incomplete."
        )
    names = display_names([r.project for r in report.projects] + report.left_for_next)
    news = any(r.has_news for r in report.projects) or bool(report.left_for_next)
    if not news and not all_clear_day:
        return None
    phrases = [_project_phrase(r, names[r.project]) for r in report.projects]
    if not news:
        head = "Nightly security check, weekly all clear: "
    else:
        head = "Nightly security check: "
    line = head + ("; ".join(phrases) if phrases else "no projects to check") + "."
    if report.left_for_next:
        left = ", ".join(names[p] for p in report.left_for_next)
        line += f" Left for tomorrow night because a build started: {left}."
    return line


# ---------------------------------------------------------------------------
# One run


def _today(deps: SweepDeps) -> str:
    return deps.clock().date().isoformat()


def _distinct(projects: Mapping[str, str]) -> list[tuple[str, str]]:
    """One project per distinct fetch address, in key order."""
    seen: set[str] = set()
    ordered: list[tuple[str, str]] = []
    for key in sorted(projects):
        address = str(projects[key] or "").strip()
        if not address or address in seen:
            continue
        seen.add(address)
        ordered.append((key, address))
    return ordered


async def run_sweep(deps: SweepDeps) -> RunReport:
    """One sweep over every project. Never raises except when another holds the lock.

    Raises :class:`~.state.SweepAlreadyRunning` when another sweep is running.
    Anything else that goes wrong ends the run as ``failed`` with one line
    saying so: a sweep is never silent about failing.
    """
    make_folder(deps.folder)
    with SweepLock(deps.folder):
        report = RunReport(started_at=deps.clock())
        state = SweepState()
        try:
            key = fingerprint_key(deps.folder)
            state = load_state(deps.folder)
            rows = drafting.security_rows(deps.queue)
            catalogue = deps.catalogue
            if catalogue is None and deps.setup.missing("opengrep") is None:
                try:
                    catalogue = RuleCatalogue.from_files(
                        deps.setup.opengrep_rules, deps.setup.compose_rules
                    )
                except Exception:  # noqa: BLE001 — the code part is then not checked
                    catalogue = None
            budget = {"questions": deps.max_model_questions, "model_failed": False}
            projects = _distinct(deps.projects)
            for index, (project, address) in enumerate(projects):
                if index > 0 and not deps.is_idle():
                    report.outcome = "stopped-busy"
                    report.left_for_next = [p for p, _ in projects[index:]]
                    break
                result = await _sweep_project(
                    deps, project, address, key, state, rows, catalogue, budget
                )
                report.projects.append(result)
                deps.save(deps.folder, state)
        except Exception as exc:  # noqa: BLE001 — a run that fails says so
            report.outcome = "failed"
            report.failure = type(exc).__name__
            logger.error(
                "security sweep: the run failed (%s); %d project(s) were done",
                type(exc).__name__,
                len(report.projects),
            )
        all_clear_day = _all_clear_day(deps, state)
        report.line = slack_line(report, all_clear_day=all_clear_day)
        posted = False
        if report.line is not None:
            try:
                await deps.publish(report.line)
                posted = True
            except Exception as exc:  # noqa: BLE001 — the history still records the run
                logger.warning(
                    "security sweep: the Slack line could not be published (%s)",
                    type(exc).__name__,
                )
        if posted and all_clear_day and report.outcome != "failed":
            state.last_all_clear = _today(deps)
        if report.outcome != "failed":
            try:
                deps.save(deps.folder, state)
            except Exception as exc:  # noqa: BLE001
                logger.error("security sweep: the state could not be saved (%s)", type(exc).__name__)
        _write_history(deps, report, posted)
        logger.info(
            "security sweep: %s — %d project(s) checked, %d left for the next night, line %s",
            report.outcome,
            len(report.projects),
            len(report.left_for_next),
            "posted" if posted else "not posted",
        )
        return report


def _all_clear_day(deps: SweepDeps, state: SweepState) -> bool:
    if not deps.weekly_all_clear:
        return False
    now = deps.clock()
    return (
        now.strftime("%A").lower() == deps.weekly_all_clear
        and state.last_all_clear != now.date().isoformat()
    )


def _write_history(deps: SweepDeps, report: RunReport, posted: bool) -> None:
    line = {
        "at": report.started_at.isoformat(),
        "outcome": report.outcome,
        "posted": posted,
        "projects": {r.project: r.history() for r in report.projects},
    }
    if report.left_for_next:
        line["left_for_next"] = list(report.left_for_next)
    if report.failure:
        line["failure"] = report.failure
    try:
        append_history(deps.folder, line)
    except OSError as exc:
        logger.warning("security sweep: the history line could not be written (%s)", type(exc).__name__)


# ---------------------------------------------------------------------------
# One project


class _Files:
    """The exported files, each read once, for line hashes and excerpts."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._cache: dict[str, bytes | None] = {}

    def data(self, relative: str) -> bytes | None:
        if relative not in self._cache:
            target = (self._root / relative).resolve()
            try:
                target.relative_to(self._root)
                self._cache[relative] = target.read_bytes() if target.is_file() else None
            except (ValueError, OSError):
                self._cache[relative] = None
        return self._cache[relative]

    def line(self, relative: str, number: int) -> bytes:
        data = self.data(relative)
        if data is None or number < 1:
            return b""
        lines = data.split(b"\n")
        return lines[number - 1] if number <= len(lines) else b""


async def _sweep_project(
    deps: SweepDeps,
    project: str,
    address: str,
    key: bytes,
    state: SweepState,
    rows: list[drafting.SecurityRow],
    catalogue: RuleCatalogue | None,
    budget: dict[str, Any],
) -> ProjectResult:
    result = ProjectResult(project=project)
    mirror = await asyncio.to_thread(deps.fetch, address, deps.folder / MIRRORS_FOLDER)
    if mirror.refusal or mirror.path is None or not mirror.commit:
        result.fetch_problem = True
        logger.warning("security sweep: %s could not be fetched", project)
        return result
    result.fetched = True
    work = Path(tempfile.mkdtemp(prefix="project-", dir=str(deps.folder / WORK_FOLDER)))
    try:
        tree = work / "tree"
        tree.mkdir()
        try:
            exported = await asyncio.to_thread(deps.export, mirror.path, mirror.commit, tree)
        except ExportFailed:
            result.unreadable = True
            logger.warning("security sweep: %s was fetched but its files could not be read", project)
            return result
        result.symlinks_skipped = [shown(p) for p in exported.symlinks]
        result.submodules_skipped = [shown(p) for p in exported.submodules]
        result.settings_left_out = [shown(p) for p in exported.settings_left_out]
        result.too_large = [shown(p) for p in exported.too_large]
        files = _Files(tree)
        current: dict[str, Finding] = {}
        checked: set[str] = set()
        secret_lines: dict[str, set[int]] = {}

        await _scan_dependencies(deps, project, tree, work, result, current, checked)
        await _scan_secrets(deps, project, tree, work, key, files, result, current, checked, secret_lines)
        await _count_history(deps, mirror.path, work, result)
        await _scan_code(deps, project, tree, work, key, files, catalogue, result, current, checked)

        new = _update_statuses(deps, project, state, rows, current, checked, result)
        await _sort_pending(deps, state, current, files, secret_lines, result, budget)
        result.new_serious = sum(
            1
            for fp in new
            if is_serious(current[fp], state.status(fp) or "", deps.serious_score)
        )
        _draft(deps, project, state, rows, current, result)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return result


async def _scan_dependencies(deps, project, tree, work, result, current, checked) -> None:
    missing = deps.setup.missing("osv-scanner")
    if missing:
        result.not_checked[PART_DEPENDENCIES] = missing
        return
    run = await deps.runner(osv_argv(deps.setup, tree), cwd=work, timeout=OSV_TIMEOUT_S, ok_codes=(0, 1))
    if not run.ok:
        result.not_checked[PART_DEPENDENCIES] = f"the dependency scanner {run.reason}"
        return
    try:
        reading = read_osv(run.stdout, tree)
    except ValueError:
        result.not_checked[PART_DEPENDENCIES] = "the dependency scanner's report could not be read"
        return
    checked.add(TOOL_OSV)
    if reading.sources == 0:
        result.no_lockfile = True
    for adv in reading.advisories:
        fp = dependency_fingerprint(project, adv.advisory, adv.package, adv.version)
        if fp in current:
            continue
        current[fp] = Finding(
            project=project,
            kind=KIND_DEPENDENCY,
            tool=TOOL_OSV,
            rule=shown(adv.advisory),
            file=shown(adv.file),
            line=None,
            fingerprint=fp,
            severity=shown(adv.severity) if adv.severity else None,
            package=shown(adv.package),
            version=shown(adv.version),
            ecosystem=shown(adv.ecosystem),
            advisory=shown(adv.advisory),
            score=adv.score,
            fixed=tuple(shown(v) for v in adv.fixed),
            path_kinds=path_kinds(adv.file) if adv.file else (),
        )


async def _scan_secrets(deps, project, tree, work, key, files, result, current, checked, secret_lines) -> None:
    missing = deps.setup.missing("gitleaks")
    if missing:
        result.not_checked[PART_SECRETS] = missing
        return
    run = await deps.runner(gitleaks_tree_argv(deps.setup, tree), cwd=work, timeout=GITLEAKS_TIMEOUT_S)
    if not run.ok:
        result.not_checked[PART_SECRETS] = f"the secret scanner {run.reason}"
        return
    try:
        locations = read_gitleaks(run.stdout, tree)
    except ValueError:
        result.not_checked[PART_SECRETS] = "the secret scanner's report could not be read"
        return
    checked.add(TOOL_GITLEAKS)
    for loc in locations:
        if not loc.file:
            continue
        secret_lines.setdefault(loc.file, set()).update(range(loc.start, loc.end + 1))
        fp = line_fingerprint(
            key,
            project=project,
            kind=KIND_SECRET,
            tool=TOOL_GITLEAKS,
            rule=loc.rule,
            file=loc.file,
            line=loc.start,
            line_text=files.line(loc.file, loc.start),
        )
        current.setdefault(
            fp,
            Finding(
                project=project,
                kind=KIND_SECRET,
                tool=TOOL_GITLEAKS,
                rule=shown(loc.rule),
                file=shown(loc.file),
                line=loc.start,
                fingerprint=fp,
                path_kinds=path_kinds(loc.file),
                source_file=loc.file,
            ),
        )


async def _count_history(deps, mirror: Path, work: Path, result: ProjectResult) -> None:
    if deps.setup.missing("gitleaks"):
        return  # already said under secrets
    run = await deps.runner(gitleaks_history_argv(deps.setup, mirror), cwd=work, timeout=GITLEAKS_TIMEOUT_S)
    if not run.ok:
        result.not_checked[PART_HISTORY] = f"the secret scanner {run.reason}"
        return
    try:
        result.history_secret_matches = len(read_gitleaks(run.stdout))
    except ValueError:
        result.not_checked[PART_HISTORY] = "the secret scanner's report could not be read"


async def _scan_code(deps, project, tree, work, key, files, catalogue, result, current, checked) -> None:
    missing = deps.setup.missing("opengrep")
    if missing:
        result.not_checked[PART_CODE] = missing
        return
    if catalogue is None:
        result.not_checked[PART_CODE] = "the code scanner's rules could not be read"
        return
    # The factory's own ignore list, in the folder Opengrep runs in, in place
    # of Opengrep's built-in one (see scanners.FACTORY_SEMGREPIGNORE).
    write_factory_ignore(work)
    run = await deps.runner(opengrep_argv(deps.setup, tree), cwd=work, timeout=OPENGREP_TIMEOUT_S, ok_codes=(0, 1))
    if not run.ok:
        result.not_checked[PART_CODE] = f"the code scanner {run.reason}"
        return
    try:
        locations = read_opengrep(run.stdout, tree, catalogue)
    except ValueError:
        result.not_checked[PART_CODE] = "the code scanner's report could not be read"
        return
    checked.add(TOOL_OPENGREP)
    for loc in locations:
        fp = line_fingerprint(
            key,
            project=project,
            kind=KIND_CODE,
            tool=TOOL_OPENGREP,
            rule=loc.rule,
            file=loc.file,
            line=loc.start,
            line_text=files.line(loc.file, loc.start),
        )
        current.setdefault(
            fp,
            Finding(
                project=project,
                kind=KIND_CODE,
                tool=TOOL_OPENGREP,
                rule=shown(loc.rule),
                file=shown(loc.file),
                line=loc.start,
                fingerprint=fp,
                message=catalogue.message(loc.rule),
                severity=loc.severity,
                path_kinds=path_kinds(loc.file),
                end_line=loc.end,
                source_file=loc.file,
            ),
        )


def _update_statuses(deps, project, state, rows, current, checked, result) -> set[str]:
    """Name each finding's status; return the fingerprints that are new tonight."""
    today = _today(deps)
    new: set[str] = set()
    drafted_in_queue = {fp: row.queue_id for row in rows for fp in row.fingerprints}
    for fp, finding in current.items():
        entry = state.findings.get(fp)
        if fp in drafted_in_queue:
            if entry is None or entry.get("status") == "gone":
                entry = {"first_seen": today}
            if entry.get("status") != "drafted":
                entry["status"] = "drafted"
                entry["queue_id"] = drafted_in_queue[fp]
                entry.setdefault("drafted_at", today)
        elif entry is None or entry.get("status") == "gone":
            entry = {
                "first_seen": today,
                "status": "real" if finding.kind == KIND_DEPENDENCY else "pending",
            }
            result.new += 1
            new.add(fp)
        entry.update(finding.record())
        entry["last_seen"] = today
        state.findings[fp] = entry
    for fp, entry in list(state.entries_for(project)):
        if fp in current or entry.get("status") == "gone":
            continue
        if entry.get("tool") in checked:
            entry["status"] = "gone"
            entry["gone_at"] = today
    return new


async def _sort_pending(deps, state, current, files, secret_lines, result, budget) -> None:
    pending = [f for fp, f in current.items() if f.needs_sorting and state.status(fp) == "pending"]
    pending.sort(
        key=lambda f: (
            0 if f.kind == KIND_SECRET else 1 if str(f.severity or "").upper() == "ERROR" else 2,
            f.file,
            f.line or 0,
            f.rule,
        )
    )
    reason: str | None = None
    for finding in pending:
        if deps.asker is None:
            reason = deps.asker_missing
            break
        if budget["model_failed"]:
            reason = "the model could not be asked"
            break
        if budget["questions"] <= 0:
            reason = "tonight's questions ran out; the rest are asked tomorrow"
            break
        if not deps.is_idle():
            reason = "a build started, so the model was left to it"
            break
        excerpt = None
        if finding.kind == KIND_CODE:
            excerpt = await _excerpt(deps, finding, files, secret_lines)
        prompt = build_prompt(finding, excerpt)
        budget["questions"] -= 1
        try:
            answer_text = await asyncio.wait_for(
                asyncio.to_thread(deps.asker, prompt), timeout=deps.model_timeout
            )
        except Exception as exc:  # noqa: BLE001 — the finding stays pending (Codex R5)
            budget["model_failed"] = True
            reason = "the model could not be asked"
            logger.warning("security sweep: the model could not be asked (%s)", type(exc).__name__)
            break
        answer = read_answer(answer_text)
        del answer_text  # the model's own words go no further
        if answer is None:
            reason = reason or "the model's answer was not a clear yes or no"
            continue
        entry = state.findings[finding.fingerprint]
        entry["status"] = "real" if answer else "not-real"
        entry["sorted_at"] = _today(deps)
    result.pending = sum(
        1 for fp, f in current.items() if f.needs_sorting and state.status(fp) == "pending"
    )
    if result.pending:
        result.pending_reason = reason or "the model could not be asked"


async def _excerpt(deps, finding: Finding, files: _Files, secret_lines: dict[str, set[int]]) -> str | None:
    """The redacted excerpt for a code finding, or None when it cannot be made safe."""
    if deps.setup.missing("gitleaks"):
        return None
    raw = finding.source_file or finding.file
    data = files.data(raw)
    if data is None or finding.line is None:
        return None
    first, lines = excerpt_lines(data, finding.line, finding.end_line or finding.line)
    raw = "\n".join(lines).encode("utf-8")
    run = await deps.runner(gitleaks_stdin_argv(deps.setup), timeout=GITLEAKS_STDIN_TIMEOUT_S, stdin=raw)
    if not run.ok:
        return None
    try:
        flagged = read_gitleaks(run.stdout)
    except ValueError:
        return None
    hidden = set(secret_lines.get(raw, set()))
    for loc in flagged:
        hidden.update(range(first + loc.start - 1, first + loc.end))
    return redacted_excerpt(first, lines, hidden)


def _draft(deps, project, state, rows, current, result) -> None:
    today = deps.clock().date()
    eligible = [
        f
        for fp, f in current.items()
        if is_serious(f, state.status(fp) or "", deps.serious_score) and state.status(fp) != "drafted"
    ]
    for fp, f in current.items():
        entry = state.findings.get(fp) or {}
        if entry.get("status") == "drafted" and entry.get("drafted_at"):
            try:
                since = datetime.fromisoformat(str(entry["drafted_at"])).date()
            except ValueError:
                continue
            if today - since >= STILL_OPEN_AFTER and isinstance(entry.get("queue_id"), int):
                row = int(entry["queue_id"])
                result.still_open[row] = result.still_open.get(row, 0) + 1
                status = next((r.status for r in rows if r.queue_id == row), None)
                if status is not None and status not in OPEN_STATUSES:
                    result.still_open_closed[row] = status
    result.serious_eligible = len(eligible)
    if not eligible:
        return
    if not deps.draft_repairs:
        result.not_drafted_reason = "drafting is switched off"
        return
    open_rows = [r for r in rows if r.target_repo == project and r.is_open]
    if open_rows:
        result.waiting_for_row = open_rows[0].queue_id
        return
    if not deps.approver:
        result.not_drafted_reason = "no approver is configured"
        return
    chosen = sorted(eligible, key=seriousness_order)[: drafting.MAX_ITEMS]
    queue_id, created, _correlation = drafting.file_repair(
        deps.queue, project=project, findings=chosen, approver=deps.approver
    )
    rows.append(
        drafting.SecurityRow(
            queue_id=queue_id,
            status="QUEUED",
            target_repo=project,
            fingerprints=tuple(f.fingerprint for f in chosen),
        )
    )
    for finding in chosen:
        entry = state.findings[finding.fingerprint]
        entry["status"] = "drafted"
        entry["queue_id"] = queue_id
        entry["drafted_at"] = today.isoformat()
    result.drafted_row = queue_id
    result.drafted_count = len(chosen)
    logger.info(
        "security sweep: %s — repair request #%d %s with %d serious finding(s)",
        project,
        queue_id,
        "filed" if created else "was already filed",
        len(chosen),
    )


__all__ = [
    "PART_CODE",
    "PART_DEPENDENCIES",
    "PART_HISTORY",
    "PART_SECRETS",
    "ProjectResult",
    "RunReport",
    "STILL_OPEN_AFTER",
    "SweepDeps",
    "display_names",
    "run_sweep",
    "slack_line",
]
