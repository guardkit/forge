"""The FAILED terminal writes a failure pack (honesty-residues lane, 2026-08-07).

THE RESIDUE
===========

Ledgered at the LI-stage-2 close (2026-08-02) and carried in the plan of
record: *"the FAILED terminal writes no failure pack (pre-existing class)"*.

Before this lane exactly ONE failure route left durable evidence — the one
where the guardkit subprocess actually ran and then failed, timed out, or was
killed by the semantic build monitor. Every other route to the FAILED terminal
(a missing ``feature_id``, an unresolvable repo, a missing guardkit binary, a
branch absent locally, a prior-build residue sweep that refused, a worktree
that would not materialise, a subprocess that would not spawn, and the two
structural loud-no-op guards) wrote NOTHING under the receipts root. For that
whole class of ordinary failures the fix-and-re-verify law had no evidence to
feed on — only a reason string on the wire.

WHAT THESE TESTS PIN
====================

1. The FAILED terminal produces a pack, and the pack ENUMERATES its evidence
   — including, in plain language, the evidence that is *not* there.
2. The WEDGE path is untouched: a build whose subprocess branch already wrote
   the richer pack (wedge verdict, semantic state, resume block) keeps it
   byte-for-byte; the terminal never rewrites it and never archives it aside.
3. A pack-write failure never changes the terminal. A failure-pack failure
   masking the build failure would be the worse defect of the two.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from langchain_core.messages import HumanMessage

from forge.pipeline.fix_journey_receipts import read_failure_pack
from forge.subagents import autobuild_runner as ar

BUILD_ID = "build-FEAT-FTP-1"
FEATURE_ID = "FEAT-FTP"
CORRELATION_ID = "1f47d7c0-0000-4000-8000-00000000ftp1"


def _launch(**overrides: Any) -> str:
    payload: dict[str, Any] = {
        "build_id": BUILD_ID,
        "feature_id": FEATURE_ID,
        "correlation_id": CORRELATION_ID,
        "repo": "appmilla/api_test",
    }
    payload.update(overrides)
    return f"RUN_AUTOBUILD subagent=autobuild_runner payload={json.dumps(payload)}"


def _state(description: str, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "messages": [HumanMessage(content=description)],
        "async_tasks": {FEATURE_ID: existing} if existing is not None else {},
    }


def _pack(receipts: Path, build_id: str = BUILD_ID) -> Path:
    return receipts / build_id


def _manifest(receipts: Path, build_id: str = BUILD_ID) -> dict[str, Any]:
    return json.loads(
        (_pack(receipts, build_id) / ar.FAILURE_MANIFEST_NAME).read_text()
    )


def _archived(receipts: Path, build_id: str = BUILD_ID) -> list[Path]:
    """Any ``failure-manifest.<stamp>.json`` sibling — the overwrite tell."""
    pack = _pack(receipts, build_id)
    if not pack.is_dir():
        return []
    return [
        p
        for p in pack.glob("failure-manifest.*.json")
        if p.name != ar.FAILURE_MANIFEST_NAME
    ]


@pytest.fixture()
def receipts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway receipts root, pinned for the whole test."""
    root = tmp_path / "receipts"
    monkeypatch.setenv(ar.RECEIPTS_DIR_ENV, str(root))
    return root


def _make_receipt_tree(root: Path) -> None:
    """The shape guardkit leaves behind in the tree it ran in."""
    (root / ".guardkit" / "autobuild-private").mkdir(parents=True, exist_ok=True)
    (root / ".guardkit" / "autobuild-private" / "coach.json").write_text(
        json.dumps({"verdict": "FAIL"})
    )
    (root / ".guardkit" / "qav-shadow").mkdir(parents=True, exist_ok=True)
    (root / ".guardkit" / "qav-shadow" / "queue.jsonl").write_text("{}\n")


# ---------------------------------------------------------------------------
# 1 — the FAILED terminal produces a pack, with its evidence enumerated
# ---------------------------------------------------------------------------


class TestFailedTerminalWritesThePack:
    def test_pre_launch_failure_leaves_a_pack_naming_what_it_holds(
        self, receipts: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The ordinary FAILED terminal — no subprocess ever ran — still writes.

        This is the residue's exact shape: guardkit could not be resolved, so
        the build died before any evidence could exist. The pack must still
        land, and must SAY that it holds nothing rather than looking like a
        complete record of an unreproducible failure.
        """
        reason = "guardkit binary not found (PATH lookup + env both failed)"
        existing = ar._build_failed_snapshot(
            {"feature_id": FEATURE_ID, "build_id": BUILD_ID}, reason=reason
        )
        with caplog.at_level(
            logging.WARNING, logger="forge.subagents.autobuild_runner"
        ):
            update = ar._node_failed(_state(_launch(), existing))  # type: ignore[arg-type]

        snapshot = update["async_tasks"][FEATURE_ID]
        assert snapshot["lifecycle"] == "failed"

        manifest = _manifest(receipts)
        assert manifest["build_id"] == BUILD_ID
        assert manifest["feature_id"] == FEATURE_ID
        assert manifest["correlation_id"] == CORRELATION_ID
        assert manifest["reason"] == reason
        assert manifest["terminal"] == ar.TERMINAL_FAILED_NODE
        assert manifest["wedged"] is False
        assert manifest["timed_out"] is False
        # `null`, never -1: the subprocess did not exit, it never started.
        assert manifest["exit_code"] is None

        evidence = manifest["evidence"]
        assert evidence["subprocess_ran"] is False
        assert evidence["worktree_kept"] is False
        assert evidence["stdout_log"] is None
        assert evidence["receipt_families"] == []
        missing = " ".join(evidence["missing"])
        assert "autobuild-stdout.log" in missing
        assert "worktree" in missing
        assert "receipt families" in missing
        assert "semantic_state_at_kill" in missing

        # The pack points back at the terminal on the wire.
        assert snapshot["failure_pack"] == str(
            _pack(receipts) / ar.FAILURE_MANIFEST_NAME
        )
        # Degrade LOUDLY: the thinness is visible from the log alone.
        assert any("failure pack for" in r.getMessage() for r in caplog.records)
        assert any("is THIN" in r.getMessage() for r in caplog.records)

    def test_the_pack_names_the_evidence_forge_cannot_see(self, receipts: Path) -> None:
        """``player_result.error`` is named, not silently absent.

        Related guardkit-side residue: a player-invocation stall is misnamed at
        the final summary layer because the orchestrator never consults
        ``player_result.error``. That field lives inside guardkit's process and
        cannot cross the subprocess boundary — so the pack says so, and a
        reader learns the silence is a KNOWN gap rather than an omission.
        """
        existing = ar._build_failed_snapshot(
            {"feature_id": FEATURE_ID, "build_id": BUILD_ID}, reason="whatever"
        )
        ar._node_failed(_state(_launch(), existing))  # type: ignore[arg-type]

        not_observable = " ".join(_manifest(receipts)["evidence"]["not_observable"])
        assert "player_result.error" in not_observable
        assert "subprocess boundary" in not_observable

    def test_a_kept_worktree_is_exported_before_the_manifest_is_written(
        self, receipts: Path, tmp_path: Path
    ) -> None:
        """EXPORT FIRST: the manifest indexes a pack that already exists.

        A manifest that claims families it has not yet copied is the same
        false-green class the receipts-landing lane cured on the subprocess
        path; the terminal must inherit that posture, not re-invent it.
        """
        worktree = tmp_path / "worktrees" / BUILD_ID
        worktree.mkdir(parents=True)
        _make_receipt_tree(worktree)

        existing = ar._build_failed_snapshot(
            {"feature_id": FEATURE_ID, "build_id": BUILD_ID},
            reason="failed to spawn guardkit subprocess: OSError()",
            worktree_path=worktree,
        )
        ar._node_failed(_state(_launch(), existing))  # type: ignore[arg-type]

        pack = _pack(receipts)
        assert (pack / ".guardkit/autobuild-private/coach.json").is_file()
        assert (pack / ".guardkit/qav-shadow/queue.jsonl").is_file()

        manifest = _manifest(receipts)
        assert ".guardkit/autobuild-private" in manifest["receipt_families_exported"]
        assert manifest["worktree_path"] == str(worktree)
        evidence = manifest["evidence"]
        assert evidence["worktree_kept"] is True
        assert ".guardkit/qav-shadow" in evidence["receipt_families"]
        missing = " ".join(evidence["missing"])
        assert "receipt families" not in missing
        assert "worktree" not in missing

    def test_a_complete_pack_is_never_reported_as_thin(
        self, receipts: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The loud warning must mean something, so it fires only when true.

        ``not_observable`` (the gap forge structurally cannot close) is kept
        OUT of ``missing`` for exactly this reason: folded in, every pack —
        including a full wedge pack — would log THIN on every failed build,
        and a warning that always fires is a warning nobody reads.
        """
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _make_receipt_tree(worktree)
        pack = _pack(receipts)
        pack.mkdir(parents=True)
        (pack / ar.STDOUT_LOG_NAME).write_text("[guardkit] wave 0 starting\n")
        receipts_result = ar._export_receipts(worktree, BUILD_ID)

        with caplog.at_level(
            logging.WARNING, logger="forge.subagents.autobuild_runner"
        ):
            ar._write_failure_manifest(
                build_id=BUILD_ID,
                payload={"feature_id": FEATURE_ID},
                reason="WEDGED: no semantic progress for 900s",
                timed_out=False,
                exit_code=-9,
                worktree_path=worktree,
                branch=None,
                receipts=receipts_result,
                wedged=True,
                semantic_state={"task_id": "TASK-1"},
            )

        evidence = _manifest(receipts)["evidence"]
        assert evidence["missing"] == []
        assert evidence["not_observable"], "the standing gap is still named"
        assert not any("is THIN" in r.getMessage() for r in caplog.records)

    def test_full_graph_run_that_never_launches_still_leaves_a_pack(
        self, receipts: Path
    ) -> None:
        """End to end through the REAL compiled graph, not a hand-called node.

        ``_resolve_guardkit_path`` returns ``None``, so ``running_wave``
        refuses before any subprocess; the conditional edge routes to the
        FAILED terminal. The pack is the proof the whole route now leaves
        evidence.
        """
        with patch.object(ar, "_resolve_repo_path", lambda payload: Path("/tmp")):
            with patch.object(ar, "_resolve_guardkit_path", lambda: None):
                graph = ar._build_runner_graph()
                result = asyncio.run(
                    graph.ainvoke({"messages": [HumanMessage(content=_launch())]})
                )

        assert result["async_tasks"][FEATURE_ID]["lifecycle"] == "failed"
        manifest = _manifest(receipts)
        assert manifest["terminal"] == ar.TERMINAL_FAILED_NODE
        assert "guardkit binary not found" in manifest["reason"]
        assert manifest["exit_code"] is None
        # Exactly ONE pack for the run — the terminal did not double-write.
        assert _archived(receipts) == []

    def test_the_branch_rides_the_pack_when_the_launch_carried_one(
        self, receipts: Path
    ) -> None:
        existing = ar._build_failed_snapshot(
            {"feature_id": FEATURE_ID, "build_id": BUILD_ID},
            reason="branch 'planning/x' does not exist locally",
        )
        ar._node_failed(_state(_launch(branch="planning/x"), existing))  # type: ignore[arg-type]
        assert _manifest(receipts)["branch"] == "planning/x"

    def test_the_fix_journey_reader_surfaces_the_new_fields(
        self, receipts: Path
    ) -> None:
        """The evidence must reach the thing that eats it.

        ``read_failure_pack`` is what the fix journey's first review reads; a
        pack whose ``terminal``/``evidence`` never reached the reviewer would
        close the residue on disk and leave it open where it matters.
        """
        existing = ar._build_failed_snapshot(
            {"feature_id": FEATURE_ID, "build_id": BUILD_ID}, reason="no guardkit"
        )
        ar._node_failed(_state(_launch(), existing))  # type: ignore[arg-type]

        index = read_failure_pack(BUILD_ID, receipts_root=receipts)
        assert index is not None
        assert index.terminal == ar.TERMINAL_FAILED_NODE
        assert index.evidence is not None
        context = index.to_context()
        assert context["terminal"] == ar.TERMINAL_FAILED_NODE
        assert "player_result.error" in " ".join(context["evidence"]["not_observable"])


# ---------------------------------------------------------------------------
# 2 — the wedge path is unchanged
# ---------------------------------------------------------------------------


class TestWedgePathUnchanged:
    def test_the_terminal_never_rewrites_a_pack_the_subprocess_path_wrote(
        self, receipts: Path, tmp_path: Path
    ) -> None:
        """The richer pack survives verbatim — no rewrite, no archive-aside.

        The wedge pack carries what only the running build could know: the
        semantic state at the kill, the ``--resume`` relaunch, the honest task
        counts. A terminal that wrote its own thinner pack on top would
        ARCHIVE that one aside and hand the fix journey the poorer of the two.
        """
        worktree = tmp_path / "wt"
        worktree.mkdir()
        written = ar._write_failure_manifest(
            build_id=BUILD_ID,
            payload={"feature_id": FEATURE_ID, "correlation_id": CORRELATION_ID},
            reason="WEDGED: no semantic progress for 900s",
            timed_out=False,
            exit_code=-9,
            worktree_path=worktree,
            branch="planning/x",
            wedged=True,
            semantic_state={"task_id": "TASK-1", "turn": 7, "phase": "implement"},
            resume={"possible": True, "command": "guardkit autobuild --resume"},
        )
        assert written is not None
        before = (_pack(receipts) / ar.FAILURE_MANIFEST_NAME).read_text()

        existing = ar._build_failed_snapshot(
            {"feature_id": FEATURE_ID, "build_id": BUILD_ID},
            reason="WEDGED: no semantic progress for 900s",
            failure_pack=written,
            worktree_path=worktree,
        )
        ar._node_failed(_state(_launch(), existing))  # type: ignore[arg-type]

        after = (_pack(receipts) / ar.FAILURE_MANIFEST_NAME).read_text()
        assert after == before, "the terminal must not touch the wedge pack"
        assert _archived(receipts) == [], "nothing was archived aside"
        manifest = json.loads(after)
        assert manifest["wedged"] is True
        assert manifest["terminal"] == ar.TERMINAL_RUNNING_WAVE
        assert manifest["semantic_state_at_kill"]["task_id"] == "TASK-1"
        assert manifest["resume"]["possible"] is True
        assert manifest["exit_code"] == -9

    def test_a_replayed_terminal_snapshot_does_not_mint_a_second_pack(
        self, receipts: Path
    ) -> None:
        """The marker rides forward, so a fetch-on-empty replay is a no-op.

        ``_node_failed``'s refreshed snapshot is what a replay re-translates;
        without the marker on it, every replay would archive the pack aside
        and write a fresh one.
        """
        existing = ar._build_failed_snapshot(
            {"feature_id": FEATURE_ID, "build_id": BUILD_ID}, reason="no guardkit"
        )
        first = ar._node_failed(_state(_launch(), existing))  # type: ignore[arg-type]
        replayed = first["async_tasks"][FEATURE_ID]
        assert replayed["failure_pack"]

        before = (_pack(receipts) / ar.FAILURE_MANIFEST_NAME).read_text()
        ar._node_failed(_state(_launch(), replayed))  # type: ignore[arg-type]
        assert (_pack(receipts) / ar.FAILURE_MANIFEST_NAME).read_text() == before
        assert _archived(receipts) == []

    def test_a_succeeded_build_still_carries_no_failure_manifest(
        self, receipts: Path
    ) -> None:
        """The finalize guard's honest-terminal branch writes nothing."""
        completed = {"lifecycle": "completed", "feature_id": FEATURE_ID}
        assert ar._node_finalize(_state(_launch(), completed)) == {}  # type: ignore[arg-type]
        assert not (_pack(receipts) / ar.FAILURE_MANIFEST_NAME).exists()


# ---------------------------------------------------------------------------
# 3 — a pack failure never changes the terminal
# ---------------------------------------------------------------------------


class TestPackFailureNeverMasksTheBuildFailure:
    def test_a_raising_manifest_writer_leaves_the_terminal_intact(
        self, receipts: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        reason = "guardkit binary not found"
        existing = ar._build_failed_snapshot(
            {"feature_id": FEATURE_ID, "build_id": BUILD_ID}, reason=reason
        )

        def _boom(**kwargs: Any) -> Any:
            raise RuntimeError("receipts volume is gone")

        with caplog.at_level(
            logging.WARNING, logger="forge.subagents.autobuild_runner"
        ):
            with patch.object(ar, "_write_failure_manifest", _boom):
                update = ar._node_failed(_state(_launch(), existing))  # type: ignore[arg-type]

        snapshot = update["async_tasks"][FEATURE_ID]
        assert snapshot["lifecycle"] == "failed"
        assert snapshot["error_message"] == reason
        assert snapshot["tasks_failed"] >= 1
        assert "failure_pack" not in snapshot
        assert any("failure pack NOT written" in r.getMessage() for r in caplog.records)

    def test_a_raising_receipt_export_leaves_the_terminal_intact(
        self, receipts: Path, tmp_path: Path
    ) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        existing = ar._build_failed_snapshot(
            {"feature_id": FEATURE_ID, "build_id": BUILD_ID},
            reason="spawn failed",
            worktree_path=worktree,
        )

        def _boom(*args: Any, **kwargs: Any) -> Any:
            raise OSError("disk gone")

        with patch.object(ar, "_export_receipts", _boom):
            update = ar._node_failed(_state(_launch(), existing))  # type: ignore[arg-type]

        assert update["async_tasks"][FEATURE_ID]["lifecycle"] == "failed"
        assert update["async_tasks"][FEATURE_ID]["error_message"] == "spawn failed"

    def test_an_unwritable_receipts_root_warns_and_the_build_still_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The whole pack is best-effort — a blocked root never raises."""
        blocked = tmp_path / "blocked"
        blocked.write_text("not a directory")
        monkeypatch.setenv(ar.RECEIPTS_DIR_ENV, str(blocked))

        existing = ar._build_failed_snapshot(
            {"feature_id": FEATURE_ID, "build_id": BUILD_ID}, reason="no guardkit"
        )
        with caplog.at_level(
            logging.WARNING, logger="forge.subagents.autobuild_runner"
        ):
            update = ar._node_failed(_state(_launch(), existing))  # type: ignore[arg-type]

        assert update["async_tasks"][FEATURE_ID]["lifecycle"] == "failed"
        assert "failure_pack" not in update["async_tasks"][FEATURE_ID]
        assert blocked.read_text() == "not a directory"
        assert any(
            "failure manifest NOT written" in r.getMessage() for r in caplog.records
        )


# ---------------------------------------------------------------------------
# 4 — the two structural guards are FAILED terminals too
# ---------------------------------------------------------------------------


class TestStructuralGuardsAlsoLeaveAPack:
    def test_the_loud_no_op_guard_writes_its_pack(self, receipts: Path) -> None:
        stalled = {"lifecycle": "running_wave", "feature_id": FEATURE_ID}
        update = ar._node_finalize(_state(_launch(), stalled))  # type: ignore[arg-type]

        assert update["async_tasks"][FEATURE_ID]["lifecycle"] == "failed"
        manifest = _manifest(receipts)
        assert manifest["terminal"] == ar.TERMINAL_FINALIZE_GUARD
        assert "without reaching a terminal lifecycle" in manifest["reason"]

    def test_the_graph_construction_placeholder_writes_its_pack(
        self, receipts: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            ar, "_RUNNER_GRAPH_CONSTRUCTION_ERROR", ImportError("langgraph gone")
        )
        update = ar._node_graph_construction_failed(_state(_launch()))  # type: ignore[arg-type]

        assert update["async_tasks"][FEATURE_ID]["lifecycle"] == "failed"
        manifest = _manifest(receipts)
        assert manifest["terminal"] == ar.TERMINAL_GRAPH_CONSTRUCTION
        assert "langgraph gone" in manifest["reason"]
        assert manifest["evidence"]["subprocess_ran"] is False
