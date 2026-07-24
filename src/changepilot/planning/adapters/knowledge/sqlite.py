from __future__ import annotations

import json
import math
import re
import struct
from collections.abc import Sequence

from sqlalchemy import Engine, delete, insert, select
from sqlalchemy.exc import IntegrityError

from changepilot.planning.adapters.knowledge.schema import (
    knowledge_chunk_embeddings,
    knowledge_chunks,
    knowledge_documents,
    planning_metadata,
)
from changepilot.planning.application.ingestion import calculate_snapshot
from changepilot.planning.domain.failures import KnowledgeError
from changepilot.planning.domain.knowledge import (
    DocumentStatus,
    KnowledgeChunk,
    KnowledgeSnapshot,
    RunbookDocument,
)
from changepilot.planning.domain.models import TrustLevel
from changepilot.planning.ports.knowledge import (
    EmbeddingProvider,
    RankedChunk,
)


_CREATE_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts
USING fts5(chunk_id UNINDEXED, content, tokenize='trigram')
"""
_QUERY_TERM = re.compile(r"[\w.-]+", re.UNICODE)


def initialize_planning_schema(engine: Engine) -> None:
    planning_metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(_CREATE_FTS)


class SQLiteKnowledgeStore:
    def __init__(
        self,
        engine: Engine,
        *,
        embeddings: EmbeddingProvider | None = None,
    ) -> None:
        self._engine = engine
        self._embeddings = embeddings

    def get_document(
        self,
        document_id: str,
        version: str,
    ) -> RunbookDocument | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(knowledge_documents).where(
                    knowledge_documents.c.document_id == document_id,
                    knowledge_documents.c.version == version,
                )
            ).mappings().first()
        return None if row is None else _document_from_row(row)

    def add_document(
        self,
        document: RunbookDocument,
        chunks: tuple[KnowledgeChunk, ...],
    ) -> None:
        vectors: tuple[tuple[float, ...], ...] = ()
        if self._embeddings is not None and chunks:
            vectors = self._embeddings.encode_documents(
                [chunk.content for chunk in chunks]
            )
            if len(vectors) != len(chunks):
                raise KnowledgeError("embedding count does not match chunks")
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(knowledge_documents).values(
                        document_id=document.document_id,
                        version=document.version,
                        source=document.source,
                        trust_level=document.trust_level.value,
                        content=document.content,
                        content_digest=document.content_digest,
                        policy_key=document.policy_key,
                        effective_at=document.effective_at,
                        status=document.status.value,
                        rule_digest=document.rule_digest or document.content_digest,
                    )
                )
                for index, chunk in enumerate(chunks):
                    connection.execute(
                        insert(knowledge_chunks).values(
                            chunk_id=chunk.chunk_id,
                            document_id=chunk.document_id,
                            document_version=chunk.document_version,
                            source=chunk.source,
                            trust_level=chunk.trust_level.value,
                            policy_key=chunk.policy_key,
                            rule_digest=chunk.rule_digest,
                            status=chunk.status.value,
                            heading_path_json=json.dumps(chunk.heading_path),
                            position=chunk.position,
                            start_offset=chunk.start_offset,
                            end_offset=chunk.end_offset,
                            content=chunk.content,
                            content_digest=chunk.content_digest,
                        )
                    )
                    connection.exec_driver_sql(
                        "INSERT INTO knowledge_fts(chunk_id, content) VALUES (?, ?)",
                        (chunk.chunk_id, chunk.content),
                    )
                    if self._embeddings is not None:
                        vector = vectors[index]
                        connection.execute(
                            insert(knowledge_chunk_embeddings).values(
                                chunk_id=chunk.chunk_id,
                                model_id=self._embeddings.model_id,
                                dimensions=len(vector),
                                vector_blob=_pack_vector(vector),
                            )
                        )
        except IntegrityError as exc:
            raise KnowledgeError("document version already exists") from exc

    def all_documents(self) -> tuple[RunbookDocument, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(knowledge_documents).order_by(
                    knowledge_documents.c.document_id,
                    knowledge_documents.c.version,
                )
            ).mappings()
            return tuple(_document_from_row(row) for row in rows)

    def all_chunks(self) -> tuple[KnowledgeChunk, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(knowledge_chunks).order_by(
                    knowledge_chunks.c.document_id,
                    knowledge_chunks.c.document_version,
                    knowledge_chunks.c.position,
                )
            ).mappings()
            return tuple(_chunk_from_row(row) for row in rows)

    def snapshot(self) -> KnowledgeSnapshot:
        return calculate_snapshot(self.all_documents(), self.all_chunks())

    def lexical_search(
        self,
        query: str,
        *,
        limit: int,
    ) -> tuple[RankedChunk, ...]:
        if limit <= 0 or len(query.strip()) < 3:
            return ()
        terms = [
            term
            for term in _QUERY_TERM.findall(query)
            if len(term) >= 3
        ]
        if not terms:
            return ()
        fts_query = " OR ".join(
            f'"{term.replace(chr(34), chr(34) * 2)}"'
            for term in terms
        )
        with self._engine.connect() as connection:
            rows = connection.exec_driver_sql(
                """
                SELECT chunk_id, bm25(knowledge_fts) AS score
                FROM knowledge_fts
                WHERE knowledge_fts MATCH ?
                ORDER BY score
                LIMIT ?
                """,
                (fts_query, limit),
            ).mappings().all()
            return tuple(
                RankedChunk(
                    chunk=self._load_chunk(connection, row["chunk_id"]),
                    score=-float(row["score"]),
                )
                for row in rows
            )

    def vector_search(
        self,
        query: str,
        *,
        limit: int,
    ) -> tuple[RankedChunk, ...]:
        if self._embeddings is None or limit <= 0:
            return ()
        query_vector = self._embeddings.encode_query(query)
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(
                    knowledge_chunk_embeddings.c.chunk_id,
                    knowledge_chunk_embeddings.c.dimensions,
                    knowledge_chunk_embeddings.c.vector_blob,
                ).where(
                    knowledge_chunk_embeddings.c.model_id
                    == self._embeddings.model_id
                )
            ).mappings()
            ranked = sorted(
                (
                    (
                        row["chunk_id"],
                        _cosine(
                            query_vector,
                            _unpack_vector(
                                row["vector_blob"],
                                row["dimensions"],
                            ),
                        ),
                    )
                    for row in rows
                ),
                key=lambda item: (-item[1], item[0]),
            )[:limit]
            return tuple(
                RankedChunk(
                    chunk=self._load_chunk(connection, chunk_id),
                    score=score,
                )
                for chunk_id, score in ranked
            )

    @staticmethod
    def _load_chunk(connection: object, chunk_id: str) -> KnowledgeChunk:
        row = connection.execute(
            select(knowledge_chunks).where(
                knowledge_chunks.c.chunk_id == chunk_id
            )
        ).mappings().one()
        return _chunk_from_row(row)


def _document_from_row(row: object) -> RunbookDocument:
    return RunbookDocument(
        document_id=row["document_id"],
        version=row["version"],
        source=row["source"],
        trust_level=TrustLevel(row["trust_level"]),
        content=row["content"],
        policy_key=row["policy_key"],
        effective_at=row["effective_at"],
        status=DocumentStatus(row["status"]),
        rule_digest=row["rule_digest"],
    )


def _chunk_from_row(row: object) -> KnowledgeChunk:
    return KnowledgeChunk(
        chunk_id=row["chunk_id"],
        document_id=row["document_id"],
        document_version=row["document_version"],
        source=row["source"],
        trust_level=TrustLevel(row["trust_level"]),
        policy_key=row["policy_key"],
        rule_digest=row["rule_digest"],
        status=DocumentStatus(row["status"]),
        heading_path=tuple(json.loads(row["heading_path_json"])),
        position=row["position"],
        start_offset=row["start_offset"],
        end_offset=row["end_offset"],
        content=row["content"],
        content_digest=row["content_digest"],
    )


def _pack_vector(vector: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack_vector(blob: bytes, dimensions: int) -> tuple[float, ...]:
    return tuple(struct.unpack(f"<{dimensions}f", blob))


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise KnowledgeError("embedding dimensions do not match")
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / (
        left_norm * right_norm
    )
