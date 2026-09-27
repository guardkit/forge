"""Fail-closed validation for the gateway's Slack heartbeat evidence."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
WATCH = ROOT / "deploy" / "estate" / "provisioner" / "gateway-watch.sh"
FIXTURES = Path(__file__).resolve().parent / "gateway-watch-fixtures"
FIXED_NOW = "1790438400"


def _run_watch(
    tmp_path: Path,
    *,
    heartbeat_body: str | None = None,
    heartbeat_path: Path | None = None,
    jq_bin_dir: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    if heartbeat_path is None:
        heartbeat_path = tmp_path / "heartbeat.json"
        assert heartbeat_body is not None
        heartbeat_path.write_text(heartbeat_body)

    notifier = tmp_path / "notifications.jsonl"
    env = {
        "PATH": f"{jq_bin_dir}:{os.environ['PATH']}"
        if jq_bin_dir is not None
        else os.environ["PATH"],
        "JARVIS_NATS_USER": "jarvis",
        "GATEWAY_WATCH_CLIENT_NAME": "bus-gateway-factory",
        "GATEWAY_WATCH_SUBJECT": "agents.command.jarvis",
        "GATEWAY_WATCH_HEARTBEAT_PATH": str(heartbeat_path),
        "GATEWAY_WATCH_NOTIFIER": "file",
        "GATEWAY_WATCH_NOTIFIER_FILE": str(notifier),
        "TZ": "UTC",
    }
    done = subprocess.run(
        [
            "bash",
            str(WATCH),
            "--once",
            "--connz-file",
            str(FIXTURES / "connz-the-gateway-is-there.json"),
            "--log-file",
            str(FIXTURES / "log-fresh.txt"),
            "--now",
            FIXED_NOW,
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    messages = []
    if notifier.exists():
        messages = [
            json.loads(line)["text"]
            for line in notifier.read_text().splitlines()
            if line.strip()
        ]
    return done, messages


@pytest.mark.parametrize(
    "heartbeat_body",
    [
        pytest.param(
            '{"state":"unrecognised-state","last_event_at":"2026-09-26T15:59:55Z"}',
            id="unrecognised-state",
        ),
        pytest.param(
            '{"state":"connected","last_event_at":"2026-09-26T15:59:55Z"}\n'
            '{"state":"connected","last_event_at":"2026-09-26T15:59:56Z"}',
            id="multiple-json-values",
        ),
        pytest.param('{"state":', id="malformed-json"),
        pytest.param(
            '["connected","2026-09-26T15:59:55Z"]',
            id="wrong-root-type",
        ),
        pytest.param(
            '{"state":7,"last_event_at":"2026-09-26T15:59:55Z"}',
            id="wrong-state-type",
        ),
        pytest.param(
            '{"state":"connected","last_event_at":1790438395}',
            id="wrong-time-type",
        ),
        pytest.param(
            json.dumps(
                {
                    "state": "connected",
                    "last_event_at": "junk\n2026-09-26T15:59:55Z",
                }
            ),
            id="time-leading-junk-line",
        ),
        pytest.param(
            json.dumps(
                {
                    "state": "connected",
                    "last_event_at": "2026-09-26T15:59:54Z\n2026-09-26T15:59:55Z",
                }
            ),
            id="time-two-lines",
        ),
        pytest.param(
            json.dumps(
                {
                    "state": "connected",
                    "last_event_at": "2026-09-26T15:59:55Z\njunk",
                }
            ),
            id="time-trailing-junk-line",
        ),
        pytest.param(
            '{"state":"connected","last_event_at":"2026-02-30T15:59:55Z"}',
            id="impossible-calendar-time",
        ),
        pytest.param(
            '{"state":"connected","last_event_at":"2026-09-26T25:59:55Z"}',
            id="impossible-clock-time",
        ),
        pytest.param(
            '{"state":"connected","last_event_at":"not-a-time"}',
            id="unreadable-time",
        ),
        pytest.param("", id="empty-file"),
    ],
)
def test_invalid_heartbeat_evidence_is_unknown_and_alerts_once(
    tmp_path: Path, heartbeat_body: str
) -> None:
    done, messages = _run_watch(tmp_path, heartbeat_body=heartbeat_body)

    assert done.returncode == 10, done.stdout + done.stderr
    assert "Slack session    unknown" in done.stdout
    assert len(messages) == 1
    assert "Slack session    unknown" in messages[0]


def test_unreadable_heartbeat_evidence_is_unknown_and_alerts_once(
    tmp_path: Path,
) -> None:
    heartbeat_directory = tmp_path / "heartbeat-is-a-directory"
    heartbeat_directory.mkdir()

    done, messages = _run_watch(tmp_path, heartbeat_path=heartbeat_directory)

    assert done.returncode == 10, done.stdout + done.stderr
    assert "Slack session    unknown" in done.stdout
    assert len(messages) == 1
    assert "Slack session    unknown" in messages[0]


def test_connected_heartbeat_accepts_producer_extensions(
    tmp_path: Path,
) -> None:
    heartbeat = json.dumps(
        {
            "state": "connected",
            "last_event_at": "2026-09-26T15:59:55.123456+00:00",
            "last_state_change_at": "2026-09-26T15:59:50Z",
            "kind": "envelope",
        }
    )

    done, messages = _run_watch(tmp_path, heartbeat_body=heartbeat)

    assert done.returncode == 0, done.stdout + done.stderr
    assert "Slack session    ok" in done.stdout
    assert messages == []


def test_nonzero_parser_status_cannot_authorize_emitted_valid_json(
    tmp_path: Path,
) -> None:
    real_jq = shutil.which("jq")
    assert real_jq is not None
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    wrapper = fake_bin / "jq"
    wrapper.write_text(
        f"""#!/bin/sh
if [ "$1" = "-cer" ] && [ "$2" = "-s" ]; then
    "{real_jq}" "$@"
    status=$?
    if [ "$status" -ne 0 ]; then
        exit "$status"
    fi
    exit 1
fi
exec "{real_jq}" "$@"
"""
    )
    wrapper.chmod(0o755)
    heartbeat = json.dumps(
        {
            "state": "connected",
            "last_event_at": "2026-09-26T15:59:55Z",
        }
    )

    done, messages = _run_watch(
        tmp_path,
        heartbeat_body=heartbeat,
        jq_bin_dir=fake_bin,
    )

    assert done.returncode == 10, done.stdout + done.stderr
    assert "Slack session    unknown" in done.stdout
    assert len(messages) == 1
    assert "Slack session    unknown" in messages[0]


def test_nonzero_timestamp_conversion_status_refuses_emitted_epoch(
    tmp_path: Path,
) -> None:
    real_jq = shutil.which("jq")
    assert real_jq is not None
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    wrapper = fake_bin / "jq"
    wrapper.write_text(
        f"""#!/bin/sh
if [ "$1" = "-nr" ] && [ "$2" = "--arg" ] && [ "$3" = "stamp" ]; then
    "{real_jq}" "$@"
    status=$?
    if [ "$status" -ne 0 ]; then
        exit "$status"
    fi
    exit 1
fi
exec "{real_jq}" "$@"
"""
    )
    wrapper.chmod(0o755)
    heartbeat = json.dumps(
        {
            "state": "connected",
            "last_event_at": "2026-09-26T15:59:55.123456+00:00",
        }
    )

    done, messages = _run_watch(
        tmp_path,
        heartbeat_body=heartbeat,
        jq_bin_dir=fake_bin,
    )

    assert done.returncode == 10, done.stdout + done.stderr
    assert "Slack session    unknown" in done.stdout
    assert len(messages) == 1
    assert "Slack session    unknown" in messages[0]


@pytest.mark.parametrize("state", ["connecting", "disconnected"])
def test_known_unhealthy_states_remain_lost_and_alert_once(
    tmp_path: Path, state: str
) -> None:
    heartbeat = json.dumps(
        {
            "state": state,
            "last_event_at": "2026-09-26T15:59:55Z",
        }
    )

    done, messages = _run_watch(tmp_path, heartbeat_body=heartbeat)

    assert done.returncode == 10, done.stdout + done.stderr
    assert "Slack session    lost" in done.stdout
    assert len(messages) == 1
    assert "Slack session    lost" in messages[0]
