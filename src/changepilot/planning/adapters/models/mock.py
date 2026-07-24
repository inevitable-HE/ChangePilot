from __future__ import annotations

from collections import deque

from changepilot.planning.ports.models import ModelRequest, ModelResponse


class MockModelGateway:
    def __init__(
        self,
        scripted: list[ModelResponse | Exception] | tuple[ModelResponse | Exception, ...],
    ) -> None:
        self._scripted = deque(scripted)
        self.requests: list[ModelRequest] = []

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if not self._scripted:
            raise AssertionError("MockModelGateway has no scripted response")
        outcome = self._scripted.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
