"""What the publisher's tests are driven against: real git, a real ledger.

NOTHING HERE CONTACTS ANYTHING REAL. The "remote" is a bare repository in the
test's own temporary folder, which is real git and nobody's account. The
"credential" is a made-up string that nothing anywhere accepts, planted so
that a test can grep for it. No service is started, no image is built, no
sandbox is touched and no memory service, broker or real remote is reached.

NOTHING HERE NAMES A LANGUAGE. The project is two text files and a folder.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations
from forge.pipeline.publication_record import (
    STEP_CANDIDATE_CHECK,
    STEP_JOIN,
    STEP_MERGE_CHECKS,
    PublicationRecordStore,
)
from forge.publisher.settings import ProjectRoute, PublisherSettings

#: The made-up credential. Nothing anywhere accepts it; it is recognisable so
#: that a test can grep every file, log line, answer and child environment a
#: run produced and prove it is in none of them.
THE_MADE_UP_CREDENTIAL = "not-a-real-credential-TESTONLY-8f31c2"

BUILD = "build-FEAT-PUB1-1"
FEATURE = "FEAT-PUB1"
PROJECT = "bench/widget-shop"


def git(where: Path, *args: str) -> str:
    done = subprocess.run(
        [
            "git",
            "-c",
            "user.email=tests@example.invalid",
            "-c",
            "user.name=tests",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=str(where),
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} in {where}: {done.stderr.strip()}")
    return done.stdout.strip()


def make_the_project(root: Path, *, branch: str = "main") -> dict[str, Any]:
    """A bare "remote", a copy of it, a build branch, and a real join.

    Returns the two paths and the three commits the publisher is told about:
    G (where the remote's branch is), the build's tip, and J (the join of the
    two, made for real by git).
    """
    bare = root / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", branch, "-q", str(bare)],
        check=True,
        capture_output=True,
    )
    # A bare repository keeps no reflog by default. It is turned on here so
    # that the remote ITSELF can say how many times anybody moved its branch
    # — which is how "sent once" is counted, rather than by asking the thing
    # that did the sending.
    subprocess.run(
        ["git", "--git-dir", str(bare), "config", "core.logAllRefUpdates", "true"],
        check=True,
        capture_output=True,
    )
    seed = root / "seed"
    seed.mkdir()
    git(seed, "init", "-b", branch, "-q")
    (seed / "README").write_text("the widget shop\n", encoding="utf-8")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "the beginning")
    git(seed, "remote", "add", "origin", str(bare))
    git(seed, "push", "-q", "origin", branch)

    copy = root / "copy"
    subprocess.run(
        ["git", "clone", "-q", str(bare), str(copy)], check=True, capture_output=True
    )
    git(copy, "checkout", "-q", "-b", f"autobuild/{FEATURE}")
    (copy / "the-feature").write_text("what the build made\n", encoding="utf-8")
    git(copy, "add", "the-feature")
    git(copy, "commit", "-q", "-m", "the feature")
    tip = git(copy, "rev-parse", "HEAD")
    git(copy, "checkout", "-q", branch)
    g = git(copy, "rev-parse", branch)

    # The join, made the way the merge word makes it: a working folder of its
    # own at G, on a branch of the factory's own, a real --no-ff merge.
    folder = root / "join"
    git(copy, "worktree", "add", "-q", "-b", f"factory-integration/{FEATURE}", str(folder), g)
    git(folder, "merge", "--no-ff", "-m", f"join {FEATURE}", f"autobuild/{FEATURE}")
    j = git(folder, "rev-parse", "HEAD")
    # The reflog starts empty for the test's own counting: everything above
    # is the world being set up, and what the tests count is what the
    # PUBLISHER did to the remote afterwards.
    log = bare / "logs" / "refs" / "heads" / branch
    if log.is_file():
        log.write_text("", encoding="utf-8")
    return {
        "bare": bare,
        "copy": copy,
        "branch": branch,
        "g": g,
        "tip": tip,
        "j": j,
        "join_folder": folder,
    }


def somebody_else_lands_work(bare: Path, root: Path, what: str, *, branch: str = "main") -> str:
    """Another hand pushes to the remote."""
    other = root / f"other-{what}"
    subprocess.run(
        ["git", "clone", "-q", str(bare), str(other)], check=True, capture_output=True
    )
    (other / what).write_text("somebody else's work\n", encoding="utf-8")
    git(other, "add", what)
    git(other, "commit", "-q", "-m", f"somebody else landed {what}")
    git(other, "push", "-q", "origin", branch)
    return git(other, "rev-parse", branch)


def make_the_ledger(
    path: Path,
    *,
    project: dict[str, Any],
    build_id: str = BUILD,
    turn_takes: int = 1,
    merge_checks: bool | None = True,
    candidate_check: bool | None = True,
    ran_on: str | None = None,
    attempt: int = 1,
) -> PublicationRecordStore:
    """A ledger with one build's publication record, written by the REAL store.

    ``merge_checks`` / ``candidate_check``: ``True`` writes a done line that
    passed, ``False`` writes one that went red, ``None`` writes none at all —
    which is the GATED case the merge word leaves when it picks a join up.
    """
    connection = sqlite_connect.connect_writer(path)
    migrations.apply_at_boot(connection)
    store = PublicationRecordStore(connection)
    now = datetime.now(timezone.utc)
    grant = None
    for _ in range(max(1, turn_takes)):
        grant = store.take_lease(
            build_id=build_id,
            holder="a test",
            now=now,
            seconds=0,
            feature_id=FEATURE,
            repo=PROJECT,
            decided_by="the owner",
            target_branch=project["branch"],
        )
        assert grant is not None
    assert grant is not None
    turn = grant.turn
    on = ran_on or project["j"]
    store.record(
        build_id=build_id,
        turn=turn,
        now=now,
        g_commit=project["g"],
        build_tip=project["tip"],
        attempt=attempt,
    )
    store.done(
        build_id=build_id,
        turn=turn,
        now=now,
        step=STEP_JOIN,
        attempt=attempt,
        result={"j_commit": project["j"], "verify_ok": True},
        j_commit=project["j"],
    )
    if merge_checks is not None:
        store.done(
            build_id=build_id,
            turn=turn,
            now=now,
            step=STEP_MERGE_CHECKS,
            attempt=attempt,
            result={
                "ran_on": on,
                "verify_ok": bool(merge_checks),
                "verify_detail": None if merge_checks else "3 checks went red",
                "checks_passed": 12 if merge_checks else 9,
                "checks_total": 12,
            },
        )
    if candidate_check is not None:
        store.done(
            build_id=build_id,
            turn=turn,
            now=now,
            step=STEP_CANDIDATE_CHECK,
            attempt=attempt,
            result={
                "ran_on": on,
                "j_commit": on,
                "verify_ok": bool(candidate_check),
                "verdict": "pass" if candidate_check else "fail",
                "checks_passed": 4 if candidate_check else 1,
                "checks_total": 4,
            },
        )
    return store


def the_credential_file(root: Path) -> Path:
    where = root / "the-credential"
    where.write_text(THE_MADE_UP_CREDENTIAL + "\n", encoding="utf-8")
    where.chmod(0o600)
    return where


def settings_for(
    root: Path,
    project: dict[str, Any],
    *,
    ledger: Path,
    credential_file: Path | None = None,
    projects: dict[str, ProjectRoute] | None = None,
) -> PublisherSettings:
    return PublisherSettings(
        credential_file=str(
            credential_file if credential_file is not None else the_credential_file(root)
        ),
        ledger=str(ledger),
        state_dir=str(root / "publisher-state"),
        projects=projects
        if projects is not None
        else {
            PROJECT: ProjectRoute(
                name=PROJECT,
                # READ-ONLY by contract: the publisher only ever fetches from
                # it. For a project built in a sandbox this is that sandbox's
                # own git service; for one built here it is the copy itself.
                source=str(project["copy"]),
                remote=str(project["bare"]),
            )
        },
        port=0,
        git_timeout_seconds=60.0,
    )


def a_request(project: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    asked = {
        "project": PROJECT,
        "build_id": BUILD,
        "turn": 1,
        "j_commit": project["j"],
        "target_branch": project["branch"],
    }
    asked.update(overrides)
    return asked


def what_the_remote_has(bare: Path, branch: str = "main") -> str:
    done = subprocess.run(
        ["git", "--git-dir", str(bare), "rev-parse", branch],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def every_push_the_remote_saw(bare: Path, branch: str = "main") -> list[str]:
    """Every time the bare repository's branch moved, from its own reflog.

    This is how "sent ONCE" is counted: the remote itself says how many
    times anybody moved its branch.
    """
    log = bare / "logs" / "refs" / "heads" / branch
    if not log.is_file():
        return []
    return [line for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def as_json(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)
