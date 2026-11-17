from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.engine.state_machine import WorkflowStatus
from app.engine.workflow_engine import WorkflowNotFound, WorkflowRunConflict
from app.observability.telemetry import current_request_id, log_event, observe_stage
from app.persistence.database import engine
from app.persistence.models import Workflow
from app.planner.ollama_planner import PlannerError, PlannerRejected, generate_plan
from app.validation.plan import WorkflowPlan


CLAIM_LEASE = timedelta(seconds=210)
TIMEOUT_RETRY_DELAY = timedelta(seconds=2)
MAX_ATTEMPTS = 3
FAILURE_MESSAGE = "Plan generation failed. Try Preview plan again."


@dataclass(frozen=True)
class PlanJobClaim:
    workflow_id: UUID
    job_id: str
    token: str
    request_id: str | None = None


def enqueue_plan_job(session: Session, workflow: Workflow) -> None:
    workflow.plan_job = {
        "id": uuid4().hex,
        "status": "planning",
        "attempts": 0,
        "request_id": current_request_id.get(),
    }
    workflow.plan_job_run_at = session.scalar(select(func.clock_timestamp()))


def claim_plan_job(workflow_id: UUID | None = None) -> PlanJobClaim | None:
    query = (
        select(Workflow)
        .where(Workflow.plan_job_run_at <= func.now())
        .order_by(Workflow.plan_job_run_at, Workflow.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if workflow_id is not None:
        query = query.where(Workflow.id == workflow_id)

    with Session(engine) as session, session.begin():
        workflow = session.scalar(query)
        if workflow is None:
            return None

        job = workflow.plan_job or {}
        job_id = job.get("id")
        if (
            not isinstance(job_id, str)
            or job.get("status") != "planning"
            or workflow.generated_plan is not None
            or workflow.status != WorkflowStatus.PENDING
        ):
            workflow.plan_job_run_at = None
            return None

        attempts = job.get("attempts")
        attempt = attempts + 1 if isinstance(attempts, int) else 1
        if attempt > MAX_ATTEMPTS:
            _fail_plan_job(workflow, job_id, "Planning was interrupted. Try Preview plan again.")
            return None

        token = uuid4().hex
        request_id = job.get("request_id")
        request_id = request_id if isinstance(request_id, str) else None
        now = session.scalar(select(func.clock_timestamp()))
        workflow.plan_job = {
            "id": job_id,
            "status": "planning",
            "attempts": attempt,
            "claim_token": token,
            "request_id": request_id,
        }
        workflow.plan_job_run_at = now + CLAIM_LEASE
        return PlanJobClaim(workflow.id, job_id, token, request_id)


def _owns_plan_job(session: Session, workflow: Workflow, claim: PlanJobClaim) -> bool:
    job = workflow.plan_job or {}
    return (
        workflow.id == claim.workflow_id
        and job.get("status") == "planning"
        and job.get("id") == claim.job_id
        and job.get("claim_token") == claim.token
        and workflow.plan_job_run_at is not None
        and workflow.plan_job_run_at > session.scalar(select(func.clock_timestamp()))
    )


def _load_for_planning(
    session: Session, workflow_id: UUID, claim: PlanJobClaim | None, *, lock: bool,
) -> Workflow:
    workflow = session.get(Workflow, workflow_id, with_for_update=lock, populate_existing=True)
    if workflow is None:
        raise WorkflowNotFound("Workflow not found")
    if claim is not None and not _owns_plan_job(session, workflow, claim):
        raise WorkflowRunConflict("Planning job expired")
    if workflow.generated_plan is None and workflow.status != WorkflowStatus.PENDING:
        raise WorkflowRunConflict("Workflow is no longer pending")
    return workflow


def generate_workflow_plan(
    workflow_id: UUID, session: Session, claim: PlanJobClaim | None = None,
) -> tuple[WorkflowPlan, float | None]:
    with session.begin():
        workflow = _load_for_planning(session, workflow_id, claim, lock=False)
        if workflow.generated_plan is not None:
            return WorkflowPlan.model_validate(workflow.generated_plan), None
        request_text = workflow.request

    with observe_stage("planning", workflow_id) as timing:
        try:
            candidate = generate_plan(request_text)
        except PlannerError as error:
            log_event("planning.failed", workflow_id=str(workflow_id), error_code=error.code)
            raise

    with session.begin():
        workflow = _load_for_planning(session, workflow_id, claim, lock=True)
        if workflow.generated_plan is not None:
            return WorkflowPlan.model_validate(workflow.generated_plan), None

        workflow.generated_plan = candidate.model_dump(mode="json")
        job = workflow.plan_job or {}
        if job.get("status") == "planning":
            workflow.plan_job = {
                "id": job.get("id"),
                "status": "ready",
                "duration_ms": timing.duration_ms,
                "request_id": job.get("request_id"),
            }
        workflow.plan_job_run_at = None

    return candidate, timing.duration_ms


def _fail_plan_job(workflow: Workflow, job_id: str, message: str) -> None:
    previous = workflow.plan_job or {}
    workflow.plan_job = {
        "id": job_id,
        "status": "failed",
        "error": message,
        "request_id": previous.get("request_id"),
    }
    workflow.plan_job_run_at = None


def run_plan_job(claim: PlanJobClaim) -> None:
    context = current_request_id.set(claim.request_id)
    try:
        _run_claimed_plan_job(claim)
    finally:
        current_request_id.reset(context)


def _run_claimed_plan_job(claim: PlanJobClaim) -> None:
    retry_timeout = False
    try:
        with Session(engine) as session:
            generate_workflow_plan(claim.workflow_id, session, claim)
    except PlannerRejected as error:
        message = str(error)
    except PlannerError as error:
        retry_timeout = error.code == "ollama_timeout"
        message = "Ollama timed out. Try Preview plan again." if retry_timeout else FAILURE_MESSAGE
    except (WorkflowNotFound, WorkflowRunConflict):
        message = FAILURE_MESSAGE
    else:
        return

    with Session(engine) as session, session.begin():
        workflow = session.get(Workflow, claim.workflow_id, with_for_update=True)
        if workflow is None or not _owns_plan_job(session, workflow, claim):
            return

        job = workflow.plan_job or {}
        attempts = job.get("attempts")
        if (
            retry_timeout
            and workflow.status == WorkflowStatus.PENDING
            and isinstance(attempts, int)
            and attempts < MAX_ATTEMPTS
        ):
            now = session.scalar(select(func.clock_timestamp()))
            workflow.plan_job = {
                "id": claim.job_id,
                "status": "planning",
                "attempts": attempts,
                "request_id": claim.request_id,
            }
            workflow.plan_job_run_at = now + TIMEOUT_RETRY_DELAY
        else:
            _fail_plan_job(workflow, claim.job_id, message)
            return

    log_event(
        "planning.retry_scheduled",
        workflow_id=str(claim.workflow_id),
        job_id=claim.job_id,
        error_code="ollama_timeout",
        next_attempt=attempts + 1,
    )
