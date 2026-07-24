from __future__ import annotations


class PlanningError(Exception):
    """Base class for expected planning failures."""


class ModelGatewayError(PlanningError):
    """A model provider failed to return a usable response."""


class ModelBudgetExceeded(PlanningError):
    """The configured planning budget was exhausted."""


class KnowledgeError(PlanningError):
    """Knowledge ingestion or retrieval failed."""


class PlanValidationError(PlanningError):
    """A plan cannot be prepared for the workflow runtime."""
