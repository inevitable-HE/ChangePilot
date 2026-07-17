from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence


class DefinitionValidationError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        step_id: str | None = None,
        field: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.step_id = step_id
        self.field = field

    def __str__(self) -> str:
        location: list[str] = []
        if self.step_id is not None:
            location.append(f"step={self.step_id}")
        if self.field is not None:
            location.append(f"field={self.field}")
        if not location:
            return self.message
        return f"[{' '.join(location)}] {self.message}"


def require_mapping(
    value: object,
    *,
    field: str,
    step_id: str | None = None,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise DefinitionValidationError(
            f"{field} must be a mapping",
            step_id=step_id,
            field=field,
        )
    return value


def require_string(
    value: object,
    *,
    field: str,
    step_id: str | None = None,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DefinitionValidationError(
            f"{field} must be a non-empty string",
            step_id=step_id,
            field=field,
        )
    return value


def require_positive_int(
    value: object,
    *,
    field: str,
    step_id: str | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DefinitionValidationError(
            f"{field} must be a positive integer",
            step_id=step_id,
            field=field,
        )
    return value


def require_string_sequence(
    value: object,
    *,
    field: str,
    step_id: str | None = None,
) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise DefinitionValidationError(
            f"{field} must be a sequence of strings",
            step_id=step_id,
            field=field,
        )
    normalized: list[str] = []
    for item in value:
        normalized.append(require_string(item, field=field, step_id=step_id))
    return tuple(sorted(dict.fromkeys(normalized)))


def find_cycle(dependencies: Mapping[str, tuple[str, ...]]) -> tuple[str, ...] | None:
    indegree = {step_id: len(depends_on) for step_id, depends_on in dependencies.items()}
    dependents: dict[str, list[str]] = {step_id: [] for step_id in dependencies}
    for step_id, depends_on in dependencies.items():
        for dependency in depends_on:
            dependents[dependency].append(step_id)

    queue = deque(sorted(step_id for step_id, degree in indegree.items() if degree == 0))
    visited = 0
    while queue:
        current = queue.popleft()
        visited += 1
        for dependent in sorted(dependents[current]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                queue.append(dependent)

    if visited == len(dependencies):
        return None
    return tuple(sorted(step_id for step_id, degree in indegree.items() if degree > 0))
