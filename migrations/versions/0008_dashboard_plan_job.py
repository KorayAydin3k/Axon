"""Persist dashboard planning job status and timing."""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workflows", sa.Column("plan_job", sa.JSON(none_as_null=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("workflows", "plan_job")
