from enum import StrEnum
from graphlib import TopologicalSorter
from json import dumps

from sqlalchemy.orm import Session

from app.engine.state_machine import WorkflowStatus, transition
from app.persistence.models import Workflow, WorkflowEvent, WorkflowStep
from app.tools.registry import SUPPORT_TOOLS, ToolOutput
from app.validation.plan import PlanStep, WorkflowPlan


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


def run_plan(session: Session, workflow: Workflow, raw_plan: str) -> None:
    plan = WorkflowPlan.model_validate_json(raw_plan)
    steps = execution_order(plan.steps)
    _change_status(session, workflow, WorkflowStatus.RUNNING, {})

    outputs: dict[str, ToolOutput] = {}
    tool_outputs: dict[str, ToolOutput] = {}
    for position, step in enumerate(steps, start=1):
        inputs = [outputs[name] for name in step.depends_on]
        output = SUPPORT_TOOLS[step.tool](
            session, workflow.id, inputs, step.arguments.model_dump()
        )
        outputs[step.name] = output
        tool_outputs[step.tool] = output
        session.add(
            WorkflowStep(
                workflow_id=workflow.id,
                name=step.name,
                tool=step.tool,
                position=position,
                output=output,
            )
        )

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


def run_support_demo(session: Session, workflow: Workflow, plan: SupportPlan) -> None:
    run_plan(session, workflow, dumps({"steps": _PLANS[plan]}))
