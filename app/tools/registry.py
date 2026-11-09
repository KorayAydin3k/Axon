from collections.abc import Callable
from uuid import UUID

from sqlalchemy.orm import Session

from app.persistence.models import FollowupTask
from app.tools.tickets import list_tickets

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
