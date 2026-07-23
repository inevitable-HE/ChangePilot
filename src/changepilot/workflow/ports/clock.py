from __future__ import annotations

from typing import Protocol


class Clock(Protocol):
    def now(self) -> str:
        """Return the current timestamp in the workflow's persisted format."""
