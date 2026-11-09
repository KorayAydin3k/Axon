from collections.abc import Iterator
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.engine.state_machine import InvalidWorkflowTransition
from app.engine.workflow_engine import SupportPlan, run_support_demo
from app.persistence.database import engine
from app.persistence.models import Workflow, WorkflowEvent
from app.planner.ollama_planner import PlannerError, PlannerRejected, generate_plan
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
    plan: SupportPlan = SupportPlan.TICKET_LIST,
) -> WorkflowResponse:
    with session.begin():
        workflow = session.get(Workflow, workflow_id, with_for_update=True)
        if workflow is None:
            raise HTTPException(status_code=404, detail="Workflow not found")

        try:
            run_support_demo(session, workflow, plan)
        except InvalidWorkflowTransition as error:
            raise HTTPException(
                status_code=409, detail=f"Workflow is already {workflow.status}"
            ) from error

        result = WorkflowResponse.model_validate(workflow)

    return result


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
        request_text = session.scalar(
            select(Workflow.request).where(Workflow.id == workflow_id)
        )
        if request_text is None:
            raise HTTPException(status_code=404, detail="Workflow not found")

    try:
        return generate_plan(request_text)
    except PlannerRejected as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except PlannerError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
