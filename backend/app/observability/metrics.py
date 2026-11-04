"""Prometheus metrics describing SentinelAI itself (not the simulated fleet).

Labels use the *route template* (``/api/v1/incidents/{incident_id}``), never the
raw path. Raw paths put every ID into a label value, and unbounded label
cardinality is the classic way to take down a Prometheus server.
"""

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

HTTP_REQUESTS = Counter(
    "sentinel_http_requests_total",
    "HTTP requests handled by the API",
    ["method", "route", "status"],
)
HTTP_LATENCY = Histogram(
    "sentinel_http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)

metrics_router = APIRouter()


@metrics_router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
