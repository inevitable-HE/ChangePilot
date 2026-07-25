from __future__ import annotations

import json
import platform
import time
from collections import defaultdict
from pathlib import Path
from uuid import uuid4

from changepilot.evaluation.domain.models import (
    CaseResult,
    EvaluationCase,
    EvaluationDataset,
    EvaluationMetadata,
    EvaluationReport,
    UsageMetrics,
)
from changepilot.operations.application.service import OperationsService
from changepilot.operations.domain.models import ApprovalCommand, RecoveryCommand


class EvaluationRunner:
    def __init__(
        self,
        root: str | Path,
        *,
        code_version: str = "working-tree",
    ) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._code_version = code_version

    def run(
        self,
        dataset: EvaluationDataset,
        *,
        baseline: EvaluationReport | None = None,
    ) -> EvaluationReport:
        results = tuple(
            self._run_case(dataset, case) for case in dataset.cases
        )
        aggregate = _aggregate(results)
        regressions = tuple(
            metric
            for metric, threshold in dataset.thresholds.items()
            if aggregate.get(metric, 0.0) < threshold
        )
        baseline_delta: dict[str, float] = {}
        if baseline is not None:
            _require_compatible(dataset, baseline)
            baseline_delta = {
                metric: round(
                    value - baseline.aggregate_metrics.get(metric, 0.0),
                    6,
                )
                for metric, value in aggregate.items()
            }
        return EvaluationReport(
            run_id=str(uuid4()),
            passed=not regressions and all(result.passed for result in results),
            metadata=EvaluationMetadata(
                code_version=self._code_version,
                dataset_id=dataset.dataset_id,
                dataset_version=dataset.version,
                metrics_version=dataset.metrics_version,
                prompt_version=dataset.prompt_version,
                knowledge_version=dataset.knowledge_version,
                model="mock-planner",
                random_seed=dataset.random_seed,
                environment={
                    "python": platform.python_version(),
                    "platform": platform.system().lower(),
                    "online": False,
                    "database": "sqlite",
                },
            ),
            case_results=results,
            aggregate_metrics=aggregate,
            thresholds=dict(dataset.thresholds),
            regressions=regressions,
            baseline_delta=baseline_delta,
        )

    def _run_case(
        self,
        dataset: EvaluationDataset,
        case: EvaluationCase,
    ) -> CaseResult:
        started = time.perf_counter()
        metrics = _planning_metrics(case)
        errors: list[str] = []
        service = OperationsService(
            self._root / dataset.version / case.case_id
        )
        try:
            submitted = service.submit_request(case.request)
            if submitted.run_id is None:
                errors.append(f"request did not produce a run: {submitted.status}")
            else:
                run_id = submitted.run_id
                waiting = service.get_snapshot(run_id)
                migration_calls_before = service.get_tool_call_count(
                    run_id,
                    "schema.migrate",
                )
                metrics["dangerous_action_block_rate"] = float(
                    waiting.summary.state == "waiting_approval"
                    and migration_calls_before == 0
                )
                pending = waiting.pending_approval
                if pending is None:
                    errors.append("expected a pending approval")
                else:
                    result = service.decide_approval(
                        run_id,
                        ApprovalCommand(
                            decision="approved",
                            expected_version=int(pending["version"]),
                            binding_digest=str(pending["binding_digest"]),
                            actor="evaluation-operator",
                            reason="deterministic evaluation approval",
                        ),
                    )
                    metrics["approval_effective_rate"] = float(
                        result.summary.state != "waiting_approval"
                    )
                    if case.request.scenario.value == "recovery":
                        result = service.recover(
                            run_id,
                            RecoveryCommand(
                                expected_run_revision=result.summary.revision
                            ),
                        )
                    final_state = result.summary.state
                    migration_calls = service.get_tool_call_count(
                        run_id,
                        "schema.migrate",
                    )
                    metrics["idempotent_replay_rate"] = float(
                        migration_calls
                        == case.expectation.expected_migration_calls
                    )
                    metrics["recovery_success_rate"] = (
                        float(final_state == case.expectation.terminal_state)
                        if case.request.scenario.value == "recovery"
                        else 1.0
                    )
                    metrics["compensation_success_rate"] = (
                        float(final_state == case.expectation.terminal_state)
                        if case.request.scenario.value == "compensation"
                        else 1.0
                    )
                    metrics["completion_rate"] = float(
                        final_state == case.expectation.terminal_state
                    )
        except Exception as error:
            errors.append(f"{type(error).__name__}: {error}")
        finally:
            service.close()
        latency_ms = int((time.perf_counter() - started) * 1000)
        passed = not errors and all(value == 1.0 for value in metrics.values())
        return CaseResult(
            case_id=case.case_id,
            split=case.split,
            passed=passed,
            metrics=metrics,
            latency_ms=latency_ms,
            usage=UsageMetrics(
                model="mock-planner",
                calls=1,
                input_tokens=100,
                output_tokens=200,
                total_tokens=300,
                cache_hits=0,
                estimated_cost=0.0,
            ),
            errors=tuple(errors),
        )


def load_dataset(path: str | Path) -> EvaluationDataset:
    return EvaluationDataset.model_validate_json(
        Path(path).read_text(encoding="utf-8"),
        strict=False,
    )


def load_report(path: str | Path) -> EvaluationReport:
    return EvaluationReport.model_validate_json(
        Path(path).read_text(encoding="utf-8"),
        strict=False,
    )


def write_report(
    report: EvaluationReport,
    output_directory: str | Path,
) -> tuple[Path, Path]:
    selected = Path(output_directory)
    selected.mkdir(parents=True, exist_ok=True)
    json_path = selected / "evaluation.json"
    markdown_path = selected / "evaluation.md"
    json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    markdown_path.write_text(_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def _planning_metrics(case: EvaluationCase) -> dict[str, float]:
    plan = case.mock_plan
    expected = case.expectation
    observed_tools = set(plan.tools)
    required_tools = set(expected.required_tools)
    required_risks = set(expected.high_risk_tools)
    required_evidence = set(expected.required_evidence)
    return {
        "plan_schema_rate": float(plan.schema_valid),
        "required_step_coverage": (
            len(observed_tools & required_tools) / len(required_tools)
            if required_tools
            else 1.0
        ),
        "forbidden_action_absence_rate": float(
            not observed_tools.intersection(expected.forbidden_tools)
        ),
        "risk_identification_rate": (
            len(set(plan.high_risk_tools) & required_risks)
            / len(required_risks)
            if required_risks
            else 1.0
        ),
        "evidence_completeness_rate": (
            len(set(plan.evidence_refs) & required_evidence)
            / len(required_evidence)
            if required_evidence
            else 1.0
        ),
    }


def _aggregate(results: tuple[CaseResult, ...]) -> dict[str, float]:
    values: dict[str, list[float]] = defaultdict(list)
    for result in results:
        for metric, value in result.metrics.items():
            values[metric].append(value)
    aggregate = {
        metric: round(sum(items) / len(items), 6)
        for metric, items in sorted(values.items())
    }
    aggregate.update(
        {
            "average_latency_ms": round(
                sum(result.latency_ms for result in results) / len(results),
                3,
            ),
            "model_calls": float(
                sum(result.usage.calls for result in results)
            ),
            "total_tokens": float(
                sum(result.usage.total_tokens for result in results)
            ),
            "cache_hits": float(
                sum(result.usage.cache_hits for result in results)
            ),
            "estimated_cost": round(
                sum(result.usage.estimated_cost for result in results),
                8,
            ),
        }
    )
    return aggregate


def _require_compatible(
    dataset: EvaluationDataset,
    baseline: EvaluationReport,
) -> None:
    metadata = baseline.metadata
    if (
        metadata.dataset_id != dataset.dataset_id
        or metadata.dataset_version != dataset.version
        or metadata.metrics_version != dataset.metrics_version
    ):
        raise ValueError("baseline uses an incompatible dataset or metrics version")


def _markdown(report: EvaluationReport) -> str:
    lines = [
        "# ChangePilot Evaluation",
        "",
        f"- Run: `{report.run_id}`",
        f"- Dataset: `{report.metadata.dataset_id}@{report.metadata.dataset_version}`",
        f"- Result: `{'PASS' if report.passed else 'FAIL'}`",
        f"- Model: `{report.metadata.model}`",
        f"- Estimated cost: `{report.aggregate_metrics['estimated_cost']}`",
        "",
        "## Metrics",
        "",
    ]
    lines.extend(
        f"- `{name}`: {value}"
        for name, value in report.aggregate_metrics.items()
    )
    lines.extend(["", "## Cases", ""])
    lines.extend(
        f"- `{result.case_id}` ({result.split}): "
        f"`{'PASS' if result.passed else 'FAIL'}`"
        for result in report.case_results
    )
    if report.regressions:
        lines.extend(["", "## Regressions", ""])
        lines.extend(f"- `{name}`" for name in report.regressions)
    lines.append("")
    return "\n".join(lines)
