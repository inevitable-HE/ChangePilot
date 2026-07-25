from __future__ import annotations

from collections.abc import Sequence

from pydantic import Field

from changepilot.planning.domain.knowledge import (
    DocumentStatus,
    KnowledgeSnapshot,
)
from changepilot.planning.domain.models import (
    EvidenceRef,
    PlanningBoundaryModel,
    RetrievedEvidence,
)
from changepilot.planning.ports.knowledge import (
    KnowledgeSearchIndex,
    KnowledgeStore,
    RankedChunk,
)


class KnowledgeConflict(PlanningBoundaryModel):
    policy_key: str
    chunk_ids: tuple[str, ...]
    rule_digests: tuple[str, ...]


class RetrievalResult(PlanningBoundaryModel):
    query: str
    evidence: tuple[RetrievedEvidence, ...]
    conflicts: tuple[KnowledgeConflict, ...]
    snapshot: KnowledgeSnapshot
    insufficient_evidence: bool


class HybridRetriever:
    def __init__(
        self,
        *,
        store: KnowledgeStore,
        index: KnowledgeSearchIndex,
        candidate_limit: int = 20,
        rrf_k: int = 60,
    ) -> None:
        if candidate_limit <= 0 or rrf_k <= 0:
            raise ValueError("retrieval limits must be positive")
        self._store = store
        self._index = index
        self._candidate_limit = candidate_limit
        self._rrf_k = rrf_k

    def retrieve(self, query: str, *, top_k: int = 5) -> RetrievalResult:
        lexical = self._index.lexical_search(
            query,
            limit=self._candidate_limit,
        )
        vector = self._index.vector_search(
            query,
            limit=self._candidate_limit,
        )
        scores = reciprocal_rank_fusion(
            [
                [result.chunk.chunk_id for result in lexical],
                [result.chunk.chunk_id for result in vector],
            ],
            k=self._rrf_k,
        )
        chunks = {
            result.chunk.chunk_id: result.chunk
            for result in (*lexical, *vector)
        }
        lexical_ranks = {
            item.chunk.chunk_id: rank
            for rank, item in enumerate(lexical, start=1)
        }
        vector_ranks = {
            item.chunk.chunk_id: rank
            for rank, item in enumerate(vector, start=1)
        }
        conflicts = _find_conflicts(tuple(chunks.values()))
        conflict_ids = {
            chunk_id
            for conflict in conflicts
            for chunk_id in conflict.chunk_ids
        }
        ranked_ids = sorted(
            scores,
            key=lambda chunk_id: (-scores[chunk_id], chunk_id),
        )[:top_k]
        evidence = tuple(
            RetrievedEvidence(
                reference=EvidenceRef(
                    document_id=chunks[chunk_id].document_id,
                    document_version=chunks[chunk_id].document_version,
                    chunk_id=chunk_id,
                    location=_location(chunks[chunk_id]),
                    content_digest=chunks[chunk_id].content_digest,
                ),
                text=chunks[chunk_id].content,
                trust_level=chunks[chunk_id].trust_level,
                lexical_rank=lexical_ranks.get(chunk_id),
                vector_rank=vector_ranks.get(chunk_id),
                hybrid_score=scores[chunk_id],
                conflict=chunk_id in conflict_ids,
            )
            for chunk_id in ranked_ids
        )
        return RetrievalResult(
            query=query,
            evidence=evidence,
            conflicts=conflicts,
            snapshot=self._store.snapshot(),
            insufficient_evidence=not evidence,
        )


def reciprocal_rank_fusion(
    ranked_lists: Sequence[Sequence[str]],
    *,
    k: int = 60,
) -> dict[str, float]:
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, chunk_id in enumerate(ranked, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
    return scores


def _find_conflicts(
    chunks: tuple[object, ...],
) -> tuple[KnowledgeConflict, ...]:
    by_policy: dict[str, list[object]] = {}
    for chunk in chunks:
        if chunk.status is DocumentStatus.ACTIVE:
            by_policy.setdefault(chunk.policy_key, []).append(chunk)
    conflicts: list[KnowledgeConflict] = []
    for policy_key, candidates in sorted(by_policy.items()):
        rule_digests = sorted({chunk.rule_digest for chunk in candidates})
        if len(rule_digests) > 1:
            conflicts.append(
                KnowledgeConflict(
                    policy_key=policy_key,
                    chunk_ids=tuple(
                        sorted({chunk.chunk_id for chunk in candidates})
                    ),
                    rule_digests=tuple(rule_digests),
                )
            )
    return tuple(conflicts)


def _location(chunk: object) -> str:
    heading = " > ".join(item for item in chunk.heading_path if item)
    prefix = f"{heading}: " if heading else ""
    return f"{prefix}{chunk.start_offset}-{chunk.end_offset}"
