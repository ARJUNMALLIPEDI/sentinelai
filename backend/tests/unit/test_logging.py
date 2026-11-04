import json
import logging

from app.core.logging import JsonFormatter, request_id_ctx


def _record(msg: str, **extra: object) -> logging.LogRecord:
    rec = logging.LogRecord("sentinel.test", logging.INFO, __file__, 1, msg, None, None)
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def test_json_formatter_includes_request_id_and_context_fields() -> None:
    token = request_id_ctx.set("req-123")
    try:
        line = JsonFormatter().format(_record("hello", ctx={"service": "orders", "latency_ms": 12}))
    finally:
        request_id_ctx.reset(token)
    payload = json.loads(line)
    assert payload["msg"] == "hello"
    assert payload["level"] == "INFO"
    assert payload["request_id"] == "req-123"
    assert payload["service"] == "orders"
    assert payload["latency_ms"] == 12


def test_json_formatter_omits_request_id_outside_a_request() -> None:
    payload = json.loads(JsonFormatter().format(_record("bg job")))
    assert "request_id" not in payload
