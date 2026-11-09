from collections.abc import Iterator
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.engine.state_machine import WorkflowStatus
from app.engine.workflow_engine import (
    PlanSelection,
    WorkflowNotFound,
    WorkflowRunConflict,
    execute_workflow,
)
from app.persistence.database import engine
from app.persistence.models import Workflow, WorkflowEvent
from app.planner.ollama_planner import PlannerError, PlannerRejected, generate_plan
from app.tools.registry import RetryableToolError
from app.validation.plan import WorkflowPlan

router = APIRouter(prefix="/workflows", tags=["workflows"])


class WorkflowCreate(BaseModel):
    request: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)
    ]


class WorkflowResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    request: str
    status: str
    created_at: datetime


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session


@router.post("", response_model=WorkflowResponse, status_code=status.HTTP_201_CREATED)
def create_workflow(
    payload: WorkflowCreate, session: Annotated[Session, Depends(get_session)]
) -> Workflow:
    workflow = Workflow(request=payload.request)
    session.add(workflow)
    session.commit()
    session.refresh(workflow)
    return workflow


@router.get("/{workflow_id}", response_model=WorkflowResponse)
def get_workflow(
    workflow_id: UUID, session: Annotated[Session, Depends(get_session)]
) -> Workflow:
    workflow = session.get(Workflow, workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return workflow


class WorkflowEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    from_status: str
    to_status: str
    details: dict[str, object]
    created_at: datetime


@router.post("/{workflow_id}/run", response_model=WorkflowResponse)
def run_workflow(
    workflow_id: UUID,
    session: Annotated[Session, Depends(get_session)],
    plan: PlanSelection | None = None,
) -> WorkflowResponse:
    try:
        workflow = execute_workflow(session, workflow_id, plan)
    except WorkflowNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except WorkflowRunConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except RetryableToolError as error:
        raise HTTPException(
            status_code=503, detail="A tool failed temporarily. Retry this workflow."
        ) from error
    return WorkflowResponse.model_validate(workflow)


@router.get("/{workflow_id}/events", response_model=list[WorkflowEventResponse])
def get_workflow_events(
    workflow_id: UUID, session: Annotated[Session, Depends(get_session)]
) -> list[WorkflowEvent]:
    if session.get(Workflow, workflow_id) is None:
        raise HTTPException(status_code=404, detail="Workflow not found")

    statement = (
        select(WorkflowEvent)
        .where(WorkflowEvent.workflow_id == workflow_id)
        .order_by(WorkflowEvent.id)
    )
    return list(session.scalars(statement))


@router.post("/{workflow_id}/plan", response_model=WorkflowPlan)
def preview_workflow_plan(
    workflow_id: UUID, session: Annotated[Session, Depends(get_session)]
) -> WorkflowPlan:
    with session.begin():
        workflow = session.get(Workflow, workflow_id)
        if workflow is None:
            raise HTTPException(status_code=404, detail="Workflow not found")
        if workflow.generated_plan is not None:
            return WorkflowPlan.model_validate(workflow.generated_plan)
        if workflow.status != WorkflowStatus.PENDING:
            raise HTTPException(status_code=409, detail="Workflow is no longer pending")
        request_text = workflow.request

    try:
        candidate = generate_plan(request_text)
    except PlannerRejected as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except PlannerError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error

    with session.begin():
        workflow = session.get(
            Workflow, workflow_id, with_for_update=True, populate_existing=True
        )
        if workflow is None:
            raise HTTPException(status_code=404, detail="Workflow not found")
        if workflow.generated_plan is not None:
            return WorkflowPlan.model_validate(workflow.generated_plan)
        if workflow.status != WorkflowStatus.PENDING:
            raise HTTPException(status_code=409, detail="Workflow is no longer pending")

        workflow.generated_plan = candidate.model_dump(mode="json")

    return candidate
