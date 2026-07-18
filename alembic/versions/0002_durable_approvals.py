from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0002_durable_approvals"
down_revision = "0001_workflow_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index(
        "ix_approval_requests_pending_unique",
        table_name="approval_requests",
    )
    op.add_column(
        "approval_requests",
        sa.Column("version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "approval_requests",
        sa.Column("binding_digest", sa.String(), nullable=True),
    )
    op.add_column(
        "approval_requests",
        sa.Column("status", sa.String(), nullable=True),
    )
    _backfill_approval_columns()

    with op.batch_alter_table("approval_requests", recreate="always") as batch_op:
        batch_op.alter_column(
            "version",
            existing_type=sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        )
        batch_op.alter_column(
            "status",
            existing_type=sa.String(),
            nullable=False,
            server_default=sa.text("'pending'"),
        )
        batch_op.create_check_constraint(
            "ck_approval_requests_version",
            "version >= 0",
        )
        batch_op.create_check_constraint(
            "ck_approval_requests_status",
            "status IN ('legacy', 'pending', 'approved', 'rejected', 'invalidated')",
        )
        batch_op.create_check_constraint(
            "ck_approval_requests_decision",
            "status = 'legacy' OR decision IS NULL OR decision IN ('approved', 'rejected')",
        )
        batch_op.create_check_constraint(
            "ck_approval_requests_state",
            "(status = 'legacy' AND version = 0 AND binding_digest IS NULL) OR "
            "(status = 'pending' AND version = 0 AND decision IS NULL) OR "
            "(status IN ('approved', 'rejected') AND version >= 1 AND decision = status) OR "
            "(status = 'invalidated' AND version >= 1 AND decision IS NULL)",
        )

    op.create_index(
        "ix_approval_requests_pending_unique",
        "approval_requests",
        ["run_id"],
        unique=True,
        sqlite_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_approval_requests_pending_unique",
        table_name="approval_requests",
    )
    with op.batch_alter_table("approval_requests", recreate="always") as batch_op:
        batch_op.drop_constraint(
            "ck_approval_requests_state",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_approval_requests_decision",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_approval_requests_status",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_approval_requests_version",
            type_="check",
        )
        batch_op.drop_column("status")
        batch_op.drop_column("binding_digest")
        batch_op.drop_column("version")

    op.create_index(
        "ix_approval_requests_pending_unique",
        "approval_requests",
        ["run_id", "approval_key"],
        unique=True,
        sqlite_where=sa.text("decision IS NULL"),
    )


def _backfill_approval_columns() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE approval_requests SET version = 0, "
            "binding_digest = NULL, status = 'legacy'"
        )
    )
