"""The disposable-resource sweeps clear a build's whole stack and spare previews.

What the sweeps must do
-----------------------
A build's tests may start services with docker compose. Everything such a
stack makes carries GuardKit's fixture label, ``guardkit.fixture.owner=<build
id>``. Two sweeps clear these disposable stacks:

* ``forge.build_processes.remove_fixture_containers(build_id)``, which the
  build runner calls when a build is cancelled, times out or finishes, and
  which clears that one build's resources;
* ``remove_leftover_fixtures`` in ``sandbox-runner.sh``, which runs before a
  build runner starts, when no build can be running, and clears every
  owner-labelled resource a previous runner left behind.

The requirements (web readiness review, finding R6; the repair belongs to the
reliability lane):

1. Both sweeps stay keyed ONLY on the ``guardkit.fixture.owner`` label. Every
   listing they make filters on it; they never list everything and pick.
2. Both sweeps remove that label's compose NETWORKS and VOLUMES as well as its
   containers. Today they remove containers only, so networks and volumes
   pile up on the sandbox's engine. Those tests are
   ``xfail(strict=True)`` and flip loudly once the repair lands.
3. Neither sweep ever lists, filters on or removes anything labelled
   ``forge.preview.lease=<id>``: those are review previews kept on purpose for
   the person reviewing a merge. A restart with one live preview stack and one
   abandoned disposable stack removes only the disposable one.
4. The cancel sweep clears only its own build's resources, never another
   build's.

How, without an engine. Both sweeps talk to the world through one Docker
client. These tests put a stand-in client in its place that keeps a small table
of containers, networks and volumes with their labels, answers ``ps`` /
``container ls``, ``network ls`` and ``volume ls`` with label filters, removes
on ``rm`` / ``network rm`` / ``volume rm`` and the ``prune`` forms, and records
every call. A network or volume still used by an existing container refuses
removal, as a real engine does. **No real engine, container, network or volume
is touched by anything in this file.** The shell sweep is run on its own:
the function is lifted out of the shipped script, exactly as written, and run
by bash with ``DOCKER`` pointing at the stand-in.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

import forge.cli.deploy_templates as templates
from forge import build_processes

OWNER = build_processes.FIXTURE_OWNER_LABEL
PREVIEW = "forge.preview.lease"

#: The bootstrap that holds the runner-start sweep, as Forge ships it.
BOOTSTRAP = Path(templates.__file__).resolve().parent / "sandbox-runner.sh"

_R6 = pytest.mark.xfail(
    strict=True,
    reason=(
        "reliability lane: disposable sweep covers networks/volumes — "
        "web readiness R6"
    ),
)

#: A stand-in Docker client: a table of resources in one JSON file.
STANDIN_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys

calls = os.environ["STANDIN_CALLS"]
table_path = os.environ["STANDIN_TABLE"]
argv = sys.argv[1:]
with open(calls, "a") as handle:
    handle.write(json.dumps(argv) + "\n")
with open(table_path) as handle:
    table = json.load(handle)


def save():
    with open(table_path, "w") as handle:
        json.dump(table, handle, indent=1)


def matches(labels, words):
    for index, word in enumerate(words):
        if word not in ("--filter", "-f") or index + 1 >= len(words):
            continue
        rule = words[index + 1]
        if rule.startswith("label!="):
            key, _, value = rule[len("label!="):].partition("=")
            if key in labels and (not value or labels[key] == value):
                return False
        elif rule.startswith("label="):
            key, sep, value = rule[len("label="):].partition("=")
            if key not in labels or (sep and labels[key] != value):
                return False
    return True


def flags_removed(words):
    out, skip = [], False
    for word in words:
        if skip:
            skip = False
            continue
        if word in ("--filter", "--format"):
            skip = True
            continue
        if word.startswith("-"):
            continue
        out.append(word)
    return out


def in_use(kind, name):
    return any(name in c.get(kind, []) for c in table["containers"].values())


def remove(kind, names):
    status = 0
    for name in names:
        if name not in table[kind]:
            status = 1
            continue
        if kind != "containers" and in_use(kind, name):
            print("Error: %s %s is in use" % (kind, name), file=sys.stderr)
            status = 1
            continue
        del table[kind][name]
    save()
    return status


def prune(kind, words):
    for name, item in list(table[kind].items()):
        if matches(item["labels"], words) and not in_use(kind, name):
            del table[kind][name]
    save()
    return 0


verb = argv[:1]
rest = argv[1:]
if argv[:1] == ["container"] and argv[1:2] in (["ls"], ["list"], ["ps"]):
    verb, rest = ["ps"], argv[2:]
elif argv[:1] == ["container"] and argv[1:2] in (["rm"], ["remove"]):
    verb, rest = ["rm"], argv[2:]
elif argv[:1] == ["container"] and argv[1:2] == ["prune"]:
    sys.exit(prune("containers", argv[2:]))

if verb == ["ps"]:
    everything = "--all" in rest or "-a" in rest
    for name, item in table["containers"].items():
        if (everything or item["running"]) and matches(item["labels"], rest):
            print(name)
    sys.exit(0)
if verb == ["rm"]:
    sys.exit(remove("containers", flags_removed(rest)))

for kind, word in (("networks", "network"), ("volumes", "volume")):
    if argv[:1] == [word]:
        sub, words = argv[1:2], argv[2:]
        if sub in (["ls"], ["list"]):
            for name, item in table[kind].items():
                if matches(item["labels"], words):
                    print(name)
            sys.exit(0)
        if sub in (["rm"], ["remove"]):
            sys.exit(remove(kind, flags_removed(words)))
        if sub == ["prune"]:
            sys.exit(prune(kind, words))
sys.exit(0)
'''


def _container(labels: dict, *, running: bool = True, networks=(), volumes=()):
    return {
        "labels": labels,
        "running": running,
        "networks": list(networks),
        "volumes": list(volumes),
    }


def _stack(table: dict, prefix: str, labels: dict, *, running: bool) -> None:
    """A two-service compose stack with its own network and volume."""
    network, volume = f"{prefix}_default", f"{prefix}_data"
    table["networks"][network] = {"labels": dict(labels)}
    table["volumes"][volume] = {"labels": dict(labels)}
    table["containers"][f"{prefix}-db-1"] = _container(
        labels, running=running, networks=[network], volumes=[volume]
    )
    table["containers"][f"{prefix}-api-1"] = _container(
        labels, running=running, networks=[network]
    )


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """A stand-in client, wired in for both sweeps, with an empty table."""
    client = tmp_path / "docker-standin"
    client.write_text(STANDIN_DOCKER)
    client.chmod(0o755)
    made = {
        "client": client,
        "calls": tmp_path / "calls.jsonl",
        "table": tmp_path / "table.json",
        "data": {"containers": {}, "networks": {}, "volumes": {}},
    }
    monkeypatch.setenv("STANDIN_CALLS", str(made["calls"]))
    monkeypatch.setenv("STANDIN_TABLE", str(made["table"]))
    # The cancel sweep asks only a configured engine; the stand-in is it.
    monkeypatch.setenv("FORGE_FIXTURE_ENGINE", str(client))
    monkeypatch.setenv("DOCKER_HOST", f"unix://{tmp_path}/standin.sock")
    return made


def _write(engine) -> None:
    engine["table"].write_text(json.dumps(engine["data"], indent=1))


def _left(engine) -> dict:
    return json.loads(engine["table"].read_text())


def _calls(engine) -> list[list[str]]:
    path = engine["calls"]
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _cancel_sweep(build_id: str):
    return asyncio.run(build_processes.remove_fixture_containers(build_id))


def _the_shell_sweep() -> str:
    """``remove_leftover_fixtures`` exactly as the shipped script defines it."""
    text = BOOTSTRAP.read_text()
    found = re.search(r"^remove_leftover_fixtures\(\) \{\n.*?^\}\n", text, re.M | re.S)
    assert found, "remove_leftover_fixtures is no longer defined in sandbox-runner.sh"
    return found.group(0)


def _runner_start_sweep(engine) -> subprocess.CompletedProcess:
    script = (
        "set -euo pipefail\n"
        'log() { printf "%s\\n" "$*"; }\n'
        f"{_the_shell_sweep()}"
        "remove_leftover_fixtures\n"
    )
    return subprocess.run(
        ["bash", "-c", script],
        env={
            "PATH": os.environ["PATH"],
            "DOCKER": str(engine["client"]),
            "STANDIN_CALLS": str(engine["calls"]),
            "STANDIN_TABLE": str(engine["table"]),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )


def _with_preview_and_two_builds(engine) -> None:
    data = engine["data"]
    _stack(data, "mine", {OWNER: "build-mine", "com.docker.compose.project": "mine"},
           running=True)
    _stack(data, "theirs", {OWNER: "build-theirs", "com.docker.compose.project": "theirs"},
           running=True)
    _stack(data, "preview", {PREVIEW: "lease-1", "com.docker.compose.project": "preview"},
           running=True)
    _write(engine)


def _preview_untouched(engine) -> None:
    left = _left(engine)
    assert {"preview-db-1", "preview-api-1"} <= set(left["containers"])
    assert "preview_default" in left["networks"]
    assert "preview_data" in left["volumes"]


def _never_named_the_preview_label(engine) -> None:
    for argv in _calls(engine):
        assert not any(PREVIEW in word for word in argv), argv


def _every_listing_is_keyed_on_the_owner_label(engine) -> None:
    for argv in _calls(engine):
        listing = argv[:1] == ["ps"] or argv[1:2] in (["ls"], ["list"], ["prune"])
        if listing:
            assert any(
                word == f"label={OWNER}" or word.startswith(f"label={OWNER}=")
                for word in argv
            ), f"a listing not keyed on {OWNER}: {argv}"


# =========================================================================
# 1. Cancel or timeout: one build's sweep
# =========================================================================


class TestTheCancelSweep:
    @_R6
    def test_it_removes_this_builds_networks_and_volumes(self, engine):
        _with_preview_and_two_builds(engine)
        _cancel_sweep("build-mine")
        left = _left(engine)
        assert "mine_default" not in left["networks"]
        assert "mine_data" not in left["volumes"]

    def test_it_removes_this_builds_containers_and_no_one_elses(self, engine):
        """Guard (passes today)."""
        _with_preview_and_two_builds(engine)
        remaining = _cancel_sweep("build-mine")
        left = _left(engine)
        assert remaining == []
        assert not {"mine-db-1", "mine-api-1"} & set(left["containers"])
        assert {"theirs-db-1", "theirs-api-1"} <= set(left["containers"])
        assert "theirs_default" in left["networks"]
        assert "theirs_data" in left["volumes"]

    def test_it_never_names_or_touches_a_preview(self, engine):
        """Guard (passes today): keyed only on the owner label."""
        _with_preview_and_two_builds(engine)
        _cancel_sweep("build-mine")
        _preview_untouched(engine)
        _never_named_the_preview_label(engine)
        _every_listing_is_keyed_on_the_owner_label(engine)


# =========================================================================
# 2. Runner start: every leftover disposable stack
# =========================================================================


class TestTheRunnerStartSweep:
    def test_the_runner_start_runs_the_sweep_first(self):
        """The sweep under test is the one ``start_runner`` calls first."""
        text = BOOTSTRAP.read_text()
        body = re.search(r"^start_runner\(\) \{\n(.*?)^\}\n", text, re.M | re.S)
        assert body, "start_runner is no longer defined in sandbox-runner.sh"
        first = [line.strip() for line in body.group(1).splitlines() if line.strip()]
        assert first[0] == "remove_leftover_fixtures"

    @_R6
    def test_it_removes_leftover_owner_labelled_networks_and_volumes(self, engine):
        data = engine["data"]
        _stack(data, "old", {OWNER: "build-old"}, running=False)
        _stack(data, "older", {OWNER: "build-older"}, running=True)
        _write(engine)
        done = _runner_start_sweep(engine)
        assert done.returncode == 0, done.stdout + done.stderr
        left = _left(engine)
        assert left["networks"] == {}
        assert left["volumes"] == {}

    def test_it_never_names_or_touches_a_preview(self, engine):
        """Guard (passes today): keyed only on the owner label."""
        _with_preview_and_two_builds(engine)
        done = _runner_start_sweep(engine)
        assert done.returncode == 0, done.stdout + done.stderr
        _preview_untouched(engine)
        _never_named_the_preview_label(engine)
        _every_listing_is_keyed_on_the_owner_label(engine)

    def test_a_restart_removes_only_the_abandoned_disposable_stack(self, engine):
        """Guard (passes today). One live preview stack, kept for a reviewer,
        and one disposable stack a killed runner abandoned: after the restart
        sweep only the disposable containers are gone."""
        data = engine["data"]
        _stack(data, "preview", {PREVIEW: "lease-7", "com.docker.compose.project": "preview"},
               running=True)
        _stack(data, "abandoned", {OWNER: "build-gone", "com.docker.compose.project": "abandoned"},
               running=False)
        _write(engine)
        done = _runner_start_sweep(engine)
        assert done.returncode == 0, done.stdout + done.stderr
        left = _left(engine)
        assert set(left["containers"]) == {"preview-db-1", "preview-api-1"}
        assert all(item["running"] for item in left["containers"].values())
        _preview_untouched(engine)
        _never_named_the_preview_label(engine)
