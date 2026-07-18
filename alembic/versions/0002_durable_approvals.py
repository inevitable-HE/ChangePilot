from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa


revision = "0002_durable_approvals"
down_revision = "0001_workflow_runtime"
branch_labels = None
depends_on = None


_VALID_DECISIONS = {"approved", "rejected"}
_VALID_STATUSES = {"pending", "approved", "rejected", "invalidated"}


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
            "status IN ('pending', 'approved', 'rejected', 'invalidated')",
        )
        batch_op.create_check_constraint(
            "ck_approval_requests_decision",
            "decision IS NULL OR decision IN ('approved', 'rejected')",
        )
        batch_op.create_check_constraint(
            "ck_approval_requests_state",
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
    rows = connection.execute(
        sa.text(
            "SELECT run_id, approval_key, payload_json, decision "
            "FROM approval_requests"
        )
    ).mappings()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError("approval payload_json is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("approval payload_json must contain an object")

        decision = row["decision"]
        payload_decision = payload.get("decision")
        if decision is None and payload_decision in _VALID_DECISIONS:
            decision = payload_decision
        if decision is not None and decision not in _VALID_DECISIONS:
            raise RuntimeError("legacy approval decision is invalid")

        status = payload.get("status")
        if decision is not None:
            status = decision
        elif status not in _VALID_STATUSES:
            status = "pending"
        if status in _VALID_DECISIONS and decision != status:
            decision = status

        version = payload.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            version = 1 if status != "pending" else 0
        if status == "pending":
            version = 0
            decision = None
        elif version < 1:
            version = 1
        if status == "invalidated":
            decision = None

        binding_digest = payload.get("binding_digest")
        if not isinstance(binding_digest, str):
            binding_digest = None

        connection.execute(
            sa.text(
                "UPDATE approval_requests SET version = :version, "
                "binding_digest = :binding_digest, status = :status, decision = :decision "
                "WHERE run_id = :run_id AND approval_key = :approval_key"
            ),
            {
                "version": version,
                "binding_digest": binding_digest,
                "status": status,
                "decision": decision,
                "run_id": row["run_id"],
                "approval_key": row["approval_key"],
            },
        )
