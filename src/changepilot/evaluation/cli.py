from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from changepilot.evaluation.application.runner import (
    EvaluationRunner,
    load_dataset,
    load_report,
    write_report,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the deterministic ChangePilot evaluation suite.",
    )
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--code-version", default="working-tree")
    arguments = parser.parse_args(argv)

    dataset = load_dataset(arguments.dataset)
    baseline = (
        load_report(arguments.baseline)
        if arguments.baseline is not None
        else None
    )
    report = EvaluationRunner(
        arguments.output / "runtime",
        code_version=arguments.code_version,
    ).run(dataset, baseline=baseline)
    json_path, markdown_path = write_report(report, arguments.output)
    print(f"result={'PASS' if report.passed else 'FAIL'}")
    print(f"json={json_path}")
    print(f"markdown={markdown_path}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
