"""Contracts the agent relies on: strict-mode-compatible schemas, grounding
rules, the wire format sent to the API, and the read-only safety boundary."""

import ast
import json
from pathlib import Path
from typing import Any

import httpx2 as httpx  # the Anthropic SDK 2.x transport package
import pytest

from app.agent.llm import AnthropicLLM, LLMError, Usage
from app.agent.report import IncidentReport, grounding_errors
from app.agent.tools import REPORT_TOOL, TOOLS, tool_definitions

SERVICES = ["orders-service", "postgres-primary"]
UNSUPPORTED = {
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "maxItems",
    "pattern",
}


def walk(node: Any, path: str = "$"):  # type: ignore[no-untyped-def]
    if isinstance(node, dict):
        yield path, node
        for k, v in node.items():
            yield from walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk(v, f"{path}[{i}]")


# --------------------------------------------------------------------------- schemas


@pytest.mark.parametrize("tool", tool_definitions(SERVICES), ids=lambda t: t["name"])
def test_every_tool_schema_is_strict_mode_compatible(tool: dict[str, Any]) -> None:
    assert tool["strict"] is True
    for path, node in walk(tool["input_schema"]):
        if node.get("type") == "object" or "properties" in node:
            assert node.get("additionalProperties") is False, f"open object at {path}"
        assert not (UNSUPPORTED & node.keys()), f"unsupported keyword at {path}"
        assert node.get("minItems", 0) <= 1, f"minItems > 1 at {path}"


def test_service_fields_are_pinned_to_real_services() -> None:
    metrics = next(t for t in tool_definitions(SERVICES) if t["name"] == "get_service_metrics")
    assert metrics["input_schema"]["properties"]["service"]["enum"] == SERVICES
    report = next(t for t in tool_definitions(SERVICES) if t["name"] == REPORT_TOOL)
    enums = [n["enum"] for _, n in walk(report["input_schema"]) if "enum" in n]
    assert SERVICES in enums  # target_service / affected_services items


# --------------------------------------------------------------------------- grounding


def report(**overrides: Any) -> IncidentReport:
    base: dict[str, Any] = {
        "summary": "Orders pool exhausted.",
        "severity": "sev2",
        "affected_services": ["orders-service"],
        "timeline": [{"at": "2026-11-12T18:00:00Z", "event": "pool saturated", "evidence": ["E1"]}],
        "key_metrics": [],
        "log_evidence": [],
        "hypotheses": [],
        "separate_issues": [],
        "confirmed_findings": [{"statement": "pool at 60/60", "evidence": ["E1"]}],
        "root_cause": {
            "statement": "connection leak",
            "status": "probable",
            "confidence": "medium",
            "evidence": ["E1", "E2"],
        },
        "recommended_remediations": [
            {
                "action": "restart_service",
                "target_service": "orders-service",
                "rationale": "releases leaked connections",
                "risk": "low",
                "evidence": ["E2"],
            }
        ],
        "overall_confidence": "medium",
    }
    base.update(overrides)
    return IncidentReport.model_validate(base)


def test_grounded_report_is_accepted() -> None:
    assert grounding_errors(report(), {"E1", "E2"}, SERVICES) == []


def test_invented_evidence_is_rejected_and_valid_ids_are_listed() -> None:
    [err] = grounding_errors(report(), {"E1"}, SERVICES)
    assert "E2" in err and "Valid IDs: E1" in err


def test_unknown_services_are_rejected() -> None:
    errs = grounding_errors(report(affected_services=["checkout-service"]), {"E1", "E2"}, SERVICES)
    assert errs == ["names services that do not exist: checkout-service"]


@pytest.mark.parametrize(
    ("root_cause", "fragment"),
    [
        (
            {"statement": "x", "status": "confirmed", "confidence": "high", "evidence": []},
            "cites no evidence",
        ),
        (
            {"statement": "x", "status": "confirmed", "confidence": "low", "evidence": ["E1"]},
            "low confidence",
        ),
    ],
)
def test_confirmed_root_cause_needs_strong_evidence(
    root_cause: dict[str, Any], fragment: str
) -> None:
    errs = grounding_errors(report(root_cause=root_cause), {"E1", "E2"}, SERVICES)
    assert any(fragment in e for e in errs)


def test_scale_needs_replicas_and_others_must_not_have_them() -> None:
    scale = {
        "action": "scale_service",
        "target_service": "orders-service",
        "rationale": "r",
        "risk": "low",
        "evidence": ["E1"],
    }
    errs = grounding_errors(report(recommended_remediations=[scale]), {"E1", "E2"}, SERVICES)
    assert errs == ["recommended_remediations[0]: scale_service requires replicas"]


def test_findings_without_evidence_fail_schema_validation() -> None:
    with pytest.raises(ValueError, match="at least 1 item"):
        report(confirmed_findings=[{"statement": "trust me", "evidence": []}])


# --------------------------------------------------------------------------- wire format


def _message(content: list[dict[str, Any]], stop: str = "tool_use") -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 120,
            "output_tokens": 40,
            "cache_read_input_tokens": 900,
            "cache_creation_input_tokens": 0,
        },
    }


async def test_request_wire_format_and_response_normalisation() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json=_message(
                [
                    {"type": "thinking", "thinking": "", "signature": "sig-abc"},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "get_service_health",
                        "input": {"service": "orders-service"},
                    },
                ]
            ),
        )

    llm = AnthropicLLM(
        "sk-test",
        timeout_s=5,
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    resp = await llm.create(
        model="claude-sonnet-5-5",
        max_tokens=1000,
        system="s",
        messages=[{"role": "user", "content": "hi"}],
        tools=tool_definitions(SERVICES),
        thinking={"type": "adaptive"},
        output_config={"effort": "medium"},
        cache_control={"type": "ephemeral"},
    )
    await llm.close()

    assert seen["output_config"] == {"effort": "medium"}
    assert seen["thinking"] == {"type": "adaptive"}
    assert seen["cache_control"] == {"type": "ephemeral"}
    assert all(t["strict"] is True for t in seen["tools"])
    assert "tool_choice" not in seen  # forced tool choice is rejected with adaptive thinking
    # The thinking block survives normalisation intact, so it can be echoed back verbatim.
    assert resp.content[0] == {"type": "thinking", "thinking": "", "signature": "sig-abc"}
    assert resp.stop_reason == "tool_use"
    assert resp.usage == Usage(120, 40, 900, 0)


async def test_api_errors_become_llm_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            529,
            json={"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
        )

    llm = AnthropicLLM(
        "sk-test",
        timeout_s=5,
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(LLMError, match="529"):
        await llm.create(model="m", max_tokens=10, messages=[{"role": "user", "content": "x"}])
    await llm.close()


def test_cost_estimate_uses_cache_read_discount() -> None:
    u = Usage(input_tokens=1_000_000, output_tokens=100_000, cache_read_tokens=1_000_000)
    assert u.cost_usd("claude-sonnet-5-5") == pytest.approx(2.0 + 1.0 + 0.1)
    assert u.cost_usd("some-unknown-model") is None


# --------------------------------------------------------------------------- safety boundary

AGENT_DIR = Path(__file__).resolve().parents[2] / "app" / "agent"
FORBIDDEN = ("app.simulation", "app.worker", "app.remediation", "app.api.routes.simulation")


@pytest.mark.parametrize("module", sorted(p.name for p in AGENT_DIR.glob("*.py")))
def test_agent_code_cannot_reach_anything_that_changes_the_fleet(module: str) -> None:
    tree = ast.parse((AGENT_DIR / module).read_text())
    imported = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    imported += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    bad = [m for m in imported if m.startswith(FORBIDDEN)]
    assert bad == [], f"{module} imports {bad}"


def test_the_agent_only_has_read_tools_plus_the_report() -> None:
    assert set(TOOLS) == {
        "get_service_metrics",
        "query_application_logs",
        "get_service_health",
        "inspect_database_connections",
        "get_recent_deployments",
        "identify_related_incidents",
        REPORT_TOOL,
    }
