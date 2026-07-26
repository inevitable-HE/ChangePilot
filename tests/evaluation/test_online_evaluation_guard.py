from __future__ import annotations

import pytest

from changepilot.evaluation.application.online import (
    OnlineEvaluationBudget,
    OnlineEvaluationBudgetExceeded,
    OnlineEvaluationConfig,
    OnlineEvaluationConfigurationError,
    OnlineEvaluationDisabled,
    require_online_credentials,
    run_guarded_online_cases,
)


def test_online_evaluation_is_off_by_default_and_does_not_read_key() -> None:
    config = OnlineEvaluationConfig.from_environment(
        {"CHANGEPILOT_DEEPSEEK_API_KEY": "unused-secret"}
    )

    assert config.enabled is False
    with pytest.raises(OnlineEvaluationDisabled):
        require_online_credentials(
            config,
            {"CHANGEPILOT_DEEPSEEK_API_KEY": "unused-secret"},
        )


def test_enabled_online_evaluation_requires_credentials() -> None:
    config = OnlineEvaluationConfig.from_environment(
        {"CHANGEPILOT_RUN_ONLINE_EVAL": "1"}
    )

    with pytest.raises(
        OnlineEvaluationConfigurationError,
        match="API_KEY",
    ):
        require_online_credentials(config, {})


@pytest.mark.parametrize(
    ("operation", "message"),
    [
        ("sample", "sample budget"),
        ("call", "call budget"),
        ("token", "token budget"),
        ("cost", "cost budget"),
    ],
)
def test_online_evaluation_budget_hard_stops_before_overspend(
    operation: str,
    message: str,
) -> None:
    budget = OnlineEvaluationBudget(
        OnlineEvaluationConfig(
            enabled=True,
            max_cases=1,
            max_calls=1,
            max_total_tokens=100,
            max_estimated_cost=0.01,
        )
    )

    if operation == "sample":
        budget.reserve_case()
        action = budget.reserve_case
    elif operation == "call":
        action = lambda: budget.reserve_model_usage(
            calls=2,
            tokens=1,
            estimated_cost=0,
        )
    elif operation == "token":
        action = lambda: budget.reserve_model_usage(
            calls=1,
            tokens=101,
            estimated_cost=0,
        )
    else:
        action = lambda: budget.reserve_model_usage(
            calls=1,
            tokens=1,
            estimated_cost=0.02,
        )

    with pytest.raises(OnlineEvaluationBudgetExceeded, match=message):
        action()


def test_guarded_online_run_stops_before_cost_limit_and_reports_partial_result(
) -> None:
    executed: list[str] = []
    config = OnlineEvaluationConfig(
        enabled=True,
        max_cases=3,
        max_calls=3,
        max_total_tokens=300,
        max_estimated_cost=0.01,
    )

    outcome = run_guarded_online_cases(
        ["case-1", "case-2", "case-3"],
        config=config,
        estimate_usage=lambda _: (1, 100, 0.006),
        execute_case=lambda case_id, _key: executed.append(case_id),
        environment={"CHANGEPILOT_DEEPSEEK_API_KEY": "test-only"},
    )

    assert executed == ["case-1"]
    assert outcome.completed_case_ids == ("case-1",)
    assert outcome.skipped_case_ids == ("case-2", "case-3")
    assert outcome.stopped is True
    assert outcome.stop_reason == "estimated cost budget exhausted"
    assert outcome.calls == 1
    assert outcome.total_tokens == 100
    assert outcome.estimated_cost == pytest.approx(0.006)
