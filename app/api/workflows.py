from collections.abc import Iterator
from datetime import datetime
from os import getenv
from secrets import compare_digest
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi import Query
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import select
from sqlalchemy import tuple_
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
from app.observability.telemetry import log_event, observe_stage
from app.persistence.database import engine
from app.persistence.models import Workflow, WorkflowEvent, WorkflowStep
from app.planner.jobs import enqueue_plan_job, generate_workflow_plan
from app.planner.ollama_planner import PlannerError, PlannerRejected
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


class WorkflowStepTiming(BaseModel):
    name: str
    tool: str
    duration_ms: float | None


class WorkflowDetails(WorkflowResponse):
    step_timings: list[WorkflowStepTiming]


def _workflow_details(session: Session, workflow: Workflow) -> WorkflowDetails:
    response = WorkflowResponse.model_validate(workflow)
    statement = (
        select(WorkflowStep.name, WorkflowStep.tool, WorkflowStep.duration_ms)
        .where(WorkflowStep.workflow_id == response.id)
        .order_by(WorkflowStep.position)
    )
    return WorkflowDetails(
        **response.model_dump(),
        step_timings=[
            WorkflowStepTiming.model_validate(row)
            for row in session.execute(statement).mappings()
        ],
    )


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


class WorkflowListItem(BaseModel):
    id: UUID
    status: str
    created_at: datetime


class WorkflowPage(BaseModel):
    items: list[WorkflowListItem]
    next_cursor: UUID | None


@router.get("", response_model=WorkflowPage)
def list_workflows(
    session: Annotated[Session, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    before: UUID | None = None,
    workflow_status: Annotated[WorkflowStatus | None, Query(alias="status")] = None,
) -> WorkflowPage:
    query = select(Workflow.id, Workflow.status, Workflow.created_at)
    if workflow_status is not None:
        query = query.where(Workflow.status == workflow_status.value)
    if before is not None:
        cursor_time = session.scalar(select(Workflow.created_at).where(Workflow.id == before))
        if cursor_time is None:
            raise HTTPException(status_code=400, detail="Unknown workflow cursor")
        query = query.where(tuple_(Workflow.created_at, Workflow.id) < (cursor_time, before))

    rows = session.execute(
        query.order_by(Workflow.created_at.desc(), Workflow.id.desc()).limit(limit + 1)
    ).mappings().all()
    items = [WorkflowListItem.model_validate(row) for row in rows[:limit]]
    return WorkflowPage(
        items=items,
        next_cursor=items[-1].id if len(rows) > limit else None,
    )


@router.get("/{workflow_id}", response_model=WorkflowDetails)
def get_workflow(
    workflow_id: UUID, session: Annotated[Session, Depends(get_session)]
) -> WorkflowDetails:
    workflow = session.get(Workflow, workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return _workflow_details(session, workflow)


class WorkflowEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    from_status: str
    to_status: str
    details: dict[str, object]
    created_at: datetime


@router.post("/{workflow_id}/run", response_model=WorkflowDetails)
def run_workflow(
    workflow_id: UUID,
    response: Response,
    session: Annotated[Session, Depends(get_session)],
    plan: PlanSelection | None = None,
) -> WorkflowDetails:
    try:
        with observe_stage("execution", workflow_id) as timing:
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
    response.headers["Server-Timing"] = f"execution;dur={timing.duration_ms:.2f}"
    return _workflow_details(session, workflow)


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
    workflow_id: UUID,
    response: Response,
    session: Annotated[Session, Depends(get_session)],
) -> WorkflowPlan:
    try:
        plan, duration = generate_workflow_plan(workflow_id, session)
    except WorkflowNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except WorkflowRunConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except PlannerRejected as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except PlannerError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    if duration is not None:
        response.headers["Server-Timing"] = f"planning;dur={duration:.2f}"
    return plan


class PlanJobView(BaseModel):
    status: Literal["idle", "planning", "ready", "failed"]
    plan: WorkflowPlan | None = None
    duration_ms: float | None = None
    error: str | None = None
    attempts: int | None = None
    error_code: str | None = None
    last_error_code: str | None = None


def _plan_job_view(workflow: Workflow) -> PlanJobView:
    job = workflow.plan_job or {}
    attempts = job.get("attempts")
    error_code = job.get("error_code")
    last_error_code = job.get("last_error_code")
    metadata = {
        "attempts": attempts if type(attempts) is int and attempts >= 0 else None,
        "error_code": error_code if isinstance(error_code, str) else None,
        "last_error_code": last_error_code if isinstance(last_error_code, str) else None,
    }
    if workflow.generated_plan is not None:
        duration = job.get("duration_ms")
        return PlanJobView(
            status="ready",
            plan=WorkflowPlan.model_validate(workflow.generated_plan),
            duration_ms=duration if isinstance(duration, (int, float)) else None,
            **metadata,
        )
    if (
        job.get("status") == "planning"
        and workflow.plan_job_run_at is not None
        and workflow.status == WorkflowStatus.PENDING
    ):
        return PlanJobView(status="planning", **metadata)
    if job:
        error = job.get("error")
        return PlanJobView(
            status="failed",
            error=error if isinstance(error, str) else "Planning expired. Try Preview plan again.",
            **metadata,
        )
    return PlanJobView(status="idle")


@router.post("/{workflow_id}/plan-job", response_model=PlanJobView, status_code=202)
def start_plan_job(
    workflow_id: UUID,
    response: Response,
    session: Annotated[Session, Depends(get_session)],
) -> PlanJobView:
    with session.begin():
        workflow = session.get(Workflow, workflow_id, with_for_update=True)
        if workflow is None:
            raise HTTPException(status_code=404, detail="Workflow not found")
        current = _plan_job_view(workflow)
        if current.status == "ready":
            response.status_code = 200
            return current
        if current.status == "planning":
            return current
        if workflow.status != WorkflowStatus.PENDING:
            raise HTTPException(status_code=409, detail="Workflow is no longer pending")

        enqueue_plan_job(session, workflow)

    return PlanJobView(status="planning", attempts=0)


@router.get("/{workflow_id}/plan-job", response_model=PlanJobView)
def get_plan_job(
    workflow_id: UUID,
    response: Response,
    session: Annotated[Session, Depends(get_session)],
) -> PlanJobView:
    workflow = session.get(Workflow, workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    response.headers["Cache-Control"] = "no-store"
    return _plan_job_view(workflow)


class ApprovalRequest(BaseModel):
    plan_hash: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


def require_approval_token(
    request: Request,
    token: Annotated[str | None, Header(alias="X-Axon-Approval-Token")] = None,
) -> None:
    expected = getenv("AXON_APPROVAL_TOKEN")
    if expected and token is not None and compare_digest(token.encode(), expected.encode()):
        return

    try:
        workflow_id = str(UUID(str(request.path_params.get("workflow_id", ""))))
    except ValueError:
        workflow_id = None

    if not expected:
        status_code = 503
        detail = "Approval is not configured"
        reason = "not_configured"
    else:
        status_code = 403
        detail = "Approval access denied"
        reason = "missing_token" if token is None else "invalid_token"

    log_event(
        "approval.access_denied",
        workflow_id=workflow_id,
        action=request.url.path.rsplit("/", 1)[-1],
        reason=reason,
        status_code=status_code,
    )
    raise HTTPException(status_code=status_code, detail=detail)


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
