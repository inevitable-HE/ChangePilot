from __future__ import annotations

from collections.abc import Sequence

from changepilot.planning.adapters.knowledge.sqlite import (
    SQLiteKnowledgeStore,
    initialize_planning_schema,
)
from changepilot.planning.application.ingestion import RunbookIngestionService
from changepilot.planning.application.retrieval import HybridRetriever
from changepilot.planning.domain.knowledge import RunbookDocument
from changepilot.planning.domain.models import TrustLevel
from changepilot.workflow.adapters.persistence.sqlite import create_sqlite_engine


class _KeywordEmbedding:
    @property
    def model_id(self) -> str:
        return "fake-keywords-v1"

    def encode_documents(
        self,
        texts: Sequence[str],
    ) -> tuple[tuple[float, ...], ...]:
        return tuple(self._encode(text) for text in texts)

    def encode_query(self, text: str) -> tuple[float, ...]:
        return self._encode(text)

    @staticmethod
    def _encode(text: str) -> tuple[float, ...]:
        lowered = text.lower()
        return (
            float("迁移" in text or "migration" in lowered),
            float("回滚" in text or "rollback" in lowered),
            float("审批" in text or "approval" in lowered),
        )


def _runbook(
    document_id: str,
    content: str,
    *,
    policy_key: str,
) -> RunbookDocument:
    return RunbookDocument(
        document_id=document_id,
        version="1",
        source=f"runbooks/{document_id}.md",
        trust_level=TrustLevel.AUTHORITATIVE,
        content=content,
        policy_key=policy_key,
        effective_at="2026-07-24",
    )


def test_sqlite_store_supports_incremental_ingestion_and_hybrid_search(
    tmp_path,
) -> None:
    engine = create_sqlite_engine(tmp_path / "knowledge.db")
    initialize_planning_schema(engine)
    store = SQLiteKnowledgeStore(engine, embeddings=_KeywordEmbedding())
    ingestion = RunbookIngestionService(store)
    ingestion.ingest(
        _runbook(
            "schema-migration",
            "# 数据库迁移\n执行 schema 迁移前必须完成审批和备份。",
            policy_key="schema-migration",
        )
    )
    ingestion.ingest(
        _runbook(
            "rollback",
            "# 回滚\n失败后执行数据库回滚并恢复服务版本。",
            policy_key="rollback",
        )
    )

    result = HybridRetriever(store=store, index=store).retrieve(
        "schema 迁移审批",
        top_k=2,
    )

    assert result.evidence
    assert result.evidence[0].reference.document_id == "schema-migration"
    assert result.evidence[0].lexical_rank == 1
    assert result.evidence[0].vector_rank is not None
    assert store.snapshot().document_count == 2
    engine.dispose()


def test_sqlite_store_reopens_with_stable_snapshot(tmp_path) -> None:
    path = tmp_path / "knowledge.db"
    engine = create_sqlite_engine(path)
    initialize_planning_schema(engine)
    store = SQLiteKnowledgeStore(engine)
    result = RunbookIngestionService(store).ingest(
        _runbook(
            "upgrade",
            "# Upgrade\nInspect, migrate, deploy, and verify.",
            policy_key="upgrade",
        )
    )
    engine.dispose()

    reopened_engine = create_sqlite_engine(path)
    reopened = SQLiteKnowledgeStore(reopened_engine)

    assert reopened.snapshot().digest == result.snapshot.digest
    assert reopened.all_chunks()[0].heading_path == ("Upgrade",)
    reopened_engine.dispose()
