from enum import StrEnum
from graphlib import TopologicalSorter
from typing import Literal
from uuid import UUID

from sqlalchemy.orm import Session

from app.engine.state_machine import WorkflowStatus, transition
from app.persistence.models import Workflow, WorkflowEvent, WorkflowStep
from app.tools.registry import SUPPORT_TOOLS, ToolOutput
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


def execute_workflow(
    session: Session, workflow_id: UUID, selection: PlanSelection | None = None
) -> Workflow:
    with session.begin():
        workflow = _locked_workflow(session, workflow_id)
        starting = workflow.status == WorkflowStatus.PENDING
        if starting:
            snapshot = _select_plan(workflow, selection)
        elif workflow.status == WorkflowStatus.RUNNING and workflow.execution_plan is not None:
            snapshot = ExecutionSnapshot.model_validate(workflow.execution_plan)
            if selection is not None and selection != snapshot.selection:
                raise WorkflowRunConflict("Resume with the original plan")
        else:
            raise WorkflowRunConflict(f"Workflow is already {workflow.status}")

        steps = execution_order(snapshot.plan.steps)
        if starting:
            workflow.execution_plan = snapshot.model_dump(mode="json")
            _change_status(session, workflow, WorkflowStatus.RUNNING, {})

    outputs: dict[str, ToolOutput] = {}
    for position, step in enumerate(steps, start=1):
        with session.begin():
            workflow = _locked_workflow(session, workflow_id)
            saved = session.get(WorkflowStep, (workflow_id, step.name))
            if saved is not None:
                outputs[step.name] = saved.output
                continue

            if workflow.status != WorkflowStatus.RUNNING:
                raise WorkflowRunConflict(f"Workflow is already {workflow.status}")

            inputs = [outputs[name] for name in step.depends_on]
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
