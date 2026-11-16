from collections.abc import Callable
from typing import Annotated, cast
from uuid import UUID

from pydantic import ConfigDict, Field, TypeAdapter, ValidationError, with_config
from sqlalchemy.orm import Session
from typing_extensions import TypedDict

from app.persistence.models import FollowupTask
from app.tools.tickets import Ticket, TicketId, TicketText, list_tickets
from app.validation.plan import MAX_TICKET_LIMIT, PlanStep

ToolOutput = list[dict[str, object]]
ToolArguments = dict[str, int]
Tool = Callable[[Session, UUID, list[ToolOutput], ToolArguments], ToolOutput]


def _list_tickets(
    _session: Session,
    _workflow_id: UUID,
    _inputs: list[ToolOutput],
    arguments: ToolArguments,
) -> ToolOutput:
    return [dict(ticket) for ticket in list_tickets(limit=arguments["limit"])]


def _summarize_tickets(
    _session: Session,
    _workflow_id: UUID,
    inputs: list[ToolOutput],
    _arguments: ToolArguments,
) -> ToolOutput:
    (tickets,) = inputs
    return [
        {
            "ticket_id": ticket["id"],
            "summary": f"Priority {ticket['priority']}: {ticket['issue']}",
        }
        for ticket in tickets
    ]


def _create_followup_tasks(
    session: Session,
    workflow_id: UUID,
    inputs: list[ToolOutput],
    _arguments: ToolArguments,
) -> ToolOutput:
    (summaries,) = inputs
    for item in summaries:
        session.add(
            FollowupTask(
                workflow_id=workflow_id,
                ticket_id=str(item["ticket_id"]),
                summary=str(item["summary"]),
            )
        )
    return summaries


SUPPORT_TOOLS: dict[str, Tool] = {
    "list_tickets": _list_tickets,
    "summarize_tickets": _summarize_tickets,
    "create_followup_tasks": _create_followup_tasks,
}


class RetryableToolError(RuntimeError):
    pass


class PermanentToolError(RuntimeError):
    pass


@with_config(ConfigDict(extra="forbid"))
class TicketSummary(TypedDict):
    ticket_id: TicketId
    summary: TicketText


_TICKET_RECORDS = TypeAdapter(Annotated[list[Ticket], Field(max_length=MAX_TICKET_LIMIT)])
_SUMMARY_RECORDS = TypeAdapter(Annotated[list[TicketSummary], Field(max_length=MAX_TICKET_LIMIT)])

_OUTPUT_ADAPTERS = {
    "list_tickets": _TICKET_RECORDS,
    "summarize_tickets": _SUMMARY_RECORDS,
    "create_followup_tasks": _SUMMARY_RECORDS,
}
_INPUT_TOOL = {
    "summarize_tickets": "list_tickets",
    "create_followup_tasks": "summarize_tickets",
}


def _validate_records(tool: str, output: object) -> ToolOutput:
    adapter = _OUTPUT_ADAPTERS[tool]
    try:
        return cast(ToolOutput, adapter.validate_python(output, strict=True))
    except ValidationError as error:
        raise PermanentToolError(f"Invalid output for {tool}") from error


def validate_tool_inputs(tool: str, inputs: list[ToolOutput]) -> list[ToolOutput]:
    if tool == "list_tickets":
        if inputs:
            raise PermanentToolError("list_tickets takes no step outputs")
        return []
    parent_tool = _INPUT_TOOL.get(tool)
    if parent_tool is None:
        raise PermanentToolError("Tool has no input contract")
    if len(inputs) != 1:
        raise PermanentToolError(f"{tool} requires one step output")
    return [_validate_records(parent_tool, inputs[0])]


def validate_tool_output(step: PlanStep, output: object, inputs: list[ToolOutput]) -> ToolOutput:
    records = _validate_records(step.tool, output)
    if step.tool == "list_tickets":
        ticket_ids = {row["id"] for row in records}
        if len(records) > step.arguments.limit or len(ticket_ids) != len(records):
            raise PermanentToolError("list_tickets returned too many or duplicate tickets")
    elif step.tool == "summarize_tickets":
        if [row["ticket_id"] for row in records] != [row["id"] for row in inputs[0]]:
            raise PermanentToolError("summarize_tickets changed the selected ticket IDs")
    elif records != inputs[0]:
        raise PermanentToolError("create_followup_tasks changed the approved summaries")
    return records
