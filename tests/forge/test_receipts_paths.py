"""The receipts path rules have ONE implementation, and the CLI can reach it.

The in-flight stage row (design §h stage 1) put a READ side on the receipts
tree for the first time: ``forge status`` must resolve
``<receipts>/<build_id>/in-flight.json`` to show a running build's stage. The
rules for resolving that root lived in
:mod:`forge.subagents.autobuild_runner`, whose first act is ``import
langgraph`` — and ``forge status`` is a SQLite-only CLI whose whole promise is
that it keeps working when the rest of the estate does not.

So the rules moved to :mod:`forge.receipts`, a stdlib-only leaf. This module
proves the move cost nothing:

* every constant the runner exported is the SAME OBJECT it always was, so no
  caller and no test that reaches for ``autobuild_runner.RECEIPTS_DIR_ENV``
  can have shifted;
* both doors resolve to the same path across all three resolution tiers;
* ``forge.cli.status`` still does not drag langgraph into its import graph —
  the point of the whole exercise.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from forge import receipts
from forge.subagents import autobuild_runner as ar


class TestTheRunnerReExportsExactlyWhatItUsedToOwn:
    def test_every_constant_is_the_same_object(self) -> None:
        assert ar.RECEIPTS_DIR_ENV is receipts.RECEIPTS_DIR_ENV
        assert ar.DEFAULT_RECEIPTS_DIR is receipts.DEFAULT_RECEIPTS_DIR
        assert ar.BOUND_STATE_ROOT is receipts.BOUND_STATE_ROOT
        assert ar.RECEIPTS_DIRNAME is receipts.RECEIPTS_DIRNAME
        assert ar.IN_FLIGHT_STATE_NAME is receipts.IN_FLIGHT_STATE_NAME

    def test_the_values_are_the_documented_ones(self) -> None:
        """A rename here would silently move every build's evidence."""
        assert receipts.RECEIPTS_DIR_ENV == "FORGE_RECEIPTS_DIR"
        assert receipts.DEFAULT_RECEIPTS_DIR == "~/forge-state/receipts"
        assert receipts.BOUND_STATE_ROOT == Path("/var/forge")
        assert receipts.RECEIPTS_DIRNAME == "receipts"
        assert receipts.IN_FLIGHT_STATE_NAME == "in-flight.json"


class TestBothDoorsResolveIdentically:
    """All three tiers, through the runner and through the leaf."""

    def test_tier_1_the_env_knob(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(receipts.RECEIPTS_DIR_ENV, str(tmp_path / "elsewhere"))
        assert ar._receipts_root() == tmp_path / "elsewhere"
        assert receipts.receipts_root() == tmp_path / "elsewhere"

    def test_tier_2_the_bind_mount(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ``/var/forge`` arm — the one that cost a fix journey's receipts.

        Both module-level globals are steered together, because both are
        legitimate patch targets: the runner's, which callers have patched
        since FEAT-DRC, and the leaf's, which is what the CLI reads.
        """
        bound = tmp_path / "var-forge"
        bound.mkdir()
        monkeypatch.delenv(receipts.RECEIPTS_DIR_ENV, raising=False)
        monkeypatch.setattr(ar, "BOUND_STATE_ROOT", bound)
        monkeypatch.setattr(receipts, "BOUND_STATE_ROOT", bound)
        assert ar._receipts_root() == bound / "receipts"
        assert receipts.receipts_root() == bound / "receipts"

    def test_the_runners_own_global_still_steers_tier_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The re-export must not have broken the existing patch target.

        ``tests/forge/pipeline/test_fix_journey_receipts.py`` patches
        ``autobuild_runner.BOUND_STATE_ROOT`` and expects it to bite. A plain
        re-export could not carry that patch — hence ``_receipts_root`` reading
        the global at call time and handing it down.
        """
        bound = tmp_path / "runner-only"
        bound.mkdir()
        monkeypatch.delenv(receipts.RECEIPTS_DIR_ENV, raising=False)
        monkeypatch.setattr(ar, "BOUND_STATE_ROOT", bound)
        assert ar._receipts_root() == bound / "receipts"

    def test_tier_3_the_host_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(receipts.RECEIPTS_DIR_ENV, raising=False)
        missing = tmp_path / "no-such-mount"
        monkeypatch.setattr(ar, "BOUND_STATE_ROOT", missing)
        monkeypatch.setattr(receipts, "BOUND_STATE_ROOT", missing)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        expected = Path(receipts.DEFAULT_RECEIPTS_DIR).expanduser()
        assert ar._receipts_root() == expected
        assert receipts.receipts_root() == expected

    def test_an_empty_env_value_is_not_a_configuration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Whitespace is not a path; it must fall through, not resolve to cwd."""
        missing = tmp_path / "no-such-mount"
        monkeypatch.setenv(receipts.RECEIPTS_DIR_ENV, "   ")
        monkeypatch.setattr(ar, "BOUND_STATE_ROOT", missing)
        monkeypatch.setattr(receipts, "BOUND_STATE_ROOT", missing)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        assert receipts.receipts_root() == Path(
            receipts.DEFAULT_RECEIPTS_DIR
        ).expanduser()


class TestTheLeafIsActuallyALeaf:
    def test_forge_receipts_imports_no_third_party_module(self) -> None:
        """Run it in a FRESH interpreter — an in-process check would pass on
        modules the test session already imported for other reasons."""
        code = (
            "import sys; import forge.receipts; "
            "print(sorted(m for m in sys.modules "
            "if m.split('.')[0] in "
            "{'langgraph','langchain','langchain_core','deepagents',"
            "'langgraph_sdk','langgraph_api','yaml','pydantic','click','nats'}))"
        )
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ},
        )
        assert out.stdout.strip() == "[]", out.stdout

    def test_forge_cli_status_never_reaches_langgraph(self) -> None:
        """THE POINT OF THE SPLIT.

        ``forge status`` is the SQLite-only read path (AC-006 already fences it
        off from NATS). The in-flight lane gave it a filesystem read as well,
        and the wrong import would have dragged the entire graph runtime into a
        CLI that must start fast and must work when nothing else does.
        """
        code = (
            "import sys; import forge.cli.status; "
            "print(sorted(m for m in sys.modules "
            "if m.split('.')[0] in "
            "{'langgraph','langchain','langchain_core','deepagents',"
            "'langgraph_sdk','langgraph_api'}))"
        )
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ},
        )
        assert out.stdout.strip() == "[]", out.stdout
