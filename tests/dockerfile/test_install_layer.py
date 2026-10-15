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
import shlex
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

IMAGE_LOCK_PATH = "/build/requirements-image-py314.lock"

PRIVATE_INSTALL_REQUIREMENTS = (
    "/tmp/nats-core",
    "/tmp/fleet-memory",
    ".[providers,memory]",
    "/tmp/guardkitfactory",
    "/tmp/guardkit",
    "--requirement",
    IMAGE_LOCK_PATH,
    "deepagents==0.7.14",
)

PRIVATE_LAYOUT_GATES = (
    NATS_CORE_LAYOUT_GATE,
    (
        'RUN test -d /tmp/fleet-memory/src/fleet_memory '
        '|| (echo "fleet-memory layout invalid" >&2; exit 1)'
    ),
    (
        'RUN test -d /tmp/guardkitfactory/src/guardkitfactory '
        '|| (echo "guardkitfactory layout invalid" >&2; exit 1)'
    ),
    (
        'RUN test -d /tmp/guardkit/guardkit '
        '|| (echo "guardkit layout invalid" >&2; exit 1)'
    ),
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


def _builder_install_command(dockerfile_text: str) -> re.Match[str]:
    """Return the builder's only pip install transaction."""

    builder = re.search(
        r"^FROM\s+python:3\.14-slim-bookworm@sha256:[0-9a-f]{64}"
        r"\s+AS\s+builder\b",
        dockerfile_text,
        re.MULTILINE | re.IGNORECASE,
    )
    runtime = re.search(
        r"^FROM\s+python:3\.14-slim-bookworm@sha256:[0-9a-f]{64}"
        r"\s+AS\s+runtime\b",
        dockerfile_text,
        re.MULTILINE | re.IGNORECASE,
    )
    assert builder and runtime and builder.end() < runtime.start(), (
        "Could not locate the ordered builder and runtime stages"
    )

    run_commands = re.compile(
        r"^RUN\b(?:[^\n]*\\\n)*[^\n]*$",
        re.MULTILINE,
    ).finditer(dockerfile_text, builder.start(), runtime.start())
    install_commands: list[re.Match[str]] = []
    install_count = 0
    for command in run_commands:
        normalized = re.sub(r"\\\s*\n", " ", command.group())
        count = len(re.findall(r"\bpip[ \t]+install\b", normalized))
        install_count += count
        if count:
            install_commands.append(command)

    assert install_count == 1 and len(install_commands) == 1, (
        "Builder stage must contain exactly one pip install transaction, "
        f"found {install_count}"
    )
    return install_commands[0]


def _builder_install_requirements(dockerfile_text: str) -> tuple[str, ...]:
    """Return the requirements resolved together before pip check."""

    command = _builder_install_command(dockerfile_text).group()
    normalized = re.sub(r"\\\s*\n", " ", command)
    install_clause, separator, remainder = normalized.partition("&&")
    assert separator and re.match(r"\s*pip\s+check\b", remainder), (
        "The single install transaction must run pip check immediately "
        "after dependency resolution"
    )
    tokens = shlex.split(install_clause)
    assert tokens[:3] == ["RUN", "pip", "install"]
    return tuple(tokens[3:])


def _assert_builder_install_contract(dockerfile_text: str) -> None:
    """Require the complete image lock without exporting it to build isolation."""

    assert _builder_install_requirements(
        dockerfile_text
    ) == PRIVATE_INSTALL_REQUIREMENTS, (
        "The only pip install must resolve all private local projects, the "
        f"exact image lock at {IMAGE_LOCK_PATH}, and deepagents==0.7.14 in "
        "one coherent transaction"
    )
    assert not re.search(
        r"^ENV\s+PIP_REQUIREMENT(?:=|\s)",
        dockerfile_text,
        re.MULTILINE,
    ), "PIP_REQUIREMENT must not be exported into isolated package builds"


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
        # Every private layout gate must fail before dependency resolution
        # inspects any local project path.
        install = _builder_install_command(dockerfile_text)
        for gate in PRIVATE_LAYOUT_GATES:
            gate_idx = dockerfile_text.find(gate)
            assert gate_idx != -1, (
                f"Builder stage must contain layout-validation gate {gate!r}"
            )
            assert gate_idx < install.start(), (
                f"Layout-validation gate {gate!r} must run before the single "
                "pip install transaction"
            )


class TestBuilderStageInstallLayer:
    """AC-E: every private project resolves in one checked transaction."""

    def test_pip_install_providers_literal_match(
        self, dockerfile_text: str
    ) -> None:
        # One resolver invocation sees every private source, Forge's extras,
        # the complete image lock, and the exact SDK pin together.
        _assert_builder_install_contract(dockerfile_text)

    def test_rejects_absent_image_lock(self, dockerfile_text: str) -> None:
        mutated = dockerfile_text.replace(
            f"--requirement {IMAGE_LOCK_PATH}",
            "",
            1,
        )
        assert mutated != dockerfile_text, "Test mutation did not find image lock"
        with pytest.raises(AssertionError, match="exact image lock"):
            _assert_builder_install_contract(mutated)

    def test_rejects_wrong_image_lock(self, dockerfile_text: str) -> None:
        mutated = dockerfile_text.replace(
            IMAGE_LOCK_PATH,
            "/build/wrong-image.lock",
            1,
        )
        assert mutated != dockerfile_text, "Test mutation did not find image lock"
        with pytest.raises(AssertionError, match="exact image lock"):
            _assert_builder_install_contract(mutated)

    def test_rejects_exported_pip_requirement(
        self, dockerfile_text: str
    ) -> None:
        copy_directive = "COPY requirements-image-py314.lock ./"
        mutated = dockerfile_text.replace(
            copy_directive,
            (
                f"{copy_directive}\n"
                f"ENV PIP_REQUIREMENT={IMAGE_LOCK_PATH}"
            ),
            1,
        )
        assert mutated != dockerfile_text, (
            "Test mutation did not find image lock COPY"
        )
        with pytest.raises(
            AssertionError,
            match="must not be exported",
        ):
            _assert_builder_install_contract(mutated)

    def test_rejects_second_install_after_pip_check_in_same_run(
        self, dockerfile_text: str
    ) -> None:
        mutated = dockerfile_text.replace(
            "&& pip check",
            "&& pip check && pip install 'deepagents==0.6.12'",
            1,
        )
        assert mutated != dockerfile_text, "Test mutation did not find pip check"
        with pytest.raises(
            AssertionError,
            match="exactly one pip install transaction",
        ):
            _builder_install_requirements(mutated)

    def test_rejects_second_python_m_pip_install_run(
        self, dockerfile_text: str
    ) -> None:
        runtime = re.search(
            r"^FROM\s+python:3\.14-slim-bookworm@sha256:[0-9a-f]{64}"
            r"\s+AS\s+runtime\b",
            dockerfile_text,
            re.MULTILINE | re.IGNORECASE,
        )
        assert runtime, "Test mutation could not locate the runtime stage"
        mutated = (
            dockerfile_text[: runtime.start()]
            + "RUN python -m pip install 'deepagents==0.6.12'\n"
            + dockerfile_text[runtime.start() :]
        )
        with pytest.raises(
            AssertionError,
            match="exactly one pip install transaction",
        ):
            _builder_install_requirements(mutated)

    def test_rejects_second_line_continued_pip_install(
        self, dockerfile_text: str
    ) -> None:
        mutated = dockerfile_text.replace(
            "&& pip check",
            "&& pip check && pip \\\n        install 'deepagents==0.6.12'",
            1,
        )
        assert mutated != dockerfile_text, "Test mutation did not find pip check"
        with pytest.raises(
            AssertionError,
            match="exactly one pip install transaction",
        ):
            _builder_install_requirements(mutated)

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
        assert (
            "/tmp/fleet-memory" in _builder_install_requirements(dockerfile_text)
        ), (
            "Builder stage must install fleet-memory from the BuildKit "
            "context in the shared dependency transaction"
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
        assert (
            "/tmp/guardkitfactory"
            in _builder_install_requirements(dockerfile_text)
        ), (
            "Builder stage must install guardkitfactory from the BuildKit "
            "context in the shared dependency transaction"
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
        # Sequential last-install-wins overrides are forbidden. Forge and
        # guardkitfactory must be inputs to the same resolver invocation.
        requirements = _builder_install_requirements(dockerfile_text)
        assert ".[providers,memory]" in requirements
        assert "/tmp/guardkitfactory" in requirements
        assert len(
            re.findall(
                r"^RUN[ \t]+pip[ \t]+install\b",
                dockerfile_text,
                re.MULTILINE,
            )
        ) == 1, (
            "Forge and guardkitfactory must share the only pip transaction"
        )

    def test_guardkitfactory_install_pins_deepagents_band(
        self, dockerfile_text: str
    ) -> None:
        requirements = _builder_install_requirements(dockerfile_text)
        sdk_requirements = [
            requirement
            for requirement in requirements
            if requirement.startswith("deepagents")
        ]
        assert sdk_requirements == ["deepagents==0.7.14"], (
            "The coherent image install must pin exactly deepagents==0.7.14"
        )

    def test_nats_core_installed_from_buildkit_context(
        self, dockerfile_text: str
    ) -> None:
        # nats-core is a local input to the shared resolver transaction.
        assert (
            "/tmp/nats-core" in _builder_install_requirements(dockerfile_text)
        ), (
            "Builder stage must install nats-core from the BuildKit "
            "context in the shared dependency transaction"
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
        # build-essential/g++ must stay in the discarded builder stage —
        # shipping them in runtime would inflate the image and broaden
        # attack surface (E1.3). We strip comment lines before scanning
        # so explanatory references in comments don't trigger a false
        # match. The check anchors on actual ``apt-get install`` /
        # ``pip install`` directives in the runtime stage body.
        #
        # gcc left this list on 12 October 2026 (Phase 1 web-app readiness,
        # W1): Go's race detector needs cgo on arm64, so the runtime stage
        # installs gcc and libc6-dev on purpose. That is pinned by
        # ``TestTheWebToolchain.test_gcc_is_installed_for_the_race_detector``;
        # the rest of the C/C++ build kit is still refused here.
        runtime_body = _runtime_stage_body(dockerfile_text)
        non_comment_lines = [
            line
            for line in runtime_body.splitlines()
            if not line.lstrip().startswith("#")
        ]
        runtime_directives = "\n".join(non_comment_lines)

        forbidden_packages = (
            "build-essential",
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


class TestTheDockerClientAndNoDaemon:
    """The runtime image carries the docker CLIENT, pinned, and no daemon.

    WHY THE FACTORY NEEDS IT (24 September 2026, stage 4e of the
    containerisation rollout gate). One of the two services that run from this
    image is the deploy helper, and the helper runs the deploy, health-check
    and live-gate commands a PROJECT declares in its own profile — whatever
    they are. A project may perfectly well declare a deploy that brings
    containers up, and inside a sandbox the bootstrap binds that sandbox's own
    engine socket into the helper for exactly that. Until this layer existed
    there was nothing in the image to use the socket. Nothing here names a
    project, a language or a toolchain: this is the factory being able to run
    what it is handed.

    The proof that the client really answers, and that no daemon came with it,
    runs against the built image in ``scripts/verify-forge-oracles.sh``. These
    are the static halves: the pin, the checksum, and the one binary.
    """

    def test_the_client_is_installed_in_the_runtime_stage(
        self, dockerfile_text: str
    ) -> None:
        runtime_body = _runtime_stage_body(dockerfile_text)
        assert re.search(
            r"download\.docker\.com/linux/static/stable/[^\s\"]*"
            r"docker-\$\{DOCKER_CLI_VERSION\}\.tgz",
            runtime_body,
        ), (
            "The runtime stage must fetch the docker client from Docker's own "
            "static release, at the version the Dockerfile pins"
        )

    def test_the_version_is_pinned_and_the_download_is_checksummed(
        self, dockerfile_text: str
    ) -> None:
        runtime_body = _runtime_stage_body(dockerfile_text)
        version = re.search(
            r"^ARG\s+DOCKER_CLI_VERSION=(\S+)", runtime_body, re.MULTILINE
        )
        digest = re.search(
            r"^ARG\s+DOCKER_CLI_SHA256=([0-9a-f]{64})\s*$",
            runtime_body,
            re.MULTILINE,
        )
        assert version, "The docker client's version must be pinned in an ARG"
        assert digest, (
            "The docker client's tarball must have its sha256 recorded in an "
            "ARG — a download nobody checks is a supply chain of its own"
        )
        assert "sha256sum --check --strict" in runtime_body, (
            "The recorded sha256 must actually be checked before the tarball "
            "is unpacked"
        )

    def test_only_the_client_binary_is_unpacked(self, dockerfile_text: str) -> None:
        runtime_body = _runtime_stage_body(dockerfile_text)
        non_comment = "\n".join(
            line
            for line in runtime_body.splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "docker/docker" in non_comment, (
            "Only ``docker/docker`` — the client — is extracted from the tarball"
        )
        for daemon_part in ("docker/dockerd", "docker/containerd", "docker/runc"):
            assert daemon_part not in non_comment, (
                f"{daemon_part} must never be extracted: there is no engine in "
                "this image, and the helper only ever speaks to an engine "
                "whose socket something outside deliberately binds in"
            )
        assert re.search(
            r"test\s+!\s+-e\s+/usr/local/bin/dockerd", non_comment
        ), "The layer must prove for itself that no daemon landed in the image"

    def test_the_dockerfile_says_why_the_factory_needs_it(
        self, dockerfile_text: str
    ) -> None:
        # The rule Rich set on 17 September: the factory names no target
        # project's toolchain. A Docker client in the factory's OWN image is
        # the factory's own need, and the file has to say so where it is added
        # rather than leave a reader to guess it is some project's.
        runtime_body = _runtime_stage_body(dockerfile_text)
        # One run-on line, so a sentence that wraps across two comment lines
        # still reads as the sentence it is.
        comments = re.sub(
            r"\s+",
            " ",
            " ".join(
                line.lstrip().lstrip("#")
                for line in runtime_body.splitlines()
                if line.lstrip().startswith("#")
            ),
        ).lower()
        assert "deploy helper" in comments
        assert "declare" in comments
        assert "never a daemon" in comments or "no daemon" in comments


class TestHealthcheckDirective:
    """AC: HEALTHCHECK uses curl against /healthz on the contract port."""

    def test_healthcheck_command_literal_match(
        self, dockerfile_text: str
    ) -> None:
        # ASSUM-005 / Contract B: HEALTHCHECK probes the port forge serves
        # on, read from FORGE_HEALTHZ_PORT at probe time, falling back to
        # 8080 (since 30 August 2026, c8a9276c: a hardcoded 8080 left the
        # live container "unhealthy" for two days while it served on another
        # port). The literal-match is checked here so a future port change
        # forces the consumer (this Dockerfile) and the producer
        # (forge.cli.serve.DEFAULT_HEALTHZ_PORT) to be updated together.
        assert (
            "curl -fs http://localhost:${FORGE_HEALTHZ_PORT:-8080}/healthz"
            in dockerfile_text
        ), (
            "HEALTHCHECK must invoke "
            "``curl -fs http://localhost:${FORGE_HEALTHZ_PORT:-8080}/healthz``"
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
            r"curl\s+-fs\s+http://localhost:\$\{FORGE_HEALTHZ_PORT:-8080\}/healthz"
            r"\s*\|\|\s*exit\s+1",
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

    # HEALTHCHECK must hit the same port: the variable the ENV above sets,
    # with the same port as its fallback.
    assert (
        "http://localhost:${FORGE_HEALTHZ_PORT:-"
        f"{DEFAULT_HEALTHZ_PORT}}}/healthz" in dockerfile
    ), "Dockerfile HEALTHCHECK must use the same port as DEFAULT_HEALTHZ_PORT"


# Sanity check: ensure the ``os`` import isn't accidentally dead. We use
# it to compare ``stat.S_IXUSR`` semantics across POSIX/Windows in
# ``TestBuildScriptExists.test_build_script_is_executable``. (No-op
# placeholder kept so a future contributor sees the intent rather than
# silently dropping the import.)
_ = os.name


def test_the_runtime_image_carries_the_pinned_compose_plugin() -> None:
    """2 October 2026: a project's suite ran `docker compose` in the build runner
    and the image had only the bare client, so every task was rejected."""
    import pathlib, re
    dockerfile = (pathlib.Path(__file__).resolve().parents[2] / "Dockerfile").read_text()
    version = re.search(r"^ARG\s+DOCKER_COMPOSE_VERSION=(\S+)", dockerfile, re.MULTILINE)
    digest = re.search(r"^ARG\s+DOCKER_COMPOSE_SHA256=([0-9a-f]{64})\s*$", dockerfile, re.MULTILINE)
    assert version and digest, "the compose plugin's version and sha256 must be pinned in ARGs"
    assert version.group(1) == "5.5.0", "the plugin must match the sandboxes' Compose version"
    assert digest.group(1) == "ff42489f5a9b879d5d117c5ffea6defc27390b3286da8ad52cbc9c6ab5df590e", (
        "the sha256 must be the one Docker publishes for docker-compose-linux-aarch64 v5.5.0")
    assert "/usr/local/lib/docker/cli-plugins/docker-compose" in dockerfile
    assert 'sha256sum --check --strict' in dockerfile.split("DOCKER_COMPOSE_SHA256=", 1)[1]



class TestTheCodeSearchPrograms:
    """12 October 2026: ripgrep, ast-grep and Probe for the planner (through
    the sandbox helper) and the coding Player and Coach. Each archive is
    pinned and checksummed before it is unpacked, the programs go in a folder
    on no PATH, and GuardKitFactory's runner checks them inside the built
    image. The static halves are here; the self-check runs during the build."""

    PROGRAMS = (
        ("RIPGREP", "15.2.0", "800b1e7206afe799dfb5a6901f23147cfaabe0e52210538100f61e86e1740915"),
        ("AST_GREP", "0.50.0", "3a4ad22dd1dca7a4900a173de65885578617af57fa13dd93b8d345d6b352770e"),
        ("PROBE", "0.6.0-rc341", "4fa43d6259428727444f7b2415f407e0697e0da8424ded3a16b7784832773032"),
    )

    @pytest.mark.parametrize(("name", "version", "digest"), PROGRAMS)
    def test_each_archive_is_pinned_and_checked(
        self, dockerfile_text: str, name: str, version: str, digest: str
    ) -> None:
        runtime_body = _runtime_stage_body(dockerfile_text)
        assert re.search(rf"^ARG\s+{name}_VERSION={re.escape(version)}\s*$", runtime_body,
                         re.MULTILINE)
        assert re.search(rf"^ARG\s+{name}_SHA256={digest}\s*$", runtime_body, re.MULTILINE)
        assert re.search(
            rf'echo "\$\{{{name}_SHA256\}}\s+/tmp/[^"]+" \| sha256sum --check --strict -',
            runtime_body,
        ), f"{name}'s archive must be checked against its sha256 before unpacking"

    def test_the_programs_are_on_no_path(self, dockerfile_text: str) -> None:
        runtime_body = _runtime_stage_body(dockerfile_text)
        assert "--directory /opt/code-search/bin" in runtime_body
        assert "test ! -e /usr/local/bin/rg && test ! -e /usr/local/bin/sg" in runtime_body
        assert "/opt/code-search" not in "".join(
            line for line in runtime_body.splitlines() if line.startswith("ENV PATH")
        )

    def test_the_runner_checks_them_inside_the_image(self, dockerfile_text: str) -> None:
        runtime_body = _runtime_stage_body(dockerfile_text)
        check = runtime_body.index("RUN python -m guardkitfactory.code_search self-check")
        assert check > runtime_body.index("COPY --from=builder /opt/venv /opt/venv")


def _run_instructions(text: str) -> list[str]:
    """Every RUN instruction in ``text``, continuation lines joined into one."""
    return [
        re.sub(r"\\\s*\n\s*", " ", match.group())
        for match in re.finditer(r"^RUN\b(?:[^\n]*\\\n)*[^\n]*$", text, re.MULTILINE)
    ]


class TestTheWebToolchain:
    """12 October 2026 (Phase 1 web-app readiness, W1): Node 22, Go 1.26,
    Staticcheck, gcc for the race detector, and the Playwright 1.63 browsers.

    The coding Player's and Coach's shell runs with PATH=/usr/bin:/bin and
    inherits none of the image's environment, so the tools are linked into
    /usr/bin and Go's toolchain setting lives in Go's own go.env. The static
    halves are here; the self-check RUN proves them inside the image as
    uid 1000 under that minimal environment."""

    ARCHIVES = (
        (
            "NODE",
            "22.23.2",
            "fff4078c5def658577f92c88db7db3bc0072924bfb93fe52c1e744a54e94abb8",
            "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-arm64.tar.xz",
            "/tmp/node.tar.xz",
        ),
        (
            "GO",
            "1.26.9",
            "4a97373d49fcacdcf3694fea368a500b00ee3e963974f3e7514132717632f052",
            "https://go.dev/dl/go${GO_VERSION}.linux-arm64.tar.gz",
            "/tmp/go.tgz",
        ),
        (
            "STATICCHECK",
            "2026.2.1",
            "594421f28ba620ea14b98377cb84a309cf73cc420ba0f52f30c7fa4d92fd2b0e",
            "https://github.com/dominikh/go-tools/releases/download/"
            "${STATICCHECK_VERSION}/staticcheck_linux_arm64.tar.gz",
            "/tmp/staticcheck.tgz",
        ),
    )

    @pytest.mark.parametrize(("name", "version", "digest", "url", "path"), ARCHIVES)
    def test_each_archive_is_pinned_and_checked_before_unpacking(
        self, dockerfile_text: str, name: str, version: str, digest: str, url: str, path: str
    ) -> None:
        runtime_body = _runtime_stage_body(dockerfile_text)
        assert re.search(
            rf"^ARG\s+{name}_VERSION={re.escape(version)}\s*$", runtime_body, re.MULTILINE
        ), f"{name} must be pinned at {version}"
        assert re.search(
            rf"^ARG\s+{name}_SHA256={digest}\s*$", runtime_body, re.MULTILINE
        ), f"{name}'s archive sha256 must be the one recorded in the baseline"
        (run,) = [r for r in _run_instructions(runtime_body) if url in r]
        check = run.index(
            f'echo "${{{name}_SHA256}}  {path}" | sha256sum --check --strict -'
        )
        unpack = run.index(f"--file {path}")
        assert check < unpack, f"{name}'s archive must be checked before it is unpacked"
        assert "--no-same-owner" in run, (
            f"{name}'s archive must be unpacked as root-owned files, never as "
            "the uids recorded inside it (Staticcheck's is uid 1000, the forge user)"
        )

    def test_debian_nodejs_is_gone(self, dockerfile_text: str) -> None:
        runtime_body = _runtime_stage_body(dockerfile_text)
        for run in _run_instructions(runtime_body):
            assert not re.search(r"apt-get\s+install[^&]*\bnodejs\b", run), (
                "Debian's nodejs (Node 18, no npm) must not be installed; the "
                "pinned Node 22 replaces it"
            )

    def test_the_tools_are_linked_where_the_player_looks(self, dockerfile_text: str) -> None:
        runtime_body = _runtime_stage_body(dockerfile_text)
        links = {
            "node": "/usr/local/bin/node",
            "npm": "/usr/local/bin/npm",
            "npx": "/usr/local/bin/npx",
            "go": "/usr/local/go/bin/go",
            "gofmt": "/usr/local/go/bin/gofmt",
            "staticcheck": "/usr/local/bin/staticcheck",
        }
        for tool, target in links.items():
            # A plain ``ln -s`` refuses to replace an existing file, so no
            # Debian binary in /usr/bin can be shadowed silently.
            assert re.search(
                rf"&& ln -s {re.escape(target)} /usr/bin/{tool}(?:\s|$)", runtime_body
            ), f"{tool} must be linked into /usr/bin with a plain ln -s"
        assert not re.search(r"\bln\s+-[a-z]*f", runtime_body), (
            "no link may be forced over an existing file"
        )

    def test_gotoolchain_local_is_written_into_go_env(self, dockerfile_text: str) -> None:
        runtime_body = _runtime_stage_body(dockerfile_text)
        # Written through a copy, not ``sed -i``: the BDD background step
        # treats any ``sed -i`` in this file as a possible pyproject rewrite.
        assert (
            "sed 's/^GOTOOLCHAIN=auto$/GOTOOLCHAIN=local/' /usr/local/go/go.env > /tmp/go.env"
            in runtime_body
        )
        assert "mv /tmp/go.env /usr/local/go/go.env" in runtime_body
        assert "grep -qx 'GOTOOLCHAIN=local' /usr/local/go/go.env" in runtime_body, (
            "the edit must be proved in the same layer, so a changed go.env "
            "in a later Go fails the build instead of leaving auto in place"
        )

    def test_gcc_is_installed_for_the_race_detector(self, dockerfile_text: str) -> None:
        runtime_body = _runtime_stage_body(dockerfile_text)
        (run,) = [r for r in _run_instructions(runtime_body) if "/tmp/go.tgz" in r]
        assert re.search(r"apt-get install --yes --no-install-recommends gcc libc6-dev\b", run)
        assert "rm -rf /var/lib/apt/lists/*" in run

    def test_the_playwright_browsers_are_installed_for_every_user(
        self, dockerfile_text: str
    ) -> None:
        runtime_body = _runtime_stage_body(dockerfile_text)
        assert re.search(r"^ARG\s+PLAYWRIGHT_VERSION=1\.63\.0\s*$", runtime_body, re.MULTILINE)
        env = re.search(
            r"^ENV\s+PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright\s*$", runtime_body, re.MULTILINE
        )
        assert env, "PLAYWRIGHT_BROWSERS_PATH must be set in the image for the pass-through"
        install = runtime_body.index(
            'RUN npx -y "playwright@${PLAYWRIGHT_VERSION}" install --with-deps '
            "chromium firefox webkit"
        )
        assert env.start() < install, "the browsers must land in /opt/ms-playwright"
        (run,) = [r for r in _run_instructions(runtime_body) if "install --with-deps" in r]
        assert 'grep -qx "Version ${PLAYWRIGHT_VERSION}"' in run
        assert 'chmod -R a+rX,go-w "${PLAYWRIGHT_BROWSERS_PATH}"' in run
        assert "rm -rf /var/lib/apt/lists/* /root/.npm /root/.cache" in run

    def test_no_shared_cache_is_baked_into_the_image(self, dockerfile_text: str) -> None:
        settings = " ".join(
            re.sub(r"\\\s*\n", " ", match.group())
            for match in re.finditer(
                r"^(?:ENV|ARG)\b(?:[^\n]*\\\n)*[^\n]*$", dockerfile_text, re.MULTILINE
            )
        )
        for name in ("GOPATH", "GOCACHE", "GOMODCACHE", "npm_config_cache", "NPM_CONFIG_CACHE"):
            assert not re.search(rf"(?<![A-Za-z0-9_]){name}=", settings), (
                f"{name} must resolve under each build's HOME, never a shared image path"
            )

    def test_the_self_check_runs_as_the_player_would(self, dockerfile_text: str) -> None:
        runtime_body = _runtime_stage_body(dockerfile_text)
        (run,) = [r for r in _run_instructions(runtime_body) if "as_player=" in r]
        assert (
            'as_player="setpriv --reuid=1000 --regid=1000 --clear-groups '
            'env -i PATH=/usr/bin:/bin HOME=${check_home}"'
        ) in run
        for check in (
            'test "$(${as_player} node --version)" = "v${NODE_VERSION}"',
            'test "$(${as_player} npm --version)" = "${NODE_NPM_VERSION}"',
            '${as_player} go version | grep -q " go${GO_VERSION} linux/arm64$"',
            'test "$(${as_player} go env GOTOOLCHAIN)" = "local"',
            'test "$(${as_player} go env CGO_ENABLED)" = "1"',
            '${as_player} go run -race "${check_home}/race.go"',
            'gofmt_out="$(${as_player} gofmt -l "${check_home}/race.go")"',
            'test -z "${gofmt_out}"',
            'test "$(${as_player} staticcheck -version)" = '
            '"staticcheck ${STATICCHECK_VERSION} (0.8.1)"',
        ):
            assert check in run, f"the self-check must include: {check}"
        assert re.search(r"^ARG\s+NODE_NPM_VERSION=10\.9\.8\s*$", runtime_body, re.MULTILINE)

    def test_the_self_check_finds_the_chromium_binaries_playwright_installs(
        self, dockerfile_text: str
    ) -> None:
        # Playwright 1.63.0 on linux-arm64 (playwright-core lib/coreBundle.js,
        # EXECUTABLE_PATHS) installs Chrome for Testing builds:
        #   chromium-<rev>/chrome-linux-arm64/chrome
        #   chromium_headless_shell-<rev>/chrome-headless-shell-linux-arm64/chrome-headless-shell
        # The first W1 draft searched for ``headless_shell``, the name older
        # Playwright builds used, and the self-check would have failed the build.
        runtime_body = _runtime_stage_body(dockerfile_text)
        (run,) = [r for r in _run_instructions(runtime_body) if "as_player=" in r]
        assert (
            "-path '*/chromium-*/chrome-linux-arm64/chrome' -name chrome)"
        ) in run
        assert (
            "-path '*/chromium_headless_shell-*/chrome-headless-shell-linux-arm64/"
            "chrome-headless-shell' -name chrome-headless-shell)"
        ) in run
        assert "-name headless_shell" not in run
        assert 'test -x "${chrome}" && test -x "${headless_shell}"' in run

    def test_every_browser_run_in_the_self_check_is_bounded(
        self, dockerfile_text: str
    ) -> None:
        # 13 October 2026: the first real build ran full Chrome with
        # --dump-dom, it never exited, and the build hung for over two hours.
        # Now the page load uses only the headless shell (what Playwright's
        # default headless mode runs), full Chrome only answers --version, and
        # each run is under a timeout and writes to a file, never a pipe.
        runtime_body = _runtime_stage_body(dockerfile_text)
        (run,) = [r for r in _run_instructions(runtime_body) if "as_player=" in r]
        run = re.sub(r"\s+", " ", run)
        assert (
            'timeout --kill-after=5 30 ${as_player} "${chrome}" --version'
            ' < /dev/null > "${check_home}/chrome-version.txt" 2>&1'
        ) in run
        assert "grep -Eq '[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+' \"${check_home}/chrome-version.txt\"" in run
        assert (
            'timeout --kill-after=5 60 ${as_player} "${headless_shell}" --no-sandbox'
        ) in run
        assert '--dump-dom about:blank < /dev/null > "${check_home}/dom.html"' in run
        assert "grep -q '<body>' \"${check_home}/dom.html\"" in run
        # Every place a browser binary is run is wrapped in a timeout.
        browser_runs = [
            segment
            for segment in re.split(r"&&|\|\|", run)
            if re.search(r'"\$\{(?:chrome|headless_shell)\}" --', segment)
        ]
        assert len(browser_runs) == 2
        for browser_run in browser_runs:
            assert "timeout --kill-after=5 " in browser_run, browser_run
        # Full Chrome is never asked to load a page, and no browser feeds a pipe.
        assert not re.search(r'"\$\{chrome\}"[^;]*--dump-dom', run)
        assert not re.search(r'about:blank\s*\|', run)

    def test_the_toolchain_layers_sit_before_the_cache_barrier(
        self, dockerfile_text: str
    ) -> None:
        # Behind the provenance barrier every layer is rebuilt for each commit;
        # the downloads and the browser install must stay cached, and the
        # self-check must run after everything it checks.
        runtime_body = _runtime_stage_body(dockerfile_text)
        barrier = runtime_body.index("ARG FORGE_GIT_SHA")
        node = runtime_body.index("ARG NODE_VERSION=")
        playwright = runtime_body.index('RUN npx -y "playwright@')
        self_check = runtime_body.index('RUN check_home="$(mktemp -d)"')
        assert runtime_body.index("ARG RIPGREP_VERSION=") < node < playwright < self_check < barrier

    def test_the_node_options_flag_is_kept(self, dockerfile_text: str) -> None:
        # Kept as the Phase 1 design records: Node 22 accepts the flag and it
        # changes nothing there.
        assert re.search(
            r"^ENV\s+NODE_OPTIONS=--experimental-global-webcrypto\s*$",
            dockerfile_text,
            re.MULTILINE,
        )
