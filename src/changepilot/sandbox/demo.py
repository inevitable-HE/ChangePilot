from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from changepilot.sandbox.application.runtime import (
    SandboxWorkflowRuntime,
    make_sandbox_runtime,
)
from changepilot.sandbox.domain.faults import FaultSpec, FaultType


def _start_approved(runtime: SandboxWorkflowRuntime) -> str:
    runtime.driver.start()
    run_id = runtime.create_upgrade_run()
    runtime.run_until("waiting_approval")
    runtime.approve_pending()
    return run_id


def _report(
    runtime: SandboxWorkflowRuntime,
    run_id: str,
    scenario: str,
) -> dict[str, Any]:
    run = runtime.query.get_run(run_id)
    state = runtime.manager.inspect(runtime.sandbox_id)
    return {
        "scenario": scenario,
        "run_id": run_id,
        "run_state": run.state,
        "service_version": state.service_version.value,
        "schema_version": state.schema_version.value,
        "migration_ids": list(state.migration_ids),
        "event_count": len(runtime.query.list_events(run_id)),
    }


def run_demo(root: str | Path, scenario: str) -> dict[str, Any]:
    selected_root = Path(root)
    if scenario == "success":
        runtime = make_sandbox_runtime(selected_root)
        try:
            run_id = _start_approved(runtime)
            runtime.run_until("succeeded")
            return _report(runtime, run_id, scenario)
        finally:
            runtime.close()

    if scenario == "compensation":
        runtime = make_sandbox_runtime(
            selected_root,
            fault_specs=(
                FaultSpec(
                    tool_name="service.health-check",
                    call_number=1,
                    fault_type=FaultType.PERMANENT_FAILURE,
                ),
            ),
        )
        try:
            run_id = _start_approved(runtime)
            runtime.run_until("compensated")
            return _report(runtime, run_id, scenario)
        finally:
            runtime.close()

    if scenario == "recovery":
        runtime = make_sandbox_runtime(
            selected_root,
            fault_specs=(
                FaultSpec(
                    tool_name="schema.migrate",
                    call_number=1,
                    fault_type=FaultType.AFTER_EFFECT,
                ),
            ),
        )
        try:
            run_id = _start_approved(runtime)
            runtime.run_until_step("migrate-schema", "result_unknown")
        finally:
            runtime.close()

        resumed = make_sandbox_runtime(
            selected_root,
            fault_specs=None,
            selected_run_id=run_id,
        )
        try:
            resumed.driver.start()
            resumed.run_until("succeeded")
            report = _report(resumed, run_id, scenario)
            report["migration_call_count"] = resumed.faults.call_count(
                resumed.sandbox_id,
                "schema.migrate",
            )
            return report
        finally:
            resumed.close()

    raise ValueError(f"unsupported scenario: {scenario}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay an offline ChangePilot execution scenario.",
    )
    parser.add_argument(
        "--scenario",
        choices=("success", "compensation", "recovery"),
        default="success",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(".demo") / "success",
        help="isolated directory for workflow and sandbox state",
    )
    arguments = parser.parse_args(argv)
    result = run_demo(arguments.root, arguments.scenario)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
