"""Tests for register_shell_handlers registry wiring (TASK-SSH-005).

Verifies that the registration entry point correctly wires deploy_compose and
run_smoke_tests handlers into the StepTypeRegistry under their step-type keys.
"""

from __future__ import annotations

import pytest

from forge.executor.registry import StepTypeRegistry
from forge.executor.shell_steps import (
    deploy_compose,
    register_shell_handlers,
    run_smoke_tests,
)


class TestRegisterShellHandlers:
    """Tests for the register_shell_handlers function."""

    def test_registers_deploy_compose_handler(self) -> None:
        """AC-001: Registers deploy_compose under 'deploy_compose' key."""
        registry = StepTypeRegistry()

        register_shell_handlers(registry)

        handler = registry.resolve("deploy_compose")
        assert handler is not None
        assert handler is deploy_compose

    def test_registers_run_smoke_tests_handler(self) -> None:
        """AC-001: Registers run_smoke_tests under 'run_smoke_tests' key."""
        registry = StepTypeRegistry()

        register_shell_handlers(registry)

        handler = registry.resolve("run_smoke_tests")
        assert handler is not None
        assert handler is run_smoke_tests

    def test_resolve_returns_non_none_for_registered_handlers(self) -> None:
        """AC-002: Both handlers are resolvable after registration."""
        registry = StepTypeRegistry()

        register_shell_handlers(registry)

        deploy_handler = registry.resolve("deploy_compose")
        smoke_handler = registry.resolve("run_smoke_tests")

        assert deploy_handler is not None
        assert smoke_handler is not None

    def test_resolve_unrelated_step_type_returns_none(self) -> None:
        """AC-003: Registration is additive and doesn't shadow other handlers."""
        registry = StepTypeRegistry()

        # Register shell handlers
        register_shell_handlers(registry)

        # Unrelated step types should still return None
        assert registry.resolve("http_request") is None
        assert registry.resolve("approval") is None
        assert registry.resolve("nonexistent") is None

    def test_registration_preserves_existing_handlers(self) -> None:
        """AC-003: Registration is additive - existing handlers remain intact."""
        registry = StepTypeRegistry()

        # Register a dummy handler first
        def dummy_handler(step):  # noqa: ARG001
            pass

        registry.register("custom_handler", dummy_handler)

        # Register shell handlers
        register_shell_handlers(registry)

        # Both the pre-existing handler and new handlers should be present
        assert registry.resolve("custom_handler") is dummy_handler
        assert registry.resolve("deploy_compose") is deploy_compose
        assert registry.resolve("run_smoke_tests") is run_smoke_tests

    def test_empty_registry_before_registration(self) -> None:
        """Verify handlers are not present before registration."""
        registry = StepTypeRegistry()

        assert registry.resolve("deploy_compose") is None
        assert registry.resolve("run_smoke_tests") is None


class TestPackageExports:
    """Tests for package-level exports (AC-004)."""

    def test_function_exported_from_shell_steps(self) -> None:
        """AC-004: register_shell_handlers is exported from shell_steps."""
        from forge.executor import shell_steps

        assert hasattr(shell_steps, "register_shell_handlers")
        assert callable(shell_steps.register_shell_handlers)

    def test_handlers_exported_from_shell_steps(self) -> None:
        """AC-004: Both handlers are exported from shell_steps."""
        from forge.executor import shell_steps

        assert hasattr(shell_steps, "deploy_compose")
        assert hasattr(shell_steps, "run_smoke_tests")
        assert callable(shell_steps.deploy_compose)
        assert callable(shell_steps.run_smoke_tests)

    def test_function_exported_from_executor_package(self) -> None:
        """AC-004: register_shell_handlers is accessible from forge.executor."""
        from forge import executor

        # Should be able to import from top-level executor package
        assert hasattr(executor, "register_shell_handlers")
        assert callable(executor.register_shell_handlers)

    def test_handlers_exported_from_executor_package(self) -> None:
        """AC-004: Both handlers are accessible from forge.executor."""
        from forge import executor

        assert hasattr(executor, "deploy_compose")
        assert hasattr(executor, "run_smoke_tests")
        assert callable(executor.deploy_compose)
        assert callable(executor.run_smoke_tests)
