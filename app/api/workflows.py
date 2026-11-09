from collections.abc import Iterator
from datetime import datetime
from os import getenv
from secrets import compare_digest
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.engine.state_machine import WorkflowStatus
from app.engine.workflow_engine import (
    ApprovalDecision,
    PlanSelection,
    WorkflowNotFound,
    WorkflowRunConflict,
    WorkflowToolFailed,
    decide_approval,
    execute_workflow,
)
from app.persistence.database import engine
from app.persistence.models import Workflow, WorkflowEvent
from app.planner.ollama_planner import PlannerError, PlannerRejected, generate_plan
from app.validation.plan import WorkflowPlan
from app.validation.policy import ToolPolicyViolation

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


@router.post(
    "",
    response_model=WorkflowResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"model": WorkflowResponse, "description": "Existing workflow for this key"},
        409: {"description": "Key already used for a different request"},
    },
)
def create_workflow(
    payload: WorkflowCreate,
    response: Response,
    session: Annotated[Session, Depends(get_session)],
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9._:-]+$",
        ),
    ] = None,
) -> WorkflowResponse:
    with session.begin():
        if idempotency_key is None:
            workflow = Workflow(request=payload.request)
            session.add(workflow)
            session.flush()
        else:
            statement = (
                insert(Workflow)
                .values(request=payload.request, idempotency_key=idempotency_key)
                .on_conflict_do_nothing(index_elements=[Workflow.idempotency_key])
                .returning(Workflow)
            )
            workflow = session.scalar(statement)
            if workflow is None:
                workflow = session.scalars(
                    select(Workflow).where(Workflow.idempotency_key == idempotency_key)
                ).one()
                if workflow.request != payload.request:
                    raise HTTPException(
                        status_code=409,
                        detail="Idempotency-Key was already used for a different request",
                    )
                response.status_code = status.HTTP_200_OK

        result = WorkflowResponse.model_validate(workflow)

    return result


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
    except ToolPolicyViolation as error:
        raise HTTPException(
            status_code=403, detail="Tool execution is blocked by policy"
        ) from error
    except WorkflowToolFailed as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
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


class ApprovalRequest(BaseModel):
    plan_hash: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


def require_approval_token(
    token: Annotated[str | None, Header(alias="X-Axon-Approval-Token")] = None,
) -> None:
    expected = getenv("AXON_APPROVAL_TOKEN")
    if not expected:
        raise HTTPException(status_code=503, detail="Approval is not configured")
    if token is None or not compare_digest(token.encode(), expected.encode()):
        raise HTTPException(status_code=403, detail="Approval access denied")


def _approval_response(
    session: Session, workflow_id: UUID, plan_hash: str, decision: ApprovalDecision
) -> WorkflowResponse:
    try:
        workflow = decide_approval(session, workflow_id, plan_hash, decision)
    except WorkflowNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except WorkflowRunConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return WorkflowResponse.model_validate(workflow)


@router.post(
    "/{workflow_id}/approve",
    response_model=WorkflowResponse,
    dependencies=[Depends(require_approval_token)],
)
def approve_workflow(
    workflow_id: UUID,
    payload: ApprovalRequest,
    session: Annotated[Session, Depends(get_session)],
) -> WorkflowResponse:
    return _approval_response(session, workflow_id, payload.plan_hash, "approved")


@router.post(
    "/{workflow_id}/reject",
    response_model=WorkflowResponse,
    dependencies=[Depends(require_approval_token)],
)
def reject_workflow(
    workflow_id: UUID,
    payload: ApprovalRequest,
    session: Annotated[Session, Depends(get_session)],
) -> WorkflowResponse:
    return _approval_response(session, workflow_id, payload.plan_hash, "rejected")
