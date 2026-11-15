from collections.abc import Iterator
from datetime import datetime
from os import getenv
from secrets import compare_digest
from time import time
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Response, status
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
) -> WorkflowPage:
    query = select(Workflow.id, Workflow.status, Workflow.created_at)
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
    response: Response,
    session: Annotated[Session, Depends(get_session)],
    plan: PlanSelection | None = None,
) -> WorkflowResponse:
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


def _generate_workflow_plan(
    workflow_id: UUID, session: Session, job_id: str | None = None,
) -> tuple[WorkflowPlan, float | None]:
    with session.begin():
        workflow = session.get(Workflow, workflow_id)
        if workflow is None:
            raise HTTPException(status_code=404, detail="Workflow not found")
        if workflow.generated_plan is not None:
            return WorkflowPlan.model_validate(workflow.generated_plan), None
        if job_id is not None and not _owns_plan_job(workflow, job_id):
            raise HTTPException(status_code=409, detail="Planning job expired")
        if workflow.status != WorkflowStatus.PENDING:
            raise HTTPException(status_code=409, detail="Workflow is no longer pending")
        request_text = workflow.request

    try:
        with observe_stage("planning", workflow_id) as timing:
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
            return WorkflowPlan.model_validate(workflow.generated_plan), None
        if job_id is not None and not _owns_plan_job(workflow, job_id):
            raise HTTPException(status_code=409, detail="Planning job expired")
        if workflow.status != WorkflowStatus.PENDING:
            raise HTTPException(status_code=409, detail="Workflow is no longer pending")

        workflow.generated_plan = candidate.model_dump(mode="json")
        if job_id is not None:
            workflow.plan_job = {"id": job_id, "status": "ready", "duration_ms": timing.duration_ms}

    return candidate, timing.duration_ms


@router.post("/{workflow_id}/plan", response_model=WorkflowPlan)
def preview_workflow_plan(
    workflow_id: UUID,
    response: Response,
    session: Annotated[Session, Depends(get_session)],
) -> WorkflowPlan:
    plan, duration = _generate_workflow_plan(workflow_id, session)
    if duration is not None:
        response.headers["Server-Timing"] = f"planning;dur={duration:.2f}"
    return plan


class PlanJobView(BaseModel):
    status: Literal["idle", "planning", "ready", "failed"]
    plan: WorkflowPlan | None = None
    duration_ms: float | None = None
    error: str | None = None


def _job_active(job: dict[str, object]) -> bool:
    deadline = job.get("deadline")
    return job.get("status") == "planning" and isinstance(deadline, (int, float)) and deadline > time()


def _owns_plan_job(workflow: Workflow, job_id: str) -> bool:
    job = workflow.plan_job or {}
    return job.get("id") == job_id and _job_active(job)


def _plan_job_view(workflow: Workflow) -> PlanJobView:
    job = workflow.plan_job or {}
    if workflow.generated_plan is not None:
        duration = job.get("duration_ms")
        return PlanJobView(
            status="ready",
            plan=WorkflowPlan.model_validate(workflow.generated_plan),
            duration_ms=duration if isinstance(duration, (int, float)) else None,
        )
    if _job_active(job):
        return PlanJobView(status="planning")
    if job:
        error = job.get("error")
        return PlanJobView(
            status="failed",
            error=error if isinstance(error, str) else "Planning expired. Try Preview plan again.",
        )
    return PlanJobView(status="idle")


def _run_plan_job(workflow_id: UUID, job_id: str) -> None:
    message = "Plan generation failed. Try Preview plan again."
    try:
        with Session(engine) as session:
            _generate_workflow_plan(workflow_id, session, job_id)
    except HTTPException as error:
        if error.status_code == 422 and isinstance(error.detail, str):
            message = error.detail
    except Exception as error:
        log_event("planning.job_error", error_type=type(error).__name__)
    else:
        return

    try:
        with Session(engine) as session, session.begin():
            workflow = session.get(Workflow, workflow_id, with_for_update=True)
            if workflow is not None and workflow.generated_plan is None and _owns_plan_job(workflow, job_id):
                workflow.plan_job = {"id": job_id, "status": "failed", "error": message}
    except Exception as error:
        log_event("planning.job_error", error_type=type(error).__name__)


@router.post("/{workflow_id}/plan-job", response_model=PlanJobView, status_code=202)
def start_plan_job(
    workflow_id: UUID,
    response: Response,
    background_tasks: BackgroundTasks,
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

        job_id = uuid4().hex
        workflow.plan_job = {"id": job_id, "status": "planning", "deadline": time() + 210}

    background_tasks.add_task(_run_plan_job, workflow_id, job_id)
    return PlanJobView(status="planning")


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
