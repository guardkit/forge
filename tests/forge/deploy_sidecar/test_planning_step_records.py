"""Every planning step is kept, so it can be replayed (1 November 2026).

The sandbox helper removes a planning step's request file and the planner's
output folder when the step ends. Before it does, it keeps them under the
project's receipts folder (``<receipts>/planning/<UTC time>-<run>-<step>-…``).
What this file pins, with the same fake container client as the route's own
tests (no container is ever started) and a real git repository:

* after a step: the request exactly as Forge posted it, the file the planner
  read, the planner's whole output folder (result and trace), the helper's
  answer and a short summary are kept, and the working files are still removed;
* a step that ran out of time or was cancelled is kept too;
* records older than the bound are removed, and nothing else under the folder;
* a credential is never kept: a secret-named field or a credential-shaped
  value is replaced, and ordinary text (commit ids, ``max_tokens``) is not;
* no receipts folder named: nothing is kept and the step still answers.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from forge.deploy_sidecar import planning_records
from forge.deploy_sidecar.planning_records import (
    PLANNING_RECORDS_DAYS_DEFAULT,
    REDACTED,
    planning_records_days,
    prune_planning_records,
    record_folder_name,
)
from forge.deploy_sidecar.service import PLANNING_STEP_ROUTE, build_server
from forge.executor import programs
from forge.executor.programs import StoppableRun
from tests.forge.deploy_sidecar.test_planning_step_route import (  # noqa: F401
    BUILD,
    REPO,
    RESULT,
    _body,
    _FakeClient,
    _helper_env,
    _leftovers,
    _post,
    _run,
    clone,
    config,
    first_commit,
)

#: A made-up key of a real provider's shape. It must never be kept.
FAKE_KEY = "sk-ant-api03-" + "Q" * 40
FAKE_GITHUB = "ghp_" + "Z" * 36


def _receipts_env(receipts: Path, **extra: str) -> dict[str, str]:
    return _helper_env(FORGE_RECEIPTS_DIR=str(receipts), **extra)


def _the_record(receipts: Path) -> Path:
    [record] = sorted((receipts / "planning").iterdir())
    return record


def _every_kept_byte(record: Path) -> bytes:
    return b"".join(p.read_bytes() for p in sorted(record.rglob("*")) if p.is_file())


def _client_with_a_trace(**kwargs: Any) -> _FakeClient:
    """The fake client, which also writes a trace beside ``result.json``."""
    client = _FakeClient(**kwargs)
    earlier = client.during_run

    def write_trace() -> None:
        assert client.output is not None
        (client.output / "trace").mkdir()
        (client.output / "trace" / "prompt.txt").write_text("the whole prompt\n")
        (client.output / "trace" / "calls.jsonl").write_text('{"tokens": 12}\n')
        if earlier is not None:
            earlier()

    client.during_run = write_trace
    return client


class TestAStepIsKept:
    def test_request_output_and_answer_are_kept_and_the_step_cleans_up(
        self, config, clone, first_commit, tmp_path: Path
    ) -> None:
        receipts = tmp_path / "receipts"
        payload = _body("feature_plan", first_commit)
        payload["request"]["args"]["feature_id"] = "FEAT-1A2B"
        # The bytes as Forge sent them: spacing and key order are its own.
        raw = json.dumps(payload, indent=1, sort_keys=True).encode("utf-8")
        client = _client_with_a_trace()

        status, body = _run(
            config, payload, client, env=_receipts_env(receipts), raw_body=raw
        )

        assert status == 200, body
        record = _the_record(receipts)
        assert body["record"] == str(record)
        assert record.name.endswith(body["container"].removeprefix("forge-planning-"))
        assert BUILD in record.name and "feature-plan" in record.name
        assert (record / "request-body.json").read_bytes() == raw
        assert json.loads((record / "request.json").read_text()) == payload["request"]
        assert client.request == payload["request"]
        assert json.loads((record / "output" / "result.json").read_text()) == RESULT
        assert (record / "output" / "trace" / "prompt.txt").read_text() == (
            "the whole prompt\n"
        )
        reply = json.loads((record / "reply.json").read_text())
        assert reply["result"] == RESULT and reply["exit_code"] == 0
        step = json.loads((record / "step.json").read_text())
        assert step["repo"] == REPO and step["build"] == BUILD
        assert step["command"] == "feature_plan" and step["role"] == "architect"
        assert step["feature_id"] == "FEAT-1A2B"
        assert step["start_commit"] == first_commit
        assert step["request_body_as_posted"] is True
        assert step["has_result"] is True
        assert step["output"]["files"] == 3
        assert step["redacted"] == {
            "request-body.json": [],
            "request.json": [],
            "reply.json": [],
            "output": [],
        }
        # The helper's own working files are still removed.
        assert _leftovers(clone) == []
        # The planner's settings file is never kept.
        assert "OPENAI_API_KEY" not in _every_kept_byte(record).decode()

    def test_the_greenfield_step_is_kept_too(self, config, clone, tmp_path) -> None:
        receipts = tmp_path / "receipts"
        client = _FakeClient(result={**RESULT, "command": "greenfield"})
        status, body = _run(
            config, _body("greenfield", None), client, env=_receipts_env(receipts)
        )
        assert status == 200, body
        record = _the_record(receipts)
        step = json.loads((record / "step.json").read_text())
        assert step["command"] == "greenfield" and step["start_commit"] is None
        # Called without the posted bytes, the request is written from the
        # parsed body, and the summary says so.
        assert step["request_body_as_posted"] is False
        assert json.loads((record / "request-body.json").read_text()) == _body(
            "greenfield", None
        )

    def test_a_step_stopped_at_its_time_limit_is_kept(
        self, config, clone, first_commit, tmp_path
    ) -> None:
        receipts = tmp_path / "receipts"
        client = _client_with_a_trace(
            result=None, run_answer=programs.TimedOut(124, "", "stopped")
        )
        status, body = _run(
            config, _body("feature_spec", first_commit), client,
            env=_receipts_env(receipts),
        )
        assert status == 200 and body["timed_out"] is True
        record = _the_record(receipts)
        step = json.loads((record / "step.json").read_text())
        assert step["timed_out"] is True and step["has_result"] is False
        # What the planner wrote before it was stopped is kept.
        assert (record / "output" / "trace" / "prompt.txt").exists()
        assert _leftovers(clone) == []

    def test_a_cancelled_step_is_kept(
        self, config, clone, first_commit, tmp_path
    ) -> None:
        receipts = tmp_path / "receipts"
        run = StoppableRun()
        client = _FakeClient(
            result=None,
            run_answer=(143, "", "terminated"),
            during_run=lambda: run.request_stop(grace=0),
        )
        status, body = _run(
            config, _body("feature_plan", first_commit), client,
            env=_receipts_env(receipts), stop_run=run,
        )
        assert status == 200 and body["cancelled"] is True
        step = json.loads((_the_record(receipts) / "step.json").read_text())
        assert step["cancelled"] is True
        assert _leftovers(clone) == []

    def test_a_link_in_the_output_is_named_not_followed(
        self, config, clone, first_commit, tmp_path
    ) -> None:
        receipts = tmp_path / "receipts"
        outside = tmp_path / "outside.txt"
        outside.write_text("not the planner's\n")
        client = _FakeClient()
        client.during_run = lambda: (client.output / "link.txt").symlink_to(outside)
        status, _ = _run(
            config, _body("feature_plan", first_commit), client,
            env=_receipts_env(receipts),
        )
        assert status == 200
        record = _the_record(receipts)
        assert not (record / "output" / "link.txt").exists()
        step = json.loads((record / "step.json").read_text())
        assert step["output"]["not_copied"] == ["link.txt"]

    def test_with_no_receipts_folder_nothing_is_kept_and_the_step_answers(
        self, config, clone, first_commit
    ) -> None:
        status, body = _run(config, _body("feature_plan", first_commit), _FakeClient())
        assert status == 200
        assert body["result"] == RESULT
        assert body["record"] is None
        assert _leftovers(clone) == []

    def test_a_record_that_cannot_be_written_never_fails_the_step(
        self, config, clone, first_commit, tmp_path
    ) -> None:
        blocked = tmp_path / "receipts"
        blocked.write_text("a file where the folder should be\n")
        status, body = _run(
            config, _body("feature_plan", first_commit), _FakeClient(),
            env=_receipts_env(blocked),
        )
        assert status == 200
        assert body["result"] == RESULT and body["record"] is None
        assert _leftovers(clone) == []


class TestThroughTheService:
    def test_the_posted_bytes_are_kept_exactly(
        self, config, clone, first_commit, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        receipts = tmp_path / "receipts"
        for name, value in _receipts_env(receipts).items():
            monkeypatch.setenv(name, value)
        srv = build_server(
            port=0, config_loader=lambda: config, planning_runner=_FakeClient()
        )
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        host, port = srv.server_address[:2]
        raw = json.dumps(_body("feature_spec", first_commit), indent=3).encode()
        try:
            import urllib.request

            request = urllib.request.Request(
                f"http://{host}:{port}{PLANNING_STEP_ROUTE}",
                data=raw,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                body = json.loads(response.read().decode("utf-8"))
        finally:
            srv.shutdown()
            srv.server_close()
        record = _the_record(receipts)
        assert body["record"] == str(record)
        assert (record / "request-body.json").read_bytes() == raw
        assert _leftovers(clone) == []


class TestTheBound:
    def test_records_older_than_the_bound_are_removed_and_nothing_else(
        self, tmp_path: Path
    ) -> None:
        now = dt.datetime(2026, 11, 20, 12, 0, tzinfo=dt.timezone.utc)
        root = tmp_path / "planning"
        old = root / record_folder_name(now - dt.timedelta(days=15), "forge-planning-r-feature-spec-1")
        old_partial = root / (
            record_folder_name(now - dt.timedelta(days=20), "forge-planning-r-feature-plan-2")
            + ".partial"
        )
        recent = root / record_folder_name(now - dt.timedelta(days=13), "forge-planning-r-feature-plan-3")
        someone_elses = root / "notes-kept-by-hand"
        for folder in (old, old_partial, recent, someone_elses):
            folder.mkdir(parents=True)
            (folder / "f").write_text("x")
        stray_file = root / "20200101T000000Z-a-file"
        stray_file.write_text("not a folder")

        removed = prune_planning_records(root, days=14, now=now)

        assert sorted(removed) == sorted([old.name, old_partial.name])
        assert recent.exists() and someone_elses.exists() and stray_file.exists()
        assert not old.exists() and not old_partial.exists()

    def test_keeping_a_step_removes_the_old_ones(
        self, config, clone, first_commit, tmp_path
    ) -> None:
        receipts = tmp_path / "receipts"
        ancient = receipts / "planning" / "20200101T000000Z-r-feature-plan-abcd1234"
        ancient.mkdir(parents=True)
        status, body = _run(
            config, _body("feature_plan", first_commit), _FakeClient(),
            env=_receipts_env(receipts, FORGE_PLANNING_RECORDS_DAYS="3"),
        )
        assert status == 200
        assert not ancient.exists()
        assert [p.name for p in (receipts / "planning").iterdir()] == [
            Path(body["record"]).name
        ]

    @pytest.mark.parametrize(
        ("setting", "days"),
        [
            (None, PLANNING_RECORDS_DAYS_DEFAULT),
            ("", PLANNING_RECORDS_DAYS_DEFAULT),
            ("30", 30),
            ("0", PLANNING_RECORDS_DAYS_DEFAULT),
            ("-2", PLANNING_RECORDS_DAYS_DEFAULT),
            ("a week", PLANNING_RECORDS_DAYS_DEFAULT),
        ],
    )
    def test_the_one_setting(self, setting, days) -> None:
        env = {} if setting is None else {"FORGE_PLANNING_RECORDS_DAYS": setting}
        assert planning_records_days(env) == days

    def test_the_default_is_fourteen_days(self) -> None:
        assert PLANNING_RECORDS_DAYS_DEFAULT == 14


class TestNothingSecretIsKept:
    def test_a_secret_named_field_and_a_key_shaped_value_are_replaced(
        self, config, clone, first_commit, tmp_path
    ) -> None:
        receipts = tmp_path / "receipts"
        payload = _body("feature_plan", first_commit)
        args = payload["request"]["args"]
        args["api_key"] = "plain-but-named-as-a-key"
        args["context"] = [
            f"File: notes.md\nour key is {FAKE_KEY} and the token {FAKE_GITHUB}",
            "File: db.md\nconnect with postgresql://app:hunter2hunter2@db:5432/app",
            # Ordinary text that must survive untouched.
            "File: plan.md\nbase commit " + "a1b2c3d4" * 5 + ", max_tokens 900, "
            "send Authorization: Bearer <token>",
        ]
        client = _FakeClient()

        def the_planner_echoes_a_key() -> None:
            (client.output / "trace.jsonl").write_text(
                json.dumps({"prompt": f"... {FAKE_KEY} ..."}) + "\n"
            )

        client.during_run = the_planner_echoes_a_key
        raw = json.dumps(payload).encode()

        status, _ = _run(
            config, payload, client, env=_receipts_env(receipts), raw_body=raw
        )

        assert status == 200
        record = _the_record(receipts)
        kept = _every_kept_byte(record).decode()
        for secret in (FAKE_KEY, FAKE_GITHUB, "plain-but-named-as-a-key", "hunter2hunter2"):
            assert secret not in kept
        request = json.loads((record / "request.json").read_text())
        assert request["args"]["api_key"] == REDACTED
        assert "postgresql://app:" + REDACTED + "@db:5432/app" in request["args"]["context"][1]
        assert request["args"]["context"][2] == args["context"][2]
        assert request["args"]["time_budget_seconds"] == 3480
        step = json.loads((record / "step.json").read_text())
        assert step["request_body_as_posted"] is False
        assert "/request/args/api_key" in step["redacted"]["request-body.json"]
        assert "/args/context[0]" in step["redacted"]["request.json"]
        assert step["redacted"]["output"] == ["trace.jsonl"]

    @pytest.mark.parametrize(
        "name",
        ["api_key", "apiKey", "authToken", "password", "db_dsn", "private-key", "Authorization"],
    )
    def test_secret_names(self, name: str) -> None:
        clean, found = planning_records.redact_json({name: "value"})
        assert clean == {name: REDACTED} and found == [f"/{name}"]

    @pytest.mark.parametrize(
        "name",
        ["max_tokens", "feature_routing_id", "time_budget_seconds", "keyboard", "monkey"],
    )
    def test_ordinary_names(self, name: str) -> None:
        clean, found = planning_records.redact_json({name: "value"})
        assert clean == {name: "value"} and found == []


class TestTheOutputIsCheckedLikeTheRequest:
    """Review finding R1, 1 November 2026: the planner's output was checked by
    shape only, on its serialized text."""

    def _keep(self, config, first_commit, tmp_path, write) -> Path:
        receipts = tmp_path / "receipts"
        client = _FakeClient(result=None)
        client.during_run = lambda: write(client.output)
        status, _ = _run(
            config, _body("feature_plan", first_commit), client,
            env=_receipts_env(receipts),
        )
        assert status == 200
        return _the_record(receipts)

    def test_a_token_right_after_a_serialized_newline_in_a_trace(
        self, config, clone, first_commit, tmp_path
    ) -> None:
        line = json.dumps({"prompt": f"first line\n{FAKE_GITHUB}\nlast line"})
        assert f"\\n{FAKE_GITHUB}" in line  # the shape the finding names

        def write(output: Path) -> None:
            (output / "result.json").write_text(json.dumps(RESULT))
            (output / "trace").mkdir()
            (output / "trace" / "calls.jsonl").write_text(
                json.dumps({"tokens": 12}) + "\n" + line + "\n"
            )
            # The same, in a file that is not JSON at all.
            (output / "trace" / "prompt.log").write_text(f"said: {line}\n")

        record = self._keep(config, first_commit, tmp_path, write)
        assert FAKE_GITHUB not in _every_kept_byte(record).decode()
        lines = (record / "output" / "trace" / "calls.jsonl").read_text().splitlines()
        # The untouched line is kept as it was; the other is re-written.
        assert lines[0] == json.dumps({"tokens": 12})
        assert json.loads(lines[1]) == {
            "prompt": f"first line\n{REDACTED}\nlast line"
        }
        step = json.loads((record / "step.json").read_text())
        assert sorted(step["redacted"]["output"]) == [
            "trace/calls.jsonl",
            "trace/prompt.log",
        ]

    def test_an_ordinary_password_under_a_credential_named_field(
        self, config, clone, first_commit, tmp_path
    ) -> None:
        answer = {
            **RESULT,
            "result": {"role_output": {"plan": "yes", "db_password": "correct horse"}},
        }

        def write(output: Path) -> None:
            (output / "result.json").write_text(json.dumps(answer, indent=2) + "\n")

        record = self._keep(config, first_commit, tmp_path, write)
        kept = (record / "output" / "result.json").read_text()
        assert "correct horse" not in kept
        assert json.loads(kept)["result"]["role_output"] == {
            "plan": "yes",
            "db_password": REDACTED,
        }
        assert kept.endswith("\n")
        step = json.loads((record / "step.json").read_text())
        assert step["redacted"]["output"] == ["result.json"]

    def test_json_that_needs_nothing_keeps_its_original_bytes(
        self, config, clone, first_commit, tmp_path
    ) -> None:
        # Spacing, key order and escapes a re-write would change.
        result_text = '{ "success" : true,\n  "command":"feature_plan", "x": "caf\\u00e9" }\n'
        trace_text = '{"a":1}\n\n{"b" : [1, 2]}\r\n'

        def write(output: Path) -> None:
            (output / "result.json").write_text(result_text)
            (output / "calls.jsonl").write_bytes(trace_text.encode())

        record = self._keep(config, first_commit, tmp_path, write)
        assert (record / "output" / "result.json").read_text() == result_text
        assert (record / "output" / "calls.jsonl").read_bytes() == trace_text.encode()
        step = json.loads((record / "step.json").read_text())
        assert step["redacted"]["output"] == []

    def test_an_output_file_that_breaks_the_reader_never_fails_the_step(
        self, config, clone, first_commit, tmp_path
    ) -> None:
        deep = "[" * 100_000 + "]" * 100_000

        def write(output: Path) -> None:
            (output / "result.json").write_text(json.dumps(RESULT))
            (output / "deep.json").write_text(deep)

        record = self._keep(config, first_commit, tmp_path, write)
        assert (record / "output" / "deep.json").read_text() == deep

    @pytest.mark.parametrize(
        "text",
        [
            f"\\n{FAKE_GITHUB}",
            f"\\t{FAKE_GITHUB}",
            "\\nBearer " + "t" * 30,
            "\\nAKIA" + "A" * 16,
            "\\nhttps://someone:a-password@example.invalid/x",
        ],
    )
    def test_a_shape_after_a_written_escape_is_caught(self, text: str) -> None:
        clean, count = planning_records.redact_text(text)
        assert count == 1 and REDACTED in clean
        assert clean.startswith("\\")

    @pytest.mark.parametrize(
        "text",
        [
            "commit " + "a1b2c3d4" * 5,
            "nghp_" + "Z" * 36,  # inside a word, not after an escape
            "Authorization: Bearer <token>",
        ],
    )
    def test_ordinary_text_is_left_alone(self, text: str) -> None:
        assert planning_records.redact_text(text) == (text, 0)
