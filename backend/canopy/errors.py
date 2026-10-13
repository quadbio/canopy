"""One error envelope for every API reply:

    {"detail": {"code": "<short code>", "message": "<sentence for people>"}, "request_id": "…"}

The code is also put on the request, so the request log line carries it (status alone does not
say why a request failed). Validation errors name the offending parameters, never their values.
"""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

STATUS_CODES = {
    400: "bad_request",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    410: "gone",
    413: "too_large",
    416: "range_not_satisfiable",
    422: "invalid_request",
    429: "too_many_requests",
}


class ApiError(Exception):
    """An expected failure with a stable code (e.g. `needs_conversion:legacy_layout`)."""

    expected = True  # logs.timed logs it as a refusal, not a fault

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message


def error_response(
    request: Request,
    status: int,
    code: str,
    message: str,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    request.state.error_code = code
    rid = getattr(request.state, "request_id", None)
    return JSONResponse(
        {"detail": {"code": code, "message": message}, "request_id": rid},
        status_code=status,
        headers=headers,
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return error_response(request, exc.status, exc.code, exc.message)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = STATUS_CODES.get(exc.status_code, f"http_{exc.status_code}")
        message = exc.detail if isinstance(exc.detail, str) else code
        return error_response(request, exc.status_code, code, message, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = sorted(
            {".".join(str(p) for p in e.get("loc", ())[1:]) or "body" for e in exc.errors()}
        )
        return error_response(
            request, 422, "invalid_request", f"Invalid parameters: {', '.join(fields)}."
        )
