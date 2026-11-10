from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Workflow(Base):
    __tablename__ = "workflows"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    request: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(
        String(128), unique=True, index=True
    )
    generated_plan: Mapped[dict[str, object] | None] = mapped_column(
        JSON(none_as_null=True)
    )
    plan_job: Mapped[dict[str, object] | None] = mapped_column(
        JSON(none_as_null=True)
    )
    execution_plan: Mapped[dict[str, object] | None] = mapped_column(
        JSON(none_as_null=True)
    )
    status: Mapped[str] = mapped_column(String(32), server_default="PENDING")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class WorkflowEvent(Base):
    __tablename__ = "workflow_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[UUID] = mapped_column(
        ForeignKey("workflows.id"), index=True
    )
    from_status: Mapped[str] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32))
    details: Mapped[dict[str, object]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class WorkflowStep(Base):
    __tablename__ = "workflow_steps"

    workflow_id: Mapped[UUID] = mapped_column(
        ForeignKey("workflows.id"), primary_key=True
    )
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    tool: Mapped[str] = mapped_column(String(64))
    position: Mapped[int]
    output: Mapped[list[dict[str, object]]] = mapped_column(JSON)


class FollowupTask(Base):
    __tablename__ = "followup_tasks"

    workflow_id: Mapped[UUID] = mapped_column(
        ForeignKey("workflows.id"), primary_key=True
    )
    ticket_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    summary: Mapped[str] = mapped_column(Text)


class WorkflowApproval(Base):
    __tablename__ = "workflow_approvals"

    workflow_id: Mapped[UUID] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), primary_key=True
    )
    plan_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    step_name: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str | None] = mapped_column(String(16))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
