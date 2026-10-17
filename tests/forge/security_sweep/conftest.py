"""Stand-ins for the nightly sweep's tests: scanners, model, bus and projects.

Nothing here reaches a network, a bus or a model. The scanners are a fake
runner that answers the way the three real ones do, from what is really in
the exported files:

- the fake Gitleaks flags every line holding an AWS-shaped key
  (``AKIA`` and sixteen capitals or digits), over the tree, the history or
  standard input, and reports file and line only (as ``--redact`` does);
- the fake OSV-Scanner reports a planted advisory list for every
  ``package-lock.json`` in the tree;
- the fake Opengrep reports ``privileged: true`` in a Compose file and a
  shell call in Python, with the check id prefixed the way Opengrep does.

The projects are real git repositories, served to the sweep as a bare
mirror, so the real export from the object store runs.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations
from forge.planning.work_queue_store import WorkQueueStore
from forge.security_sweep.repository import Mirror
from forge.security_sweep.scanners import RuleCatalogue, ScannerSetup, ToolRun
from forge.security_sweep.sweep import SweepDeps

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_RULES = REPO_ROOT / "deploy" / "security" / "compose-rules.yaml"

#: A fake AWS-shaped key, built at run time so no key-shaped literal sits in
#: this file.
def planted_key(tail: str = "QYLPMN5HHHFPZAM2") -> str:
    return "AKIA" + tail


KEY_PATTERN = re.compile(rb"AKIA[0-9A-Z]{16}")

#: The code rules the fake Opengrep knows (id, severity, message).
CODE_RULES = [
    {
        "id": "subprocess-shell-true",
        "languages": ["python"],
        "severity": "ERROR",
        "message": "Found '$FUNC' with 'shell=True'. This is dangerous because $X may run anything.",
        "metadata": {"category": "security"},
        "pattern": "subprocess.call(..., shell=True)",
    },
    {
        "id": "weak-hash",
        "languages": ["python"],
        "severity": "WARNING",
        "message": "A weak hash is used.",
        "metadata": {"category": "security"},
        "pattern": "hashlib.md5(...)",
    },
    {
        "id": "privileged-service",
        "languages": ["yaml"],
        "severity": "WARNING",
        "message": "Service '$SERVICE' is running in privileged mode.",
        "metadata": {"category": "security", "technology": ["docker-compose"]},
        "pattern": "privileged: true",
    },
]

FIXED_TIME = datetime(2026, 10, 19, 2, 30, tzinfo=timezone.utc)  # a Monday


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(repo),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        },
    )
    return done.stdout.strip()


@dataclass
class Project:
    """A project's working repository and the bare mirror the sweep reads."""

    work: Path
    mirror: Path

    def write(self, files: dict[str, str | bytes]) -> None:
        for name, content in files.items():
            path = self.work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                path.write_bytes(content)
            else:
                path.write_text(content)

    def remove(self, *names: str) -> None:
        for name in names:
            (self.work / name).unlink()

    def commit(self, message: str = "change", *, stage: bool = True) -> str:
        if stage:
            git(self.work, "add", "-A")
        git(self.work, "commit", "-qm", message, "--allow-empty")
        shutil.rmtree(self.mirror, ignore_errors=True)
        subprocess.run(
            ["git", "clone", "-q", "--bare", str(self.work), str(self.mirror)],
            check=True,
            capture_output=True,
            env={"PATH": "/usr/bin:/bin", "HOME": str(self.work), "GIT_CONFIG_NOSYSTEM": "1"},
        )
        return git(self.work, "rev-parse", "HEAD")


def make_project(root: Path, name: str, files: dict[str, str | bytes]) -> Project:
    work = root / f"{name}-work"
    work.mkdir(parents=True)
    git(work, "init", "-q", "-b", "main")
    project = Project(work=work, mirror=root / f"{name}.git")
    project.write(files)
    project.commit("first")
    return project


@dataclass
class FakeScanners:
    """A runner that answers like Gitleaks, OSV-Scanner and Opengrep."""

    advisories: list[dict[str, Any]] = field(default_factory=list)
    calls: list[list[str]] = field(default_factory=list)
    fail: dict[str, str] = field(default_factory=dict)
    stdin_inputs: list[bytes] = field(default_factory=list)

    async def __call__(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        timeout: float,
        stdin: bytes | None = None,
        ok_codes: tuple[int, ...] = (0,),
    ) -> ToolRun:
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        tool = Path(argv[0]).name
        mode = argv[1]
        key = f"{tool} {mode}"
        if key in self.fail:
            return ToolRun(False, reason=self.fail[key])
        target = Path(argv[-1])
        if tool == "gitleaks" and mode == "dir":
            return ToolRun(True, json.dumps(self._keys_in_tree(target)).encode())
        if tool == "gitleaks" and mode == "git":
            return ToolRun(True, json.dumps(self._keys_in_history(target)).encode())
        if tool == "gitleaks" and mode == "stdin":
            self.stdin_inputs.append(stdin or b"")
            return ToolRun(True, json.dumps(self._keys_in(stdin or b"", "")).encode())
        if tool == "osv-scanner":
            return ToolRun(True, json.dumps(self._osv(target)).encode())
        if tool == "opengrep":
            return ToolRun(True, json.dumps(self._opengrep(target)).encode())
        return ToolRun(False, reason="unknown tool")

    @staticmethod
    def _keys_in(data: bytes, name: str) -> list[dict[str, Any]]:
        found = []
        for number, line in enumerate(data.split(b"\n"), start=1):
            if KEY_PATTERN.search(line):
                found.append(
                    {"RuleID": "aws-access-token", "File": name, "StartLine": number,
                     "EndLine": number, "Secret": "REDACTED", "Match": "REDACTED"}
                )
        return found

    def _keys_in_tree(self, root: Path) -> list[dict[str, Any]]:
        found = []
        for path in sorted(root.rglob("*")):
            if path.is_file():
                found += self._keys_in(path.read_bytes(), str(path))
        return found

    def _keys_in_history(self, mirror: Path) -> list[dict[str, Any]]:
        log = subprocess.run(
            ["git", "--git-dir", str(mirror), "log", "-p", "--all"],
            capture_output=True,
            env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(mirror)},
        ).stdout
        return [
            {"RuleID": "aws-access-token", "File": "x", "StartLine": 1, "EndLine": 1}
            for line in log.split(b"\n")
            if line.startswith(b"+") and KEY_PATTERN.search(line)
        ]

    def _osv(self, root: Path) -> dict[str, Any]:
        results = []
        for lock in sorted(root.rglob("package-lock.json")):
            results.append(
                {
                    "source": {"path": str(lock), "type": "lockfile"},
                    "packages": [self._package(entry) for entry in self.advisories],
                }
            )
        return {"results": results}

    @staticmethod
    def _package(entry: dict[str, Any]) -> dict[str, Any]:
        return {
            "package": {"name": entry["package"], "version": entry["version"], "ecosystem": "npm"},
            "vulnerabilities": [
                {
                    "id": entry["id"],
                    "database_specific": {"severity": entry.get("label", "HIGH")},
                    "affected": [
                        {
                            "package": {"name": entry["package"], "ecosystem": "npm"},
                            "ranges": [{"events": [{"introduced": "0"}, *[{"fixed": f} for f in entry.get("fixed", [])]]}],
                        }
                    ],
                }
            ],
            "groups": [{"ids": [entry["id"]], "max_severity": str(entry.get("score", ""))}],
        }

    @staticmethod
    def _opengrep(root: Path) -> dict[str, Any]:
        results = []
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            lines = path.read_bytes().split(b"\n")
            for number, line in enumerate(lines, start=1):
                rule = None
                if "compose" in path.name and path.suffix in (".yml", ".yaml"):
                    if b"privileged: true" in line:
                        rule = "factory-compose-privileged"
                        results.append(_og("privileged-service", path, number))
                elif path.suffix == ".py":
                    if b"shell=True" in line:
                        rule = "subprocess-shell-true"
                    elif b"hashlib.md5(" in line:
                        rule = "weak-hash"
                if rule:
                    results.append(_og(rule, path, number))
        return {"results": results, "errors": []}


def _og(rule: str, path: Path, line: int) -> dict[str, Any]:
    return {
        "check_id": f"tmp.somewhere.rules.{rule}",
        "path": str(path),
        "start": {"line": line, "col": 1},
        "end": {"line": line, "col": 10},
        "extra": {"message": "interpolated text from the file", "lines": "the source line"},
    }


@dataclass
class FakeModel:
    """Answers every question; records every prompt as sent."""

    answer: Callable[[str], str] = lambda prompt: '{"real": true}'
    prompts: list[str] = field(default_factory=list)
    down: bool = False

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if self.down:
            raise ConnectionError("the model is down")
        return self.answer(prompt)


@dataclass
class Slack:
    lines: list[str] = field(default_factory=list)

    async def __call__(self, text: str) -> None:
        self.lines.append(text)


@dataclass
class Lab:
    """Everything a sweep run needs, wired to stand-ins."""

    root: Path
    queue: WorkQueueStore
    scanners: FakeScanners
    model: FakeModel
    slack: Slack
    setup: ScannerSetup
    catalogue: RuleCatalogue
    projects: dict[str, Project] = field(default_factory=dict)
    idle: list[bool] = field(default_factory=list)
    now: list[datetime] = field(default_factory=lambda: [FIXED_TIME])
    serious_score: float = 7.0
    draft_repairs: bool = True
    approver: str | None = "rich"
    saves: list[int] = field(default_factory=list)
    fail_save_after_filing: bool = False

    @property
    def folder(self) -> Path:
        return self.root / "state" / "security-sweep"

    def add(self, key: str, files: dict[str, str | bytes]) -> Project:
        name = key.rsplit("/", 1)[-1]
        project = make_project(self.root / "repos", name, files)
        self.projects[key] = project
        return project

    def is_idle(self) -> bool:
        return self.idle.pop(0) if self.idle else True

    def fetch(self, address: str, mirrors: Path) -> Mirror:
        for key, project in self.projects.items():
            if address == f"https://example.invalid/{key}":
                commit = git(project.work, "rev-parse", "HEAD")
                return Mirror(path=project.mirror, branch="main", commit=commit)
        return Mirror(refusal="could not be fetched")

    def save(self, folder: Path, state: Any) -> None:
        from forge.security_sweep.state import save_state

        self.saves.append(1)
        if self.fail_save_after_filing and self.queue.connection.execute(
            "SELECT COUNT(*) FROM work_queue"
        ).fetchone()[0]:
            raise RuntimeError("stopped between filing and saving")
        save_state(folder, state)

    def deps(self, **overrides: Any) -> SweepDeps:
        values: dict[str, Any] = dict(
            projects={key: f"https://example.invalid/{key}" for key in self.projects},
            folder=self.folder,
            queue=self.queue,
            approver=self.approver,
            serious_score=self.serious_score,
            draft_repairs=self.draft_repairs,
            weekly_all_clear=None,
            setup=self.setup,
            publish=self.slack,
            is_idle=self.is_idle,
            asker=self.model,
            clock=lambda: self.now[0],
            runner=self.scanners,
            fetch=self.fetch,
            save=self.save,
            catalogue=self.catalogue,
        )
        values.update(overrides)
        return SweepDeps(**values)

    def rows(self) -> list[Any]:
        return list(self.queue.connection.execute("SELECT * FROM work_queue ORDER BY id"))

    def every_stored_text(self) -> str:
        """Everything the sweep wrote that outlives the run, as one text."""
        texts = []
        for table in ("work_queue", "work_queue_events"):
            for row in self.queue.connection.execute(f"SELECT * FROM {table}"):
                texts.append(json.dumps([str(v) for v in tuple(row)]))
        for name in ("state.json", "history.jsonl"):
            path = self.folder / name
            if path.exists():
                texts.append(path.read_text())
        return "\n".join(texts)


@pytest.fixture
def lab(tmp_path: Path) -> Iterator[Lab]:
    connection = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(connection)
    factory = tmp_path / "factory-security"
    (factory / "gitleaks-ignore").mkdir(parents=True)
    (factory / "osv-scanner.toml").write_text("")
    shutil.copy(COMPOSE_RULES, factory / "compose-rules.yaml")
    rules = tmp_path / "opengrep" / "security-rules.yaml"
    rules.parent.mkdir()
    rules.write_text(json.dumps({"rules": CODE_RULES}))
    setup = ScannerSetup(
        gitleaks="/fake/gitleaks",
        osv_scanner="/fake/osv-scanner",
        opengrep="/fake/opengrep",
        factory_dir=factory,
        opengrep_rules=rules,
    )
    catalogue = RuleCatalogue.from_files(rules, factory / "compose-rules.yaml")
    yield Lab(
        root=tmp_path,
        queue=WorkQueueStore(connection),
        scanners=FakeScanners(),
        model=FakeModel(),
        slack=Slack(),
        setup=setup,
        catalogue=catalogue,
    )
    connection.close()
