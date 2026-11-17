"""Record the duration of completed workflow steps."""

import sqlalchemy as sa
from alembic import op

revision = "0010_workflow_step_duration"
down_revision = "0009_plan_job_queue"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workflow_steps", sa.Column("duration_ms", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("workflow_steps", "duration_ms")
