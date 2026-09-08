"""The merge-ready gates reader for a repository that has a sandbox
(sandbox first, 2026-09-07, rule 88).

Found by L3a's second coach: the reader reads a repository's declared
toolchain from a checkout on the host and runs the declared test command in
the fix journey's worktree on the host. For a repository whose factory lives
in its own sandbox NEITHER IS ON THE HOST, so the reader could only ever
answer UNKNOWN — which is red — and such a repository's fix journey could
never publish a merge card at all.

Here both acts go through the REAL sidecar service on a real ephemeral
loopback port, against a real git repository and a real journey worktree in
``tmp_path``: the declaration is read out of the clone's ``main``, and the
declared command really runs in the worktree. The other half of every case is
the repository with no sandbox, which must behave exactly as it did before.

Nothing live is touched: no ``sbx``, no docker, no sandbox, no service. The
one stand-in is guardkit's own toolchain loader, which is not installed in
this interpreter (the existing gates-reader tests inject it for the same
reason); a small module is registered and named explicitly, so the reading
itself is the real function.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli import _serve_conductor as conductor
from forge.config.models import ForgeConfig
from forge.deploy_sidecar.service import build_server
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.pipeline.merge_ready_checkpoint import GateStatus

REPO_WITH = "guardkit/api_test"
REPO_WITHOUT = "guardkit/plain"
BUILD_ID = "build-FEAT-G88-20260907210000"
TEST_COMMAND = "pwd"
FAKE_LOADER_MODULE = "tests_fake_toolchain_declaration"

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "PATH": "/usr/bin:/bin:/usr/local/bin",
    "HOME": "/nonexistent",
}


class _Declaration:
    """Stands in for guardkit's ``ToolchainDeclaration`` (duck-typed)."""

    def __init__(self, test: str | None, test_timeout: int = 120) -> None:
        self.test = test
        self.test_timeout = test_timeout


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def fake_guardkit_loader(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """A module shaped like guardkit's toolchain declaration loader.

    It reads the same file guardkit's own loader reads —
    ``<root>/.guardkit/config.yaml`` — so a test that asserts the declaration
    came out of the clone is asserting about a real file that really arrived.
    """
    module = ModuleType(FAKE_LOADER_MODULE)

    def load_toolchain_declaration(root: Any) -> Any:
        path = Path(root) / ".guardkit" / "config.yaml"
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8")
        command: str | None = None
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("test:"):
                command = stripped.removeprefix("test:").strip().strip("\"'")
        return _Declaration(command) if command else None

    module.load_toolchain_declaration = load_toolchain_declaration  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, FAKE_LOADER_MODULE, module)
    return module


def _config_yaml() -> str:
    return "toolchain:\n" f'  test: "{TEST_COMMAND}"\n' "  test_timeout: 120\n"


def _scratch_repo(path: Path, *, with_toolchain: bool) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    (path / "README").write_text("scratch\n", encoding="utf-8")
    if with_toolchain:
        (path / ".guardkit").mkdir()
        (path / ".guardkit" / "config.yaml").write_text(
            _config_yaml(), encoding="utf-8"
        )
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "init")
    return path.resolve()


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    return _scratch_repo(tmp_path / "api_test", with_toolchain=True)


@pytest.fixture
def plain_checkout(tmp_path: Path) -> Path:
    return _scratch_repo(tmp_path / "plain", with_toolchain=True)


@pytest.fixture
def worktree(clone: Path) -> Path:
    """The fix journey's own tree, where the declared command must run."""
    path = clone / ".forge" / "worktrees" / BUILD_ID
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(clone, "worktree", "add", "-b", f"fix/{BUILD_ID}", str(path), "main")
    return path.resolve()


@pytest.fixture
def sidecar(clone: Path, plain_checkout: Path, monkeypatch: pytest.MonkeyPatch):
    """The REAL sidecar, standing where the real one stands: inside the
    repository's sandbox.

    It says so the way the real one does — the in-sandbox bootstrap sets
    ``FORGE_SIDECAR_IN_SANDBOX`` — because only a sidecar inside a sandbox
    will run a repository's own declared test command (L3b's coach,
    2026-09-08). The host sidecar's refusal of the same request is proved
    beside the route itself, in
    ``tests/forge/deploy_sidecar/test_sidecar_run_route_gate_and_suite.py``.
    """
    from forge.deploy_sidecar.service import SIDECAR_IN_SANDBOX_ENV

    monkeypatch.setenv(SIDECAR_IN_SANDBOX_ENV, "1")
    holder: dict[str, ForgeConfig] = {}
    srv = build_server(port=0, config_loader=lambda: holder["config"])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host, port = srv.server_address[:2]
    assert host == "127.0.0.1"
    url = f"http://{host}:{port}"
    holder["config"] = ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": [str(clone.parent)]}},
            "planning": {
                "target_repo_paths": {
                    REPO_WITH: str(clone),
                    REPO_WITHOUT: str(plain_checkout),
                },
                "sandboxes": {
                    REPO_WITH: {
                        "name": "api-test-factory",
                        "sidecar_url": url,
                        "runner_url": "http://127.0.0.1:8924",
                    }
                },
            },
        }
    )
    try:
        yield SimpleNamespace(url=url, config=holder["config"])
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def entry(sidecar: Any) -> Any:
    return sidecar.config.planning.sandboxes[REPO_WITH]


@pytest.fixture
def sidecar_reads_the_real_declaration(
    monkeypatch: pytest.MonkeyPatch, fake_guardkit_loader: ModuleType
) -> None:
    """Point the sidecar's own declaration reader at the stand-in module.

    The service resolves guardkit's loader by import name; naming the module
    here is the only stand-in, and the reading, the checking and the running
    are all the real thing.
    """
    from forge.deploy_sidecar import service

    real = service.declared_test_command
    monkeypatch.setattr(
        service,
        "declared_test_command",
        lambda repo_path: real(
            repo_path, module_candidates=(FAKE_LOADER_MODULE,)
        ),
    )


@pytest.fixture
def pool(tmp_path: Path, clone: Path, worktree: Path) -> SqliteLifecyclePersistence:
    cx: sqlite3.Connection = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    return SqliteLifecyclePersistence(connection=cx, db_path=tmp_path / "forge.db")


def _row(pool: SqliteLifecyclePersistence, repo: str, worktree: Path) -> None:
    pool.connection.execute(
        "INSERT INTO builds (build_id, feature_id, repo, branch, "
        "feature_yaml_path, status, triggered_by, correlation_id, queued_at, "
        "worktree_path, mode, task_id) VALUES (?, 'FEAT-G88', ?, ?, "
        "'f.yaml', 'RUNNING', 'cli', 'corr-g88', '2026-09-07T21:00:00Z', ?, "
        "'mode-c', 'TASK-G88')",
        (BUILD_ID, repo, f"fix/{BUILD_ID}", str(worktree)),
    )
    pool.connection.commit()


# ---------------------------------------------------------------------------
# Reading the declaration out of the clone
# ---------------------------------------------------------------------------


class TestTheToolchainIsReadFromTheCloneThroughTheSidecar:
    def test_the_declaration_comes_back_over_the_real_sidecar(
        self,
        sidecar: Any,
        entry: Any,
        clone: Path,
        fake_guardkit_loader: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen: list[Path] = []
        real = conductor.load_declared_toolchain

        def _loader(root: Any, **kwargs: Any) -> Any:
            seen.append(Path(root))
            return real(root, module_candidates=(FAKE_LOADER_MODULE,))

        monkeypatch.setattr(conductor, "load_declared_toolchain", _loader)

        declaration = conductor.load_declared_toolchain_from_sandbox(
            clone, sandbox=entry, repo=REPO_WITH
        )

        assert declaration is not None
        assert declaration.test == TEST_COMMAND
        # It was read into a tree of its own, not out of any checkout here.
        assert len(seen) == 1
        assert seen[0] != clone
        assert (
            (seen[0] / ".guardkit" / "config.yaml").exists() is False
        ), "the temporary tree is cleaned up after the read"

    def test_the_canonical_branch_is_what_is_read_not_the_journeys(
        self, sidecar: Any, entry: Any, clone: Path, worktree: Path,
        fake_guardkit_loader: ModuleType, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The journey rewrites the declaration to something that always passes
        # — the very thing the canonical-not-worktree law exists to stop.
        (worktree / ".guardkit" / "config.yaml").write_text(
            'toolchain:\n  test: "true"\n', encoding="utf-8"
        )
        _git(worktree, "add", "-A")
        _git(worktree, "commit", "-m", "green myself")

        real = conductor.load_declared_toolchain
        monkeypatch.setattr(
            conductor,
            "load_declared_toolchain",
            lambda root, **kw: real(root, module_candidates=(FAKE_LOADER_MODULE,)),
        )

        declaration = conductor.load_declared_toolchain_from_sandbox(
            clone, sandbox=entry, repo=REPO_WITH
        )

        assert declaration.test == TEST_COMMAND

    def test_an_unreachable_sidecar_answers_none_and_says_so(
        self, clone: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        dead = SimpleNamespace(name="gone", sidecar_url="http://127.0.0.1:9")

        with caplog.at_level("ERROR"):
            declaration = conductor.load_declared_toolchain_from_sandbox(
                clone, sandbox=dead, repo=REPO_WITH
            )

        assert declaration is None
        assert "could not be reached" in caplog.text


# ---------------------------------------------------------------------------
# Running the declared command in the journey worktree, inside the sandbox
# ---------------------------------------------------------------------------


class TestTheDeclaredCommandRunsInTheSandbox:
    def test_it_really_runs_in_the_journey_worktree(
        self,
        sidecar: Any,
        entry: Any,
        worktree: Path,
        sidecar_reads_the_real_declaration: None,
    ) -> None:
        exit_code, detail = conductor.run_declared_command_in_sandbox(
            command=TEST_COMMAND,
            cwd=worktree,
            timeout_seconds=120,
            sandbox=entry,
            repo=REPO_WITH,
        )

        assert exit_code == 0
        assert str(worktree) in detail
        assert "api-test-factory" in detail
        # The command's own output says where it ran.
        assert detail.strip().endswith(str(worktree))

    def test_a_command_the_repository_does_not_declare_is_refused(
        self,
        sidecar: Any,
        entry: Any,
        worktree: Path,
        sidecar_reads_the_real_declaration: None,
    ) -> None:
        exit_code, detail = conductor.run_declared_command_in_sandbox(
            command="rm -rf /",
            cwd=worktree,
            timeout_seconds=30,
            sandbox=entry,
            repo=REPO_WITH,
        )

        # Not a fail and not a pass: it could not be run, which is UNKNOWN.
        assert exit_code is None
        assert "refused" in detail
        assert "the one this repository declares" in detail

    def test_a_working_directory_that_is_not_a_journey_tree_is_refused(
        self,
        sidecar: Any,
        entry: Any,
        clone: Path,
        sidecar_reads_the_real_declaration: None,
    ) -> None:
        exit_code, detail = conductor.run_declared_command_in_sandbox(
            command=TEST_COMMAND,
            cwd=clone,
            timeout_seconds=30,
            sandbox=entry,
            repo=REPO_WITH,
        )

        assert exit_code is None
        assert "not a journey worktree" in detail

    def test_an_unreachable_sidecar_is_unknown_not_a_verdict(
        self, worktree: Path
    ) -> None:
        dead = SimpleNamespace(name="gone", sidecar_url="http://127.0.0.1:9")

        exit_code, detail = conductor.run_declared_command_in_sandbox(
            command=TEST_COMMAND,
            cwd=worktree,
            timeout_seconds=5,
            sandbox=dead,
            repo=REPO_WITH,
        )

        assert exit_code is None
        assert "could not be sent" in detail


# ---------------------------------------------------------------------------
# The reader itself: which way each repository goes
# ---------------------------------------------------------------------------


class TestTheReaderChoosesPerRepository:
    def test_a_sandbox_repository_is_read_and_run_inside_its_sandbox(
        self,
        pool: SqliteLifecyclePersistence,
        sidecar: Any,
        worktree: Path,
        fake_guardkit_loader: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        sidecar_reads_the_real_declaration: None,
    ) -> None:
        _row(pool, REPO_WITH, worktree)
        real = conductor.load_declared_toolchain
        monkeypatch.setattr(
            conductor,
            "load_declared_toolchain",
            lambda root, **kw: real(root, module_candidates=(FAKE_LOADER_MODULE,)),
        )
        on_the_host: list[str] = []

        reader = conductor.make_gates_green_reader(
            pool=pool,
            config=sidecar.config,
            # If either of today's in-container seams were used for this
            # repository, these would record it and the test would say so.
            declaration_loader=lambda root: on_the_host.append("declaration"),
            command_runner=lambda **kw: on_the_host.append("command") or (1, "host"),
            stamps_leg=lambda **kw: SimpleNamespace(
                status="not-enforced", detail="", blocks_card=False, attended=()
            ),
        )

        report = reader(build_id=BUILD_ID, branch=f"fix/{BUILD_ID}")

        assert report.status is GateStatus.GREEN, report.detail
        assert on_the_host == []
        assert "api-test-factory" in report.detail

    def test_a_repository_without_a_sandbox_uses_todays_seams_untouched(
        self,
        pool: SqliteLifecyclePersistence,
        sidecar: Any,
        plain_checkout: Path,
        tmp_path: Path,
    ) -> None:
        plain_worktree = tmp_path / "plain-wt"
        plain_worktree.mkdir()
        _row(pool, REPO_WITHOUT, plain_worktree)
        calls: list[dict[str, Any]] = []

        def _run(*, command: str, cwd: Any, timeout_seconds: int) -> Any:
            calls.append({"command": command, "cwd": str(cwd)})
            return 0, f"`{command}` exited 0 in {cwd}"

        reader = conductor.make_gates_green_reader(
            pool=pool,
            config=sidecar.config,
            declaration_loader=lambda _root: _Declaration("npm test"),
            command_runner=_run,
            stamps_leg=lambda **kw: SimpleNamespace(
                status="not-enforced", detail="", blocks_card=False, attended=()
            ),
            # If the sandbox path were taken for this repository these would
            # fire; they are never reached.
            sandbox_declaration_loader=lambda *a, **k: pytest.fail(
                "a repository with no sandbox must not be read through one"
            ),
            sandbox_command_runner=lambda *a, **k: pytest.fail(
                "a repository with no sandbox must not be run through one"
            ),
        )

        report = reader(build_id=BUILD_ID, branch=f"fix/{BUILD_ID}")

        assert report.status is GateStatus.GREEN
        assert calls == [{"command": "npm test", "cwd": str(plain_worktree)}]

    def test_a_red_suite_in_the_sandbox_is_red_here_too(
        self,
        pool: SqliteLifecyclePersistence,
        sidecar: Any,
        worktree: Path,
    ) -> None:
        _row(pool, REPO_WITH, worktree)

        reader = conductor.make_gates_green_reader(
            pool=pool,
            config=sidecar.config,
            sandbox_declaration_loader=lambda root, **kw: _Declaration("the suite"),
            sandbox_command_runner=lambda **kw: (1, "`the suite` exited 1"),
        )

        report = reader(build_id=BUILD_ID, branch=f"fix/{BUILD_ID}")

        assert report.status is GateStatus.RED
        assert report.failed_gates == ("declared toolchain test",)

    def test_a_sandbox_repositorys_worktree_is_not_looked_for_on_the_host(
        self, pool: SqliteLifecyclePersistence, sidecar: Any, clone: Path
    ) -> None:
        """The tree is inside the sandbox; the reader must not call it missing."""
        absent = clone / ".forge" / "worktrees" / "build-not-on-this-side"
        _row(pool, REPO_WITH, absent)
        asked: list[str] = []

        reader = conductor.make_gates_green_reader(
            pool=pool,
            config=sidecar.config,
            sandbox_declaration_loader=lambda root, **kw: _Declaration("the suite"),
            sandbox_command_runner=lambda **kw: (
                asked.append(str(kw["cwd"])) or (0, "green")
            ),
            stamps_leg=lambda **kw: SimpleNamespace(
                status="not-enforced", detail="", blocks_card=False, attended=()
            ),
        )

        report = reader(build_id=BUILD_ID, branch=f"fix/{BUILD_ID}")

        assert report.status is GateStatus.GREEN, report.detail
        assert asked == [str(absent)]


# ---------------------------------------------------------------------------
# Step 5: the routing law's own evidence, read where it lives
# ---------------------------------------------------------------------------
#
# L3b's coach, 2026-09-08: this lane routed steps 3 and 4 into the sandbox and
# left step 5 — the routing law's stamped-verifier check — reading the host,
# where a sandbox repository's feature file and gate receipts are not. The
# check then found nothing, said the feature carried no stamps, had no effect,
# and a GREEN merge card went out with the law silently unenforced. These
# tests are that defect and its fix, driven through the REAL sidecar route.


def _stamp_the_feature(clone: Path, *, verifier: str, title: str) -> None:
    """Put a feature YAML carrying one stamped scenario on the clone's main.

    And then REMOVE IT FROM THE WORKING TREE on this side. That models the
    real thing honestly: for a repository whose factory lives in its sandbox,
    the plan of record is in the clone in there, and this side has no copy of
    it to read. A fixture that leaves the file where both sides can see it is
    exactly how the fail-open hid — the coach's finding, 2026-09-08.
    """
    features = clone / ".guardkit" / "features"
    features.mkdir(parents=True, exist_ok=True)
    path = features / "FEAT-G88.yaml"
    path.write_text(
        "id: FEAT-G88\nscenarios:\n" f'  "{title}": {verifier}\n',
        encoding="utf-8",
    )
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "the plan of record")
    path.unlink()


def _write_envelope(
    worktree: Path, *, gate_id: str, exit_code: int, verdict: str = "pass"
) -> Path:
    import json
    from datetime import datetime, timedelta, timezone

    history = worktree / "qa" / "gates" / "history"
    history.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc) + timedelta(minutes=5)
    path = history / "run-1.json"
    path.write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "verdict": verdict,
                "started": started.isoformat(),
                "feature_id": "FEAT-G88",
                "gates": [{"gate_id": gate_id, "exit_code": exit_code}],
            }
        ),
        encoding="utf-8",
    )
    return path


class TestTheRoutingLawIsReadInsideTheSandbox:
    def test_a_stamped_scenario_with_no_evidence_publishes_no_card(
        self,
        pool: SqliteLifecyclePersistence,
        sidecar: Any,
        clone: Path,
        worktree: Path,
    ) -> None:
        """The regression: this answered GREEN before the repair."""
        from forge.pipeline.routing_stamps import make_stamps_leg

        _stamp_the_feature(clone, verifier="hurl", title="a caller sees 201")
        _row(pool, REPO_WITH, worktree)

        # What the leg that reads THIS SIDE says about the same journey: the
        # feature file is not here, so it finds no stamps, has no effect, and
        # would let a green suite publish a card with the routing law never
        # applied. That is the defect this test pins.
        on_the_host = make_stamps_leg()(
            feature_id="FEAT-G88",
            repo_root=clone,
            worktree=worktree,
            branch=f"fix/{BUILD_ID}",
            toolchain_green=True,
        )
        assert on_the_host.blocks_card is False
        assert str(on_the_host.status) == "not-enforced"

        reader = conductor.make_gates_green_reader(
            pool=pool,
            config=sidecar.config,
            sandbox_declaration_loader=lambda root, **kw: _Declaration("true"),
            sandbox_command_runner=lambda **kw: (0, "the suite is green"),
        )

        report = reader(build_id=BUILD_ID, branch=f"fix/{BUILD_ID}")

        assert report.status is GateStatus.UNKNOWN, report.detail
        assert report.failed_gates == ("routing law: hurl (scenario 'a caller sees 201')",)
        assert "a caller sees 201" in report.detail

    def test_a_stamped_scenario_whose_gate_really_ran_green_is_a_card(
        self,
        pool: SqliteLifecyclePersistence,
        sidecar: Any,
        clone: Path,
        worktree: Path,
    ) -> None:
        _stamp_the_feature(clone, verifier="hurl", title="a caller sees 201")
        _write_envelope(worktree, gate_id="hurl-twins", exit_code=0)
        _row(pool, REPO_WITH, worktree)

        reader = conductor.make_gates_green_reader(
            pool=pool,
            config=sidecar.config,
            sandbox_declaration_loader=lambda root, **kw: _Declaration("true"),
            sandbox_command_runner=lambda **kw: (0, "the suite is green"),
        )

        report = reader(build_id=BUILD_ID, branch=f"fix/{BUILD_ID}")

        assert report.status is GateStatus.GREEN, report.detail
        assert "all 1 stamped scenario(s)" in report.detail

    def test_a_toolchain_stamp_rides_the_suite_that_ran_in_the_sandbox(
        self,
        pool: SqliteLifecyclePersistence,
        sidecar: Any,
        clone: Path,
        worktree: Path,
    ) -> None:
        _stamp_the_feature(clone, verifier="toolchain", title="the suite proves it")
        _row(pool, REPO_WITH, worktree)

        reader = conductor.make_gates_green_reader(
            pool=pool,
            config=sidecar.config,
            sandbox_declaration_loader=lambda root, **kw: _Declaration("true"),
            sandbox_command_runner=lambda **kw: (0, "the suite is green"),
        )

        report = reader(build_id=BUILD_ID, branch=f"fix/{BUILD_ID}")

        assert report.status is GateStatus.GREEN, report.detail
        assert "toolchain: 1" in report.detail

    def test_the_stamps_are_read_from_main_not_from_the_journeys_tree(
        self,
        pool: SqliteLifecyclePersistence,
        sidecar: Any,
        clone: Path,
        worktree: Path,
    ) -> None:
        """A journey that deletes its own stamps cannot green itself."""
        _stamp_the_feature(clone, verifier="hurl", title="a caller sees 201")
        features = worktree / ".guardkit" / "features"
        features.mkdir(parents=True, exist_ok=True)
        (features / "FEAT-G88.yaml").write_text("id: FEAT-G88\n", encoding="utf-8")
        _git(worktree, "add", "-A")
        _git(worktree, "commit", "-m", "no stamps here")
        _row(pool, REPO_WITH, worktree)

        reader = conductor.make_gates_green_reader(
            pool=pool,
            config=sidecar.config,
            sandbox_declaration_loader=lambda root, **kw: _Declaration("true"),
            sandbox_command_runner=lambda **kw: (0, "the suite is green"),
        )

        report = reader(build_id=BUILD_ID, branch=f"fix/{BUILD_ID}")

        assert report.status is GateStatus.UNKNOWN, report.detail

    def test_an_unreadable_answer_is_unknown_never_a_card(
        self, pool: SqliteLifecyclePersistence, sidecar: Any, worktree: Path
    ) -> None:
        dead = SimpleNamespace(name="gone", sidecar_url="http://127.0.0.1:9")
        _row(pool, REPO_WITH, worktree)

        verdict = conductor.read_stamps_in_sandbox(
            feature_id="FEAT-G88",
            repo_root=worktree.parent.parent.parent,
            worktree=worktree,
            branch=f"fix/{BUILD_ID}",
            toolchain_green=True,
            sandbox=dead,
            repo=REPO_WITH,
        )

        assert verdict.blocks_card is True
        assert "could not be reached" in verdict.detail

    def test_a_repository_without_a_sandbox_keeps_the_in_container_leg(
        self,
        pool: SqliteLifecyclePersistence,
        sidecar: Any,
        plain_checkout: Path,
        tmp_path: Path,
    ) -> None:
        plain_worktree = tmp_path / "plain-wt-stamps"
        plain_worktree.mkdir()
        _row(pool, REPO_WITHOUT, plain_worktree)
        asked: list[str] = []

        reader = conductor.make_gates_green_reader(
            pool=pool,
            config=sidecar.config,
            declaration_loader=lambda _root: _Declaration("npm test"),
            command_runner=lambda **kw: (0, "green"),
            stamps_leg=lambda **kw: (
                asked.append("in-container")
                or SimpleNamespace(
                    status="not-enforced", detail="", blocks_card=False, attended=()
                )
            ),
            sandbox_stamps_leg=lambda **kw: pytest.fail(
                "a repository with no sandbox must not read its stamps through one"
            ),
        )

        report = reader(build_id=BUILD_ID, branch=f"fix/{BUILD_ID}")

        assert report.status is GateStatus.GREEN
        assert asked == ["in-container"]


# ---------------------------------------------------------------------------
# The checks left to the merge press: deferred, not missing (ruled 2026-09-08)
# ---------------------------------------------------------------------------
#
# FEAT-39F6's scenarios are stamped ``probe:process``, a home whose evidence
# only the live gate writes — and a fix journey runs no live gate before this
# checkpoint. So the leg above could only ever say ABSENT and no fix journey on
# such a repository could reach its merge card. Since protect-main the merge
# press stands the candidate up inside the sandbox and runs the repository's
# own live gate on it BEFORE anything lands, so the checkpoint leaves those
# checks to the press — and ONLY where the press really runs a gate: the
# settings, the candidate block and the live gate, all three.


def _profile(repo_root: Path, *, candidate: bool, live_gate: bool = True) -> Path:
    """Write the repository's real ``deploy/profile.yaml`` — the same file,
    read by the same loader, as the deploy stage's own.

    ``candidate`` stands the build up before the merge; ``live_gate`` is the
    gate the press then runs on it. api_test has both, which is why journey
    one's checks can be left to the press at all.
    """
    path = repo_root / "deploy" / "profile.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "env_id: local\ncompose:\n  file: deploy/docker-compose.yml\n"
    if candidate:
        text += 'candidate:\n  env:\n    CANDIDATE_PORT: "8902"\n'
    if live_gate:
        text += "live_gate:\n  driver: [python3, qa/gates/local_live_gate.py]\n"
    path.write_text(text, encoding="utf-8")
    return path


def _deploys_through_a_sidecar(
    config: ForgeConfig, *, run_live_gate: bool = True
) -> ForgeConfig:
    """The same settings, with the deploy stage's scripts on a sidecar — which
    is what forge-prod runs with (it has no docker of its own). The live-gate
    switch is the settings half of "the press really does check the
    candidate"; with it off a candidate is stood up and passed on its health
    checks alone."""
    return config.model_copy(
        update={
            "deploy": config.deploy.model_copy(
                update={
                    "execution_surface": "sidecar",
                    "run_live_gate": run_live_gate,
                }
            )
        }
    )


class TestTheStampsLeftToTheMergePress:
    def test_a_gate_home_stamp_is_deferred_and_the_record_says_how_many(
        self,
        pool: SqliteLifecyclePersistence,
        sidecar: Any,
        clone: Path,
        worktree: Path,
    ) -> None:
        _stamp_the_feature(clone, verifier="probe:process", title="a caller sees 201")
        _profile(clone, candidate=True)
        _row(pool, REPO_WITH, worktree)

        reader = conductor.make_gates_green_reader(
            pool=pool,
            config=_deploys_through_a_sidecar(sidecar.config),
            sandbox_declaration_loader=lambda root, **kw: _Declaration("true"),
            sandbox_command_runner=lambda **kw: (0, "the suite is green"),
        )

        report = reader(build_id=BUILD_ID, branch=f"fix/{BUILD_ID}")

        assert report.status is GateStatus.GREEN, report.detail
        assert report.deferred_detail == (
            "1 stamped check (probe:process) has no live-gate evidence yet: "
            "the merge press stands the candidate up in the sandbox and runs "
            "this repository's live gate on it before anything lands."
        )
        assert "a caller sees 201" in report.detail

    def test_the_same_repository_without_a_candidate_check_is_unchanged(
        self,
        pool: SqliteLifecyclePersistence,
        sidecar: Any,
        clone: Path,
        worktree: Path,
    ) -> None:
        """No candidate block: nothing checks the stamp before the merge."""
        _stamp_the_feature(clone, verifier="probe:process", title="a caller sees 201")
        _profile(clone, candidate=False)
        _row(pool, REPO_WITH, worktree)

        reader = conductor.make_gates_green_reader(
            pool=pool,
            config=_deploys_through_a_sidecar(sidecar.config),
            sandbox_declaration_loader=lambda root, **kw: _Declaration("true"),
            sandbox_command_runner=lambda **kw: (0, "the suite is green"),
        )

        report = reader(build_id=BUILD_ID, branch=f"fix/{BUILD_ID}")

        assert report.status is GateStatus.UNKNOWN, report.detail
        assert report.deferred_detail == ""

    def test_no_deploy_profile_at_all_is_unchanged(
        self,
        pool: SqliteLifecyclePersistence,
        sidecar: Any,
        clone: Path,
        worktree: Path,
    ) -> None:
        _stamp_the_feature(clone, verifier="probe:process", title="a caller sees 201")
        _row(pool, REPO_WITH, worktree)

        reader = conductor.make_gates_green_reader(
            pool=pool,
            config=_deploys_through_a_sidecar(sidecar.config),
            sandbox_declaration_loader=lambda root, **kw: _Declaration("true"),
            sandbox_command_runner=lambda **kw: (0, "the suite is green"),
        )

        report = reader(build_id=BUILD_ID, branch=f"fix/{BUILD_ID}")

        assert report.status is GateStatus.UNKNOWN, report.detail

    def test_a_deploy_that_does_not_run_on_a_sidecar_is_unchanged(
        self,
        pool: SqliteLifecyclePersistence,
        sidecar: Any,
        clone: Path,
        worktree: Path,
    ) -> None:
        """The candidate block alone is not the check; the surface matters."""
        _stamp_the_feature(clone, verifier="probe:process", title="a caller sees 201")
        _profile(clone, candidate=True)
        _row(pool, REPO_WITH, worktree)

        reader = conductor.make_gates_green_reader(
            pool=pool,
            config=sidecar.config,  # execution_surface stays 'local'
            sandbox_declaration_loader=lambda root, **kw: _Declaration("true"),
            sandbox_command_runner=lambda **kw: (0, "the suite is green"),
        )

        report = reader(build_id=BUILD_ID, branch=f"fix/{BUILD_ID}")

        assert report.status is GateStatus.UNKNOWN, report.detail

    def test_a_repository_with_no_sandbox_is_asked_exactly_what_it_always_was(
        self,
        pool: SqliteLifecyclePersistence,
        sidecar: Any,
        plain_checkout: Path,
        tmp_path: Path,
    ) -> None:
        """Even with a candidate block: no sandbox, no deferral, same call."""
        _profile(plain_checkout, candidate=True)
        plain_worktree = tmp_path / "plain-wt-defer"
        plain_worktree.mkdir()
        _row(pool, REPO_WITHOUT, plain_worktree)
        seen: list[dict[str, Any]] = []

        reader = conductor.make_gates_green_reader(
            pool=pool,
            config=_deploys_through_a_sidecar(sidecar.config),
            declaration_loader=lambda _root: _Declaration("npm test"),
            command_runner=lambda **kw: (0, "green"),
            stamps_leg=lambda **kw: (
                seen.append(kw)
                or SimpleNamespace(
                    status="not-enforced", detail="", blocks_card=False, attended=()
                )
            ),
        )

        report = reader(build_id=BUILD_ID, branch=f"fix/{BUILD_ID}")

        assert report.status is GateStatus.GREEN
        assert seen == [
            {
                "feature_id": "FEAT-G88",
                "repo_root": str(plain_checkout),
                "worktree": str(plain_worktree),
                "branch": f"fix/{BUILD_ID}",
                "toolchain_green": True,
            }
        ]


class TestWhoseMergeChecksACandidateFirst:
    def test_a_sidecar_deploy_with_a_candidate_block_says_yes(
        self, sidecar: Any, clone: Path
    ) -> None:
        _profile(clone, candidate=True)

        assert (
            conductor.candidate_is_checked_before_the_merge(
                _deploys_through_a_sidecar(sidecar.config), clone
            )
            is True
        )

    def test_a_profile_that_cannot_be_read_says_no(
        self, sidecar: Any, tmp_path: Path
    ) -> None:
        nowhere = tmp_path / "no-such-repository"

        assert (
            conductor.candidate_is_checked_before_the_merge(
                _deploys_through_a_sidecar(sidecar.config), nowhere
            )
            is False
        )

    def test_a_candidate_with_no_live_gate_in_the_profile_says_no(
        self, sidecar: Any, clone: Path
    ) -> None:
        """A candidate the press never gates is not a check to defer to.

        With a candidate block and no ``live_gate`` block the deploy stage
        stands the build up, takes its health checks as the whole check and
        writes ``verdict: pass``, and the merge proceeds — so a stamped check
        left to it would be run by nobody.
        """
        _profile(clone, candidate=True, live_gate=False)

        assert (
            conductor.candidate_is_checked_before_the_merge(
                _deploys_through_a_sidecar(sidecar.config), clone
            )
            is False
        )

    def test_the_live_gate_switched_off_in_the_settings_says_no(
        self, sidecar: Any, clone: Path
    ) -> None:
        """Same hole, from the settings side: ``deploy.run_live_gate`` off."""
        _profile(clone, candidate=True, live_gate=True)

        assert (
            conductor.candidate_is_checked_before_the_merge(
                _deploys_through_a_sidecar(sidecar.config, run_live_gate=False),
                clone,
            )
            is False
        )

    def test_a_repository_whose_press_never_gates_is_not_deferred_end_to_end(
        self,
        pool: SqliteLifecyclePersistence,
        sidecar: Any,
        clone: Path,
        worktree: Path,
    ) -> None:
        """The whole reader, not just the helper: no live gate, no deferral."""
        _stamp_the_feature(clone, verifier="probe:process", title="a caller sees 201")
        _profile(clone, candidate=True, live_gate=False)
        _row(pool, REPO_WITH, worktree)

        reader = conductor.make_gates_green_reader(
            pool=pool,
            config=_deploys_through_a_sidecar(sidecar.config),
            sandbox_declaration_loader=lambda root, **kw: _Declaration("true"),
            sandbox_command_runner=lambda **kw: (0, "the suite is green"),
        )

        report = reader(build_id=BUILD_ID, branch=f"fix/{BUILD_ID}")

        assert report.status is GateStatus.UNKNOWN, report.detail
        assert report.deferred_detail == ""
