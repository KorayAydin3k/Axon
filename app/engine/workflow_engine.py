from dataclasses import dataclass
from enum import StrEnum
from graphlib import TopologicalSorter

from sqlalchemy.orm import Session

from app.engine.state_machine import WorkflowStatus, transition
from app.persistence.models import Workflow, WorkflowEvent, WorkflowStep
from app.tools.registry import SUPPORT_TOOLS, ToolOutput


class SupportPlan(StrEnum):
    TICKET_LIST = "ticket_list"
    SUPPORT_FOLLOWUP = "support_followup"


@dataclass(frozen=True)
class StepSpec:
    name: str
    tool: str
    depends_on: tuple[str, ...] = ()


_FIND_TICKETS = StepSpec("find_tickets", "list_tickets")
_PLANS = {
    SupportPlan.TICKET_LIST: (_FIND_TICKETS,),
    SupportPlan.SUPPORT_FOLLOWUP: (
        _FIND_TICKETS,
        StepSpec("summarize_tickets", "summarize_tickets", ("find_tickets",)),
        StepSpec("create_tasks", "create_followup_tasks", ("summarize_tickets",)),
    ),
}


def execution_order(steps: tuple[StepSpec, ...]) -> list[StepSpec]:
    by_name = {step.name: step for step in steps}
    if len(by_name) != len(steps):
        raise ValueError("Duplicate step name")

    for step in steps:
        if step.tool not in SUPPORT_TOOLS:
            raise ValueError(f"Unknown tool: {step.tool}")
        missing = set(step.depends_on) - by_name.keys()
        if missing:
            raise ValueError(f"Unknown dependencies for {step.name}: {sorted(missing)}")

    graph = {step.name: step.depends_on for step in steps}
    return [by_name[name] for name in TopologicalSorter(graph).static_order()]


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


def run_support_demo(
    session: Session, workflow: Workflow, plan: SupportPlan
) -> None:
    steps = execution_order(_PLANS[plan])
    _change_status(session, workflow, WorkflowStatus.RUNNING, {})

    outputs: dict[str, ToolOutput] = {}
    for position, step in enumerate(steps, start=1):
        inputs = [outputs[name] for name in step.depends_on]
        output = SUPPORT_TOOLS[step.tool](session, workflow.id, inputs)
        outputs[step.name] = output
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
            "tickets": outputs["find_tickets"],
            "steps": [step.name for step in steps],
            "followup_tasks": outputs.get("create_tasks", []),
        },
    )
