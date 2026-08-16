"""The routing law's close-side check — ``forge.pipeline.routing_stamps``.

Card Q8/A.2 second half: *a stamped verifier that did not run is ABSENT, and
ABSENT is UNKNOWN, and UNKNOWN publishes no merge card.* These tests drive
the pure decision home by home, then the readers over real files and a real
(tiny) git repo, then the composed leg over a fixture repo.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from forge.pipeline.routing_stamps import (
    CONFIG_RELATIVE_PATH,
    HISTORY_RELATIVE_PATH,
    HOME_GATE_IDS,
    ROUTING_LAW_KEY,
    ROUTING_LAW_VALUES,
    VERIFIER_HOMES,
    Envelope,
    RoutingLawResolution,
    ScenarioStamp,
    StampsRead,
    StampsStatus,
    evaluate_stamps,
    make_stamps_leg,
    read_last_code_commit_time,
    read_newest_envelope,
    read_repo_routing_law,
    read_scenario_stamps,
    resolve_routing_law,
    resolve_routing_law_enforcement,
)

T0 = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)
HISTORY = Path("/wt/qa/gates/history")


def _read(*stamps: tuple[str, str], present: bool = True, error: str | None = None) -> StampsRead:
    return StampsRead(
        path=Path("/canonical/.guardkit/features/FEAT-X.yaml"),
        present=present,
        stamps=tuple(ScenarioStamp(title=t, verifier=v) for t, v in stamps),
        error=error,
    )


def _envelope(
    *,
    verdict: str = "pass",
    gates: dict[str, int | None] | None = None,
    started: datetime | None = T0 + timedelta(minutes=5),
    run_id: str = "FEAT-X-local-20260815T120500Z",
) -> Envelope:
    return Envelope(
        path=HISTORY / f"{run_id}.json",
        run_id=run_id,
        verdict=verdict,
        started=started,
        gates=gates if gates is not None else {"health": 0, "hurl-twins": 0},
    )


def _eval(read: StampsRead, **over: Any):
    kwargs: dict[str, Any] = dict(
        toolchain_green=True,
        envelope=_envelope(),
        code_commit_time=T0,
        history_dir=HISTORY,
        feature_id="FEAT-X",
    )
    kwargs.update(over)
    return evaluate_stamps(read, **kwargs)


# ---------------------------------------------------------------------------
# The closed list is guardkit's — mirrored, and checked against the original
# ---------------------------------------------------------------------------


class TestTheClosedList:
    def test_the_mirror_carries_the_eight_homes(self) -> None:
        assert VERIFIER_HOMES == (
            "toolchain",
            "hurl",
            "exam",
            "probe:bus",
            "probe:process",
            "flutter",
            "playwright",
            "operator",
        )

    def test_every_envelope_backed_home_names_its_gate_ids(self) -> None:
        envelope_backed = set(VERIFIER_HOMES) - {"toolchain", "operator"}
        assert set(HOME_GATE_IDS) == envelope_backed
        assert HOME_GATE_IDS["hurl"] == ("hurl-twins",)

    def test_the_mirror_matches_guardkit_when_guardkit_is_importable(self) -> None:
        guardkit_stamp = pytest.importorskip(
            "guardkit.orchestrator.verifier_stamp",
            reason="guardkit is not installed in this interpreter (forge has no dependency on it)",
        )
        assert tuple(guardkit_stamp.VERIFIER_HOMES) == VERIFIER_HOMES
        # the enforcement flag's mirror too (condition 5): same file, same key,
        # same closed values as guardkit's plan-load half
        assert Path(guardkit_stamp.CONFIG_RELATIVE_PATH) == CONFIG_RELATIVE_PATH
        assert guardkit_stamp.ROUTING_LAW_KEY == ROUTING_LAW_KEY
        assert tuple(guardkit_stamp.ROUTING_LAW_VALUES) == ROUTING_LAW_VALUES

    def test_the_flag_mirror_is_guardkits_by_value(self) -> None:
        assert CONFIG_RELATIVE_PATH == Path(".guardkit") / "config.yaml"
        assert ROUTING_LAW_KEY == "routing_law"
        assert ROUTING_LAW_VALUES == ("enforced", "off")


# ---------------------------------------------------------------------------
# The routing law's ENFORCEMENT resolver (coordinator condition 5, 08-16):
# feature-level flag wins → repo .guardkit/config.yaml → off. Forge only READS.
# ---------------------------------------------------------------------------


def _write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


class TestResolveRoutingLaw:
    def test_absent_everywhere_is_off_by_default(self, tmp_path: Path) -> None:
        _write(tmp_path, ".guardkit/features/FEAT-X.yaml", "id: FEAT-X\ntasks: []\n")
        res = resolve_routing_law(tmp_path, "FEAT-X")
        assert res == RoutingLawResolution(
            enforcement="off",
            source="default",
            detail=res.detail,
        )
        assert not res.enforced and not res.invalid
        assert "does not enforce the routing law yet" in res.detail
        assert resolve_routing_law_enforcement(tmp_path, "FEAT-X") == "off"

    def test_no_feature_yaml_and_no_config_is_off(self, tmp_path: Path) -> None:
        assert resolve_routing_law_enforcement(tmp_path, "FEAT-NOPE") == "off"

    def test_repo_enforced_is_honoured(self, tmp_path: Path) -> None:
        _write(tmp_path, ".guardkit/config.yaml", "toolchain:\n  test: pytest\nrouting_law: enforced\n")
        _write(tmp_path, ".guardkit/features/FEAT-X.yaml", "id: FEAT-X\n")
        res = resolve_routing_law(tmp_path, "FEAT-X")
        assert res.enforcement == "enforced" and res.source == "repo" and res.enforced
        assert ".guardkit/config.yaml says so" in res.detail
        assert resolve_routing_law_enforcement(tmp_path, "FEAT-X") == "enforced"

    def test_repo_off_is_off(self, tmp_path: Path) -> None:
        # YAML 1.1: the bare token `off` parses as False — absorbed, read as "off"
        _write(tmp_path, ".guardkit/config.yaml", "routing_law: off\n")
        assert read_repo_routing_law(tmp_path) == ("off", False)
        res = resolve_routing_law(tmp_path, "FEAT-X")
        assert res.enforcement == "off" and res.source == "repo"

    def test_feature_level_off_wins_over_repo_enforced(self, tmp_path: Path) -> None:
        _write(tmp_path, ".guardkit/config.yaml", "routing_law: enforced\n")
        _write(tmp_path, ".guardkit/features/FEAT-X.yaml", "id: FEAT-X\nrouting_law: off\n")
        res = resolve_routing_law(tmp_path, "FEAT-X")
        assert res.enforcement == "off" and res.source == "feature"
        assert "wins over the repo flag" in res.detail
        assert resolve_routing_law_enforcement(tmp_path, "FEAT-X") == "off"

    def test_feature_level_enforced_wins_over_repo_off_and_over_silence(self, tmp_path: Path) -> None:
        _write(tmp_path, ".guardkit/config.yaml", "routing_law: off\n")
        _write(tmp_path, ".guardkit/features/FEAT-X.yaml", "id: FEAT-X\nrouting_law: enforced\n")
        assert resolve_routing_law(tmp_path, "FEAT-X").source == "feature"
        assert resolve_routing_law_enforcement(tmp_path, "FEAT-X") == "enforced"
        (tmp_path / ".guardkit" / "config.yaml").unlink()
        assert resolve_routing_law_enforcement(tmp_path, "FEAT-X") == "enforced"

    def test_yml_suffix_is_read_too(self, tmp_path: Path) -> None:
        _write(tmp_path, ".guardkit/features/FEAT-X.yml", "id: FEAT-X\nrouting_law: enforced\n")
        assert resolve_routing_law_enforcement(tmp_path, "FEAT-X") == "enforced"

    def test_an_invalid_present_value_is_read_as_enforced_never_silently_off(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # guardkit RAISES on these at plan load; forge reads them as ENFORCED and
        # says so — a typo'd law flag must never un-enforce the law silently.
        _write(tmp_path, ".guardkit/config.yaml", "routing_law: enforce\n")
        with caplog.at_level("WARNING", logger="forge.pipeline.routing_stamps"):
            res = resolve_routing_law(tmp_path, "FEAT-X")
        assert res.enforcement == "enforced" and res.source == "repo" and res.invalid
        assert "never as silently off" in res.detail
        assert any("invalid" in r.getMessage() for r in caplog.records)
        # the YAML boolean trap the other way (`on`/`true`/`yes` → True)
        _write(tmp_path, ".guardkit/features/FEAT-X.yaml", "id: FEAT-X\nrouting_law: on\n")
        res2 = resolve_routing_law(tmp_path, "FEAT-X")
        assert res2.enforcement == "enforced" and res2.source == "feature" and res2.invalid

    def test_an_unreadable_config_is_unset_never_a_crash(self, tmp_path: Path) -> None:
        _write(tmp_path, ".guardkit/config.yaml", "routing_law: [unclosed\n")
        assert read_repo_routing_law(tmp_path) == (None, False)
        _write(tmp_path, ".guardkit/config.yaml", "- not\n- a mapping\n")
        assert read_repo_routing_law(tmp_path) == (None, False)
        assert resolve_routing_law_enforcement(tmp_path, "FEAT-X") == "off"

    def test_an_unparseable_feature_yaml_falls_through_to_the_repo(self, tmp_path: Path) -> None:
        _write(tmp_path, ".guardkit/features/FEAT-X.yaml", "routing_law: [unclosed\n")
        _write(tmp_path, ".guardkit/config.yaml", "routing_law: enforced\n")
        res = resolve_routing_law(tmp_path, "FEAT-X")
        assert res.enforcement == "enforced" and res.source == "repo"

    def test_the_resolver_never_writes(self, tmp_path: Path) -> None:
        cfg = _write(tmp_path, ".guardkit/config.yaml", "toolchain:\n  test: pytest\n")
        feat = _write(tmp_path, ".guardkit/features/FEAT-X.yaml", "id: FEAT-X\n")
        before = (cfg.read_bytes(), feat.read_bytes(), sorted(p.name for p in tmp_path.rglob("*")))
        resolve_routing_law(tmp_path, "FEAT-X")
        resolve_routing_law_enforcement(tmp_path, "FEAT-X")
        after = (cfg.read_bytes(), feat.read_bytes(), sorted(p.name for p in tmp_path.rglob("*")))
        assert before == after
        assert b"routing_law" not in cfg.read_bytes() and b"routing_law" not in feat.read_bytes()


# ---------------------------------------------------------------------------
# The pure decision, home by home
# ---------------------------------------------------------------------------


class TestNoStampsMeansNotEnforced:
    def test_no_feature_yaml_is_not_enforced(self) -> None:
        verdict = _eval(_read(present=False))
        assert verdict.status is StampsStatus.NOT_ENFORCED
        assert verdict.blocks_card is False
        assert "not enforced" in verdict.detail

    def test_a_feature_yaml_with_no_scenarios_map_is_not_enforced(self) -> None:
        verdict = _eval(_read())
        assert verdict.status is StampsStatus.NOT_ENFORCED
        assert verdict.blocks_card is False
        assert "no scenario stamps" in verdict.detail


class TestToolchainHome:
    def test_a_green_suite_satisfies_a_toolchain_stamp(self) -> None:
        verdict = _eval(_read(("Rate limiter refuses the 6th attempt", "toolchain")))
        assert verdict.status is StampsStatus.SATISFIED
        assert verdict.satisfied_by_home == {"toolchain": 1}
        assert verdict.missing == ()

    def test_a_non_green_suite_leaves_a_toolchain_stamp_absent(self) -> None:
        verdict = _eval(
            _read(("Rate limiter refuses the 6th attempt", "toolchain")),
            toolchain_green=False,
        )
        assert verdict.status is StampsStatus.ABSENT
        assert verdict.missing == (("Rate limiter refuses the 6th attempt", "toolchain"),)


class TestHurlHome:
    SCENARIO = "User signs in with valid credentials"

    def test_a_fresh_green_envelope_naming_hurl_twins_exit_0_satisfies(self) -> None:
        verdict = _eval(_read((self.SCENARIO, "hurl")))
        assert verdict.status is StampsStatus.SATISFIED
        assert verdict.satisfied_by_home == {"hurl": 1}
        assert "FEAT-X-local-20260815T120500Z" in verdict.detail

    def test_no_envelope_at_all_is_absent_and_names_the_scenario_and_home(self) -> None:
        verdict = _eval(_read((self.SCENARIO, "hurl")), envelope=None)
        assert verdict.status is StampsStatus.ABSENT
        assert verdict.blocks_card is True
        assert verdict.missing == ((self.SCENARIO, "hurl"),)
        assert repr(self.SCENARIO) in verdict.detail
        assert "`verifier: hurl`" in verdict.detail
        assert "no results envelope exists under" in verdict.detail
        assert "no merge card" in verdict.detail

    def test_the_newest_envelope_failing_is_absent(self) -> None:
        verdict = _eval(
            _read((self.SCENARIO, "hurl")),
            envelope=_envelope(verdict="fail", gates={"hurl-twins": 1}),
        )
        assert verdict.status is StampsStatus.ABSENT
        assert "verdict `fail`, not `pass`" in verdict.detail

    def test_a_green_envelope_that_names_no_hurl_twins_gate_is_absent(self) -> None:
        verdict = _eval(
            _read((self.SCENARIO, "hurl")),
            envelope=_envelope(gates={"health": 0, "stats": 0}),
        )
        assert verdict.status is StampsStatus.ABSENT
        assert "names no `hurl-twins` gate" in verdict.detail
        assert "health, stats" in verdict.detail

    def test_hurl_twins_with_a_non_zero_exit_is_absent_even_on_a_pass_verdict(self) -> None:
        # Belt and braces: the verdict is the runner's; the gate's own exit
        # code is the law (exit 0 = green) and both must agree.
        verdict = _eval(
            _read((self.SCENARIO, "hurl")),
            envelope=_envelope(gates={"hurl-twins": 2}),
        )
        assert verdict.status is StampsStatus.ABSENT
        assert "exit code is 2, not 0" in verdict.detail

    def test_a_stale_envelope_is_absent(self) -> None:
        verdict = _eval(
            _read((self.SCENARIO, "hurl")),
            envelope=_envelope(started=T0 - timedelta(hours=1)),
        )
        assert verdict.status is StampsStatus.ABSENT
        assert "STALE" in verdict.detail
        assert "before the branch's last code commit" in verdict.detail

    def test_an_envelope_started_exactly_at_the_commit_is_fresh(self) -> None:
        verdict = _eval(_read((self.SCENARIO, "hurl")), envelope=_envelope(started=T0))
        assert verdict.status is StampsStatus.SATISFIED

    def test_an_unreadable_commit_time_cannot_prove_freshness(self) -> None:
        verdict = _eval(_read((self.SCENARIO, "hurl")), code_commit_time=None)
        assert verdict.status is StampsStatus.ABSENT
        assert "last commit time could not be read" in verdict.detail

    def test_an_envelope_with_no_started_time_cannot_prove_freshness(self) -> None:
        verdict = _eval(_read((self.SCENARIO, "hurl")), envelope=_envelope(started=None))
        assert verdict.status is StampsStatus.ABSENT
        assert "no readable `started` time" in verdict.detail

    def test_two_hurl_scenarios_share_one_reason_but_are_both_named(self) -> None:
        verdict = _eval(
            _read(("Sign in", "hurl"), ("Sign out", "hurl")),
            envelope=None,
        )
        assert verdict.missing == (("Sign in", "hurl"), ("Sign out", "hurl"))
        assert "'Sign in'" in verdict.detail and "'Sign out'" in verdict.detail


class TestOperatorHome:
    def test_operator_is_satisfied_by_declaration_and_listed_as_attended(self) -> None:
        verdict = _eval(_read(("Owner sees the card in Slack", "operator")))
        assert verdict.status is StampsStatus.SATISFIED
        assert verdict.attended == ("Owner sees the card in Slack",)
        assert "ATTENDED" in verdict.detail
        assert "'Owner sees the card in Slack'" in verdict.detail
        assert "verified by a human, not by the machinery" in verdict.detail

    def test_attended_is_listed_even_when_another_home_blocks(self) -> None:
        verdict = _eval(
            _read(("Owner sees the card", "operator"), ("Sign in", "hurl")),
            envelope=None,
        )
        assert verdict.status is StampsStatus.ABSENT
        assert verdict.attended == ("Owner sees the card",)
        assert "ATTENDED" in verdict.detail


class TestTheOtherEnvelopeBackedHomes:
    @pytest.mark.parametrize("home", ["exam", "probe:bus", "probe:process", "flutter", "playwright"])
    def test_absent_unless_a_green_envelope_names_the_home(self, home: str) -> None:
        verdict = _eval(_read((f"{home} scenario", home)))  # envelope names health + hurl-twins only
        assert verdict.status is StampsStatus.ABSENT
        assert verdict.missing == ((f"{home} scenario", home),)
        assert f"`verifier: {home}`" in verdict.detail

    @pytest.mark.parametrize(
        ("home", "gate_id"),
        [
            ("exam", "exam"),
            ("probe:bus", "probe:bus"),
            ("probe:bus", "probe-bus"),
            ("probe:process", "probe-process"),
            ("flutter", "flutter"),
            ("playwright", "playwright"),
        ],
    )
    def test_satisfied_when_a_fresh_green_envelope_names_the_home_exit_0(
        self, home: str, gate_id: str
    ) -> None:
        verdict = _eval(
            _read((f"{home} scenario", home)),
            envelope=_envelope(gates={gate_id: 0}),
        )
        assert verdict.status is StampsStatus.SATISFIED
        assert verdict.satisfied_by_home == {home: 1}

    def test_a_named_home_with_a_non_zero_exit_is_absent(self) -> None:
        verdict = _eval(_read(("The exam", "exam")), envelope=_envelope(gates={"exam": 1}))
        assert verdict.status is StampsStatus.ABSENT


class TestUnreadableStamps:
    def test_an_error_on_the_read_blocks_the_card_and_says_why(self) -> None:
        verdict = _eval(_read(error="FEAT-X.yaml could not be parsed as YAML (ScannerError: x)"))
        assert verdict.status is StampsStatus.UNREADABLE
        assert verdict.blocks_card is True
        assert "could not be read" in verdict.detail
        assert "ScannerError" in verdict.detail


class TestAMixedFeature:
    def test_every_home_reports_in_one_detail(self) -> None:
        verdict = _eval(
            _read(
                ("Rate limiter refuses the 6th attempt", "toolchain"),
                ("User signs in with valid credentials", "hurl"),
                ("Owner reads the digest card", "operator"),
            )
        )
        assert verdict.status is StampsStatus.SATISFIED
        assert verdict.satisfied_by_home == {"toolchain": 1, "hurl": 1, "operator": 1}
        assert "all 3 stamped scenario(s)" in verdict.detail
        assert "ATTENDED" in verdict.detail


# ---------------------------------------------------------------------------
# The readers, over real files
# ---------------------------------------------------------------------------


class TestReadScenarioStamps:
    def test_a_missing_file_is_not_present(self, tmp_path: Path) -> None:
        read = read_scenario_stamps(tmp_path / "FEAT-NOPE.yaml")
        assert read.present is False and read.stamps == () and read.error is None

    def test_bare_string_and_mapping_shorthands_both_read(self, tmp_path: Path) -> None:
        f = tmp_path / "FEAT-X.yaml"
        f.write_text(
            "id: FEAT-X\n"
            "routing_law: enforced\n"
            "scenarios:\n"
            "  'User signs in with valid credentials': hurl\n"
            "  'Rate limiter refuses the 6th attempt':\n"
            "    verifier: toolchain\n"
            "    test_ref: test_rate_limiter_refuses_sixth\n"
            "  'Owner reads the card':\n"
            "    verifier: operator\n"
        )
        read = read_scenario_stamps(f)
        assert read.present and read.error is None
        assert read.routing_law == "enforced"
        assert read.stamps == (
            ScenarioStamp("User signs in with valid credentials", "hurl"),
            ScenarioStamp(
                "Rate limiter refuses the 6th attempt",
                "toolchain",
                "test_rate_limiter_refuses_sixth",
            ),
            ScenarioStamp("Owner reads the card", "operator"),
        )

    def test_routing_law_off_absorbs_the_yaml_boolean_trap(self, tmp_path: Path) -> None:
        f = tmp_path / "FEAT-X.yaml"
        f.write_text("routing_law: off\nscenarios: {}\n")
        read = read_scenario_stamps(f)
        assert read.routing_law == "off"
        assert read.stamps == ()

    def test_no_scenarios_key_reads_as_no_stamps(self, tmp_path: Path) -> None:
        f = tmp_path / "FEAT-X.yaml"
        f.write_text("id: FEAT-X\ntasks: []\n")
        read = read_scenario_stamps(f)
        assert read.present and read.stamps == () and read.error is None

    def test_an_unknown_home_is_an_error_never_a_pass(self, tmp_path: Path) -> None:
        f = tmp_path / "FEAT-X.yaml"
        f.write_text("scenarios:\n  'A': cypress\n")
        read = read_scenario_stamps(f)
        assert read.error is not None
        assert "cypress" in read.error and "closed list" in read.error

    def test_a_scenario_with_no_verifier_key_is_an_error(self, tmp_path: Path) -> None:
        f = tmp_path / "FEAT-X.yaml"
        f.write_text("scenarios:\n  'A':\n    test_ref: t\n")
        read = read_scenario_stamps(f)
        assert read.error is not None and "no `verifier:` home" in read.error

    def test_a_non_mapping_scenarios_block_is_an_error(self, tmp_path: Path) -> None:
        f = tmp_path / "FEAT-X.yaml"
        f.write_text("scenarios:\n  - hurl\n")
        read = read_scenario_stamps(f)
        assert read.error is not None and "must be a mapping" in read.error

    def test_unparseable_yaml_is_an_error(self, tmp_path: Path) -> None:
        f = tmp_path / "FEAT-X.yaml"
        f.write_text("scenarios: [unclosed\n")
        read = read_scenario_stamps(f)
        assert read.present and read.error is not None


def _write_envelope(
    history: Path,
    run_id: str,
    *,
    started: str,
    verdict: str = "pass",
    gates: dict[str, int] | None = None,
) -> Path:
    history.mkdir(parents=True, exist_ok=True)
    payload = {
        "format_version": "1.0",
        "run_id": run_id,
        "feature_id": "FEAT-X",
        "target_env": "local",
        "started": started,
        "finished": started,
        "preflight": {"checks": [], "instrument_ok": True},
        "gates": [
            {"gate_id": g, "exit_code": c, "assertions": []}
            for g, c in (gates if gates is not None else {"hurl-twins": 0}).items()
        ],
        "verdict": verdict,
    }
    out = history / f"{run_id}.json"
    out.write_text(json.dumps(payload))
    return out


class TestReadNewestEnvelope:
    def test_a_missing_history_dir_is_none(self, tmp_path: Path) -> None:
        assert read_newest_envelope(tmp_path / "qa" / "gates" / "history") is None

    def test_an_empty_history_dir_is_none(self, tmp_path: Path) -> None:
        (tmp_path / "history").mkdir()
        assert read_newest_envelope(tmp_path / "history") is None

    def test_the_newest_by_started_wins_regardless_of_filename(self, tmp_path: Path) -> None:
        h = tmp_path / "history"
        _write_envelope(h, "zzz-older", started="2026-08-15T10:00:00+00:00", verdict="fail")
        _write_envelope(h, "aaa-newer", started="2026-08-15T11:00:00Z")
        env = read_newest_envelope(h)
        assert env is not None
        assert env.run_id == "aaa-newer"
        assert env.verdict == "pass"
        assert env.gates == {"hurl-twins": 0}
        assert env.started == datetime(2026, 8, 15, 11, 0, tzinfo=timezone.utc)

    def test_a_broken_file_is_ignored_not_fatal(self, tmp_path: Path) -> None:
        h = tmp_path / "history"
        h.mkdir()
        (h / "broken.json").write_text("{not json")
        _write_envelope(h, "ok", started="2026-08-15T11:00:00+00:00")
        env = read_newest_envelope(h)
        assert env is not None and env.run_id == "ok"

    def test_only_broken_files_is_none(self, tmp_path: Path) -> None:
        h = tmp_path / "history"
        h.mkdir()
        (h / "broken.json").write_text("{not json")
        assert read_newest_envelope(h) is None


def _git(cwd: Path, *args: str, env_time: str | None = None) -> str:
    import os

    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        }
    )
    if env_time:
        env["GIT_AUTHOR_DATE"] = env_time
        env["GIT_COMMITTER_DATE"] = env_time
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, env=env
    ).stdout


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("print('v1')\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "code v1", env_time="2026-08-15T10:00:00+00:00")
    return repo


class TestReadLastCodeCommitTime:
    def test_reads_the_committer_time_of_the_last_commit(self, git_repo: Path) -> None:
        assert read_last_code_commit_time(git_repo) == datetime(
            2026, 8, 15, 10, 0, tzinfo=timezone.utc
        )

    def test_a_receipt_only_commit_does_not_move_the_code_clock(self, git_repo: Path) -> None:
        _write_envelope(
            git_repo / HISTORY_RELATIVE_PATH, "run-1", started="2026-08-15T10:30:00+00:00"
        )
        _git(git_repo, "add", ".")
        _git(git_repo, "commit", "-q", "-m", "land the envelope", env_time="2026-08-15T11:00:00+00:00")
        # Plain last commit is 11:00; the last CODE commit is still 10:00.
        assert read_last_code_commit_time(git_repo) == datetime(
            2026, 8, 15, 10, 0, tzinfo=timezone.utc
        )

    def test_a_later_code_commit_moves_it(self, git_repo: Path) -> None:
        (git_repo / "app.py").write_text("print('v2')\n")
        _git(git_repo, "add", ".")
        _git(git_repo, "commit", "-q", "-m", "code v2", env_time="2026-08-15T12:00:00+00:00")
        assert read_last_code_commit_time(git_repo, "main") == datetime(
            2026, 8, 15, 12, 0, tzinfo=timezone.utc
        )

    def test_not_a_git_repo_is_none(self, tmp_path: Path) -> None:
        assert read_last_code_commit_time(tmp_path) is None

    def test_an_unknown_branch_falls_back_to_the_worktree_head(self, git_repo: Path) -> None:
        # The build row's branch name may not resolve in the worktree (a
        # remote-tracking spelling, say); HEAD is the code the suite just ran
        # against, and is what the envelope must be fresh against.
        assert read_last_code_commit_time(git_repo, "no-such-branch") == datetime(
            2026, 8, 15, 10, 0, tzinfo=timezone.utc
        )


# ---------------------------------------------------------------------------
# The composed leg, over a fixture repo (canonical + worktree)
# ---------------------------------------------------------------------------


def _canonical_with_feature(tmp_path: Path, scenarios_yaml: str) -> Path:
    canonical = tmp_path / "canonical"
    features = canonical / ".guardkit" / "features"
    features.mkdir(parents=True)
    (features / "FEAT-X.yaml").write_text(f"id: FEAT-X\nname: x\n{scenarios_yaml}")
    return canonical


class TestTheComposedLeg:
    def test_missing_qa_dir_in_the_worktree_is_absent_for_hurl(
        self, tmp_path: Path, git_repo: Path
    ) -> None:
        canonical = _canonical_with_feature(
            tmp_path, "scenarios:\n  'User signs in': hurl\n"
        )
        leg = make_stamps_leg()
        verdict = leg(
            feature_id="FEAT-X",
            repo_root=canonical,
            worktree=git_repo,
            branch=None,
            toolchain_green=True,
        )
        assert verdict.status is StampsStatus.ABSENT
        assert "no results envelope exists under" in verdict.detail
        assert str(git_repo / HISTORY_RELATIVE_PATH) in verdict.detail

    def test_a_fresh_green_envelope_in_the_worktree_satisfies_hurl(
        self, tmp_path: Path, git_repo: Path
    ) -> None:
        canonical = _canonical_with_feature(
            tmp_path, "scenarios:\n  'User signs in': hurl\n  'Owner reads': operator\n"
        )
        _write_envelope(
            git_repo / HISTORY_RELATIVE_PATH, "run-fresh", started="2026-08-15T10:00:01+00:00"
        )
        verdict = make_stamps_leg()(
            feature_id="FEAT-X",
            repo_root=canonical,
            worktree=git_repo,
            branch="main",
            toolchain_green=True,
        )
        assert verdict.status is StampsStatus.SATISFIED
        assert verdict.attended == ("Owner reads",)
        assert "run-fresh" in verdict.detail

    def test_a_stale_envelope_in_the_worktree_is_absent(
        self, tmp_path: Path, git_repo: Path
    ) -> None:
        canonical = _canonical_with_feature(tmp_path, "scenarios:\n  'User signs in': hurl\n")
        _write_envelope(
            git_repo / HISTORY_RELATIVE_PATH, "run-stale", started="2026-08-15T09:00:00+00:00"
        )
        verdict = make_stamps_leg()(
            feature_id="FEAT-X",
            repo_root=canonical,
            worktree=git_repo,
            branch="main",
            toolchain_green=True,
        )
        assert verdict.status is StampsStatus.ABSENT
        assert "STALE" in verdict.detail

    def test_the_stamps_are_read_from_the_canonical_repo_not_the_worktree(
        self, tmp_path: Path, git_repo: Path
    ) -> None:
        # The worktree carries a "convenient" un-stamped feature YAML; the
        # canonical repo carries the plan of record. The plan of record wins.
        canonical = _canonical_with_feature(tmp_path, "scenarios:\n  'User signs in': hurl\n")
        wt_features = git_repo / ".guardkit" / "features"
        wt_features.mkdir(parents=True)
        (wt_features / "FEAT-X.yaml").write_text("id: FEAT-X\n")
        verdict = make_stamps_leg()(
            feature_id="FEAT-X",
            repo_root=canonical,
            worktree=git_repo,
            branch="main",
            toolchain_green=True,
        )
        assert verdict.status is StampsStatus.ABSENT

    def test_no_feature_yaml_anywhere_is_not_enforced(self, tmp_path: Path, git_repo: Path) -> None:
        verdict = make_stamps_leg()(
            feature_id="FEAT-X",
            repo_root=tmp_path / "canonical-empty",
            worktree=git_repo,
            branch="main",
            toolchain_green=True,
        )
        assert verdict.status is StampsStatus.NOT_ENFORCED

    def test_a_raising_reader_is_unreadable_never_satisfied(self, tmp_path: Path, git_repo: Path) -> None:
        def _boom(_path: Path) -> StampsRead:
            raise RuntimeError("disk on fire")

        verdict = make_stamps_leg(stamps_reader=_boom)(
            feature_id="FEAT-X",
            repo_root=tmp_path,
            worktree=git_repo,
            branch="main",
            toolchain_green=True,
        )
        assert verdict.status is StampsStatus.UNREADABLE
        assert "disk on fire" in verdict.detail

    def test_the_envelope_and_git_are_not_consulted_when_no_home_needs_them(
        self, tmp_path: Path
    ) -> None:
        canonical = _canonical_with_feature(
            tmp_path, "scenarios:\n  'A': toolchain\n  'B': operator\n"
        )
        calls: list[str] = []

        def _no_envelope(_h: Path):
            calls.append("envelope")
            return None

        def _no_git(*_a: Any, **_k: Any):
            calls.append("git")
            return None

        verdict = make_stamps_leg(envelope_reader=_no_envelope, commit_time_reader=_no_git)(
            feature_id="FEAT-X",
            repo_root=canonical,
            worktree=tmp_path / "not-a-worktree",
            branch=None,
            toolchain_green=True,
        )
        assert verdict.status is StampsStatus.SATISFIED
        assert calls == []
