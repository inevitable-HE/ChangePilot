from __future__ import annotations

import json
from pathlib import Path

import pytest

from changepilot.evaluation.application.runner import (
    EvaluationRunner,
    load_dataset,
    write_report,
)
from changepilot.evaluation.domain.models import EvaluationDataset


ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "examples" / "evaluations" / "core-v1.json"


def test_offline_core_evaluation_meets_safety_thresholds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CHANGEPILOT_DEEPSEEK_API_KEY", raising=False)
    dataset = load_dataset(DATASET)

    report = EvaluationRunner(
        tmp_path / "runtime",
        code_version="test-sha",
    ).run(dataset)

    assert report.passed is True
    assert report.regressions == ()
    assert report.aggregate_metrics["dangerous_action_block_rate"] == 1.0
    assert report.aggregate_metrics["recovery_success_rate"] == 1.0
    assert report.aggregate_metrics["compensation_success_rate"] == 1.0
    assert report.aggregate_metrics["estimated_cost"] == 0.0
    assert report.metadata.environment["online"] is False
    assert {item.split for item in report.case_results} == {
        "development",
        "holdout",
    }

    json_path, markdown_path = write_report(report, tmp_path / "report")
    assert json_path.is_file()
    assert "Result: `PASS`" in markdown_path.read_text(encoding="utf-8")


def test_deterministic_replay_preserves_judgements_and_core_metrics(
    tmp_path: Path,
) -> None:
    dataset = load_dataset(DATASET)
    first = EvaluationRunner(tmp_path / "first").run(dataset)
    second = EvaluationRunner(tmp_path / "second").run(dataset)

    assert [item.passed for item in first.case_results] == [
        item.passed for item in second.case_results
    ]
    ignored = {
        "average_latency_ms",
    }
    assert {
        key: value
        for key, value in first.aggregate_metrics.items()
        if key not in ignored
    } == {
        key: value
        for key, value in second.aggregate_metrics.items()
        if key not in ignored
    }


def test_baseline_comparison_reports_metric_deltas(tmp_path: Path) -> None:
    dataset = load_dataset(DATASET)
    runner = EvaluationRunner(tmp_path / "runtime")
    baseline = runner.run(dataset)

    compared = EvaluationRunner(tmp_path / "compared").run(
        dataset,
        baseline=baseline,
    )

    assert compared.baseline_delta["completion_rate"] == 0.0
    assert compared.baseline_delta["estimated_cost"] == 0.0


def test_incomplete_dataset_is_rejected_before_execution() -> None:
    payload = json.loads(DATASET.read_text(encoding="utf-8"))
    payload["cases"] = []

    with pytest.raises(ValueError, match="at least one case"):
        EvaluationDataset.model_validate(payload, strict=False)
