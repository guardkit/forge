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
    GUARDKIT_FACTORY_LAUNCH_ENV,
    GUARDKIT_MEMORY_PROJECT_ENV,
    GUARDKIT_RUN_OWNER_ENV,
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
    "GUARDKIT_SDK_TIMEOUT": "1800",
    "GUARDKIT_STAMP_MODEL": "a-stamp-model",
    "GUARDKIT_STAMP_MODEL_URL": "http://localhost:4000/v1",
    "GUARDKIT_STAMP_MODEL_TIMEOUT_S": "30",
    "GUARDKIT_STAMP_MODEL_MAX_TOKENS": "4096",
    "GUARDKIT_MAX_PARALLEL_TASKS": "2",
    "GUARDKIT_WAVE_SAME_AREA": "parallel",
    "GUARDKIT_PLAYER_MODEL_LIMITS": "another-model=4",
    "GUARDKIT_ARCH_CONFORMANCE_BLOCKING": "1",
    "GUARDKIT_ZERO_TEST_BLOCKING": "1",
    "GUARDKIT_BOOT_SMOKE_BLOCKING": "1",
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


def test_a_project_tools_own_setting_is_not_on_the_central_list() -> None:
    """A package manager's cache is the project's to declare, not the factory's.

    22 September 2026: the central list once carried one Python package
    manager's cache, and a project built with any other tool got nothing.
    Central code names no project tool; what a project's builds need beyond
    the factory's own settings is declared by the project (next stage).
    """
    parent = dict(PARENT)
    parent.update({"UV_CACHE_DIR": "/scratch/uv", "npm_config_cache": "/scratch/npm",
                   "CARGO_HOME": "/scratch/cargo", "GRADLE_USER_HOME": "/scratch/gradle"})
    env = build_launch_env(parent=parent)
    for name in ("UV_CACHE_DIR", "npm_config_cache", "CARGO_HOME", "GRADLE_USER_HOME"):
        assert name not in env
    assert "UV_CACHE_DIR" not in launch_setting_names()


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
    list nobody can read is the copy-of-everything by another name.

    Raised from 30 to 34 on 5 October 2026 for the four settings of the plan's
    stamp check, which the list had left out (see the test below)."""
    assert len(LAUNCH_SETTINGS) <= 34


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
    env = build_launch_env(
        parent=PARENT, memory_project="widget_shop", run_owner="build-FEAT-W-1"
    )

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


def test_both_time_limits_the_machine_sets_reach_the_build() -> None:
    """The base time for one attempt, and the time for one task.

    3 October 2026: the machine's settings had set both since 18 September,
    but this list carried only the second, so every build ran each attempt on
    the build system's shorter default. Both names are written out here rather
    than imported from the build system, which this repository does not depend
    on.
    """
    env = build_launch_env(parent=PARENT, memory_project="widget_shop")

    assert env["GUARDKIT_SDK_TIMEOUT"] == "1800"
    assert env["GUARDKIT_AUTOBUILD_TASK_TIMEOUT_FLOOR"] == "900"


def test_a_setting_the_parent_does_not_have_stays_unset() -> None:
    """An unset setting is not an empty one: almost everything that reads these
    treats the two differently."""
    env = build_launch_env(parent={"PATH": "/usr/bin"}, memory_project="widget_shop")

    # The two the launch DECIDES rather than inherits are always there: which
    # memory this work belongs to, and the fact that a factory launched it.
    assert set(env) == {
        "PATH",
        GUARDKIT_MEMORY_PROJECT_ENV,
        GUARDKIT_FACTORY_LAUNCH_ENV,
    }
    assert "GUARDKIT_HARNESS" not in env


# ---------------------------------------------------------------------------
# A switch the owner turned on keeps working
# ---------------------------------------------------------------------------

#: The three switches the build system reads straight out of the environment at
#: build time. Each one ships OFF, and an operator turns it on by dropping a
#: file beside the runner's unit that sets it — the drop-in's own comment calls
#: itself "the whole switch". They are written out here, and not imported from
#: the build system, because this repository does not depend on it: if one is
#: ever renamed there, this test is where the rename surfaces.
#:
#: WHY THIS TEST EXISTS. The named list was written without them, and a named
#: list that leaves out a switch turns it off — silently. No error, no log line,
#: the drop-in still sitting there looking switched on, and architecture
#: findings quietly not reaching the code generator any more.
OPERATOR_SWITCHES = (
    "GUARDKIT_ARCH_CONFORMANCE_BLOCKING",
    "GUARDKIT_ZERO_TEST_BLOCKING",
    "GUARDKIT_BOOT_SMOKE_BLOCKING",
)


def test_a_switch_the_owner_turned_on_reaches_the_build() -> None:
    env = build_launch_env(parent=PARENT, memory_project="widget_shop")

    for switch in OPERATOR_SWITCHES:
        assert env.get(switch) == "1", switch


def test_every_operator_switch_is_accounted_for_one_way_or_the_other() -> None:
    """On the list with its reason, or on the left-out list with its reason.
    Never simply missing, which is how the first one was lost."""
    named = set(launch_setting_names()) | {
        name for name, _ in SETTINGS_DELIBERATELY_NOT_PASSED
    }
    for switch in OPERATOR_SWITCHES:
        assert switch in named, switch


def test_a_switch_nobody_turned_on_is_not_invented() -> None:
    """Today only the architecture one is set on this estate. The other two must
    stay unset rather than arrive as an empty string, which several readers of
    these treat as a value."""
    parent = {
        name: value
        for name, value in PARENT.items()
        if name not in ("GUARDKIT_ZERO_TEST_BLOCKING", "GUARDKIT_BOOT_SMOKE_BLOCKING")
    }

    env = build_launch_env(parent=parent, memory_project="widget_shop")

    assert env["GUARDKIT_ARCH_CONFORMANCE_BLOCKING"] == "1"
    assert "GUARDKIT_ZERO_TEST_BLOCKING" not in env
    assert "GUARDKIT_BOOT_SMOKE_BLOCKING" not in env


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


# ---------------------------------------------------------------------------
# Concurrent builds (3 October 2026)
# ---------------------------------------------------------------------------

#: The build system's own concurrency settings. Written out here rather than
#: imported from the build system, which this repository does not depend on.
CONCURRENCY_SETTINGS = {
    "GUARDKIT_MAX_PARALLEL_TASKS": "1",
    "GUARDKIT_WAVE_SAME_AREA": "serial",
    "GUARDKIT_PLAYER_MODEL_LIMITS": "a-model=2",
}


def test_the_build_systems_concurrency_settings_reach_the_build() -> None:
    """How many tasks at once, whether two tasks in one area wait for each
    other, and each model's own limit. Set on the machine, they reach the
    build; unset, the build system keeps its own defaults."""
    env = build_launch_env(parent={**PARENT, **CONCURRENCY_SETTINGS})
    for name, value in CONCURRENCY_SETTINGS.items():
        assert env[name] == value

    unset = build_launch_env(
        parent={k: v for k, v in PARENT.items() if k not in CONCURRENCY_SETTINGS}
    )
    for name in CONCURRENCY_SETTINGS:
        assert name not in unset


def test_the_run_owner_is_the_build_and_never_inherited() -> None:
    """Which build this child belongs to, so its test fixtures carry the
    build's own name and a stop can find every process it started. Decided
    from the record like the memory name; one the launching process happens
    to hold never decides it."""
    parent = {**PARENT, GUARDKIT_RUN_OWNER_ENV: "somebody-elses-build"}

    handed = build_launch_env(parent=parent, run_owner="build-FEAT-A-1")
    none_given = build_launch_env(parent=parent)

    assert handed[GUARDKIT_RUN_OWNER_ENV] == "build-FEAT-A-1"
    assert GUARDKIT_RUN_OWNER_ENV not in none_given


# ---------------------------------------------------------------------------
# The model the plan's stamp check asks (5 October 2026)
# ---------------------------------------------------------------------------

#: The stamp check's own settings, as the build system reads them
#: (``guardkit/orchestrator/stamp_model_fallback.py``). Written out here
#: rather than imported from the build system, which this repository does not
#: depend on.
STAMP_SETTINGS = {
    "GUARDKIT_STAMP_MODEL": "flash-next-t06",
    "GUARDKIT_STAMP_MODEL_URL": "http://router:4000/v1",
    "GUARDKIT_STAMP_MODEL_TIMEOUT_S": "60",
    "GUARDKIT_STAMP_MODEL_MAX_TOKENS": "8192",
}

#: The parent above with none of the stamp check's settings in it.
PARENT_WITHOUT_STAMP = {k: v for k, v in PARENT.items() if k not in STAMP_SETTINGS}


def test_the_stamp_checks_model_settings_reach_the_check() -> None:
    """5 October 2026: the release set GUARDKIT_STAMP_MODEL in the sandbox's
    settings and the sandbox helper had it, but this list left it out, so the
    stamp check asked the build system's built-in default instead — a retired
    model whose load ran the GPU out of memory. Set, each one reaches the
    launch with the parent's own value; unset, it stays unset and the build
    system keeps its own default."""
    env = build_launch_env(parent={**PARENT_WITHOUT_STAMP, **STAMP_SETTINGS})
    for name, value in STAMP_SETTINGS.items():
        assert env[name] == value, f"{name} was not handed to the launch"

    unset = build_launch_env(parent=PARENT_WITHOUT_STAMP)
    for name in STAMP_SETTINGS:
        assert name not in unset


def test_each_stamp_setting_travels_on_its_own() -> None:
    """Any one of them set alone is passed, so setting only the model name —
    which is what the release does — is enough."""
    for name, value in STAMP_SETTINGS.items():
        env = build_launch_env(parent={**PARENT_WITHOUT_STAMP, name: value})
        assert env[name] == value
        for other in STAMP_SETTINGS:
            if other != name:
                assert other not in env
