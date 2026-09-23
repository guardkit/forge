"""The coordinator's read-only answer, and the helper asking it for real.

WHY THIS FILE EXISTS (27 September 2026, the seventh review). The helper that
runs a project's deploy scripts binds the commit it reads that project's
declarations at to the coordinator's own record: a commit on a request is
honoured only when the coordinator confirms it is the one recorded for that
build, and with nobody to ask, every request naming a commit is refused. The
coordinator stamps that commit on every deploy request for a build that came
through planning, so that refusal is the whole factory not deploying — and the
address the helper asks, ``FORGE_TARGET_OWNER_URL``, named something nothing in
the estate served. This file drives the thing that now serves it.

NOTHING LIVE IS TOUCHED. The record is a throwaway database in a temporary
directory, made by Forge's own migration code and written by Forge's own
writers. The answer service and the helper are both children of this process
on 127.0.0.1, on ports the kernel picks. No sandbox, coordinator, live service
or live ledger is anywhere near it, and no image is built.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from nats_core.events import BuildQueuedPayload

from forge.adapters.sqlite import connect as sqlite_connect
from forge.config.models import ForgeConfig
from forge.deploy_sidecar.service import (
    COORDINATOR_OWNER_ENV,
    build_server,
    coordinator_owner_asker,
    coordinator_recorded_build,
)
from forge.lifecycle import migrations as lifecycle_migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.pipeline.deployment_lock import DeploymentLockStore
from forge.planning.run_store import SqlitePlanningRunStore
from forge.record_answer.service import (
    ANSWER_ROUTE,
    HEALTH_ROUTE,
    TheCoordinatorsRecord,
    TheRecordIsUnreadable,
    serve,
)

REPO = "org/widget-shop"
THE_SCRIPT = "deploy/say-what-i-was-given.sh"
DECLARED_SETTING = "SOME_TOOL_CACHE"
CID = "corr-answer-0001"
TARGET = "org/widget-shop::live"


# ---------------------------------------------------------------------------
# A throwaway record, made and written the way the coordinator makes and
# writes its own
# ---------------------------------------------------------------------------


@pytest.fixture()
def record(tmp_path: Path) -> Iterator[SqliteLifecyclePersistence]:
    db_path = tmp_path / "record" / "forge.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    cx = sqlite_connect.connect_writer(db_path)
    lifecycle_migrations.apply_at_boot(cx)
    try:
        yield SqliteLifecyclePersistence(connection=cx, db_path=db_path)
    finally:
        cx.close()


def _a_build_starting_from(
    record: SqliteLifecyclePersistence, commit: str, *, correlation_id: str = CID
) -> str:
    """Queue a planning run at ``commit``, then the build it becomes."""
    record.connection.row_factory = sqlite3.Row
    store = SqlitePlanningRunStore(record.connection)
    store.record_queued(
        correlation_id=correlation_id,
        originating_user="U1",
        expected_approver="U1",
        request_text="a sentence",
        triggered_by="cli",
        target_repo=REPO,
    )
    store.record_start_point(
        correlation_id, start_commit=commit, target_branch="main"
    )
    now = datetime.now(UTC)
    return record.record_pending_build(
        BuildQueuedPayload(
            feature_id="FEAT-ANSW",
            repo=REPO,
            feature_yaml_path=".guardkit/features/FEAT-ANSW.yaml",
            triggered_by="forge-internal",
            correlation_id=correlation_id,
            requested_at=now,
            queued_at=now,
        )
    )


def _a_build_with_nothing_recorded(record: SqliteLifecyclePersistence) -> str:
    now = datetime.now(UTC)
    return record.record_pending_build(
        BuildQueuedPayload(
            feature_id="FEAT-HAND",
            repo=REPO,
            feature_yaml_path=".guardkit/features/FEAT-HAND.yaml",
            triggered_by="forge-internal",
            correlation_id="corr-nothing-recorded",
            requested_at=now,
            queued_at=now,
        )
    )


# ---------------------------------------------------------------------------
# The service itself
# ---------------------------------------------------------------------------


def _answering(record: SqliteLifecyclePersistence) -> tuple[Any, str]:
    server, _thread = serve(ledger=record.db_path, host="127.0.0.1", port=0)
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"


def _get(url: str) -> tuple[int, dict[str, Any]]:
    try:
        with urllib.request.urlopen(url, timeout=30) as answer:
            return answer.status, json.loads(answer.read().decode("utf-8"))
    except urllib.error.HTTPError as refused:  # an answer, not a fault
        return refused.code, json.loads(refused.read().decode("utf-8"))


class TestTheAnswerService:
    """Two questions, read-only, and nothing else at all."""

    def test_it_answers_what_commit_a_build_starts_from(
        self, record: SqliteLifecyclePersistence
    ) -> None:
        commit = "a" * 40
        build = _a_build_starting_from(record, commit)
        server, base = _answering(record)
        try:
            status, answer = _get(f"{base}{ANSWER_ROUTE}?build={build}")
        finally:
            server.shutdown()
            server.server_close()

        assert status == 200
        assert answer == {
            "build": build,
            "recorded": True,
            "start_commit": commit,
        }

    def test_a_build_nobody_wrote_a_commit_for_says_so(
        self, record: SqliteLifecyclePersistence
    ) -> None:
        build = _a_build_with_nothing_recorded(record)
        server, base = _answering(record)
        try:
            status, answer = _get(f"{base}{ANSWER_ROUTE}?build={build}")
            _, unknown = _get(f"{base}{ANSWER_ROUTE}?build=no-such-build")
        finally:
            server.shutdown()
            server.server_close()

        assert status == 200
        assert answer["recorded"] is False and answer["start_commit"] is None
        assert unknown["recorded"] is False and unknown["start_commit"] is None

    def test_it_answers_for_a_planning_run_that_has_no_build_yet(
        self, record: SqliteLifecyclePersistence
    ) -> None:
        """A planning run is this coordinator's work too, and it is asked about.

        Planning's writes go to the same helper and read the project's own
        declarations the same way, so they name the id this coordinator keeps
        the RUN under — a different string from any build id, filed in the
        planning record rather than the builds table. Both are this
        coordinator's own record of where a piece of work starts, so one
        question answers from whichever holds it.
        """
        commit = "d" * 40
        record.connection.row_factory = sqlite3.Row
        store = SqlitePlanningRunStore(record.connection)
        store.record_queued(
            correlation_id="corr-run-with-no-build",
            originating_user="U1",
            expected_approver="U1",
            request_text="a sentence",
            triggered_by="cli",
            target_repo=REPO,
        )
        assert store.record_start_point(
            "corr-run-with-no-build", start_commit=commit, target_branch="main"
        )
        server, base = _answering(record)
        try:
            status, answer = _get(f"{base}{ANSWER_ROUTE}?build=corr-run-with-no-build")
            _, neither = _get(f"{base}{ANSWER_ROUTE}?build=in-neither-record")
        finally:
            server.shutdown()
            server.server_close()

        assert status == 200
        assert answer == {
            "build": "corr-run-with-no-build",
            "recorded": True,
            "start_commit": commit,
        }
        # A name in neither record still gets the honest "nobody said".
        assert neither["recorded"] is False and neither["start_commit"] is None

    def test_a_planning_run_with_no_recorded_commit_says_so(
        self, record: SqliteLifecyclePersistence
    ) -> None:
        """A run from before the starting rule is an absence, never a guess."""
        record.connection.row_factory = sqlite3.Row
        store = SqlitePlanningRunStore(record.connection)
        store.record_queued(
            correlation_id="corr-run-with-no-start",
            originating_user="U1",
            expected_approver="U1",
            request_text="a sentence",
            triggered_by="cli",
            target_repo=REPO,
        )
        server, base = _answering(record)
        try:
            status, answer = _get(f"{base}{ANSWER_ROUTE}?build=corr-run-with-no-start")
        finally:
            server.shutdown()
            server.server_close()

        assert status == 200
        assert answer["recorded"] is False and answer["start_commit"] is None

    def test_it_answers_who_holds_a_deployment_target(
        self, record: SqliteLifecyclePersistence
    ) -> None:
        """The older question, which had no answer in the estate either."""
        build = _a_build_starting_from(record, "b" * 40)
        grant = DeploymentLockStore(record.connection).grant(
            target=TARGET,
            build_id=build,
            turn=1,
            holder="merge-word:tests",
            now=datetime.now(UTC),
        )
        record.connection.commit()
        assert grant is not None
        server, base = _answering(record)
        try:
            status, answer = _get(f"{base}{ANSWER_ROUTE}?target={TARGET}")
            _, never = _get(f"{base}{ANSWER_ROUTE}?target=org/nothing::live")
        finally:
            server.shutdown()
            server.server_close()

        assert status == 200
        assert answer["counter"] == grant.counter and answer["build"] == build
        assert never["recorded"] is False and never["build"] is None

    def test_the_executors_own_asker_reads_this_answer(
        self,
        record: SqliteLifecyclePersistence,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The two askers in the helper, pointed at the real service.

        They are what the gates use, so the fields have to line up with what
        those gates read — the counter and the build for a target, the build
        and its starting commit for a build.
        """
        commit = "c" * 40
        build = _a_build_starting_from(record, commit)
        DeploymentLockStore(record.connection).grant(
            target=TARGET,
            build_id=build,
            turn=1,
            holder="merge-word:tests",
            now=datetime.now(UTC),
        )
        record.connection.commit()
        server, base = _answering(record)
        monkeypatch.setenv(COORDINATOR_OWNER_ENV, f"{base}{ANSWER_ROUTE}")
        try:
            about_build = coordinator_recorded_build()
            about_target = coordinator_owner_asker()
            assert about_build is not None and about_target is not None
            said_build = about_build(build)
            said_target = about_target(TARGET)
        finally:
            server.shutdown()
            server.server_close()

        assert said_build == {
            "build": build,
            "recorded": True,
            "start_commit": commit,
        }
        assert said_target is not None
        assert int(said_target["counter"]) == 1
        assert str(said_target["build"]) == build

    def test_it_changes_nothing_and_takes_nothing(
        self, record: SqliteLifecyclePersistence
    ) -> None:
        """No verb but GET, and the handle itself refuses a write."""
        server, base = _answering(record)
        try:
            request = urllib.request.Request(
                f"{base}{ANSWER_ROUTE}",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as answer:
                    status, body = answer.status, json.loads(answer.read())
            except urllib.error.HTTPError as refused:
                status, body = refused.code, json.loads(refused.read())
            no_such, missing = _get(f"{base}/somewhere-else")
            asked_nothing, nothing = _get(f"{base}{ANSWER_ROUTE}")
            both, two = _get(f"{base}{ANSWER_ROUTE}?build=b-1&target={TARGET}")
            alive, health = _get(f"{base}{HEALTH_ROUTE}")
        finally:
            server.shutdown()
            server.server_close()

        assert status == 405 and "changes nothing" in body["error"]
        assert no_such == 404 and ANSWER_ROUTE in missing["error"]
        assert asked_nothing == 400 and "asked nothing" in nothing["error"]
        assert both == 400 and "one question at a time" in two["error"]
        assert alive == 200 and health == {"status": "healthy"}

        # The handle is read-only in the strong sense: the store itself
        # refuses the write, so it is a property of the handle and not a
        # habit kept by the code above it.
        handle = TheCoordinatorsRecord(record.db_path)._connect()
        try:
            with pytest.raises(sqlite3.OperationalError) as refused:
                handle.execute("UPDATE builds SET start_commit = 'x'")
        finally:
            handle.close()
        assert "readonly" in str(refused.value).lower()

    def test_a_record_that_cannot_be_read_is_an_unanswered_question(
        self, tmp_path: Path
    ) -> None:
        """Never an answer: everything that asks treats this as "nobody said"."""
        missing = tmp_path / "there-is-nothing-here.db"
        server, _thread = serve(ledger=missing, host="127.0.0.1", port=0)
        host, port = server.server_address[:2]
        try:
            status, body = _get(f"http://{host}:{port}{ANSWER_ROUTE}?build=b-1")
        finally:
            server.shutdown()
            server.server_close()

        assert status == 503
        assert str(missing) in body["error"]
        with pytest.raises(TheRecordIsUnreadable):
            TheCoordinatorsRecord(missing).what_build_starts_from("b-1")


# ---------------------------------------------------------------------------
# The rollout: the real helper, asking the real answer, for a real project
# ---------------------------------------------------------------------------


def _git(where: Path, *args: str) -> str:
    done = subprocess.run(
        [
            "git",
            "-c", "user.email=tests@example.invalid",
            "-c", "user.name=tests",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        cwd=str(where),
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def _a_project(root: Path) -> tuple[Path, str, str]:
    """A throwaway project with two commits: without the line, then with it.

    Answers ``(root, the older commit, HEAD)``.
    """
    root.mkdir(parents=True, exist_ok=True)
    script = root / THE_SCRIPT
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "for name in sorted(os.environ):\n"
        "    print('SETTING ' + name)\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | 0o100)
    (root / "deploy" / "profile.yaml").write_text(
        yaml.safe_dump(
            {
                "env_id": "widgetshop",
                "compose": {"file": "docker-compose.yml", "script": THE_SCRIPT},
                "cwd": str(root),
            }
        ),
        encoding="utf-8",
    )
    declaration = root / ".guardkit" / "config.yaml"
    declaration.parent.mkdir(parents=True, exist_ok=True)
    declaration.write_text("toolchain:\n  test: qa/run\n", encoding="utf-8")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "the project before it declared anything")
    older = _git(root, "rev-parse", "HEAD")
    declaration.write_text(
        f"launch:\n  settings: [{DECLARED_SETTING}]\n", encoding="utf-8"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "the project declares what its builds need")
    return root, older, _git(root, "rev-parse", "HEAD")


def _helper(project: Path) -> tuple[Any, str]:
    config = ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": [str(project.parent)]}},
            "planning": {"target_repo_paths": {REPO: str(project)}},
        }
    )
    server = build_server(port=0, config_loader=lambda: config)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"


def _run(base: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        base + "/run",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as answer:
            return answer.status, json.loads(answer.read().decode("utf-8"))
    except urllib.error.HTTPError as refused:
        return refused.code, json.loads(refused.read().decode("utf-8"))


class TestTheHelperAsksThisServiceForReal:
    """The blocker: a stamped request, through the real helper, now runs."""

    def test_a_stamped_request_is_admitted_and_reads_at_the_recorded_commit(
        self,
        record: SqliteLifecyclePersistence,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(DECLARED_SETTING, "/var/cache/some-tool")
        project, older, head = _a_project(tmp_path / "widget-shop")
        build = _a_build_starting_from(record, head)
        answer_server, answer_base = _answering(record)
        monkeypatch.setenv(COORDINATOR_OWNER_ENV, f"{answer_base}{ANSWER_ROUTE}")
        helper, helper_base = _helper(project)
        try:
            # The commit the coordinator recorded, stamped on the request the
            # way the deploy stage stamps it. Before this service existed the
            # same request was refused: "this helper has no way to ask the
            # coordinator what commit that build was recorded as starting
            # from".
            admitted = _run(
                helper_base,
                {
                    "repo": REPO,
                    "script": THE_SCRIPT,
                    "timeout_seconds": 30,
                    "build": build,
                    "declared_at": head,
                    "launch_settings": [DECLARED_SETTING],
                },
            )
            # The same build, and the commit BEFORE the line was committed.
            # The service says HEAD, so the request's own commit loses.
            refused = _run(
                helper_base,
                {
                    "repo": REPO,
                    "script": THE_SCRIPT,
                    "timeout_seconds": 30,
                    "build": build,
                    "declared_at": older,
                    "launch_settings": [DECLARED_SETTING],
                },
            )
            # A build the coordinator wrote no commit for, with a commit on
            # the request: the service answers honestly, and the helper
            # refuses rather than taking the request's word.
            by_hand_build = _a_build_with_nothing_recorded(record)
            nothing_recorded = _run(
                helper_base,
                {
                    "repo": REPO,
                    "script": THE_SCRIPT,
                    "timeout_seconds": 30,
                    "build": by_hand_build,
                    "declared_at": head,
                    "launch_settings": [DECLARED_SETTING],
                },
            )
            # And with no commit on the request, the helper is bound to the
            # commit the service names — not to this copy's HEAD by luck.
            stamped_without_a_commit = _run(
                helper_base,
                {
                    "repo": REPO,
                    "script": THE_SCRIPT,
                    "timeout_seconds": 30,
                    "build": build,
                    "launch_settings": [DECLARED_SETTING],
                },
            )
        finally:
            helper.shutdown()
            helper.server_close()
            answer_server.shutdown()
            answer_server.server_close()

        assert admitted[0] == 200, admitted
        assert DECLARED_SETTING in {
            line[len("SETTING ") :].strip()
            for line in str(admitted[1].get("output_tail") or "").splitlines()
            if line.startswith("SETTING ")
        }
        assert refused[0] == 400
        assert older in refused[1]["error"] and head in refused[1]["error"]
        assert nothing_recorded[0] == 400
        assert "did not say what commit" in nothing_recorded[1]["error"]
        assert stamped_without_a_commit[0] == 200, stamped_without_a_commit
