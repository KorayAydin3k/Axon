from collections.abc import Awaitable, Callable
from logging import getLogger

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.workflows import router as workflows_router

app = FastAPI(title="Axon")
app.include_router(workflows_router)
logger = getLogger(__name__)

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


@app.middleware("http")
async def mask_unhandled_error(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    try:
        return await call_next(request)
    except Exception as error:
        logger.error("Unhandled API error (%s)", type(error).__name__)
        return JSONResponse(status_code=500, content={"detail": _SERVER_ERROR})


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
