"""Add an optional idempotency key to workflow creation."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workflows", sa.Column("idempotency_key", sa.String(128), nullable=True)
    )
    op.create_index(
        "ix_workflows_idempotency_key",
        "workflows",
        ["idempotency_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_workflows_idempotency_key", table_name="workflows")
    op.drop_column("workflows", "idempotency_key")
