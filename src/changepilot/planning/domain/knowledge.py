from __future__ import annotations

import hashlib
from enum import StrEnum

from pydantic import Field

from changepilot.planning.domain.models import (
    PlanningBoundaryModel,
    TrustLevel,
)


class DocumentStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    RETIRED = "retired"


class RunbookDocument(PlanningBoundaryModel):
    document_id: str
    version: str
    source: str
    trust_level: TrustLevel
    content: str
    policy_key: str
    effective_at: str
    status: DocumentStatus = DocumentStatus.ACTIVE
    rule_digest: str | None = None

    @property
    def content_digest(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


class KnowledgeChunk(PlanningBoundaryModel):
    chunk_id: str
    document_id: str
    document_version: str
    source: str
    trust_level: TrustLevel
    policy_key: str
    rule_digest: str
    status: DocumentStatus
    heading_path: tuple[str, ...]
    position: int = Field(ge=0)
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=0)
    content: str
    content_digest: str


class KnowledgeSnapshot(PlanningBoundaryModel):
    digest: str
    document_count: int = Field(ge=0)
    chunk_count: int = Field(ge=0)


class IngestionResult(PlanningBoundaryModel):
    document_id: str
    document_version: str
    content_digest: str
    snapshot: KnowledgeSnapshot
    created_chunks: int = Field(ge=0)
    reused: bool
