from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from json import dumps
from logging import INFO, Formatter, StreamHandler, getLogger
from sys import stdout
from time import perf_counter
from typing import Literal
from uuid import UUID

from prometheus_client import CollectorRegistry, Histogram

from app.observability.tracing import active_trace_id, trace_span

current_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)

logger = getLogger("axon.telemetry")
handler = StreamHandler(stdout)
handler.setFormatter(Formatter("%(message)s"))
logger.addHandler(handler)
logger.setLevel(INFO)
logger.propagate = False

metrics_registry = CollectorRegistry()
stage_duration = Histogram(
    "axon_stage_duration_seconds",
    "Time spent in an Axon workflow stage.",
    labelnames=("stage", "outcome"),
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 40, 90, 180),
    registry=metrics_registry,
)


def log_event(event: str, **fields: object) -> None:
    if trace_id := active_trace_id():
        fields["trace_id"] = trace_id
    logger.info(dumps({
        "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "event": event,
        "request_id": current_request_id.get(),
        **fields,
    }))


@contextmanager
def observe_stage(
    stage: Literal["planning", "execution"], workflow_id: UUID
) -> Iterator[None]:
    with trace_span(f"workflow.{stage}", attributes={"axon.workflow_id": str(workflow_id)}):
        started = perf_counter()
        outcome = "ok"
        try:
            yield
        except Exception:
            outcome = "error"
            raise
        finally:
            seconds = perf_counter() - started
            stage_duration.labels(stage=stage, outcome=outcome).observe(seconds)
            log_event(
                "workflow.stage",
                stage=stage,
                workflow_id=str(workflow_id),
                outcome=outcome,
                duration_ms=round(seconds * 1000, 2),
            )
