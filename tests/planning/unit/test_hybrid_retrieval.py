from __future__ import annotations

from changepilot.planning.application.ingestion import (
    InMemoryKnowledgeStore,
    RunbookIngestionService,
)
from changepilot.planning.application.retrieval import (
    HybridRetriever,
    reciprocal_rank_fusion,
)
from changepilot.planning.domain.knowledge import RunbookDocument
from changepilot.planning.domain.models import TrustLevel
from changepilot.planning.ports.knowledge import RankedChunk


class _ScriptedIndex:
    def __init__(
        self,
        lexical: tuple[RankedChunk, ...],
        vector: tuple[RankedChunk, ...],
    ) -> None:
        self._lexical = lexical
        self._vector = vector

    def lexical_search(self, query: str, *, limit: int) -> tuple[RankedChunk, ...]:
        return self._lexical[:limit]

    def vector_search(self, query: str, *, limit: int) -> tuple[RankedChunk, ...]:
        return self._vector[:limit]


def _document(
    document_id: str,
    *,
    rule_digest: str,
    content: str,
) -> RunbookDocument:
    return RunbookDocument(
        document_id=document_id,
        version="1",
        source=f"runbooks/{document_id}.md",
        trust_level=TrustLevel.AUTHORITATIVE,
        content=content,
        policy_key="schema-approval",
        effective_at="2026-07-24",
        rule_digest=rule_digest,
    )


def test_rrf_rewards_results_present_in_both_rankings() -> None:
    scores = reciprocal_rank_fusion(
        [["lexical-only", "both"], ["both", "vector-only"]],
        k=60,
    )

    assert scores["both"] > scores["lexical-only"]
    assert scores["both"] > scores["vector-only"]


def test_retrieval_keeps_stable_citations_and_marks_conflicts() -> None:
    store = InMemoryKnowledgeStore()
    service = RunbookIngestionService(store)
    service.ingest(
        _document(
            "strict-policy",
            rule_digest="strict",
            content="# Approval\nSchema writes require approval.",
        )
    )
    service.ingest(
        _document(
            "loose-policy",
            rule_digest="loose",
            content="# Approval\nSchema writes can run automatically.",
        )
    )
    first, second = store.all_chunks()
    index = _ScriptedIndex(
        lexical=(
            RankedChunk(chunk=first, score=1.0),
            RankedChunk(chunk=second, score=0.5),
        ),
        vector=(RankedChunk(chunk=first, score=0.9),),
    )

    result = HybridRetriever(store=store, index=index).retrieve(
        "schema approval",
        top_k=2,
    )

    assert result.evidence[0].reference.document_id == first.document_id
    assert result.evidence[0].reference.location.startswith("Approval:")
    assert result.conflicts[0].policy_key == "schema-approval"
    assert all(evidence.conflict for evidence in result.evidence)
    assert result.insufficient_evidence is False
