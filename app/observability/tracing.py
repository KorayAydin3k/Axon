from collections.abc import Iterator
from contextlib import contextmanager
from json import dumps
from os import getenv

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
from opentelemetry.trace import Span, SpanKind, StatusCode


def _span_json(span: ReadableSpan) -> str:
    context = span.get_span_context()
    return dumps({
        "event": "otel.span",
        "service": "axon",
        "name": span.name,
        "kind": span.kind.name,
        "trace_id": f"{context.trace_id:032x}" if context else None,
        "span_id": f"{context.span_id:016x}" if context else None,
        "parent_span_id": f"{span.parent.span_id:016x}" if span.parent else None,
        "start_time_ns": span.start_time,
        "end_time_ns": span.end_time,
        "status": span.status.status_code.name,
        "attributes": dict(span.attributes or {}),
    }) + "\n"


if getenv("AXON_TRACE_EXPORT") == "console":
    provider = TracerProvider(resource=Resource.create({"service.name": "axon"}))
    provider.add_span_processor(
        SimpleSpanProcessor(ConsoleSpanExporter(formatter=_span_json))
    )
    tracer = provider.get_tracer("axon")
else:
    tracer = trace.get_tracer("axon")


def active_trace_id() -> str | None:
    context = trace.get_current_span().get_span_context()
    return f"{context.trace_id:032x}" if context.is_valid else None


@contextmanager
def trace_span(
    name: str,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    attributes: dict[str, str | int] | None = None,
) -> Iterator[Span]:
    with tracer.start_as_current_span(
        name,
        kind=kind,
        attributes=attributes,
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            yield span
        except Exception:
            span.set_status(StatusCode.ERROR)
            raise
