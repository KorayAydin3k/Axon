from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class PlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


StepName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


class StepSpec(PlanModel):
    name: StepName
    depends_on: tuple[StepName, ...]


TicketLimit = Annotated[int, Field(strict=True, ge=1, le=10)]


class ListTicketsArguments(PlanModel):
    limit: TicketLimit


class NoArguments(PlanModel):
    pass


class ListTicketsStep(StepSpec):
    tool: Literal["list_tickets"]
    arguments: ListTicketsArguments


class SummarizeTicketsStep(StepSpec):
    tool: Literal["summarize_tickets"]
    arguments: NoArguments


class CreateFollowupTasksStep(StepSpec):
    tool: Literal["create_followup_tasks"]
    arguments: NoArguments


PlanStep = ListTicketsStep | SummarizeTicketsStep | CreateFollowupTasksStep


class WorkflowPlan(PlanModel):
    steps: Annotated[tuple[PlanStep, ...], Field(min_length=1, max_length=3)]


class SupportAction(StrEnum):
    LIST = "list_tickets"
    SUMMARIZE = "summarize_tickets"
    FOLLOWUP = "create_followup_tasks"


def build_support_plan(action: SupportAction, limit: int) -> WorkflowPlan:
    steps: list[PlanStep] = [
        ListTicketsStep(
            name="find_tickets", tool="list_tickets", depends_on=(),
            arguments=ListTicketsArguments(limit=limit),
        )
    ]
    if action in (SupportAction.SUMMARIZE, SupportAction.FOLLOWUP):
        steps.append(SummarizeTicketsStep(
            name="summarize_tickets", tool="summarize_tickets",
            depends_on=("find_tickets",), arguments=NoArguments(),
        ))
    if action is SupportAction.FOLLOWUP:
        steps.append(CreateFollowupTasksStep(
            name="create_tasks", tool="create_followup_tasks",
            depends_on=("summarize_tickets",), arguments=NoArguments(),
        ))
    return WorkflowPlan(steps=tuple(steps))
