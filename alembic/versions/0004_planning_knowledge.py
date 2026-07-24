from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0004_planning_knowledge"
down_revision = "0003_compensation_errors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_documents",
        sa.Column("document_id", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("trust_level", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_digest", sa.String(), nullable=False),
        sa.Column("policy_key", sa.String(), nullable=False),
        sa.Column("effective_at", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("rule_digest", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint(
            "document_id",
            "version",
            name="pk_knowledge_documents",
        ),
    )
    op.create_table(
        "knowledge_chunks",
        sa.Column("chunk_id", sa.String(), nullable=False),
        sa.Column("document_id", sa.String(), nullable=False),
        sa.Column("document_version", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("trust_level", sa.String(), nullable=False),
        sa.Column("policy_key", sa.String(), nullable=False),
        sa.Column("rule_digest", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("heading_path_json", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_digest", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id", "document_version"],
            ["knowledge_documents.document_id", "knowledge_documents.version"],
            name="fk_knowledge_chunks_document",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("chunk_id", name="pk_knowledge_chunks"),
    )
    op.create_table(
        "knowledge_chunk_embeddings",
        sa.Column("chunk_id", sa.String(), nullable=False),
        sa.Column("model_id", sa.String(), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("vector_blob", sa.LargeBinary(), nullable=False),
        sa.ForeignKeyConstraint(
            ["chunk_id"],
            ["knowledge_chunks.chunk_id"],
            name="fk_knowledge_embeddings_chunk",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "chunk_id",
            "model_id",
            name="pk_knowledge_chunk_embeddings",
        ),
    )
    op.execute(
        "CREATE VIRTUAL TABLE knowledge_fts "
        "USING fts5(chunk_id UNINDEXED, content, tokenize='trigram')"
    )
    op.create_table(
        "planning_sessions",
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("request_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("latest_plan_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.String(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.String(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("session_id", name="pk_planning_sessions"),
    )
    op.create_table(
        "planning_attempts",
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("input_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["planning_sessions.session_id"],
            name="fk_planning_attempts_session",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("session_id", "attempt_no", name="pk_planning_attempts"),
    )
    op.create_table(
        "planning_plans",
        sa.Column("plan_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("content_digest", sa.String(), nullable=False),
        sa.Column("knowledge_snapshot_digest", sa.String(), nullable=False),
        sa.Column("definition_id", sa.String(), nullable=True),
        sa.Column("definition_version", sa.Integer(), nullable=True),
        sa.Column("definition_digest", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("plan_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["planning_sessions.session_id"],
            name="fk_planning_plans_session",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("plan_id", "version", name="pk_planning_plans"),
    )
    op.create_table(
        "model_usage",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("request_digest", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("estimated_cost_microunits", sa.Integer(), nullable=False),
        sa.Column("cache_hit", sa.Integer(), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "model_cache",
        sa.Column("cache_key", sa.String(), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("cache_key", name="pk_model_cache"),
    )


def downgrade() -> None:
    op.drop_table("model_cache")
    op.drop_table("model_usage")
    op.drop_table("planning_plans")
    op.drop_table("planning_attempts")
    op.drop_table("planning_sessions")
    op.execute("DROP TABLE knowledge_fts")
    op.drop_table("knowledge_chunk_embeddings")
    op.drop_table("knowledge_chunks")
    op.drop_table("knowledge_documents")
