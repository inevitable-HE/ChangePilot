from __future__ import annotations

from changepilot.planning.domain.models import (
    PlanToolRef,
    PlanningBoundaryModel,
    TrustLevel,
)
from changepilot.workflow.ports.tools import ToolRisk


class PlanningToolPolicy(PlanningBoundaryModel):
    tool: PlanToolRef
    minimum_risk: ToolRisk
    side_effecting: bool
    compensation_required: bool
    minimum_trust: TrustLevel


class PlanningPolicyCatalog:
    def __init__(
        self,
        *,
        version: str,
        policies: tuple[PlanningToolPolicy, ...],
    ) -> None:
        self.version = version
        self._policies = {
            (policy.tool.name, policy.tool.version): policy
            for policy in policies
        }
        if len(self._policies) != len(policies):
            raise ValueError("duplicate planning tool policy")

    def get(
        self,
        name: str,
        version: str,
    ) -> PlanningToolPolicy | None:
        return self._policies.get((name, version))
