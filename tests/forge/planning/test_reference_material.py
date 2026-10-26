"""Reference material a project keeps beside its product code (17 October 2026).

Prototype-to-factory design, sections 2.2 and 2.4. A project may declare, in
its own ``.guardkit/config.yaml``::

    reference:
      paths: ["reference/"]
      signed_off: "reference/signed-off.yaml"
      request_words: ["prototype"]

What these hold down:

* the setting is read as declared and a malformed one is refused plainly;
* the planner's inventory leaves reference files out of its list and its
  file count and says each folder in one line;
* code windows from reference folders are kept, labelled and capped at 8;
* the facts sheet and the set search skip reference folders;
* a reference window can never prove that something is already built;
* files a request names under a reference folder reach both writers whole,
  under their heading, on every call; too large, missing or outside stops
  the run before any model is asked; a request using a marker word and
  naming nothing is stopped with the design's sentence;
* a declared signed-off record is checked at the start commit;
* a project WITHOUT the setting is planned exactly as before.

Everything is invented: the "project" is a handful of text files in a scratch
repository or a dictionary. Nothing names a real product or prototype.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from forge.planning.factory_deployment_context import load_bundled_document

from forge.planning import nothing_to_build
from forge.planning.code_evidence import MAX_REFERENCE_WINDOWS
from forge.planning.declared_memory import (
    DeclaredReference,
    read_declared_reference,
)
from forge.planning.driver import PlanningRunDriver
from forge.planning.reference_material import (
    MAX_REFERENCE_FILE_BYTES,
    MAX_REFERENCE_FILES_BYTES,
    NAMES_NO_BRIEF,
    REFERENCE_FILE_HEADING,
    REFERENCE_WINDOW_LABEL,
    ReferenceMaterial,
    gather_reference_material,
    mentions_a_request_word,
    named_reference_files,
    read_signed_off_record,
)
from forge.planning.repository_facts import LocalCheckoutReader, read_repository_facts
from forge.planning.run_store import SqlitePlanningRunStore
from forge.planning.states import PlanningState
from tests.forge.planning import test_driver_spec_digest_door as door
from tests.forge.planning.test_project_documents_for_the_writers import (
    AGENTS,
    DECLARED,
    DECLARES_DOCUMENTS,
    DECLARES_NOTHING,
    MISSION_V1,
    START,
    TECH_V1,
    _chain,
    _Tree,
    chain_store,  # noqa: F401 — fixture
)

REFERENCE_STAGE = "reference-material"
RECORD = "reference/signed-off.yaml"
GOOD_RECORD = (
    "repository: https://example.invalid/acme/widget-mockup.git\n"
    f"commit: {'0123456789abcdef' * 2}01234567\n"
    "signed_off: 2026-10-01\n"
)
DECLARES_REFERENCE = (
    "reference:\n"
    '  paths: ["reference/"]\n'
    f'  signed_off: "{RECORD}"\n'
    '  request_words: ["prototype"]\n'
)
SCREEN = "reference/prototype/src/screens/Gadgets.view\n" + "shows gadgets newest first\n"


# ---------------------------------------------------------------------------
# The setting, as declared
# ---------------------------------------------------------------------------


def test_no_reference_block_declares_nothing() -> None:
    for text in (None, "", "memory:\n  project: p\n", "reference:\n", "reference: null\n"):
        assert read_declared_reference(text) == (DeclaredReference(), None)


def test_a_full_block_is_read_as_declared() -> None:
    declared, why = read_declared_reference(DECLARES_REFERENCE)
    assert why is None
    assert declared == DeclaredReference(
        paths=("reference/",), signed_off=RECORD, request_words=("prototype",)
    )
    assert declared.declared


@pytest.mark.parametrize(
    ("block", "said"),
    [
        ("reference: [x]\n", "is not a set of settings"),
        ('reference:\n  paths: ["reference/"]\n  extra: 1\n', "unknown keys ['extra']"),
        ("reference:\n  signed_off: a.yaml\n", "must list at least one folder"),
        ('reference:\n  paths: ["reference"]\n', "end with '/'"),
        ('reference:\n  paths: ["/reference/"]\n', "repository-relative"),
        ('reference:\n  paths: ["a/../reference/"]\n', "'..'"),
        ('reference:\n  paths: ["./reference/"]\n', "'.'"),
        ('reference:\n  paths: ["reference/"]\n  signed_off: "/r.yaml"\n', "signed_off"),
        ('reference:\n  paths: ["reference/"]\n  signed_off: 7\n', "signed_off"),
        ('reference:\n  paths: ["reference/"]\n  request_words: word\n', "not a list of words"),
        ('reference:\n  paths: ["reference/"]\n  request_words: [""]\n', "is not a word"),
    ],
)
def test_a_malformed_block_is_refused_plainly(block: str, said: str) -> None:
    declared, why = read_declared_reference(block)
    assert declared == DeclaredReference()
    assert why is not None and said in why and ".guardkit/config.yaml" in why


def test_named_files_are_found_however_they_are_written() -> None:
    text = (
        "Build the gadgets page shown in reference/briefs/gadgets.md. "
        "See [the screen](/reference/prototype/src/screens/Gadgets.view) and "
        "`reference/prototype/src/data.mock`; also reference/briefs/gadgets.md again, "
        "the folder reference/prototype/ and myreference/x.md."
    )
    assert named_reference_files(text, ("reference/",)) == [
        "reference/briefs/gadgets.md",
        "reference/prototype/src/screens/Gadgets.view",
        "reference/prototype/src/data.mock",
    ]
    assert named_reference_files("add a GET /version endpoint", ("reference/",)) == []


def test_slack_links_dot_slash_and_fragments_are_read_as_typed() -> None:
    """Slack turns ``gadgets.md`` in a typed path into a link; the text part
    is what was typed. ``./`` and a ``#fragment`` are dropped."""
    text = (
        "build what reference/briefs/<http://gadgets.md|gadgets.md> says, "
        "see ./reference/prototype/src/a.view and "
        "[the list](/reference/briefs/list.md#states)"
    )
    assert named_reference_files(text, ("reference/",)) == [
        "reference/briefs/gadgets.md",
        "reference/prototype/src/a.view",
        "reference/briefs/list.md",
    ]


def test_marker_words_match_whole_words_ignoring_case() -> None:
    assert mentions_a_request_word("Build it from the PROTOTYPE's page", ("prototype",))
    assert not mentions_a_request_word("several prototypes exist", ("prototype",))
    assert not mentions_a_request_word("anything at all", ())
    # Any language's letters are word characters.
    assert not mentions_a_request_word("the prototypeé page", ("prototype",))
    assert mentions_a_request_word("le prototype: écran", ("prototype",))


# ---------------------------------------------------------------------------
# Read at the start commit: the record and the named files
# ---------------------------------------------------------------------------


def _declared() -> DeclaredReference:
    return read_declared_reference(DECLARES_REFERENCE)[0]


async def _gather(tree: _Tree, request: str, declared: DeclaredReference | None = None):
    return await gather_reference_material(
        tree,
        repo_path="/r",
        commit=START,
        declared=declared or _declared(),
        request_text=request,
    )


@pytest.mark.asyncio
async def test_a_good_record_is_read() -> None:
    record, why = await read_signed_off_record(
        _Tree({RECORD: GOOD_RECORD}), repo_path="/r", commit=START, path=RECORD
    )
    assert why is None and record is not None
    assert record.repository_name == "widget-mockup"
    assert record.short == "0123456"
    assert record.signed_off == "2026-10-01"


@pytest.mark.parametrize(
    ("files", "said"),
    [
        ({}, "does not exist"),
        ({RECORD: "just words"}, "is not a set of settings"),
        ({RECORD: "repository: x\n"}, "full 40-character commit"),
        ({RECORD: "repository: x\ncommit: abc1234\n"}, "full 40-character commit"),
        ({RECORD: f"commit: {'a' * 40}\n"}, "names no repository"),
        ({RECORD: "repository: [x\n"}, "could not be read"),
    ],
)
@pytest.mark.asyncio
async def test_a_broken_record_stops_the_run_plainly(files: dict[str, str], said: str) -> None:
    material, why = await _gather(_Tree(files), "add a GET /version endpoint")
    assert material is None
    assert why is not None and said in why and RECORD in why and START[:12] in why


@pytest.mark.asyncio
async def test_without_a_record_setting_nothing_is_checked() -> None:
    tree = _Tree({})
    declared = DeclaredReference(paths=("reference/",))
    material, why = await _gather(tree, "add a GET /version endpoint", declared)
    assert why is None and material == ReferenceMaterial(paths=("reference/",))
    assert tree.reads == []


@pytest.mark.asyncio
async def test_named_files_are_read_whole_at_the_start_commit() -> None:
    tree = _Tree({RECORD: GOOD_RECORD, "reference/briefs/gadgets.md": "# Gadgets\n"})
    material, why = await _gather(tree, "build the page in reference/briefs/gadgets.md")
    assert why is None and material is not None
    (document,) = material.files
    assert (document.path, document.text, document.commit) == (
        "reference/briefs/gadgets.md",
        "# Gadgets\n",
        START,
    )
    assert {commit for commit, _path, _raw in tree.reads} == {START}
    assert ReferenceMaterial.from_record(json.loads(json.dumps(material.to_record()))) == material


@pytest.mark.parametrize(
    ("request_text", "files", "said"),
    [
        ("build reference/briefs/missing.md", {}, "is not there"),
        ("build reference/../src/app.code", {}, "lies outside the reference folders"),
        ("build reference/briefs", {"reference/briefs/a.md": "x"}, "is not an ordinary file"),
    ],
)
@pytest.mark.asyncio
async def test_what_stops_the_run_and_why(
    request_text: str, files: dict[str, str], said: str
) -> None:
    material, why = await _gather(_Tree({RECORD: GOOD_RECORD, **files}), request_text)
    assert material is None
    assert why is not None and said in why


@pytest.mark.parametrize(
    ("request_text", "files", "said"),
    [
        (
            "build reference/a.md",
            {"reference/a.md": "x" * (MAX_REFERENCE_FILE_BYTES + 1)},
            "over the guide",
        ),
        (
            "build reference/a.md and reference/b.md and reference/c.md",
            {
                "reference/a.md": "x" * MAX_REFERENCE_FILE_BYTES,
                "reference/b.md": "x" * MAX_REFERENCE_FILE_BYTES,
                "reference/c.md": "x",
            },
            "over the guide",
        ),
        ("build the page from the prototype", {}, NAMES_NO_BRIEF),
    ],
)
@pytest.mark.asyncio
async def test_what_is_advice_and_carries_on(
    request_text: str,
    files: dict[str, str],
    said: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Step 2 (26 October 2026, census FRG-026 and FRG-027): the size limits
    are a guide, and a marker word with no screen brief named is advice. The
    run carries on; the named files are given whole."""
    with caplog.at_level("WARNING"):
        material, why = await _gather(
            _Tree({RECORD: GOOD_RECORD, **files}), request_text
        )
    assert why is None and material is not None
    assert [d.path for d in material.files] == sorted(files)
    assert said in caplog.text and "ADVISORY" in caplog.text


@pytest.mark.asyncio
async def test_the_limits_are_whole_files_up_to_the_totals() -> None:
    files = {
        "reference/a.md": "x" * MAX_REFERENCE_FILE_BYTES,
        "reference/b.md": "y" * (MAX_REFERENCE_FILES_BYTES - MAX_REFERENCE_FILE_BYTES),
    }
    material, why = await _gather(
        _Tree({RECORD: GOOD_RECORD, **files}),
        "the prototype screens reference/a.md and reference/b.md",
    )
    assert why is None and material is not None
    assert [d.text for d in material.files] == list(files.values())


# ---------------------------------------------------------------------------
# The planner's description of the repository
# ---------------------------------------------------------------------------


def _repo(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
         "commit", "-q", "-m", "seed"],
        check=True,
    )
    return root


SPEC = "Feature: gadgets\n  Scenario: list\n    When I ask for the gadget_list\n"
REQUEST = "show the gadget_list on the gadgets page"


def _product_and_reference(tmp_path: Path, reference_files: int = 3) -> Path:
    files = {
        "src/gadgets/api.code": "route gadget_list returns all gadgets\n",
        "src/gadgets/store.code": "keeps gadgets\n",
        "tests/gadgets/test_list.code": "checks gadget_list\n",
    }
    for n in range(reference_files):
        files[f"reference/prototype/src/screen{n:03d}.view"] = (
            f"screen {n} draws the gadget_list\n"
        )
    return _repo(tmp_path / "project", files)


def _describe(repo: Path, **kwargs: Any) -> dict[str, Any]:
    return PlanningRunDriver._build_target_repo_descriptor(
        "acme/gadgets",
        str(repo),
        SPEC,
        reader=LocalCheckoutReader(str(repo)),
        request_text=REQUEST,
        **kwargs,
    )


def _windows(descriptor: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        window
        for entry in descriptor.get("where_the_specs_words_already_appear") or []
        for window in entry.get("evidence") or []
    ]


def test_without_reference_folders_the_description_is_unchanged(tmp_path: Path) -> None:
    repo = _product_and_reference(tmp_path)
    assert _describe(repo) == _describe(repo, reference_paths=())
    plain = _describe(repo)
    assert "reference_material" not in plain["repository_inventory"]
    assert "reference/prototype/src/screen000.view" in plain["repository_inventory"]["files"]
    assert all("label" not in window for window in _windows(plain))


def test_the_inventory_lists_product_files_and_one_line_per_folder(tmp_path: Path) -> None:
    # Enough reference files to push the whole tree over the 400-file list.
    repo = _product_and_reference(tmp_path, reference_files=450)
    inventory = _describe(repo, reference_paths=("reference/",))["repository_inventory"]
    assert inventory["files"] == [
        "src/gadgets/api.code",
        "src/gadgets/store.code",
        "tests/gadgets/test_list.code",
    ]
    assert inventory["file_count"] == 3
    assert inventory["reference_material"] == [
        "`reference/` — 450 files of reference material (not product code); "
        "see the bound index"
    ]
    # The control: without the setting the same tree is folder counts only.
    assert _describe(repo)["repository_inventory"].get("directories_only") is True


def test_reference_windows_are_labelled_and_capped(tmp_path: Path) -> None:
    repo = _product_and_reference(tmp_path, reference_files=MAX_REFERENCE_WINDOWS + 6)
    windows = _windows(_describe(repo, reference_paths=("reference/",)))
    reference = [w for w in windows if w["path"].startswith("reference/")]
    product = [w for w in windows if not w["path"].startswith("reference/")]
    assert 1 <= len(reference) <= MAX_REFERENCE_WINDOWS
    assert all(w["label"] == REFERENCE_WINDOW_LABEL for w in reference)
    assert {w["path"] for w in product} >= {"src/gadgets/api.code"}
    assert all("label" not in w for w in product)
    # The control: without the setting more reference windows are shown.
    plain = [w for w in _windows(_describe(repo)) if w["path"].startswith("reference/")]
    assert len(plain) > MAX_REFERENCE_WINDOWS


def test_no_set_candidate_comes_from_a_reference_folder(tmp_path: Path) -> None:
    repo = _product_and_reference(tmp_path)

    def candidates(**kwargs: Any) -> list[str]:
        sets = PlanningRunDriver._sets_the_request_names(
            "list all the gadgets", reader=LocalCheckoutReader(str(repo)), **kwargs
        )
        return [c["path"] for entry in sets or [] for c in entry["candidates"]]

    assert any(p.startswith("reference/") for p in candidates())
    kept = candidates(reference_paths=("reference/",))
    assert kept and not any(p.startswith("reference/") for p in kept)


def test_no_test_folder_is_found_inside_reference_material(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path / "project",
        {
            "src/app.py": "x = 1\n",
            "tests/test_app.py": "def test_x():\n    pass\n",
            "reference/prototype/tests/test_screen.py": "def test_y():\n    pass\n",
        },
    )
    roots = _describe(repo, reference_paths=("reference/",))["test_roots"]
    assert not any(str(root).startswith("reference/") for root in roots)


def test_the_facts_sheet_skips_reference_folders(tmp_path: Path) -> None:
    routes = (
        "from fastapi import APIRouter\n\nrouter = APIRouter()\n\n\n"
        '@router.get("/gadgets/recent")\nasync def recent():\n    return []\n'
    )
    repo = _repo(
        tmp_path / "project",
        {"src/readme.md": "nothing\n", "reference/prototype/api/routes.py": routes},
    )
    request = "add GET /gadgets/count"
    plain = read_repository_facts(LocalCheckoutReader(str(repo)), request).text or ""
    assert "reference/prototype/api/routes.py" in plain
    skipped = read_repository_facts(
        LocalCheckoutReader(str(repo)), request, reference_paths=("reference/",)
    ).text
    assert "reference/" not in (skipped or "")


def test_a_reference_window_never_proves_something_is_built() -> None:
    descriptor = {
        "where_the_specs_words_already_appear": [
            {
                "evidence": [
                    {"path": "src/a.code", "first_line": 1, "last_line": 9, "text": ""},
                    {
                        "path": "reference/prototype/b.view",
                        "first_line": 1,
                        "last_line": 9,
                        "text": "",
                        "label": REFERENCE_WINDOW_LABEL,
                    },
                ]
            }
        ]
    }
    claim = nothing_to_build.Claim(
        parts=(("show gadgets", ("reference/prototype/b.view:3",)),),
        scenarios=(("lists gadgets", ("src/a.code:2",)),),
    )
    everything = nothing_to_build.windows_sent(descriptor)
    assert len(everything) == 2
    assert nothing_to_build.citations_outside(claim, everything) == []
    product_only = nothing_to_build.windows_sent(descriptor, reference_paths=("reference/",))
    assert [w.path for w in product_only] == ["src/a.code"]
    assert nothing_to_build.citations_outside(claim, product_only) == [
        "reference/prototype/b.view:3"
    ]


# ---------------------------------------------------------------------------
# The whole chain: what the writers are sent, and what stops the run
# ---------------------------------------------------------------------------


def _queue(store: SqlitePlanningRunStore, request_text: str) -> None:
    store.record_queued(
        correlation_id=door.CID,
        originating_user=door.ORIGINATOR,
        expected_approver=door.ORIGINATOR,
        request_text=request_text,
        triggered_by="jarvis",
        target_repo=door.TARGET_REPO,
    )


def _events(store: SqlitePlanningRunStore, stage: str) -> list[dict[str, Any]]:
    return [
        json.loads(event["details_json"] or "{}")
        for event in store.list_events(door.CID)
        if event["stage_label"] == stage
    ]


def _reference_tree(config: str = DECLARES_DOCUMENTS + DECLARES_REFERENCE) -> _Tree:
    return _Tree(
        {
            ".guardkit/config.yaml": config,
            "AGENTS.md": AGENTS,
            DECLARED[0]: MISSION_V1,
            DECLARED[1]: TECH_V1,
            RECORD: GOOD_RECORD,
            "reference/briefs/gadgets.md": "# Gadgets screen\n\nNewest first.\n",
        },
        links={"CLAUDE.md": "AGENTS.md"},
    )


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """What the plan leg handed the description and the facts sheet."""
    calls: dict[str, list[Any]] = {"descriptor": [], "facts": []}
    real_describe = PlanningRunDriver._build_target_repo_descriptor

    def describe(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls["descriptor"].append(kwargs.get("reference_paths", ()))
        return real_describe(*args, **kwargs)

    monkeypatch.setattr(
        PlanningRunDriver, "_build_target_repo_descriptor", staticmethod(describe)
    )
    import forge.planning.driver as driver_module

    real_facts = driver_module.read_repository_facts

    def facts(*args: Any, **kwargs: Any) -> Any:
        calls["facts"].append(kwargs.get("reference_paths", ()))
        return real_facts(*args, **kwargs)

    monkeypatch.setattr(driver_module, "read_repository_facts", facts)
    return calls


@pytest.mark.asyncio
async def test_named_reference_files_reach_both_writers_on_every_call(
    chain_store: SqlitePlanningRunStore, seen: dict[str, list[Any]]
) -> None:
    tree = _reference_tree()
    _queue(chain_store, "build the gadgets page from the prototype, as reference/briefs/gadgets.md says")
    h = _chain(
        chain_store,
        tree,
        answers=[
            door._answer("reject", notes="say newest first"),
            door._answer("approve", attempt=1),
        ],
    )
    await h.driver.drive(door.CID)

    assert chain_store.get_run(door.CID)["state"] == PlanningState.BUILD_QUEUED.value
    expected = [
        f"File: AGENTS.md\n{AGENTS}",
        f"File: {DECLARED[0]}\n{MISSION_V1}",
        f"File: {DECLARED[1]}\n{TECH_V1}",
        f"{REFERENCE_FILE_HEADING}\nFile: reference/briefs/gadgets.md\n"
        "# Gadgets screen\n\nNewest first.\n",
    ]
    expected.append(load_bundled_document().text)
    assert [call["context"] for call in h.calls["spec"]] == [expected, expected]
    assert [call["context"] for call in h.calls["plan"]] == [expected]
    # Recorded once, at the door, before anything was asked of a model.
    (recorded,) = _events(chain_store, REFERENCE_STAGE)
    assert recorded["reference_material"]["paths"] == ["reference/"]
    labels = [event["stage_label"] for event in chain_store.list_events(door.CID)]
    assert labels.index(REFERENCE_STAGE) < labels.index("product_owner")
    # The receipts say which reference file, which version.
    (plan_row,) = _events(chain_store, "feature-plan")
    assert plan_row["reference_files"]["documents"][0]["path"] == "reference/briefs/gadgets.md"
    # The plan's description and the facts sheet were told the folders.
    assert seen["descriptor"] and set(seen["descriptor"]) == {("reference/",)}
    assert seen["facts"] and set(seen["facts"]) == {("reference/",)}


@pytest.mark.asyncio
async def test_a_request_naming_no_reference_file_sends_todays_call(
    chain_store: SqlitePlanningRunStore,
) -> None:
    tree = _reference_tree(DECLARES_NOTHING + DECLARES_REFERENCE)
    _queue(chain_store, "add a GET /version endpoint")
    h = _chain(chain_store, tree, answers=[door._answer("approve")])
    await h.driver.drive(door.CID)

    assert chain_store.get_run(door.CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert all(call["context"] == [load_bundled_document().text]
               for call in h.calls["spec"] + h.calls["plan"])
    (recorded,) = _events(chain_store, REFERENCE_STAGE)
    assert recorded["reference_material"]["files"] == []
    (plan_row,) = _events(chain_store, "feature-plan")
    assert "reference_files" not in plan_row


@pytest.mark.asyncio
async def test_a_project_without_the_setting_is_planned_as_before(
    chain_store: SqlitePlanningRunStore, seen: dict[str, list[Any]]
) -> None:
    tree = _Tree({".guardkit/config.yaml": DECLARES_NOTHING, "AGENTS.md": AGENTS})
    # Even a request that names a reference-looking file and the marker word.
    _queue(chain_store, "build the prototype page in reference/briefs/gadgets.md")
    h = _chain(chain_store, tree, answers=[door._answer("approve")])
    await h.driver.drive(door.CID)

    assert chain_store.get_run(door.CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert all(call["context"] == [load_bundled_document().text]
               for call in h.calls["spec"] + h.calls["plan"])
    assert _events(chain_store, REFERENCE_STAGE) == []
    assert [path for _c, path, _r in tree.reads] == [".guardkit/config.yaml"] * len(tree.reads)
    assert set(seen["descriptor"]) == {()}
    assert set(seen["facts"]) == {()}


@pytest.mark.parametrize(
    ("request_text", "change", "said"),
    [
        ("build reference/briefs/missing.md", None, "reference/briefs/missing.md"),
        ("add a GET /version endpoint", lambda t: t.files.pop(RECORD), RECORD),
        (
            "add a GET /version endpoint",
            lambda t: t.files.__setitem__(
                ".guardkit/config.yaml",
                DECLARES_NOTHING + 'reference:\n  paths: ["reference"]\n',
            ),
            "end with '/'",
        ),
    ],
    ids=["missing", "no-record", "malformed"],
)
@pytest.mark.asyncio
async def test_a_refusal_stops_the_run_before_any_model_is_asked(
    chain_store: SqlitePlanningRunStore, request_text: str, change: Any, said: str
) -> None:
    tree = _reference_tree()
    if change is not None:
        change(tree)
    _queue(chain_store, request_text)
    h = _chain(chain_store, tree, answers=[door._answer("approve")])

    await h.driver.drive(door.CID)

    assert chain_store.get_run(door.CID)["state"] == PlanningState.FAILED.value
    assert h.calls == {"po": [], "spec": [], "plan": []}
    errors = [message for _cid, message, level in h.notifications if level == "error"]
    assert len(errors) == 1 and said in errors[0]
    assert _events(chain_store, REFERENCE_STAGE) == []
    assert chain_store.get_memory_project(door.CID) is None
