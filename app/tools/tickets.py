from typing import Annotated

from pydantic import ConfigDict, StringConstraints, with_config
from typing_extensions import TypedDict


TicketId = Annotated[str, StringConstraints(min_length=1, max_length=32)]
TicketText = Annotated[str, StringConstraints(min_length=1)]


@with_config(ConfigDict(extra="forbid"))
class Ticket(TypedDict):
    id: TicketId
    priority: int
    issue: TicketText


_SAMPLE_TICKETS: tuple[Ticket, ...] = (
    {"id": "T-101", "priority": 3, "issue": "Duplicate invoice"},
    {"id": "T-102", "priority": 5, "issue": "Checkout unavailable"},
    {"id": "T-103", "priority": 2, "issue": "Profile image not updating"},
    {"id": "T-104", "priority": 4, "issue": "Account access blocked"},
)


def list_tickets(limit: int) -> list[Ticket]:
    ranked = sorted(_SAMPLE_TICKETS, key=lambda ticket: (-ticket["priority"], ticket["id"]))
    return ranked[:limit]
