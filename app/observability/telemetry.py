from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
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
tool_duration = Histogram(
    "axon_tool_duration_seconds",
    "Time spent invoking an Axon tool and validating its output.",
    labelnames=("tool", "outcome"),
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20),
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


@dataclass
class StageMeasurement:
    duration_ms: float = 0.0


@contextmanager
def observe_stage(
    stage: Literal["planning", "execution"], workflow_id: UUID
) -> Iterator[StageMeasurement]:
    with trace_span(f"workflow.{stage}", attributes={"axon.workflow_id": str(workflow_id)}):
        started = perf_counter()
        outcome = "ok"
        measurement = StageMeasurement()
        try:
            yield measurement
        except Exception:
            outcome = "error"
            raise
        finally:
            seconds = perf_counter() - started
            measurement.duration_ms = round(seconds * 1000, 2)
            stage_duration.labels(stage=stage, outcome=outcome).observe(seconds)
            log_event(
                "workflow.stage",
                stage=stage,
                workflow_id=str(workflow_id),
                outcome=outcome,
                duration_ms=measurement.duration_ms,
            )


@contextmanager
def observe_tool(
    workflow_id: UUID, tool: str, step_name: str, position: int,
) -> Iterator[StageMeasurement]:
    with trace_span(
        "workflow.tool",
        attributes={
            "axon.workflow_id": str(workflow_id),
            "axon.tool": tool,
            "axon.step.name": step_name,
            "axon.step.position": position,
        },
    ):
        started = perf_counter()
        outcome = "ok"
        error_type = None
        measurement = StageMeasurement()
        try:
            yield measurement
        except BaseException as error:
            outcome = "error"
            error_type = type(error).__name__
            raise
        finally:
            seconds = perf_counter() - started
            measurement.duration_ms = round(seconds * 1000, 2)
            tool_duration.labels(tool=tool, outcome=outcome).observe(seconds)
            log_event(
                "workflow.tool",
                workflow_id=str(workflow_id),
                tool=tool,
                step=step_name,
                position=position,
                outcome=outcome,
                duration_ms=measurement.duration_ms,
                error_type=error_type,
            )
