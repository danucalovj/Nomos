"""Consistent API envelope and error shapes.

Success:  {"ok": true, "data": <payload>}
Error:    {"ok": false, "error": {"code": "<slug>", "message": "<human text>", ...extra}}

A success envelope for an AGENT with unseen admin @mentions additionally
carries a top-level "attention" object (issue #32): the signal piggybacks on
every outbound call the agent makes, so an agent that never polls its inbox
still cannot miss the admin. Additive; admin and keyless responses never
carry it.
"""
from __future__ import annotations

import contextvars
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

# Set per-request by the auth dependencies (server/auth.py) in the request's
# own task context; read once by ok(). Default None means "no signal".
attention_var: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "nomos_attention", default=None
)


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra or {}


def ok(data: Any = None) -> dict[str, Any]:
    body: dict[str, Any] = {"ok": True, "data": data}
    attention = attention_var.get()
    if attention:
        body["attention"] = attention
    return body


def error_body(code: str, message: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message, **(extra or {})}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content=error_body(exc.code, exc.message, exc.extra)
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body("http_error", str(exc.detail)),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=error_body(
                "validation_error",
                "Request validation failed.",
                # errors() can carry non-serializable objects (exception ctx,
                # UploadFile inputs); encode or the error response itself 500s.
                {"details": jsonable_encoder(exc.errors())},
            ),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # The envelope is the documented contract for ALL errors — clients
        # parse body["ok"] unconditionally, so even a bug must not surface as
        # Starlette's plain-text 500 (issue #28). Full traceback to the log.
        logging.getLogger("nomos").exception(
            "unhandled error on %s %s", request.method, request.url.path
        )
        return JSONResponse(
            status_code=500,
            content=error_body("internal_error", "Internal server error."),
        )
