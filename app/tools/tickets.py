from typing import TypedDict


class Ticket(TypedDict):
    id: str
    priority: int
    issue: str


_SAMPLE_TICKETS: tuple[Ticket, ...] = (
    {"id": "T-101", "priority": 3, "issue": "Duplicate invoice"},
    {"id": "T-102", "priority": 5, "issue": "Checkout unavailable"},
    {"id": "T-103", "priority": 2, "issue": "Profile image not updating"},
    {"id": "T-104", "priority": 4, "issue": "Account access blocked"},
)


def list_tickets(limit: int) -> list[Ticket]:
    ranked = sorted(_SAMPLE_TICKETS, key=lambda ticket: (-ticket["priority"], ticket["id"]))
    return ranked[:limit]
