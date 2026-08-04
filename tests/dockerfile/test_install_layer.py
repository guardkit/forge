"""Static lint tests for the Dockerfile install layer + build script.

These tests cover the additions made by TASK-F009-005 — the BuildKit
``nats-core`` named-context wiring, the install layer (``pip install
.[providers]``), the runtime venv copy, the HEALTHCHECK directive, and
``scripts/build-image.sh``. They parse files with regular expressions
rather than invoking ``docker build``/``docker run`` so they can run as
fast unit tests without a Docker daemon present.

The end-to-end build smoke (``bash scripts/build-image.sh`` on a fresh
clone with the sibling ``nats-core`` working tree present) is owned by
the BDD/integration tier of the FEAT-FORGE-009 suite (T6) and lives
outside this module.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest

# Resolve the repository root deterministically:
# tests/dockerfile/test_install_layer.py -> two parents up.
REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE_PATH = REPO_ROOT / "Dockerfile"
BUILD_SCRIPT_PATH = REPO_ROOT / "scripts" / "build-image.sh"
ORACLE_SCRIPT_PATH = REPO_ROOT / "scripts" / "verify-forge-oracles.sh"

# Canonical BuildKit invocation per Contract A (TASK-F009-005,
# updated by TASK-FORGE-FRR-003). The exact string must literal-match
# the runbook ``RUNBOOK-FEAT-FORGE-008-validation.md`` §6.1 so a
# copy-paste from one file to the other reproduces the build
# (LES1 §3 DKRX). Do not soften this match — the literal-grep is the
# contract. Note: the invocation runs from inside forge/ (the script
# cd's there before invoking buildx), so the Dockerfile path is
# ``Dockerfile`` (no ``forge/`` prefix) and the context root is ``.``
# (forge/). ``../nats-core`` resolves to the sibling working tree.
CONTRACT_A_INVOCATION = (
    "docker buildx build --build-context nats-core=../nats-core "
    "--build-context guardkit=../guardkit "
    "--build-context fleet-memory=../fleet-memory "
    "--build-context guardkitfactory=../guardkitfactory "
    "-t forge:production-validation -f Dockerfile ."
)

# Layout-validation gate (R3 mitigation). Must appear verbatim in the
# Dockerfile so a stale/empty ``nats-core`` checkout fails fast with a
# named diagnostic instead of producing a misleading pip stack trace.
NATS_CORE_LAYOUT_GATE = (
    'RUN test -d /tmp/nats-core/src/nats_core '
    '|| (echo "nats-core layout invalid" >&2; exit 1)'
)


@pytest.fixture(scope="module")
def dockerfile_text() -> str:
    """Return the Dockerfile contents, failing fast if it is missing."""
    if not DOCKERFILE_PATH.is_file():
        pytest.fail(
            f"Dockerfile not found at {DOCKERFILE_PATH}. "
            "TASK-F009-005 extends the T2 Dockerfile skeleton."
        )
    return DOCKERFILE_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def build_script_text() -> str:
    """Return scripts/build-image.sh contents, failing fast if missing."""
    if not BUILD_SCRIPT_PATH.is_file():
        pytest.fail(
            f"scripts/build-image.sh not found at {BUILD_SCRIPT_PATH}. "
            "TASK-F009-005 introduces this canonical Contract A producer."
        )
    return BUILD_SCRIPT_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def oracle_script_text() -> str:
    """Return scripts/verify-forge-oracles.sh contents, failing fast if missing."""
    if not ORACLE_SCRIPT_PATH.is_file():
        pytest.fail(
            f"scripts/verify-forge-oracles.sh not found at {ORACLE_SCRIPT_PATH}. "
            "Every build must prove its in-image oracles before it can ship."
        )
    return ORACLE_SCRIPT_PATH.read_text(encoding="utf-8")


def _oracle_prog_body(oracle_script_text: str, name: str) -> str:
    """Slice one ``read -r -d '' <NAME>_PROG <<'PY' … PY`` heredoc body out.

    The oracle script carries each in-image probe as a heredoc-captured Python
    program which is then handed to ``docker run … python -c``. Tests that pin a
    clause's *content* slice its own body first so a string present in a
    neighbouring clause cannot produce a false match.
    """
    match = re.search(
        rf"^read\s+-r\s+-d\s+''\s+{re.escape(name)}\s+<<'PY'[^\n]*\n"
        r"(?P<body>.*?)\n^PY$",
        oracle_script_text,
        re.MULTILINE | re.DOTALL,
    )
    assert match, (
        f"scripts/verify-forge-oracles.sh must declare the {name} probe using "
        f"the established heredoc idiom: ``read -r -d '' {name} <<'PY'`` … ``PY``"
    )
    return match.group("body")


def _runtime_stage_body(dockerfile_text: str) -> str:
    """Slice the runtime stage body out of the Dockerfile.

    The runtime stage starts at the second ``FROM ... AS runtime`` and
    extends to end-of-file. Tests that need to assert directive ordering
    against the runtime stage call this helper to avoid false matches in
    the builder stage.
    """
    match = re.search(
        r"^FROM\s+python:3\.14-slim-bookworm@sha256:[0-9a-f]{64}"
        r"\s+AS\s+runtime\b(?P<body>.*)\Z",
        dockerfile_text,
        re.MULTILINE | re.IGNORECASE | re.DOTALL,
    )
    assert match, "Could not locate the runtime stage body"
    return match.group("body")


class TestBuildScriptExists:
    """AC: ``scripts/build-image.sh`` exists and is executable."""

    def test_build_script_present(self) -> None:
        assert (
            BUILD_SCRIPT_PATH.is_file()
        ), f"scripts/build-image.sh must exist at {BUILD_SCRIPT_PATH}"

    def test_build_script_is_executable(self) -> None:
        # POSIX exec bit must be set so ``bash scripts/build-image.sh``
        # and direct invocation both work without a host-side chmod.
        mode = BUILD_SCRIPT_PATH.stat().st_mode
        any_exec_bit = mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        assert any_exec_bit, (
            "scripts/build-image.sh must have the executable bit set "
            f"(mode={oct(mode)})"
        )

    def test_build_script_starts_with_shebang(self, build_script_text: str) -> None:
        # First line must be a bash/sh shebang so the kernel can invoke
        # the script directly without ``bash`` prefix.
        first_line = build_script_text.splitlines()[0]
        assert first_line.startswith("#!"), (
            f"build-image.sh must start with a shebang, got {first_line!r}"
        )


class TestBuildScriptContractA:
    """AC: build script runs the canonical Contract A BuildKit invocation."""

    def test_contract_a_invocation_literal_match(
        self, build_script_text: str
    ) -> None:
        # Case-sensitive substring grep against the canonical command.
        # The runbook copy-paste assertion fails the moment any token
        # drifts (image tag, context name, dockerfile path, ...).
        assert CONTRACT_A_INVOCATION in build_script_text, (
            "scripts/build-image.sh must contain the canonical Contract A "
            f"invocation: {CONTRACT_A_INVOCATION!r}"
        )

    def test_script_changes_into_forge_directory(
        self, build_script_text: str
    ) -> None:
        # The relative ``../nats-core`` path is resolved against the
        # buildx invocation cwd. From inside forge/, ``../nats-core``
        # dereferences to the sibling working tree (the right place);
        # from forge's PARENT the same relative path would dereference
        # to the grandparent's nats-core (wrong) — that was the bug
        # TASK-FORGE-FRR-003 fixed. The canonical incantation cd's one
        # parent up from the script's dirname (script lives at
        # forge/scripts/build-image.sh — one parent up = forge/).
        assert re.search(
            r'cd\s+"\$\(dirname\s+"\$0"\)/\.\."',
            build_script_text,
        ), (
            "scripts/build-image.sh must cd into forge/ via "
            'cd "$(dirname "$0")/.." before invoking buildx so '
            "``../nats-core`` resolves to the sibling working tree"
        )

    def test_script_uses_strict_bash_modes(self, build_script_text: str) -> None:
        # ``set -euo pipefail`` (or equivalent) makes the script fail
        # fast on the first error rather than continuing past a failed
        # ``cd``/``docker`` and producing a misleading exit code.
        assert re.search(
            r"^set\s+-[a-z]*e[a-z]*\b", build_script_text, re.MULTILINE
        ), "scripts/build-image.sh must enable errexit (set -e or set -euo pipefail)"


class TestBuilderStageNatsCoreContext:
    """AC: BuildKit named context wired into the builder stage."""

    def test_copy_from_nats_core_named_context(self, dockerfile_text: str) -> None:
        # ``COPY --from=nats-core / /tmp/nats-core`` mirrors the entire
        # context root into a known absolute path so subsequent install
        # commands can reference ``/tmp/nats-core`` without depending
        # on the build cwd.
        assert re.search(
            r"^COPY\s+--from=nats-core\s+/\s+/tmp/nats-core\s*$",
            dockerfile_text,
            re.MULTILINE,
        ), (
            "Builder stage must contain "
            "``COPY --from=nats-core / /tmp/nats-core``"
        )

    def test_layout_validation_gate_present(self, dockerfile_text: str) -> None:
        # R3 mitigation: refuse to proceed if the COPYed working tree is
        # missing the expected ``src/nats_core`` package layout. The
        # error message is part of the contract — operators grep for it.
        assert NATS_CORE_LAYOUT_GATE in dockerfile_text, (
            "Builder stage must contain the literal layout-validation gate: "
            f"{NATS_CORE_LAYOUT_GATE!r}"
        )

    def test_layout_gate_runs_before_pip_install(
        self, dockerfile_text: str
    ) -> None:
        # The early-fail must precede the ``pip install`` RUN line so
        # its diagnostic surfaces before pip emits a wall of resolution
        # noise. We anchor on the literal ``RUN pip install
        # .[providers]`` directive rather than the bare substring so
        # the test is not confused by mentions of the same string in
        # leading comments/docstrings.
        gate_idx = dockerfile_text.find(NATS_CORE_LAYOUT_GATE)
        pip_match = re.search(
            r"^RUN\s+pip\s+install\s+\.\[providers,memory\]\s*$",
            dockerfile_text,
            re.MULTILINE,
        )
        assert gate_idx != -1
        assert pip_match, (
            "Dockerfile must declare ``RUN pip install .[providers,memory]``"
        )
        assert gate_idx < pip_match.start(), (
            "Layout-validation gate must appear before the "
            "``RUN pip install .[providers,memory]`` directive so it fails fast"
        )


class TestBuilderStageInstallLayer:
    """AC-E: ``pip install .[providers,memory]`` literal-matches runbook §0.4 / §6.1."""

    def test_pip_install_providers_literal_match(
        self, dockerfile_text: str
    ) -> None:
        # B3 scenario: the runbook validation steps and the Dockerfile
        # share this exact install command. Drift here breaks the
        # equivalence claim of FEAT-FORGE-008.
        assert "pip install .[providers,memory]" in dockerfile_text, (
            "Builder stage must run ``pip install .[providers,memory]`` "
            "(literal-match to runbook §0.4 and §6.1)"
        )

    def test_fleet_memory_installed_from_buildkit_context(
        self, dockerfile_text: str
    ) -> None:
        # fleet-memory has no PyPI distribution at all, so the ``memory``
        # extra can only resolve from the COPYed BuildKit context — the
        # same posture nats-core takes against its malformed 0.2.0 wheel.
        assert re.search(
            r"^COPY\s+--from=fleet-memory\s+/\s+/tmp/fleet-memory\s*$",
            dockerfile_text,
            re.MULTILINE,
        ), (
            "Builder stage must contain "
            "``COPY --from=fleet-memory / /tmp/fleet-memory``"
        )
        assert re.search(
            r"\b(?:uv\s+)?pip\s+install\s+(?:-e\s+)?/tmp/fleet-memory\b",
            dockerfile_text,
        ), (
            "Builder stage must install fleet-memory from the BuildKit "
            "context at /tmp/fleet-memory (not from PyPI)"
        )
        assert (
            'RUN test -d /tmp/fleet-memory/src/fleet_memory '
            '|| (echo "fleet-memory layout invalid" >&2; exit 1)'
        ) in dockerfile_text, (
            "Builder stage must carry the fleet-memory layout-validation "
            "gate (mirrors the nats-core gate)"
        )

    def test_guardkitfactory_installed_from_buildkit_context(
        self, dockerfile_text: str
    ) -> None:
        # guardkitfactory is the LangGraph leg harness that guardkit's
        # ``GUARDKIT_HARNESS=langgraph`` path imports at runtime. The
        # conductor's first real leg died in-container with
        # ``GUARDKIT_HARNESS=langgraph but guardkitfactory is not importable``
        # because the image baked guardkit but not its harness runtime. Like
        # fleet-memory it has no PyPI distribution, so the COPYed BuildKit
        # context is the only source.
        assert re.search(
            r"^COPY\s+--from=guardkitfactory\s+/\s+/tmp/guardkitfactory\s*$",
            dockerfile_text,
            re.MULTILINE,
        ), (
            "Builder stage must contain "
            "``COPY --from=guardkitfactory / /tmp/guardkitfactory``"
        )
        assert re.search(
            r"\b(?:uv\s+)?pip\s+install\s+(?:-e\s+)?/tmp/guardkitfactory\b",
            dockerfile_text,
        ), (
            "Builder stage must install guardkitfactory from the BuildKit "
            "context at /tmp/guardkitfactory (not from PyPI)"
        )
        assert (
            'RUN test -d /tmp/guardkitfactory/src/guardkitfactory '
            '|| (echo "guardkitfactory layout invalid" >&2; exit 1)'
        ) in dockerfile_text, (
            "Builder stage must carry the guardkitfactory layout-validation "
            "gate (mirrors the nats-core gate)"
        )

    def test_guardkitfactory_installs_after_forge(
        self, dockerfile_text: str
    ) -> None:
        # ORDER IS LOAD-BEARING and the reverse of the nats-core /
        # fleet-memory posture. forge pins ``deepagents>=0.5.3,<0.6``;
        # guardkitfactory requires ``deepagents>=0.6.7,<1``. The pair is
        # unsatisfiable in one venv and pip's sequential installs make the
        # LAST install the winner, so guardkitfactory MUST come after
        # ``pip install .[providers,memory]`` — otherwise forge's install
        # downgrades deepagents to 0.5.x, where ``create_deep_agent`` has no
        # ``state_schema`` keyword (upstream 0.6.6) and the harness imports
        # cleanly but dies at call time. See the Dockerfile comment block for
        # the full reasoning.
        #
        # Ordering alone does NOT determine which deepagents lands — that is
        # the band pin asserted by ``test_guardkitfactory_install_pins_
        # deepagents_band`` below. Keep the two facts separate: this test
        # says "guardkitfactory's floor wins"; that one says "and the winner
        # is the 0.6.x the estate is actually developed against".
        forge_match = re.search(
            r"^RUN\s+pip\s+install\s+\.\[providers,memory\]\s*$",
            dockerfile_text,
            re.MULTILINE,
        )
        gkf_match = re.search(
            r"^RUN\s+pip\s+install\s+/tmp/guardkitfactory\b.*$",
            dockerfile_text,
            re.MULTILINE,
        )
        assert forge_match, (
            "Dockerfile must declare ``RUN pip install .[providers,memory]``"
        )
        assert gkf_match, (
            "Dockerfile must declare ``RUN pip install /tmp/guardkitfactory``"
        )
        assert forge_match.start() < gkf_match.start(), (
            "``RUN pip install /tmp/guardkitfactory`` must come AFTER "
            "``RUN pip install .[providers,memory]`` so guardkitfactory's "
            "deepagents>=0.6.7 floor wins the venv (forge's declared "
            "deepagents<0.6 pin cannot be honoured at the same time)"
        )

    def test_guardkitfactory_install_pins_deepagents_band(
        self, dockerfile_text: str
    ) -> None:
        # THE BARE INSTALL IS A TRAP. guardkitfactory declares
        # ``deepagents>=0.6.7,<1``; pip resolves that to the NEWEST match on
        # PyPI, and the band runs 0.6.7…0.6.12 then 0.7.0…0.7.3 — so a bare
        # ``pip install /tmp/guardkitfactory`` lands 0.7.3, not the 0.6.7 the
        # estate is developed against (guardkitfactory's own .venv carries
        # 0.6.7).
        #
        # 0.7.x is a SILENT regression for the daemon: it deleted the module
        # constant ``ASYNC_TASK_SYSTEM_PROMPT`` and changed
        # ``AsyncSubAgentMiddleware.__init__``'s ``system_prompt`` default
        # from that constant to ``None``. src/forge/cli/serve.py constructs
        # ``AsyncSubAgentMiddleware(async_subagents=[spec])`` with no
        # ``system_prompt``, so under 0.7.x the supervisor silently loses the
        # whole async-subagent operating protocol. Nothing raises. It also
        # cascade-upgrades langchain / langchain-core / langchain-anthropic
        # under forge's recorded SSE contract fixtures.
        #
        # The image's ``state_schema`` oracle cannot catch it — that keyword
        # exists in 0.6.7 and 0.7.3 alike (a floor probe, not a version
        # probe). The band pin on the install line is the control; the oracle
        # asserts it took.
        assert re.search(
            r"^RUN\s+pip\s+install\s+/tmp/guardkitfactory\s+"
            r"'deepagents>=0\.6\.7,<0\.7'\s*$",
            dockerfile_text,
            re.MULTILINE,
        ), (
            "The guardkitfactory install must pin the deepagents band "
            "explicitly — ``RUN pip install /tmp/guardkitfactory "
            "'deepagents>=0.6.7,<0.7'``. Without it pip resolves "
            "guardkitfactory's own ``deepagents>=0.6.7,<1`` to the newest "
            "release (0.7.x), which silently strips the supervisor's "
            "async-task system prompt in src/forge/cli/serve.py and "
            "cascade-upgrades the langchain stack under forge's recorded SSE "
            "contract fixtures"
        )

    def test_nats_core_installed_from_buildkit_context(
        self, dockerfile_text: str
    ) -> None:
        # Whether the recipe uses ``pip install /tmp/nats-core`` or
        # ``uv pip install -e /tmp/nats-core`` (scoping §11.4 shape (a)),
        # nats-core MUST be installed from the COPYed BuildKit context,
        # not from PyPI (where the 0.2.0 wheel is malformed —
        # TASK-FIX-F0E6).
        assert re.search(
            r"\b(?:uv\s+)?pip\s+install\s+(?:-e\s+)?/tmp/nats-core\b",
            dockerfile_text,
        ), (
            "Builder stage must install nats-core from the BuildKit "
            "context at /tmp/nats-core (not from PyPI)"
        )

    def test_pyproject_toml_not_mutated_in_layer(
        self, dockerfile_text: str
    ) -> None:
        # Scoping §11.4 explicitly forbids rewriting ``pyproject.toml``
        # inside the Docker layer — recommendation locked to shape (a).
        # Detect the most common mutation patterns.
        forbidden_patterns = [
            r"\bsed\s+-i[^\n]*pyproject\.toml",
            r">\s*pyproject\.toml",
            r"\bsed[^\n]*tool\.uv\.sources",
        ]
        for pattern in forbidden_patterns:
            assert not re.search(pattern, dockerfile_text), (
                f"Dockerfile must not mutate pyproject.toml in-place "
                f"(pattern matched: {pattern!r})"
            )


class TestOracleProducerClause:
    """AC: the fifth oracle clause — the fix-task producer must import in-image.

    THE PRECEDING FOUR CLAUSES PROBE EVERYTHING EXCEPT THE PRODUCER. They cover
    the normalizer module, forge's resolver, the guardkit CLI binary, and the
    guardkitfactory / deepagents-band / protocol-prompt trio — none of which
    imports guardkit's fix-task producer. That gap is how the ``lib`` namespace
    shadow baked into a shipped image and stayed green: guardkitfactory ships a
    BARE TOP-LEVEL ``lib`` distribution package, which shadows the producer's
    ``from lib.review_parser import …`` in
    ``installer/core/lib/implement_orchestrator.py``. The review leg wrote both
    artefacts, the deterministic mint step ran, and ``produce_fix_tasks``
    SWALLOWED ``ModuleNotFoundError: No module named 'lib.review_parser'`` into
    ``info['error']`` — exactly one receipt in the estate records
    ``producer.called: true``, and that is how it died.

    These tests pin the clause the same way
    ``test_guardkitfactory_install_pins_deepagents_band`` pins the band string:
    the guard cannot be dropped by edit the way the four clauses' blind spot was
    left open by omission.
    """

    def test_oracle_script_declares_producer_prog(
        self, oracle_script_text: str
    ) -> None:
        # Mirrors the NORMALIZER_PROG / RESOLVER_PROG / HARNESS_PROG idiom.
        body = _oracle_prog_body(oracle_script_text, "PRODUCER_PROG")
        assert body.strip(), "PRODUCER_PROG heredoc must not be empty"

    def test_producer_prog_binds_harness_before_importing_producer(
        self, oracle_script_text: str
    ) -> None:
        # ORDER IS THE WHOLE ORACLE. A clean interpreter imports the producer
        # fine — the shadow only exists once guardkitfactory has bound the bare
        # name ``lib`` in sys.modules, which the real leg always does first
        # (``select_harness``'s langgraph branch imports guardkitfactory.harness
        # long before the review runner reaches the mint step). A probe that
        # called _import_producer() on a clean interpreter would be a FALSE
        # GREEN, so the binding order is pinned, not merely the two imports.
        body = _oracle_prog_body(oracle_script_text, "PRODUCER_PROG")

        selector_match = re.search(
            r"guardkit\.orchestrator\.harness\.selector", body
        )
        harness_match = re.search(r"guardkitfactory\.harness", body)
        producer_match = re.search(r"_import_producer\s*\(\s*\)", body)

        assert selector_match, (
            "PRODUCER_PROG must import guardkit's harness selector — the "
            "module whose langgraph branch performs the guardkitfactory import "
            "that binds the shadowing top-level ``lib``"
        )
        assert harness_match, (
            "PRODUCER_PROG must import ``guardkitfactory.harness`` — the exact "
            "import select_harness performs, and the one that puts "
            "guardkitfactory's top-level ``lib`` into sys.modules"
        )
        assert producer_match, (
            "PRODUCER_PROG must call ``review_runner._import_producer()`` — the "
            "private seam ``produce_fix_tasks`` itself calls, whose failure the "
            "leg swallows into info['error']"
        )
        assert harness_match.start() < producer_match.start(), (
            "PRODUCER_PROG must bind guardkitfactory's modules BEFORE calling "
            "_import_producer(). Reversed, the probe runs on a clean "
            "interpreter where the producer imports fine — a false green that "
            "reproduces the blind spot this clause exists to close"
        )

    def test_producer_prog_requires_a_callable_back(
        self, oracle_script_text: str
    ) -> None:
        # "Did not raise" is not the bar — the mint step CALLS what comes back.
        body = _oracle_prog_body(oracle_script_text, "PRODUCER_PROG")
        assert re.search(r"\bcallable\s*\(", body), (
            "PRODUCER_PROG must require a CALLABLE back from "
            "_import_producer(), not merely that the import did not raise"
        )
        assert re.search(r"\bSystemExit\b", body), (
            "PRODUCER_PROG must raise SystemExit on failure so the "
            "``docker run`` exits non-zero and ``set -e`` fails the build"
        )

    def test_producer_prog_names_the_namespace_shadow_class(
        self, oracle_script_text: str
    ) -> None:
        # The message is the handover. A bare traceback would send the next
        # reader hunting sys.path remedies, all of which are dead by
        # construction (the name is already bound, and the shadow is
        # bidirectional). Name the class and the upstream cure.
        body = _oracle_prog_body(oracle_script_text, "PRODUCER_PROG")
        for needle in (
            "namespace-hygiene",
            "lib.review_parser",
            "guardkitfactory",
        ):
            assert needle in body, (
                f"PRODUCER_PROG's failure message must name {needle!r} so the "
                "diagnosis ships with the failure — this is the "
                "externally-defined-namespace shadow class, and the cure is "
                "structural and upstream"
            )
        assert re.search(r"SHADOW|shadow", body), (
            "PRODUCER_PROG's failure message must name the namespace-SHADOW "
            "class explicitly"
        )

    def test_producer_prog_is_actually_run_in_the_image(
        self, oracle_script_text: str
    ) -> None:
        # Declaring a probe and never running it is the same false-pass class
        # the script's own header warns about for stdin heredocs.
        assert re.search(
            r'^docker\s+run\s+--rm\s+--entrypoint\s+python\s+"\$\{IMAGE\}"\s+'
            r'-c\s+"\$\{PRODUCER_PROG\}"\s*$',
            oracle_script_text,
            re.MULTILINE,
        ), (
            "verify-forge-oracles.sh must execute the fifth clause in-image: "
            '``docker run --rm --entrypoint python "${IMAGE}" -c '
            '"${PRODUCER_PROG}"``'
        )

    def test_every_declared_prog_is_executed(
        self, oracle_script_text: str
    ) -> None:
        # Generic guard over ALL clauses, present and future: a heredoc-captured
        # program that no ``docker run`` ever consumes proves nothing while
        # reading like coverage.
        declared = set(
            re.findall(
                r"^read\s+-r\s+-d\s+''\s+(\w+_PROG)\s+<<'PY'",
                oracle_script_text,
                re.MULTILINE,
            )
        )
        executed = set(re.findall(r'-c\s+"\$\{(\w+_PROG)\}"', oracle_script_text))
        assert declared, "verify-forge-oracles.sh declares no *_PROG probes"
        assert declared <= executed, (
            "every *_PROG probe declared in verify-forge-oracles.sh must be run "
            f"by a ``docker run … -c`` line; never executed: "
            f"{sorted(declared - executed)}"
        )


class TestRuntimeVenvHandover:
    """AC: only the resolved venv crosses the builder→runtime boundary."""

    def test_runtime_copies_opt_venv_from_builder(
        self, dockerfile_text: str
    ) -> None:
        runtime_body = _runtime_stage_body(dockerfile_text)
        assert re.search(
            r"^COPY\s+--from=builder\s+/opt/venv\s+/opt/venv\s*$",
            runtime_body,
            re.MULTILINE,
        ), (
            "Runtime stage must contain "
            "``COPY --from=builder /opt/venv /opt/venv``"
        )

    def test_runtime_does_not_install_build_essentials(
        self, dockerfile_text: str
    ) -> None:
        # gcc/build-essential must stay in the discarded builder stage —
        # shipping them in runtime would inflate the image and broaden
        # attack surface (E1.3). We strip comment lines before scanning
        # so explanatory references in comments don't trigger a false
        # match. The check anchors on actual ``apt-get install`` /
        # ``pip install`` directives in the runtime stage body.
        runtime_body = _runtime_stage_body(dockerfile_text)
        non_comment_lines = [
            line
            for line in runtime_body.splitlines()
            if not line.lstrip().startswith("#")
        ]
        runtime_directives = "\n".join(non_comment_lines)

        forbidden_packages = (
            "build-essential",
            "gcc",
            "g++",
        )
        for pkg in forbidden_packages:
            # Match only when the package name appears as an
            # apt-get/install argument — not in surrounding text.
            offending = re.search(
                rf"(?:apt(?:-get)?\s+install|pip\s+install)[^\n]*\b{re.escape(pkg)}\b",
                runtime_directives,
            )
            assert offending is None, (
                f"Runtime stage must not install {pkg!r} "
                "(belongs in the discarded builder stage)"
            )

    def test_runtime_path_includes_opt_venv_bin(
        self, dockerfile_text: str
    ) -> None:
        # PATH must front-load /opt/venv/bin so ``forge`` resolves to
        # the venv shim rather than the system-python executable.
        runtime_body = _runtime_stage_body(dockerfile_text)
        assert re.search(
            r'^ENV\s+PATH\s*=\s*"?/opt/venv/bin', runtime_body, re.MULTILINE
        ) or re.search(
            r'PATH\s*=\s*"?/opt/venv/bin:\$\{?PATH\}?',
            runtime_body,
        ), (
            "Runtime stage must front-load /opt/venv/bin onto PATH so "
            "the ``forge`` console-script resolves correctly"
        )


class TestHealthcheckDirective:
    """AC: HEALTHCHECK uses curl against /healthz on the contract port."""

    def test_healthcheck_command_literal_match(
        self, dockerfile_text: str
    ) -> None:
        # ASSUM-005 / Contract B: HEALTHCHECK probes the fixed port 8080.
        # The literal-match is checked here so a future port change
        # forces the consumer (this Dockerfile) and the producer
        # (forge.cli.serve.DEFAULT_HEALTHZ_PORT) to be updated together.
        assert "curl -fs http://localhost:8080/healthz" in dockerfile_text, (
            "HEALTHCHECK must invoke "
            "``curl -fs http://localhost:8080/healthz``"
        )

    def test_healthcheck_directive_in_runtime_stage(
        self, dockerfile_text: str
    ) -> None:
        runtime_body = _runtime_stage_body(dockerfile_text)
        assert re.search(
            r"^HEALTHCHECK\b", runtime_body, re.MULTILINE
        ), "Runtime stage must declare a HEALTHCHECK directive"

    def test_healthcheck_includes_exit_1_fallback(
        self, dockerfile_text: str
    ) -> None:
        # ``|| exit 1`` makes the probe fail explicitly if curl returns
        # non-zero rather than relying on Docker's implicit exit-code
        # handling — clearer in container logs.
        assert re.search(
            r"curl\s+-fs\s+http://localhost:8080/healthz\s*\|\|\s*exit\s+1",
            dockerfile_text,
        ), "HEALTHCHECK must use ``curl -fs ... || exit 1``"

    def test_curl_installed_in_runtime_stage(self, dockerfile_text: str) -> None:
        # ``curl`` is not in python:3.14-slim-bookworm by default; the
        # HEALTHCHECK depends on it so the runtime stage must apt-install it.
        runtime_body = _runtime_stage_body(dockerfile_text)
        assert re.search(
            r"apt-get[^\n]*install[^\n]*\bcurl\b", runtime_body
        ), "Runtime stage must apt-install curl for HEALTHCHECK"

    def test_git_installed_in_runtime_stage(self, dockerfile_text: str) -> None:
        # ``git`` is not in python:3.14-slim-bookworm by default; the Mode P
        # planning PLANNED-HANDOFF terminal runs ``WorktreeGitRunner``
        # in-process (``git worktree add``), so the runtime stage must
        # apt-install git (TASK-FWD-PLAN-GITMOUNT option (a)).
        runtime_body = _runtime_stage_body(dockerfile_text)
        assert re.search(
            r"apt-get[^\n]*install[^\n]*\bgit\b", runtime_body
        ), "Runtime stage must apt-install git for the Mode P planning handoff"


class TestPortContract:
    """AC: ENV FORGE_HEALTHZ_PORT=8080 + EXPOSE 8080 (and only 8080)."""

    def test_env_forge_healthz_port_set_to_8080(
        self, dockerfile_text: str
    ) -> None:
        # The ENV must appear on its own line so the seam test's
        # ``^ENV\s+FORGE_HEALTHZ_PORT=`` regex (re.MULTILINE) anchors.
        # A multi-line ``ENV PYTHON... \\\n FORGE_HEALTHZ_PORT=...``
        # block would not match — keep this directive standalone.
        assert re.search(
            r"^ENV\s+FORGE_HEALTHZ_PORT=8080\b",
            dockerfile_text,
            re.MULTILINE,
        ), "Dockerfile must declare ``ENV FORGE_HEALTHZ_PORT=8080``"

    def test_expose_8080_present(self, dockerfile_text: str) -> None:
        assert re.search(
            r"^EXPOSE\s+8080\s*$",
            dockerfile_text,
            re.MULTILINE,
        ), "Dockerfile must declare ``EXPOSE 8080``"

    def test_only_port_8080_is_exposed(self, dockerfile_text: str) -> None:
        # E1.3: only the healthz/serve port may be EXPOSEd. SSH/debug
        # surfaces are forbidden — listing other ports here would
        # signal that they exist.
        expose_lines = re.findall(
            r"^EXPOSE\s+(.+?)\s*$", dockerfile_text, re.MULTILINE
        )
        ports: list[str] = []
        for line in expose_lines:
            ports.extend(line.split())
        assert ports == ["8080"], (
            f"Only port 8080 may be EXPOSEd (E1.3); found ports {ports}"
        )


@pytest.mark.integration_contract("HEALTHZ_PORT")
def test_healthz_port_dockerfile_match() -> None:
    """Verify Dockerfile HEALTHCHECK port matches DEFAULT_HEALTHZ_PORT.

    Contract: Port 8080 — must literal-match
    ``forge.cli.serve.DEFAULT_HEALTHZ_PORT``; mirrored as
    ``ENV FORGE_HEALTHZ_PORT=8080`` in Dockerfile so HEALTHCHECK
    and runtime agree (Contract B; ASSUM-005).
    Producer: TASK-F009-001
    Consumer: TASK-F009-005 (this task)
    """
    # Import lazily so the test can still collect on workers where the
    # forge package is not yet installed; xdist-friendly.
    from forge.cli.serve import DEFAULT_HEALTHZ_PORT

    dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")

    env_match = re.search(
        r"^ENV\s+FORGE_HEALTHZ_PORT=(\d+)\b", dockerfile, re.MULTILINE
    )
    assert env_match, "Dockerfile must declare ENV FORGE_HEALTHZ_PORT"
    assert int(env_match.group(1)) == DEFAULT_HEALTHZ_PORT, (
        f"Dockerfile ENV FORGE_HEALTHZ_PORT={env_match.group(1)} but "
        f"DEFAULT_HEALTHZ_PORT={DEFAULT_HEALTHZ_PORT}"
    )

    # HEALTHCHECK must hit the same port.
    assert (
        f"http://localhost:{DEFAULT_HEALTHZ_PORT}/healthz" in dockerfile
    ), "Dockerfile HEALTHCHECK must use the same port as DEFAULT_HEALTHZ_PORT"


# Sanity check: ensure the ``os`` import isn't accidentally dead. We use
# it to compare ``stat.S_IXUSR`` semantics across POSIX/Windows in
# ``TestBuildScriptExists.test_build_script_is_executable``. (No-op
# placeholder kept so a future contributor sees the intent rather than
# silently dropping the import.)
_ = os.name
