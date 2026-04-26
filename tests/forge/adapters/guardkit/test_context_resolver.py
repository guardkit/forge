"""Unit tests for :mod:`forge.adapters.guardkit.context_resolver`
(TASK-GCI-003 / DDR-005).

Test classes mirror the acceptance criteria in the task file:

- AC-001: ``resolve_context_flags()`` returns a :class:`ResolvedContext`
  with ``flags``, ``paths`` and ``warnings`` attributes.
- AC-002: ``_COMMAND_CATEGORY_FILTER`` matches DDR-005 verbatim — nine
  entries, no Graphiti subcommands.
- AC-003: missing manifest degrades to empty flags + a single
  ``context_manifest_missing`` warning, never raises.
- AC-004: ``internal_docs.always_include`` paths are prepended.
- AC-005: dependency chase honours the depth-2 cap and emits a
  ``context_manifest_cycle_detected`` warning when stopped.
- AC-006: cycle detection skips already-visited manifests in the chain.
- AC-007: documents outside ``read_allowlist`` are omitted with a warning.
- AC-008: documents whose path resolves outside the repo root are
  omitted with a warning.
- AC-009: resolution is stateless — two calls produce independent
  ``ResolvedContext`` values.
- AC-010: order is stable: always_include first, then categories in
  ``_COMMAND_CATEGORY_FILTER`` insertion order.
- AC-011: symlinks are followed before the allowlist check.
- AC-012: Graphiti subcommand keys raise :class:`KeyError` on the
  resolver (caller must skip resolution).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from forge.adapters.guardkit.context_resolver import (
    _COMMAND_CATEGORY_FILTER,
    ResolvedContext,
    resolve_context_flags,
)
from forge.adapters.guardkit.models import GuardKitWarning

# ---------------------------------------------------------------------------
# Manifest fixture helpers
# ---------------------------------------------------------------------------


def _write_manifest(repo_root: Path, manifest: dict[str, Any]) -> Path:
    """Write a ``.guardkit/context-manifest.yaml`` under ``repo_root``."""
    manifest_dir = repo_root / ".guardkit"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    path = manifest_dir / "context-manifest.yaml"
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return path


def _make_repo(root: Path, name: str) -> Path:
    """Create an empty repo directory ``root/name``."""
    repo = root / name
    repo.mkdir(parents=True, exist_ok=True)
    return repo


def _touch(repo: Path, rel_path: str) -> Path:
    """Create an empty file at ``repo/rel_path`` (parents created)."""
    path = repo / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# fixture\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# AC-001: shape of ResolvedContext
# ---------------------------------------------------------------------------


class TestResolvedContextShape:
    """AC-001 — ResolvedContext exposes flags / paths / warnings."""

    def test_resolved_context_has_three_named_attributes(self, tmp_path: Path) -> None:
        # Run against a missing manifest — guarantees the shape exists
        # even on the degenerate path.
        result = resolve_context_flags(
            repo_path=tmp_path / "nope",
            subcommand="feature-spec",
            read_allowlist=[tmp_path],
        )
        assert isinstance(result, ResolvedContext)
        assert hasattr(result, "flags")
        assert hasattr(result, "paths")
        assert hasattr(result, "warnings")
        assert isinstance(result.flags, list)
        assert isinstance(result.paths, list)
        assert isinstance(result.warnings, list)


# ---------------------------------------------------------------------------
# AC-002: _COMMAND_CATEGORY_FILTER matches DDR-005
# ---------------------------------------------------------------------------


class TestCommandCategoryFilterMatchesDDR005:
    """AC-002 — the filter dict matches DDR-005 verbatim."""

    EXPECTED: dict[str, set[str]] = {
        "system-arch": {"architecture", "decisions"},
        "system-design": {"specs", "decisions", "contracts", "architecture"},
        "system-plan": {"architecture", "decisions", "specs"},
        "feature-spec": {"specs", "contracts", "source", "decisions"},
        "feature-plan": {"specs", "decisions", "architecture"},
        "task-review": {"contracts", "source"},
        "task-work": {"contracts", "source"},
        "task-complete": {"contracts", "source"},
        "autobuild": {"contracts", "source"},
    }

    def test_filter_has_exactly_nine_entries(self) -> None:
        assert len(_COMMAND_CATEGORY_FILTER) == 9

    def test_filter_keys_match_ddr005(self) -> None:
        assert set(_COMMAND_CATEGORY_FILTER.keys()) == set(self.EXPECTED.keys())

    def test_filter_values_match_ddr005(self) -> None:
        for key, value in self.EXPECTED.items():
            assert _COMMAND_CATEGORY_FILTER[key] == value, key

    @pytest.mark.parametrize(
        "graphiti_subcommand",
        [
            "graphiti-search",
            "graphiti-export",
            "graphiti-import",
            "graphiti-prune",
        ],
    )
    def test_graphiti_subcommands_are_absent(self, graphiti_subcommand: str) -> None:
        assert graphiti_subcommand not in _COMMAND_CATEGORY_FILTER


# ---------------------------------------------------------------------------
# AC-003: missing manifest degrades gracefully
# ---------------------------------------------------------------------------


class TestMissingManifestDegradesGracefully:
    """AC-003 — missing manifest never raises."""

    def test_missing_manifest_returns_empty_flags(self, tmp_path: Path) -> None:
        result = resolve_context_flags(
            repo_path=tmp_path / "nonexistent",
            subcommand="feature-spec",
            read_allowlist=[tmp_path],
        )
        assert result.flags == []
        assert result.paths == []

    def test_missing_manifest_emits_single_warning(self, tmp_path: Path) -> None:
        result = resolve_context_flags(
            repo_path=tmp_path / "nonexistent",
            subcommand="feature-spec",
            read_allowlist=[tmp_path],
        )
        assert len(result.warnings) == 1
        warning = result.warnings[0]
        assert isinstance(warning, GuardKitWarning)
        assert warning.code == "context_manifest_missing"

    def test_missing_manifest_does_not_raise(self, tmp_path: Path) -> None:
        # Implicit: if it raised, this test would error.
        resolve_context_flags(
            repo_path=tmp_path / "nope",
            subcommand="task-work",
            read_allowlist=[tmp_path],
        )


# ---------------------------------------------------------------------------
# AC-004: always_include is prepended
# ---------------------------------------------------------------------------


class TestAlwaysIncludePrepended:
    """AC-004 — internal_docs.always_include is prepended regardless of filter."""

    def test_always_include_paths_appear_before_category_paths(
        self, tmp_path: Path
    ) -> None:
        repo = _make_repo(tmp_path, "origin")
        _touch(repo, "docs/architecture/ARCHITECTURE.md")
        _touch(repo, "docs/specs/feature-spec.md")
        dep = _make_repo(tmp_path, "dep")
        _touch(dep, "docs/specs/dep-spec.md")

        _write_manifest(
            repo,
            {
                "repo": "origin",
                "internal_docs": {
                    "always_include": ["docs/architecture/ARCHITECTURE.md"]
                },
                "dependencies": {
                    "dep": {
                        "path": "../dep",
                        "key_docs": [
                            {
                                "path": "docs/specs/dep-spec.md",
                                "category": "specs",
                            },
                        ],
                    }
                },
            },
        )

        result = resolve_context_flags(
            repo_path=repo,
            subcommand="feature-spec",
            read_allowlist=[tmp_path],
        )

        assert len(result.paths) == 2
        assert result.paths[0].endswith("docs/architecture/ARCHITECTURE.md")
        assert result.paths[1].endswith("docs/specs/dep-spec.md")

    def test_always_include_added_for_uncategorised_doc(self, tmp_path: Path) -> None:
        # A doc in always_include needs no category; it is included
        # regardless of the subcommand's filter.
        repo = _make_repo(tmp_path, "origin")
        _touch(repo, "docs/random/not_in_any_filter.md")
        _write_manifest(
            repo,
            {
                "repo": "origin",
                "internal_docs": {
                    "always_include": ["docs/random/not_in_any_filter.md"]
                },
            },
        )

        # task-work filter is {contracts, source} — neither matches.
        result = resolve_context_flags(
            repo_path=repo,
            subcommand="task-work",
            read_allowlist=[tmp_path],
        )

        assert len(result.paths) == 1
        assert result.paths[0].endswith("docs/random/not_in_any_filter.md")

    def test_flags_pair_each_path_with_context_argument(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, "origin")
        _touch(repo, "a.md")
        _touch(repo, "b.md")
        _write_manifest(
            repo,
            {
                "repo": "origin",
                "internal_docs": {"always_include": ["a.md", "b.md"]},
            },
        )

        result = resolve_context_flags(
            repo_path=repo,
            subcommand="feature-spec",
            read_allowlist=[tmp_path],
        )

        assert len(result.flags) == 4
        assert result.flags[0] == "--context"
        assert result.flags[2] == "--context"
        assert result.flags[1] == result.paths[0]
        assert result.flags[3] == result.paths[1]


# ---------------------------------------------------------------------------
# AC-005: depth-cap chase
# ---------------------------------------------------------------------------


class TestDepthCapChase:
    """AC-005 — dependency chase up to depth 2 then warns."""

    def _build_chain(self, tmp_path: Path, length: int) -> Path:
        """Build a linear dep chain of ``length`` repos: r0 → r1 → … → r{length-1}.

        Each repo contains a single ``docs/specs/<repo>-spec.md`` file
        and a manifest declaring the next repo as its sole dependency.
        Returns the path to ``r0``.
        """
        repos: list[Path] = []
        for i in range(length):
            r = _make_repo(tmp_path, f"r{i}")
            _touch(r, f"docs/specs/r{i}-spec.md")
            repos.append(r)

        for i, r in enumerate(repos):
            manifest: dict[str, Any] = {"repo": f"r{i}"}
            if i + 1 < length:
                manifest["dependencies"] = {
                    f"r{i + 1}": {
                        "path": f"../r{i + 1}",
                        "key_docs": [
                            {
                                "path": f"docs/specs/r{i + 1}-spec.md",
                                "category": "specs",
                            }
                        ],
                    }
                }
            _write_manifest(r, manifest)

        return repos[0]

    def test_depth_one_chase_collects_dep_doc(self, tmp_path: Path) -> None:
        origin = self._build_chain(tmp_path, length=2)
        result = resolve_context_flags(
            repo_path=origin,
            subcommand="feature-spec",
            read_allowlist=[tmp_path],
        )
        assert any(p.endswith("r1-spec.md") for p in result.paths)
        # No depth-cap warning because we never tried to go past the cap.
        assert not any(
            w.code == "context_manifest_cycle_detected"
            and w.details.get("reason") == "depth_cap"
            for w in result.warnings
        )

    def test_depth_two_chase_collects_dep_doc(self, tmp_path: Path) -> None:
        origin = self._build_chain(tmp_path, length=3)
        result = resolve_context_flags(
            repo_path=origin,
            subcommand="feature-spec",
            read_allowlist=[tmp_path],
        )
        assert any(p.endswith("r1-spec.md") for p in result.paths)
        assert any(p.endswith("r2-spec.md") for p in result.paths)

    def test_depth_three_stops_with_depth_cap_warning(self, tmp_path: Path) -> None:
        # Chain of 4 repos: r0→r1→r2→r3. r3-spec.md would be at depth 3 —
        # collected when its parent (r2's manifest) is BFS-visited at
        # depth 2 — but going *to* r3's manifest (depth 3) is rejected.
        origin = self._build_chain(tmp_path, length=4)
        result = resolve_context_flags(
            repo_path=origin,
            subcommand="feature-spec",
            read_allowlist=[tmp_path],
        )
        depth_cap_warnings = [
            w
            for w in result.warnings
            if w.code == "context_manifest_cycle_detected"
            and w.details.get("reason") == "depth_cap"
        ]
        assert len(depth_cap_warnings) >= 1


# ---------------------------------------------------------------------------
# AC-006: cycle detection
# ---------------------------------------------------------------------------


class TestCycleDetection:
    """AC-006 — already-visited manifest in chain is not re-visited."""

    def test_two_node_cycle_resolves_safely(self, tmp_path: Path) -> None:
        a = _make_repo(tmp_path, "a")
        b = _make_repo(tmp_path, "b")
        _touch(a, "docs/specs/a-spec.md")
        _touch(b, "docs/specs/b-spec.md")

        _write_manifest(
            a,
            {
                "repo": "a",
                "dependencies": {
                    "b": {
                        "path": "../b",
                        "key_docs": [
                            {
                                "path": "docs/specs/b-spec.md",
                                "category": "specs",
                            }
                        ],
                    }
                },
            },
        )
        _write_manifest(
            b,
            {
                "repo": "b",
                "dependencies": {
                    "a": {
                        "path": "../a",
                        "key_docs": [
                            {
                                "path": "docs/specs/a-spec.md",
                                "category": "specs",
                            }
                        ],
                    }
                },
            },
        )

        result = resolve_context_flags(
            repo_path=a,
            subcommand="feature-spec",
            read_allowlist=[tmp_path],
        )

        # Cycle does not crash; both docs surfaced exactly once.
        assert sum(1 for p in result.paths if p.endswith("a-spec.md")) == 1
        assert sum(1 for p in result.paths if p.endswith("b-spec.md")) == 1

        cycle_warnings = [
            w
            for w in result.warnings
            if w.code == "context_manifest_cycle_detected"
            and w.details.get("reason") == "cycle"
        ]
        assert len(cycle_warnings) >= 1


# ---------------------------------------------------------------------------
# AC-007: read_allowlist exclusion
# ---------------------------------------------------------------------------


class TestReadAllowlistExclusion:
    """AC-007 — docs outside the read_allowlist are omitted with a warning."""

    def test_doc_outside_allowlist_is_omitted_and_warned(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, "origin")
        outside = _make_repo(tmp_path, "outside")
        _touch(outside, "secret.md")
        # Reference outside-repo file via an explicit relative path that
        # *resolves inside the origin repo* — to isolate the allowlist
        # check we keep the doc inside the repo but allowlist a sibling
        # directory that excludes it.
        _touch(repo, "docs/specs/repo-spec.md")
        _write_manifest(
            repo,
            {
                "repo": "origin",
                "internal_docs": {"always_include": ["docs/specs/repo-spec.md"]},
            },
        )

        # Allowlist deliberately excludes ``repo``.
        result = resolve_context_flags(
            repo_path=repo,
            subcommand="feature-spec",
            read_allowlist=[outside],
        )

        assert result.paths == []
        omit_warnings = [
            w
            for w in result.warnings
            if w.code == "context_manifest_path_outside_allowlist"
        ]
        assert len(omit_warnings) == 1
        assert "repo-spec.md" in omit_warnings[0].details["path"]


# ---------------------------------------------------------------------------
# AC-008: doc path escapes repo root
# ---------------------------------------------------------------------------


class TestDocPathEscapesRepoRoot:
    """AC-008 — manifest entry that escapes the repo root is rejected."""

    def test_path_with_dotdot_escape_is_rejected(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, "origin")
        # Sibling file outside the repo root.
        sibling = tmp_path / "sibling-secret.md"
        sibling.write_text("# secret\n", encoding="utf-8")

        _write_manifest(
            repo,
            {
                "repo": "origin",
                "internal_docs": {"always_include": ["../sibling-secret.md"]},
            },
        )

        result = resolve_context_flags(
            repo_path=repo,
            subcommand="feature-spec",
            read_allowlist=[tmp_path],
        )

        assert result.paths == []
        escape_warnings = [
            w for w in result.warnings if w.code == "context_manifest_path_outside_repo"
        ]
        assert len(escape_warnings) == 1
        assert "sibling-secret.md" in escape_warnings[0].details["path"]


# ---------------------------------------------------------------------------
# AC-009: statelessness
# ---------------------------------------------------------------------------


class TestStatelessness:
    """AC-009 — concurrent calls produce independent ResolvedContext values."""

    def test_two_calls_return_independent_lists(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, "origin")
        _touch(repo, "a.md")
        _write_manifest(
            repo,
            {
                "repo": "origin",
                "internal_docs": {"always_include": ["a.md"]},
            },
        )

        first = resolve_context_flags(
            repo_path=repo,
            subcommand="feature-spec",
            read_allowlist=[tmp_path],
        )
        second = resolve_context_flags(
            repo_path=repo,
            subcommand="feature-spec",
            read_allowlist=[tmp_path],
        )

        assert first is not second
        assert first.flags is not second.flags
        assert first.paths is not second.paths
        assert first.warnings is not second.warnings

        first.paths.append("/tmp/should-not-leak.md")
        first.flags.append("/tmp/should-not-leak.md")
        assert "/tmp/should-not-leak.md" not in second.paths
        assert "/tmp/should-not-leak.md" not in second.flags


# ---------------------------------------------------------------------------
# AC-010: stable order
# ---------------------------------------------------------------------------


class TestStableOrder:
    """AC-010 — always_include first, then categories, then declaration order."""

    def test_always_include_then_categories_then_declaration_order(
        self, tmp_path: Path
    ) -> None:
        repo = _make_repo(tmp_path, "origin")
        dep = _make_repo(tmp_path, "dep")
        _touch(repo, "docs/always.md")
        _touch(dep, "docs/arch.md")
        _touch(dep, "docs/spec1.md")
        _touch(dep, "docs/spec2.md")
        _touch(dep, "docs/decision.md")

        _write_manifest(
            repo,
            {
                "repo": "origin",
                "internal_docs": {"always_include": ["docs/always.md"]},
                "dependencies": {
                    "dep": {
                        "path": "../dep",
                        # Declaration order intentionally mixes categories.
                        "key_docs": [
                            {"path": "docs/spec1.md", "category": "specs"},
                            {
                                "path": "docs/decision.md",
                                "category": "decisions",
                            },
                            {"path": "docs/arch.md", "category": "architecture"},
                            {"path": "docs/spec2.md", "category": "specs"},
                        ],
                    }
                },
            },
        )

        # feature-plan filter = {specs, decisions, architecture}.
        result = resolve_context_flags(
            repo_path=repo,
            subcommand="feature-plan",
            read_allowlist=[tmp_path],
        )

        names = [Path(p).name for p in result.paths]
        # always_include first.
        assert names[0] == "always.md"
        # Architecture comes before decisions which comes before specs in
        # the canonical _CATEGORY_ORDER.
        assert names.index("arch.md") < names.index("decision.md")
        assert names.index("decision.md") < names.index("spec1.md")
        # Within specs, declaration order preserved (spec1 before spec2).
        assert names.index("spec1.md") < names.index("spec2.md")


# ---------------------------------------------------------------------------
# AC-011: symlinks followed before allowlist check
# ---------------------------------------------------------------------------


class TestSymlinksFollowedBeforeAllowlistCheck:
    """AC-011 — symlinks are resolved before the allowlist check."""

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="symlinks require admin on Windows",
    )
    def test_symlink_target_outside_allowlist_is_rejected(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, "origin")
        outside_dir = tmp_path / "outside_repo_target"
        outside_dir.mkdir()
        real_target = outside_dir / "real.md"
        real_target.write_text("# real\n", encoding="utf-8")

        # Place a symlink *inside* the repo pointing outside the repo.
        symlink_path = repo / "symlinked.md"
        symlink_path.symlink_to(real_target)

        _write_manifest(
            repo,
            {
                "repo": "origin",
                "internal_docs": {"always_include": ["symlinked.md"]},
            },
        )

        # The repo itself is on the allowlist; the symlink target is
        # NOT — and resolution must follow the symlink before checking.
        result = resolve_context_flags(
            repo_path=repo,
            subcommand="feature-spec",
            read_allowlist=[repo],
        )

        # The symlink target is outside the *repo root* once resolved,
        # so the path-outside-repo check fires first and rejects it.
        assert result.paths == []
        assert any(
            w.code
            in (
                "context_manifest_path_outside_repo",
                "context_manifest_path_outside_allowlist",
            )
            for w in result.warnings
        )


# ---------------------------------------------------------------------------
# AC-012: KeyError on Graphiti subcommands
# ---------------------------------------------------------------------------


class TestGraphitiSubcommandsRaiseKeyError:
    """AC-012 — Graphiti subcommands are programmer-error, not graceful."""

    @pytest.mark.parametrize(
        "graphiti_subcommand",
        [
            "graphiti-search",
            "graphiti-export",
            "graphiti-import",
            "graphiti-prune",
        ],
    )
    def test_resolver_raises_keyerror_for_graphiti_subcommands(
        self, graphiti_subcommand: str, tmp_path: Path
    ) -> None:
        with pytest.raises(KeyError):
            resolve_context_flags(
                repo_path=tmp_path,
                subcommand=graphiti_subcommand,
                read_allowlist=[tmp_path],
            )

    def test_resolver_raises_keyerror_for_unknown_subcommand(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(KeyError):
            resolve_context_flags(
                repo_path=tmp_path,
                subcommand="completely-unknown-subcommand",
                read_allowlist=[tmp_path],
            )
