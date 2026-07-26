from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence

from pydantic import Field

from changepilot.evaluation.domain.models import EvaluationBoundaryModel


class OnlineEvaluationDisabled(RuntimeError):
    pass


class OnlineEvaluationConfigurationError(RuntimeError):
    pass


class OnlineEvaluationBudgetExceeded(RuntimeError):
    pass


class OnlineEvaluationConfig(EvaluationBoundaryModel):
    enabled: bool = False
    max_cases: int = Field(default=1, ge=1)
    max_calls: int = Field(default=1, ge=1)
    max_total_tokens: int = Field(default=1024, ge=1)
    max_estimated_cost: float = Field(default=0.01, gt=0)

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> OnlineEvaluationConfig:
        selected = os.environ if environment is None else environment
        try:
            return cls(
                enabled=selected.get("CHANGEPILOT_RUN_ONLINE_EVAL") == "1",
                max_cases=int(
                    selected.get("CHANGEPILOT_ONLINE_EVAL_MAX_CASES", "1")
                ),
                max_calls=int(
                    selected.get("CHANGEPILOT_ONLINE_EVAL_MAX_CALLS", "1")
                ),
                max_total_tokens=int(
                    selected.get("CHANGEPILOT_ONLINE_EVAL_MAX_TOKENS", "1024")
                ),
                max_estimated_cost=float(
                    selected.get("CHANGEPILOT_ONLINE_EVAL_MAX_COST", "0.01")
                ),
            )
        except ValueError as error:
            raise OnlineEvaluationConfigurationError(
                "online evaluation limits must be numeric"
            ) from error


def require_online_credentials(
    config: OnlineEvaluationConfig,
    environment: Mapping[str, str] | None = None,
) -> str:
    if not config.enabled:
        raise OnlineEvaluationDisabled(
            "online evaluation is disabled; set "
            "CHANGEPILOT_RUN_ONLINE_EVAL=1 explicitly"
        )
    selected = os.environ if environment is None else environment
    api_key = selected.get("CHANGEPILOT_DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise OnlineEvaluationConfigurationError(
            "CHANGEPILOT_DEEPSEEK_API_KEY is required for online evaluation"
        )
    return api_key


class OnlineEvaluationOutcome(EvaluationBoundaryModel):
    completed_case_ids: tuple[str, ...]
    skipped_case_ids: tuple[str, ...]
    stopped: bool
    stop_reason: str | None = None
    calls: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    estimated_cost: float = Field(ge=0)


class OnlineEvaluationBudget:
    def __init__(self, config: OnlineEvaluationConfig) -> None:
        self._config = config
        self._cases = 0
        self._calls = 0
        self._tokens = 0
        self._estimated_cost = 0.0

    @property
    def usage(self) -> tuple[int, int, float]:
        return self._calls, self._tokens, self._estimated_cost

    def reserve_case(self) -> None:
        if self._cases + 1 > self._config.max_cases:
            raise OnlineEvaluationBudgetExceeded("sample budget exhausted")
        self._cases += 1

    def reserve_model_usage(
        self,
        *,
        calls: int,
        tokens: int,
        estimated_cost: float,
    ) -> None:
        if calls < 0 or tokens < 0 or estimated_cost < 0:
            raise ValueError("usage reservation cannot be negative")
        if self._calls + calls > self._config.max_calls:
            raise OnlineEvaluationBudgetExceeded("model call budget exhausted")
        if self._tokens + tokens > self._config.max_total_tokens:
            raise OnlineEvaluationBudgetExceeded("model token budget exhausted")
        if (
            self._estimated_cost + estimated_cost
            > self._config.max_estimated_cost
        ):
            raise OnlineEvaluationBudgetExceeded(
                "estimated cost budget exhausted"
            )
        self._calls += calls
        self._tokens += tokens
        self._estimated_cost += estimated_cost


def run_guarded_online_cases(
    case_ids: Sequence[str],
    *,
    config: OnlineEvaluationConfig,
    estimate_usage: Callable[[str], tuple[int, int, float]],
    execute_case: Callable[[str, str], None],
    environment: Mapping[str, str] | None = None,
) -> OnlineEvaluationOutcome:
    api_key = require_online_credentials(config, environment)
    budget = OnlineEvaluationBudget(config)
    completed: list[str] = []
    for index, case_id in enumerate(case_ids):
        try:
            budget.reserve_case()
            calls, tokens, cost = estimate_usage(case_id)
            budget.reserve_model_usage(
                calls=calls,
                tokens=tokens,
                estimated_cost=cost,
            )
        except OnlineEvaluationBudgetExceeded as error:
            usage = budget.usage
            return OnlineEvaluationOutcome(
                completed_case_ids=tuple(completed),
                skipped_case_ids=tuple(case_ids[index:]),
                stopped=True,
                stop_reason=str(error),
                calls=usage[0],
                total_tokens=usage[1],
                estimated_cost=usage[2],
            )
        execute_case(case_id, api_key)
        completed.append(case_id)
    usage = budget.usage
    return OnlineEvaluationOutcome(
        completed_case_ids=tuple(completed),
        skipped_case_ids=(),
        stopped=False,
        calls=usage[0],
        total_tokens=usage[1],
        estimated_cost=usage[2],
    )
