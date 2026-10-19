"""A Git door per sandbox (design "One sandbox per project", 17 October 2026).

``planning.sandboxes.<repo>.git_door`` is that sandbox's own port on the Git
door. A repository's fetch address through a door is taken only when it is
the door of the sandbox that repository is mapped to (or the single
``planning.git_door`` of an estate with one sandbox), followed by that same
repository's ``<owner>/<name>.git``. Every address here is made up.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from forge.config.models import PlanningConfig, SandboxEntry
from forge.config.sandboxes import git_door_for

DOOR_A = "http://192.0.2.1:31831/"
DOOR_B = "http://192.0.2.1:31832/"
A, B, SHARED = "example/project-a", "example/project-b", "example/shared"


def _entry(name: str, slot: int, door: str | None) -> dict:
    return {
        "name": name,
        "sidecar_url": f"http://192.0.2.10:{8925 + 10 * slot}",
        "runner_url": f"http://192.0.2.10:{8924 + 10 * slot}",
        **({"git_door": door} if door else {}),
    }


def _planning(fetch: dict[str, str], **extra) -> PlanningConfig:
    return PlanningConfig(
        target_repo_paths={A: "/w/a", B: "/w/b", SHARED: "/w/s"},
        sandboxes={
            A: _entry("sbx-a", 1, DOOR_A),
            B: _entry("sbx-b", 2, DOOR_B),
            SHARED: _entry("api-test-deploy", 0, None),
        },
        fetch_addresses=fetch,
        **extra,
    )


@pytest.mark.parametrize("door", [
    "http://0.0.0.0:31831/", "https://192.0.2.1:31831/", "http://192.0.2.1/",
    "http://192.0.2.1:31831/x/", "http://t@192.0.2.1:31831/", " http://192.0.2.1:31831/",
])
def test_a_sandbox_s_door_is_one_plain_http_base(door):
    with pytest.raises(ValueError, match="git_door must be exactly"):
        SandboxEntry(**_entry("sbx-a", 1, door))
    assert SandboxEntry(**_entry("sbx-a", 1, DOOR_A)).git_door == DOOR_A
    assert SandboxEntry(**_entry("sbx-a", 1, None)).git_door is None


def test_each_repository_is_read_through_its_own_sandbox_s_door():
    taken = _planning({A: f"{DOOR_A}{A}.git", B: f"{DOOR_B}{B}.git",
                       SHARED: f"https://github.com/{SHARED}.git"})
    assert taken.git_door_for(A) == DOOR_A and taken.git_door_for(B) == DOOR_B
    assert taken.git_door_for(SHARED) is None


def test_a_repository_through_another_sandbox_s_door_is_refused():
    with pytest.raises(ValueError, match=f"planning.fetch_addresses for {A}"):
        _planning({A: f"{DOOR_B}{A}.git"})


def test_the_shared_sandbox_without_a_door_takes_no_door_address():
    with pytest.raises(ValueError, match=f"planning.fetch_addresses for {SHARED}"):
        _planning({SHARED: f"{DOOR_A}{SHARED}.git"})


def test_a_door_address_must_name_its_own_repository():
    with pytest.raises(ValueError, match="must name the repository"):
        _planning({A: f"{DOOR_A}{B}.git"})
    assert _planning({A: f"{DOOR_A}Example/Project-A.git"})


def test_two_sandboxes_never_share_a_door():
    with pytest.raises(ValueError, match="same git_door"):
        PlanningConfig(
            target_repo_paths={A: "/w/a", B: "/w/b"},
            sandboxes={A: _entry("sbx-a", 1, DOOR_A), B: _entry("sbx-b", 2, DOOR_A)},
        )


def test_the_single_door_is_for_an_estate_with_one_sandbox():
    with pytest.raises(ValueError, match="leave it unset"):
        _planning({}, git_door=DOOR_A)
    one = PlanningConfig(
        git_door=DOOR_A,
        target_repo_paths={A: "/w/a"},
        sandboxes={A: _entry("only-sbx", 1, None)},
        fetch_addresses={A: f"{DOOR_A}{A}.git"},
    )
    assert one.git_door_for(A) == DOOR_A


def test_the_shared_sandbox_never_reads_through_the_single_door():
    """Codex implementation review 1, R1: a repository mapped to the shared
    sandbox is refused a door address, through the single door too."""
    with pytest.raises(ValueError, match="never reads through a door"):
        PlanningConfig(
            git_door=DOOR_A,
            target_repo_paths={SHARED: "/w/s"},
            sandboxes={SHARED: _entry("api-test-deploy", 0, None)},
            fetch_addresses={SHARED: f"{DOOR_A}{SHARED}.git"},
        )
    # Over https it is as before.
    assert PlanningConfig(
        git_door=DOOR_A,
        target_repo_paths={SHARED: "/w/s"},
        sandboxes={SHARED: _entry("api-test-deploy", 0, None)},
        fetch_addresses={SHARED: f"https://github.com/{SHARED}.git"},
    ).git_door_for(SHARED) == DOOR_A


def test_the_shared_sandbox_never_has_a_door_of_its_own():
    with pytest.raises(ValueError, match="never has one"):
        PlanningConfig(
            target_repo_paths={SHARED: "/w/s"},
            sandboxes={SHARED: _entry("api-test-deploy", 0, DOOR_A)},
        )


def test_the_shared_question_answers_the_same_for_any_configuration_shape():
    config = SimpleNamespace(planning=_planning({}))
    assert git_door_for(config, A) == DOOR_A
    assert git_door_for(config, SHARED) is None
    assert git_door_for(config, "example/unknown") is None
    assert git_door_for(SimpleNamespace(), A) is None
    single = SimpleNamespace(planning=SimpleNamespace(git_door=DOOR_A, sandboxes={}))
    assert git_door_for(single, "example/anything") == DOOR_A


def test_the_sweep_sends_each_private_project_to_its_own_sandbox_never_the_door():
    """19 October 2026: the coordinator cannot reach a door, so a project read
    through one is fetched through its own sandbox (its helper, its door named
    to that helper, its git_export); a public project is fetched as before."""
    from forge.security_sweep.schedule import _fetch_for_each_project

    public: list[str] = []
    private: list[tuple] = []
    config = SimpleNamespace(planning=_planning(
        {A: f"{DOOR_A}{A}.git", B: f"{DOOR_B}{B}.git", SHARED: f"https://github.com/{SHARED}.git"}
    ))
    config.planning.sandboxes[A] = SandboxEntry(
        **_entry("sbx-a", 1, DOOR_A), git_export="git://192.0.2.10:8928/project-a"
    )
    fetch = _fetch_for_each_project(
        config,
        sandbox_post="the-post",
        fetch_public=lambda address, folder: public.append(address),
        fetch_private=lambda repo, address, folder, **kw: private.append(
            (repo, address, kw["sidecar_url"], kw["git_door"], kw["git_export"], kw["post"])
        ),
    )
    fetch(f"{DOOR_A}{A}.git", "/tmp/x")
    fetch(f"{DOOR_B}{B}.git", "/tmp/x")
    fetch(f"https://github.com/{SHARED}.git", "/tmp/x")
    assert public == [f"https://github.com/{SHARED}.git"]
    assert private == [
        (A, f"{DOOR_A}{A}.git", "http://192.0.2.10:8935", DOOR_A, "git://192.0.2.10:8928/project-a", "the-post"),
        (B, f"{DOOR_B}{B}.git", "http://192.0.2.10:8945", DOOR_B, None, "the-post"),
    ]


def test_the_planning_git_of_each_sandboxed_repository_gets_its_own_door():
    from forge.cli._serve_planning import compose_planning_git_runner

    _, runner_for = compose_planning_git_runner(
        _planning({A: f"{DOOR_A}{A}.git"}), worktree_runner_factory=lambda: object()
    )
    assert runner_for(A)._git_door == DOOR_A
    assert runner_for(B)._git_door == DOOR_B
    assert runner_for(SHARED)._git_door is None


def test_the_merge_word_s_git_and_the_merge_check_reader_get_each_sandbox_s_door():
    from forge.cli.serve import compose_merge_check_reader, compose_merge_git_surface

    config = SimpleNamespace(planning=_planning({A: f"{DOOR_A}{A}.git"}))
    surface_for = compose_merge_git_surface(config)
    reader_for = compose_merge_check_reader(config)
    for repo, door in ((A, DOOR_A), (B, DOOR_B), (SHARED, None)):
        assert surface_for(repo, "/w")._git_door == door
        assert reader_for(repo, "/w")._git_door == door


def test_a_repair_fetches_through_its_own_sandbox_s_door(tmp_path, monkeypatch):
    """The repair's base is fetched with the door of the repository's own
    sandbox, not the single planning.git_door (coach review, 17 October)."""
    import asyncio

    from forge.adapters.sqlite import connect as sqlite_connect
    from forge.config.models import ForgeConfig
    from forge.lifecycle import migrations as lifecycle_migrations
    from forge.lifecycle.persistence import SqliteLifecyclePersistence
    from forge.pipeline import fix_admission
    from forge.pipeline.fix_row_producer import fix_correlation_id
    from forge.planning.work_queue_store import WorkQueueStore
    from tests.forge.pipeline import test_fix_admission as fa

    fa.isolate_git(monkeypatch)
    repo_root = fa.make_feature_repo(tmp_path / "api_test")
    config = ForgeConfig.model_validate({
        "permissions": {"filesystem": {"allowlist": [str(repo_root.parent)]}},
        "queue": {"repo_allowlist": [str(repo_root)]},
        "planning": {
            "target_repo_paths": {fa.REPO_KEY: str(repo_root)},
            "sandboxes": {fa.REPO_KEY: {**_entry("own-sbx", 1, DOOR_A)}},
            "fetch_addresses": {fa.REPO_KEY: f"{DOOR_A}{fa.REPO_KEY}.git"},
        },
        "conductor": {"enabled": True, "seat": "qwen3-coder-30b"},
        "budget": {"default_profile": "attended", "profiles": {
            "attended": {}, fa.FIX_JOURNEY_PROFILE_NAME: {"max_review_cycles": 2}}},
    })
    asked: dict = {}

    class Stop(Exception):
        pass

    async def resolve_repair_base(**kwargs):
        asked.update(kwargs)
        raise Stop

    monkeypatch.setattr(fix_admission, "resolve_repair_base", resolve_repair_base)
    cx = sqlite_connect.connect_writer(tmp_path / "forge.db")
    lifecycle_migrations.apply_at_boot(cx)
    try:
        pool = SqliteLifecyclePersistence(connection=cx, db_path=tmp_path / "forge.db")
        store = WorkQueueStore(pool.connection)
        fa.seed_failed_build(pool)
        with pytest.raises(Stop):
            asyncio.run(fix_admission.admit_fix_row(
                config=config, persistence=pool, store=store,
                queue_id=fa._file_build_failed_row(store),
                correlation_id=fix_correlation_id(fa.SOURCE_BUILD),
                sentence="The feature build failed after coding began",
                target_repo=fa.REPO_KEY, publish=fa.Publisher(),
                profile=fa.FIX_JOURNEY_PROFILE_NAME, receipts_root=tmp_path / "receipts",
                sidecar_post=fa._RetainedCandidateSidecar("a" * 40),
            ))
    finally:
        cx.close()
    assert asked["git_door"] == DOOR_A
    assert asked["fetch_address"] == f"{DOOR_A}{fa.REPO_KEY}.git"
