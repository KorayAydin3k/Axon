from enum import StrEnum


class WorkflowStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class InvalidWorkflowTransition(ValueError):
    pass


_ALLOWED_TRANSITIONS = {
    WorkflowStatus.PENDING: {WorkflowStatus.RUNNING},
    WorkflowStatus.RUNNING: {WorkflowStatus.COMPLETED, WorkflowStatus.FAILED},
    WorkflowStatus.COMPLETED: set(),
    WorkflowStatus.FAILED: set(),
}


def transition(current: WorkflowStatus, target: WorkflowStatus) -> WorkflowStatus:
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise InvalidWorkflowTransition(f"{current.value} -> {target.value}")

    return target
