from __future__ import annotations

from typing import Any, Protocol


class ToolCatalog(Protocol):
    def contains(self, name: str, version: str) -> bool:
        """Return whether the tool version is registered."""

    def validate_arguments(
        self,
        name: str,
        version: str,
        arguments: dict[str, Any],
    ) -> None:
        """Raise when the provided arguments are not valid for the tool."""
