from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from changepilot.planning.domain.knowledge import (
    KnowledgeChunk,
    KnowledgeSnapshot,
    RunbookDocument,
)
from changepilot.planning.domain.models import PlanningBoundaryModel


class RankedChunk(PlanningBoundaryModel):
    chunk: KnowledgeChunk
    score: float


class EmbeddingProvider(Protocol):
    @property
    def model_id(self) -> str:
        """Return the stable embedding model identity."""

    def encode_documents(
        self,
        texts: Sequence[str],
    ) -> tuple[tuple[float, ...], ...]:
        """Encode document passages."""

    def encode_query(self, text: str) -> tuple[float, ...]:
        """Encode one search query."""


class KnowledgeStore(Protocol):
    def get_document(
        self,
        document_id: str,
        version: str,
    ) -> RunbookDocument | None:
        """Return one immutable document version."""

    def add_document(
        self,
        document: RunbookDocument,
        chunks: tuple[KnowledgeChunk, ...],
    ) -> None:
        """Atomically add one document version and its chunks."""

    def all_documents(self) -> tuple[RunbookDocument, ...]:
        """Return all known document versions."""

    def all_chunks(self) -> tuple[KnowledgeChunk, ...]:
        """Return all indexed chunks."""

    def snapshot(self) -> KnowledgeSnapshot:
        """Return a stable digest for current knowledge content."""


class KnowledgeSearchIndex(Protocol):
    def lexical_search(
        self,
        query: str,
        *,
        limit: int,
    ) -> tuple[RankedChunk, ...]:
        """Return keyword-ranked chunks."""

    def vector_search(
        self,
        query: str,
        *,
        limit: int,
    ) -> tuple[RankedChunk, ...]:
        """Return vector-ranked chunks, or an empty tuple when disabled."""
