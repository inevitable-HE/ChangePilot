from __future__ import annotations

from sqlalchemy import (
    Column,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    text,
)


metadata = MetaData()

workflow_definitions = Table(
    "workflow_definitions",
    metadata,
    Column("definition_id", String(), nullable=False),
    Column("version", Integer(), nullable=False),
    Column("digest", String(), nullable=False),
    Column("definition_json", Text(), nullable=False),
    Column(
        "created_at",
        String(),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    ),
    PrimaryKeyConstraint("definition_id", "version", name="pk_workflow_definitions"),
)

workflow_runs = Table(
    "workflow_runs",
    metadata,
    Column("run_id", String(), nullable=False),
    Column("definition_id", String(), nullable=False),
    Column("definition_version", Integer(), nullable=False),
    Column("definition_digest", String(), nullable=False),
    Column("state", String(), nullable=False),
    Column("revision", Integer(), nullable=False),
    Column(
        "last_event_sequence",
        Integer(),
        nullable=False,
        server_default=text("0"),
    ),
    Column("original_error", Text(), nullable=True),
    Column(
        "created_at",
        String(),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    ),
    Column(
        "updated_at",
        String(),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    ),
    PrimaryKeyConstraint("run_id", name="pk_workflow_runs"),
)

step_runs = Table(
    "step_runs",
    metadata,
    Column("run_id", String(), nullable=False),
    Column("step_id", String(), nullable=False),
    Column("state", String(), nullable=False),
    Column("revision", Integer(), nullable=False),
    Column("completion_sequence", Integer(), nullable=True),
    Column("logical_idempotency_key", String(), nullable=True),
    PrimaryKeyConstraint("run_id", "step_id", name="pk_step_runs"),
    ForeignKeyConstraint(
        ["run_id"],
        ["workflow_runs.run_id"],
        name="fk_step_runs_run_id",
        ondelete="CASCADE",
    ),
)

step_attempts = Table(
    "step_attempts",
    metadata,
    Column("run_id", String(), nullable=False),
    Column("step_id", String(), nullable=False),
    Column("attempt_no", Integer(), nullable=False),
    Column("phase", String(), nullable=False),
    Column("record_module", String(), nullable=False),
    Column("record_qualname", String(), nullable=False),
    Column("payload_json", Text(), nullable=False),
    Column("tool_version", String(), nullable=True),
    Column("error_class", String(), nullable=True),
    Column("result_json", Text(), nullable=True),
    Column(
        "created_at",
        String(),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    ),
    PrimaryKeyConstraint(
        "run_id",
        "step_id",
        "attempt_no",
        "phase",
        name="pk_step_attempts",
    ),
    ForeignKeyConstraint(
        ["run_id", "step_id"],
        ["step_runs.run_id", "step_runs.step_id"],
        name="fk_step_attempts_step",
        ondelete="CASCADE",
    ),
)

approval_requests = Table(
    "approval_requests",
    metadata,
    Column("run_id", String(), nullable=False),
    Column("approval_key", String(), nullable=False),
    Column("record_module", String(), nullable=False),
    Column("record_qualname", String(), nullable=False),
    Column("payload_json", Text(), nullable=False),
    Column("version", Integer(), nullable=False, server_default=text("0")),
    Column("binding_digest", String(), nullable=True),
    Column("status", String(), nullable=False, server_default=text("'pending'")),
    Column("decision", String(), nullable=True),
    Column(
        "created_at",
        String(),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    ),
    PrimaryKeyConstraint("run_id", "approval_key", name="pk_approval_requests"),
    ForeignKeyConstraint(
        ["run_id"],
        ["workflow_runs.run_id"],
        name="fk_approval_requests_run_id",
        ondelete="CASCADE",
    ),
)

audit_events = Table(
    "audit_events",
    metadata,
    Column("run_id", String(), nullable=False),
    Column("sequence", Integer(), nullable=False),
    Column("step_id", String(), nullable=True),
    Column("event_type", String(), nullable=False),
    Column("occurred_at", String(), nullable=False),
    Column("previous_state", String(), nullable=False),
    Column("new_state", String(), nullable=False),
    Column("previous_revision", Integer(), nullable=False),
    Column("revision", Integer(), nullable=False),
    Column("record_module", String(), nullable=False),
    Column("record_qualname", String(), nullable=False),
    Column("payload_json", Text(), nullable=False),
    Column(
        "created_at",
        String(),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    ),
    PrimaryKeyConstraint("run_id", "sequence", name="pk_audit_events"),
    ForeignKeyConstraint(
        ["run_id"],
        ["workflow_runs.run_id"],
        name="fk_audit_events_run_id",
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["run_id", "step_id"],
        ["step_runs.run_id", "step_runs.step_id"],
        name="fk_audit_events_step",
        ondelete="CASCADE",
    ),
)

Index(
    "ix_approval_requests_pending_unique",
    approval_requests.c.run_id,
    unique=True,
    sqlite_where=approval_requests.c.status == "pending",
)
