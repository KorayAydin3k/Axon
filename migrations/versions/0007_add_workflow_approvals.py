"""Record human decisions for a specific execution plan."""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workflow_approvals",
        sa.Column("workflow_id", sa.Uuid(), nullable=False),
        sa.Column("plan_hash", sa.String(64), nullable=False),
        sa.Column("step_name", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(16), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workflow_id", "plan_hash"),
    )


def downgrade() -> None:
    op.drop_table("workflow_approvals")
