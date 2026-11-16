"""Persist the time at which a planning job can be claimed."""

import sqlalchemy as sa
from alembic import op

revision = "0009_plan_job_queue"
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workflows", sa.Column("plan_job_run_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index("ix_workflows_plan_job_run_at", "workflows", ["plan_job_run_at"])
    op.execute("""
        UPDATE workflows
        SET plan_job_run_at = CURRENT_TIMESTAMP
        WHERE status = 'PENDING'
          AND generated_plan IS NULL
          AND plan_job ->> 'status' = 'planning'
          AND plan_job ->> 'id' IS NOT NULL
          AND CASE
              WHEN json_typeof(plan_job -> 'deadline') = 'number' THEN
                  (plan_job ->> 'deadline')::numeric > EXTRACT(EPOCH FROM CURRENT_TIMESTAMP)
              ELSE FALSE
          END
    """)


def downgrade() -> None:
    op.drop_index("ix_workflows_plan_job_run_at", table_name="workflows")
    op.drop_column("workflows", "plan_job_run_at")
