"""Tests for :mod:`forge.gating.identity` (TASK-CGCP-003).

One ``Test*`` class per acceptance criterion in
``tasks/backlog/TASK-CGCP-003-request-id-derivation-helper.md``:

* AC-001 — keyword-only signature; pure function with no I/O / no
  hidden state.
* AC-002 — same inputs produce identical output across calls
  (manual property test exercising a generated parameter sweep, since
  ``hypothesis`` is not in the dev dependency set).
* AC-003 — different ``attempt_count`` values produce different
  output (refresh distinguishability for ``API §7``).
* AC-004 — output format is stable, documented, and URL-safe; no
  characters that would break NATS subject parsing if embedded.
* AC-005 — negative ``attempt_count`` raises ``ValueError``.
* AC-006 — empty ``build_id`` or ``stage_label`` raises ``ValueError``.
* AC-007 — module imports nothing from ``nats_core``, ``nats-py``, or
  ``langgraph``.
"""

from __future__ import annotations

import inspect
import re
import string

import pytest

from forge.gating import derive_request_id as reexported_derive
from forge.gating.identity import derive_request_id


# Characters NATS treats as subject delimiters / wildcards. None of
# these may appear anywhere in the derived request_id.
_NATS_FORBIDDEN_CHARS = {".", "*", ">"} | set(string.whitespace)

# Allowed alphabet in the output: RFC 3986 unreserved minus ``.`` /
# ``~`` (which the encoder additionally percent-encodes), the percent
# sign used for percent-encoding, the digits used by ``attempt_count``,
# and the ``:`` separator.
_ALLOWED_OUTPUT_RE = re.compile(r"^[A-Za-z0-9_\-:%]+$")


# ---------------------------------------------------------------- AC-001 #


class TestSignatureIsKeywordOnlyAndPure:
    """AC-001: keyword-only signature; pure function with no I/O."""

    def test_signature_is_keyword_only(self) -> None:
        sig = inspect.signature(derive_request_id)
        assert list(sig.parameters) == ["build_id", "stage_label", "attempt_count"]
        for name, param in sig.parameters.items():
            assert param.kind is inspect.Parameter.KEYWORD_ONLY, (
                f"parameter {name!r} should be keyword-only, got {param.kind!r}"
            )

    def test_positional_arguments_are_rejected(self) -> None:
        with pytest.raises(TypeError):
            # Mypy would reject this too; the runtime check is what we
            # exercise here.
            derive_request_id("build", "Stage", 0)  # type: ignore[misc]

    def test_returns_str(self) -> None:
        result = derive_request_id(
            build_id="b", stage_label="s", attempt_count=0
        )
        assert isinstance(result, str)

    def test_reexported_from_package(self) -> None:
        # The public ``forge.gating`` re-export shim must surface the
        # same callable so callers can ``from forge.gating import
        # derive_request_id``.
        assert reexported_derive is derive_request_id


# ---------------------------------------------------------------- AC-002 #


class TestSameInputsProduceIdenticalOutput:
    """AC-002: idempotency — equal inputs ⇒ equal outputs across calls."""

    @pytest.mark.parametrize(
        ("build_id", "stage_label", "attempt_count"),
        [
            ("build-1", "Architecture Review", 0),
            ("build-1", "Architecture Review", 7),
            ("BUILD_42", "Code Quality Gate", 1),
            ("b", "s", 0),
            ("very-long-build-id-with-dashes-and_underscores", "X", 99),
            ("build/with/slashes", "stage:with:colons", 3),
            ("unicode-buïld", "Stäge Räview", 2),
        ],
    )
    def test_repeated_calls_agree(
        self, build_id: str, stage_label: str, attempt_count: int
    ) -> None:
        first = derive_request_id(
            build_id=build_id,
            stage_label=stage_label,
            attempt_count=attempt_count,
        )
        for _ in range(5):
            again = derive_request_id(
                build_id=build_id,
                stage_label=stage_label,
                attempt_count=attempt_count,
            )
            assert again == first

    def test_property_sweep_is_idempotent(self) -> None:
        # Manual property test: a generated parameter sweep stands in
        # for ``hypothesis`` (not in the dev deps). Each combination
        # must agree on a second call.
        builds = ["b", "build-1", "BUILD_2", "b/c", "b c", "ünïcode"]
        labels = [
            "Architecture Review",
            "code-quality",
            "Stage:With:Colons",
            "x",
        ]
        counts = [0, 1, 2, 17, 12345]
        for build in builds:
            for label in labels:
                for count in counts:
                    a = derive_request_id(
                        build_id=build,
                        stage_label=label,
                        attempt_count=count,
                    )
                    b = derive_request_id(
                        build_id=build,
                        stage_label=label,
                        attempt_count=count,
                    )
                    assert a == b


# ---------------------------------------------------------------- AC-003 #


class TestAttemptCountChangesOutput:
    """AC-003: different ``attempt_count`` ⇒ different request_id."""

    def test_consecutive_attempt_counts_differ(self) -> None:
        ids = {
            derive_request_id(
                build_id="build-1",
                stage_label="Architecture Review",
                attempt_count=n,
            )
            for n in range(50)
        }
        assert len(ids) == 50

    @pytest.mark.parametrize(
        ("a", "b"),
        [(0, 1), (1, 2), (5, 10), (0, 100), (12, 13)],
    )
    def test_pairwise_distinct(self, a: int, b: int) -> None:
        x = derive_request_id(
            build_id="b", stage_label="s", attempt_count=a
        )
        y = derive_request_id(
            build_id="b", stage_label="s", attempt_count=b
        )
        assert x != y


# ---------------------------------------------------------------- AC-004 #


class TestOutputIsUrlSafeAndNatsSafe:
    """AC-004: output is stable, URL-safe, and NATS-subject-safe."""

    def test_stable_documented_format(self) -> None:
        # The exact wire-contract format. Spaces become ``%20`` (URL
        # encoding); ``-`` is RFC-3986 unreserved and survives.
        result = derive_request_id(
            build_id="build-1",
            stage_label="Architecture Review",
            attempt_count=0,
        )
        assert result == "build-1:Architecture%20Review:0"

    @pytest.mark.parametrize(
        ("build_id", "stage_label", "attempt_count"),
        [
            ("build-1", "Architecture Review", 0),
            ("build with spaces", "Stage", 1),
            ("b/c?d#e", "p.q*r>s", 2),
            ("build.with.dots", "stage~with~tilde", 3),
            ("ünïcode", "läbel", 4),
            ("a", "b", 0),
        ],
    )
    def test_output_alphabet_is_restricted(
        self, build_id: str, stage_label: str, attempt_count: int
    ) -> None:
        result = derive_request_id(
            build_id=build_id,
            stage_label=stage_label,
            attempt_count=attempt_count,
        )
        assert _ALLOWED_OUTPUT_RE.fullmatch(result), (
            f"output {result!r} contains characters outside the allowed alphabet"
        )

    @pytest.mark.parametrize(
        ("build_id", "stage_label"),
        [
            ("build with spaces", "Stage"),
            ("b\tc", "s"),
            ("b\nc", "s"),
            ("b.c", "s"),
            ("b*c", "s"),
            ("b>c", "s"),
            ("b", "stage with spaces"),
            ("b", "stage.with.dots"),
            ("b", "stage>with>arrow"),
            ("b", "stage*with*star"),
            ("b", "stage~with~tilde"),
        ],
    )
    def test_no_nats_subject_breakers(
        self, build_id: str, stage_label: str
    ) -> None:
        result = derive_request_id(
            build_id=build_id, stage_label=stage_label, attempt_count=0
        )
        for ch in _NATS_FORBIDDEN_CHARS:
            assert ch not in result, (
                f"forbidden NATS subject char {ch!r} present in {result!r}"
            )

    def test_separator_layout(self) -> None:
        # Three colon-separated tokens; ``:`` must not appear inside
        # any encoded component (it is in the gen-delims set, so
        # ``urllib.parse.quote(safe="")`` escapes it).
        result = derive_request_id(
            build_id="b:1", stage_label="s:2", attempt_count=3
        )
        assert result.count(":") == 2
        encoded_build, encoded_stage, encoded_count = result.split(":")
        assert encoded_build == "b%3A1"
        assert encoded_stage == "s%3A2"
        assert encoded_count == "3"


# ---------------------------------------------------------------- AC-005 #


class TestNegativeAttemptCountRaises:
    """AC-005: negative ``attempt_count`` raises ``ValueError``."""

    @pytest.mark.parametrize("count", [-1, -2, -100, -(2**31)])
    def test_negative_value_rejected(self, count: int) -> None:
        with pytest.raises(ValueError):
            derive_request_id(
                build_id="b", stage_label="s", attempt_count=count
            )

    def test_zero_is_accepted(self) -> None:
        # Boundary: zero is the natural "first attempt" value and must
        # not raise.
        derive_request_id(build_id="b", stage_label="s", attempt_count=0)


# ---------------------------------------------------------------- AC-006 #


class TestEmptyComponentsRaise:
    """AC-006: empty ``build_id`` or ``stage_label`` raises ``ValueError``."""

    def test_empty_build_id_raises(self) -> None:
        with pytest.raises(ValueError):
            derive_request_id(
                build_id="", stage_label="s", attempt_count=0
            )

    def test_empty_stage_label_raises(self) -> None:
        with pytest.raises(ValueError):
            derive_request_id(
                build_id="b", stage_label="", attempt_count=0
            )

    def test_both_empty_raises(self) -> None:
        with pytest.raises(ValueError):
            derive_request_id(
                build_id="", stage_label="", attempt_count=0
            )


# ---------------------------------------------------------------- AC-007 #


class TestModuleImportsAreDomainPure:
    """AC-007: identity module imports nothing transport-tied."""

    @staticmethod
    def _imported_module_names(module: object) -> set[str]:
        """Return the set of root module names imported by ``module``.

        Parses the module source with :mod:`ast` so the check ignores
        docstrings and comments (which may legitimately mention
        forbidden module names while explaining the *absence* of those
        imports).
        """
        import ast

        source = inspect.getsource(module)
        tree = ast.parse(source)
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module is not None:
                    names.add(node.module.split(".")[0])
        return names

    def test_no_forbidden_imports(self) -> None:
        from forge.gating import identity as identity_mod

        forbidden = {"nats_core", "nats", "langgraph"}
        imported = self._imported_module_names(identity_mod)
        leaked = forbidden & imported
        assert not leaked, (
            f"identity module must not import {leaked!r}; full import "
            f"set was {imported!r}"
        )
