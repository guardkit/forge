"""The nightly sweep, run end to end against stand-ins.

Each test runs the real sweep (fetch handed a real bare mirror, the real
export from the object store, the real state, queue and Slack wording) with
fake scanners, a fake model and a recording Slack. The design's Forge
acceptance cases are named in each test's docstring.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta

import pytest

from forge.security_sweep import drafting
from forge.security_sweep.state import STATE_FILE, SweepAlreadyRunning, SweepLock
from forge.security_sweep.sweep import run_sweep

from .conftest import Lab, planted_key

LOCK = '{"name": "web", "lockfileVersion": 3}\n'

VITEST = {
    "package": "vitest",
    "version": "4.0.15",
    "id": "GHSA-5xrq-8626-4rwp",
    "score": 9.8,
    "label": "CRITICAL",
    "fixed": ["4.1.0", "3.2.6"],
}


def _queue_text(lab: Lab) -> str:
    texts = []
    for table in ("work_queue", "work_queue_events"):
        for row in lab.queue.connection.execute(f"SELECT * FROM {table}"):
            texts.append(json.dumps([str(v) for v in tuple(row)]))
    return "\n".join(texts)


def _state(lab: Lab) -> dict:
    return json.loads((lab.folder / STATE_FILE).read_text())


# ---------------------------------------------------------------------------
# The redaction boundary (Codex R3)


@pytest.mark.asyncio
async def test_a_planted_key_appears_nowhere_the_sweep_writes_or_sends(
    lab: Lab, caplog: pytest.LogCaptureFixture
) -> None:
    """R3: a fake key in a scanned file and inside a code excerpt reaches none
    of the prompt as sent, the queue row and its event, the state file, the
    history line, the log or the Slack payload."""
    key = planted_key()
    lab.model.answer = lambda prompt: (
        f'I saw {key} somewhere, and it is real. {{"real": true}}'
    )
    lab.add(
        "org/app",
        {
            "config/settings.py": f'AWS_KEY = "{key}"\n',
            "app/run.py": (
                "import subprocess\n"
                f'TOKEN = "{key}"\n'
                "def go(cmd):\n"
                "    subprocess.call(cmd, shell=True)\n"
            ),
        },
    )
    caplog.set_level(logging.DEBUG)
    report = await run_sweep(lab.deps())

    assert report.outcome == "finished"
    assert lab.model.prompts, "the model was asked"
    # The code finding's excerpt was checked for secrets before it was sent.
    assert any(key.encode() in data for data in lab.scanners.stdin_inputs)
    assert any("[line hidden: may hold a secret]" in p for p in lab.model.prompts)
    for prompt in lab.model.prompts:
        assert key not in prompt
    assert lab.rows(), "a repair was drafted"
    assert key not in _queue_text(lab)
    assert key not in (lab.folder / "state.json").read_text()
    assert key not in (lab.folder / "history.jsonl").read_text()
    assert key not in caplog.text
    assert lab.slack.lines
    for line in lab.slack.lines:
        assert key not in line
    # The model's own words never leave: only its yes is kept.
    assert "somewhere" not in lab.every_stored_text()
    assert "somewhere" not in caplog.text
    assert all("somewhere" not in line for line in lab.slack.lines)


@pytest.mark.asyncio
async def test_a_secret_finding_is_sent_as_metadata_only(lab: Lab) -> None:
    """R3: a secret's surrounding source is never read into the prompt."""
    key = planted_key()
    lab.add("org/app", {"deploy/env.sh": f"echo start\nexport KEY={key}\necho done\n"})
    await run_sweep(lab.deps())
    (prompt,) = lab.model.prompts
    assert "file: deploy/env.sh" in prompt
    assert "line: 2" in prompt
    assert "echo start" not in prompt and "echo done" not in prompt
    assert key not in prompt


@pytest.mark.asyncio
async def test_no_excerpt_is_sent_when_the_secret_check_cannot_run(lab: Lab) -> None:
    """R3: if ``gitleaks stdin`` cannot run, the code finding goes without code."""
    lab.scanners.fail["gitleaks stdin"] = "stopped with exit code 2"
    lab.add("org/app", {"app/run.py": "import subprocess\nsubprocess.call(c, shell=True)\n"})
    await run_sweep(lab.deps())
    (prompt,) = lab.model.prompts
    assert "subprocess.call" not in prompt
    assert "The code around it" not in prompt


@pytest.mark.asyncio
async def test_outward_text_takes_the_rules_fixed_message_never_the_scanners(lab: Lab) -> None:
    """R3: the rule's message comes from the rule file, placeholders removed,
    never the scanner's interpolated text."""
    lab.add("org/app", {"app/run.py": "import subprocess\nsubprocess.call(c, shell=True)\n"})
    await run_sweep(lab.deps())
    (row,) = lab.rows()
    assert "interpolated text from the file" not in row["sentence"]
    assert "the source line" not in lab.every_stored_text()
    assert "Found 'this value' with 'shell=True'" in row["sentence"]


# ---------------------------------------------------------------------------
# Drafting at most once (Codex R4, R10)


@pytest.mark.asyncio
async def test_a_serious_advisory_becomes_one_ordinary_repair_request(lab: Lab) -> None:
    lab.scanners.advisories = [VITEST]
    lab.add("org/react-go-notes-fixture", {"web/package-lock.json": LOCK})
    report = await run_sweep(lab.deps())

    (row,) = lab.rows()
    assert row["kind"] == "feature"
    assert row["status"] == "QUEUED"
    assert row["originating_user"] == "rich"
    assert row["target_repo"] == "org/react-go-notes-fixture"
    assert row["correlation_id"].startswith("security-org-react-go-notes-fixture-")
    assert (
        "vitest 4.0.15 in web/package-lock.json has advisory GHSA-5xrq-8626-4rwp "
        "(critical, 9.8); OSV lists fixes in 3.2.6 and 4.1.0 — move to a version "
        "where every listed serious advisory is fixed"
    ) in row["sentence"]
    assert row["sentence"].endswith("The factory's per-change security check confirms the result.")
    details = lab.queue.origin_details(int(row["id"]))
    assert details["source"] == "security-sweep"
    assert details["triggered_by"] == "forge-internal"
    assert len(details["fingerprints"]) == 1
    (line,) = lab.slack.lines
    assert line == (
        "Nightly security check: react-go-notes-fixture — 1 serious problem, "
        "repair request sent (its spec card follows)."
    )
    assert report.projects[0].drafted_row == int(row["id"])


@pytest.mark.asyncio
async def test_an_interrupted_run_files_one_row(lab: Lab) -> None:
    """R4: stopped between filing and saving its state, then run again: one row."""
    lab.scanners.advisories = [VITEST]
    lab.add("org/app", {"package-lock.json": LOCK})
    lab.fail_save_after_filing = True
    first = await run_sweep(lab.deps())
    assert first.outcome == "failed"
    assert len(lab.rows()) == 1
    assert not (lab.folder / STATE_FILE).exists()
    assert "the run failed" in lab.slack.lines[-1]

    lab.fail_save_after_filing = False
    second = await run_sweep(lab.deps())
    assert second.outcome == "finished"
    assert len(lab.rows()) == 1
    assert second.projects[0].drafted_row is None
    entries = _state(lab)["findings"].values()
    assert [e["status"] for e in entries] == ["drafted"]


@pytest.mark.asyncio
async def test_a_deleted_state_file_still_gives_one_row(lab: Lab) -> None:
    """R4: the queue, not the state file, is the record."""
    lab.scanners.advisories = [VITEST]
    lab.add("org/app", {"package-lock.json": LOCK})
    await run_sweep(lab.deps())
    (lab.folder / STATE_FILE).unlink()
    await run_sweep(lab.deps())
    assert len(lab.rows()) == 1
    assert [e["status"] for e in _state(lab)["findings"].values()] == ["drafted"]


@pytest.mark.asyncio
async def test_the_same_findings_give_the_same_row_identity_any_day(lab: Lab) -> None:
    """R4: the correlation id does not depend on the date."""
    prints = ["b" * 32, "a" * 32]
    assert drafting.repair_correlation_id("org/app", prints) == drafting.repair_correlation_id(
        "org/app", list(reversed(prints))
    )


@pytest.mark.asyncio
async def test_more_than_ten_serious_findings_with_one_open_repair_per_project(lab: Lab) -> None:
    """R10: sixteen serious findings give a row of ten, six still eligible,
    and a second row only once the first is closed."""
    lab.scanners.advisories = [
        {"package": f"pkg{n:02d}", "version": "1.0.0", "id": f"GHSA-{n:04d}", "score": 7.0 + n / 10}
        for n in range(16)
    ]
    lab.add("org/study-tutor", {"package-lock.json": LOCK})
    first = await run_sweep(lab.deps())
    (row,) = lab.rows()
    details = lab.queue.origin_details(int(row["id"]))
    assert len(details["fingerprints"]) == 10
    # The ten highest scores were chosen.
    for n in range(6, 16):
        assert f"GHSA-{n:04d}" in row["sentence"]
    for n in range(6):
        assert f"GHSA-{n:04d}" not in row["sentence"]
    assert first.projects[0].serious_eligible == 16
    assert "10 serious problems, repair request sent (its spec card follows); 6 more wait for it" in lab.slack.lines[-1]

    # While the first row is open, nothing more is filed.
    second = await run_sweep(lab.deps())
    assert len(lab.rows()) == 1
    assert second.projects[0].waiting_for_row == int(row["id"])
    assert second.projects[0].serious_eligible == 6

    # Once it closes, having fixed its ten, the remaining six are filed.
    lab.queue.close(int(row["id"]), status="DONE", actor_identity="test")
    lab.scanners.advisories = lab.scanners.advisories[:6]
    await run_sweep(lab.deps())
    rows = lab.rows()
    assert len(rows) == 2
    assert len(lab.queue.origin_details(int(rows[1]["id"]))["fingerprints"]) == 6
    for n in range(6):
        assert f"GHSA-{n:04d}" in rows[1]["sentence"]


@pytest.mark.asyncio
async def test_malicious_packages_then_real_secrets_rank_first(lab: Lab) -> None:
    """R10: a malicious package, then a real secret, then the highest scores."""
    lab.scanners.advisories = [
        {"package": f"pkg{n:02d}", "version": "1.0.0", "id": f"GHSA-{n:04d}", "score": 9.9}
        for n in range(10)
    ] + [{"package": "evil", "version": "6.6.6", "id": "MAL-2026-1", "score": ""}]
    lab.add("org/app", {"package-lock.json": LOCK, "ops/key.sh": f"K={planted_key()}\n"})
    await run_sweep(lab.deps())
    (row,) = lab.rows()
    sentence = row["sentence"]
    assert sentence.index("evil 6.6.6") < sentence.index("ops/key.sh line 1")
    assert sentence.index("ops/key.sh line 1") < sentence.index("pkg00")
    assert "known malicious package" in sentence
    assert "GHSA-0009" not in sentence  # the eleventh and twelfth wait


@pytest.mark.asyncio
async def test_lower_scores_and_unsorted_findings_are_never_drafted(lab: Lab) -> None:
    lab.scanners.advisories = [{"package": "x", "version": "1", "id": "GHSA-low", "score": 5.0}]
    lab.model.down = True
    lab.add("org/app", {"package-lock.json": LOCK, "k.sh": f"K={planted_key()}\n"})
    report = await run_sweep(lab.deps())
    assert lab.rows() == []
    assert report.projects[0].pending == 1


# ---------------------------------------------------------------------------
# Fingerprints (Codex R11) and statuses


def _prints(lab: Lab, kind: str) -> dict[str, dict]:
    return {fp: e for fp, e in _state(lab)["findings"].items() if e["kind"] == kind}


@pytest.mark.asyncio
async def test_two_occurrences_in_one_file_are_two_findings(lab: Lab) -> None:
    lab.add("org/app", {"docs/API.md": f"a\nkey {planted_key()}\nb\nkey {planted_key('ABCDEFGHIJKLMNOP')}\n"})
    await run_sweep(lab.deps())
    assert len(_prints(lab, "secret")) == 2


@pytest.mark.asyncio
async def test_a_new_real_key_beside_a_dismissed_example_is_sorted(lab: Lab) -> None:
    """R11: a dismissed documentation example, then a real key added later in
    the same file: two fingerprints, and only the new one is sorted."""
    project = lab.add("org/app", {"docs/API.md": f"example {planted_key()}\n"})
    lab.model.answer = lambda prompt: '{"real": false}'
    await run_sweep(lab.deps())
    (old,) = _prints(lab, "secret")
    assert _state(lab)["findings"][old]["status"] == "not-real"

    project.write({"docs/API.md": f"example {planted_key()}\nreal {planted_key('ZZZZZZZZZZZZZZZZ')}\n"})
    project.commit()
    lab.model.prompts.clear()
    lab.model.answer = lambda prompt: '{"real": true}'
    await run_sweep(lab.deps())
    prints = _prints(lab, "secret")
    assert len(prints) == 2
    assert prints[old]["status"] == "not-real"
    (new,) = set(prints) - {old}
    assert prints[new]["status"] == "drafted"
    assert len(lab.model.prompts) == 1 and "line: 2" in lab.model.prompts[0]


@pytest.mark.asyncio
async def test_a_dismissed_example_replaced_in_place_by_a_real_key_is_new(lab: Lab) -> None:
    """R11: the dismissed token at docs/API.md:717 replaced at the same place
    gives a new fingerprint, sorted afresh; an untouched finding elsewhere
    keeps its old one."""
    filler = "".join(f"line {n}\n" for n in range(1, 717))
    project = lab.add(
        "org/app",
        {
            "docs/API.md": filler + f"token {planted_key()}\n",
            "docs/other.md": f"x\nother {planted_key('OTHERKEYOTHERKEY')}\n",
        },
    )
    lab.model.answer = lambda prompt: '{"real": false}'
    await run_sweep(lab.deps())
    before = _prints(lab, "secret")
    (api,) = [fp for fp, e in before.items() if e["file"] == "docs/API.md"]
    (other,) = [fp for fp, e in before.items() if e["file"] == "docs/other.md"]
    assert before[api]["line"] == 717

    project.write({"docs/API.md": filler + f"token {planted_key('REALREALREALREAL')}\n"})
    project.commit()
    lab.model.prompts.clear()
    lab.model.answer = lambda prompt: '{"real": true}'
    await run_sweep(lab.deps())
    after = _prints(lab, "secret")
    assert after[other]["status"] == "not-real"
    assert after[api]["status"] == "gone"
    (replaced,) = [
        fp for fp, e in after.items() if e["file"] == "docs/API.md" and e["status"] != "gone"
    ]
    assert replaced != api
    assert after[replaced]["line"] == 717
    assert after[replaced]["status"] == "drafted"
    assert len(lab.model.prompts) == 1 and "file: docs/API.md" in lab.model.prompts[0]


@pytest.mark.asyncio
async def test_findings_that_go_away_are_marked_gone(lab: Lab) -> None:
    project = lab.add("org/app", {"k.sh": f"K={planted_key()}\n"})
    lab.model.answer = lambda prompt: '{"real": false}'
    await run_sweep(lab.deps())
    project.remove("k.sh")
    project.write({"other.txt": "nothing\n"})
    project.commit()
    await run_sweep(lab.deps())
    assert [e["status"] for e in _state(lab)["findings"].values()] == ["gone"]


@pytest.mark.asyncio
async def test_a_scanner_that_fails_does_not_mark_its_findings_gone(lab: Lab) -> None:
    lab.add("org/app", {"k.sh": f"K={planted_key()}\n"})
    lab.model.answer = lambda prompt: '{"real": false}'
    await run_sweep(lab.deps())
    lab.scanners.fail["gitleaks dir"] = "did not finish within 300 seconds"
    report = await run_sweep(lab.deps())
    assert [e["status"] for e in _state(lab)["findings"].values()] == ["not-real"]
    assert report.projects[0].not_checked["secrets"] == (
        "the secret scanner did not finish within 300 seconds"
    )


@pytest.mark.asyncio
async def test_the_key_file_is_private_and_kept_apart_from_the_state(lab: Lab) -> None:
    lab.add("org/app", {"a.txt": "a\n"})
    await run_sweep(lab.deps())
    key = lab.folder / "fingerprint.key"
    assert key.stat().st_mode & 0o777 == 0o600
    assert len(key.read_bytes()) == 32
    assert key.read_bytes().hex() not in (lab.folder / STATE_FILE).read_text()


# ---------------------------------------------------------------------------
# Sorting retried after an outage (Codex R5)


@pytest.mark.asyncio
async def test_pending_findings_are_retried_after_a_model_outage(lab: Lab) -> None:
    lab.add("org/app", {"k.sh": f"K={planted_key()}\n"})
    lab.model.down = True
    first = await run_sweep(lab.deps())
    assert first.projects[0].pending == 1
    assert lab.rows() == []
    assert "1 waiting to be sorted (the model could not be asked)" in lab.slack.lines[-1]

    lab.model.down = False
    second = await run_sweep(lab.deps())
    assert second.projects[0].pending == 0
    assert len(lab.rows()) == 1


@pytest.mark.asyncio
async def test_an_unclear_answer_leaves_the_finding_pending(lab: Lab) -> None:
    lab.add("org/app", {"k.sh": f"K={planted_key()}\n"})
    lab.model.answer = lambda prompt: "probably"
    report = await run_sweep(lab.deps())
    assert report.projects[0].pending == 1
    assert "not a clear yes or no" in lab.slack.lines[-1]


@pytest.mark.asyncio
async def test_no_model_route_leaves_findings_pending_and_says_so(lab: Lab) -> None:
    lab.add("org/app", {"k.sh": f"K={planted_key()}\n"})
    await run_sweep(lab.deps(asker=None, asker_missing="the model is not configured"))
    assert "1 waiting to be sorted (the model is not configured)" in lab.slack.lines[-1]


# ---------------------------------------------------------------------------
# Idle, busy and the lock (Codex R6)


@pytest.mark.asyncio
async def test_a_build_starting_stops_the_sweep_after_the_current_project(lab: Lab) -> None:
    lab.add("org/aaa", {"a.txt": "a\n"})
    lab.add("org/bbb", {"b.txt": "b\n"})
    lab.add("org/ccc", {"c.txt": "c\n"})
    # Idle for the first project's checks, busy when the second would start.
    lab.idle = [False]
    report = await run_sweep(lab.deps())
    assert [r.project for r in report.projects] == ["org/aaa"]
    assert report.left_for_next == ["org/bbb", "org/ccc"]
    assert report.outcome == "stopped-busy"
    assert lab.slack.lines[-1].endswith(
        "Left for tomorrow night because a build started: bbb, ccc."
    )


@pytest.mark.asyncio
async def test_the_model_is_not_asked_once_the_factory_is_busy(lab: Lab) -> None:
    lab.add("org/app", {"k.sh": f"K={planted_key()}\n"})
    lab.idle = [False]
    report = await run_sweep(lab.deps())
    assert lab.model.prompts == []
    assert report.projects[0].pending == 1
    assert "a build started, so the model was left to it" in lab.slack.lines[-1]


@pytest.mark.asyncio
async def test_a_second_sweep_does_not_start_while_one_runs(lab: Lab) -> None:
    lab.add("org/app", {"a.txt": "a\n"})
    lab.folder.mkdir(parents=True)
    with SweepLock(lab.folder):
        with pytest.raises(SweepAlreadyRunning):
            await run_sweep(lab.deps())
    assert lab.slack.lines == []
    await run_sweep(lab.deps())  # the lock is free again


# ---------------------------------------------------------------------------
# The Slack line (step 8)


@pytest.mark.asyncio
async def test_silence_when_nothing_changed(lab: Lab) -> None:
    lab.scanners.advisories = [{"package": "x", "version": "1", "id": "GHSA-low", "score": 5.0}]
    lab.add("org/app", {"package-lock.json": LOCK})
    await run_sweep(lab.deps())
    assert len(lab.slack.lines) == 1  # the first night: one new finding
    await run_sweep(lab.deps())
    assert len(lab.slack.lines) == 1  # nothing changed: silent


@pytest.mark.asyncio
async def test_a_clean_project_is_silent_and_the_weekly_all_clear_speaks_once(lab: Lab) -> None:
    lab.add("org/app", {"package-lock.json": LOCK})
    await run_sweep(lab.deps())
    assert lab.slack.lines == []
    await run_sweep(lab.deps(weekly_all_clear="monday"))
    assert lab.slack.lines == ["Nightly security check, weekly all clear: app — nothing new."]
    await run_sweep(lab.deps(weekly_all_clear="monday"))
    assert len(lab.slack.lines) == 1  # once that day
    lab.now[0] = lab.now[0] + timedelta(days=1)
    await run_sweep(lab.deps(weekly_all_clear="monday"))
    assert len(lab.slack.lines) == 1  # not on a Tuesday


@pytest.mark.asyncio
async def test_the_line_names_a_project_that_could_not_be_fetched_or_checked(lab: Lab) -> None:
    lab.add("org/api_test", {"pyproject.toml": "[project]\nname='x'\n"})
    projects = {
        "org/api_test": "https://example.invalid/org/api_test",
        "org/broken": "https://example.invalid/org/broken",
    }
    await run_sweep(lab.deps(projects=projects))
    (line,) = lab.slack.lines
    assert line == (
        "Nightly security check: api_test — dependencies can't be checked (no lockfile); "
        "broken — could not be fetched, so it was not checked."
    )


@pytest.mark.asyncio
async def test_a_mirror_whose_files_cannot_be_read_is_said_plainly(lab: Lab) -> None:
    project = lab.add("org/app", {"a.txt": "a\n"})
    import shutil

    shutil.rmtree(project.mirror)
    project.mirror.mkdir()
    report = await run_sweep(lab.deps())
    assert report.outcome == "finished"
    assert report.projects[0].unreadable
    assert "app — its files could not be read from the fetched copy" in lab.slack.lines[-1]


@pytest.mark.asyncio
async def test_drafted_findings_still_present_after_seven_days_are_listed(lab: Lab) -> None:
    lab.scanners.advisories = [VITEST]
    lab.add("org/app", {"package-lock.json": LOCK})
    await run_sweep(lab.deps())
    (row,) = lab.rows()
    lab.now[0] = lab.now[0] + timedelta(days=7)
    lab.add("org/zzz", {"k.sh": f"K={planted_key()}\n"})  # something new, so a line is posted
    lab.model.answer = lambda prompt: '{"real": false}'
    await run_sweep(lab.deps())
    assert (
        f"app — findings from repair request #{row['id']} are still present after 7 days "
        "(1 of them);" in lab.slack.lines[-1] + ";"
    )


@pytest.mark.asyncio
async def test_the_slack_line_and_history_carry_allowlisted_fields_only(lab: Lab) -> None:
    lab.scanners.advisories = [VITEST]
    lab.add("org/app", {"package-lock.json": LOCK, "app/run.py": "import subprocess\nsubprocess.call(c, shell=True)\n"})
    await run_sweep(lab.deps())
    history = [json.loads(line) for line in (lab.folder / "history.jsonl").read_text().splitlines()]
    (entry,) = history
    assert set(entry) <= {"at", "outcome", "posted", "projects", "left_for_next", "failure"}
    assert entry["projects"]["org/app"]["drafted_row"] == int(lab.rows()[0]["id"])
    for record in _state(lab)["findings"].values():
        assert set(record) <= {
            "project", "kind", "tool", "rule", "file", "line", "message", "severity",
            "package", "version", "ecosystem", "advisory", "score", "fixed", "path_kinds",
            "status", "first_seen", "last_seen", "sorted_at", "queue_id", "drafted_at",
            "gone_at",
        }


@pytest.mark.asyncio
async def test_drafting_switched_off_reports_without_filing(lab: Lab) -> None:
    lab.scanners.advisories = [VITEST]
    lab.add("org/app", {"package-lock.json": LOCK})
    await run_sweep(lab.deps(draft_repairs=False))
    assert lab.rows() == []
    assert "1 serious problem, no repair request drafted (drafting is switched off)" in lab.slack.lines[-1]


# ---------------------------------------------------------------------------
# The factory's scanner settings (Codex R9) and the export (Codex R12)


@pytest.mark.asyncio
async def test_the_nightly_run_passes_disable_nosem_and_the_factory_osv_settings(lab: Lab) -> None:
    """R9: ``--disable-nosem`` and the factory's own OSV settings on every run."""
    lab.add("org/app", {"a.py": "x = 1\n", "osv-scanner.toml": "[[IgnoredVulns]]\nid='GHSA-x'\n"})
    await run_sweep(lab.deps())
    by_tool = {(call[0], call[1]): call for call in lab.scanners.calls}
    opengrep = by_tool[("/fake/opengrep", "scan")]
    assert "--disable-nosem" in opengrep
    osv = by_tool[("/fake/osv-scanner", "scan")]
    assert osv[osv.index("--config") + 1] == str(lab.setup.osv_config)
    for mode in ("dir", "git"):
        gitleaks = by_tool[("/fake/gitleaks", mode)]
        assert "--redact" in gitleaks and "--ignore-gitleaks-allow" in gitleaks
        assert gitleaks[gitleaks.index("--gitleaks-ignore-path") + 1] == str(
            lab.setup.gitleaks_ignore_dir
        )


@pytest.mark.asyncio
async def test_export_ignore_cannot_hide_a_lockfile_or_a_compose_file(lab: Lab) -> None:
    """R12: ``.gitattributes`` marks a vulnerable lockfile and a privileged
    Compose file ``export-ignore``; both are still scanned and found."""
    lab.scanners.advisories = [VITEST]
    lab.add(
        "org/app",
        {
            ".gitattributes": "web/package-lock.json export-ignore\ncompose.yaml export-ignore\n",
            "web/package-lock.json": LOCK,
            "compose.yaml": "services:\n  app:\n    image: x\n    privileged: true\n",
        },
    )
    await run_sweep(lab.deps())
    findings = _state(lab)["findings"].values()
    assert {(e["kind"], e["file"]) for e in findings} >= {
        ("dependency", "web/package-lock.json"),
        ("code", "compose.yaml"),
    }
    rules = {e["rule"] for e in findings if e["kind"] == "code"}
    # The factory's Compose rule, not the community one it replaces.
    assert rules == {"factory-compose-privileged"}


# ---------------------------------------------------------------------------
# Fix pass, 17 October 2026


@pytest.mark.asyncio
async def test_a_clean_lockfile_is_not_reported_as_no_lockfile(lab: Lab) -> None:
    """Fix 1: with --all-packages a lockfile with no advisories is still read."""
    lab.add("org/clean", {"package-lock.json": LOCK})
    lab.add("org/empty", {"README.md": "nothing to install\n"})
    report = await run_sweep(lab.deps())
    by_project = {r.project: r for r in report.projects}
    assert not by_project["org/clean"].no_lockfile
    assert by_project["org/empty"].no_lockfile
    osv = next(call for call in lab.scanners.calls if call[0] == "/fake/osv-scanner")
    assert "--all-packages" in osv


@pytest.mark.asyncio
async def test_control_characters_in_a_file_name_never_reach_outward_text(lab: Lab) -> None:
    """Fix 4: a file named weird<newline>name.py is shown with a visible escape."""
    lab.add("org/app", {"weird\nname.py": "import subprocess\nsubprocess.call(c, shell=True)\n"})
    await run_sweep(lab.deps())
    (row,) = lab.rows()
    assert "weird\\x0aname.py" in row["sentence"]
    assert "\n" not in row["sentence"]
    for line in lab.slack.lines:
        assert "\n" not in line
    state = (lab.folder / STATE_FILE).read_text()
    assert "weird\\\\x0aname.py" in state  # the escape, written as JSON
    assert "weird\\nname" not in state
    # The model still saw the code: the raw path was used to read it.
    assert any("subprocess.call(c, shell=True)" in p for p in lab.model.prompts)


@pytest.mark.asyncio
async def test_a_finished_repair_that_did_not_fix_is_drafted_again_once(lab: Lab) -> None:
    """Decision 3: a DONE row whose work has ended, with its finding still
    present at the next sweep, gives one new row; not a second for the same
    closure."""
    lab.scanners.advisories = [VITEST]
    lab.add("org/app", {"package-lock.json": LOCK})
    await run_sweep(lab.deps())
    (first,) = lab.rows()
    lab.queue.close(int(first["id"]), status="DONE", actor_identity="test")

    await run_sweep(lab.deps())
    rows = lab.rows()
    assert len(rows) == 2
    second = rows[1]
    assert second["correlation_id"] != first["correlation_id"]
    details = lab.queue.origin_details(int(second["id"]))
    assert details["fingerprints"] == lab.queue.origin_details(int(first["id"]))["fingerprints"]
    assert details["drafted_again_after"] == {details["fingerprints"][0]: int(first["id"])}
    assert "repair request sent" in lab.slack.lines[-1]

    # The same closure never drafts it a third time, even with the state lost.
    await run_sweep(lab.deps())
    (lab.folder / STATE_FILE).unlink()
    await run_sweep(lab.deps())
    assert len(lab.rows()) == 2


@pytest.mark.asyncio
async def test_a_handed_over_repair_still_building_is_waited_for(lab: Lab) -> None:
    """A DONE row whose build has not landed yet is still open: nothing new."""
    from forge.planning.run_store import SqlitePlanningRunStore

    lab.scanners.advisories = [VITEST]
    lab.add("org/app", {"package-lock.json": LOCK})
    await run_sweep(lab.deps())
    (row,) = lab.rows()
    runs = SqlitePlanningRunStore(lab.queue.connection)
    runs.record_queued(
        correlation_id=row["correlation_id"], originating_user="rich",
        expected_approver="rich", request_text="x", triggered_by="forge-internal",
    )
    lab.queue.connection.execute(
        "UPDATE planning_runs SET state = 'BUILD_QUEUED' WHERE correlation_id = ?",
        (row["correlation_id"],),
    )
    lab.queue.close(int(row["id"]), status="DONE", actor_identity="test")
    report = await run_sweep(lab.deps())
    assert len(lab.rows()) == 1
    assert report.projects[0].serious_eligible == 0


@pytest.mark.asyncio
async def test_a_declined_repair_is_never_drafted_again_and_is_said(lab: Lab) -> None:
    """Decision 3: WITHDRAWN (or BLOCKED) never redrafts; the line says so."""
    lab.scanners.advisories = [VITEST]
    lab.add("org/app", {"package-lock.json": LOCK})
    await run_sweep(lab.deps())
    (row,) = lab.rows()
    lab.queue.drop(int(row["id"]), actor_identity="rich")
    lab.add("org/zzz", {"k.sh": f"K={planted_key()}\n"})  # something new, so a line is posted
    lab.model.answer = lambda prompt: '{"real": false}'
    for _ in range(2):
        await run_sweep(lab.deps())
    assert len(lab.rows()) == 1
    assert (
        f"app — 1 finding still present; repair request #{row['id']} was declined"
        in lab.slack.lines[1]
    )
    (lab.folder / STATE_FILE).unlink()
    await run_sweep(lab.deps())
    assert len(lab.rows()) == 1


@pytest.mark.asyncio
async def test_a_finished_repair_that_fixed_it_leaves_the_finding_gone(lab: Lab) -> None:
    lab.scanners.advisories = [VITEST]
    project = lab.add("org/app", {"package-lock.json": LOCK})
    await run_sweep(lab.deps())
    (row,) = lab.rows()
    lab.queue.close(int(row["id"]), status="DONE", actor_identity="test")
    lab.scanners.advisories = []
    project.write({"package-lock.json": LOCK.replace("web", "web2")})
    project.commit()
    await run_sweep(lab.deps())
    assert len(lab.rows()) == 1
    assert [e["status"] for e in _state(lab)["findings"].values()] == ["gone"]


@pytest.mark.asyncio
async def test_files_too_large_to_scan_are_said_not_checked(lab: Lab, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fix 6: a file over the size limit leaves a trace in the line and history."""
    from forge.security_sweep import repository

    monkeypatch.setattr(repository, "LARGEST_FILE_BYTES", 100)
    lab.add("org/app", {"big.bin": "x" * 500, "k.sh": f"K={planted_key()}\n"})
    lab.model.answer = lambda prompt: '{"real": false}'
    await run_sweep(lab.deps())
    assert "1 file not checked: too large" in lab.slack.lines[-1]
    history = json.loads((lab.folder / "history.jsonl").read_text().splitlines()[-1])
    assert history["projects"]["org/app"]["too_large"] == ["big.bin"]


@pytest.mark.asyncio
async def test_a_hard_coded_value_gitleaks_misses_never_reaches_the_model(lab: Lab) -> None:
    """A value only a CWE-798 Opengrep rule flags is hidden from the prompt
    whatever Gitleaks says: the rule's matched lines are always hidden."""
    value = "kq" + "Zt7mW3pLx9vRb2"  # not a shape the (fake) Gitleaks knows
    lab.add(
        "org/app",
        {"app/auth.py": f'import jwt\n\ndef token(p):\n    return jwt.encode(p, "{value}", "HS256")\n'},
    )
    await run_sweep(lab.deps())
    (prompt,) = lab.model.prompts
    assert "rule: hardcoded-jwt-secret" in prompt
    assert "4: [line hidden: may hold a secret]" in prompt
    assert "def token(p):" in prompt  # the rest of the excerpt is still shown
    assert value not in prompt
    assert value not in lab.every_stored_text()
