"""FastAPI factory for the v2 contract."""

import logging
from datetime import UTC, datetime
from time import monotonic
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from psycopg import OperationalError
from psycopg.errors import LockNotAvailable, QueryCanceled
from sqlalchemy.exc import OperationalError as EngineUnavailable
from sqlalchemy.exc import TimeoutError as PoolTimeout
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from app.common import canonical, iso
from app.settings import Settings
from app.v2.core import COOKIE_BOOTSTRAP, COOKIE_SESSION, PREFIX, Failure, Wire
from app.v2.service import Service

LOGGER = logging.getLogger("casechamp.v2")


def create_app(settings=None, *, engine=None, clock=None):
    settings = settings if settings is not None else Settings()
    origin = urlsplit(settings.web_origin)
    if origin.scheme != "https" and (
        origin.scheme != "http" or origin.hostname not in ("localhost", "127.0.0.1", "::1")
    ):
        raise ValueError("HTTP for the v2 demo requires a loopback origin")
    wire = Wire()
    service = Service(settings, wire, engine=engine, clock=clock)
    app = FastAPI(
        title="Первый заказ из сообщества",
        version="2.0.0",
        docs_url=PREFIX + "/docs",
        openapi_url=PREFIX + "/openapi.json",
        redoc_url=None,
        redirect_slashes=False,
    )
    app.openapi = lambda: wire.openapi
    app.state.service = service

    def render(value, request_id, now):
        replayed = False if isinstance(value, Failure) else value.replayed
        meta = {"request_id": request_id, "server_now": iso(now), "replayed": replayed}
        headers = {
            "Cache-Control": "no-store",
            "X-Request-ID": request_id,
            "X-Content-Type-Options": "nosniff",
        }
        if isinstance(value, Failure):
            wait = value.error["details"]["retry_after_seconds"]
            if wait:
                headers["Retry-After"] = str(wait)
            return JSONResponse(
                {"error": value.error, "meta": meta}, status_code=value.status, headers=headers
            )
        response = JSONResponse({"data": value.data, "meta": meta}, status_code=value.status, headers=headers)
        for identity, expiry, name, domain in (
            (value.session_id, value.session_expiry, COOKIE_SESSION, "session"),
            (value.bootstrap_id, value.bootstrap_expiry, COOKIE_BOOTSTRAP, "bootstrap"),
        ):
            if identity:
                remaining = max(0, int((expiry - now).total_seconds()))
                response.set_cookie(
                    name,
                    service.security.token(domain, identity),
                    max_age=remaining,
                    expires=expiry.astimezone(UTC),
                    path=PREFIX,
                    secure=origin.scheme == "https",
                    httponly=True,
                    samesite="strict",
                )
        return response

    @app.exception_handler(HTTPException)
    async def route_error(_request, exc):
        code = "NOT_FOUND" if exc.status_code in (404, 405) else "VALIDATION_ERROR"
        return render(Failure(code), str(uuid4()), datetime.now(UTC))

    def handler(name):
        async def endpoint(request: Request):
            request_id = str(uuid4())
            started = monotonic()
            now = datetime.now(UTC)
            command = None
            try:
                command = await wire.read(request, name, request_id, settings.web_origin)
                result, now = await run_in_threadpool(service.run, command)
            except Failure as exc:
                result = exc
            except (LockNotAvailable, QueryCanceled, OperationalError, EngineUnavailable, PoolTimeout):
                result = Failure("COMPUTATION_UNAVAILABLE")
            except Exception:
                if settings.app_mode == "test":
                    raise
                LOGGER.error("internal failure in %s request_id=%s", name, request_id)
                result = Failure("INTERNAL_ERROR")
            LOGGER.info(
                canonical(
                    {
                        "request_id": request_id,
                        "operation": name,
                        "duration_ms": round((monotonic() - started) * 1000, 3),
                        "result": result.code if isinstance(result, Failure) else result.status,
                    }
                ).decode()
            )
            return render(result, request_id, now)

        return endpoint

    for name, (path, method, _) in wire.operations.items():
        app.add_api_route(PREFIX + path, handler(name), methods=[method], name=name, operation_id=name)
    return app
