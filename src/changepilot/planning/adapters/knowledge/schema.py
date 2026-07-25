from __future__ import annotations

from sqlalchemy import (
    Column,
    ForeignKeyConstraint,
    Integer,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    text,
)


planning_metadata = MetaData()

knowledge_documents = Table(
    "knowledge_documents",
    planning_metadata,
    Column("document_id", String(), nullable=False),
    Column("version", String(), nullable=False),
    Column("source", String(), nullable=False),
    Column("trust_level", String(), nullable=False),
    Column("content", Text(), nullable=False),
    Column("content_digest", String(), nullable=False),
    Column("policy_key", String(), nullable=False),
    Column("effective_at", String(), nullable=False),
    Column("status", String(), nullable=False),
    Column("rule_digest", String(), nullable=False),
    PrimaryKeyConstraint(
        "document_id",
        "version",
        name="pk_knowledge_documents",
    ),
)

knowledge_chunks = Table(
    "knowledge_chunks",
    planning_metadata,
    Column("chunk_id", String(), nullable=False),
    Column("document_id", String(), nullable=False),
    Column("document_version", String(), nullable=False),
    Column("source", String(), nullable=False),
    Column("trust_level", String(), nullable=False),
    Column("policy_key", String(), nullable=False),
    Column("rule_digest", String(), nullable=False),
    Column("status", String(), nullable=False),
    Column("heading_path_json", Text(), nullable=False),
    Column("position", Integer(), nullable=False),
    Column("start_offset", Integer(), nullable=False),
    Column("end_offset", Integer(), nullable=False),
    Column("content", Text(), nullable=False),
    Column("content_digest", String(), nullable=False),
    PrimaryKeyConstraint("chunk_id", name="pk_knowledge_chunks"),
    ForeignKeyConstraint(
        ["document_id", "document_version"],
        ["knowledge_documents.document_id", "knowledge_documents.version"],
        name="fk_knowledge_chunks_document",
        ondelete="CASCADE",
    ),
)

knowledge_chunk_embeddings = Table(
    "knowledge_chunk_embeddings",
    planning_metadata,
    Column("chunk_id", String(), nullable=False),
    Column("model_id", String(), nullable=False),
    Column("dimensions", Integer(), nullable=False),
    Column("vector_blob", LargeBinary(), nullable=False),
    PrimaryKeyConstraint(
        "chunk_id",
        "model_id",
        name="pk_knowledge_chunk_embeddings",
    ),
    ForeignKeyConstraint(
        ["chunk_id"],
        ["knowledge_chunks.chunk_id"],
        name="fk_knowledge_embeddings_chunk",
        ondelete="CASCADE",
    ),
)

planning_sessions = Table(
    "planning_sessions",
    planning_metadata,
    Column("session_id", String(), nullable=False),
    Column("request_json", Text(), nullable=False),
    Column("status", String(), nullable=False),
    Column("latest_plan_version", Integer(), nullable=False, server_default=text("0")),
    Column("created_at", String(), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    Column("updated_at", String(), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    PrimaryKeyConstraint("session_id", name="pk_planning_sessions"),
)

planning_attempts = Table(
    "planning_attempts",
    planning_metadata,
    Column("session_id", String(), nullable=False),
    Column("attempt_no", Integer(), nullable=False),
    Column("input_json", Text(), nullable=False),
    Column("result_json", Text(), nullable=False),
    Column("created_at", String(), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    PrimaryKeyConstraint(
        "session_id",
        "attempt_no",
        name="pk_planning_attempts",
    ),
    ForeignKeyConstraint(
        ["session_id"],
        ["planning_sessions.session_id"],
        name="fk_planning_attempts_session",
        ondelete="CASCADE",
    ),
)

planning_plans = Table(
    "planning_plans",
    planning_metadata,
    Column("plan_id", String(), nullable=False),
    Column("version", Integer(), nullable=False),
    Column("session_id", String(), nullable=False),
    Column("content_digest", String(), nullable=False),
    Column("knowledge_snapshot_digest", String(), nullable=False),
    Column("definition_id", String(), nullable=True),
    Column("definition_version", Integer(), nullable=True),
    Column("definition_digest", String(), nullable=True),
    Column("status", String(), nullable=False),
    Column("plan_json", Text(), nullable=False),
    Column("created_at", String(), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    PrimaryKeyConstraint("plan_id", "version", name="pk_planning_plans"),
    ForeignKeyConstraint(
        ["session_id"],
        ["planning_sessions.session_id"],
        name="fk_planning_plans_session",
        ondelete="CASCADE",
    ),
)

model_usage = Table(
    "model_usage",
    planning_metadata,
    Column("id", Integer(), primary_key=True, autoincrement=True),
    Column("request_digest", String(), nullable=False),
    Column("model", String(), nullable=False),
    Column("latency_ms", Integer(), nullable=False),
    Column("input_tokens", Integer(), nullable=False),
    Column("output_tokens", Integer(), nullable=False),
    Column("total_tokens", Integer(), nullable=False),
    Column("estimated_cost_microunits", Integer(), nullable=False),
    Column("cache_hit", Integer(), nullable=False),
    Column("retry_count", Integer(), nullable=False),
    Column("created_at", String(), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
)

model_cache = Table(
    "model_cache",
    planning_metadata,
    Column("cache_key", String(), nullable=False),
    Column("response_json", Text(), nullable=False),
    Column("created_at", String(), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    PrimaryKeyConstraint("cache_key", name="pk_model_cache"),
)
