from collections.abc import Awaitable, Callable
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from opentelemetry.trace import SpanKind, StatusCode
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.workflows import router as workflows_router
from app.observability.telemetry import current_request_id, log_event, metrics_registry
from app.observability.tracing import trace_span

app = FastAPI(title="Axon")
app.include_router(workflows_router)

_SAFE_LOCATIONS = {
    "body", "header", "path", "query",
    "request", "workflow_id", "plan", "plan_hash",
    "idempotency-key", "x-axon-approval-token",
}
_SERVER_ERROR = "Could not complete the request"


@app.exception_handler(RequestValidationError)
async def mask_validation_input(
    _request: Request, error: RequestValidationError
) -> JSONResponse:
    details = []
    for item in error.errors():
        location = [
            part if isinstance(part, int)
            or (isinstance(part, str) and part.lower() in _SAFE_LOCATIONS)
            else "<field>"
            for part in item["loc"]
        ]
        details.append({"loc": location, "type": item["type"], "msg": "Invalid value"})
    return JSONResponse(status_code=422, content={"detail": details})


@app.exception_handler(StarletteHTTPException)
async def mask_server_error(request: Request, error: StarletteHTTPException) -> Response:
    if error.status_code < 500:
        return await http_exception_handler(request, error)
    return JSONResponse(status_code=error.status_code, content={"detail": _SERVER_ERROR})


_HTTP_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}


@app.middleware("http")
async def observe_request(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    request_id = uuid4().hex
    context_token = current_request_id.set(request_id)
    started = perf_counter()
    method = request.method if request.method in _HTTP_METHODS else "OTHER"
    try:
        with trace_span(
            "http.request",
            kind=SpanKind.SERVER,
            attributes={"http.request.method": method, "axon.request_id": request_id},
        ) as span:
            try:
                response = await call_next(request)
            except Exception as error:
                log_event("http.error", error_type=type(error).__name__)
                response = JSONResponse(status_code=500, content={"detail": _SERVER_ERROR})

            response.headers["X-Request-ID"] = request_id
            route = getattr(request.scope.get("route"), "path", "<unmatched>")
            if route != "<unmatched>":
                span.set_attribute("http.route", route)
            span.set_attribute("http.response.status_code", response.status_code)
            if response.status_code >= 500:
                span.set_status(StatusCode.ERROR)

            log_event(
                "http.request",
                method=method,
                route=route,
                status_code=response.status_code,
                duration_ms=round((perf_counter() - started) * 1000, 2),
            )
            return response
    finally:
        current_request_id.reset(context_token)


@app.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    return Response(
        content=generate_latest(metrics_registry),
        headers={"Content-Type": CONTENT_TYPE_LATEST},
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

dashboard_dist = Path(__file__).resolve().parents[1] / "dashboard" / "dist"
if dashboard_dist.is_dir():
    app.mount("/dashboard", StaticFiles(directory=dashboard_dist, html=True), name="dashboard")
