"""Fingerprints, the serious rule, the order of seriousness, and what is kept
of each scanner's output."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from forge.security_sweep.findings import (
    KIND_CODE,
    KIND_DEPENDENCY,
    KIND_SECRET,
    Finding,
    dependency_fingerprint,
    is_serious,
    line_fingerprint,
    path_kinds,
    seriousness_order,
)
from forge.security_sweep.scanners import (
    RuleCatalogue,
    ScannerSetup,
    fixed_message,
    gitleaks_history_argv,
    gitleaks_stdin_argv,
    gitleaks_tree_argv,
    opengrep_argv,
    osv_argv,
    read_gitleaks,
    read_opengrep,
    read_osv,
    run_tool,
)
from forge.security_sweep.sorting import build_prompt, read_answer, redacted_excerpt

KEY = b"\x01" * 32


def _line(**overrides: object) -> str:
    values = dict(
        project="org/app", kind=KIND_SECRET, tool="gitleaks", rule="r", file="a.md", line=3,
        line_text=b"token = example",
    )
    values.update(overrides)
    return line_fingerprint(KEY, **values)  # type: ignore[arg-type]


class TestFingerprints:
    def test_the_same_line_keeps_its_fingerprint(self) -> None:
        assert _line() == _line()

    def test_a_changed_token_on_an_unchanged_line_is_a_new_finding(self) -> None:
        assert _line(line_text=b"token = real-value") != _line()

    def test_every_part_of_the_name_counts(self) -> None:
        base = _line()
        for change in (
            {"project": "org/other"}, {"rule": "other"}, {"file": "b.md"},
            {"line": 4}, {"tool": "opengrep"}, {"kind": KIND_CODE},
        ):
            assert _line(**change) != base

    def test_the_key_matters_so_a_guess_cannot_be_checked_without_it(self) -> None:
        other = line_fingerprint(
            b"\x02" * 32, project="org/app", kind=KIND_SECRET, tool="gitleaks", rule="r",
            file="a.md", line=3, line_text=b"token = example",
        )
        assert other != _line()

    def test_dependency_fingerprints_name_project_advisory_and_package_version(self) -> None:
        one = dependency_fingerprint("org/app", "GHSA-1", "vitest", "4.0.15")
        assert one == dependency_fingerprint("org/app", "GHSA-1", "vitest", "4.0.15")
        assert one != dependency_fingerprint("org/app", "GHSA-1", "vitest", "4.0.18")
        assert one != dependency_fingerprint("org/b", "GHSA-1", "vitest", "4.0.15")


def _finding(kind: str, **values: object) -> Finding:
    base: dict[str, object] = dict(
        project="org/app", kind=kind, tool="t", rule="r", file="f", line=1, fingerprint="x"
    )
    base.update(values)
    return Finding(**base)  # type: ignore[arg-type]


class TestTheSeriousRule:
    def test_dependency_scores_at_or_over_the_line(self) -> None:
        assert is_serious(_finding(KIND_DEPENDENCY, score=7.0), "real", 7.0)
        assert not is_serious(_finding(KIND_DEPENDENCY, score=6.9), "real", 7.0)
        assert not is_serious(_finding(KIND_DEPENDENCY, score=None), "real", 7.0)

    def test_a_malicious_package_is_always_serious(self) -> None:
        assert is_serious(_finding(KIND_DEPENDENCY, advisory="MAL-2026-1", score=None), "real", 7.0)

    def test_a_secret_only_when_sorted_real(self) -> None:
        assert is_serious(_finding(KIND_SECRET), "real", 7.0)
        for status in ("pending", "not-real"):
            assert not is_serious(_finding(KIND_SECRET), status, 7.0)

    def test_a_code_finding_only_when_error_and_real(self) -> None:
        assert is_serious(_finding(KIND_CODE, severity="ERROR"), "real", 7.0)
        assert not is_serious(_finding(KIND_CODE, severity="WARNING"), "real", 7.0)
        assert not is_serious(_finding(KIND_CODE, severity="ERROR"), "pending", 7.0)

    def test_the_order_is_malicious_secrets_then_score(self) -> None:
        items = [
            _finding(KIND_CODE, severity="ERROR", file="c"),
            _finding(KIND_DEPENDENCY, score=7.5, file="d75"),
            _finding(KIND_SECRET, file="s"),
            _finding(KIND_DEPENDENCY, score=9.8, file="d98"),
            _finding(KIND_DEPENDENCY, advisory="MAL-1", file="m"),
        ]
        assert [f.file for f in sorted(items, key=seriousness_order)] == ["m", "s", "d98", "d75", "c"]


def test_path_kinds_label_tests_documentation_and_lockfiles() -> None:
    assert path_kinds("apps/api/internal/auth/auth_test.go") == ("test",)
    assert path_kinds("docs/API.md") == ("documentation",)
    assert path_kinds("web/package-lock.json") == ("lockfile",)
    assert path_kinds("tests/fixtures/README.md") == ("test", "documentation")
    assert path_kinds("src/app.py") == ()


class TestWhatIsKeptOfTheOutput:
    def test_osv_keeps_package_advisory_score_and_fixed_versions(self, tmp_path: Path) -> None:
        output = {
            "results": [
                {
                    "source": {"path": str(tmp_path / "web" / "package-lock.json")},
                    "packages": [
                        {
                            "package": {"name": "vitest", "version": "4.0.15", "ecosystem": "npm"},
                            "vulnerabilities": [
                                {
                                    "id": "GHSA-5xrq-8626-4rwp",
                                    "summary": "text that is never kept",
                                    "database_specific": {"severity": "CRITICAL"},
                                    "affected": [
                                        {"package": {"name": "vitest"}, "ranges": [{"events": [{"introduced": "4.0.0"}, {"fixed": "4.1.0"}]}]},
                                        {"package": {"name": "vitest"}, "ranges": [{"events": [{"introduced": "0"}, {"fixed": "3.2.6"}]}]},
                                    ],
                                }
                            ],
                            "groups": [{"ids": ["GHSA-5xrq-8626-4rwp"], "max_severity": "9.8"}],
                        }
                    ],
                }
            ]
        }
        reading = read_osv(json.dumps(output).encode(), tmp_path)
        (adv,) = reading.advisories
        assert reading.sources == 1
        assert (adv.file, adv.package, adv.version, adv.advisory) == (
            "web/package-lock.json", "vitest", "4.0.15", "GHSA-5xrq-8626-4rwp",
        )
        assert adv.score == 9.8 and adv.severity == "critical"
        assert adv.fixed == ("3.2.6", "4.1.0")

    def test_a_malicious_id_represents_its_group(self, tmp_path: Path) -> None:
        output = {"results": [{"source": {"path": "x/package-lock.json"}, "packages": [{
            "package": {"name": "evil", "version": "1", "ecosystem": "npm"},
            "vulnerabilities": [],
            "groups": [{"ids": ["GHSA-1", "MAL-2026-9"], "max_severity": ""}],
        }]}]}
        (adv,) = read_osv(json.dumps(output).encode(), tmp_path).advisories
        assert adv.advisory == "MAL-2026-9" and adv.score is None

    def test_gitleaks_keeps_rule_file_and_lines_only(self, tmp_path: Path) -> None:
        output = [{"RuleID": "aws-access-token", "File": str(tmp_path / "a" / "b.sh"),
                   "StartLine": 2, "EndLine": 2, "Secret": "REDACTED", "Match": "REDACTED",
                   "Author": "someone", "Email": "someone@example.invalid"}]
        (loc,) = read_gitleaks(json.dumps(output).encode(), tmp_path)
        assert (loc.rule, loc.file, loc.start, loc.end) == ("aws-access-token", "a/b.sh", 2, 2)

    def test_opengrep_ids_are_read_back_and_community_compose_rules_dropped(self, tmp_path: Path) -> None:
        catalogue = RuleCatalogue(
            [
                {"id": "x-rule", "severity": "ERROR", "message": "Bad $X here. More words."},
                {"id": "privileged-service", "severity": "WARNING", "message": "m",
                 "metadata": {"technology": ["docker-compose"]}},
                {"id": "factory-compose-privileged", "severity": "ERROR", "message": "m",
                 "metadata": {"technology": ["docker-compose"], "source": "factory"}},
            ]
        )
        assert catalogue.community_compose_rules == ("privileged-service",)
        output = {"results": [
            {"check_id": "opt.opengrep.x-rule", "path": str(tmp_path / "a.py"),
             "start": {"line": 4}, "end": {"line": 6}, "extra": {"lines": "source"}},
            {"check_id": "opt.opengrep.privileged-service", "path": "compose.yaml",
             "start": {"line": 1}, "end": {"line": 1}},
            {"check_id": "opt.factory-security.factory-compose-privileged", "path": "compose.yaml",
             "start": {"line": 1}, "end": {"line": 1}},
        ]}
        found = read_opengrep(json.dumps(output).encode(), tmp_path, catalogue)
        assert [(f.rule, f.file, f.start, f.end, f.severity) for f in found] == [
            ("x-rule", "a.py", 4, 6, "ERROR"),
            ("factory-compose-privileged", "compose.yaml", 1, 1, "ERROR"),
        ]
        assert catalogue.message("x-rule") == "Bad this value here."

    def test_a_path_outside_the_export_is_not_kept(self, tmp_path: Path) -> None:
        output = [{"RuleID": "r", "File": "/etc/passwd", "StartLine": 1, "EndLine": 1}]
        (loc,) = read_gitleaks(json.dumps(output).encode(), tmp_path)
        assert loc.file == ""

    def test_a_long_message_is_cut_to_one_short_sentence(self) -> None:
        text = fixed_message("A " + "very " * 80 + "long message. Second sentence.")
        assert len(text) <= 200 and "Second" not in text


def _setup(tmp_path: Path) -> ScannerSetup:
    return ScannerSetup(
        gitleaks="/x/gitleaks", osv_scanner="/x/osv-scanner", opengrep="/x/opengrep",
        factory_dir=tmp_path / "factory", opengrep_rules=tmp_path / "rules.yaml",
    )


class TestTheArguments:
    """Codex R9: the factory's settings, never the project's."""

    def test_osv_gets_the_factory_settings_and_ignores_gitignore(self, tmp_path: Path) -> None:
        argv = osv_argv(_setup(tmp_path), tmp_path / "tree")
        assert argv[argv.index("--config") + 1] == str(tmp_path / "factory" / "osv-scanner.toml")
        assert "--no-ignore" in argv and "--recursive" in argv
        assert "--no-call-analysis=all" in argv  # no project build script runs

    def test_gitleaks_always_redacts_and_ignores_inline_allows(self, tmp_path: Path) -> None:
        setup = _setup(tmp_path)
        for argv in (
            gitleaks_tree_argv(setup, tmp_path),
            gitleaks_history_argv(setup, tmp_path),
            gitleaks_stdin_argv(setup),
        ):
            assert "--redact" in argv and "--ignore-gitleaks-allow" in argv
            assert argv[argv.index("--gitleaks-ignore-path") + 1] == str(
                tmp_path / "factory" / "gitleaks-ignore"
            )
            assert "--config" not in argv and "-c" not in argv
            assert argv[argv.index("--report-path") + 1] == "-"

    def test_opengrep_disables_nosem_and_uses_both_rule_files(self, tmp_path: Path) -> None:
        argv = opengrep_argv(_setup(tmp_path), tmp_path / "tree")
        assert "--disable-nosem" in argv and "--no-git-ignore" in argv
        configs = [argv[i + 1] for i, a in enumerate(argv) if a == "--config"]
        assert configs == [str(tmp_path / "rules.yaml"), str(tmp_path / "factory" / "compose-rules.yaml")]

    def test_a_missing_tool_or_setting_is_said_never_skipped_silently(self, tmp_path: Path) -> None:
        setup = ScannerSetup(gitleaks=None, osv_scanner="/x", opengrep="/x", factory_dir=tmp_path)
        assert setup.missing("gitleaks") == "the secret scanner is not installed"
        assert setup.missing("osv-scanner") == "the factory's dependency scanner settings are missing"


class TestRunningATool:
    @pytest.mark.asyncio
    async def test_the_environment_holds_only_path_and_a_fresh_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FORGE_SOMETHING_SECRET", "do-not-pass")
        run = await run_tool(
            [sys.executable, "-c", "import os, json; print(json.dumps(dict(os.environ)))"],
            timeout=30,
        )
        assert run.ok
        env = json.loads(run.stdout)
        assert set(env) - {"LC_CTYPE"} == {"PATH", "HOME"}
        assert env["PATH"] == "/usr/local/bin:/usr/bin:/bin"
        assert not Path(env["HOME"]).exists()  # removed afterwards

    @pytest.mark.asyncio
    async def test_a_scanner_past_its_limit_is_stopped(self) -> None:
        loop = asyncio.get_running_loop()
        started = loop.time()
        run = await run_tool([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5)
        assert not run.ok and run.reason == "did not finish within 0.5 seconds"
        assert loop.time() - started < 10

    @pytest.mark.asyncio
    async def test_an_unexpected_exit_is_not_read_as_clean(self) -> None:
        run = await run_tool([sys.executable, "-c", "import sys; sys.exit(2)"], timeout=30)
        assert not run.ok and run.reason == "stopped with exit code 2"

    @pytest.mark.asyncio
    async def test_a_program_that_cannot_start(self, tmp_path: Path) -> None:
        run = await run_tool([str(tmp_path / "no-such-program")], timeout=5)
        assert not run.ok and run.reason == "could not be started"


class TestTheQuestion:
    def test_the_answer_is_reduced_to_one_boolean(self) -> None:
        assert read_answer('{"real": true}') is True
        assert read_answer('<think>{"real": true}</think>```json\n{"real": false}\n```') is False
        assert read_answer("yes it is real") is None
        assert read_answer('{"real": "yes"}') is None
        assert read_answer('{"real": true} {"real": false}') is None
        assert read_answer(None) is None

    def test_hidden_lines_are_replaced_with_their_numbers_kept(self) -> None:
        text = redacted_excerpt(10, ["a", "secret here", "b"], {11})
        assert text == "10: a\n11: [line hidden: may hold a secret]\n12: b"

    def test_a_code_prompt_carries_the_excerpt_and_asks_for_json(self) -> None:
        finding = _finding(KIND_CODE, message="Shell call.", severity="ERROR", file="a.py", line=2)
        prompt = build_prompt(finding, "1: x\n2: y")
        assert "1: x\n2: y" in prompt
        assert prompt.endswith('{"real": true} or {"real": false}.')


class TestTheModelRoute:
    """Item 9, as decided: the sweep's own names, handed to GuardKit's client
    as arguments; the process environment is never changed."""

    def _fake_guardkit(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
        import types

        built: list[tuple] = []

        class ConfiguredAsker:
            def __init__(self, base_url, model, timeout, *, extra_body=None, timeout_note=""):
                built.append((base_url, model, timeout, extra_body))

        module = types.ModuleType("guardkit.orchestrator.stamp_model_fallback")
        module.ConfiguredAsker = ConfiguredAsker  # type: ignore[attr-defined]
        module.DEFAULT_TIMEOUT_S = 60.0  # type: ignore[attr-defined]
        package = types.ModuleType("guardkit.orchestrator")
        package.stamp_model_fallback = module  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "guardkit", types.ModuleType("guardkit"))
        monkeypatch.setitem(sys.modules, "guardkit.orchestrator", package)
        monkeypatch.setitem(sys.modules, "guardkit.orchestrator.stamp_model_fallback", module)
        return built

    def test_the_sweeps_names_are_passed_as_arguments(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import os

        from forge.security_sweep.sorting import default_asker

        built = self._fake_guardkit(monkeypatch)
        monkeypatch.setenv("FORGE_SWEEP_MODEL_URL", "http://192.0.2.1:4100/v1")
        monkeypatch.setenv("FORGE_SWEEP_MODEL", "qwen3.8-flash-next-autoround")
        monkeypatch.setenv("FORGE_SWEEP_MODEL_EXTRA_BODY", '{"chat_template_kwargs":{"enable_thinking":false}}')
        for name in ("GUARDKIT_STAMP_MODEL_URL", "GUARDKIT_STAMP_MODEL", "OPENAI_BASE_URL"):
            monkeypatch.delenv(name, raising=False)
        before = dict(os.environ)
        asker, why = default_asker()
        assert asker is not None and why == ""
        assert built == [(
            "http://192.0.2.1:4100/v1", "qwen3.8-flash-next-autoround", 60.0,
            {"chat_template_kwargs": {"enable_thinking": False}},
        )]
        assert dict(os.environ) == before
        assert "GUARDKIT_STAMP_MODEL_URL" not in os.environ

    @pytest.mark.parametrize("url, model", [("", "m"), ("http://192.0.2.1:4100/v1", ""), (None, None)])
    def test_an_empty_address_or_name_is_not_configured(
        self, monkeypatch: pytest.MonkeyPatch, url: str | None, model: str | None
    ) -> None:
        from forge.security_sweep.sorting import default_asker

        built = self._fake_guardkit(monkeypatch)
        for name, value in (("FORGE_SWEEP_MODEL_URL", url), ("FORGE_SWEEP_MODEL", model)):
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        # Neither GuardKit's names nor OPENAI_BASE_URL count for the sweep.
        monkeypatch.setenv("OPENAI_BASE_URL", "http://192.0.2.1:4000/v1")
        monkeypatch.setenv("GUARDKIT_STAMP_MODEL_URL", "http://192.0.2.1:4000/v1")
        monkeypatch.setenv("GUARDKIT_STAMP_MODEL", "other")
        assert default_asker() == (None, "the model is not configured")
        assert built == []

    def test_an_extra_body_that_is_not_an_object_is_left_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from forge.security_sweep.sorting import default_asker

        built = self._fake_guardkit(monkeypatch)
        env = {"FORGE_SWEEP_MODEL_URL": "http://192.0.2.1:4100/v1", "FORGE_SWEEP_MODEL": "m",
               "FORGE_SWEEP_MODEL_EXTRA_BODY": "[1, 2]"}
        asker, _ = default_asker(env)
        assert asker is not None and built[0][3] == {}
