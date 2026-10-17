"""The three scanners the nightly sweep runs, and what it keeps of their output.

Public surface
==============

- :class:`ScannerSetup` — where the three programs and the factory's own
  scanner settings are. :meth:`ScannerSetup.from_image` finds them where the
  release image installs them.
- :func:`run_tool` — one scanner run as an ``asyncio`` subprocess with its own
  time limit and an environment holding only ``PATH`` and a fresh ``HOME``.
- The argument lists, one function each (:func:`osv_argv`,
  :func:`gitleaks_tree_argv`, :func:`gitleaks_history_argv`,
  :func:`gitleaks_stdin_argv`, :func:`opengrep_argv`), so the tests can pin
  exactly what is passed.
- The readers (:func:`read_osv`, :func:`read_gitleaks`, :func:`read_opengrep`)
  and :class:`RuleCatalogue`, which turn raw output into the few allowlisted
  fields the sweep keeps.

Why the settings are the factory's and never the project's (Codex R9)
--------------------------------------------------------------------

The coding model can write any file in a project, so no scanner may take a
setting from the project. The sweep removes the scanners' settings files from
what it exports (see :mod:`.repository`), and on top of that:

- OSV-Scanner is given the factory's own empty ``osv-scanner.toml``
  (``--config``), which overrides any file beside a lockfile, and
  ``--no-ignore``, so a ``.gitignore`` cannot hide a lockfile;
- Gitleaks uses its built-in rules (no ``--config``, and the environment
  carries no ``GITLEAKS_CONFIG``), looks for an ignore file only in the
  factory's empty folder (``--gitleaks-ignore-path``), and ignores inline
  ``gitleaks:allow`` comments (``--ignore-gitleaks-allow``);
- Opengrep runs with ``--disable-nosem``, so a ``nosemgrep`` comment hides
  nothing, and ``--no-git-ignore``.

Why nothing raw leaves (Codex R3)
---------------------------------

Every scanner writes to a pipe this process reads; nothing is written to a
file. Gitleaks always runs with ``--redact``. The readers keep only rule ids,
file paths, line numbers, packages, versions, advisory ids, scores and fixed
versions. The rule's message is taken from the rule file the factory ships
(:class:`RuleCatalogue`), never from the scanner's output, because the output
fills a rule's ``$NAME`` placeholders with text from the scanned file.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable, Mapping, Sequence

logger = logging.getLogger(__name__)

#: The only ``PATH`` a scanner is given. Gitleaks runs ``git`` from it.
SCANNER_PATH = "/usr/local/bin:/usr/bin:/bin"

#: Where the release image keeps the factory's scanner settings.
FACTORY_SECURITY_DIR = Path("/opt/factory-security")

#: Where the release image keeps the Opengrep security rules.
OPENGREP_RULES = Path("/opt/opengrep/security-rules.yaml")

#: Time limits, one scanner at a time (design, "Keeping off the event loop").
OSV_TIMEOUT_S = 300.0
GITLEAKS_TIMEOUT_S = 300.0
OPENGREP_TIMEOUT_S = 600.0
#: The excerpt check is one small input; a minute is plenty.
GITLEAKS_STDIN_TIMEOUT_S = 60.0

#: How long a rule message may be when it is repeated outward.
MESSAGE_LIMIT = 200


@dataclass(frozen=True)
class ToolRun:
    """One scanner run: whether it finished, its standard output, and why not."""

    ok: bool
    stdout: bytes = b""
    #: Plain words when ``ok`` is False ("timed out after 300 seconds").
    reason: str = ""


#: ``async (argv, *, cwd, timeout, stdin) -> ToolRun`` — the injectable runner.
Runner = Callable[..., Awaitable[ToolRun]]


async def run_tool(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: float,
    stdin: bytes | None = None,
    ok_codes: Sequence[int] = (0,),
) -> ToolRun:
    """Run one scanner as an ``asyncio`` subprocess. Never raises.

    The environment is built from nothing: ``PATH`` (:data:`SCANNER_PATH`) and
    ``HOME`` set to a fresh empty folder, removed afterwards, so none of the
    coordinator's settings, credentials or caches reach the scanner. The
    scanner's error output is read and dropped: it is never logged, because a
    scanner may quote what it scanned there. A run that passes its time limit
    is killed, with every process it started.
    """
    home = Path(tempfile.mkdtemp(prefix="forge-sweep-home-"))
    try:
        environment = {"PATH": SCANNER_PATH, "HOME": str(home)}
        try:
            process = await asyncio.create_subprocess_exec(
                *[str(a) for a in argv],
                cwd=str(cwd or home),
                env=environment,
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError:
            return ToolRun(False, reason="could not be started")
        try:
            stdout, _stderr = await asyncio.wait_for(process.communicate(stdin), timeout=timeout)
        except asyncio.TimeoutError:
            _kill(process)
            await process.wait()
            return ToolRun(False, reason=f"did not finish within {timeout:g} seconds")
        except asyncio.CancelledError:
            _kill(process)
            raise
        if process.returncode not in ok_codes:
            return ToolRun(False, reason=f"stopped with exit code {process.returncode}")
        return ToolRun(True, stdout or b"")
    finally:
        shutil.rmtree(home, ignore_errors=True)


def _kill(process: Any) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except ProcessLookupError:
            pass


@dataclass(frozen=True)
class ScannerSetup:
    """Where the three scanners and the factory's settings are.

    A program that is ``None`` is not installed; the part it does is then
    reported as not checked, never as clean.
    """

    gitleaks: str | None
    osv_scanner: str | None
    opengrep: str | None
    factory_dir: Path = FACTORY_SECURITY_DIR
    opengrep_rules: Path = OPENGREP_RULES

    @classmethod
    def from_image(
        cls,
        *,
        factory_dir: Path = FACTORY_SECURITY_DIR,
        opengrep_rules: Path = OPENGREP_RULES,
        search_path: str = SCANNER_PATH,
    ) -> "ScannerSetup":
        """The programs found on :data:`SCANNER_PATH`, by absolute path."""
        return cls(
            gitleaks=shutil.which("gitleaks", path=search_path),
            osv_scanner=shutil.which("osv-scanner", path=search_path),
            opengrep=shutil.which("opengrep", path=search_path),
            factory_dir=factory_dir,
            opengrep_rules=opengrep_rules,
        )

    @property
    def osv_config(self) -> Path:
        return self.factory_dir / "osv-scanner.toml"

    @property
    def gitleaks_ignore_dir(self) -> Path:
        return self.factory_dir / "gitleaks-ignore"

    @property
    def compose_rules(self) -> Path:
        return self.factory_dir / "compose-rules.yaml"

    def missing(self, tool: str) -> str | None:
        """Why ``tool`` cannot run, in plain words, or None when it can."""
        if tool == "osv-scanner":
            if not self.osv_scanner:
                return "the dependency scanner is not installed"
            if not self.osv_config.is_file():
                return "the factory's dependency scanner settings are missing"
        elif tool == "gitleaks":
            if not self.gitleaks:
                return "the secret scanner is not installed"
            if not self.gitleaks_ignore_dir.is_dir():
                return "the factory's secret scanner settings are missing"
        elif tool == "opengrep":
            if not self.opengrep:
                return "the code scanner is not installed"
            if not self.opengrep_rules.is_file() or not self.compose_rules.is_file():
                return "the code scanner's rules are missing"
        return None


# ---------------------------------------------------------------------------
# Argument lists


def osv_argv(setup: ScannerSetup, target: Path) -> list[str]:
    """OSV-Scanner over the whole exported tree, with the factory's settings."""
    return [
        str(setup.osv_scanner),
        "scan",
        "source",
        "--recursive",
        "--no-ignore",
        "--allow-no-lockfiles",
        # Every lockfile is listed, with or without advisories, so a project
        # whose dependencies are clean is not mistaken for one with no
        # lockfile (fix pass, 17 October 2026).
        "--all-packages",
        "--no-call-analysis=all",
        "--config",
        str(setup.osv_config),
        "--format",
        "json",
        "--verbosity",
        "error",
        str(target),
    ]


def _gitleaks_common(setup: ScannerSetup) -> list[str]:
    return [
        "--no-banner",
        "--redact",
        "--ignore-gitleaks-allow",
        "--gitleaks-ignore-path",
        str(setup.gitleaks_ignore_dir),
        "--report-format",
        "json",
        "--report-path",
        "-",
        "--exit-code",
        "0",
        "--log-level",
        "error",
    ]


def gitleaks_tree_argv(setup: ScannerSetup, target: Path) -> list[str]:
    """Gitleaks over the exported files of the default branch."""
    return [str(setup.gitleaks), "dir", *_gitleaks_common(setup), str(target)]


def gitleaks_history_argv(setup: ScannerSetup, mirror: Path) -> list[str]:
    """Gitleaks over the bare mirror's history (counted only)."""
    return [str(setup.gitleaks), "git", *_gitleaks_common(setup), str(mirror)]


def gitleaks_stdin_argv(setup: ScannerSetup) -> list[str]:
    """Gitleaks over a code excerpt on standard input, before the model sees it."""
    return [str(setup.gitleaks), "stdin", *_gitleaks_common(setup)]


#: The factory's own ignore list for Opengrep (fix pass, 17 October 2026).
#: With no ``.semgrepignore`` Opengrep falls back to a built-in list that
#: silently skips ``build/``, ``dist/``, ``vendor/``, ``test/``, ``tests/`` and
#: ``node_modules/``. The sweep skips only installed packages
#: (``node_modules/``): everything else is scanned, test folders included,
#: because the model sorts out the noise and a privileged Compose file under
#: ``build/`` is still a privileged Compose file. Opengrep reads this file from
#: the folder it runs in (checked with Opengrep 1.30.1), so it is written
#: there, beside the exported tree and never inside it; the project's own
#: ``.semgrepignore`` files are left out of the export.
FACTORY_SEMGREPIGNORE = "node_modules/\n"


def write_factory_ignore(folder: Path) -> Path:
    """Write the factory's ``.semgrepignore`` into the folder Opengrep runs in."""
    path = folder / ".semgrepignore"
    path.write_text(FACTORY_SEMGREPIGNORE, encoding="utf-8")
    return path


def opengrep_argv(setup: ScannerSetup, target: Path) -> list[str]:
    """Opengrep with the image's security rules and the factory's Compose rules."""
    return [
        str(setup.opengrep),
        "scan",
        "--config",
        str(setup.opengrep_rules),
        "--config",
        str(setup.compose_rules),
        "--disable-nosem",
        "--no-git-ignore",
        "--disable-version-check",
        "--json",
        "--quiet",
        "--jobs",
        "2",
        str(target),
    ]


# ---------------------------------------------------------------------------
# Reading the output


def _relative(path: str, root: Path) -> str | None:
    """A scanner's path as a path inside the export, or None outside it."""
    text = str(path or "")
    if not text:
        return None
    candidate = Path(text)
    if candidate.is_absolute():
        try:
            text = candidate.resolve().relative_to(root.resolve()).as_posix()
        except (ValueError, OSError):
            return None
    pure = PurePosixPath(text)
    parts = [p for p in pure.parts if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    return "/".join(parts)


@dataclass(frozen=True)
class Advisory:
    """One advisory against one package, as the sweep keeps it."""

    file: str
    package: str
    version: str
    ecosystem: str
    advisory: str
    score: float | None
    severity: str | None
    fixed: tuple[str, ...]


@dataclass(frozen=True)
class OsvReading:
    advisories: tuple[Advisory, ...]
    #: How many lockfiles or manifests the scanner read packages from.
    sources: int


def _severity_word(score: float | None, label: object) -> str | None:
    if isinstance(label, str) and label.strip():
        return label.strip().lower()
    if score is None:
        return None
    if score >= 9.0:
        return "critical"
    if score >= 7.0:
        return "high"
    if score >= 4.0:
        return "moderate"
    return "low"


_NUMBER_RUN = re.compile(r"(\d+)")


def _version_order(text: str) -> tuple[object, ...]:
    """Display order for fixed versions. Only for listing, never for deciding."""
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part) for part in _NUMBER_RUN.split(text) if part
    )


def read_osv(stdout: bytes, root: Path) -> OsvReading:
    """The advisories in OSV-Scanner's JSON, one per package and advisory group.

    An advisory group is OSV's own merge of ids that name one problem; the
    group's representative id is a ``MAL-`` id when there is one, otherwise
    the first. Its score is OSV's ``max_severity`` for the group.
    """
    data = json.loads(stdout.decode("utf-8") or "{}")
    results = data.get("results") if isinstance(data, dict) else None
    advisories: list[Advisory] = []
    sources = 0
    for result in results if isinstance(results, list) else []:
        if not isinstance(result, dict):
            continue
        source = result.get("source") if isinstance(result.get("source"), dict) else {}
        file = _relative(str(source.get("path") or ""), root) or ""
        packages = result.get("packages")
        if file:
            sources += 1
        for entry in packages if isinstance(packages, list) else []:
            if not isinstance(entry, dict):
                continue
            package = entry.get("package") if isinstance(entry.get("package"), dict) else {}
            name = str(package.get("name") or "")
            version = str(package.get("version") or "")
            ecosystem = str(package.get("ecosystem") or "")
            vulns = {
                str(v.get("id")): v
                for v in entry.get("vulnerabilities") or []
                if isinstance(v, dict) and v.get("id")
            }
            for group in entry.get("groups") or []:
                if not isinstance(group, dict):
                    continue
                ids = [str(i) for i in group.get("ids") or [] if i]
                if not ids:
                    continue
                malicious = [i for i in ids if i.startswith("MAL-")]
                advisory = malicious[0] if malicious else ids[0]
                try:
                    score: float | None = float(str(group.get("max_severity") or ""))
                except ValueError:
                    score = None
                label = None
                fixed: set[str] = set()
                for vid in ids:
                    vuln = vulns.get(vid)
                    if not vuln:
                        continue
                    specific = vuln.get("database_specific")
                    if label is None and isinstance(specific, dict):
                        label = specific.get("severity")
                    for affected in vuln.get("affected") or []:
                        if not isinstance(affected, dict):
                            continue
                        affected_pkg = affected.get("package")
                        if isinstance(affected_pkg, dict) and affected_pkg.get("name") not in (None, name):
                            continue
                        for span in affected.get("ranges") or []:
                            for event in (span or {}).get("events") or []:
                                if isinstance(event, dict) and isinstance(event.get("fixed"), str):
                                    fixed.add(event["fixed"])
                advisories.append(
                    Advisory(
                        file=file,
                        package=name,
                        version=version,
                        ecosystem=ecosystem,
                        advisory=advisory,
                        score=score,
                        severity=_severity_word(score, label),
                        fixed=tuple(sorted(fixed, key=_version_order)),
                    )
                )
    return OsvReading(tuple(advisories), sources)


@dataclass(frozen=True)
class Location:
    """A rule match: rule id, file, first and last line."""

    rule: str
    file: str
    start: int
    end: int
    severity: str | None = None


def read_gitleaks(stdout: bytes, root: Path | None = None) -> list[Location]:
    """Gitleaks' JSON report as rule, file and lines. Nothing else is kept."""
    text = stdout.decode("utf-8", errors="replace").strip()
    data = json.loads(text) if text else []
    found: list[Location] = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        start = item.get("StartLine")
        end = item.get("EndLine")
        if not isinstance(start, int):
            continue
        file = str(item.get("File") or "")
        if root is not None:
            file = _relative(file, root) or ""
        found.append(
            Location(
                rule=str(item.get("RuleID") or "unknown"),
                file=file,
                start=start,
                end=end if isinstance(end, int) and end >= start else start,
            )
        )
    return found


def read_opengrep(stdout: bytes, root: Path, catalogue: "RuleCatalogue") -> list[Location]:
    """Opengrep's JSON results as rule, file, lines and the rule's severity.

    The ``extra`` block (which carries the matched source and an interpolated
    message) is never read. The rule id is taken back to the id written in the
    rule file: Opengrep prefixes a local rule file's ids with the file's path.
    The community Compose rules, which the factory's own replace (Codex R8),
    are dropped here.
    """
    data = json.loads(stdout.decode("utf-8") or "{}")
    results = data.get("results") if isinstance(data, dict) else None
    found: list[Location] = []
    for item in results if isinstance(results, list) else []:
        if not isinstance(item, dict):
            continue
        rule = catalogue.rule_id(str(item.get("check_id") or ""))
        if rule in catalogue.community_compose_rules:
            continue
        file = _relative(str(item.get("path") or ""), root)
        start = (item.get("start") or {}).get("line")
        end = (item.get("end") or {}).get("line")
        if not file or not isinstance(start, int):
            continue
        found.append(
            Location(
                rule=rule,
                file=file,
                start=start,
                end=end if isinstance(end, int) and end >= start else start,
                severity=catalogue.severity(rule),
            )
        )
    return found


_PLACEHOLDER = re.compile(r"\$[A-Z_][A-Z0-9_]*")


def fixed_message(text: str) -> str:
    """A rule's own message as one short sentence, placeholders taken out."""
    plain = " ".join(_PLACEHOLDER.sub("this value", str(text or "")).split())
    sentence_end = re.search(r"(?<=[.!?])\s", plain)
    if sentence_end:
        plain = plain[: sentence_end.start()]
    if len(plain) > MESSAGE_LIMIT:
        plain = plain[: MESSAGE_LIMIT - 1].rstrip() + "…"
    return plain


class RuleCatalogue:
    """The rules the factory ships, read from the rule files themselves.

    Gives each rule's fixed message and severity, which ids are the community
    Compose rules the factory's own replace, and how to read an Opengrep
    result id back to the rule id.
    """

    def __init__(self, rules: Sequence[Mapping[str, Any]]) -> None:
        self._messages: dict[str, str] = {}
        self._severity: dict[str, str] = {}
        community_compose: list[str] = []
        for rule in rules:
            rule_id = rule.get("id")
            if not isinstance(rule_id, str):
                continue
            self._messages.setdefault(rule_id, fixed_message(str(rule.get("message") or "")))
            severity = rule.get("severity")
            if isinstance(severity, str):
                self._severity.setdefault(rule_id, severity.upper())
            metadata = rule.get("metadata") if isinstance(rule.get("metadata"), dict) else {}
            technology = metadata.get("technology") if isinstance(metadata, dict) else None
            if (
                isinstance(technology, list)
                and "docker-compose" in technology
                and metadata.get("source") != "factory"
            ):
                community_compose.append(rule_id)
        self.community_compose_rules: tuple[str, ...] = tuple(sorted(set(community_compose)))

    @classmethod
    def from_files(cls, *paths: Path) -> "RuleCatalogue":
        import yaml

        rules: list[Mapping[str, Any]] = []
        for path in paths:
            text = path.read_text(encoding="utf-8")
            loaded = json.loads(text) if text.lstrip().startswith("{") else yaml.safe_load(text)
            for rule in (loaded or {}).get("rules") or []:
                if isinstance(rule, Mapping):
                    rules.append(rule)
        return cls(rules)

    def rule_id(self, check_id: str) -> str:
        if check_id in self._messages:
            return check_id
        tail = check_id.rsplit(".", 1)[-1]
        return tail if tail in self._messages else (tail or "unknown")

    def message(self, rule_id: str) -> str | None:
        return self._messages.get(rule_id) or None

    def severity(self, rule_id: str) -> str | None:
        return self._severity.get(rule_id)


__all__ = [
    "Advisory",
    "FACTORY_SECURITY_DIR",
    "GITLEAKS_STDIN_TIMEOUT_S",
    "GITLEAKS_TIMEOUT_S",
    "Location",
    "OPENGREP_RULES",
    "OPENGREP_TIMEOUT_S",
    "OSV_TIMEOUT_S",
    "OsvReading",
    "RuleCatalogue",
    "Runner",
    "SCANNER_PATH",
    "ScannerSetup",
    "ToolRun",
    "FACTORY_SEMGREPIGNORE",
    "fixed_message",
    "gitleaks_history_argv",
    "gitleaks_stdin_argv",
    "gitleaks_tree_argv",
    "opengrep_argv",
    "osv_argv",
    "read_gitleaks",
    "read_opengrep",
    "read_osv",
    "run_tool",
    "write_factory_ignore",
]
