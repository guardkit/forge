"""The merge card says when the merge will use main's copy of a judge file.

Design: "The merge check uses main's copy of a check script the build changed"
(15 October 2026), Forge items 6 and 7, and review rounds 1 to 5. Tests 9 and
10, the discovery-to-card link tests (round 2), the second-offer regression
(round 3) and the target-renamed judge (round 4).

A judge file is a file a declared install or check command names directly.
The merge reads the declaration on the commit it joins onto and, from this
change, checks the joined code with that commit's copies of the judge files.
The card says so before approval, reading the declaration where the merge
will read it: the preview's base commit, or the target branch fetched afresh.

Nothing here touches a real repository or a real build system: the trees are
dictionaries, and GuardKit's rules are a stand-in with the same names and the
same matching rule. Nothing names a language: the "project" is a few paths.
"""

from __future__ import annotations

import asyncio
import json
import shlex
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

import pytest
import yaml

from forge.cli._serve_gate_activation import merge_card_words
from forge.deploy.candidate_tree import FileAtCommit, RemoteStartPoint
from forge.pipeline.merge_offer import MERGE_OFFER_DETAILS_KEY, MergeOfferService
from forge.pipeline.merge_ready_checkpoint import parse_changed_files
from forge.pipeline.scope_report import ScopeReport
from tests.forge.pipeline.test_merge_offer import (  # noqa: F401 - fixtures
    BUILD_ID,
    CANDIDATE_SHA,
    CANDIDATE_TREE,
    FEATURE_ID,
    _event,
    _insert_build,
    _offer_rows,
    _Recorder,
    config,
    pool,
    repo_root,
)

#: The target branch's commit, fetched afresh from the remote.
TARGET = "a" * 40
#: The clone's own local main, which nothing moves forward.
LOCAL_MAIN = "b" * 40
#: The commit a preview was joined onto.
PREVIEW_BASE = "f" * 40
#: Where the preview keeps its joined commit.
JOINED = "refs/forge/preview/lease-1"
#: The commit the build started from.
START = "9" * 40

CHECK_AND_INSTALL = (
    "toolchain:\n  test: bash scripts/check.sh\n  install: bash scripts/install.sh\n"
)

#: Words a sentence about what the merge WILL do must never carry: they
#: would claim something ran somewhere else (review round 1, R2).
EXECUTION_CLAIMS = ("ran", "were run", "during the build", "in the build", "in the preview")


# ---------------------------------------------------------------------------
# GuardKit's rules, stood in for
# ---------------------------------------------------------------------------


def _match_keys(word: str) -> list[str]:
    whole = [word]
    if "=" in word:
        whole.append(word.split("=", 1)[1])
    keys = list(whole)
    for key in whole:
        for sep in (",", ":"):
            if sep in key:
                keys.extend(piece for piece in key.replace(":", ",").split(",") if piece)
                break
    out = []
    for key in keys:
        while key.startswith("./"):
            key = key[2:]
        if key:
            out.append(key)
    return out


class StandInGuardKit:
    """``read_merge_declarations``, ``pin_merge_environment``, ``judge_words``,
    ``judge_files`` and ``links_on_judge_paths``, with GuardKit's matching rule,
    and a record of every call so a test can prove Forge asked GuardKit."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def read_merge_declarations(self, tree_root: Path) -> tuple[Any, str | None]:
        self.calls.append(("read_merge_declarations", Path(tree_root)))
        path = Path(tree_root) / ".guardkit" / "config.yaml"
        if not path.is_file():
            return SimpleNamespace(declared_test=None, toolchain=None), None
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if "broken" in data:
            return None, ".guardkit/config.yaml is not valid"
        block = data.get("toolchain") or {}
        return (
            SimpleNamespace(
                declared_test=block.get("test"),
                toolchain=SimpleNamespace(install=block.get("install") or ""),
            ),
            None,
        )

    def judge_words(self, command: str, *, through_shell: bool) -> tuple[Any, str | None]:
        self.calls.append(("judge_words", command))
        return tuple(shlex.split(command)), None

    def pin_merge_environment(
        self, declarations: Any, test_command: str | None, verify_timeout: int
    ) -> tuple[Any, str | None]:
        self.calls.append(("pin_merge_environment", test_command))
        words: list[str] = []
        if test_command:
            words.extend(shlex.split(test_command))
        install = getattr(declarations.toolchain, "install", "") if declarations.toolchain else ""
        if install:
            words.extend(shlex.split(install))
        return SimpleNamespace(judge_words=tuple(words)), None

    def judge_files(self, changed: Sequence[str], words: Sequence[str]) -> list[str]:
        self.calls.append(("judge_files", tuple(changed)))
        keys = {key for word in words for key in _match_keys(word)}
        hits = []
        for path in changed:
            name = path.rsplit("/", 1)[-1]
            if any(
                k == path or k.endswith("/" + path) or k == name or k.endswith("/" + name)
                for k in keys
            ):
                hits.append(path)
        return hits

    def links_on_judge_paths(self, entries: Mapping[str, str], words: Sequence[str]) -> list[str]:
        """GuardKit's link rule: a link the judge-file rule names, or a link
        that is a leading folder of a word that names a path."""
        self.calls.append(("links_on_judge_paths", dict(entries)))
        keys = {key for word in words for key in _match_keys(word)}
        links = sorted(path for path, mode in entries.items() if mode == "120000")
        named = set(self.judge_files(links, words))
        for path in links:
            if any(key.startswith(path + "/") or ("/" + path + "/") in key for key in keys):
                named.add(path)
        return sorted(named)


@pytest.fixture(params=["stand-in rules", "GuardKit's own rules"])
def rules(request: pytest.FixtureRequest) -> Any:
    """The rules the card's reading is judged by: the stand-in, and GuardKit's
    own when GuardKit is importable here (skipped otherwise), so the card is
    proved against the real link and judge rule too."""
    if request.param == "stand-in rules":
        return StandInGuardKit()
    from forge.pipeline.merge_check_reading import guardkit_rules

    real = guardkit_rules()
    if real is None:
        pytest.skip("GuardKit is not importable in this interpreter")
    return real


# ---------------------------------------------------------------------------
# Trees at commits, read the way the commit reader reads them
# ---------------------------------------------------------------------------


class Trees:
    """``read_file_at_commit`` over dictionaries of ``path -> (mode, text)``.

    A folder is any path with files below it (mode ``040000``, not found). A
    link answers its target name with mode ``120000``; asked for an ordinary
    file only, a link is refused. ``refuse`` names commits whose reads are
    refused. ``hold`` suspends every read until it is set."""

    def __init__(self, trees: Mapping[str, Mapping[str, tuple[str, str]]]) -> None:
        self.trees = {commit: dict(files) for commit, files in trees.items()}
        self.refuse: set[str] = set()
        self.refuse_modes_at: set[str] = set()
        self.calls: list[tuple[str, str, bool]] = []
        self.hold: asyncio.Event | None = None
        self.entered = asyncio.Event()

    async def read_file_at_commit(
        self,
        repo_path: str,
        commit: str,
        file_path: str,
        *,
        ordinary_file_only: bool = False,
        raw: bool = False,
        mode_only: bool = False,
    ) -> FileAtCommit:
        self.calls.append((commit, file_path, mode_only))
        self.entered.set()
        if self.hold is not None:
            await self.hold.wait()
        if commit in self.refuse or (mode_only and commit in self.refuse_modes_at):
            return FileAtCommit(refusal=f"the commit {commit[:10]} could not be read")
        if commit not in self.trees:
            return FileAtCommit(refusal=f"the copy does not have {commit[:10]}")
        files = self.trees[commit]
        if file_path in files:
            mode, text = files[file_path]
            if mode == "120000" and ordinary_file_only and not (raw or mode_only):
                return FileAtCommit(refusal=f"{file_path} is a symbolic link")
            return FileAtCommit(
                content="" if mode_only else text,
                found=True,
                ordinary=mode != "120000",
                mode=mode if (raw or mode_only) else None,
            )
        if any(p.startswith(file_path + "/") for p in files):
            return FileAtCommit(found=False, mode="040000" if (raw or mode_only) else None)
        return FileAtCommit(found=False)


class TargetGit:
    """The git surface the offer holds: the candidate's pins, a stale local
    main, and the target branch fetched afresh."""

    async def rev_parse(self, ref: str) -> str | None:
        if ref.endswith("^{tree}"):
            return CANDIDATE_TREE
        if ref in ("main", "refs/heads/main"):
            return LOCAL_MAIN
        return CANDIDATE_SHA

    async def fetch_remote_start_point(self, *_a: Any, **_k: Any) -> RemoteStartPoint:
        return RemoteStartPoint(branch="main", commit=TARGET)


class _Evidence:
    """A review preview's evidence, as much of it as the offer reads."""

    def __init__(self, state: str, base: str | None) -> None:
        self.state = state
        self.preview_tree = "e" * 40
        self.preview_base_sha = base
        self.run_id = "run-1"
        self.failure_reason = "the journey did not finish"

    def model_dump(self, mode: str = "json") -> dict[str, Any]:
        return {"state": self.state, "preview_tree": self.preview_tree, "run_id": self.run_id}


class _Preview:
    """The review-preview service, stood in for: it joins onto the base the
    offer fetched, and keeps the joined commit under a ref."""

    def __init__(self, state: str = "ready") -> None:
        self.state = state
        self.prepared: list[Any] = []
        self.released: list[Any] = []

    def enabled_for(self, project: str) -> bool:
        return True

    async def prepare(self, req: Any) -> _Evidence:
        self.prepared.append(req)
        return _Evidence(self.state, PREVIEW_BASE)

    async def release(self, **kwargs: Any) -> None:
        self.released.append(kwargs)

    def joined_ref_for(self, evidence: Any) -> str | None:
        return JOINED if getattr(evidence, "state", None) == "ready" else None


class _PreviewGit(TargetGit):
    """With a preview, the remote is at the preview's base."""

    async def fetch_remote_start_point(self, *_a: Any, **_k: Any) -> RemoteStartPoint:
        return RemoteStartPoint(branch="main", commit=PREVIEW_BASE)


# ---------------------------------------------------------------------------
# Driving the two cards
# ---------------------------------------------------------------------------


def _scope(name_status: str) -> ScopeReport:
    report = ScopeReport(read=True)
    report.changes = parse_changed_files(name_status)
    report.files_changed = len(report.changes)
    return report


def _record_start(pool: Any, build_id: str = BUILD_ID) -> None:
    pool.connection.execute(
        "UPDATE builds SET start_commit = ?, target_branch = 'main' WHERE build_id = ?",
        (START, build_id),
    )
    pool.connection.commit()


def _service(
    config: Any,
    pool: Any,
    recorder: _Recorder,
    *,
    trees: Trees | None,
    rules: StandInGuardKit | None,
    name_status: str = "",
    git: Any = None,
    preview: Any = None,
) -> MergeOfferService:
    async def _git_head(_root: Path, *_a: Any) -> str | None:
        return LOCAL_MAIN

    extra: dict[str, Any] = {}
    if trees is not None:
        extra["commit_reader"] = lambda _repo, _root: trees
    if rules is not None:
        extra["merge_check_rules"] = rules
    if preview is not None:
        extra["review_preview"] = preview
    return MergeOfferService(
        config=config,
        pool=pool,
        pipeline_publisher=SimpleNamespace(publish_build_paused=recorder.publish_build_paused),
        raw_publish=recorder.raw_publish,
        git_head=_git_head,
        git_surface=lambda _repo, _root: git or TargetGit(),
        baseline_reader=lambda _build_id: None,
        finished_feature_reader=lambda *_a, **_k: (None, None, "nothing was exported"),
        scope_pass=lambda **_k: _scope(name_status),
        after_deploy_check_reader=lambda _pool, _row: None,
        **extra,
    )


def _approval_words(recorder: _Recorder) -> str:
    approvals = [body for kind, body in recorder.events if kind == "approval"]
    assert len(approvals) == 1, recorder.events
    _subject, raw = approvals[0]
    return json.loads(raw)["payload"]["action_description"]


def _reading(pool: Any, build_id: str = BUILD_ID) -> dict[str, Any]:
    rows = _offer_rows(pool, build_id)
    assert len(rows) == 1
    return rows[0].details[MERGE_OFFER_DETAILS_KEY]["merge_check_reading"]


async def _routine_card(config, pool, **kwargs: Any) -> tuple[str, dict[str, Any]]:
    _insert_build(pool)
    _record_start(pool)
    recorder = _Recorder()
    await _service(config, pool, recorder, **kwargs).maybe_offer(_event())
    return _approval_words(recorder), _reading(pool)


async def _repair_card(config, pool, **kwargs: Any) -> tuple[str, dict[str, Any]]:
    _insert_build(pool)
    _record_start(pool)
    recorder = _Recorder()
    name_status = kwargs.get("name_status", "")
    await _service(config, pool, recorder, **kwargs).offer(
        build_id=BUILD_ID,
        feature_id=FEATURE_ID,
        card_words=lambda branch, _mb: merge_card_words(feature_id=FEATURE_ID, branch=branch),
        changed_files=parse_changed_files(name_status),
    )
    return _approval_words(recorder), _reading(pool)


CARDS = {"routine card": _routine_card, "repair card": _repair_card}


def _trees(target: dict[str, tuple[str, str]], candidate: dict[str, tuple[str, str]] | None = None) -> Trees:
    return Trees({TARGET: target, CANDIDATE_SHA: candidate if candidate is not None else target, LOCAL_MAIN: target})


def _project(config_text: str = CHECK_AND_INSTALL, **extra: tuple[str, str]) -> dict[str, tuple[str, str]]:
    files = {
        ".guardkit/config.yaml": ("100644", config_text),
        "scripts/check.sh": ("100755", "#!/bin/sh\nrun the checks\n"),
        "scripts/install.sh": ("100755", "#!/bin/sh\ninstall\n"),
        "src/app.txt": ("100644", "the product\n"),
    }
    files.update(extra)
    return files


# ---------------------------------------------------------------------------
# Test 9: each case's sentence, on both cards, with no execution claim
# ---------------------------------------------------------------------------

CASES: dict[str, dict[str, Any]] = {
    "changed check script": {
        "target": _project(),
        "name_status": "M\0scripts/check.sh\0",
        "says": (
            "This build changes scripts/check.sh, which runs the project's checks. "
            "The merge will check the joined code with main's version of that file "
            "and will not run this build's changes to it."
        ),
    },
    "changed install script": {
        "target": _project(),
        "name_status": "M\0scripts/install.sh\0",
        "says": (
            "This build changes scripts/install.sh, which the project's install runs. "
            "The merge will install with main's version of that file and will not run "
            "this build's changes to it."
        ),
    },
    "deleted": {
        "target": _project(),
        "name_status": "D\0scripts/check.sh\0",
        "says": (
            "This build deletes scripts/check.sh, which the project's declared commands "
            "run. The merge uses main's version of it, and after the merge main's "
            "declared commands will name a file that is gone."
        ),
    },
    "added": {
        "target": _project(),
        "name_status": "A\0tools/check.sh\0",
        "says": (
            "This build adds tools/check.sh, which has the same name as a file the "
            "project's declared commands run. The merge will run without it."
        ),
    },
    "named install list": {
        "target": _project(
            "toolchain:\n  test: bash scripts/check.sh\n  install: pip install -r requirements.txt\n",
            **{"requirements.txt": ("100644", "a-library\n")},
        ),
        "name_status": "M\0requirements.txt\0",
        "says": (
            "This build changes requirements.txt, which the project's install names. "
            "The merge will install with main's version of it, so a dependency this "
            "build adds will be missing in the merge check."
        ),
    },
    "link": {
        "target": _project(**{"scripts/check.sh": ("120000", "../tools/real-check.sh")}),
        "name_status": "M\0src/app.txt\0",
        "says": (
            "scripts/check.sh is a symbolic link, so the merge will not be able to "
            "check this build. Approving will join it but publish nothing."
        ),
    },
    "settings": {
        "target": _project(),
        "name_status": "M\0.guardkit/config.yaml\0",
        "says": (
            "This build also changes .guardkit/config.yaml, where the project declares "
            "its commands. The merge uses main's declarations."
        ),
    },
}


@pytest.mark.parametrize("card", sorted(CARDS))
@pytest.mark.parametrize("case", sorted(CASES))
@pytest.mark.asyncio
async def test_9_each_case_puts_its_one_sentence_on_both_cards(
    config, pool, rules, card: str, case: str
) -> None:
    spec = CASES[case]
    words, reading = await CARDS[card](
        config,
        pool,
        trees=_trees(spec["target"]),
        rules=rules,
        name_status=spec["name_status"],
    )

    assert spec["says"] in words
    line = next(line for line in words.splitlines() if spec["says"] in line)
    for claim in EXECUTION_CLAIMS:
        assert f" {claim} " not in f" {line.lower()} ", (claim, line)
    assert reading["declared_at"] == TARGET
    assert reading["read"] is True


@pytest.mark.parametrize("card", sorted(CARDS))
@pytest.mark.asyncio
async def test_9_the_not_readable_case_says_so_with_the_commit(config, pool, rules, card: str) -> None:
    trees = _trees(_project())
    trees.refuse.add(TARGET)

    words, reading = await CARDS[card](
        config, pool, trees=trees, rules=rules, name_status="M\0scripts/check.sh\0"
    )

    assert (
        f"Which files run the project's checks on main ({TARGET[:10]}) could not be "
        "read here, so this card cannot say whether the merge will use main's "
        "versions of any of them."
    ) in words
    assert reading["read"] is False
    assert reading["reason"]


@pytest.mark.parametrize("card", sorted(CARDS))
@pytest.mark.asyncio
async def test_9_with_no_judge_file_the_card_is_byte_for_byte_as_before(
    config, pool, rules, tmp_path: Path, card: str
) -> None:
    with_reading, reading = await CARDS[card](
        config, pool, trees=_trees(_project()), rules=rules,
        name_status="M\0src/app.txt\0",
    )
    assert reading["judge_files"] == []

    # The same card from a service that makes no reading at all.
    from forge.adapters.sqlite import connect as sqlite_connect
    from forge.lifecycle import migrations
    from forge.lifecycle.persistence import SqliteLifecyclePersistence

    cx = sqlite_connect.connect_writer(tmp_path / "other.db")
    migrations.apply_at_boot(cx)
    other = SqliteLifecyclePersistence(connection=cx)
    _insert_build(other)
    _record_start(other)
    recorder = _Recorder()
    service = _service(config, other, recorder, trees=None, rules=None, name_status="M\0src/app.txt\0")
    if card == "routine card":
        await service.maybe_offer(_event())
    else:
        await service.offer(
            build_id=BUILD_ID,
            feature_id=FEATURE_ID,
            card_words=lambda branch, _mb: merge_card_words(feature_id=FEATURE_ID, branch=branch),
        )
    assert _approval_words(recorder) == with_reading


# ---------------------------------------------------------------------------
# Test 10: GuardKit's rule, the commit it reads at, and the record
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_10_the_reading_uses_guardkits_rule(config, pool) -> None:
    rules = StandInGuardKit()
    _words, reading = await _routine_card(
        config, pool, trees=_trees(_project()), rules=rules, name_status="M\0scripts/check.sh\0"
    )

    assert [j["path"] for j in reading["judge_files"]] == ["scripts/check.sh"]
    assert reading["judge_files"][0]["how"] == "changed"
    asked = [name for name, _ in rules.calls]
    for name in ("read_merge_declarations", "pin_merge_environment", "judge_files", "links_on_judge_paths"):
        assert name in asked, name
    assert ("judge_files", ("scripts/check.sh",)) in rules.calls


@pytest.mark.asyncio
async def test_10_a_stale_local_main_is_never_read(config, pool, rules) -> None:
    trees = Trees(
        {
            LOCAL_MAIN: _project("toolchain:\n  test: bash scripts/check.sh\n"),
            TARGET: _project(
                "toolchain:\n  test: bash qa/run.sh\n",
                **{"qa/run.sh": ("100755", "#!/bin/sh\nrun\n")},
            ),
            CANDIDATE_SHA: _project(
                "toolchain:\n  test: bash qa/run.sh\n",
                **{"qa/run.sh": ("100755", "#!/bin/sh\nrun more\n")},
            ),
        }
    )

    words, reading = await _routine_card(
        config, pool, trees=trees, rules=rules, name_status="M\0qa/run.sh\0"
    )

    assert "This build changes qa/run.sh" in words
    assert "scripts/check.sh" not in words
    assert reading["declared_at"] == TARGET
    commits = {commit for commit, _path, _mode in trees.calls}
    assert LOCAL_MAIN not in commits
    assert "main" not in commits and "refs/heads/main" not in commits
    assert (TARGET, ".guardkit/config.yaml", False) in trees.calls


@pytest.mark.asyncio
async def test_10_with_a_preview_it_reads_at_the_previews_base(config, pool, rules) -> None:
    project = _project()
    trees = Trees({PREVIEW_BASE: project, CANDIDATE_SHA: project, JOINED: project})

    _words, reading = await _routine_card(
        config, pool, trees=trees, rules=rules,
        name_status="M\0scripts/check.sh\0", git=_PreviewGit(), preview=_Preview(),
    )

    assert reading["declared_at"] == PREVIEW_BASE
    assert {commit for commit, _p, _m in trees.calls} <= {PREVIEW_BASE, CANDIDATE_SHA, JOINED}
    assert any(commit == JOINED for commit, _p, _m in trees.calls)


@pytest.mark.asyncio
async def test_10_guardkit_not_importable_is_said_plainly(config, pool, monkeypatch) -> None:
    import forge.pipeline.merge_check_reading as reading_module

    monkeypatch.setattr(reading_module, "guardkit_rules", lambda: None)
    words, reading = await _routine_card(
        config, pool, trees=_trees(_project()), rules=None, name_status="M\0scripts/check.sh\0"
    )

    assert reading["read"] is False
    assert "could not be read here" in words


# ---------------------------------------------------------------------------
# Round 2: links found on their own, in all three trees, check and install
# ---------------------------------------------------------------------------

LINK_SENTENCE = "is a symbolic link, so the merge will not be able to check this build."


@pytest.mark.parametrize("judge", ["scripts/check.sh", "scripts/install.sh"])
@pytest.mark.asyncio
async def test_an_unchanged_judge_link_whose_referent_changed_is_shown(config, pool, rules, judge) -> None:
    target = _project(**{judge: ("120000", "../tools/real.sh"), "tools/real.sh": ("100755", "real\n")})
    candidate = dict(target, **{"tools/real.sh": ("100755", "exit 0\n")})

    words, reading = await _routine_card(
        config, pool, trees=_trees(target, candidate), rules=rules,
        name_status="M\0tools/real.sh\0",
    )

    assert f"{judge} {LINK_SENTENCE}" in words
    assert {"path": judge, "how": "link"}.items() <= next(
        j for j in reading["judge_files"] if j["path"] == judge
    ).items()


@pytest.mark.parametrize("judge", ["scripts/check.sh", "scripts/install.sh"])
@pytest.mark.asyncio
async def test_a_judge_under_a_linked_folder_is_shown(config, pool, rules, judge) -> None:
    target = {
        ".guardkit/config.yaml": ("100644", CHECK_AND_INSTALL),
        "scripts": ("120000", "tools"),
        "tools/check.sh": ("100755", "check\n"),
        "tools/install.sh": ("100755", "install\n"),
    }

    words, _reading = await _routine_card(
        config, pool, trees=_trees(target), rules=rules, name_status="M\0src/x.txt\0"
    )

    assert f"scripts {LINK_SENTENCE}" in words


@pytest.mark.parametrize("judge", ["scripts/check.sh", "scripts/install.sh"])
@pytest.mark.asyncio
async def test_a_link_the_build_created_is_shown(config, pool, rules, judge) -> None:
    target = _project()
    candidate = dict(target, **{judge: ("120000", "../tools/weak.sh")})

    words, _reading = await _routine_card(
        config, pool, trees=_trees(target, candidate), rules=rules,
        name_status=f"T\0{judge}\0",
    )

    assert f"{judge} {LINK_SENTENCE}" in words


@pytest.mark.asyncio
async def test_unreadable_link_evidence_is_said_plainly(config, pool, rules) -> None:
    trees = _trees(_project())
    trees.refuse_modes_at.add(CANDIDATE_SHA)

    words, reading = await _routine_card(
        config, pool, trees=trees, rules=rules, name_status="M\0src/app.txt\0"
    )

    assert (
        f"The factory could not read whether scripts/check.sh is a symbolic link at "
        f"{CANDIDATE_SHA[:10]}, so the merge will not be able to check this build."
    ) in words
    assert reading["link_unreadable"]


# ---------------------------------------------------------------------------
# Round 4: a judge the target renamed, carrying the build's edits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_judge_the_target_renamed_is_listed_when_the_joined_version_differs(config, pool, rules) -> None:
    renamed = "toolchain:\n  test: bash scripts/verify.sh\n"
    base = _project(renamed, **{"scripts/verify.sh": ("100755", "main's checks\n")})
    del base["scripts/check.sh"]
    joined = dict(base, **{"scripts/verify.sh": ("100755", "main's checks, weakened\n")})
    candidate = _project(renamed)
    trees = Trees({PREVIEW_BASE: base, CANDIDATE_SHA: candidate, JOINED: joined})

    words, reading = await _routine_card(
        config, pool, trees=trees, rules=rules,
        name_status="M\0scripts/check.sh\0", git=_PreviewGit(), preview=_Preview(),
    )

    assert "This build changes scripts/verify.sh, which runs the project's checks." in words
    assert [j["path"] for j in reading["judge_files"]] == ["scripts/verify.sh"]


# ---------------------------------------------------------------------------
# Round 3: one offer per build while the new reads are in flight
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("with_preview", [False, True], ids=["no preview", "preview"])
@pytest.mark.asyncio
async def test_a_second_offer_while_the_reader_is_suspended_makes_nothing(
    config, pool, rules, with_preview: bool
) -> None:
    project = _project()
    trees = Trees({TARGET: project, PREVIEW_BASE: project, CANDIDATE_SHA: project, JOINED: project})
    trees.hold = asyncio.Event()
    preview = _Preview() if with_preview else None
    _insert_build(pool)
    _record_start(pool)
    recorder = _Recorder()
    service = _service(
        config, pool, recorder, trees=trees, rules=rules,
        name_status="M\0scripts/check.sh\0",
        git=_PreviewGit() if with_preview else TargetGit(), preview=preview,
    )

    first = asyncio.create_task(service.maybe_offer(_event()))
    await asyncio.wait_for(trees.entered.wait(), timeout=5)
    second = await service.offer(
        build_id=BUILD_ID,
        feature_id=FEATURE_ID,
        card_words=lambda branch, _mb: merge_card_words(feature_id=FEATURE_ID, branch=branch),
    )
    trees.hold.set()
    await asyncio.wait_for(first, timeout=5)

    assert second is False
    assert len(_offer_rows(pool, BUILD_ID)) == 1
    assert [kind for kind, _ in recorder.events].count("approval") == 1
    if preview is not None:
        assert len(preview.prepared) == 1


# ---------------------------------------------------------------------------
# The preview service names the ref its joined commit is kept under
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state, tree, expected",
    [
        ("ready", "e" * 40, "refs/forge/preview/lease-7"),
        ("ended", "e" * 40, None),
        ("ready", None, None),
    ],
    ids=["live with a joined tree", "ended", "no joined tree"],
)
def test_the_preview_names_its_joined_ref_only_while_it_has_one(
    tmp_path: Path, state: str, tree: str | None, expected: str | None
) -> None:
    from datetime import datetime, timezone

    from forge.adapters.sqlite import connect as sqlite_connect
    from forge.deploy.review_preview import (
        PreviewLease,
        PreviewLeaseStore,
        ReviewPreviewService,
    )
    from forge.lifecycle import migrations

    cx = sqlite_connect.connect_writer(tmp_path / "leases.db")
    migrations.apply_at_boot(cx)
    store = PreviewLeaseStore(cx)
    now = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)
    store.insert(
        PreviewLease(
            lease_id="lease-7", slot=None, port=None, token="t", project="p",
            feature_id=FEATURE_ID, build_id=BUILD_ID, candidate_sha=CANDIDATE_SHA,
            candidate_tree=CANDIDATE_TREE, preview_tree=tree, run_id="run-7",
            compose_project=None, evidence_dir=None, state=state, reason=None,
            created_at=now.isoformat(), app_expires_at=now.isoformat(),
            evidence_expires_at=now.isoformat(),
        ),
        now=now,
    )
    service = SimpleNamespace(_store=store)

    found = ReviewPreviewService.joined_ref_for(service, SimpleNamespace(run_id="run-7"))  # type: ignore[arg-type]

    assert found == expected
