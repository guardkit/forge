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
    REPO,
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

#: The project protects its scripts (28 October 2026: only a judge file under
#: the protected paths is checked with main's copy; see
#: ``test_a_judge_outside_the_protected_paths_runs_the_builds_copy``).
PROTECTS_SCRIPTS = (
    "autobuild:\n  player:\n    protected_paths: [scripts, tools, docs]\n"
)
CHECK_AND_INSTALL = PROTECTS_SCRIPTS + (
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
        player = (data.get("autobuild") or {}).get("player") or {}
        return (
            SimpleNamespace(
                declared_test=block.get("test"),
                toolchain=SimpleNamespace(install=block.get("install") or ""),
                protected_paths=tuple(player.get("protected_paths") or ()),
            ),
            None,
        )

    def judges_kept_from_target(
        self, paths: Sequence[str], protected_paths: Sequence[str]
    ) -> list[str]:
        roots = [p.rstrip("/") for p in protected_paths if p.rstrip("/")]
        return [
            path for path in paths
            if any(path == root or path.startswith(root + "/") for root in roots)
        ]

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
        self.refuse_listing_at: set[str] = set()
        self.calls: list[tuple[str, str, bool]] = []
        self.listed: list[str] = []
        self.hold: asyncio.Event | None = None
        self.entered = asyncio.Event()

    async def list_tree_at_commit(self, repo_path: str, commit: str) -> Any:
        """``git ls-tree -r -z --full-tree``: every leaf with its mode, type and
        object id; no content."""
        import hashlib

        from forge.deploy.candidate_tree import TreeEntry, TreeListing

        self.listed.append(commit)
        self.entered.set()
        if self.hold is not None:
            await self.hold.wait()
        if commit in self.refuse or commit in self.refuse_listing_at:
            return TreeListing(refusal=f"the commit {commit[:10]} could not be listed")
        if commit not in self.trees:
            return TreeListing(refusal=f"the copy does not have {commit[:10]}")
        return TreeListing(
            entries=tuple(
                TreeEntry(
                    path=path,
                    mode=mode,
                    kind="blob",
                    object=hashlib.sha1(text.encode()).hexdigest(),
                )
                for path, (mode, text) in sorted(self.trees[commit].items())
            )
        )

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
        if commit in self.refuse:
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
    # The build's start commit is in the copy too: the card reads the
    # project's settings there for its prototype line (17 October 2026), and
    # a start commit the copy cannot read is said on the card.
    return Trees(
        {
            TARGET: target,
            CANDIDATE_SHA: candidate if candidate is not None else target,
            LOCAL_MAIN: target,
            START: target,
        }
    )


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
            "autobuild:\n  player:\n    protected_paths: [scripts, requirements.txt]\n"
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
    commits = {commit for commit, _path, _mode in trees.calls} | set(trees.listed)
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
    # The card's prototype line (17 October 2026) reads only the settings,
    # at the build's start commit; that read is the card's, not the check's.
    assert {p for c, p, _m in trees.calls if c == START} <= {".guardkit/config.yaml"}
    seen = {commit for commit, _p, _m in trees.calls if commit != START} | set(trees.listed)
    assert seen <= {PREVIEW_BASE, CANDIDATE_SHA, JOINED}
    assert JOINED in trees.listed


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
async def test_a_listing_that_cannot_be_read_is_said_plainly(config, pool, rules) -> None:
    trees = _trees(_project())
    trees.refuse_listing_at.add(CANDIDATE_SHA)

    words, reading = await _routine_card(
        config, pool, trees=trees, rules=rules, name_status="M\0src/app.txt\0"
    )

    assert words.splitlines()[-1] == (
        f"The files at {CANDIDATE_SHA[:10]} could not be listed here, so this card "
        "cannot say whether a file the project's declared commands run is a "
        "symbolic link there."
    )
    assert "will not be able to check" not in words
    assert reading["listing_unreadable"] == [CANDIDATE_SHA]


# ---------------------------------------------------------------------------
# Round 4: a judge the target renamed, carrying the build's edits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_judge_the_target_renamed_is_listed_when_the_joined_version_differs(config, pool, rules) -> None:
    renamed = PROTECTS_SCRIPTS + "toolchain:\n  test: bash scripts/verify.sh\n"
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


# ---------------------------------------------------------------------------
# Coach review 1: the card uses GuardKit's exact rule on whole-tree listings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_path_is_ever_looked_up_on_its_own(config, pool, rules) -> None:
    trees = _trees(_project())

    await _routine_card(config, pool, trees=trees, rules=rules, name_status="M\0scripts/check.sh\0")

    # Only the declaration is read; every link decision comes from listings.
    # (The card's prototype line reads the settings at the build's start
    # commit, 17 October 2026; that read is the card's, not the check's.)
    assert {p for c, p, _m in trees.calls if c == START} <= {".guardkit/config.yaml"}
    assert [path for c, path, _m in trees.calls if c != START] == [".guardkit/config.yaml"]
    assert sorted(set(trees.listed)) == sorted({TARGET, CANDIDATE_SHA})


@pytest.mark.asyncio
async def test_links_are_found_when_the_changes_cannot_be_read(config, pool, rules) -> None:
    target = _project(**{"scripts/check.sh": ("120000", "../tools/real.sh")})
    _insert_build(pool)
    _record_start(pool)
    recorder = _Recorder()
    service = _service(config, pool, recorder, trees=_trees(target), rules=rules)
    service._scope_pass = lambda **_k: ScopeReport(read=False)  # noqa: SLF001

    await service.maybe_offer(_event())
    words = _approval_words(recorder)

    assert f"scripts/check.sh {LINK_SENTENCE}" in words
    assert (
        "Which files this build changed could not be read here, so this card cannot "
        "say whether it changes a file the project's declared commands run."
    ) in words
    assert "Which files run the project's checks on main" not in words


@pytest.mark.asyncio
async def test_an_install_script_that_is_not_executable_still_runs(config, pool, rules) -> None:
    target = _project(**{"scripts/install.sh": ("100644", "install\n")})

    words, _reading = await _routine_card(
        config, pool, trees=_trees(target), rules=rules, name_status="M\0scripts/install.sh\0"
    )

    assert "This build changes scripts/install.sh, which the project's install runs." in words


@pytest.mark.asyncio
async def test_a_judge_matched_only_by_name_is_described_as_named_like_one(config, pool, rules) -> None:
    target = _project(**{"docs/check.sh": ("100644", "notes\n")})

    words, _reading = await _routine_card(
        config, pool, trees=_trees(target), rules=rules, name_status="M\0docs/check.sh\0"
    )

    assert (
        "This build changes docs/check.sh, a file named like one the project's "
        "checks run. The merge will check the joined code with main's version of "
        "that file and will not run this build's changes to it."
    ) in words
    assert "docs/check.sh, which runs" not in words


# ---------------------------------------------------------------------------
# Real git, the real commit reader, and (when importable) GuardKit's own rules
# ---------------------------------------------------------------------------


def _git_run(root: Path, *args: str) -> str:
    import subprocess

    done = subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
        cwd=str(root), capture_output=True, text=True, check=True,
    )
    return done.stdout.strip()


class RealProject:
    """A real repository at the configured path: main, the build's branch, and
    optionally the preview's joined commit kept under its ref."""

    def __init__(self, root: Path) -> None:
        import os

        self.root = root
        self.os = os
        _git_run(root, "init", "-q", "-b", "main")

    def write(self, files: Mapping[str, Any]) -> None:
        for path, value in files.items():
            where = self.root / path
            if where.is_symlink() or where.exists():
                where.unlink()
            if value is None:
                continue
            where.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(value, tuple) and value[0] == "link":
                self.os.symlink(value[1], where)
            elif isinstance(value, bytes):
                where.write_bytes(value)
            else:
                where.write_text(value, encoding="utf-8")

    def commit(self, files: Mapping[str, Any], message: str) -> str:
        self.write(files)
        _git_run(self.root, "add", "-A")
        _git_run(self.root, "commit", "-q", "--allow-empty", "-m", message)
        return _git_run(self.root, "rev-parse", "HEAD")

    def build(self, main: Mapping[str, Any], changes: Mapping[str, Any]) -> None:
        self.main = self.commit(main, "main")
        _git_run(self.root, "checkout", "-q", "-b", f"autobuild/{FEATURE_ID}")
        self.candidate = self.commit(changes, "the build")
        self.name_status = _git_run(
            self.root, "diff", "--name-status", "-z", "--no-renames", self.main, self.candidate
        ) + "\0"
        _git_run(self.root, "checkout", "-q", "main")

    def join(self) -> str:
        tree = _git_run(self.root, "merge-tree", "--write-tree", self.main, self.candidate).splitlines()[0]
        joined = _git_run(
            self.root, "commit-tree", tree, "-p", self.main, "-p", self.candidate, "-m", "preview"
        )
        _git_run(self.root, "update-ref", JOINED, joined)
        return joined


class RealGit:
    def __init__(self, project: RealProject) -> None:
        self.project = project

    async def rev_parse(self, ref: str) -> str | None:
        return _git_run(self.project.root, "rev-parse", ref) or None

    async def fetch_remote_start_point(self, *_a: Any, **_k: Any) -> RemoteStartPoint:
        return RemoteStartPoint(branch="main", commit=self.project.main)


class _RealPreview(_Preview):
    def __init__(self, project: RealProject) -> None:
        super().__init__()
        self.project = project

    async def prepare(self, req: Any) -> _Evidence:
        self.prepared.append(req)
        self.project.join()
        return _Evidence(self.state, self.project.main)


async def _real_card(
    config, pool, rules, project: RealProject, *, preview: bool = False, changes_read: bool = True
) -> tuple[str, dict[str, Any]]:
    from forge.adapters.git.planning_runner import WorktreeGitRunner

    _insert_build(pool)
    _record_start(pool)
    # The build started from the real main commit, which the copy holds.
    pool.connection.execute(
        "UPDATE builds SET start_commit = ? WHERE build_id = ?", (project.main, BUILD_ID)
    )
    pool.connection.commit()
    recorder = _Recorder()
    reader = WorktreeGitRunner(target_repo_paths={REPO: str(project.root)})
    service = _service(
        config, pool, recorder, trees=None, rules=rules,
        name_status=project.name_status, git=RealGit(project),
        preview=_RealPreview(project) if preview else None,
    )
    service._commit_reader = lambda _repo, _root: reader  # noqa: SLF001
    if not changes_read:
        service._scope_pass = lambda **_k: ScopeReport(read=False)  # noqa: SLF001
    await service.maybe_offer(_event())
    return _approval_words(recorder), _reading(pool)


MAIN_FILES: dict[str, Any] = {
    ".guardkit/config.yaml": CHECK_AND_INSTALL,
    "scripts/check.sh": "#!/bin/sh\nrun the checks\n",
    "scripts/install.sh": "#!/bin/sh\ninstall\n",
    "src/app.txt": "the product\n",
}


@pytest.fixture()
def real(repo_root: Path) -> RealProject:
    return RealProject(repo_root)


@pytest.mark.asyncio
async def test_real_an_added_link_named_like_a_judge_stops_the_merge(config, pool, rules, real) -> None:
    real.build(MAIN_FILES, {"tools/check.sh": ("link", "../scripts/check.sh")})

    words, _reading = await _real_card(config, pool, rules, real)

    assert f"tools/check.sh {LINK_SENTENCE}" in words
    assert "The merge will run without it." not in words


@pytest.mark.asyncio
async def test_real_an_existing_link_elsewhere_on_main_stops_the_merge(config, pool, rules, real) -> None:
    real.build(
        {**MAIN_FILES, "vendor/x/check.sh": ("link", "../../scripts/check.sh")},
        {"src/app.txt": "changed\n"},
    )

    words, _reading = await _real_card(config, pool, rules, real)

    assert f"vendor/x/check.sh {LINK_SENTENCE}" in words


@pytest.mark.parametrize(
    "test_command",
    ["pytest .", "cd .. && bash scripts/check.sh", "ruff check . && bash scripts/check.sh"],
)
@pytest.mark.asyncio
async def test_real_dot_and_dot_dot_say_nothing_false(config, pool, rules, real, test_command) -> None:
    declared = f"toolchain:\n  test: {test_command}\n"
    real.build({**MAIN_FILES, ".guardkit/config.yaml": declared}, {"src/app.txt": "changed\n"})

    words, reading = await _real_card(config, pool, rules, real)

    assert reading["read"] is True
    assert reading["judge_files"] == []
    assert "symbolic link" not in words
    assert "could not" not in words


@pytest.mark.asyncio
async def test_real_a_binary_judge_with_a_preview_says_nothing_false(config, pool, rules, real) -> None:
    declared = "toolchain:\n  test: java -jar tools/checker.jar\n"
    real.build(
        {**MAIN_FILES, ".guardkit/config.yaml": declared, "tools/checker.jar": bytes(range(256)) * 4},
        {"src/app.txt": "changed\n"},
    )

    words, reading = await _real_card(config, pool, rules, real, preview=True)

    assert reading["declared_at"] == real.main
    assert reading["judge_files"] == []
    assert "symbolic link" not in words
    assert "could not" not in words


@pytest.mark.asyncio
async def test_real_unreadable_changes_with_mains_judge_a_link(config, pool, rules, real) -> None:
    real.build(
        {**MAIN_FILES, "scripts/check.sh": ("link", "../tools/real.sh"), "tools/real.sh": "real\n"},
        {"tools/real.sh": "exit 0\n"},
    )

    words, _reading = await _real_card(config, pool, rules, real, changes_read=False)

    assert f"scripts/check.sh {LINK_SENTENCE}" in words
    assert "Which files this build changed could not be read here" in words


@pytest.mark.asyncio
async def test_real_a_renamed_judge_is_listed_from_the_listings(config, pool, rules, real) -> None:
    renamed = PROTECTS_SCRIPTS + "toolchain:\n  test: bash scripts/verify.sh\n"
    real.main = real.commit(MAIN_FILES, "main before the rename")
    _git_run(real.root, "checkout", "-q", "-b", f"autobuild/{FEATURE_ID}")
    real.candidate = real.commit({"scripts/check.sh": "#!/bin/sh\nrun the checks\nexit 0\n"}, "weakened")
    _git_run(real.root, "checkout", "-q", "main")
    _git_run(real.root, "mv", "scripts/check.sh", "scripts/verify.sh")
    real.main = real.commit({".guardkit/config.yaml": renamed}, "main renames its check")
    real.name_status = "M\0scripts/check.sh\0"

    words, reading = await _real_card(config, pool, rules, real, preview=True)

    assert "This build changes scripts/verify.sh, which runs the project's checks." in words
    assert [j["path"] for j in reading["judge_files"]] == ["scripts/verify.sh"]


# ---------------------------------------------------------------------------
# Item 4: the production composition hands the offer a working reader
# ---------------------------------------------------------------------------


def test_the_production_composition_wires_a_working_reader(pool, tmp_path, monkeypatch) -> None:
    import tests.forge.test_serve_review_preview as boot
    from forge.adapters.git.planning_runner import WorktreeGitRunner
    from forge.config.models import ForgeConfig
    from forge.planning.sidecar_git_runner import SidecarGitRunner

    repo = tmp_path / "plain"
    repo.mkdir()
    project = RealProject(repo)
    sha = project.commit(MAIN_FILES, "main")
    raw = {
        "pipeline": {
            "build_queue_subject": "pipeline.build-queued.team-a",
            "approved_originators": ["terminal"],
        },
        "permissions": {"filesystem": {"allowlist": ["/work"]}},
        "merge_executor": {"enabled": True},
        "planning": {
            "target_repo_paths": {"o/plain": str(repo), "o/boxed": "/srv/boxed"},
            "sandboxes": {
                "o/boxed": {
                    "name": "boxed",
                    "sidecar_url": "http://127.0.0.1:1",
                    "runner_url": "http://127.0.0.1:2",
                }
            },
        },
    }
    monkeypatch.setattr(boot, "_config", lambda **_k: ForgeConfig.model_validate(raw))

    seen = boot._drive_boot(pool, tmp_path, merge_on=True, preview=None, compose=False, preview_on=False)
    reader_for = seen["offer"][0]["commit_reader"]
    plain = reader_for("o/plain", repo)
    boxed = reader_for("o/boxed", Path("/srv/boxed"))

    assert isinstance(plain, WorktreeGitRunner)
    assert isinstance(boxed, SidecarGitRunner)
    declared = asyncio.run(
        plain.read_file_at_commit(str(repo), sha, ".guardkit/config.yaml", ordinary_file_only=True)
    )
    assert declared.ok and declared.found
    listing = asyncio.run(plain.list_tree_at_commit(str(repo), sha))
    assert listing.ok
    assert ("scripts/check.sh", "100644") in {(e.path, e.mode) for e in listing.entries}
    assert callable(getattr(boxed, "list_tree_at_commit", None))


# ---------------------------------------------------------------------------
# The sandbox's listing route, and the sandbox runner that asks it
# ---------------------------------------------------------------------------


def test_the_sandbox_lists_a_commits_tree_with_modes_and_no_content(tmp_path: Path) -> None:
    from forge.config.models import ForgeConfig
    from forge.deploy.candidate_tree import TreeListing
    from forge.deploy_sidecar.service import process_git_list_tree_at_commit_request
    from forge.planning.sidecar_git_runner import SidecarGitRunner

    repo = tmp_path / "clone"
    repo.mkdir()
    project = RealProject(repo)
    sha = project.commit(
        {**MAIN_FILES, "tools/check.sh": ("link", "../scripts/check.sh")}, "main"
    )
    config = ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": [str(tmp_path)]}},
            "planning": {"target_repo_paths": {"o/boxed": str(repo)}},
        }
    )

    status, body = process_git_list_tree_at_commit_request(
        {"repo": "o/boxed", "commit": sha}, config=config
    )
    assert status == 200
    listing = TreeListing.from_wire(body)
    assert listing.ok
    modes = {e.path: e.mode for e in listing.entries}
    assert modes["tools/check.sh"] == "120000"
    assert modes["scripts/check.sh"] == "100644"
    assert "run the checks" not in json.dumps(body)

    # A commit the clone does not have is an answer with a refusal; a name the
    # route will not pass to git is refused outright.
    status, body = process_git_list_tree_at_commit_request(
        {"repo": "o/boxed", "commit": "0" * 40}, config=config
    )
    assert status == 200 and TreeListing.from_wire(body).refusal
    status, _body = process_git_list_tree_at_commit_request(
        {"repo": "o/boxed", "commit": "--output=x"}, config=config
    )
    assert status == 400

    asked: list[tuple[str, Any]] = []

    def post(url: str, body: Any, timeout: float) -> tuple[int, Any]:
        asked.append((url, body))
        return process_git_list_tree_at_commit_request(body, config=config)

    runner = SidecarGitRunner("http://127.0.0.1:1", repo="o/boxed", post=post)
    listing = asyncio.run(runner.list_tree_at_commit("/not/used", sha))
    assert listing.ok
    assert asked == [("http://127.0.0.1:1/git/list-tree-at-commit", {"repo": "o/boxed", "commit": sha})]


# ---------------------------------------------------------------------------
# Coach review 2: a judge gone from the joined tree, with the changes unread
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_judge_gone_from_the_joined_tree_is_said_to_be_deleted(config, pool, rules) -> None:
    base = _project()
    joined = dict(base)
    del joined["scripts/check.sh"]
    trees = Trees({PREVIEW_BASE: base, CANDIDATE_SHA: joined, JOINED: joined})
    _insert_build(pool)
    _record_start(pool)
    recorder = _Recorder()
    service = _service(
        config, pool, recorder, trees=trees, rules=rules, git=_PreviewGit(), preview=_Preview()
    )
    service._scope_pass = lambda **_k: ScopeReport(read=False)  # noqa: SLF001

    await service.maybe_offer(_event())
    words = _approval_words(recorder)

    assert "This build deletes scripts/check.sh" in words
    assert "This build changes scripts/check.sh" not in words
    assert next(
        j for j in _reading(pool)["judge_files"] if j["path"] == "scripts/check.sh"
    )["how"] == "deleted"


@pytest.mark.asyncio
async def test_real_a_file_name_that_is_not_utf8_does_not_stop_the_card(config, pool, rules, real) -> None:
    import os

    real.main = real.commit(MAIN_FILES, "main")
    odd = os.path.join(os.fsencode(str(real.root)), b"src", b"caf\xe9-notes.txt")
    with open(odd, "wb") as handle:
        handle.write(b"a name that is not UTF-8\n")
    _git_run(real.root, "add", "-A")
    _git_run(real.root, "commit", "-q", "-m", "an odd name on main")
    real.main = _git_run(real.root, "rev-parse", "HEAD")
    _git_run(real.root, "checkout", "-q", "-b", f"autobuild/{FEATURE_ID}")
    real.candidate = real.commit({"scripts/check.sh": "#!/bin/sh\nmore checks\n"}, "the build")
    _git_run(real.root, "checkout", "-q", "main")
    real.name_status = "M\0scripts/check.sh\0"

    words, reading = await _real_card(config, pool, rules, real, preview=True)

    assert reading["read"] is True
    assert "This build changes scripts/check.sh, which runs the project's checks." in words
    assert "could not be listed" not in words


# ---------------------------------------------------------------------------
# Review R2: file names are kept byte-exact, so two names never become one
# ---------------------------------------------------------------------------

#: A link whose folder name is the single byte 0xE9 (not UTF-8) ...
ODD_LINK = b"vendor/\xe9/check.sh"
#: ... and an ordinary file whose folder name is the UTF-8 bytes of U+FFFD,
#: the character a lossy decoding turns 0xE9 into. git lists it second.
LOOKALIKE = b"vendor/\xef\xbf\xbd/check.sh"


def _commit_lookalike_names(project: RealProject) -> None:
    """Main with a link and an ordinary file whose names decode alike when
    decoding is lossy; the build changes only an unrelated file."""
    import os

    project.write(MAIN_FILES)
    root = os.fsencode(str(project.root))
    for name in (ODD_LINK, LOOKALIKE):
        os.makedirs(os.path.dirname(os.path.join(root, name)), exist_ok=True)
    os.symlink(b"../../scripts/check.sh", os.path.join(root, ODD_LINK))
    with open(os.path.join(root, LOOKALIKE), "wb") as handle:
        handle.write(b"#!/bin/sh\nnot a link\n")
    _git_run(project.root, "add", "-A")
    _git_run(project.root, "commit", "-q", "-m", "main")
    project.main = _git_run(project.root, "rev-parse", "HEAD")
    _git_run(project.root, "checkout", "-q", "-b", f"autobuild/{FEATURE_ID}")
    project.candidate = project.commit({"src/app.txt": "changed\n"}, "the build")
    _git_run(project.root, "checkout", "-q", "main")
    project.name_status = "M\0src/app.txt\0"


def _guardkits_links(project: RealProject, commit: str) -> list[str] | None:
    """The links GuardKit's merge would refuse on at ``commit``, by its own
    tree reading and link rule, or ``None`` when GuardKit is not importable."""
    try:
        from guardkit.orchestrator.merge_environment import judge_links
    except Exception:  # noqa: BLE001
        return None
    words = ("bash", "scripts/check.sh", "bash", "scripts/install.sh")
    return judge_links(project.root, commit, words)


def _sidecar_reader(project: RealProject, tmp_path: Path) -> Any:
    """The sandbox runner, answered by the sandbox's own routes through a real
    JSON round trip, exactly as the HTTP transport carries them."""
    from forge.config.models import ForgeConfig
    from forge.deploy_sidecar.service import (
        process_git_list_tree_at_commit_request,
        process_git_read_file_at_commit_request,
    )
    from forge.planning.sidecar_git_runner import SidecarGitRunner

    sidecar_config = ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": [str(tmp_path), str(project.root)]}},
            "planning": {"target_repo_paths": {REPO: str(project.root)}},
        }
    )
    routes = {
        "/git/list-tree-at-commit": process_git_list_tree_at_commit_request,
        "/git/read-file-at-commit": process_git_read_file_at_commit_request,
    }

    def post(url: str, body: Any, timeout: float) -> tuple[int, Any]:
        route = next(r for r in routes if url.endswith(r))
        status, answer = routes[route](json.loads(json.dumps(body)), config=sidecar_config)
        return status, json.loads(json.dumps(answer).encode("utf-8").decode("utf-8"))

    return SidecarGitRunner("http://127.0.0.1:1", repo=REPO, post=post)


@pytest.mark.parametrize("venue", ["local copy", "sandbox route"])
@pytest.mark.asyncio
async def test_real_names_that_decode_alike_stay_two_files(
    config, pool, rules, real, tmp_path, venue
) -> None:
    import os

    _commit_lookalike_names(real)
    reader = None if venue == "local copy" else _sidecar_reader(real, tmp_path)

    if reader is None:
        words, reading = await _real_card(config, pool, rules, real)
    else:
        _insert_build(pool)
        _record_start(pool)
        recorder = _Recorder()
        service = _service(
            config, pool, recorder, trees=None, rules=rules,
            name_status=real.name_status, git=RealGit(real),
        )
        service._commit_reader = lambda _repo, _root: reader  # noqa: SLF001
        await service.maybe_offer(_event())
        words, reading = _approval_words(recorder), _reading(pool)

    from forge.pipeline.merge_check_reading import shown_path

    # The record and the card name the link as a person reads it; the
    # decision was made on the exact name (the listing test below proves the
    # two names arrive apart).
    card_links = [j["path"] for j in reading["judge_files"] if j["how"] == "link"]
    assert card_links == ["vendor/\\xe9/check.sh"]
    assert "vendor/\\xe9/check.sh is a symbolic link" in words
    assert "could not be listed" not in words
    guardkit = _guardkits_links(real, real.main)
    if guardkit is not None:
        assert guardkit == [os.fsdecode(ODD_LINK)]
        assert card_links == [shown_path(path) for path in guardkit]


def test_the_sandbox_route_keeps_names_that_are_not_utf8_through_json(tmp_path: Path) -> None:
    import os

    from forge.config.models import ForgeConfig
    from forge.deploy.candidate_tree import TreeListing, list_tree_at_commit_sync
    from forge.deploy_sidecar.service import process_git_list_tree_at_commit_request

    (tmp_path / "clone").mkdir()
    project = RealProject(tmp_path / "clone")
    _commit_lookalike_names(project)
    sidecar_config = ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": [str(tmp_path)]}},
            "planning": {"target_repo_paths": {"o/boxed": str(project.root)}},
        }
    )

    status, body = process_git_list_tree_at_commit_request(
        {"repo": "o/boxed", "commit": project.main}, config=sidecar_config
    )
    assert status == 200
    carried = TreeListing.from_wire(json.loads(json.dumps(body, ensure_ascii=False).encode("utf-8")))
    local = list_tree_at_commit_sync(project.root, project.main)

    assert carried.ok and local.ok
    assert carried.entries == local.entries
    modes = {e.path: e.mode for e in carried.entries}
    assert modes[os.fsdecode(ODD_LINK)] == "120000"
    assert modes[os.fsdecode(LOOKALIKE)] == "100644"
    assert {os.fsencode(e.path) for e in carried.entries} >= {ODD_LINK, LOOKALIKE}


# ---------------------------------------------------------------------------
# Review R3: what the merge does with each judge comes only from the target's
# own tree, the same listing GuardKit reads
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("card", sorted(CARDS))
@pytest.mark.asyncio
async def test_a_judge_both_branches_added_uses_mains_copy(config, pool, rules, card: str) -> None:
    same = ("100755", "#!/bin/sh\nthe same new check\n")
    target = _project(**{"tools/check.sh": same})

    words, reading = await CARDS[card](
        config, pool, trees=_trees(target), rules=rules, name_status="A\0tools/check.sh\0"
    )

    assert "The merge will run without it." not in words
    assert (
        "This build adds tools/check.sh, which has the same name as a file the "
        "project's declared commands run. Main has its own copy, so the merge will "
        "use main's version of it and will not run this build's copy."
    ) in words
    judge = next(j for j in reading["judge_files"] if j["path"] == "tools/check.sh")
    assert judge["how"] == "added"
    assert judge["on_target"] is True


@pytest.mark.asyncio
async def test_real_a_judge_both_branches_added_uses_mains_copy(config, pool, rules, real) -> None:
    start = real.commit(MAIN_FILES, "start")
    _git_run(real.root, "checkout", "-q", "-b", f"autobuild/{FEATURE_ID}")
    real.candidate = real.commit({"tools/check.sh": "#!/bin/sh\nthe same new check\n"}, "the build")
    real.name_status = _git_run(
        real.root, "diff", "--name-status", "-z", "--no-renames", start, real.candidate
    ) + "\0"
    _git_run(real.root, "checkout", "-q", "main")
    real.main = real.commit(
        {"tools/check.sh": "#!/bin/sh\nthe same new check\n", "src/app.txt": "main moved\n"},
        "main adds the same file",
    )

    words, reading = await _real_card(config, pool, rules, real, preview=True)

    assert "The merge will run without it." not in words
    assert "Main has its own copy, so the merge will use main's version of it" in words
    assert next(j for j in reading["judge_files"] if j["path"] == "tools/check.sh")["on_target"] is True


@pytest.mark.asyncio
async def test_a_changed_judge_main_no_longer_has_is_left_out(config, pool, rules) -> None:
    target = _project()  # no tools/check.sh: main removed it after the build started
    candidate = _project(**{"tools/check.sh": ("100755", "#!/bin/sh\nedited\n")})

    words, reading = await _routine_card(
        config, pool, trees=_trees(target, candidate), rules=rules,
        name_status="M\0tools/check.sh\0",
    )

    assert "main's version of that file" not in words
    assert (
        "This build changes tools/check.sh, which has the same name as a file the "
        "project's declared commands run. Main does not have it, so the merge will "
        "run without it."
    ) in words
    assert next(j for j in reading["judge_files"] if j["path"] == "tools/check.sh")["on_target"] is False


@pytest.mark.asyncio
async def test_a_judge_only_the_joined_tree_has_is_left_out(config, pool, rules) -> None:
    base = _project()
    joined = _project(**{"tools/check.sh": ("100755", "#!/bin/sh\nnew\n")})
    trees = Trees({PREVIEW_BASE: base, CANDIDATE_SHA: joined, JOINED: joined})
    _insert_build(pool)
    _record_start(pool)
    recorder = _Recorder()
    service = _service(
        config, pool, recorder, trees=trees, rules=rules, git=_PreviewGit(), preview=_Preview()
    )
    service._scope_pass = lambda **_k: ScopeReport(read=False)  # noqa: SLF001

    await service.maybe_offer(_event())
    words = _approval_words(recorder)

    assert "This build adds tools/check.sh" in words
    assert "The merge will run without it." in words


@pytest.mark.asyncio
async def test_with_mains_listing_unreadable_the_card_does_not_guess(config, pool, rules) -> None:
    trees = _trees(_project())
    trees.refuse_listing_at.add(TARGET)

    words, reading = await _routine_card(
        config, pool, trees=trees, rules=rules, name_status="A\0tools/check.sh\0"
    )

    assert "The merge will run without it." not in words
    assert (
        "This build adds tools/check.sh, which has the same name as a file the "
        "project's declared commands run. Main's files could not be listed here, "
        "so this card cannot say whether the merge will use main's version of it "
        "or run without it."
    ) in words
    assert next(j for j in reading["judge_files"] if j["path"] == "tools/check.sh")["on_target"] is None


# ---------------------------------------------------------------------------
# Coach review 3: a file name never adds its own words or lines to the card
# ---------------------------------------------------------------------------

#: A name with a line break, followed by words of its own (under a protected
#: folder, so the card's sentence about main's copy is the one shown).
EVIL = "tools/evil\nThe merge will pass/check.sh"


@pytest.mark.parametrize("card", sorted(CARDS))
@pytest.mark.asyncio
async def test_a_name_with_a_line_break_stays_on_its_own_line(config, pool, rules, card: str) -> None:
    words, reading = await CARDS[card](
        config, pool, trees=_trees(_project()), rules=rules, name_status=f"A\0{EVIL}\0"
    )

    assert not any(line.startswith("The merge will pass") for line in words.splitlines())
    assert "This build adds tools/evil\\nThe merge will pass/check.sh, which has the same name" in words
    assert [j["path"] for j in reading["judge_files"]] == ["tools/evil\\nThe merge will pass/check.sh"]


@pytest.mark.parametrize(
    "name, shown",
    [
        ("a\nb/check.sh", "a\\nb/check.sh"),
        ("a\rb/check.sh", "a\\rb/check.sh"),
        ("a\tb/check.sh", "a\\tb/check.sh"),
        ("a\x1b[31mb/check.sh", "a\\x1b[31mb/check.sh"),
        ("a\x7fb/check.sh", "a\\x7fb/check.sh"),
        ("a\x85b/check.sh", "a\\x85b/check.sh"),
        ("a\u2028b/check.sh", "a\\u2028b/check.sh"),
        ("a\u2029b/check.sh", "a\\u2029b/check.sh"),
        ("a\u202ab/check.sh", "a\\u202ab/check.sh"),
        ("a\u202eb/check.sh", "a\\u202eb/check.sh"),
        ("a\u2066b/check.sh", "a\\u2066b/check.sh"),
        ("a\u2069b/check.sh", "a\\u2069b/check.sh"),
        ("a\u200bb/check.sh", "a\\u200bb/check.sh"),
        ("a\u200fb/check.sh", "a\\u200fb/check.sh"),
        ("a\ufeffb/check.sh", "a\\ufeffb/check.sh"),
        ("a\u00adb/check.sh", "a\\u00adb/check.sh"),
        ("caf\udce9/check.sh", "caf\\xe9/check.sh"),
        ("plain/check.sh", "plain/check.sh"),
        ("caf\u00e9/check.sh", "caf\u00e9/check.sh"),
    ],
)
def test_a_shown_name_has_no_control_characters_and_no_raw_bytes(name: str, shown: str) -> None:
    from forge.pipeline.merge_check_reading import shown_path

    assert shown_path(name) == shown
    assert shown_path(shown_path(name)) == shown
    shown_path(name).encode("utf-8")
    try:
        from guardkit.orchestrator.merge_environment import shown_path as guardkits
    except Exception:  # noqa: BLE001 - GuardKit not importable here
        return
    assert shown_path(name) == guardkits(name)


@pytest.mark.parametrize("card", sorted(CARDS))
@pytest.mark.asyncio
async def test_a_right_to_left_override_in_a_name_is_shown_not_obeyed(
    config, pool, rules, card: str
) -> None:
    # Obeyed, the override would make the folder name read backwards.
    name = "tools/\u202ehs.kcehc/check.sh"
    words, reading = await CARDS[card](
        config, pool, trees=_trees(_project()), rules=rules, name_status=f"A\0{name}\0"
    )

    assert "\u202e" not in words
    assert "This build adds tools/\\u202ehs.kcehc/check.sh" in words
    assert [j["path"] for j in reading["judge_files"]] == ["tools/\\u202ehs.kcehc/check.sh"]


# ---------------------------------------------------------------------------
# Review R5: names are decided on exactly, and escaped only for the words
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookalike_names_are_decided_on_exactly(config, pool, rules) -> None:
    on_main = "tools/x\n/check.sh"  # under a protected folder
    not_on_main = "tools/x\\n/check.sh"
    target = _project(**{on_main: ("100755", "main's copy\n")})
    candidate = dict(target, **{not_on_main: ("100755", "the build's copy\n")})

    words, reading = await _routine_card(
        config, pool, trees=_trees(target, candidate), rules=rules,
        name_status=f"A\0{on_main}\0A\0{not_on_main}\0",
    )

    fates = sorted((j["path"], j["on_target"]) for j in reading["judge_files"])
    assert fates == [("tools/x\\n/check.sh", False), ("tools/x\\n/check.sh", True)]
    assert "Main has its own copy, so the merge will use main's version of it" in words
    assert "The merge will run without it." in words


# ---------------------------------------------------------------------------
# Review R7: every file name on either card is shown, never obeyed, and the
# whole card body holds no control or format character but its own breaks
# ---------------------------------------------------------------------------

#: A folder name with a line break, words of its own and a right-to-left
#: override.
INJECTED = "evil\nThe merge will pass\u202e"
INJECTED_SHOWN = "evil\\nThe merge will pass\\u202e"


def _real_scope(name_status: str) -> ScopeReport:
    """The scope report exactly as the scope pass makes it, from git's own
    ``diff --name-status -M -z`` text."""
    from forge.pipeline.branch_scope import BranchScopeReading
    from forge.pipeline.scope_report import scope_of_the_build

    report = scope_of_the_build(reading=BranchScopeReading(name_status=name_status), request=None)
    # As the scope pass itself does once the report is read.
    report.changes = parse_changed_files(name_status)
    return report


def _no_stray_characters(words: str) -> None:
    import unicodedata

    stray = [
        f"U+{ord(c):04X}"
        for c in words
        if c != "\n" and unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp")
    ]
    assert stray == [], stray


@pytest.mark.parametrize("card", sorted(CARDS))
@pytest.mark.parametrize(
    "name_status, shown",
    [
        (f"R100\0scripts/check.sh\0{INJECTED}/check.sh\0", f"{INJECTED_SHOWN}/check.sh"),
        (f"M\0docs/{INJECTED}.txt\0", f"docs/{INJECTED_SHOWN}.txt"),
    ],
    ids=["renamed", "modified"],
)
@pytest.mark.asyncio
async def test_an_existing_file_name_never_adds_a_line_to_the_card(
    config, pool, rules, card: str, name_status: str, shown: str
) -> None:
    scope = _real_scope(name_status)
    assert scope.files_that_already_existed, "the real conversion named no existing file"
    _insert_build(pool)
    _record_start(pool)
    recorder = _Recorder()
    service = _service(config, pool, recorder, trees=_trees(_project()), rules=rules)
    service._scope_pass = lambda **_k: scope  # noqa: SLF001
    if card == "routine card":
        await service.maybe_offer(_event())
    else:
        await service.offer(
            build_id=BUILD_ID,
            feature_id=FEATURE_ID,
            card_words=lambda branch, _mb: merge_card_words(
                feature_id=FEATURE_ID, branch=branch, scope=scope
            ),
            changed_files=scope.changes,
        )
    words = _approval_words(recorder)

    assert "This branch changes 1 file that already existed: " + shown in words
    assert not any(line.startswith("The merge will pass") for line in words.splitlines())
    assert "\u202e" not in words
    _no_stray_characters(words)


# ---------------------------------------------------------------------------
# 28 October 2026: only a judge file under the protected paths uses main's copy
# ---------------------------------------------------------------------------
#
# Step 2's request 7 (FEAT-9107): a feature that extended the root check
# script failed its own tests of it, because the merge ran main's copy. The
# merge now runs the build's copy of a judge file outside the project's
# protected paths, and the card says so before approval.

#: The career copies' shape: the design's check is protected, the root check
#: script is the project's own.
CAREER_SHAPE = (
    "autobuild:\n  player:\n    protected_paths: [reference]\n"
    "toolchain:\n  test: bash reference/check.sh reference/fence.txt && bash scripts/check.sh\n"
)


@pytest.mark.parametrize("card", sorted(CARDS))
@pytest.mark.asyncio
async def test_a_judge_outside_the_protected_paths_runs_the_builds_copy(
    config, pool, rules, card: str
) -> None:
    target = _project(CAREER_SHAPE, **{"reference/check.sh": ("100755", "design\n")})

    words, reading = await CARDS[card](
        config, pool, trees=_trees(target), rules=rules,
        name_status="M\0scripts/check.sh\0M\0reference/check.sh\0",
    )

    assert (
        "This build changes scripts/check.sh, named by the project's declared "
        "commands. It is not under the project's protected paths, so the merge will "
        "keep this build's version of it."
    ) in words
    assert (
        "This build changes reference/check.sh, which runs the project's checks. "
        "The merge will check the joined code with main's version of that file "
        "and will not run this build's changes to it."
    ) in words
    fates = {j["path"]: j["main_copy"] for j in reading["judge_files"]}
    assert fates == {"scripts/check.sh": False, "reference/check.sh": True}


@pytest.mark.asyncio
async def test_a_deleted_judge_outside_the_protected_paths_is_left_out(config, pool, rules) -> None:
    words, _reading = await _routine_card(
        config, pool, trees=_trees(_project(CAREER_SHAPE)), rules=rules,
        name_status="D\0scripts/check.sh\0",
    )

    assert (
        "This build deletes scripts/check.sh, named by the project's declared "
        "commands. It is not under the project's protected paths, so the merge will "
        "run without it."
    ) in words
    assert "main's version" not in words
