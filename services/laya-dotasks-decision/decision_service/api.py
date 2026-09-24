from __future__ import annotations

import asyncio
import hmac
import json
import logging
import threading
from .schemas import AssignmentRequest, AssignmentResponse
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .engine import DecisionEngine, InputTooLong
from .schemas import FailureRequest, FailureResponse, HistoryRequest, HistoryResponse
from .settings import Settings

LOG = logging.getLogger("uvicorn.error")
MAX_BODY_BYTES = 96 * 1024


class RequestBoundary:
    """Authenticate before reading bodies and bound streamed/chunked requests too."""

    def __init__(self, app, settings: Settings):
        self.app, self.settings = app, settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] == "/healthz":
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        expected = ("Bearer " + self.settings.token).encode()
        if not hmac.compare_digest(headers.get(b"authorization", b""), expected):
            return await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
        if b"origin" in headers:
            return await JSONResponse({"error": "browser_requests_not_allowed"}, status_code=403)(scope, receive, send)
        body = bytearray()
        try:
            async with asyncio.timeout(10):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    body.extend(message.get("body", b""))
                    if len(body) > MAX_BODY_BYTES:
                        return await JSONResponse({"error": "body_too_large"}, status_code=413)(scope, receive, send)
                    if not message.get("more_body"):
                        break
        except TimeoutError:
            return await JSONResponse({"error": "request_timeout"}, status_code=408)(scope, receive, send)

        async def buffered_receive():
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, buffered_receive, send)


def create_app(settings: Settings | None = None, engine=None) -> FastAPI:
    settings = settings or Settings.from_environment()

    @asynccontextmanager
    async def lifespan(app):
        app.state.engine = engine if engine is not None else DecisionEngine(settings)
        yield
        app.state.engine = None

    app = FastAPI(title="laya-dotasks-decision", version="0.1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(RequestBoundary, settings=settings)
    app.state.engine = None
    lock = threading.Lock()

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # Do not echo private task text or logs in validation responses.
        return JSONResponse({"error": "invalid_request"}, status_code=422)

    @app.get("/healthz")
    def health():
        ready = app.state.engine is not None
        return JSONResponse({"service": "laya-dotasks-decision", "ready": ready},
                            status_code=200 if ready else 503)

    def decide(kind, payload, response_type):
        if not lock.acquire(blocking=False):
            raise HTTPException(503, "inference_busy", headers={"Retry-After": "1"})
        started, request_id = time.monotonic(), str(uuid.uuid4())
        status = "ok"
        try:
            current = app.state.engine
            if current is None:
                raise HTTPException(503, "model_not_ready")
            result = getattr(current, kind)(payload)
            return response_type(**result, **current.metadata, request_id=request_id,
                                 latency_ms=int((time.monotonic() - started) * 1000))
        except InputTooLong:
            status = "input_too_long"
            raise HTTPException(422, status) from None
        except HTTPException:
            status = "unavailable"
            raise
        except Exception:
            status = "inference_failed"
            raise HTTPException(503, status) from None
        finally:
            lock.release()
            LOG.info(json.dumps({"request_id": request_id, "decision": kind, "status": status,
                                 "latency_ms": int((time.monotonic() - started) * 1000)}))

    @app.post("/v1/history/rank", response_model=HistoryResponse)
    def history(request: HistoryRequest):
        return decide("history", request, HistoryResponse)

    @app.post("/v1/failure/classify", response_model=FailureResponse)
    def failure(request: FailureRequest):
        return decide("failure", request, FailureResponse)

    @app.post('/v1/assignment/recommend', response_model=AssignmentResponse)
    def assignment(request: AssignmentRequest):
        return decide('assignment', request, AssignmentResponse)

    return app
