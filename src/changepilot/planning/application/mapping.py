from __future__ import annotations

from changepilot.planning.domain.failures import PlanValidationError
from changepilot.planning.domain.models import (
    ChangePlan,
    PreparedWorkflow,
    RetrievedEvidence,
)
from changepilot.planning.application.validation import PlanValidator
from changepilot.workflow.application.tooling import ToolRegistry
from changepilot.workflow.domain.definitions import WorkflowDefinition


class WorkflowDefinitionMapper:
    def __init__(
        self,
        *,
        tools: ToolRegistry,
        validator: PlanValidator,
    ) -> None:
        self._tools = tools
        self._validator = validator

    def prepare(
        self,
        plan: ChangePlan,
        evidence: tuple[RetrievedEvidence, ...],
    ) -> PreparedWorkflow:
        report = self._validator.validate(plan, evidence)
        if not report.valid or report.normalized_plan is None:
            details = "; ".join(
                f"{issue.code}: {issue.message}"
                for issue in report.errors
            )
            raise PlanValidationError(details)
        normalized = report.normalized_plan
        payload = {
            "definition_id": normalized.plan_id,
            "version": normalized.version,
            "steps": [
                {
                    "id": step.id,
                    "tool": step.tool.model_dump(mode="json"),
                    "arguments": step.arguments,
                    "depends_on": list(step.depends_on),
                    "risk": step.risk.value,
                    "retry": {
                        "max_attempts": 3,
                        "initial_backoff_seconds": 1.0,
                    },
                    **(
                        {}
                        if step.compensation_tool is None
                        else {
                            "compensation_tool": step.compensation_tool.model_dump(
                                mode="json"
                            )
                        }
                    ),
                }
                for step in normalized.steps
            ],
        }
        definition = WorkflowDefinition.from_mapping(payload, self._tools)
        return PreparedWorkflow(
            plan_id=normalized.plan_id,
            plan_version=normalized.version,
            knowledge_snapshot_digest=normalized.knowledge_snapshot_digest,
            definition_id=definition.definition_id,
            definition_version=definition.version,
            definition_digest=definition.digest,
            payload=payload,
        )
