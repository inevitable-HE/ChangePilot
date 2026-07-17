from __future__ import annotations

from alembic import op


revision = "0001_workflow_runtime"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    from changepilot.workflow.adapters.persistence.schema import metadata

    metadata.create_all(op.get_bind())


def downgrade() -> None:
    from changepilot.workflow.adapters.persistence.schema import metadata

    metadata.drop_all(op.get_bind())

