"""Receipts per project (design "One sandbox per project", section 2.2a).

Each project's own sandbox keeps its receipts in a folder of its own, which
the coordinator sees at ``planning.sandboxes.<repo>.receipts_root``. Every
coordinator reader and writer of receipts resolves a build's folder from the
build's repository through ONE function,
:func:`forge.receipts.receipts_root_for`; a project without a folder of its own
reads and writes exactly where it always did. Every path here is a temporary
one and every name is made up.
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from forge import receipts
from forge.config.models import PlanningConfig, SandboxEntry
from forge.config.sandboxes import use_the_settings_receipts
from forge.receipts import receipts_root, receipts_root_for, use_project_receipts

SRC = Path(__file__).resolve().parents[2] / "src" / "forge"
OWN_REPO, LEGACY_REPO = "example/own-project", "example/legacy-project"
BUILD = "build-FEAT-1234-20261017120000"

#: The only places that may ask for the single receipts root by itself: the
#: resolver's own module, and the build side's door onto it, which runs
#: inside the project's own sandbox, where FORGE_RECEIPTS_DIR names that
#: sandbox's own folder (it is not a coordinator reader or writer).
ALLOWED = {
    ("receipts.py", None),
    ("subagents/autobuild_runner.py", "_receipts_root"),
}

#: ``_receipts_root()`` (the build side's door onto the single root): the
#: build side itself, and the fix journey's resolver, which asks it only
#: after receipts_root_for found no folder of the build's project's own.
ALLOWED_BUILD_SIDE = {
    ("subagents/autobuild_runner.py", "*"),
    ("pipeline/fix_journey_receipts.py", "fix_journey_receipts_root"),
}


@pytest.fixture(autouse=True)
def _two_roots(tmp_path, monkeypatch):
    """The single root (FORGE_RECEIPTS_DIR) and nothing registered; whatever
    a test registers is taken away afterwards."""
    monkeypatch.setenv(receipts.RECEIPTS_DIR_ENV, str(tmp_path / "global"))
    use_project_receipts({})
    yield
    use_project_receipts({})


def _entry(name: str, slot: int, receipts_root: str | None) -> dict:
    return {
        "name": name,
        "sidecar_url": f"http://192.0.2.10:{8925 + 10 * slot}",
        "runner_url": f"http://192.0.2.10:{8924 + 10 * slot}",
        **({"receipts_root": receipts_root} if receipts_root else {}),
    }


# ---------------------------------------------------------------------------
# No coordinator code asks for the single root except through the resolver


def _calls_of_the_single_root(path: Path) -> list[tuple[int, str | None]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[int, str | None]] = []

    def visit(node: ast.AST, function: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            inner = function
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                inner = child.name
            if isinstance(child, ast.Call):
                func = child.func
                name = func.id if isinstance(func, ast.Name) else (
                    func.attr if isinstance(func, ast.Attribute) else None
                )
                if name == "receipts_root":
                    found.append((child.lineno, function))
            visit(child, inner)

    visit(tree, None)
    return found


def test_every_receipts_root_call_outside_the_resolver_is_refused():
    bypasses = []
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC).as_posix()
        for line, function in _calls_of_the_single_root(path):
            if (relative, None) in ALLOWED or (relative, function) in ALLOWED:
                continue
            bypasses.append(f"{relative}:{line} (in {function})")
    assert bypasses == [], (
        "these ask for the single receipts root directly; ask "
        "forge.receipts.receipts_root_for(<the build's repository>) instead: "
        + ", ".join(bypasses)
    )


def _other_bypasses(path: Path, relative: str) -> list[str]:
    """``_receipts_root()`` outside its allowed places, a
    ``fix_journey_receipts_root(...)`` that is given neither a folder nor the
    build's repository, and either name imported under another name."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []

    def visit(node: ast.AST, function: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            inner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else function
            if isinstance(child, ast.Call):
                func = child.func
                name = func.id if isinstance(func, ast.Name) else (
                    func.attr if isinstance(func, ast.Attribute) else None
                )
                if name == "_receipts_root" and not (
                    (relative, "*") in ALLOWED_BUILD_SIDE or (relative, function) in ALLOWED_BUILD_SIDE
                ):
                    found.append(f"{relative}:{child.lineno} _receipts_root() in {function}")
                if name == "fix_journey_receipts_root":
                    keywords = {k.arg for k in child.keywords}
                    if "repo" not in keywords and not child.args and "receipts_root" not in keywords:
                        found.append(f"{relative}:{child.lineno} fix_journey_receipts_root() with no folder")
                    elif "repo" not in keywords:
                        found.append(f"{relative}:{child.lineno} fix_journey_receipts_root() without repo=")
            if isinstance(child, ast.ImportFrom):
                for alias in child.names:
                    if alias.name in ("receipts_root", "_receipts_root", "receipts_root_for") and (
                        alias.asname not in (None, alias.name)
                    ):
                        found.append(f"{relative}:{child.lineno} imports {alias.name} as {alias.asname}")
            visit(child, inner)

    visit(tree, None)
    return found


def test_no_coordinator_code_reaches_the_single_folder_another_way():
    found: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        found += _other_bypasses(path, path.relative_to(SRC).as_posix())
    assert found == [], "these reach the single receipts folder without the build's repository: " + ", ".join(found)


def test_the_wider_check_would_see_each_way_round(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text(
        "from forge.receipts import receipts_root as where\n"
        "from forge.subagents.autobuild_runner import _receipts_root\n"
        "def reader(build_id, root):\n"
        "    a = _receipts_root() / build_id\n"
        "    b = fix_journey_receipts_root()\n"
        "    c = fix_journey_receipts_root(root)\n"
        "    d = fix_journey_receipts_root(root, repo='x/y')\n"
        "    return a, b, c, d\n"
    )
    found = _other_bypasses(sample, "pipeline/sample.py")
    assert [line.split(" ", 1)[1] for line in found] == [
        "imports receipts_root as where",
        "_receipts_root() in reader",
        "fix_journey_receipts_root() with no folder",
        "fix_journey_receipts_root() without repo=",
    ]


def test_the_check_would_see_a_new_bypass(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text(
        "from forge.receipts import receipts_root\n"
        "import forge.receipts as r\n"
        "def reader(build_id):\n"
        "    return receipts_root() / build_id, r.receipts_root()\n"
        "def fine():\n"
        "    '''receipts_root() in a docstring is no call'''\n"
        "    return receipts_root_for('x'), _receipts_root()\n"
    )
    assert _calls_of_the_single_root(sample) == [(4, "reader"), (4, "reader")]


# ---------------------------------------------------------------------------
# The setting and the resolver


@pytest.mark.parametrize("folder", ["relative/receipts", "/var/../etc", "/a b", "/", " /x"])
def test_a_receipts_folder_is_one_absolute_path(folder):
    with pytest.raises(ValueError, match="receipts_root must be one absolute folder path"):
        SandboxEntry(**_entry("sbx-a", 1, folder))
    assert SandboxEntry(**_entry("sbx-a", 1, "/r/a/")).receipts_root == "/r/a"
    assert SandboxEntry(**_entry("sbx-a", 1, None)).receipts_root is None


def test_two_sandboxes_never_share_a_receipts_folder():
    with pytest.raises(ValueError, match="same receipts_root"):
        PlanningConfig(
            target_repo_paths={OWN_REPO: "/w/a", LEGACY_REPO: "/w/b"},
            sandboxes={OWN_REPO: _entry("sbx-a", 1, "/r/x"), LEGACY_REPO: _entry("sbx-b", 2, "/r/x")},
        )


def test_the_resolver_answers_the_project_s_own_folder_and_otherwise_the_single_root(tmp_path):
    assert receipts_root_for(OWN_REPO) == receipts_root() == tmp_path / "global"
    assert receipts_root_for(None) == receipts_root()
    config = SimpleNamespace(planning=PlanningConfig(
        target_repo_paths={OWN_REPO: "/w/a", LEGACY_REPO: "/w/b"},
        sandboxes={
            OWN_REPO: _entry("own-project", 1, str(tmp_path / "own")),
            LEGACY_REPO: _entry("api-test-deploy", 0, None),
        },
    ))
    use_the_settings_receipts(config)
    assert receipts_root_for(OWN_REPO) == tmp_path / "own"
    assert receipts_root_for(LEGACY_REPO) == tmp_path / "global"
    assert receipts_root_for("example/unknown") == tmp_path / "global"
    assert receipts_root_for(None) == tmp_path / "global"
    use_the_settings_receipts(SimpleNamespace())
    assert receipts_root_for(OWN_REPO) == tmp_path / "global"


def test_loading_settings_on_the_command_line_puts_the_folders_in_use(tmp_path):
    from forge.cli.main import _resolve_context_object

    settings = tmp_path / "forge.yaml"
    settings.write_text(
        "permissions:\n"
        "  filesystem:\n"
        "    allowlist: [/w]\n"
        "planning:\n"
        "  target_repo_paths:\n"
        f"    {OWN_REPO}: /w/a\n"
        "  sandboxes:\n"
        f"    {OWN_REPO}:\n"
        "      name: own-project\n"
        "      sidecar_url: http://192.0.2.10:8935\n"
        "      runner_url: http://192.0.2.10:8934\n"
        f"      receipts_root: {tmp_path / 'own'}\n"
    )
    _resolve_context_object(settings)
    assert receipts_root_for(OWN_REPO) == tmp_path / "own"


# ---------------------------------------------------------------------------
# Readers and writers: a project of its own uses its own folder; a project
# without one reads and writes exactly where it did


def _the_two_folders(tmp_path) -> tuple[Path, Path]:
    own, single = tmp_path / "own", tmp_path / "global"
    use_project_receipts({OWN_REPO: own})
    return own, single


def test_the_merge_card_reads_the_baseline_and_records_from_the_build_s_project(tmp_path):
    from forge.pipeline.merge_offer import read_baseline_failing, read_what_was_checked

    own, single = _the_two_folders(tmp_path)
    for root, failing in ((own, ["t_own"]), (single, ["t_single"])):
        (root / BUILD / "x").mkdir(parents=True)
        (root / BUILD / "x" / "baseline.json").write_text(json.dumps(failing))
    assert read_baseline_failing(BUILD, repo=OWN_REPO) == ["t_own"]
    assert read_baseline_failing(BUILD, repo=LEGACY_REPO) == ["t_single"]
    assert read_baseline_failing(BUILD) == ["t_single"], "no repository: the single root, as before"

    _, _, why_not, _ = read_what_was_checked(BUILD, "FEAT-1234", repo=OWN_REPO)
    assert why_not and "no " in why_not, "looked in the project's own build folder"
    (own / "other-build").mkdir()
    _, _, why_not, _ = read_what_was_checked("other-build", "FEAT-1234", repo=LEGACY_REPO)
    assert why_not == "nothing was exported for this build", "not looked for in the own folder"


def test_the_scope_receipt_is_written_beside_the_build_s_own_evidence(tmp_path):
    from forge.pipeline.scope_report import unread_scope, write_scope_report

    own, single = _the_two_folders(tmp_path)
    (own / BUILD).mkdir(parents=True)
    (single / BUILD).mkdir(parents=True)
    written = write_scope_report(BUILD, unread_scope("made up"), repo=OWN_REPO)
    assert written is not None and written.parent == own / BUILD
    assert list((single / BUILD).iterdir()) == []
    legacy = write_scope_report(BUILD, unread_scope("made up"), repo=LEGACY_REPO)
    assert legacy is not None and legacy.parent == single / BUILD


def test_forge_status_reads_a_running_build_s_heartbeat_from_its_project(tmp_path):
    from forge.cli.status import _read_in_flight

    own, single = _the_two_folders(tmp_path)
    for root, stage in ((own, "own"), (single, "single")):
        (root / BUILD).mkdir(parents=True)
        (root / BUILD / receipts.IN_FLIGHT_STATE_NAME).write_text(json.dumps({"stage": stage}))
    assert _read_in_flight(BUILD, OWN_REPO) == {"stage": "own"}
    assert _read_in_flight(BUILD, LEGACY_REPO) == {"stage": "single"}
    assert _read_in_flight(BUILD) == {"stage": "single"}


def test_the_status_view_carries_the_repository_but_its_json_is_unchanged():
    from datetime import datetime, timezone

    from forge.lifecycle.persistence import BuildStatusView

    view = BuildStatusView(build_id=BUILD, feature_id="FEAT-1234", status="RUNNING",
                           queued_at=datetime(2026, 10, 17, tzinfo=timezone.utc), repo=OWN_REPO)
    assert view.repo == OWN_REPO
    assert "repo" not in json.loads(view.model_dump_json())


def test_the_merge_executor_writes_where_the_build_s_project_keeps_its_receipts(tmp_path):
    from forge.pipeline.merge_executor import _default_receipts_root, _receipts_root_of

    own, single = _the_two_folders(tmp_path)
    deps = SimpleNamespace(receipts_root_fn=_default_receipts_root)
    assert _receipts_root_of(deps, OWN_REPO) == own
    assert _receipts_root_of(deps, LEGACY_REPO) == single
    assert _default_receipts_root() == single
    seam = SimpleNamespace(receipts_root_fn=lambda: tmp_path / "seam")
    assert _receipts_root_of(seam, OWN_REPO) == tmp_path / "seam", "a test's seam still wins"


# ---------------------------------------------------------------------------
# End to end: the merge report the executor writes for a project of its own
# is what that project's repair task reads, and nothing is in the single root


@pytest.mark.parametrize("its_own", [True, False], ids=["its-own-folder", "the-single-root"])
def test_a_repair_reads_the_merge_report_from_the_project_s_own_folder(tmp_path, monkeypatch, its_own):
    from forge.pipeline.fix_admission import MERGE_REPORT_NAME, admit_fix_row
    from forge.pipeline.fix_row_producer import fix_correlation_id
    from forge.pipeline.merge_executor import _default_receipts_root, _receipts_root_of
    from tests.forge.pipeline import test_fix_admission as fa

    fa.isolate_git(monkeypatch)

    repo_root = fa.make_feature_repo(tmp_path / "api_test")
    own, single = tmp_path / "own", tmp_path / "global"
    if its_own:
        use_project_receipts({fa.REPO_KEY: own})
    # Where the press writes the report for this build of this repository.
    where = _receipts_root_of(SimpleNamespace(receipts_root_fn=_default_receipts_root), fa.REPO_KEY)
    report = fa._write_merge_report(where)
    expected = own if its_own else single
    assert report.parent == expected / f"merge-{fa.SOURCE_BUILD}" and report.name == MERGE_REPORT_NAME

    from forge.adapters.sqlite import connect as sqlite_connect
    from forge.lifecycle import migrations as lifecycle_migrations
    from forge.lifecycle.persistence import SqliteLifecyclePersistence
    from forge.planning.work_queue_store import WorkQueueStore

    cx = sqlite_connect.connect_writer(tmp_path / "forge.db")
    lifecycle_migrations.apply_at_boot(cx)
    try:
        pool = SqliteLifecyclePersistence(connection=cx, db_path=tmp_path / "forge.db")
        store = WorkQueueStore(pool.connection)
        fa.seed_failed_build(pool)
        queue_id = fa.TestTheRepairTaskFile()._file_merge_row(store, pack=None)
        config = fa.make_config(
            repo_root, profiles={"attended": {}, fa.FIX_JOURNEY_PROFILE_NAME: {"max_review_cycles": 2}}
        )
        admission = asyncio.run(admit_fix_row(
            config=config, persistence=pool, store=store, queue_id=queue_id,
            correlation_id=fix_correlation_id(fa.SOURCE_BUILD), sentence=fa.MERGE_SENTENCE,
            target_repo=fa.REPO_KEY, publish=fa.Publisher(), originating_user="rich",
            profile=fa.FIX_JOURNEY_PROFILE_NAME,
        ))
    finally:
        cx.close()
    body = fa.show(repo_root, fa.REPAIR_BRANCH, admission.task_file_path)
    assert "- Result: merged-deploy-failed\n" in body, "the report was read where it was written"
    assert f"merge-{fa.SOURCE_BUILD}/{MERGE_REPORT_NAME}" in body
    if its_own:
        assert not single.exists() or list(single.rglob("*")) == [], "nothing in the single root"
    else:
        assert not own.exists(), "a project without a folder of its own is as before"


def test_a_repair_journey_s_own_pack_and_stages_are_in_its_project_s_folder(tmp_path):
    """The conductor writes a repair journey's failure pack and stage receipts
    through fix_journey_receipts, given the build's repository (coach review,
    17 October): a project of its own's go to its folder, any other's to the
    single root, as before."""
    from forge.pipeline.fix_journey_receipts import (
        STAGES_DIRNAME,
        export_stage_receipts,
        fix_journey_receipts_root,
        read_failure_pack,
        write_fix_journey_failure_pack,
    )
    from forge.pipeline.fix_task_context_builder import read_prior_review_evidence

    own, single = _the_two_folders(tmp_path)
    assert fix_journey_receipts_root(repo=OWN_REPO) == own
    assert fix_journey_receipts_root(repo=LEGACY_REPO) == single
    assert fix_journey_receipts_root(tmp_path / "given", repo=OWN_REPO) == tmp_path / "given"

    written = write_fix_journey_failure_pack(
        build_id=BUILD, reason="made up", outcome="failed", repo=OWN_REPO
    )
    assert written is not None and written.parent == own / BUILD
    assert read_failure_pack(BUILD, repo=OWN_REPO) is not None
    assert read_failure_pack(BUILD, repo=LEGACY_REPO) is None
    exported = export_stage_receipts(build_id=BUILD, stage="task-review", worktree_path=None,
                                     repo=OWN_REPO)
    assert exported.dest.parent == own / BUILD / STAGES_DIRNAME
    assert not (single / BUILD).exists(), "nothing in the single root"
    # The follow-up review's stage evidence is read from the same folder.
    assert read_prior_review_evidence(BUILD, repo=OWN_REPO) is None  # one review, no work since

    legacy = write_fix_journey_failure_pack(
        build_id="build-legacy", reason="made up", outcome="failed", repo=LEGACY_REPO
    )
    assert legacy is not None and legacy.parent == single / "build-legacy"
