from collections.abc import Callable
from uuid import UUID

from sqlalchemy.orm import Session

from app.persistence.models import FollowupTask
from app.tools.tickets import list_tickets

ToolOutput = list[dict[str, object]]
Tool = Callable[[Session, UUID, list[ToolOutput]], ToolOutput]


def _list_tickets(
    _session: Session, _workflow_id: UUID, _inputs: list[ToolOutput]
) -> ToolOutput:
    return [dict(ticket) for ticket in list_tickets(limit=3)]


def _summarize_tickets(
    _session: Session, _workflow_id: UUID, inputs: list[ToolOutput]
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
    session: Session, workflow_id: UUID, inputs: list[ToolOutput]
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
