from __future__ import annotations

from typing import Protocol


class IdentifierGenerator(Protocol):
    def new(self) -> str:
        """Return a new stable identifier."""
