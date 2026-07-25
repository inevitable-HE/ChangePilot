from __future__ import annotations

from pathlib import Path

from changepilot.sandbox.application.faults import (
    DeterministicFaultInjector,
)
from changepilot.sandbox.application.manager import SandboxManager
from changepilot.sandbox.domain.faults import FaultSpec, FaultType


def test_fault_occurs_at_configured_call_and_persists_count(
    tmp_path: Path,
) -> None:
    manager = SandboxManager(tmp_path / "sandboxes")
    manager.create("demo")
    first = DeterministicFaultInjector(manager)
    first.configure(
        "demo",
        (
            FaultSpec(
                tool_name="schema.migrate",
                call_number=2,
                fault_type=FaultType.AFTER_EFFECT,
            ),
        ),
    )

    assert first.next_fault("demo", "schema.migrate") is None
    assert (
        first.next_fault("demo", "schema.migrate")
        is FaultType.AFTER_EFFECT
    )

    reopened = DeterministicFaultInjector(manager)
    assert reopened.call_count("demo", "schema.migrate") == 2
    assert reopened.next_fault("demo", "schema.migrate") is None


def test_reset_clears_fault_configuration(tmp_path: Path) -> None:
    manager = SandboxManager(tmp_path / "sandboxes")
    manager.create("demo")
    injector = DeterministicFaultInjector(manager)
    injector.configure(
        "demo",
        (
            FaultSpec(
                tool_name="service.health-check",
                call_number=1,
                fault_type=FaultType.PERMANENT_FAILURE,
            ),
        ),
    )
    assert (
        injector.next_fault("demo", "service.health-check")
        is FaultType.PERMANENT_FAILURE
    )

    manager.reset("demo")

    assert injector.next_fault("demo", "service.health-check") is None
