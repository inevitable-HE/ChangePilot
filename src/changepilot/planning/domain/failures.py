from __future__ import annotations


class PlanningError(Exception):
    """Base class for expected planning failures."""


class ModelGatewayError(PlanningError):
    """A model provider failed to return a usable response."""


class TransientModelError(ModelGatewayError):
    """A timeout, throttling response, or provider-side failure."""


class PermanentModelError(ModelGatewayError):
    """A provider response that must not be retried."""


class ModelBudgetExceeded(PlanningError):
    """The configured planning budget was exhausted."""


class KnowledgeError(PlanningError):
    """Knowledge ingestion or retrieval failed."""


class PlanValidationError(PlanningError):
    """A plan cannot be prepared for the workflow runtime."""
