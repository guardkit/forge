"""The spec writer sees the code (17 October 2026, FEAT-15DD).

Design: ai-transition ``docs/designs/spec-writer-sees-the-code-2026-10-17.md``
(Codex design review round 3 accepted).

FEAT-15DD asked for "3 notes" above the notes list. The notes fixture already
rendered ``{notes.length} notes`` and a committed browser check required "N
notes" for every count, but the spec writer was shown no code and guessed "1
note", and neither writer was shown either line: the word search looks only for
names, and the sentence is plain English. These tests cover the ordinary-code
half of the fix:

* the quoted-example search (:func:`quoted_example_shapes`) and its rejects;
* a replay over the fixture's real lines at ``2799d7e0`` (only the few lines
  needed, at their real line numbers; the fixture is public);
* the plan writer's description is unchanged when that search finds nothing;
* the spec call: windows only, built once per run, no ranked blocks,
  reference material labelled and capped, 8,000 characters of window text.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli._serve_planning import build_feature_spec_command_args
from forge.lifecycle import migrations
from forge.planning import driver as driver_module
from forge.planning.code_evidence import (
    MAX_EVIDENCE_CHARS,
    MAX_REFERENCE_WINDOWS,
    MAX_SPEC_EVIDENCE_CHARS,
    quoted_example_shapes,
    spec_writer_windows,
)
from forge.planning.driver import PlanningRunDriver
from forge.planning.reference_material import REFERENCE_WINDOW_LABEL
from forge.planning.repository_facts import LocalCheckoutReader
from forge.planning.run_store import SqlitePlanningRunStore
from forge.planning.states import PlanningState
from tests.forge.planning import test_driver_spec_digest_door as door
from tests.forge.planning.test_memory_name_at_the_door import (  # noqa: F401
    _local_remotes,  # the autouse fixture: the stand-in remote is a local folder
)
from tests.forge.planning.test_project_documents_for_the_writers import (
    DECLARES_NOTHING,
    _chain,
    _Tree,
)

#: The FEAT-15DD sentence, word for word.
REQUEST = (
    'Show how many notes there are above the notes list, for example "3 notes". '
    "Change only the web app and its tests."
)
#: The approved specification of FEAT-15DD, as the design saved it from the
#: ledger (event 8628): it carries the invented "1 note".
APPROVED_SPEC = (
    Path(__file__).parent / "fixtures" / "feat_15dd" / "approved_spec.feature"
).read_text(encoding="utf-8")

# The fixture's real lines at 2799d7e0, keyed by their real line numbers.
APP_TSX = {
    110: "              }}",
    111: "            />",
    112: '            <button type="submit" disabled={busy || !label.trim()}>',
    113: '              {busy ? "Saving…" : "Save"}',
    114: "            </button>",
    115: "          </form>",
    116: '          <p data-testid="notes-count">{notes.length} notes</p>',
    117: '          <ul aria-label="Your notes">',
    118: "            {notes.map((note) => (",
    119: "              <li key={note.id}>{note.label}</li>",
    120: "            ))}",
}
JOURNEY = {
    43: '// Asserts the total is displayed above the notes list in the format "N notes",',
    44: "// that N is the number of notes the list actually renders, and that the total",
    45: "// belongs to the summary region rather than to any note item.",
    46: "async function expectTotalAboveList(",
    47: "  summary: Locator,",
    48: "  list: Locator,",
    49: "  expected: number,",
    50: "): Promise<void> {",
    51: "  await expect(summary).toHaveText(`${expected} notes`);",
    52: "  await expect(summary).toHaveText(/^\\d+ notes$/);",
    53: "  // The same collection populates both: the digits shown are the number of",
    54: "  // notes shown in the list below.",
    55: '  await expect(list.getByRole("listitem")).toHaveCount(expected);',
    56: "  expect(await displayedTotal(summary)).toBe(",
    57: '    await list.getByRole("listitem").count(),',
    58: "  );",
    59: "  // The total is a summary of the collection, never part of a note item.",
    60: "  await expect(list.getByTestId(SUMMARY)).toHaveCount(0);",
    61: "  await expect(",
    62: "    list.getByText(`${expected} notes`, { exact: true }),",
    63: "  ).toHaveCount(0);",
}
APP_TEST = {
    550: '  it("carries the count the moment the list is first shown", async () => {',
    551: "    const container = await viewNotes(collectionOf(3));",
    552: '    expect(tabTitle()).toBe("Notes (3)");',
    553: "    // The two surfaces of the one collection agree on load.",
    554: '    expect(totalAboveTheList(container).textContent).toBe("3 notes");',
    555: "    expect(tabTitleCount()).toBe(",
    556: '      Number(totalAboveTheList(container).textContent.replace(" notes", "")),',
}
#: Lines that name notes without a count in the number's place: never kept.
STORE_GO = {
    1: "package notes",
    2: "",
    3: "// List returns owner's newest notes first.",
    4: "func (s PGStore) List(owner string) ([]Note, error) {",
    5: "\tnotes := []Note{}",
    6: '\treturn notes, fmt.Errorf("query notes: %w", err)',
    7: "}",
}


def _at_their_lines(lines: dict[int, str]) -> str:
    """The lines at their real numbers, every other line blank."""
    last = max(lines)
    return "\n".join(lines.get(n, "") for n in range(1, last + 1)) + "\n"


def _repo(root: Path, files: dict[str, str]) -> tuple[Path, str]:
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
    sha = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return root, sha


def _notes_fixture(tmp_path: Path, extra: dict[str, str] | None = None) -> tuple[Path, str]:
    return _repo(
        tmp_path / "notes",
        {
            "apps/web/src/App.tsx": _at_their_lines(APP_TSX),
            "tests/e2e/specs/journey.spec.ts": _at_their_lines(JOURNEY),
            "apps/web/src/App.test.tsx": _at_their_lines(APP_TEST),
            "apps/api/internal/notes/store.go": _at_their_lines(STORE_GO),
            **(extra or {}),
        },
    )


def _describe(repo: Path, sha: str, spec: str = "", **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("request_text", REQUEST)
    return PlanningRunDriver._build_target_repo_descriptor(
        "guardkit/react-go-notes-fixture",
        str(repo),
        spec,
        reader=LocalCheckoutReader(str(repo), commit=sha),
        unavailable=[],
        partial=[],
        **kwargs,
    )


def _windows(descriptor: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [
        window
        for entry in (descriptor or {}).get("where_the_specs_words_already_appear") or []
        for window in entry.get("evidence") or []
    ]


def _covers(windows: list[dict[str, Any]], path: str, line: int) -> bool:
    return any(
        w["path"] == path and w["first_line"] <= line <= w["last_line"] for w in windows
    )


# ---------------------------------------------------------------------------
# The quoted-example search, ordinary code
# ---------------------------------------------------------------------------


def _one(request: str) -> Any:
    (example,) = quoted_example_shapes(request)
    return example


@pytest.mark.parametrize(
    "line",
    [
        '<p data-testid="notes-count">{notes.length} notes</p>',
        "await expect(summary).toHaveText(`${expected} notes`);",
        'expect(total.textContent).toBe("12 notes");',
        'printf("%d notes\\n", count);',
        "const match = /^(\\d+) notes$/.exec(text);",
    ],
)
def test_the_count_example_keeps_every_way_code_writes_a_count(line: str) -> None:
    example = _one(REQUEST)
    assert example.phrase == "3 notes"
    assert example.literal == " notes"
    assert example.holds(line)


@pytest.mark.parametrize(
    "line",
    [
        "const total = notes;",
        "  return { notes: [] };",
        "there are many notes here",
        "// the notes list",
        "\tnotes := []Note{}",
    ],
)
def test_the_count_example_rejects_lines_without_a_count(line: str) -> None:
    assert not _one(REQUEST).holds(line)


def test_a_count_inside_brackets_matches_the_tab_title_example() -> None:
    example = _one('Show the number of notes in the tab title, for example "Notes (3)".')
    assert example.literal == "Notes ("
    for line in (
        "document.title = `Notes (${notes.length})`;",
        "expect(tabTitle()).toBe(`Notes (${size})`);",
        'expect(tabTitle()).toBe("Notes (3)");',
    ):
        assert example.holds(line), line
    for line in ("Notes (see below)", "Notes ()", "const Notes = 3;"):
        assert not example.holds(line), line


def test_only_quoted_phrases_with_a_number_are_examples() -> None:
    assert quoted_example_shapes("Show how many notes there are above the list.") == []
    assert quoted_example_shapes('Rename the "Save" button.') == []
    # An apostrophe inside a word is not a quote.
    assert quoted_example_shapes("Don't show it; it's 3 notes at most.") == []
    # A phrase with no letters beside its number would find everything.
    assert quoted_example_shapes('Show "(3)" after the title.') == []


def test_at_most_two_examples_and_each_quoting_style() -> None:
    found = quoted_example_shapes(
        "Show “3 notes”, `Notes (3)`, '2 items' and \"5 tags\"."
    )
    assert [e.phrase for e in found] == ["3 notes", "Notes (3)"]


# ---------------------------------------------------------------------------
# The FEAT-15DD replay over the fixture's real lines
# ---------------------------------------------------------------------------


def test_the_request_alone_finds_the_label_and_the_committed_check(tmp_path: Path) -> None:
    repo, sha = _notes_fixture(tmp_path)
    descriptor = _describe(repo, sha)
    windows = _windows(descriptor)
    assert _covers(windows, "apps/web/src/App.tsx", 116)
    assert _covers(windows, "tests/e2e/specs/journey.spec.ts", 51)
    (entry,) = descriptor["where_the_specs_words_already_appear"]
    assert entry["words"] == '"3 notes"'
    # Lines naming notes with no count in the number's place are not places.
    assert not any("store.go" in place for place in entry["already_in"])
    assert "apps/web/src/App.tsx:116" in entry["already_in"]


def test_the_approved_spec_finds_them_too(tmp_path: Path) -> None:
    repo, sha = _notes_fixture(tmp_path)
    windows = _windows(_describe(repo, sha, APPROVED_SPEC))
    assert _covers(windows, "apps/web/src/App.tsx", 116)
    assert _covers(windows, "tests/e2e/specs/journey.spec.ts", 51)


def test_the_spec_writer_is_shown_the_same_windows(tmp_path: Path) -> None:
    repo, sha = _notes_fixture(tmp_path)
    shown = spec_writer_windows(_describe(repo, sha, max_evidence_chars=MAX_SPEC_EVIDENCE_CHARS))
    assert shown is not None and list(shown) == ["where_the_specs_words_already_appear"]
    windows = _windows(shown)
    assert _covers(windows, "apps/web/src/App.tsx", 116)
    assert _covers(windows, "tests/e2e/specs/journey.spec.ts", 51)


# ---------------------------------------------------------------------------
# Nothing found, nothing changed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "request_text",
    [
        # A quoted number found nowhere.
        'Show the tag count, for example "7 tags". Keep the notes-count label.',
        # No quoted number at all, with an identifier that is found.
        "Keep the notes-count label above the list.",
        # Nothing to look for.
        "Make it nicer.",
    ],
)
def test_when_the_quoted_search_finds_nothing_the_plan_description_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request_text: str
) -> None:
    repo, sha = _notes_fixture(tmp_path)
    now = json.dumps(_describe(repo, sha, APPROVED_SPEC, request_text=request_text))
    # Today's description: the same builder with the quoted search taken out.
    monkeypatch.setattr(driver_module, "quoted_example_shapes", lambda _text: [])
    before = json.dumps(_describe(repo, sha, APPROVED_SPEC, request_text=request_text))
    assert now == before


def test_with_no_quoted_number_an_identifier_still_reaches_the_spec_writer(
    tmp_path: Path,
) -> None:
    repo, sha = _notes_fixture(tmp_path)
    shown = spec_writer_windows(
        _describe(
            repo,
            sha,
            request_text="Move the notes-count label below the list.",
            max_evidence_chars=MAX_SPEC_EVIDENCE_CHARS,
        )
    )
    assert shown is not None
    assert _covers(_windows(shown), "apps/web/src/App.tsx", 116)


def test_no_windows_means_no_field(tmp_path: Path) -> None:
    repo, sha = _notes_fixture(tmp_path)
    assert spec_writer_windows(_describe(repo, sha, request_text="Make it nicer.")) is None
    # The wire builder sends the key only when it has something.
    for empty in (None, {}):
        assert "code_evidence" not in build_feature_spec_command_args(
            from_input="x", code_evidence=empty
        )
    sent = build_feature_spec_command_args(
        from_input="x", code_evidence={"where_the_specs_words_already_appear": [{"words": "w"}]}
    )
    assert sent["code_evidence"] == {"where_the_specs_words_already_appear": [{"words": "w"}]}


# ---------------------------------------------------------------------------
# The spec call's size and its reference material
# ---------------------------------------------------------------------------


def _many_counts(tmp_path: Path) -> tuple[Path, str]:
    files = {
        f"apps/web/src/view{n:02d}.tsx": (
            "\n".join(f"// filler {n}-{k} for the count view" for k in range(20))
            + f'\n<p className="total">{{items{n}.length}} notes</p>\n'
            + "\n".join(f"// more filler {n}-{k}" for k in range(12))
            + "\n"
        )
        for n in range(30)
    }
    return _repo(tmp_path / "many", files)


def test_the_spec_windows_hold_at_most_8000_characters(tmp_path: Path) -> None:
    repo, sha = _many_counts(tmp_path)

    def text_chars(descriptor: dict[str, Any] | None) -> int:
        return sum(len(w["text"]) for w in _windows(descriptor))

    spec = spec_writer_windows(_describe(repo, sha, max_evidence_chars=MAX_SPEC_EVIDENCE_CHARS))
    plan = _describe(repo, sha)
    assert 0 < text_chars(spec) <= MAX_SPEC_EVIDENCE_CHARS
    # The control: the plan's budget is the one it always was, and bigger.
    assert MAX_SPEC_EVIDENCE_CHARS < text_chars(plan) <= MAX_EVIDENCE_CHARS


def _with_reference(tmp_path: Path) -> tuple[Path, str]:
    extra = {
        f"reference/prototype/screen{n:02d}.view": f"screen {n}: {{count}} notes\n"
        for n in range(MAX_REFERENCE_WINDOWS + 4)
    }
    # Found only by a ranked search: it holds no word or example of the request.
    extra["reference/prototype/hidden.view"] = "a summary line for the list\n"
    return _notes_fixture(tmp_path, extra)


def test_reference_windows_reach_the_spec_writer_labelled_and_capped(tmp_path: Path) -> None:
    repo, sha = _with_reference(tmp_path)
    shown = spec_writer_windows(
        _describe(
            repo,
            sha,
            reference_paths=("reference/",),
            max_evidence_chars=MAX_SPEC_EVIDENCE_CHARS,
        )
    )
    windows = _windows(shown)
    reference = [w for w in windows if w["path"].startswith("reference/")]
    assert 1 <= len(reference) <= MAX_REFERENCE_WINDOWS
    assert all(w["label"] == REFERENCE_WINDOW_LABEL for w in reference)
    assert all("label" not in w for w in windows if not w["path"].startswith("reference/"))


# ---------------------------------------------------------------------------
# The spec leg: once per run, every spec call, never the ranked blocks
# ---------------------------------------------------------------------------


@pytest.fixture
def chain_store(tmp_path: Path) -> SqlitePlanningRunStore:
    cx = sqlite_connect.connect_writer(tmp_path / "chain.db")
    migrations.apply_at_boot(cx)
    return SqlitePlanningRunStore(cx, target_terminal_enabled=True)


def _queue(store: SqlitePlanningRunStore, request_text: str = REQUEST) -> None:
    store.record_queued(
        correlation_id=door.CID,
        originating_user=door.ORIGINATOR,
        expected_approver=door.ORIGINATOR,
        request_text=request_text,
        triggered_by="jarvis",
        target_repo=door.TARGET_REPO,
    )


class _RankedReader:
    """The checkout reader, plus a ranked search that answers with one block
    from a reference file and counts how often it was asked."""

    def __init__(self, inner: LocalCheckoutReader, sha: str) -> None:
        self._inner = inner
        self._sha = sha
        self.ranked_asks: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def ranked_source(self, query: str, **_: Any) -> dict[str, Any]:
        self.ranked_asks.append(query)
        return {
            "status": "ok",
            "source": {"commit": self._sha},
            "query": {"words": query.split()},
            "results": [
                {"path": "reference/prototype/hidden.view", "first_line": 1,
                 "last_line": 1, "score": 9.0},
            ],
        }


@pytest.mark.asyncio
async def test_every_spec_call_of_the_run_gets_the_same_windows_built_once(
    chain_store: SqlitePlanningRunStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, sha = _notes_fixture(tmp_path)
    built: list[int] = []
    real = PlanningRunDriver._build_target_repo_descriptor

    def counting(*args: Any, **kwargs: Any) -> dict[str, Any]:
        built.append(kwargs.get("max_evidence_chars", MAX_EVIDENCE_CHARS))
        return real(*args, **kwargs)

    monkeypatch.setattr(PlanningRunDriver, "_build_target_repo_descriptor", staticmethod(counting))
    _queue(chain_store)
    h = _chain(
        chain_store,
        _Tree({".guardkit/config.yaml": DECLARES_NOTHING}),
        answers=[
            door._answer("reject", notes="say what the label reads for no notes"),
            door._answer("reject", notes="keep the worked examples short", attempt=1),
            door._answer("approve", attempt=2),
        ],
    )
    h.driver._deps.git_runner.reader = LocalCheckoutReader(str(repo), commit=sha)

    await h.driver.drive(door.CID)

    assert chain_store.get_run(door.CID)["state"] == PlanningState.BUILD_QUEUED.value
    spec_calls = h.calls["spec"]
    assert len(spec_calls) == 3
    first = spec_calls[0]["code_evidence"]
    assert all(call["code_evidence"] == first for call in spec_calls)
    assert _covers(_windows(first), "apps/web/src/App.tsx", 116)
    assert _covers(_windows(first), "tests/e2e/specs/journey.spec.ts", 51)
    # Built once for the spec leg, at the spec's budget; the plan leg builds its own.
    assert built.count(MAX_SPEC_EVIDENCE_CHARS) == 1
    assert built.count(MAX_EVIDENCE_CHARS) == 1
    # The plan writer's description carries the same lines.
    (plan_call,) = h.calls["plan"]
    plan_windows = _windows(plan_call["target_repo_descriptor"])
    assert _covers(plan_windows, "apps/web/src/App.tsx", 116)
    assert _covers(plan_windows, "tests/e2e/specs/journey.spec.ts", 51)


@pytest.mark.asyncio
async def test_a_run_whose_repository_shows_nothing_sends_no_field(
    chain_store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    repo, sha = _notes_fixture(tmp_path)
    _queue(chain_store, "Make it nicer.")
    h = _chain(
        chain_store,
        _Tree({".guardkit/config.yaml": DECLARES_NOTHING}),
        answers=[door._answer("approve")],
    )
    h.driver._deps.git_runner.reader = LocalCheckoutReader(str(repo), commit=sha)
    await h.driver.drive(door.CID)
    assert h.calls["spec"] and all("code_evidence" not in c for c in h.calls["spec"])


@pytest.mark.asyncio
async def test_the_spec_call_never_carries_ranked_blocks(
    chain_store: SqlitePlanningRunStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, sha = _with_reference(tmp_path)
    reader = _RankedReader(LocalCheckoutReader(str(repo), commit=sha), sha)
    _queue(chain_store)
    h = _chain(
        chain_store,
        _Tree({".guardkit/config.yaml": DECLARES_NOTHING}),
        answers=[door._answer("approve")],
    )
    h.driver._deps.git_runner.reader = reader
    monkeypatch.setattr(PlanningRunDriver, "_ranked_source_enabled", lambda self: True)
    monkeypatch.setattr(
        PlanningRunDriver, "_reference_paths_of", lambda self, cid: ("reference/",)
    )

    evidence = await h.driver._spec_code_evidence(
        {"request_text": REQUEST},
        door.CID,
        target_repo=door.TARGET_REPO,
        repo_path=str(repo),
    )

    assert reader.ranked_asks == []
    assert evidence is not None
    assert "ranked_source" not in json.dumps(evidence)
    windows = _windows(evidence)
    paths = {w["path"] for w in windows}
    assert "reference/prototype/hidden.view" not in paths
    reference = [w for w in windows if w["path"].startswith("reference/")]
    assert 1 <= len(reference) <= MAX_REFERENCE_WINDOWS
    assert all(w["label"] == REFERENCE_WINDOW_LABEL for w in reference)
    # The control: the plan's description, with the switch on, does ask the
    # ranked search, and the hidden reference file reaches the plan only there.
    plan = PlanningRunDriver._build_target_repo_descriptor(
        door.TARGET_REPO,
        str(repo),
        "",
        reader=reader,
        request_text=REQUEST,
        ranked_source=True,
        reference_paths=("reference/",),
    )
    assert reader.ranked_asks
    assert [b["path"] for b in plan["ranked_source"]["blocks"]] == [
        "reference/prototype/hidden.view"
    ]
