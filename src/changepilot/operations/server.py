from __future__ import annotations

import os
from pathlib import Path

from changepilot.operations.adapters.api import create_operations_app
from changepilot.operations.application.service import OperationsService


def create_app():
    root = Path(os.getenv("CHANGEPILOT_OPERATIONS_ROOT", ".operations"))
    configured = os.getenv("CHANGEPILOT_EVALUATION_ROOTS")
    evaluation_roots = (
        tuple(Path(item) for item in configured.split(os.pathsep) if item)
        if configured
        else (Path("examples/evaluations"), Path(".eval-results"))
    )
    return create_operations_app(
        OperationsService(root),
        evaluation_roots=evaluation_roots,
    )


app = create_app()
