"""``forge register-repo`` — the register-repo spec (2026-09-05), rule 10.

Every test builds its own ``forge.yaml`` and its own repository under
``tmp_path``. Nothing here reads or writes the live estate: the repository base
is redirected with ``FORGE_REPO_BASE``, the config comes from ``--config``, and
the two seams that reach outside the process (``guardkit init`` and the build
ledger) are rebound on the module.

The fixture ``forge.yaml`` carries comment lines on purpose. The live file's
comments are the record of why entries exist, so "the comments survive byte for
byte" is a test, not a nicety.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from forge.cli import register_repo
from forge.cli.main import main
from forge.config.loader import load_config

#: The real discovery seam, captured before the autouse fixture stubs it, so the
#: fallback below can be exercised end to end.
_REAL_DISCOVER = register_repo._discover_test_roots

#: The real estate gate, captured before the autouse fixture stubs it, so the
#: gate's own branches can be exercised directly without docker.
_REAL_ESTATE_STEP = register_repo._estate_step

# A comment on nearly every block: this is what must survive the edit.
FIXTURE_CONFIG = """\
# forge.yaml — the fixture. Every comment in this file is load-bearing prose.
permissions:
  filesystem:
    # The allowlist is deliberately explicit: no implicit default.
    allowlist:
    - /home/forge
    - /home/richardwoollcott/Projects/appmilla_github/forge
approval:
  expected_approver: U03QR8WKT29
planning:
  default_target_repo: guardkit/api_test
  target_repo_paths:
    guardkit/api_test: /home/richardwoollcott/Projects/appmilla_github/api_test
    # Namespace aliases (2026-08-02, attended): builds are queued with
    # repo=appmilla_github/<name>.
    appmilla_github/api_test: /home/richardwoollcott/Projects/appmilla_github/api_test
"""


def _write_config(tmp_path: Path, text: str = FIXTURE_CONFIG) -> Path:
    path = tmp_path / "forge.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _make_repo(
    base: Path,
    name: str,
    *,
    git: bool = True,
    remote: bool = True,
    guardkit: bool = True,
    toolchain: str | None = None,
    extras: bool = True,
) -> Path:
    repo = base / name
    repo.mkdir(parents=True)
    if git:
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        if remote:
            subprocess.run(
                ["git", "remote", "add", "origin", "https://example.invalid/x.git"],
                cwd=repo,
                check=True,
            )
    if guardkit:
        (repo / ".guardkit").mkdir()
        body = "# the repository's guardkit config\n"
        if toolchain is not None:
            body += toolchain
        (repo / ".guardkit" / "config.yaml").write_text(body, encoding="utf-8")
    if extras:
        (repo / "tests" / "smoke").mkdir(parents=True)
        (repo / "qa" / "gates").mkdir(parents=True)
        (repo / "qa" / "gates" / "registry.yaml").write_text("{}\n", encoding="utf-8")
        (repo / "deploy").mkdir()
        (repo / "deploy" / "profile.yaml").write_text("{}\n", encoding="utf-8")
        (repo / "docs").mkdir()
        (repo / "docs" / "architecture-rules.yaml").write_text(
            "{}\n", encoding="utf-8"
        )
    return repo


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Point the command at tmp_path and stub the two outside-the-process seams."""
    base = tmp_path / "base"
    base.mkdir()
    monkeypatch.setenv(register_repo.FORGE_REPO_BASE_ENV, str(base))
    # A tmp_path checkout belongs to whoever runs the suite; the uid rule is
    # exercised directly in its own test.
    monkeypatch.setattr(register_repo, "EXPECTED_OWNER_UID", os.getuid())
    monkeypatch.setattr(
        register_repo,
        "_estate_step",
        lambda: register_repo.Step("estate", "ok", "all builds terminal"),
    )
    monkeypatch.setattr(
        register_repo, "_discover_test_roots", lambda repo: ["tests/smoke"]
    )

    def _no_init(repo, template):  # pragma: no cover — asserted where it matters
        raise AssertionError("guardkit init was shelled out to unexpectedly")

    monkeypatch.setattr(register_repo, "_run_guardkit_init", _no_init)
    return base


def _run(config: Path, *args: str):
    return CliRunner().invoke(main, ["--config", str(config), "register-repo", *args])


def _steps(result) -> list[tuple[str, str, str]]:
    return [
        (row["step"], row["status"], row["detail"]) for row in json.loads(result.output)
    ]


def _status_of(result, step: str) -> list[str]:
    return [s for name, s, _ in _steps(result) if name == step]


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_fresh_repo_writes_the_allowlist_entry_and_both_keys(_isolate, tmp_path):
    repo = _make_repo(_isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n")
    config = _write_config(tmp_path)

    result = _run(config, str(repo))

    assert result.exit_code == 0, result.output
    parsed = load_config(config)
    assert Path(str(repo)) in [Path(p) for p in parsed.permissions.filesystem.allowlist]
    assert parsed.planning.target_repo_paths["guardkit/bench-one"] == str(repo)
    assert parsed.planning.target_repo_paths["appmilla_github/bench-one"] == str(repo)


def test_the_report_ends_with_the_recreate_command_and_the_slack_sentence(
    _isolate, tmp_path
):
    repo = _make_repo(_isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n")
    config = _write_config(tmp_path)

    result = _run(config, str(repo))

    lines = result.output.strip().splitlines()
    assert lines[-2] == "next  run: bash ops/forge-prod-recreate.sh"
    assert lines[-1] == "slack target: bench-one  <your first feature>"


def test_comment_lines_survive_byte_for_byte(_isolate, tmp_path):
    repo = _make_repo(_isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n")
    config = _write_config(tmp_path)

    _run(config, str(repo))

    after = config.read_text(encoding="utf-8")
    for line in FIXTURE_CONFIG.splitlines():
        if line.strip().startswith("#"):
            assert line in after.splitlines(), line
    # And every original line is still there, in order.
    original = FIXTURE_CONFIG.splitlines()
    kept = [line for line in after.splitlines() if line in original]
    assert kept == original


def test_the_edited_yaml_re_parses_through_load_config(_isolate, tmp_path):
    repo = _make_repo(_isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n")
    config = _write_config(tmp_path)

    result = _run(config, str(repo), "--json")

    assert result.exit_code == 0, result.output
    assert "ok" in _status_of(result, "config")
    load_config(config)  # raises if the surgical edit broke the document


def test_a_dated_backup_is_taken_before_the_first_change(_isolate, tmp_path):
    repo = _make_repo(_isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n")
    config = _write_config(tmp_path)

    _run(config, str(repo))

    backups = list(tmp_path.glob("forge.yaml.bak-*-pre-register-bench-one"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == FIXTURE_CONFIG


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------


def test_second_run_is_byte_identical_and_takes_no_backup(_isolate, tmp_path):
    repo = _make_repo(_isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n")
    config = _write_config(tmp_path)

    _run(config, str(repo))
    after_first = config.read_text(encoding="utf-8")
    repo_config_first = (repo / ".guardkit" / "config.yaml").read_text(encoding="utf-8")
    for backup in tmp_path.glob("forge.yaml.bak-*"):
        backup.unlink()

    second = _run(config, str(repo), "--json")

    assert second.exit_code == 0, second.output
    assert config.read_text(encoding="utf-8") == after_first
    assert (repo / ".guardkit" / "config.yaml").read_text(
        encoding="utf-8"
    ) == repo_config_first
    assert list(tmp_path.glob("forge.yaml.bak-*")) == []
    assert {status for _, status, _ in _steps(second)} <= {"ok", "unchanged"}


# ---------------------------------------------------------------------------
# The checks that refuse
# ---------------------------------------------------------------------------


def test_non_git_path_is_refused(_isolate, tmp_path):
    repo = _make_repo(_isolate, "bench-one", git=False)
    config = _write_config(tmp_path)
    before = config.read_text(encoding="utf-8")

    result = _run(config, str(repo), "--toolchain-test", "pytest")

    assert result.exit_code == 1
    assert "is not a git checkout" in result.output
    assert config.read_text(encoding="utf-8") == before


def test_a_path_outside_the_base_is_refused(_isolate, tmp_path):
    outside = tmp_path / "elsewhere" / "bench-one"
    outside.mkdir(parents=True)
    (outside / ".git").mkdir()
    config = _write_config(tmp_path)
    before = config.read_text(encoding="utf-8")

    result = _run(config, str(outside), "--toolchain-test", "pytest")

    assert result.exit_code == 1
    assert "is not directly under" in result.output
    assert config.read_text(encoding="utf-8") == before


def test_a_missing_path_is_refused(_isolate, tmp_path):
    config = _write_config(tmp_path)

    result = _run(config, str(_isolate / "nope"), "--toolchain-test", "pytest")

    assert result.exit_code == 1
    assert "does not exist" in result.output


def test_a_checkout_owned_by_someone_else_is_refused(_isolate, tmp_path, monkeypatch):
    repo = _make_repo(_isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n")
    config = _write_config(tmp_path)
    monkeypatch.setattr(register_repo, "EXPECTED_OWNER_UID", os.getuid() + 5000)

    result = _run(config, str(repo))

    assert result.exit_code == 1
    assert "is owned by uid" in result.output


def test_a_map_key_pointing_somewhere_else_is_refused(_isolate, tmp_path):
    repo = _make_repo(_isolate, "api_test", toolchain="toolchain:\n  test: pytest\n")
    config = _write_config(tmp_path)
    before = config.read_text(encoding="utf-8")

    result = _run(config, str(repo))

    assert result.exit_code == 1
    assert "already points at" in result.output
    assert config.read_text(encoding="utf-8") == before


def test_missing_toolchain_test_flag_is_refused(_isolate, tmp_path):
    repo = _make_repo(_isolate, "bench-one", toolchain=None)
    config = _write_config(tmp_path)
    before = config.read_text(encoding="utf-8")

    result = _run(config, str(repo))

    assert result.exit_code == 1
    assert "--toolchain-test" in result.output
    assert config.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("bad", ["guardkit/bench-one", "../bench-one", "a\\b"])
def test_a_name_with_a_path_separator_is_refused_before_anything_is_written(
    _isolate, tmp_path, bad
):
    """The name becomes two map keys, a folder name and part of a backup's name.

    A separator in it would mint the key ``guardkit/guardkit/bench-one``, which
    nothing looks up, and would put the dated backup in another directory. It is
    refused before the first write, so both files are exactly as they were —
    checked by mtime, not by reading, so "nothing was written" means nothing at
    all, not "nothing that changed the bytes".
    """
    repo = _make_repo(_isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n")
    repo_config = repo / ".guardkit" / "config.yaml"
    config = _write_config(tmp_path)
    before = (config.stat().st_mtime_ns, repo_config.stat().st_mtime_ns)
    before_text = config.read_text(encoding="utf-8")

    result = _run(config, str(repo), "--name", bad)

    assert result.exit_code == 1
    assert "path separator" in result.output
    assert (config.stat().st_mtime_ns, repo_config.stat().st_mtime_ns) == before
    assert config.read_text(encoding="utf-8") == before_text
    assert list(tmp_path.glob("forge.yaml.bak-*")) == []


def test_the_separator_refusal_is_a_refused_step_named_name(_isolate, tmp_path):
    repo = _make_repo(_isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n")
    config = _write_config(tmp_path)

    result = _run(config, str(repo), "--name", "guardkit/bench-one", "--json")

    payload = json.loads(result.output.split("Error:")[0])
    assert payload[-1]["status"] == "refused"
    assert payload[-1]["step"] == "name"


def test_a_plain_name_is_still_accepted(_isolate, tmp_path):
    repo = _make_repo(_isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n")
    config = _write_config(tmp_path)

    result = _run(config, str(repo), "--name", "bench_one")

    assert result.exit_code == 0, result.output
    parsed = load_config(config)
    assert parsed.planning.target_repo_paths["guardkit/bench_one"] == str(repo)


def test_a_refusal_is_reported_as_a_refused_step_under_json(_isolate, tmp_path):
    repo = _make_repo(_isolate, "bench-one", toolchain=None)
    config = _write_config(tmp_path)

    result = _run(config, str(repo), "--json")

    payload = json.loads(result.output.split("Error:")[0])
    assert payload[-1]["status"] == "refused"
    assert payload[-1]["step"] == "toolchain"


def test_an_unparseable_edit_restores_the_file_and_exits_non_zero(
    _isolate, tmp_path, monkeypatch
):
    repo = _make_repo(_isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n")
    config = _write_config(tmp_path)
    before = config.read_text(encoding="utf-8")
    monkeypatch.setattr(
        register_repo,
        "append_sequence_item",
        lambda lines, path, value: lines.insert(0, "  not: valid: yaml: at: all"),
    )

    result = _run(config, str(repo))

    assert result.exit_code == 1
    assert "no longer parses" in result.output
    assert config.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# The checks that warn and carry on
# ---------------------------------------------------------------------------


def test_no_remote_warns_and_exits_zero(_isolate, tmp_path):
    repo = _make_repo(
        _isolate, "bench-one", remote=False, toolchain="toolchain:\n  test: pytest\n"
    )
    config = _write_config(tmp_path)

    result = _run(config, str(repo), "--json")

    assert result.exit_code == 0, result.output
    assert _status_of(result, "remote") == ["warn"]
    assert any("not-pushed" in detail for _, _, detail in _steps(result))


def test_empty_test_roots_warn_with_the_spec_sentence(_isolate, tmp_path, monkeypatch):
    repo = _make_repo(_isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n")
    config = _write_config(tmp_path)
    monkeypatch.setattr(register_repo, "_discover_test_roots", lambda repo: [])

    result = _run(config, str(repo), "--json")

    assert result.exit_code == 0, result.output
    detail = [d for step, _, d in _steps(result) if step == "test-roots"][0]
    assert detail == (
        "tests/ holds no subdirectory, so plans that name smoke gates will "
        "fail plan-containment; add tests/<area>/"
    )


def test_missing_deploy_profile_and_architecture_rules_warn(_isolate, tmp_path):
    repo = _make_repo(
        _isolate,
        "bench-one",
        extras=False,
        toolchain="toolchain:\n  test: pytest\n",
    )
    config = _write_config(tmp_path)

    result = _run(config, str(repo), "--json")

    assert result.exit_code == 0, result.output
    assert _status_of(result, "deploy") == ["warn"]
    assert _status_of(result, "arch-rules") == ["warn"]
    assert _status_of(result, "qa-gates") == ["warn"]


# ---------------------------------------------------------------------------
# The repository's own guardkit config
# ---------------------------------------------------------------------------


def test_an_existing_toolchain_declaration_is_never_overwritten(_isolate, tmp_path):
    repo = _make_repo(
        _isolate,
        "bench-one",
        toolchain="toolchain:\n  test: uv run pytest\n  test_timeout: 900\n",
    )
    config = _write_config(tmp_path)

    result = _run(config, str(repo), "--toolchain-test", "make test", "--json")

    assert result.exit_code == 0, result.output
    body = (repo / ".guardkit" / "config.yaml").read_text(encoding="utf-8")
    assert "uv run pytest" in body
    assert "make test" not in body
    assert "test_timeout: 900" in body
    assert _status_of(result, "toolchain") == ["unchanged"]


def test_a_toolchain_block_that_declares_only_a_timeout_keeps_that_timeout(
    _isolate, tmp_path
):
    """The blocker: a block with ``test_timeout`` but no ``test``.

    Writing ``test_timeout`` unconditionally appended a second key; PyYAML takes
    the last one, so the repository's declared 900 silently became 300 and the
    file carried a duplicate key that stricter parsers reject.
    """
    repo = _make_repo(
        _isolate,
        "bench-one",
        toolchain="toolchain:\n  test_timeout: 900\n  lint: ruff check\n",
    )
    config = _write_config(tmp_path)

    result = _run(config, str(repo), "--toolchain-test", "uv run pytest", "--json")

    assert result.exit_code == 0, result.output
    body = (repo / ".guardkit" / "config.yaml").read_text(encoding="utf-8")
    assert body.count("test_timeout:") == 1
    assert "  test: uv run pytest" in body
    assert "  lint: ruff check" in body
    declared = yaml.safe_load(body)["toolchain"]
    assert declared == {
        "test": "uv run pytest",
        "test_timeout": 900,
        "lint": "ruff check",
    }
    assert _status_of(result, "toolchain") == ["added"]


def test_a_toolchain_block_without_a_timeout_gets_the_default_once(
    _isolate, tmp_path
):
    repo = _make_repo(
        _isolate, "bench-one", toolchain="toolchain:\n  lint: ruff check\n"
    )
    config = _write_config(tmp_path)

    result = _run(config, str(repo), "--toolchain-test", "uv run pytest", "--json")

    assert result.exit_code == 0, result.output
    body = (repo / ".guardkit" / "config.yaml").read_text(encoding="utf-8")
    assert yaml.safe_load(body)["toolchain"] == {
        "test": "uv run pytest",
        "test_timeout": 300,
        "lint": "ruff check",
    }


def test_the_minimal_toolchain_block_is_written_when_absent(_isolate, tmp_path):
    repo = _make_repo(_isolate, "bench-one", toolchain=None)
    config = _write_config(tmp_path)

    result = _run(config, str(repo), "--toolchain-test", "uv run pytest", "--json")

    assert result.exit_code == 0, result.output
    body = (repo / ".guardkit" / "config.yaml").read_text(encoding="utf-8")
    assert "# the repository's guardkit config" in body
    assert "toolchain:" in body
    assert "  test: uv run pytest" in body
    assert "  test_timeout: 300" in body
    assert _status_of(result, "toolchain") == ["added"]


def test_the_memory_project_id_is_written_once_and_sanitised(_isolate, tmp_path):
    repo = _make_repo(
        _isolate, "ts-api-test", toolchain="toolchain:\n  test: pytest\n"
    )
    config = _write_config(tmp_path)

    result = _run(config, str(repo), "--json")

    assert result.exit_code == 0, result.output
    body = (repo / ".guardkit" / "config.yaml").read_text(encoding="utf-8")
    assert "memory:" in body
    assert "  project: ts_api_test" in body
    assert _status_of(result, "project-id") == ["ok"]


def test_an_existing_memory_project_is_left_alone(_isolate, tmp_path):
    repo = _make_repo(
        _isolate,
        "bench-one",
        toolchain="toolchain:\n  test: pytest\nmemory:\n  project: chosen_by_hand\n",
    )
    config = _write_config(tmp_path)

    result = _run(config, str(repo), "--json")

    body = (repo / ".guardkit" / "config.yaml").read_text(encoding="utf-8")
    assert body.count("project:") == 1
    assert "chosen_by_hand" in body
    assert _status_of(result, "memory") == ["unchanged"]


def test_guardkit_init_is_shelled_out_to_when_the_repo_has_none(
    _isolate, tmp_path, monkeypatch
):
    repo = _make_repo(_isolate, "bench-one", guardkit=False)
    config = _write_config(tmp_path)
    calls: list[tuple[Path, str]] = []

    def _fake_init(repo_arg, template):
        calls.append((repo_arg, template))
        (repo_arg / ".guardkit").mkdir(exist_ok=True)
        return subprocess.CompletedProcess(["guardkit", "init"], 0, "", "")

    monkeypatch.setattr(register_repo, "_run_guardkit_init", _fake_init)

    result = _run(config, str(repo), "--toolchain-test", "pytest", "--json")

    assert result.exit_code == 0, result.output
    assert calls == [(repo, "default")]
    assert _status_of(result, "guardkit") == ["added"]


def test_a_failing_guardkit_init_refuses_before_the_config_is_touched(
    _isolate, tmp_path, monkeypatch
):
    repo = _make_repo(_isolate, "bench-one", guardkit=False)
    config = _write_config(tmp_path)
    before = config.read_text(encoding="utf-8")
    monkeypatch.setattr(
        register_repo,
        "_run_guardkit_init",
        lambda repo_arg, template: subprocess.CompletedProcess(
            ["guardkit", "init"], 1, "", "template not found"
        ),
    )

    result = _run(config, str(repo), "--toolchain-test", "pytest")

    assert result.exit_code == 1
    assert "guardkit init default failed" in result.output
    assert config.read_text(encoding="utf-8") == before
    assert list(tmp_path.glob("forge.yaml.bak-*")) == []


# ---------------------------------------------------------------------------
# --dry-run and --json
# ---------------------------------------------------------------------------


def test_dry_run_leaves_both_files_mtimes_unchanged(_isolate, tmp_path):
    repo = _make_repo(_isolate, "bench-one", toolchain=None)
    config = _write_config(tmp_path)
    repo_config = repo / ".guardkit" / "config.yaml"
    before = (config.stat().st_mtime_ns, repo_config.stat().st_mtime_ns)

    result = _run(
        config, str(repo), "--toolchain-test", "pytest", "--dry-run", "--json"
    )

    assert result.exit_code == 0, result.output
    assert (config.stat().st_mtime_ns, repo_config.stat().st_mtime_ns) == before
    assert list(tmp_path.glob("forge.yaml.bak-*")) == []
    assert "would-add" in _status_of(result, "allowlist")
    assert _status_of(result, "repo-map") == ["would-add", "would-add"]
    assert _status_of(result, "toolchain") == ["would-add"]


def test_json_is_a_list_of_step_status_detail(_isolate, tmp_path):
    repo = _make_repo(_isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n")
    config = _write_config(tmp_path)

    result = _run(config, str(repo), "--json")

    payload = json.loads(result.output)
    assert isinstance(payload, list)
    assert all(set(row) == {"step", "status", "detail"} for row in payload)
    assert payload[-2]["step"] == "next"
    assert payload[-2]["detail"] == "run: bash ops/forge-prod-recreate.sh"
    assert payload[-1]["step"] == "slack"
    assert payload[-1]["detail"] == "target: bench-one  <your first feature>"


# ---------------------------------------------------------------------------
# The estate gate — prints, never runs
# ---------------------------------------------------------------------------


# The gate reads a ledger. It reads it the way the recreate script of record
# does — ask a running forge-prod container first, fall back to FORGE_DB_PATH,
# and say so plainly when there is neither. No test here touches docker: the
# thing that runs commands is a parameter, and every test answers it itself.


def _fake_runner(answers):
    """A runner that answers by command word and records what it was asked."""
    calls: list[list[str]] = []

    def run(argv):
        argv = list(argv)
        calls.append(argv)
        return answers[argv[1]]  # 'inspect' or 'exec'

    run.calls = calls  # type: ignore[attr-defined]
    return run


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["docker"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _rows(*statuses):
    return json.dumps(
        [{"build_id": f"b{i}", "status": s} for i, s in enumerate(statuses)]
    )


def test_a_running_container_is_asked_the_way_the_recreate_script_asks_it():
    runner = _fake_runner(
        {
            "inspect": _completed(stdout="true\n"),
            "exec": _completed(stdout=_rows("COMPLETE", "FAILED")),
        }
    )

    step = _REAL_ESTATE_STEP(runner=runner)

    assert (step.status, step.detail) == ("ok", "all builds terminal")
    assert runner.calls[1] == [
        "docker",
        "exec",
        "forge-prod",
        "forge",
        "--config",
        "/var/forge/forge.yaml",
        "status",
        "--json",
    ]


def test_the_container_branch_counts_the_builds_that_are_not_terminal():
    runner = _fake_runner(
        {
            "inspect": _completed(stdout="true\n"),
            "exec": _completed(stdout=_rows("COMPLETE", "RUNNING", "QUEUED")),
        }
    )

    step = _REAL_ESTATE_STEP(runner=runner)

    assert (step.status, step.detail) == ("wait", "2 builds are not terminal")


def test_one_non_terminal_build_reads_as_a_sentence():
    runner = _fake_runner(
        {
            "inspect": _completed(stdout="true\n"),
            "exec": _completed(stdout=_rows("COMPLETE", "RUNNING")),
        }
    )

    step = _REAL_ESTATE_STEP(runner=runner)

    assert step.detail == "1 build is not terminal"


def test_a_container_that_cannot_be_read_warns_rather_than_stopping():
    runner = _fake_runner(
        {
            "inspect": _completed(stdout="true\n"),
            "exec": _completed(returncode=1, stderr="forge status: database error"),
        }
    )

    step = _REAL_ESTATE_STEP(runner=runner)

    assert step.status == "warn"
    assert step.detail.startswith("could not read the build ledger")


def test_a_stopped_container_falls_through_to_the_ledger_path(monkeypatch, tmp_path):
    """A container that exists but is down is not asked; FORGE_DB_PATH is read."""
    ledger = tmp_path / "forge.db"
    ledger.write_text("", encoding="utf-8")
    monkeypatch.setenv("FORGE_DB_PATH", str(ledger))
    monkeypatch.setattr(
        register_repo, "_read_ledger_views", lambda path: ["running-build"]
    )
    monkeypatch.setattr(register_repo, "_all_terminal", lambda views: False)
    runner = _fake_runner({"inspect": _completed(stdout="false\n")})

    step = _REAL_ESTATE_STEP(runner=runner)

    assert (step.status, step.detail) == ("wait", "1 build is not terminal")
    assert [c[1] for c in runner.calls] == ["inspect"]  # never asked the container


def test_no_container_at_all_falls_through_to_the_ledger_path(monkeypatch, tmp_path):
    ledger = tmp_path / "forge.db"
    ledger.write_text("", encoding="utf-8")
    monkeypatch.setenv("FORGE_DB_PATH", str(ledger))
    monkeypatch.setattr(register_repo, "_read_ledger_views", lambda path: [])
    runner = _fake_runner({"inspect": _completed(returncode=1, stderr="No such object")})

    step = _REAL_ESTATE_STEP(runner=runner)

    assert (step.status, step.detail) == ("ok", "all builds terminal")


def test_a_machine_with_no_docker_command_at_all_still_reads_the_ledger(
    monkeypatch, tmp_path
):
    ledger = tmp_path / "forge.db"
    ledger.write_text("", encoding="utf-8")
    monkeypatch.setenv("FORGE_DB_PATH", str(ledger))
    monkeypatch.setattr(register_repo, "_read_ledger_views", lambda path: [])

    def _no_docker(argv):
        raise FileNotFoundError("docker")

    step = _REAL_ESTATE_STEP(runner=_no_docker)

    assert step.status == "ok"


def test_neither_a_container_nor_a_ledger_path_says_so_plainly(monkeypatch):
    monkeypatch.delenv("FORGE_DB_PATH", raising=False)
    runner = _fake_runner({"inspect": _completed(returncode=1)})

    step = _REAL_ESTATE_STEP(runner=runner)

    assert step.status == "warn"
    assert step.detail == (
        "could not read the build ledger (no forge-prod container and "
        "FORGE_DB_PATH unset)"
    )


def test_the_gate_reads_the_ledger_and_never_creates_one(monkeypatch, tmp_path):
    """The read-only path, run for real against a path that is not a database.

    It must warn — and it must leave no file behind. A ledger this command
    invented would show no builds, and the gate would say "all terminal" about
    an estate it had never read.
    """
    missing = tmp_path / "nowhere" / "forge.db"
    monkeypatch.setenv("FORGE_DB_PATH", str(missing))
    runner = _fake_runner({"inspect": _completed(returncode=1)})

    step = _REAL_ESTATE_STEP(runner=runner)

    assert step.status == "warn"
    assert step.detail.startswith("could not read the build ledger")
    assert not missing.exists()
    assert not missing.parent.exists()


def test_the_gate_line_reaches_the_report_and_the_command_still_prints(
    _isolate, tmp_path, monkeypatch
):
    repo = _make_repo(_isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n")
    config = _write_config(tmp_path)
    monkeypatch.setattr(
        register_repo,
        "_estate_step",
        lambda: register_repo.Step("estate", "wait", "2 builds are not terminal"),
    )

    result = _run(config, str(repo))

    assert result.exit_code == 0, result.output
    assert "2 builds are not terminal" in result.output
    assert "run: bash ops/forge-prod-recreate.sh" in result.output


def test_an_unreadable_build_ledger_warns_rather_than_stopping(
    _isolate, tmp_path, monkeypatch
):
    repo = _make_repo(_isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n")
    config = _write_config(tmp_path)
    monkeypatch.setattr(
        register_repo,
        "_estate_step",
        lambda: register_repo.Step("estate", "warn", "could not read the build ledger"),
    )

    result = _run(config, str(repo), "--json")

    assert result.exit_code == 0, result.output
    assert _status_of(result, "estate") == ["warn"]


# ---------------------------------------------------------------------------
# The surgical writer, on its own
# ---------------------------------------------------------------------------


def test_a_missing_target_repo_paths_block_is_created_under_planning(
    _isolate, tmp_path
):
    text = (
        "permissions:\n"
        "  filesystem:\n"
        "    allowlist:\n"
        "    - /home/forge\n"
        "planning:\n"
        "  # no map yet\n"
        "  default_target_repo: guardkit/api_test\n"
    )
    repo = _make_repo(_isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n")
    config = _write_config(tmp_path, text)

    result = _run(config, str(repo))

    assert result.exit_code == 0, result.output
    parsed = load_config(config)
    assert parsed.planning.target_repo_paths["guardkit/bench-one"] == str(repo)
    assert "  # no map yet" in config.read_text(encoding="utf-8")


def test_the_planning_block_is_created_when_the_config_has_none(_isolate, tmp_path):
    text = (
        "permissions:\n"
        "  filesystem:\n"
        "    allowlist:\n"
        "    - /home/forge\n"
    )
    repo = _make_repo(_isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n")
    config = _write_config(tmp_path, text)

    result = _run(config, str(repo))

    assert result.exit_code == 0, result.output
    parsed = load_config(config)
    assert set(parsed.planning.target_repo_paths) == {
        "guardkit/bench-one",
        "appmilla_github/bench-one",
    }


def test_project_id_follows_the_existing_sanitiser_rule():
    assert register_repo.project_id_for("ts-api-test") == "ts_api_test"
    assert register_repo.project_id_for("api_test") == "api_test"
    assert register_repo.project_id_for("--weird--name--") == "weird_name"
    assert register_repo.project_id_for("") == "unknown"


def test_locate_walks_indentation_to_a_nested_key():
    lines = FIXTURE_CONFIG.split("\n")
    block = register_repo.locate(lines, ("permissions", "filesystem", "allowlist"))
    assert block is not None
    assert lines[block.key_line].strip() == "allowlist:"
    assert block.child_indent == 4


# ---------------------------------------------------------------------------
# The backup really is taken before the first mutation, and a refusal undoes
# ---------------------------------------------------------------------------


def test_the_backup_is_taken_before_the_repository_is_touched(
    _isolate, tmp_path, monkeypatch
):
    repo = _make_repo(_isolate, "bench-one", guardkit=False)
    config = _write_config(tmp_path)
    backup_existed_at_init: list[bool] = []

    def _init(repo_arg, template):
        backup_existed_at_init.append(
            bool(list(tmp_path.glob("forge.yaml.bak-*-pre-register-bench-one")))
        )
        (repo_arg / ".guardkit").mkdir()
        (repo_arg / ".guardkit" / "config.yaml").write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(["guardkit", "init"], 0, "", "")

    monkeypatch.setattr(register_repo, "_run_guardkit_init", _init)

    result = _run(config, str(repo), "--toolchain-test", "uv run pytest")

    assert result.exit_code == 0, result.output
    assert backup_existed_at_init == [True]


def test_a_refused_yaml_edit_undoes_the_repository_write_and_the_backup(
    _isolate, tmp_path, monkeypatch
):
    """A refusal is "nothing was registered", so nothing may be left changed."""
    repo = _make_repo(_isolate, "bench-one", toolchain=None)
    config = _write_config(tmp_path)
    repo_config = repo / ".guardkit" / "config.yaml"
    repo_before = repo_config.read_text(encoding="utf-8")
    config_before = config.read_text(encoding="utf-8")

    def _refuse(lines, path, value):
        raise register_repo.YamlEditRefused(
            "the allowlist has a value on the same line — edit the file by hand"
        )

    monkeypatch.setattr(register_repo, "append_sequence_item", _refuse)

    result = _run(config, str(repo), "--toolchain-test", "uv run pytest")

    assert result.exit_code == 1
    assert "edit the file by hand" in result.output
    assert repo_config.read_text(encoding="utf-8") == repo_before
    assert config.read_text(encoding="utf-8") == config_before
    assert list(tmp_path.glob("forge.yaml.bak-*")) == []


def test_a_refused_map_edit_undoes_a_repository_config_it_created(
    _isolate, tmp_path, monkeypatch
):
    repo = _make_repo(_isolate, "bench-one", guardkit=False)
    (repo / ".guardkit").mkdir()
    config = _write_config(tmp_path)

    def _refuse(lines, path, key, value):
        raise register_repo.YamlEditRefused("the map cannot be edited safely")

    monkeypatch.setattr(register_repo, "set_mapping_entry", _refuse)

    result = _run(config, str(repo), "--toolchain-test", "uv run pytest")

    assert result.exit_code == 1
    assert not (repo / ".guardkit" / "config.yaml").exists()
    assert list(tmp_path.glob("forge.yaml.bak-*")) == []


# ---------------------------------------------------------------------------
# Test roots on the surface Rich actually runs the command from
# ---------------------------------------------------------------------------


def test_test_roots_fall_back_to_a_plain_scan_when_guardkit_is_absent(
    _isolate, tmp_path, monkeypatch
):
    """forge's venv has the guardkit CLI, not the guardkit package.

    Forge's own discovery raises there, and the report used to say the roots
    could not be listed on every real run. The plain scan answers instead.
    """
    from forge.planning import target_terminal_tools

    repo = _make_repo(_isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n")
    (repo / "tests" / "unit").mkdir(parents=True)
    (repo / "tests" / "__pycache__").mkdir(parents=True)
    (repo / "tests" / "conftest.py").write_text("", encoding="utf-8")
    config = _write_config(tmp_path)

    def _no_guardkit(path, **kwargs):
        raise target_terminal_tools.TargetTestRootsUnresolved("no guardkit here")

    monkeypatch.setattr(
        target_terminal_tools, "discover_target_test_roots", _no_guardkit
    )
    monkeypatch.setattr(register_repo, "_discover_test_roots", _REAL_DISCOVER)

    result = _run(config, str(repo), "--json")

    assert result.exit_code == 0, result.output
    assert _status_of(result, "test-roots") == ["ok"]
    detail = [d for step, _, d in _steps(result) if step == "test-roots"][0]
    assert detail == "tests/smoke, tests/unit"


def test_the_fallback_still_gives_the_spec_sentence_on_an_empty_tests_tree(
    _isolate, tmp_path, monkeypatch
):
    from forge.planning import target_terminal_tools

    repo = _make_repo(
        _isolate, "bench-one", extras=False, toolchain="toolchain:\n  test: pytest\n"
    )
    (repo / "tests").mkdir()
    config = _write_config(tmp_path)

    def _no_guardkit(path, **kwargs):
        raise target_terminal_tools.TargetTestRootsUnresolved("no guardkit here")

    monkeypatch.setattr(
        target_terminal_tools, "discover_target_test_roots", _no_guardkit
    )
    monkeypatch.setattr(register_repo, "_discover_test_roots", _REAL_DISCOVER)

    result = _run(config, str(repo), "--json")

    assert result.exit_code == 0, result.output
    detail = [d for step, _, d in _steps(result) if step == "test-roots"][0]
    assert detail == (
        "tests/ holds no subdirectory, so plans that name smoke gates will "
        "fail plan-containment; add tests/<area>/"
    )


def test_the_plain_scan_follows_guardkits_own_rule(tmp_path):
    repo = tmp_path / "repo"
    (repo / "tests" / "unit").mkdir(parents=True)
    (repo / "tests" / "smoke").mkdir()
    (repo / "tests" / ".cache").mkdir()
    (repo / "tests" / "node_modules").mkdir()
    (repo / "tests" / "test_it.py").write_text("", encoding="utf-8")

    assert register_repo._shallow_test_roots(repo) == ["tests/smoke", "tests/unit"]
    assert register_repo._shallow_test_roots(tmp_path / "nothing") == []


# ---------------------------------------------------------------------------
# --deploy-port — the four files a repository needs to be deployed into its
# own Docker Sandbox (the 2026-09-06 decision, rule 7, and rule 14 of the
# 15:10Z amendment)
#
# None of these tests runs sbx, creates a sandbox, or asks systemd for
# anything: the wrapper is driven with a fake sbx and a fake systemctl first
# on PATH, which record what they were asked to do and answer as told.
# ---------------------------------------------------------------------------


DEPLOY_FILE_LIST = (
    "deploy/profile.yaml",
    "deploy/sandbox-deploy.sh",
    "deploy/deploy.sh",
    "deploy/docker-compose.candidate.yml",
)


def _load_written_profile(repo: Path):
    from forge.deploy.profile import load_deploy_profile

    return load_deploy_profile(repo / "deploy" / "profile.yaml")


def test_deploy_port_writes_the_four_files(_isolate, tmp_path):
    repo = _make_repo(
        _isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n", extras=False
    )
    config = _write_config(tmp_path)

    result = _run(config, str(repo), "--deploy-port", "8911", "--json")

    assert result.exit_code == 0, result.output
    for relative in DEPLOY_FILE_LIST:
        assert (repo / relative).is_file(), relative
    assert _status_of(result, "deploy-files").count("added") == 4


def test_the_written_profile_names_the_sandbox_and_both_ports(_isolate, tmp_path):
    repo = _make_repo(
        _isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n", extras=False
    )
    config = _write_config(tmp_path)

    _run(config, str(repo), "--deploy-port", "8911")

    profile = _load_written_profile(repo)
    assert profile.sandbox is not None
    assert profile.sandbox.name == "bench-one-deploy"
    assert profile.sandbox.publish == (
        "127.0.0.1:8911:8911",
        "127.0.0.1:8912:8912",
    )
    assert profile.compose.script == "deploy/sandbox-deploy.sh"
    assert profile.cwd == str(repo)
    assert profile.candidate is not None
    assert profile.candidate.env["CANDIDATE_PORT"] == "8912"


def test_the_debian_and_python_rules_are_always_there(_isolate, tmp_path):
    repo = _make_repo(
        _isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n", extras=False
    )
    config = _write_config(tmp_path)

    _run(config, str(repo), "--deploy-port", "8911")

    allowed = _load_written_profile(repo).sandbox.allow_network
    assert "deb.debian.org" in allowed
    assert "*.debian.org" in allowed
    assert "pypi.org" in allowed
    assert "files.pythonhosted.org" in allowed


def test_deploy_allow_adds_the_model_door(_isolate, tmp_path):
    repo = _make_repo(
        _isolate, "agent-repo", toolchain="toolchain:\n  test: pytest\n", extras=False
    )
    config = _write_config(tmp_path)

    _run(
        config,
        str(repo),
        "--deploy-port",
        "8911",
        "--deploy-allow",
        "172.30.1.253:4000",
    )

    assert "172.30.1.253:4000" in _load_written_profile(repo).sandbox.allow_network


def test_without_deploy_allow_no_extra_host_is_reachable(_isolate, tmp_path):
    repo = _make_repo(
        _isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n", extras=False
    )
    config = _write_config(tmp_path)

    _run(config, str(repo), "--deploy-port", "8911")

    allowed = _load_written_profile(repo).sandbox.allow_network
    assert not any(host.startswith("172.") for host in allowed)


def test_the_deploy_script_carries_this_repository_s_project_and_ports(
    _isolate, tmp_path
):
    repo = _make_repo(
        _isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n", extras=False
    )
    config = _write_config(tmp_path)

    _run(config, str(repo), "--deploy-port", "8911")

    script = (repo / "deploy" / "deploy.sh").read_text(encoding="utf-8")
    assert 'COMPOSE_PROJECT="${COMPOSE_PROJECT:-bench-one}"' in script
    assert "http://localhost:8911/health" in script
    assert 'CANDIDATE_PORT="${CANDIDATE_PORT:-8912}"' in script
    # api_test's own names never travel to another repository.
    assert "apitest-f2" not in script


def test_no_placeholder_survives_in_either_script(_isolate, tmp_path):
    repo = _make_repo(
        _isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n", extras=False
    )
    config = _write_config(tmp_path)

    _run(config, str(repo), "--deploy-port", "8911")

    for relative in ("deploy/sandbox-deploy.sh", "deploy/deploy.sh"):
        assert "@@" not in (repo / relative).read_text(encoding="utf-8"), relative


def test_both_scripts_are_runnable(_isolate, tmp_path):
    repo = _make_repo(
        _isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n", extras=False
    )
    config = _write_config(tmp_path)

    _run(config, str(repo), "--deploy-port", "8911")

    for relative in ("deploy/sandbox-deploy.sh", "deploy/deploy.sh"):
        path = repo / relative
        assert os.access(path, os.X_OK), relative
        assert (
            subprocess.run(["bash", "-n", str(path)]).returncode == 0
        ), f"{relative} is not valid shell"


def test_without_the_flag_nothing_about_deploys_is_written(_isolate, tmp_path):
    repo = _make_repo(
        _isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n", extras=False
    )
    config = _write_config(tmp_path)

    result = _run(config, str(repo), "--json")

    assert result.exit_code == 0, result.output
    assert not (repo / "deploy").exists()
    assert _status_of(result, "deploy-files") == []
    # And the old warning still stands, word for word.
    assert ("deploy", "warn", "no deploy/profile.yaml") in _steps(result)


def test_a_profile_already_there_is_never_rewritten(_isolate, tmp_path):
    repo = _make_repo(
        _isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n", extras=False
    )
    (repo / "deploy").mkdir()
    written_by_hand = "# mine, thanks\nenv_id: local\ncompose:\n  file: dc.yml\n"
    (repo / "deploy" / "profile.yaml").write_text(written_by_hand, encoding="utf-8")
    config = _write_config(tmp_path)

    result = _run(config, str(repo), "--deploy-port", "8911", "--json")

    assert result.exit_code == 0, result.output
    assert (repo / "deploy" / "profile.yaml").read_text(
        encoding="utf-8"
    ) == written_by_hand
    assert "unchanged" in _status_of(result, "deploy-files")
    # The two scripts it did not have are still written.
    assert (repo / "deploy" / "sandbox-deploy.sh").is_file()


def test_a_dry_run_writes_no_deploy_file(_isolate, tmp_path):
    repo = _make_repo(
        _isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n", extras=False
    )
    config = _write_config(tmp_path)

    result = _run(config, str(repo), "--deploy-port", "8911", "--dry-run", "--json")

    assert result.exit_code == 0, result.output
    assert not (repo / "deploy").exists()
    assert _status_of(result, "deploy-files") == ["would-add"] * 4


def test_deploy_allow_without_deploy_port_is_refused(_isolate, tmp_path):
    repo = _make_repo(
        _isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n", extras=False
    )
    config = _write_config(tmp_path)

    result = _run(config, str(repo), "--deploy-allow", "172.30.1.253:4000")

    assert result.exit_code != 0
    assert "--deploy-allow" in result.output
    assert not (repo / "deploy").exists()
    # Nothing was registered either.
    assert "bench-one" not in config.read_text(encoding="utf-8")


@pytest.mark.parametrize("port", ["0", "65535", "-1"])
def test_a_port_the_pair_cannot_fit_in_is_refused(_isolate, tmp_path, port):
    repo = _make_repo(
        _isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n", extras=False
    )
    config = _write_config(tmp_path)

    result = _run(config, str(repo), "--deploy-port", port)

    assert result.exit_code != 0
    assert "--deploy-port" in result.output
    assert not (repo / "deploy").exists()


def test_a_bad_host_in_deploy_allow_is_refused_before_anything_is_written(
    _isolate, tmp_path
):
    repo = _make_repo(
        _isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n", extras=False
    )
    config = _write_config(tmp_path)

    result = _run(
        config, str(repo), "--deploy-port", "8911", "--deploy-allow", "http://door/"
    )

    assert result.exit_code != 0
    assert "deploy profile would not load" in result.output
    assert not (repo / "deploy" / "profile.yaml").exists()


def test_a_name_no_sandbox_could_carry_is_refused(_isolate, tmp_path):
    repo = _make_repo(
        _isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n", extras=False
    )
    config = _write_config(tmp_path)

    result = _run(config, str(repo), "--name", "___", "--deploy-port", "8911")

    assert result.exit_code != 0
    assert "Docker Sandbox" in result.output
    assert not (repo / "deploy").exists()


def test_the_name_becomes_the_project_and_the_sandbox(_isolate, tmp_path):
    from forge.cli.register_repo import compose_project_for, sandbox_name_for

    assert compose_project_for("api_test") == "api-test"
    assert sandbox_name_for("api_test") == "api-test-deploy"
    assert sandbox_name_for("content-agent-py") == "content-agent-py-deploy"
    assert sandbox_name_for("Bench.One") == "bench-one-deploy"
    assert sandbox_name_for("___") == ""


# ---------------------------------------------------------------------------
# The wrapper, driven — with a fake sbx and a fake systemctl first on PATH
#
# The wrapper is the only script the deploy step runs. It brings the sandbox
# up, runs the repository's own deploy script inside it, and hands back that
# script's exit code unchanged. Every test below drives the real file the
# command writes; the two fakes record every argument they are given and
# answer as the test tells them to. No real sbx, no real systemctl, no
# sandbox, no daemon.
# ---------------------------------------------------------------------------


FAKE_SBX = """#!/usr/bin/env bash
# A stand-in for Docker's `sbx`, put first on PATH by the test. It writes down
# every argument it is given and answers the way the test told it to. It never
# creates, starts, stops or looks at a real sandbox, and it never runs the real
# tool: no test in this file goes anywhere near the sandbox daemon.
#
# It models the three forms the real 0.39.0 tool actually has:
#   sbx ls                                                  lists the sandboxes
#   sbx policy check network --sandbox NAME TARGET           read-only question
#   sbx policy allow network --sandbox NAME RULES            adds the rules
# and `sbx create shell ...` and `sbx exec ...`.
#
# THE ANSWER TO THE QUESTION IS THE EXIT CODE: 0 means the target is allowed,
# anything else means it is not. The test names the allowed targets in
# SBX_ALLOWED (comma separated); SBX_POLICY_CHECK_STATUS forces one answer for
# every target, which is how "the tool could not answer at all" is played.
printf '%s\\n' "$*" >> "$SBX_LOG"
case "$1" in
  ls)
    printf '%s\\n' "${SBX_LS:-}"
    ;;
  policy)
    if [ "$2 $3" = "check network" ]; then
      if [ -n "${SBX_POLICY_CHECK_STATUS:-}" ]; then
        exit "${SBX_POLICY_CHECK_STATUS}"
      fi
      target="${@: -1}"
      case ",${SBX_ALLOWED:-}," in
        *",${target},"*) exit 0 ;;
        *) exit 1 ;;
      esac
    fi
    ;;
  exec)
    exit "${SBX_EXEC_STATUS:-0}"
    ;;
esac
exit 0
"""

FAKE_SYSTEMCTL = """#!/usr/bin/env bash
# A stand-in for systemctl. It writes down what it was asked to do and does
# nothing: no unit is started, stopped or reloaded by any test in this file.
printf '%s\\n' "$*" >> "$SYSTEMCTL_LOG"
exit 0
"""


@pytest.fixture
def wrapper_repo(_isolate, tmp_path):
    """A registered repository with its deploy files, plus the two fakes."""
    repo = _make_repo(
        _isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n", extras=False
    )
    config = _write_config(tmp_path)
    result = _run(config, str(repo), "--deploy-port", "8911")
    assert result.exit_code == 0, result.output

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    (fake_bin / "sbx").write_text(FAKE_SBX, encoding="utf-8")
    (fake_bin / "systemctl").write_text(FAKE_SYSTEMCTL, encoding="utf-8")
    for name in ("sbx", "systemctl"):
        (fake_bin / name).chmod(0o755)
    return repo, fake_bin


def _drive_wrapper(wrapper_repo, tmp_path, **env):
    """Run the wrapper with the fakes first on PATH; return (result, sbx, systemctl)."""
    repo, fake_bin = wrapper_repo
    sbx_log = tmp_path / "sbx.log"
    systemctl_log = tmp_path / "systemctl.log"
    sbx_log.write_text("", encoding="utf-8")
    systemctl_log.write_text("", encoding="utf-8")
    run_env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "SBX_LOG": str(sbx_log),
        "SYSTEMCTL_LOG": str(systemctl_log),
        "SANDBOX_NAME": "bench-one-deploy",
        "SANDBOX_MEMORY": "6g",
        "SANDBOX_CPUS": "4",
        "SANDBOX_PUBLISH": "127.0.0.1:8911:8911,127.0.0.1:8912:8912",
        "SANDBOX_ALLOW_NETWORK": "pypi.org,*.debian.org",
    }
    run_env.update({k: str(v) for k, v in env.items()})
    result = subprocess.run(
        [str(repo / "deploy" / "sandbox-deploy.sh")],
        cwd=repo,
        env=run_env,
        capture_output=True,
        text=True,
    )
    return (
        result,
        [line for line in sbx_log.read_text().splitlines() if line.strip()],
        [line for line in systemctl_log.read_text().splitlines() if line.strip()],
    )


def test_the_wrapper_creates_the_sandbox_when_it_is_not_there(wrapper_repo, tmp_path):
    result, sbx, _ = _drive_wrapper(wrapper_repo, tmp_path, SBX_LS="")

    assert result.returncode == 0, result.stderr
    created = [line for line in sbx if line.startswith("create ")]
    assert len(created) == 1
    assert "create shell" in created[0]
    assert "--name bench-one-deploy" in created[0]
    assert "--memory 6g" in created[0]
    assert "--cpus 4" in created[0]
    assert "--publish 127.0.0.1:8911:8911" in created[0]
    assert "--publish 127.0.0.1:8912:8912" in created[0]


def test_the_wrapper_does_not_create_a_sandbox_that_is_already_there(
    wrapper_repo, tmp_path
):
    result, sbx, _ = _drive_wrapper(
        wrapper_repo, tmp_path, SBX_LS="bench-one-deploy   running"
    )

    assert result.returncode == 0, result.stderr
    assert [line for line in sbx if line.startswith("create ")] == []


def test_a_similar_name_is_not_mistaken_for_this_sandbox(wrapper_repo, tmp_path):
    result, sbx, _ = _drive_wrapper(
        wrapper_repo, tmp_path, SBX_LS="bench-one-deploy-old   running"
    )

    assert result.returncode == 0, result.stderr
    assert len([line for line in sbx if line.startswith("create ")]) == 1


def test_the_network_rules_are_added_once_in_one_call(wrapper_repo, tmp_path):
    result, sbx, _ = _drive_wrapper(wrapper_repo, tmp_path, SBX_LS="")

    assert result.returncode == 0, result.stderr
    allowed = [line for line in sbx if line.startswith("policy allow ")]
    assert allowed == [
        "policy allow network --sandbox bench-one-deploy pypi.org,*.debian.org"
    ]


def test_each_address_is_asked_about_one_at_a_time(wrapper_repo, tmp_path):
    # The real tool judges a bare host name as if it were being reached over
    # HTTPS on port 443, but the Debian mirrors are fetched over plain HTTP, so
    # a bare host has to be asked about as an http:// address. An entry that
    # already names a port is asked about exactly as it is written.
    result, sbx, _ = _drive_wrapper(
        wrapper_repo,
        tmp_path,
        SBX_LS="bench-one-deploy   running",
        SANDBOX_ALLOW_NETWORK="pypi.org,172.30.1.253:4000",
        SBX_ALLOWED="http://pypi.org,172.30.1.253:4000",
    )

    assert result.returncode == 0, result.stderr
    assert [line for line in sbx if line.startswith("policy check ")] == [
        "policy check network --sandbox bench-one-deploy http://pypi.org",
        "policy check network --sandbox bench-one-deploy 172.30.1.253:4000",
    ]
    assert [line for line in sbx if line.startswith("policy allow ")] == []


def test_the_rules_are_not_added_again_when_they_are_already_allowed(
    wrapper_repo, tmp_path
):
    result, sbx, _ = _drive_wrapper(
        wrapper_repo,
        tmp_path,
        SBX_LS="bench-one-deploy   running",
        SBX_ALLOWED="http://pypi.org,http://*.debian.org",
    )

    assert result.returncode == 0, result.stderr
    assert [line for line in sbx if line.startswith("policy allow ")] == []


def test_a_missing_rule_means_the_whole_set_is_added(wrapper_repo, tmp_path):
    result, sbx, _ = _drive_wrapper(
        wrapper_repo,
        tmp_path,
        SBX_LS="bench-one-deploy   running",
        SBX_ALLOWED="http://pypi.org",
    )

    assert result.returncode == 0, result.stderr
    assert len([line for line in sbx if line.startswith("policy allow ")]) == 1


def test_rules_are_added_when_the_question_cannot_be_answered(
    wrapper_repo, tmp_path
):
    # A tool that cannot answer the question must not leave a sandbox walled
    # off — adding a rule that is already there changes nothing.
    result, sbx, _ = _drive_wrapper(
        wrapper_repo,
        tmp_path,
        SBX_LS="bench-one-deploy   running",
        SBX_POLICY_CHECK_STATUS="2",
    )

    assert result.returncode == 0, result.stderr
    assert len([line for line in sbx if line.startswith("policy allow ")]) == 1


def test_the_keeper_is_started_for_this_sandbox(wrapper_repo, tmp_path):
    result, _, systemctl = _drive_wrapper(wrapper_repo, tmp_path, SBX_LS="")

    assert result.returncode == 0, result.stderr
    assert systemctl == ["--user start forge-sandbox-keeper@bench-one-deploy"]


def test_the_deploy_script_runs_inside_with_exactly_the_named_settings(
    wrapper_repo, tmp_path
):
    repo, _ = wrapper_repo
    result, sbx, _ = _drive_wrapper(wrapper_repo, tmp_path, SBX_LS="")

    assert result.returncode == 0, result.stderr
    ran = [line for line in sbx if line.startswith("exec ")]
    assert len(ran) == 1
    assert ran[0] == (
        f"exec -w {repo} "
        "-e CANDIDATE -e PROMOTE -e REVERT -e CANDIDATE_DOWN "
        "-e CANDIDATE_PORT -e ROLLBACK_IMAGE_REF -e ENV_FILE "
        "bench-one-deploy deploy/deploy.sh"
    )


@pytest.mark.parametrize("status", ["0", "1", "2", "7"])
def test_the_inner_exit_code_comes_back_unchanged(wrapper_repo, tmp_path, status):
    result, _, _ = _drive_wrapper(
        wrapper_repo, tmp_path, SBX_LS="", SBX_EXEC_STATUS=status
    )

    assert result.returncode == int(status), result.stderr
    assert f"exited {status}" in result.stdout


def test_no_sandbox_name_means_it_refuses_and_touches_nothing(wrapper_repo, tmp_path):
    result, sbx, systemctl = _drive_wrapper(
        wrapper_repo, tmp_path, SBX_LS="", SANDBOX_NAME=""
    )

    assert result.returncode == 2
    assert "SANDBOX_NAME is not set" in result.stdout
    assert sbx == []
    assert systemctl == []


def test_no_network_rules_means_no_policy_call_at_all(wrapper_repo, tmp_path):
    result, sbx, _ = _drive_wrapper(
        wrapper_repo, tmp_path, SBX_LS="", SANDBOX_ALLOW_NETWORK=""
    )

    assert result.returncode == 0, result.stderr
    assert [line for line in sbx if line.startswith("policy ")] == []


def test_settings_left_empty_are_left_off_the_create(wrapper_repo, tmp_path):
    result, sbx, _ = _drive_wrapper(
        wrapper_repo,
        tmp_path,
        SBX_LS="",
        SANDBOX_MEMORY="",
        SANDBOX_CPUS="",
        SANDBOX_PUBLISH="",
    )

    assert result.returncode == 0, result.stderr
    created = [line for line in sbx if line.startswith("create ")][0]
    assert created == f"create shell {wrapper_repo[0]} --name bench-one-deploy"


# ---------------------------------------------------------------------------
# The candidate overlay (rule 14) — the file that puts the throwaway copy on
# its own port. deploy/deploy.sh has always layered this file on top of
# docker-compose.yml for the candidate leg; until now register-repo did not
# write it, so a repository born by this command could not run that leg.
# ---------------------------------------------------------------------------


def test_the_candidate_overlay_is_written_with_this_repository_s_ports(
    _isolate, tmp_path
):
    repo = _make_repo(
        _isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n", extras=False
    )
    config = _write_config(tmp_path)

    _run(config, str(repo), "--deploy-port", "8911")

    overlay = (repo / "deploy" / "docker-compose.candidate.yml").read_text(
        encoding="utf-8"
    )
    # The candidate publishes on the port above the app's, and the app still
    # listens on its own port inside the container.
    assert '- "${CANDIDATE_PORT:-8912}:8911"' in overlay
    # !override REPLACES the base file's port list. Without it the candidate
    # would also try to publish the live port and `up` would fail.
    assert "ports: !override" in overlay
    assert "@@" not in overlay


def test_the_overlay_is_the_file_the_deploy_script_looks_for(_isolate, tmp_path):
    repo = _make_repo(
        _isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n", extras=False
    )
    config = _write_config(tmp_path)

    _run(config, str(repo), "--deploy-port", "8911")

    script = (repo / "deploy" / "deploy.sh").read_text(encoding="utf-8")
    assert "deploy/docker-compose.candidate.yml" in script
    assert (repo / "deploy" / "docker-compose.candidate.yml").is_file()


def test_the_overlay_is_compose_yaml_that_names_only_the_app_s_ports(
    _isolate, tmp_path
):
    repo = _make_repo(
        _isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n", extras=False
    )
    config = _write_config(tmp_path)

    _run(config, str(repo), "--deploy-port", "8911")

    text = (repo / "deploy" / "docker-compose.candidate.yml").read_text(
        encoding="utf-8"
    )

    # `!override` is compose's own merge tag, which plain YAML does not know,
    # so it is read here with the tag ignored — the point of the check is the
    # shape of the file: one service, one key changed, nothing else.
    class _IgnoreTags(yaml.SafeLoader):
        pass

    _IgnoreTags.add_constructor(
        "!override", lambda loader, node: loader.construct_sequence(node)
    )
    parsed = yaml.load(text, Loader=_IgnoreTags)
    assert list(parsed) == ["services"]
    assert list(parsed["services"]) == ["app"]
    assert list(parsed["services"]["app"]) == ["ports"]


# ---------------------------------------------------------------------------
# The wrapper is ONE file (rule 13 of the 15:10Z amendment)
#
# api_test and every repository born by this command deploy with the same
# wrapper, byte for byte. It holds no value belonging to any one repository:
# the sandbox's name, size, ports and rules all reach it in its environment.
# ---------------------------------------------------------------------------


def _api_test_wrapper() -> Path | None:
    """api_test's own wrapper, if a checkout of it sits beside this one."""
    estate = Path(__file__).resolve().parents[3].parent
    for checkout in ("api_test-wt-sandbox", "api_test"):
        candidate = estate / checkout / "deploy" / "sandbox-deploy.sh"
        if candidate.is_file():
            return candidate
    return None


def test_the_shipped_wrapper_is_api_test_s_wrapper_byte_for_byte():
    theirs = _api_test_wrapper()
    if theirs is None:
        pytest.skip(
            "no api_test checkout beside this one, so there is nothing to "
            "compare the shipped wrapper with"
        )
    ours = (
        Path(register_repo.__file__).resolve().parent
        / "deploy_templates"
        / "sandbox-deploy.sh"
    )
    assert ours.read_bytes() == theirs.read_bytes(), (
        f"{ours} and {theirs} have drifted apart; they are meant to be one "
        "file, so whichever changed should be copied over the other"
    )


def test_the_wrapper_written_into_a_repository_is_the_shipped_file_unchanged(
    _isolate, tmp_path
):
    repo = _make_repo(
        _isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n", extras=False
    )
    config = _write_config(tmp_path)

    _run(config, str(repo), "--deploy-port", "8911")

    shipped = (
        Path(register_repo.__file__).resolve().parent
        / "deploy_templates"
        / "sandbox-deploy.sh"
    )
    written = repo / "deploy" / "sandbox-deploy.sh"
    assert written.read_bytes() == shipped.read_bytes()
    # Nothing in it is filled in for this repository — not even its name.
    assert "bench-one" not in written.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The closing line (rule 14) — it reports the profile that is on disk, never
# the ports the run happened to ask for.
# ---------------------------------------------------------------------------


def _closing_line(result) -> str:
    return [
        detail
        for name, status, detail in _steps(result)
        if name == "deploy-files" and status == "ok"
    ][0]


def test_the_closing_line_names_the_sandbox_and_the_ports_just_written(
    _isolate, tmp_path
):
    repo = _make_repo(
        _isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n", extras=False
    )
    config = _write_config(tmp_path)

    result = _run(config, str(repo), "--deploy-port", "8911", "--json")

    assert result.exit_code == 0, result.output
    assert _closing_line(result) == "sandbox bench-one-deploy on ports 8911 and 8912"


def test_a_re_run_reports_the_ports_the_profile_on_disk_carries(_isolate, tmp_path):
    # The profile that is already there is left alone, so the ports it names
    # are the ports this repository really deploys on — reporting the ones the
    # command was asked for would name ports nothing uses.
    repo = _make_repo(
        _isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n", extras=False
    )
    config = _write_config(tmp_path)
    assert _run(config, str(repo), "--deploy-port", "9000").exit_code == 0

    result = _run(config, str(repo), "--deploy-port", "8911", "--json")

    assert result.exit_code == 0, result.output
    assert _closing_line(result) == "sandbox bench-one-deploy on ports 9000 and 9001"
    assert _status_of(result, "deploy-files").count("added") == 0


def test_a_hand_written_profile_with_no_sandbox_is_reported_as_unchanged(
    _isolate, tmp_path
):
    repo = _make_repo(
        _isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n", extras=False
    )
    config = _write_config(tmp_path)
    assert _run(config, str(repo), "--deploy-port", "8911").exit_code == 0
    # Someone replaces the profile with one that deploys on the host, with no
    # sandbox block at all. There are then no sandbox ports to report.
    (repo / "deploy" / "profile.yaml").write_text(
        'format_version: "1.0"\nenv_id: local\ncompose:\n  file: docker-compose.yml\n',
        encoding="utf-8",
    )

    result = _run(config, str(repo), "--deploy-port", "8911", "--json")

    assert result.exit_code == 0, result.output
    assert _closing_line(result) == "deploy files already present, unchanged"


def test_the_ports_sentence_reads_plainly_for_one_port_and_for_three():
    assert register_repo._ports_phrase(["8911"]) == "port 8911"
    assert register_repo._ports_phrase(["8911", "8912"]) == "ports 8911 and 8912"
    assert register_repo._ports_phrase(["1", "2", "3"]) == "ports 1, 2 and 3"


def test_the_host_port_is_read_from_every_shape_a_publish_rule_takes():
    assert register_repo._host_ports_of(
        ("8080", "9000:8080", "127.0.0.1:8911:8901")
    ) == ["8080", "9000", "8911"]
    assert register_repo._host_ports_of(()) == []
