from enum import StrEnum


class WorkflowStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    RETRYING = "RETRYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class InvalidWorkflowTransition(ValueError):
    pass


_ALLOWED_TRANSITIONS = {
    WorkflowStatus.PENDING: {WorkflowStatus.RUNNING},
    WorkflowStatus.RUNNING: {
        WorkflowStatus.RETRYING, WorkflowStatus.WAITING_FOR_APPROVAL,
        WorkflowStatus.COMPLETED, WorkflowStatus.FAILED,
    },
    WorkflowStatus.RETRYING: {WorkflowStatus.RUNNING},
    WorkflowStatus.COMPLETED: set(),
    WorkflowStatus.FAILED: set(),
    WorkflowStatus.CANCELLED: set(),
    WorkflowStatus.WAITING_FOR_APPROVAL: {
        WorkflowStatus.RUNNING, WorkflowStatus.CANCELLED,
    },
}


def transition(current: WorkflowStatus, target: WorkflowStatus) -> WorkflowStatus:
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise InvalidWorkflowTransition(f"{current.value} -> {target.value}")

    return target
