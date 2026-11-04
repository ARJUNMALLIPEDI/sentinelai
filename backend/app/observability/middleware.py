"""Per-request context: request ID, access log, Prometheus metrics, last-resort 500s.

Written as pure ASGI middleware rather than ``@app.middleware("http")``
(BaseHTTPMiddleware) because that wrapper runs the app in a separate task,
which breaks ContextVar propagation and buffers streaming responses.

Unhandled exceptions are converted to a JSON 500 *here*, inside the request
context, so the error body and the ``X-Request-ID`` header both carry the ID
that also appears on the server's log line.
"""

import json
import logging
import re
import time
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import error_body
from app.core.logging import request_id_ctx
from app.observability.metrics import HTTP_LATENCY, HTTP_REQUESTS

logger = logging.getLogger("sentinel.request")

# Accept a caller-supplied ID (e.g. from a gateway) only if it's short and safe
# to echo into headers/logs; otherwise generate our own.
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_UNTRACKED_ROUTES = {"/metrics"}


def route_template(scope: Scope) -> str:
    """Full route template for the matched request, e.g. ``/api/v1/health/live``.

    Recent FastAPI versions no longer flatten included routers, so
    ``scope["route"].path`` is relative to its router's prefix. The full
    template lives on FastAPI's (private) effective route context; we read it
    defensively and fall back to the relative path, then to ``unmatched``.
    ``tests/integration/test_health.py`` pins this behaviour across upgrades.
    """
    fastapi_scope = scope.get("fastapi")
    if isinstance(fastapi_scope, dict):
        ctx = fastapi_scope.get("effective_route_context")
        template = getattr(ctx, "path_format", None)
        if isinstance(template, str) and template:
            return template
    route = scope.get("route")
    template = getattr(route, "path_format", None) or getattr(route, "path", None)
    return template if isinstance(template, str) and template else "unmatched"


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope["headers"]).get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        token = request_id_ctx.set(request_id)

        status_code = 500
        response_started = False
        start = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                message.setdefault("headers", [])
                message["headers"].append((b"x-request-id", request_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            logger.exception("unhandled_exception", extra={"ctx": {"path": scope["path"]}})
            if response_started:
                raise  # too late to send a clean error; let the server drop the connection
            await self._send_500(send_wrapper)
        finally:
            elapsed = time.perf_counter() - start
            route = route_template(scope)
            if route not in _UNTRACKED_ROUTES:
                HTTP_REQUESTS.labels(scope["method"], route, str(status_code)).inc()
                HTTP_LATENCY.labels(scope["method"], route).observe(elapsed)
                logger.info(
                    "request",
                    extra={"ctx": {
                        "method": scope["method"],
                        "path": scope["path"],
                        "route": route,
                        "status": status_code,
                        "duration_ms": round(elapsed * 1000, 2),
                    }},
                )
            request_id_ctx.reset(token)

    @staticmethod
    async def _send_500(send: Send) -> None:
        body = json.dumps(error_body("internal_error", "An unexpected error occurred.")).encode()
        await send({
            "type": "http.response.start",
            "status": 500,
            "headers": [(b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})
