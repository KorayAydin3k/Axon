from datetime import UTC, datetime, timedelta
from enum import StrEnum
from graphlib import TopologicalSorter
from hashlib import sha256
from json import dumps
from time import sleep
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.engine.state_machine import WorkflowStatus, transition
from app.persistence.models import Workflow, WorkflowApproval, WorkflowEvent, WorkflowStep
from app.tools.registry import (
    APPROVAL_REQUIRED_TOOLS,
    PermanentToolError,
    RetryableToolError,
    SUPPORT_TOOLS,
    ToolOutput,
)
from app.validation.plan import PlanModel, PlanStep, WorkflowPlan


class SupportPlan(StrEnum):
    TICKET_LIST = "ticket_list"
    SUPPORT_FOLLOWUP = "support_followup"


_FIND_TICKETS = {
    "name": "find_tickets",
    "tool": "list_tickets",
    "depends_on": [],
    "arguments": {"limit": 3},
}
_PLANS = {
    SupportPlan.TICKET_LIST: (_FIND_TICKETS,),
    SupportPlan.SUPPORT_FOLLOWUP: (
        _FIND_TICKETS,
        {
            "name": "summarize_tickets",
            "tool": "summarize_tickets",
            "depends_on": ["find_tickets"],
            "arguments": {},
        },
        {
            "name": "create_tasks",
            "tool": "create_followup_tasks",
            "depends_on": ["summarize_tickets"],
            "arguments": {},
        },
    ),
}
_REQUIRED_INPUTS = {
    "list_tickets": (),
    "summarize_tickets": ("list_tickets",),
    "create_followup_tasks": ("summarize_tickets",),
}


def execution_order(steps: tuple[PlanStep, ...]) -> list[PlanStep]:
    by_name = {step.name: step for step in steps}
    if len(by_name) != len(steps):
        raise ValueError("Duplicate step name")
    if len({step.tool for step in steps}) != len(steps):
        raise ValueError("A tool may appear only once in a support plan")

    for step in steps:
        if step.tool not in SUPPORT_TOOLS:
            raise ValueError(f"Unregistered tool: {step.tool}")
        missing = set(step.depends_on) - by_name.keys()
        if missing:
            raise ValueError(f"Unknown dependencies for {step.name}: {sorted(missing)}")

    graph = {step.name: step.depends_on for step in steps}
    ordered = [by_name[name] for name in TopologicalSorter(graph).static_order()]
    for step in ordered:
        input_tools = tuple(by_name[name].tool for name in step.depends_on)
        if input_tools != _REQUIRED_INPUTS[step.tool]:
            raise ValueError(f"Incompatible dependencies for {step.name}")

    return ordered


def _change_status(
    session: Session,
    workflow: Workflow,
    target: WorkflowStatus,
    details: dict[str, object],
) -> None:
    current = WorkflowStatus(workflow.status)
    workflow.status = transition(current, target).value
    session.add(
        WorkflowEvent(
            workflow_id=workflow.id,
            from_status=current.value,
            to_status=target.value,
            details=details,
        )
    )


PlanSelection = SupportPlan | Literal["generated"]


class WorkflowNotFound(LookupError):
    pass


class WorkflowRunConflict(ValueError):
    pass


class WorkflowToolFailed(RuntimeError):
    pass


_RETRY_DELAYS = (1, 2, 4)


class RetrySchedule(PlanModel):
    step: str
    failure_count: int
    retry_at: datetime


class ExecutionSnapshot(PlanModel):
    selection: PlanSelection
    plan: WorkflowPlan


def _locked_workflow(session: Session, workflow_id: UUID) -> Workflow:
    workflow = session.get(
        Workflow, workflow_id, with_for_update=True, populate_existing=True
    )
    if workflow is None:
        raise WorkflowNotFound("Workflow not found")
    return workflow


def _select_plan(
    workflow: Workflow, selection: PlanSelection | None
) -> ExecutionSnapshot:
    if workflow.generated_plan is not None:
        if selection not in (None, "generated"):
            raise WorkflowRunConflict("Workflow already has a generated plan")
        return ExecutionSnapshot(
            selection="generated",
            plan=WorkflowPlan.model_validate(workflow.generated_plan),
        )

    if selection == "generated":
        raise WorkflowRunConflict("Generate a plan first")

    demo = selection or SupportPlan.TICKET_LIST
    return ExecutionSnapshot(
        selection=demo,
        plan=WorkflowPlan.model_validate({"steps": _PLANS[demo]}),
    )


ApprovalDecision = Literal["approved", "rejected"]


def plan_fingerprint(plan: object) -> str:
    canonical = dumps(plan, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


def _check_plan_fingerprint(workflow: Workflow, plan_hash: str) -> None:
    snapshot = workflow.execution_plan
    if snapshot is None or plan_fingerprint(snapshot["plan"]) != plan_hash:
        raise WorkflowRunConflict("Approval does not match the current execution plan")


def _approval_granted(
    session: Session,
    workflow: Workflow,
    step: PlanStep,
    inputs: list[ToolOutput],
    plan_hash: str,
) -> bool:
    _check_plan_fingerprint(workflow, plan_hash)
    approval = session.get(WorkflowApproval, (workflow.id, plan_hash))
    if approval is not None:
        return approval.decision == "approved"

    if workflow.status != WorkflowStatus.RUNNING:
        raise WorkflowRunConflict("Approval request does not match the current plan")

    (summaries,) = inputs
    ticket_ids = [item["ticket_id"] for item in summaries]
    session.add(
        WorkflowApproval(
            workflow_id=workflow.id,
            plan_hash=plan_hash,
            step_name=step.name,
        )
    )
    _change_status(
        session, workflow, WorkflowStatus.WAITING_FOR_APPROVAL,
        {
            "plan_hash": plan_hash,
            "step": step.name,
            "tool": step.tool,
            "ticket_ids": ticket_ids,
            "task_count": len(ticket_ids),
        },
    )
    return False


def decide_approval(
    session: Session,
    workflow_id: UUID,
    plan_hash: str,
    decision: ApprovalDecision,
) -> Workflow:
    with session.begin():
        workflow = _locked_workflow(session, workflow_id)
        _check_plan_fingerprint(workflow, plan_hash)
        approval = session.get(WorkflowApproval, (workflow_id, plan_hash))
        if approval is None:
            raise WorkflowRunConflict("No approval request exists for this plan")
        if approval.decision == decision:
            return workflow
        if approval.decision is not None or workflow.status != WorkflowStatus.WAITING_FOR_APPROVAL:
            raise WorkflowRunConflict("Approval is no longer pending")

        approval.decision = decision
        approval.decided_at = datetime.now(UTC)
        target = WorkflowStatus.RUNNING if decision == "approved" else WorkflowStatus.CANCELLED
        _change_status(
            session, workflow, target,
            {"plan_hash": plan_hash, "step": approval.step_name, "decision": decision},
        )
        return workflow


def _execute_step(
    session: Session,
    workflow_id: UUID,
    step: PlanStep,
    position: int,
    inputs: list[ToolOutput],
    plan_hash: str,
) -> ToolOutput | None:
    while True:
        wait_seconds = 0.0
        terminal_failure = False

        with session.begin():
            workflow = _locked_workflow(session, workflow_id)
            saved = session.get(WorkflowStep, (workflow_id, step.name))
            if saved is not None:
                return saved.output

            if workflow.status not in (
                WorkflowStatus.RUNNING,
                WorkflowStatus.RETRYING,
                WorkflowStatus.WAITING_FOR_APPROVAL,
            ):
                raise WorkflowRunConflict(f"Workflow is already {workflow.status}")

            if step.tool in APPROVAL_REQUIRED_TOOLS:
                if not _approval_granted(session, workflow, step, inputs, plan_hash):
                    return None
            if workflow.status == WorkflowStatus.WAITING_FOR_APPROVAL:
                raise WorkflowRunConflict("Workflow is waiting for approval")

            failure_count = 0
            if workflow.status == WorkflowStatus.RETRYING:
                event = session.scalar(
                    select(WorkflowEvent)
                    .where(
                        WorkflowEvent.workflow_id == workflow_id,
                        WorkflowEvent.to_status == WorkflowStatus.RETRYING,
                    )
                    .order_by(WorkflowEvent.id.desc())
                    .limit(1)
                )
                if event is None:
                    raise WorkflowRunConflict("Retry schedule is missing")
                schedule = RetrySchedule.model_validate(event.details)
                if schedule.step != step.name:
                    raise WorkflowRunConflict("Retry schedule belongs to a different step")

                failure_count = schedule.failure_count
                wait_seconds = max(
                    0.0, (schedule.retry_at - datetime.now(UTC)).total_seconds()
                )
                if wait_seconds == 0:
                    _change_status(
                        session, workflow, WorkflowStatus.RUNNING, {"step": step.name}
                    )

            if wait_seconds == 0:
                try:
                    with session.begin_nested():
                        output = SUPPORT_TOOLS[step.tool](
                            session, workflow_id, inputs, step.arguments.model_dump()
                        )
                        session.add(
                            WorkflowStep(
                                workflow_id=workflow_id,
                                name=step.name,
                                tool=step.tool,
                                position=position,
                                output=output,
                            )
                        )
                except (RetryableToolError, TimeoutError, PermanentToolError) as error:
                    failure_count += 1
                    retryable = isinstance(error, (RetryableToolError, TimeoutError))
                    if retryable and failure_count <= len(_RETRY_DELAYS):
                        wait_seconds = _RETRY_DELAYS[failure_count - 1]
                        schedule = RetrySchedule(
                            step=step.name,
                            failure_count=failure_count,
                            retry_at=datetime.now(UTC) + timedelta(seconds=wait_seconds),
                        )
                        _change_status(
                            session, workflow, WorkflowStatus.RETRYING,
                            schedule.model_dump(mode="json"),
                        )
                    else:
                        terminal_failure = True
                        _change_status(
                            session, workflow, WorkflowStatus.FAILED,
                            {
                                "step": step.name,
                                "failure_count": failure_count,
                                "reason": "retry_exhausted" if retryable else "permanent",
                            },
                        )
                else:
                    return output

        if terminal_failure:
            raise WorkflowToolFailed(f"Workflow failed at step '{step.name}'")
        if wait_seconds > 0:
            sleep(wait_seconds)


def execute_workflow(
    session: Session, workflow_id: UUID, selection: PlanSelection | None = None
) -> Workflow:
    with session.begin():
        workflow = _locked_workflow(session, workflow_id)
        starting = workflow.status == WorkflowStatus.PENDING
        if starting:
            snapshot = _select_plan(workflow, selection)
        elif (
            workflow.status in (
                WorkflowStatus.RUNNING,
                WorkflowStatus.RETRYING,
                WorkflowStatus.WAITING_FOR_APPROVAL,
            )
            and workflow.execution_plan is not None
        ):
            snapshot = ExecutionSnapshot.model_validate(workflow.execution_plan)
            if selection is not None and selection != snapshot.selection:
                raise WorkflowRunConflict("Resume with the original plan")
        else:
            raise WorkflowRunConflict(f"Workflow is already {workflow.status}")

        steps = execution_order(snapshot.plan.steps)
        if starting:
            workflow.execution_plan = snapshot.model_dump(mode="json")
            _change_status(session, workflow, WorkflowStatus.RUNNING, {})

    plan_hash = plan_fingerprint(snapshot.plan.model_dump(mode="json"))
    outputs: dict[str, ToolOutput] = {}
    for position, step in enumerate(steps, start=1):
        inputs = [outputs[name] for name in step.depends_on]
        output = _execute_step(session, workflow_id, step, position, inputs, plan_hash)
        if output is None:
            return workflow
        outputs[step.name] = output

    with session.begin():
        workflow = _locked_workflow(session, workflow_id)
        if workflow.status == WorkflowStatus.RUNNING:
            tool_outputs = {step.tool: outputs[step.name] for step in steps}
            _change_status(
                session,
                workflow,
                WorkflowStatus.COMPLETED,
                {
                    "tool": steps[-1].tool,
                    "tickets": tool_outputs["list_tickets"],
                    "steps": [step.name for step in steps],
                    "followup_tasks": tool_outputs.get("create_followup_tasks", []),
                },
            )
        return workflow
