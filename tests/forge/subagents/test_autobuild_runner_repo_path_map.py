"""The build runner finds a repository where the factory says it is.

WHY THIS FILE EXISTS. On 2026-09-11 the first routine feature build after
routine builds moved inside the repository's sandbox died two seconds
after launch with "unable to resolve repo path for repo='guardkit/api_test'".
Nothing was wrong with the repository: the runner was GUESSING where it
lives. It took the last part of the name and looked for it under a base
directory that defaults to ``~/Projects/appmilla_github`` — and inside the
sandbox the runner is the user ``agent``, so ``~`` is ``/home/agent`` and
the guess pointed at a directory that has never existed. The repository
was mounted all along, at the same path it has on the host, and that exact
path was already written in the factory's own configuration, in
``planning.target_repo_paths``, which this same module already reads for
the permissions allowlist and the routine seat.

WHAT IS PINNED HERE. The map is consulted FIRST, by exact key, including
the namespace alias the queue actually uses. A repository the map does not
name still resolves exactly as it did before. A path that comes from the
map is held to exactly the same checks as a guessed one, and when it fails
one of them the build is REFUSED — never quietly swapped for the guess,
because a wrong path in the factory's own configuration is an operator's
mistake the factory has to show. And no way of failing to read the
configuration can kill a build.

HOW REAL THIS IS. Real git repositories created in temporary directories
and a real ``forge.yaml`` on disk read through the real config loader.
``HOME`` and ``FORGE_REPO_BASE`` are pointed at a directory that does NOT
contain the repository, which is the sandbox's situation exactly and is
the test that would have caught the defect. No live service is touched.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from forge.subagents import autobuild_runner as ar
from forge.subagents.autobuild_runner import (
    FORGE_DEFAULT_REPO_ENV,
    FORGE_DEFAULT_REPO_OPT_IN_ENV,
    FORGE_REPO_BASE_ENV,
    MISSING_REPO_REFUSAL,
    _resolve_repo_path,
    repo_resolution_failure_reason,
)

LOGGER_NAME = "forge.subagents.autobuild_runner"

#: The name the queue uses today, and the alias the same repository also
#: answers to in the live configuration.
REPO_KEY = "guardkit/api_test"
REPO_ALIAS = "appmilla_github/api_test"


# ---------------------------------------------------------------------------
# Fixtures — nothing inherited from the machine running the suite
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_inherited_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every test states its own world.

    The resolver reads four things from the environment: the repo base,
    the two default-repo switches, and ``$FORGE_CONFIG_PATH``. It also
    falls back to ``forge.yaml`` beside the working directory, and a
    developer running this suite from the forge checkout has a real one
    sitting right there — so the working directory is an empty temporary
    one and each test writes whatever configuration it wants.
    """
    monkeypatch.delenv(FORGE_REPO_BASE_ENV, raising=False)
    monkeypatch.delenv(FORGE_DEFAULT_REPO_ENV, raising=False)
    monkeypatch.delenv(FORGE_DEFAULT_REPO_OPT_IN_ENV, raising=False)
    monkeypatch.delenv("FORGE_CONFIG_PATH", raising=False)
    cwd = tmp_path / "empty-cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)


def _make_git_repo(path: Path) -> Path:
    """Create ``path`` as a real git repository (the resolver checks ``.git``)."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    return path


def _write_config(
    cwd_file: Path,
    *,
    allowlist: list[str],
    repo_paths: dict[str, str] | None = None,
    include_planning: bool = True,
) -> Path:
    """Write a real ``forge.yaml`` that the real loader accepts.

    ``permissions`` is the one required section on the whole document, so
    a configuration meant to LOAD has to carry one or the test would be
    exercising a validation failure while claiming to test the map.
    """
    lines = ["permissions:", "  filesystem:", "    allowlist:"]
    lines += [f"    - {entry}" for entry in allowlist]
    if include_planning:
        lines.append("planning:")
        if repo_paths:
            lines.append("  target_repo_paths:")
            lines += [f"    {key}: {value}" for key, value in repo_paths.items()]
        else:
            lines.append("  enabled: false")
    cwd_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return cwd_file


def _sandbox_environment(monkeypatch: pytest.MonkeyPatch, nowhere: Path) -> None:
    """Point HOME and the repo base at a place with no repository in it.

    This is the sandbox's situation: the runner is a different user, so
    ``~`` expands somewhere else entirely, and the environment carries no
    ``FORGE_REPO_BASE``. Before this lane, every resolution in this state
    failed.
    """
    nowhere.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(nowhere))
    monkeypatch.delenv(FORGE_REPO_BASE_ENV, raising=False)


def _messages(caplog: pytest.LogCaptureFixture) -> str:
    return " ".join(record.getMessage() for record in caplog.records)


# ---------------------------------------------------------------------------
# The map is consulted first, and it is what the sandbox needed
# ---------------------------------------------------------------------------


def test_the_configured_path_is_found_where_the_guess_could_never_look(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The defect of 2026-09-11, in a test: HOME is wrong, the map is right."""
    repo = _make_git_repo(tmp_path / "checkouts" / "api_test")
    _sandbox_environment(monkeypatch, tmp_path / "home-agent")
    cfg = _write_config(
        Path("forge.yaml"),
        allowlist=[str(tmp_path)],
        repo_paths={REPO_KEY: str(repo)},
    )

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        resolved = _resolve_repo_path({"repo": REPO_KEY})

    assert resolved == repo
    # The route is stated in one plain line, and it names the file it read.
    assert "comes from the factory's configuration" in _messages(caplog)
    assert str(cfg) in _messages(caplog) or cfg.name in _messages(caplog)


def test_the_namespace_alias_resolves_the_same_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Builds are queued under ``appmilla_github/<name>``; the map carries both."""
    repo = _make_git_repo(tmp_path / "checkouts" / "api_test")
    _sandbox_environment(monkeypatch, tmp_path / "home-agent")
    _write_config(
        Path("forge.yaml"),
        allowlist=[str(tmp_path)],
        repo_paths={REPO_KEY: str(repo), REPO_ALIAS: str(repo)},
    )

    assert _resolve_repo_path({"repo": REPO_ALIAS}) == repo


def test_the_lookup_is_by_exact_key_not_by_basename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A name the map does not carry is a miss, even if a sibling key shares
    its last part — the base-directory route then answers, as it always did."""
    mapped = _make_git_repo(tmp_path / "mapped" / "api_test")
    under_base = _make_git_repo(tmp_path / "base" / "api_test")
    monkeypatch.setenv(FORGE_REPO_BASE_ENV, str(tmp_path / "base"))
    _write_config(
        Path("forge.yaml"),
        allowlist=[str(tmp_path)],
        repo_paths={REPO_KEY: str(mapped)},
    )

    # A DIFFERENT namespace: same basename, key not in the map.
    assert _resolve_repo_path({"repo": "someone_else/api_test"}) == under_base


def test_the_configured_path_is_read_from_forge_config_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``$FORGE_CONFIG_PATH`` names the file, exactly as it does for the
    permissions allowlist and the routine seat — this is the environment
    the runner actually has inside the sandbox."""
    repo = _make_git_repo(tmp_path / "checkouts" / "api_test")
    _sandbox_environment(monkeypatch, tmp_path / "home-agent")
    elsewhere = tmp_path / "forge-state" / "forge.yaml"
    elsewhere.parent.mkdir(parents=True)
    _write_config(
        elsewhere, allowlist=[str(tmp_path)], repo_paths={REPO_KEY: str(repo)}
    )
    monkeypatch.setenv("FORGE_CONFIG_PATH", str(elsewhere))

    assert _resolve_repo_path({"repo": REPO_KEY}) == repo


# ---------------------------------------------------------------------------
# Nothing else changed: the old route still answers
# ---------------------------------------------------------------------------


def test_a_repository_absent_from_the_map_still_resolves_under_the_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A configuration that says nothing about this repository changes nothing."""
    other = _make_git_repo(tmp_path / "checkouts" / "ts-api-test")
    repo = _make_git_repo(tmp_path / "base" / "api_test")
    monkeypatch.setenv(FORGE_REPO_BASE_ENV, str(tmp_path / "base"))
    _write_config(
        Path("forge.yaml"),
        allowlist=[str(tmp_path)],
        repo_paths={"guardkit/ts-api-test": str(other)},
    )

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        assert _resolve_repo_path({"repo": REPO_KEY}) == repo

    assert "is not named in the factory's configuration" in _messages(caplog)


def test_no_configuration_file_at_all_behaves_exactly_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``forge.yaml`` anywhere in reach: the base-directory route, untouched."""
    repo = _make_git_repo(tmp_path / "base" / "api_test")
    monkeypatch.setenv(FORGE_REPO_BASE_ENV, str(tmp_path / "base"))

    assert _resolve_repo_path({"repo": REPO_KEY}) == repo


def test_a_planning_section_with_no_map_falls_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configuration with a planning section but no map is simply a miss."""
    repo = _make_git_repo(tmp_path / "base" / "api_test")
    monkeypatch.setenv(FORGE_REPO_BASE_ENV, str(tmp_path / "base"))
    _write_config(Path("forge.yaml"), allowlist=[str(tmp_path)], repo_paths=None)

    assert _resolve_repo_path({"repo": REPO_KEY}) == repo


# ---------------------------------------------------------------------------
# A configured path that is wrong is REFUSED, never swapped for the guess
# ---------------------------------------------------------------------------


def test_a_configured_path_that_does_not_exist_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """And it is refused EVEN THOUGH the guess would have found a repository.

    This is the heart of the rule: a wrong path in the factory's own
    configuration is an operator's mistake, and a build that quietly
    builds a different checkout instead reports green for the wrong work.
    """
    _make_git_repo(tmp_path / "base" / "api_test")  # the guess WOULD succeed
    monkeypatch.setenv(FORGE_REPO_BASE_ENV, str(tmp_path / "base"))
    missing = tmp_path / "checkouts" / "api_test"
    _write_config(
        Path("forge.yaml"),
        allowlist=[str(tmp_path)],
        repo_paths={REPO_KEY: str(missing)},
    )

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        assert _resolve_repo_path({"repo": REPO_KEY}) is None

    assert "does not exist on disk" in _messages(caplog)


def test_a_configured_path_that_is_not_a_directory_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A file where a checkout should be is refused, with today's wording."""
    _make_git_repo(tmp_path / "base" / "api_test")
    monkeypatch.setenv(FORGE_REPO_BASE_ENV, str(tmp_path / "base"))
    a_file = tmp_path / "checkouts" / "api_test"
    a_file.parent.mkdir(parents=True)
    a_file.write_text("not a checkout\n", encoding="utf-8")
    _write_config(
        Path("forge.yaml"),
        allowlist=[str(tmp_path)],
        repo_paths={REPO_KEY: str(a_file)},
    )

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        assert _resolve_repo_path({"repo": REPO_KEY}) is None

    assert "is not a directory" in _messages(caplog)


def test_a_configured_path_that_is_not_a_git_repository_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A directory with no ``.git`` marker is refused, with today's wording."""
    _make_git_repo(tmp_path / "base" / "api_test")
    monkeypatch.setenv(FORGE_REPO_BASE_ENV, str(tmp_path / "base"))
    plain = tmp_path / "checkouts" / "api_test"
    plain.mkdir(parents=True)
    _write_config(
        Path("forge.yaml"),
        allowlist=[str(tmp_path)],
        repo_paths={REPO_KEY: str(plain)},
    )

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        assert _resolve_repo_path({"repo": REPO_KEY}) is None

    assert "is not a git repo" in _messages(caplog)


def test_a_configured_path_outside_the_allowlist_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The allowlist gate applies to a configured path as it does to a guess."""
    outside = _make_git_repo(tmp_path / "outside" / "api_test")
    monkeypatch.setenv(FORGE_REPO_BASE_ENV, str(tmp_path / "base"))
    _write_config(
        Path("forge.yaml"),
        allowlist=[str(tmp_path / "base")],
        repo_paths={REPO_KEY: str(outside)},
    )

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        assert _resolve_repo_path({"repo": REPO_KEY}) is None

    assert "outside the" in _messages(caplog)


# ---------------------------------------------------------------------------
# Reading the configuration can never kill a build
# ---------------------------------------------------------------------------


def test_a_malformed_configuration_warns_and_falls_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Broken YAML: one plain warning, no exception, today's route answers."""
    repo = _make_git_repo(tmp_path / "base" / "api_test")
    monkeypatch.setenv(FORGE_REPO_BASE_ENV, str(tmp_path / "base"))
    Path("forge.yaml").write_text("planning: [this is not\n  valid: yaml\n", "utf-8")

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        assert _resolve_repo_path({"repo": REPO_KEY}) == repo

    assert "could not read the repository map" in _messages(caplog)


def test_a_configuration_the_loader_refuses_falls_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A map value that is not a path is refused by the loader, not by a build.

    The document is well-formed YAML and invalid configuration — the shape
    an operator typo actually takes. The build still runs, on the route it
    always used, and the warning says what was ignored.
    """
    repo = _make_git_repo(tmp_path / "base" / "api_test")
    monkeypatch.setenv(FORGE_REPO_BASE_ENV, str(tmp_path / "base"))
    Path("forge.yaml").write_text(
        "permissions:\n"
        "  filesystem:\n"
        f"    allowlist:\n    - {tmp_path}\n"
        "planning:\n"
        "  target_repo_paths:\n"
        f"    {REPO_KEY}: 12345\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        assert _resolve_repo_path({"repo": REPO_KEY}) == repo

    assert "could not read the repository map" in _messages(caplog)


def test_an_unreadable_configuration_falls_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A file that cannot be opened is a warning and a fall-through."""
    repo = _make_git_repo(tmp_path / "base" / "api_test")
    monkeypatch.setenv(FORGE_REPO_BASE_ENV, str(tmp_path / "base"))
    cfg = Path("forge.yaml")
    _write_config(cfg, allowlist=[str(tmp_path)], repo_paths={REPO_KEY: str(repo)})
    cfg.chmod(0o000)
    try:
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            assert _resolve_repo_path({"repo": REPO_KEY}) == repo
    finally:
        cfg.chmod(0o644)

    assert "could not read the repository map" in _messages(caplog)


# ---------------------------------------------------------------------------
# The refusal that comes before everything
# ---------------------------------------------------------------------------


def test_a_launch_with_no_repo_refuses_before_the_map_is_consulted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No repo named means no build, whatever the configuration says.

    The map answers "where does this repository live", never "which
    repository is this" — so a launch that lost its repo is refused
    without the map being opened at all.
    """
    repo = _make_git_repo(tmp_path / "checkouts" / "api_test")
    _write_config(
        Path("forge.yaml"),
        allowlist=[str(tmp_path)],
        repo_paths={REPO_KEY: str(repo)},
    )

    def _must_not_be_called(repo_key: str) -> Path | None:
        raise AssertionError(
            "the repository map was consulted for a launch that named no repo"
        )

    monkeypatch.setattr(ar, "_configured_repo_path", _must_not_be_called)

    assert _resolve_repo_path({"repo": None}) is None
    assert repo_resolution_failure_reason({"repo": None}) == MISSING_REPO_REFUSAL
