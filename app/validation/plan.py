from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class PlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


StepName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


class StepSpec(PlanModel):
    name: StepName
    depends_on: tuple[StepName, ...]


class ListTicketsArguments(PlanModel):
    limit: Annotated[int, Field(strict=True, ge=1, le=10)]


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
