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
from click.testing import CliRunner

from forge.cli import register_repo
from forge.cli.main import main
from forge.config.loader import load_config

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
    monkeypatch.setattr(register_repo, "_estate_status_views", lambda: [])
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


def test_the_estate_gate_reports_non_terminal_builds_and_still_prints_the_command(
    _isolate, tmp_path, monkeypatch
):
    repo = _make_repo(_isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n")
    config = _write_config(tmp_path)
    monkeypatch.setattr(register_repo, "_estate_status_views", lambda: ["a", "b"])
    monkeypatch.setattr(register_repo, "_all_terminal", lambda views: False)

    result = _run(config, str(repo))

    assert result.exit_code == 0, result.output
    assert "estate" in result.output
    assert "2 builds not terminal" in result.output
    assert "run: bash ops/forge-prod-recreate.sh" in result.output


def test_an_unreadable_build_ledger_warns_rather_than_stopping(
    _isolate, tmp_path, monkeypatch
):
    repo = _make_repo(_isolate, "bench-one", toolchain="toolchain:\n  test: pytest\n")
    config = _write_config(tmp_path)

    def _boom():
        raise RuntimeError("no such database")

    monkeypatch.setattr(register_repo, "_estate_status_views", _boom)

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
