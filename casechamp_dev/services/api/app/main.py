import logging
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.common import Problem, iso
from app.service import Service
from app.settings import Settings
from app.wire import Contract

LOGGER = logging.getLogger("casechamp")


def create_app(settings=None, *, engine=None, clock=None, plan=None):
    settings = settings if settings is not None else Settings()
    contract = Contract(settings.contract_root)
    service = Service(settings, contract, engine=engine, clock=clock, plan=plan)
    app = FastAPI(
        title="Собрать как в посте",
        version=__version__,
        openapi_url="/api/v1/openapi.json",
        docs_url="/api/v1/docs",
        redoc_url=None,
        redirect_slashes=False,
    )
    app.openapi = lambda: contract.openapi
    app.state.service = service

    def render(result, request_id, now, epoch):
        key = "data" if result.status < 400 else "error"
        return JSONResponse(
            {
                key: result.body,
                "meta": {
                    "request_id": request_id,
                    "server_now": iso(now),
                    "sandbox_epoch": epoch,
                    "idempotency_replayed": result.replay,
                },
            },
            status_code=result.status,
            headers={"Cache-Control": "no-store", "X-Request-ID": request_id} | result.headers,
        )

    @app.exception_handler(StarletteHTTPException)
    async def exception_envelope(request, exc):
        code = "RESOURCE_NOT_FOUND" if exc.status_code in (404, 405) else "VALIDATION_ERROR"
        return render(service.error(Problem(code)), str(uuid4()), datetime.now(timezone.utc), None)

    def endpoint(operation):
        async def serve(request: Request):
            request_id = str(uuid4())
            try:
                command = await contract.parse(
                    request, operation, request_id, settings, cookie_name=service.cookie_name
                )
                result, now, epoch, token = await run_in_threadpool(service.dispatch, command)
            except Problem as problem:
                return render(service.error(problem), request_id, datetime.now(timezone.utc), None)
            if result.status < 400 and operation == "downloadAsset":
                target = settings.media_root / result.body["storage_key"]
                if not target.is_file():
                    return render(service.error(Problem("RESOURCE_NOT_FOUND")), request_id, now, epoch)
                return FileResponse(
                    target,
                    media_type=result.body["content_type"],
                    headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"},
                )
            response = render(result, request_id, now, epoch)
            if token:
                response.set_cookie(
                    service.cookie_name,
                    token,
                    max_age=86400,
                    httponly=True,
                    samesite="lax",
                    secure=settings.web_origin.startswith("https://"),
                    path="/",
                )
            return response

        return serve

    for name, (route, method, _) in contract.operations.items():
        app.add_api_route("/api/v1" + route, endpoint(name), methods=[method], name=name, operation_id=name)
    return app


app = create_app()
