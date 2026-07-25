from __future__ import annotations

import os
from pathlib import Path

from changepilot.operations.adapters.api import create_operations_app
from changepilot.operations.application.service import OperationsService


def create_app():
    root = Path(os.getenv("CHANGEPILOT_OPERATIONS_ROOT", ".operations"))
    return create_operations_app(OperationsService(root))


app = create_app()
