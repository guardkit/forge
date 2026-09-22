"""What a project declares its builds need, by name (22 September 2026).

The launch list is the FACTORY'S own: its binary, its settings file, its
receipts folder, its bus, its memory, the model seat. It carries nothing
belonging to any project's tools, because central code that named one tool's
setting would be a factory with a favourite language. So a project says for
itself what its own builds need, in its own ``.guardkit/config.yaml``:

    launch:
      settings: [SOME_TOOL_CACHE, ANOTHER_HOME]

NAMES ONLY. A value never comes out of a project's file: the launch takes each
value from the launching process, and only if it has one.

These tests hold down the reading, the checking, and the launch. Nothing here
starts a process, reads anyone's real environment, or touches any service: the
"parent environment" is a dictionary made here and the settings file is a
string.
"""

from __future__ import annotations

import pytest

from forge.launch_environment import (
    GUARDKIT_FACTORY_LAUNCH_ENV,
    MAX_DECLARED_SETTINGS,
    build_launch_env,
    declared_setting_refusal,
    launch_setting_names,
)
from forge.planning.declared_memory import (
    MAX_DECLARATION_DEPTH,
    read_declared_launch_settings,
    read_declared_memory,
)

REPO = "guardkit/api_test"
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _read(text: str | None, *, found: bool = True):
    return read_declared_launch_settings(
        repo=REPO, commit=COMMIT, content=text, found=found
    )


# ---------------------------------------------------------------------------
# Reading what the project said
# ---------------------------------------------------------------------------


def test_the_names_a_project_declares_are_read_in_order() -> None:
    answer = _read(
        "memory:\n  project: widget_shop\n"
        "launch:\n  settings: [SOME_TOOL_CACHE, ANOTHER_HOME]\n"
    )
    assert answer.ok
    assert answer.names == ("SOME_TOOL_CACHE", "ANOTHER_HOME")
    assert answer.declared


def test_a_project_that_says_nothing_about_its_launch_asks_for_nothing() -> None:
    answer = _read("memory:\n  project: widget_shop\n")
    assert answer.ok and answer.names == () and not answer.declared


def test_a_launch_block_with_no_settings_is_the_same_as_saying_nothing() -> None:
    answer = _read("memory:\n  project: x\nlaunch:\n  timeout: 30\n")
    assert answer.ok and answer.names == ()


def test_declared_and_empty_is_a_different_fact_from_saying_nothing() -> None:
    """Somebody wrote the block and asked for nothing. That is a decision."""
    answer = _read("memory:\n  project: x\nlaunch:\n  settings:\n")
    assert answer.ok and answer.names == () and answer.declared


def test_the_same_name_twice_is_read_once() -> None:
    answer = _read("launch:\n  settings: [A_CACHE, A_CACHE]\n")
    assert answer.names == ("A_CACHE",)


def test_a_list_longer_than_the_bound_is_refused() -> None:
    many = ", ".join(f"NAME_{i}" for i in range(MAX_DECLARED_SETTINGS + 1))
    answer = _read(f"launch:\n  settings: [{many}]\n")
    assert not answer.ok
    assert f"at most {MAX_DECLARED_SETTINGS}" in answer.refusal


def test_something_that_is_not_a_list_of_names_is_refused() -> None:
    answer = _read("launch:\n  settings: SOME_TOOL_CACHE\n")
    assert not answer.ok and "not a list of names" in answer.refusal


# ---------------------------------------------------------------------------
# The names the factory keeps for itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, because",
    [
        ("FORGE_DB_PATH", "keeps for itself"),
        ("GUARDKIT_MEMORY_PROJECT", "keeps for itself"),
        ("FLEET_MEMORY_PG_DSN", "keeps for itself"),
        ("OPENAI_BASE_URL", "keeps for itself"),
        ("ANTHROPIC_BASE_URL", "keeps for itself"),
        ("PATH", "sets itself"),
        ("HOME", "sets itself"),
        ("TMPDIR", "sets itself"),
        ("SSH_AUTH_SOCK", "sets itself"),
        ("GH_TOKEN", "credential-shaped"),
        ("SOME_SECRET", "credential-shaped"),
        ("MY_API_KEY", "credential-shaped"),
        ("DB_PASSWORD", "credential-shaped"),
    ],
)
def test_a_reserved_name_is_refused_and_says_why(name: str, because: str) -> None:
    refusal = declared_setting_refusal(name)
    assert refusal is not None and because in refusal
    answer = _read(f"launch:\n  settings: [{name}]\n")
    assert not answer.ok and name in answer.refusal


@pytest.mark.parametrize("bad", ["MY-CACHE", "2CACHE", "my cache", "", "a.b"])
def test_a_name_that_is_not_a_setting_name_is_refused(bad: str) -> None:
    assert declared_setting_refusal(bad) is not None


def test_a_name_that_is_not_text_is_refused() -> None:
    answer = _read("launch:\n  settings: [3]\n")
    assert not answer.ok and "must be text" in answer.refusal


def test_an_ordinary_toolchain_name_is_allowed_whatever_tool_it_belongs_to() -> None:
    """Central code knows nothing about what these belong to, and must not."""
    for name in ("UV_CACHE_DIR", "GRADLE_USER_HOME", "CARGO_HOME", "GOPATH", "npm_config_cache"):
        assert declared_setting_refusal(name) is None


# ---------------------------------------------------------------------------
# The launch itself
# ---------------------------------------------------------------------------


PARENT = {
    "PATH": "/opt/venv/bin",
    "HOME": "/home/agent",
    "SOME_TOOL_CACHE": "/scratch/cache",
    "ANOTHER_HOME": "/opt/toolchain",
    "NOT_DECLARED": "should not travel",
    "GH_TOKEN": "the-credential-nobody-should-see",
}


def test_a_declared_name_arrives_with_the_parents_value() -> None:
    env = build_launch_env(
        parent=PARENT,
        memory_project="widget_shop",
        declared=["SOME_TOOL_CACHE", "ANOTHER_HOME"],
    )
    assert env["SOME_TOOL_CACHE"] == "/scratch/cache"
    assert env["ANOTHER_HOME"] == "/opt/toolchain"


def test_an_undeclared_name_does_not_travel() -> None:
    env = build_launch_env(parent=PARENT, declared=["SOME_TOOL_CACHE"])
    assert "NOT_DECLARED" not in env
    assert "GH_TOKEN" not in env


def test_a_declared_name_the_parent_does_not_have_stays_unset() -> None:
    env = build_launch_env(parent=PARENT, declared=["NOT_IN_THE_PARENT"])
    assert "NOT_IN_THE_PARENT" not in env


def test_a_reserved_name_that_somehow_reached_the_launch_is_dropped() -> None:
    """The door refuses these in plain words; this is the second fence."""
    env = build_launch_env(
        parent={**PARENT, "FORGE_DB_PATH": "/state/forge.db"},
        declared=["FORGE_DB_PATH", "GH_TOKEN", "SOME_TOOL_CACHE"],
    )
    assert "FORGE_DB_PATH" not in env
    assert "GH_TOKEN" not in env
    assert env["SOME_TOOL_CACHE"] == "/scratch/cache"


def test_declaring_nothing_is_the_factorys_list_exactly() -> None:
    plain = build_launch_env(parent=PARENT)
    declared_none = build_launch_env(parent=PARENT, declared=[])
    assert plain == declared_none
    assert set(plain) <= set(launch_setting_names())


def test_every_launch_says_a_factory_made_it() -> None:
    """The fence that stops a build system taking the name from its folder."""
    env = build_launch_env(parent=PARENT)
    assert env[GUARDKIT_FACTORY_LAUNCH_ENV] == "1"


# ---------------------------------------------------------------------------
# The parse is bounded, and every failure is an answer
# ---------------------------------------------------------------------------


#: The review's own input: about 1.2 KB, nested deeply enough to exhaust the
#: parser's own stack. It used to raise ``RecursionError`` — not a YAML error.
THE_NESTED_DECLARATION = "memory:\n  project: " + "[" * 600 + "]" * 600


def test_the_nested_declaration_is_an_answer_to_both_questions() -> None:
    assert len(THE_NESTED_DECLARATION.encode()) < 2048
    memory = read_declared_memory(
        repo=REPO, commit=COMMIT, content=THE_NESTED_DECLARATION, found=True
    )
    assert memory.outcome == "unreadable"
    assert "nests more than" in memory.refusal
    assert str(MAX_DECLARATION_DEPTH) in memory.refusal
    settings = _read(THE_NESTED_DECLARATION)
    assert not settings.ok and "nests more than" in settings.refusal


def test_an_ordinary_settings_file_is_not_thought_too_deep() -> None:
    ordinary = (
        "memory:\n  project: widget_shop\n"
        "launch:\n  settings: [A_CACHE]\n"
        "toolchain:\n  test:\n    command: whatever\n    when:\n      - always\n"
    )
    assert read_declared_memory(
        repo=REPO, commit=COMMIT, content=ordinary, found=True
    ).project == "widget_shop"
    assert _read(ordinary).names == ("A_CACHE",)


def test_a_file_that_is_not_yaml_at_all_is_still_an_answer() -> None:
    broken = "memory: [unclosed\n"
    assert (
        read_declared_memory(repo=REPO, commit=COMMIT, content=broken, found=True).outcome
        == "unreadable"
    )
    assert not _read(broken).ok
