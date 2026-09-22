"""What a build is launched with: the short named list, and nothing else.

The design pass of 21 September 2026 (item 1, second revision, section D):
*"Builds and checks are launched with a short list of named settings, not a copy
of everything."* Until then a build was launched with ``env=os.environ.copy()``
— whatever the launching process happened to be holding, an operator's shell and
all.

These tests hold two things down. The list is the list, in one place. And
nothing that is not on it gets through, **including a credential planted in the
parent environment** — which is the whole reason the list exists.

Nothing here starts a process, reads anyone's real environment, or touches any
service. The "parent environment" in every test is a dictionary made here.
"""

from __future__ import annotations

from forge.launch_environment import (
    GUARDKIT_MEMORY_PROJECT_ENV,
    LAUNCH_SETTINGS,
    SETTINGS_DELIBERATELY_NOT_PASSED,
    build_launch_env,
    launch_setting_names,
)

#: A parent environment shaped like the runner's, plus everything a real
#: process picks up that a build has no business seeing.
PARENT = {
    # on the list
    "PATH": "/opt/venv/bin:/usr/bin",
    "HOME": "/home/agent",
    "TMPDIR": "/scratch",
    "UV_CACHE_DIR": "/scratch/uv",
    "FORGE_GUARDKIT_PATH": "/opt/venv/bin/guardkit",
    "GUARDKIT_HARNESS": "langgraph",
    "FORGE_CONFIG_PATH": "/state/forge.yaml",
    "FORGE_RECEIPTS_DIR": "/receipts",
    "FORGE_NATS_URL": "nats://bus:4222",
    "OPENAI_BASE_URL": "http://localhost:9000/v1",
    "OPENAI_API_KEY": "placeholder",
    "ANTHROPIC_BASE_URL": "http://localhost:9000",
    "GUARDKIT_TIMEOUT_MULTIPLIER": "4.0",
    "GUARDKIT_AUTOBUILD_TASK_TIMEOUT_FLOOR": "900",
    "FLEET_MEMORY_ENABLED": "true",
    "FLEET_MEMORY_PG_DSN": "postgresql://somewhere/memory",
    "FLEET_MEMORY_EMBED_URL": "http://embed:9000",
    "FLEET_MEMORY_EMBED_MODEL": "embed",
    "FLEET_MEMORY_EMBED_DIMS": "1024",
    "FLEET_MEMORY_NATS_URL": "nats://bus:4222",
    "GUARDKIT_NATS_PASSWORD": "opaque",
    # NOT on the list, and every one of these was reaching builds before
    "FORGE_DB_PATH": "/state/.forge/forge.db",
    "FORGE_SIDECAR_IN_SANDBOX": "1",
    "FORGE_BUILD_MONITOR": "1",
    "SSH_AUTH_SOCK": "/run/user/1000/keyring/ssh",
    "GH_TOKEN": "the-publishing-credential-nobody-should-see",
    "AWS_SECRET_ACCESS_KEY": "another-one",
    "SUDO_ASKPASS": "/usr/bin/askpass",
    "LS_COLORS": "an operator's shell, in full",
}


# ---------------------------------------------------------------------------
# The list is a list, written down once
# ---------------------------------------------------------------------------


def test_every_setting_carries_a_reason() -> None:
    """A setting that is not worth a sentence is not worth handing to a build."""
    for name, reason in LAUNCH_SETTINGS:
        assert name and name.strip() == name
        assert reason and len(reason) > 20, name


def test_the_list_names_nothing_twice() -> None:
    names = launch_setting_names()
    assert len(names) == len(set(names))


def test_the_list_is_short() -> None:
    """"A SHORT named list" is the design's own wording, and it stays short: a
    list nobody can read is the copy-of-everything by another name."""
    assert len(LAUNCH_SETTINGS) <= 30


def test_the_list_names_no_language_framework_or_product() -> None:
    """The factory is agnostic: nothing on this list belongs to one kind of
    project."""
    forbidden = (
        "PYTHON", "PYTEST", "PIP", "POETRY", "NODE", "NPM", "YARN", "PNPM",
        "GRADLE", "MAVEN", "JAVA", "DOTNET", "GO", "CARGO", "RUBY", "RAILS",
        "DJANGO", "FLASK", "POSTGRES", "MYSQL", "REDIS", "MONGO", "HTTP_PORT",
        "DOCKER", "KUBE", "AWS", "GCP", "AZURE", "API_TEST", "STUDY_TUTOR",
    )
    for name, _reason in LAUNCH_SETTINGS:
        upper = name.upper()
        for word in forbidden:
            assert word not in upper, f"{name} names {word}"


def test_what_is_left_out_is_named_too() -> None:
    """So nobody has to wonder whether they were forgotten."""
    left_out = {name for name, _ in SETTINGS_DELIBERATELY_NOT_PASSED}
    assert "FORGE_DB_PATH" in left_out
    assert left_out.isdisjoint(set(launch_setting_names()))


# ---------------------------------------------------------------------------
# Nothing that is not on the list gets through
# ---------------------------------------------------------------------------


def test_the_launch_environment_is_exactly_the_list() -> None:
    env = build_launch_env(parent=PARENT, memory_project="widget_shop")

    expected = set(launch_setting_names())
    assert set(env) == expected


def test_a_credential_planted_in_the_parent_environment_does_not_leak() -> None:
    """The reason the list exists, said as a test."""
    env = build_launch_env(parent=PARENT, memory_project="widget_shop")

    assert "GH_TOKEN" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "SSH_AUTH_SOCK" not in env
    # and not hiding inside a value, either
    assert all(
        "the-publishing-credential-nobody-should-see" not in value
        for value in env.values()
    )


def test_the_coordinators_ledger_is_not_handed_to_a_build() -> None:
    """Section D of the design turns on a build being unable to write the
    ledger. The sandbox's own start script already unsets this; so does this."""
    env = build_launch_env(parent=PARENT, memory_project="widget_shop")

    assert "FORGE_DB_PATH" not in env


def test_the_values_that_do_come_through_are_the_parents_own() -> None:
    env = build_launch_env(parent=PARENT, memory_project="widget_shop")

    assert env["PATH"] == "/opt/venv/bin:/usr/bin"
    assert env["FORGE_GUARDKIT_PATH"] == "/opt/venv/bin/guardkit"
    assert env["GUARDKIT_HARNESS"] == "langgraph"
    assert env["FLEET_MEMORY_PG_DSN"] == "postgresql://somewhere/memory"


def test_a_setting_the_parent_does_not_have_stays_unset() -> None:
    """An unset setting is not an empty one: almost everything that reads these
    treats the two differently."""
    env = build_launch_env(parent={"PATH": "/usr/bin"}, memory_project="widget_shop")

    assert set(env) == {"PATH", GUARDKIT_MEMORY_PROJECT_ENV}
    assert "GUARDKIT_HARNESS" not in env


# ---------------------------------------------------------------------------
# The memory name is decided per build, never inherited
# ---------------------------------------------------------------------------


def test_the_memory_name_is_set_from_the_record() -> None:
    env = build_launch_env(parent=PARENT, memory_project="widget_shop")

    assert env[GUARDKIT_MEMORY_PROJECT_ENV] == "widget_shop"


def test_nothing_recorded_means_the_name_is_not_set_at_all() -> None:
    """Never a guess, and never "guardkit": with no name the build system reads
    the project's own declaration in the folder it is building, and runs with
    memory off if there is none."""
    env = build_launch_env(parent=PARENT, memory_project=None)

    assert GUARDKIT_MEMORY_PROJECT_ENV not in env
    env_blank = build_launch_env(parent=PARENT, memory_project="   ")
    assert GUARDKIT_MEMORY_PROJECT_ENV not in env_blank


def test_a_name_in_the_parent_environment_never_decides_it() -> None:
    """The name comes from what Forge read at the starting commit, not from
    whatever the launching process was carrying."""
    parent = {**PARENT, GUARDKIT_MEMORY_PROJECT_ENV: "somebody_elses_memory"}

    handed = build_launch_env(parent=parent, memory_project="widget_shop")
    none_recorded = build_launch_env(parent=parent, memory_project=None)

    assert handed[GUARDKIT_MEMORY_PROJECT_ENV] == "widget_shop"
    assert GUARDKIT_MEMORY_PROJECT_ENV not in none_recorded


def test_the_name_is_trimmed_but_never_rewritten() -> None:
    env = build_launch_env(parent=PARENT, memory_project="  widget_shop  ")

    assert env[GUARDKIT_MEMORY_PROJECT_ENV] == "widget_shop"


def test_two_launches_of_the_same_build_are_identical() -> None:
    first = build_launch_env(parent=PARENT, memory_project="widget_shop")
    second = build_launch_env(parent=PARENT, memory_project="widget_shop")

    assert first == second
    assert list(first) == list(second)


def test_the_answer_is_a_new_dictionary_every_time() -> None:
    first = build_launch_env(parent=PARENT, memory_project="widget_shop")
    first["PATH"] = "tampered"
    second = build_launch_env(parent=PARENT, memory_project="widget_shop")

    assert second["PATH"] == "/opt/venv/bin:/usr/bin"
