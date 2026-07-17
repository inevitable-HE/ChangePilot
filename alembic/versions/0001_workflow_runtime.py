from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0001_workflow_runtime"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workflow_definitions",
        sa.Column("definition_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("digest", sa.String(), nullable=False),
        sa.Column("definition_json", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.String(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint(
            "definition_id",
            "version",
            name="pk_workflow_definitions",
        ),
    )
    op.create_table(
        "workflow_runs",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("definition_id", sa.String(), nullable=False),
        sa.Column("definition_version", sa.Integer(), nullable=False),
        sa.Column("definition_digest", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "last_event_sequence",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("original_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.String(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.String(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("run_id", name="pk_workflow_runs"),
    )
    op.create_table(
        "step_runs",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("step_id", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("completion_sequence", sa.Integer(), nullable=True),
        sa.Column("logical_idempotency_key", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["workflow_runs.run_id"],
            name="fk_step_runs_run_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", "step_id", name="pk_step_runs"),
    )
    op.create_table(
        "step_attempts",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("step_id", sa.String(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("phase", sa.String(), nullable=False),
        sa.Column("record_module", sa.String(), nullable=False),
        sa.Column("record_qualname", sa.String(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("tool_version", sa.String(), nullable=True),
        sa.Column("error_class", sa.String(), nullable=True),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.String(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "step_id"],
            ["step_runs.run_id", "step_runs.step_id"],
            name="fk_step_attempts_step",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "run_id",
            "step_id",
            "attempt_no",
            "phase",
            name="pk_step_attempts",
        ),
    )
    op.create_table(
        "approval_requests",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("approval_key", sa.String(), nullable=False),
        sa.Column("record_module", sa.String(), nullable=False),
        sa.Column("record_qualname", sa.String(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("decision", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.String(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["workflow_runs.run_id"],
            name="fk_approval_requests_run_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", "approval_key", name="pk_approval_requests"),
    )
    op.create_table(
        "audit_events",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("step_id", sa.String(), nullable=True),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("occurred_at", sa.String(), nullable=False),
        sa.Column("previous_state", sa.String(), nullable=False),
        sa.Column("new_state", sa.String(), nullable=False),
        sa.Column("previous_revision", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("record_module", sa.String(), nullable=False),
        sa.Column("record_qualname", sa.String(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.String(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["workflow_runs.run_id"],
            name="fk_audit_events_run_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "step_id"],
            ["step_runs.run_id", "step_runs.step_id"],
            name="fk_audit_events_step",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", "sequence", name="pk_audit_events"),
    )
    op.create_index(
        "ix_approval_requests_pending_unique",
        "approval_requests",
        ["run_id", "approval_key"],
        unique=True,
        sqlite_where=sa.text("decision IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_approval_requests_pending_unique",
        table_name="approval_requests",
    )
    op.drop_table("audit_events")
    op.drop_table("approval_requests")
    op.drop_table("step_attempts")
    op.drop_table("step_runs")
    op.drop_table("workflow_runs")
    op.drop_table("workflow_definitions")
