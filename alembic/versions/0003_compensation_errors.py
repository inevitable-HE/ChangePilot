from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0003_compensation_errors"
down_revision = "0002_durable_approvals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workflow_runs", sa.Column("compensation_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("workflow_runs", "compensation_error")
