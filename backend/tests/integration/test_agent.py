"""The investigation loop end to end against real Postgres, with a scripted model.

The scripted model plays Claude's side of the protocol (thinking blocks, text,
parallel tool calls, a report), so every rule the orchestrator enforces can be
exercised deterministically and without an API key.
"""

import asyncio
import copy
import itertools
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.agent import tools as agent_tools
from app.agent.llm import LLMError, LLMResponse, Usage
from app.agent.orchestrator import AgentLimits
from app.agent.service import execute_investigation, fail_stale, request_investigation
from app.agent.tools import REPORT_TOOL, build_context
from app.domain.enums import IncidentEventKind, IncidentStatus, InvestigationStatus
from app.models import AgentStep, Incident, IncidentEvent, IncidentReportRecord, Investigation
from app.worker import jobs
from app.worker.detection import run_detection
from app.worker.jobs import WorkerState, run_tick, utcnow
from tests.conftest import make_settings

pytestmark = pytest.mark.integration

Script = list[LLMResponse | Exception | Callable[[dict[str, Any]], LLMResponse]]


class ScriptedLLM:
    """Returns pre-written turns in order and records every request it receives."""

    def __init__(self, script: Script) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    async def create(self, **request: Any) -> LLMResponse:
        self.requests.append(copy.deepcopy(request))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step(request) if callable(step) else step


_ids = itertools.count(1)


def use(name: str, **inp: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": f"toolu_{next(_ids)}", "name": name, "input": inp}


def turn(*blocks: dict[str, Any], stop: str = "tool_use") -> LLMResponse:
    return LLMResponse(
        content=list(blocks),
        stop_reason=stop,
        usage=Usage(1000, 200, 3000, 0),
        model="claude-sonnet-5-5",
    )


THINK = {"type": "thinking", "thinking": "", "signature": "sig-opaque-123"}


def report_input(evidence: list[str], **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "summary": "inventory-service lost all instances; callers failed.",
        "severity": "sev1",
        "affected_services": ["inventory-service", "orders-service", "api-gateway"],
        "timeline": [
            {"at": "2026-11-12T18:00:00Z", "event": "inventory down", "evidence": evidence[:1]}
        ],
        "key_metrics": [
            {
                "service": "inventory-service",
                "signal": "availability",
                "observation": "0/2 instances healthy",
                "evidence": evidence[:1],
            }
        ],
        "log_evidence": [
            {
                "service": "inventory-service",
                "excerpt": "Health check failed: 0/2 instances healthy",
                "evidence": evidence[-1:],
            }
        ],
        "confirmed_findings": [
            {"statement": "inventory-service has no healthy instances", "evidence": evidence[:1]}
        ],
        "hypotheses": [
            {
                "statement": "a crash loop after a bad config",
                "confidence": "low",
                "evidence": [],
                "how_to_verify": "check container exit codes",
            }
        ],
        "root_cause": {
            "statement": "inventory-service outage",
            "status": "confirmed",
            "confidence": "high",
            "evidence": evidence,
        },
        "recommended_remediations": [
            {
                "action": "restart_service",
                "target_service": "inventory-service",
                "rationale": "restore instances",
                "risk": "low",
                "evidence": evidence[:1],
            }
        ],
        "separate_issues": [],
        "overall_confidence": "high",
    }
    body.update(overrides)
    return body


@pytest.fixture
async def incident(worker: WorkerState) -> Incident:
    """An outage incident opened by real detection."""
    await jobs.inject_fault(
        {"state": worker}, "flt_o", "service_outage", "inventory-service", 1.0, 3600
    )
    start = worker.last_tick or utcnow()
    for i in range(1, 4):
        await run_tick(worker, start + timedelta(seconds=5 * i))
        async with worker.sessionmaker() as s, s.begin():
            await run_detection(s, worker.settings.sim_tick_seconds)
    async with worker.sessionmaker() as s:
        return (await s.execute(select(Incident))).scalar_one()


async def investigate(
    worker: WorkerState, inc: Incident, llm: ScriptedLLM, limits: AgentLimits | None = None
) -> Any:
    async with worker.sessionmaker() as s:
        inv = await request_investigation(
            s, inc, trigger="manual", requested_by="test", settings=worker.settings
        )
        await s.commit()
    outcome = await execute_investigation(
        worker.sessionmaker, inv.id, llm, worker.settings, limits=limits or AgentLimits()
    )
    return inv.id, outcome


async def rows(worker: WorkerState, model: Any, *where: Any) -> list[Any]:
    async with worker.sessionmaker() as s:
        return list((await s.execute(select(model).where(*where))).scalars())


# --------------------------------------------------------------------------- happy path


async def test_full_investigation(worker: WorkerState, incident: Incident) -> None:
    llm = ScriptedLLM(
        [
            turn(
                THINK,
                {"type": "text", "text": "Checking the symptom map first."},
                use("identify_related_incidents"),
                use("get_service_health", service="inventory-service"),
            ),
            turn(use("query_application_logs", service="inventory-service", min_level="CRITICAL")),
            turn(use(REPORT_TOOL, **report_input(["E1", "E2", "E3"]))),
        ]
    )
    inv_id, outcome = await investigate(worker, incident, llm)

    assert outcome.status == "completed"
    assert outcome.tool_calls == 3 and outcome.llm_calls == 3
    assert outcome.report.root_cause.status == "confirmed"

    # Protocol: the assistant turn (thinking signature included) is echoed verbatim,
    # and every tool_use gets a tool_result, first in the next user message.
    second = llm.requests[1]["messages"]
    assert second[1] == {"role": "assistant", "content": llm.requests[1]["messages"][1]["content"]}
    assert second[1]["content"][0] == THINK
    results = second[2]["content"]
    assert [r["type"] for r in results] == ["tool_result", "tool_result"]
    assert [r["tool_use_id"] for r in results] == [b["id"] for b in second[1]["content"][2:]]
    assert [json.loads(r["content"])["evidence_id"] for r in results] == ["E1", "E2"]
    health = json.loads(results[1]["content"])
    assert health["status"] == "down" and health["called_by"] == ["api-gateway", "orders-service"]

    # The request carries the model settings we expect.
    req = llm.requests[0]
    assert req["thinking"] == {"type": "adaptive"} and req["output_config"] == {"effort": "medium"}
    assert "tool_choice" not in req and req["cache_control"] == {"type": "ephemeral"}
    assert "INC-1" in req["messages"][0]["content"]

    [inv] = await rows(worker, Investigation, Investigation.id == inv_id)
    assert inv.status is InvestigationStatus.COMPLETED and inv.cost_usd and inv.cost_usd > 0
    steps = await rows(worker, AgentStep, AgentStep.investigation_id == inv_id)
    assert [s.kind for s in sorted(steps, key=lambda s: s.seq)] == [
        "llm_call",
        "tool_call",
        "tool_call",
        "llm_call",
        "tool_call",
        "llm_call",
        "tool_call",
    ]
    assert {s.evidence_id for s in steps if s.evidence_id} == {"E1", "E2", "E3"}

    [rec] = await rows(worker, IncidentReportRecord)
    assert rec.partial is False and rec.root_cause_status == "confirmed"
    [inc] = await rows(worker, Incident)
    assert inc.status is IncidentStatus.INVESTIGATING  # engaged: no auto-resolve now
    kinds = [e.kind for e in await rows(worker, IncidentEvent)]
    assert IncidentEventKind.INVESTIGATION_STARTED in kinds
    assert IncidentEventKind.INVESTIGATION_COMPLETED in kinds


# --------------------------------------------------------------------------- self-correction


async def test_report_with_invented_evidence_is_rejected_then_corrected(
    worker: WorkerState, incident: Incident
) -> None:
    llm = ScriptedLLM(
        [
            turn(use("get_service_health", service="inventory-service")),
            turn(use(REPORT_TOOL, **report_input(["E1", "E7"]))),  # E7 was never issued
            turn(use(REPORT_TOOL, **report_input(["E1"]))),
        ]
    )
    _, outcome = await investigate(worker, incident, llm)
    assert outcome.status == "completed"
    rejection = llm.requests[2]["messages"][-1]["content"][0]
    assert rejection["is_error"] is True
    assert "E7" in rejection["content"] and "Valid IDs: E1" in rejection["content"]


@pytest.mark.parametrize(
    ("block", "fragment"),
    [
        (lambda: use("get_service_health", service="ghost-service"), "unknown service"),
        (
            lambda: use("get_service_metrics", service="orders-service", minutes=5000),
            "Invalid input",
        ),
        (lambda: use("delete_database"), "Unknown tool"),
    ],
)
async def test_bad_tool_calls_become_errors_the_model_can_fix(
    worker: WorkerState, incident: Incident, block: Callable[[], dict[str, Any]], fragment: str
) -> None:
    llm = ScriptedLLM(
        [
            turn(block()),
            turn(use("get_service_health", service="inventory-service")),
            turn(use(REPORT_TOOL, **report_input(["E1"]))),
        ]
    )
    _, outcome = await investigate(worker, incident, llm)
    assert outcome.status == "completed"
    err = llm.requests[1]["messages"][-1]["content"][0]
    assert err["is_error"] is True and fragment in err["content"]


async def test_tool_timeouts_and_crashes_do_not_kill_the_investigation(
    worker: WorkerState, incident: Incident, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def slow(ctx: Any, inp: Any) -> dict[str, Any]:
        await asyncio.sleep(1)
        return {}

    async def broken(ctx: Any, inp: Any) -> dict[str, Any]:
        raise RuntimeError("bug")

    tools = dict(agent_tools.TOOLS)
    tools["get_recent_deployments"] = agent_tools.ToolSpec(
        "get_recent_deployments", "d", agent_tools.DeploymentsInput, slow
    )
    tools["get_service_metrics"] = agent_tools.ToolSpec(
        "get_service_metrics", "d", agent_tools.MetricsInput, broken
    )
    monkeypatch.setattr("app.agent.orchestrator.TOOLS", tools)

    llm = ScriptedLLM(
        [
            turn(
                use("get_recent_deployments"),
                use("get_service_metrics", service="orders-service"),
                use("get_service_health", service="inventory-service"),
            ),
            turn(use(REPORT_TOOL, **report_input(["E1"]))),
        ]
    )
    _, outcome = await investigate(worker, incident, llm, AgentLimits(tool_timeout_s=0.05))
    assert outcome.status == "completed"
    timeout, crash, ok = llm.requests[1]["messages"][-1]["content"]
    assert "timed out" in timeout["content"] and timeout["is_error"]
    assert "internal error (RuntimeError)" in crash["content"] and crash["is_error"]
    assert json.loads(ok["content"])["evidence_id"] == "E1"


# --------------------------------------------------------------------------- budgets


async def test_tool_budget_is_enforced_and_the_model_is_steered(
    worker: WorkerState, incident: Incident
) -> None:
    llm = ScriptedLLM(
        [
            turn(
                use("get_service_health", service="inventory-service"),
                use("get_service_health", service="orders-service"),
                use("get_service_health", service="api-gateway"),
            ),
            turn(use(REPORT_TOOL, **report_input(["E1", "E2"]))),
        ]
    )
    _, outcome = await investigate(worker, incident, llm, AgentLimits(max_tool_calls=2))
    assert outcome.status == "completed" and outcome.tool_calls == 2
    content = llm.requests[1]["messages"][-1]["content"]
    assert "budget" in content[2]["content"] and content[2]["is_error"]
    assert content[-1]["type"] == "text" and "generate_incident_report now" in content[-1]["text"]


async def test_stopping_without_a_report_gets_one_nudge_then_a_partial_report(
    worker: WorkerState, incident: Incident
) -> None:
    llm = ScriptedLLM(
        [
            turn(use("get_service_health", service="inventory-service")),
            turn({"type": "text", "text": "Inventory is down."}, stop="end_turn"),
            turn({"type": "text", "text": "Still just inventory."}, stop="end_turn"),
        ]
    )
    inv_id, outcome = await investigate(worker, incident, llm)
    assert outcome.status == "incomplete"
    assert "without submitting a report" in outcome.error
    [rec] = await rows(worker, IncidentReportRecord)
    assert rec.partial is True
    assert rec.report["evidence_collected"][0]["evidence_id"] == "E1"
    assert rec.report["last_model_text"] == "Still just inventory."


async def test_turn_and_time_budgets_end_in_incomplete(
    worker: WorkerState, incident: Incident
) -> None:
    forever = [turn(use("get_service_health", service="inventory-service")) for _ in range(5)]
    _, outcome = await investigate(worker, incident, ScriptedLLM(forever), AgentLimits(max_turns=2))
    assert outcome.status == "incomplete" and "turn budget" in outcome.error

    ticks = iter(range(0, 10_000, 200))
    async with worker.sessionmaker() as s:
        inc = (await s.execute(select(Incident))).scalar_one()
    from app.agent.orchestrator import run_investigation

    ctx = await build_context(worker.sessionmaker, inc.id, 5)
    out = await run_investigation(
        ctx,
        ScriptedLLM(forever),
        "brief",
        model="m",
        limits=AgentLimits(max_wall_s=300),
        clock=lambda: next(ticks),
    )
    assert out.status == "incomplete" and "time budget" in out.error


async def test_truncated_output_still_answers_every_tool_use(
    worker: WorkerState, incident: Incident
) -> None:
    cut = use("get_service_health", service="inventory-service")
    llm = ScriptedLLM(
        [
            turn(cut, stop="max_tokens"),
            turn(use("get_service_health", service="inventory-service")),
            turn(use(REPORT_TOOL, **report_input(["E1"]))),
        ]
    )
    _, outcome = await investigate(worker, incident, llm)
    assert outcome.status == "completed"
    reply = llm.requests[1]["messages"][-1]["content"]
    assert reply[0]["tool_use_id"] == cut["id"] and reply[0]["is_error"]
    assert reply[-1]["type"] == "text"


@pytest.mark.parametrize(
    ("step", "error"),
    [
        (LLMError("Claude API returned 529: Overloaded"), "529"),
        (turn({"type": "text", "text": "no"}, stop="refusal"), "declined"),
    ],
)
async def test_model_failures_end_in_failed(
    worker: WorkerState, incident: Incident, step: Any, error: str
) -> None:
    inv_id, outcome = await investigate(worker, incident, ScriptedLLM([step]))
    assert outcome.status == "failed" and error in outcome.error
    [inv] = await rows(worker, Investigation, Investigation.id == inv_id)
    assert inv.status is InvestigationStatus.FAILED and error in inv.error
    assert IncidentEventKind.INVESTIGATION_FAILED in [
        e.kind for e in await rows(worker, IncidentEvent)
    ]


# --------------------------------------------------------------------------- lifecycle & API


async def test_only_one_active_investigation_per_incident(
    worker: WorkerState, incident: Incident
) -> None:
    from app.core.errors import ConflictError

    async with worker.sessionmaker() as s:
        await request_investigation(
            s, incident, trigger="manual", requested_by="a", settings=worker.settings
        )
        await s.commit()
        with pytest.raises(ConflictError, match="already has an investigation"):
            await request_investigation(
                s, incident, trigger="manual", requested_by="b", settings=worker.settings
            )


async def test_stale_cleanup_spares_recent_investigations(
    worker: WorkerState, incident: Incident
) -> None:
    async with worker.sessionmaker() as s:
        inv = await request_investigation(
            s, incident, trigger="manual", requested_by="a", settings=worker.settings
        )
        await s.commit()
    assert await fail_stale(worker.sessionmaker, max_age_s=300) == 0
    async with worker.sessionmaker() as s:
        row = await s.get(Investigation, inv.id)
        assert row is not None
        row.created_at = datetime.now(UTC) - timedelta(hours=1)
        await s.commit()
    assert await fail_stale(worker.sessionmaker, max_age_s=300) == 1


@pytest.fixture
async def api(
    worker: WorkerState, client_factory: Callable[..., Any]
) -> AsyncIterator[AsyncClient]:
    async with client_factory(make_settings(anthropic_api_key="sk-ant-test", sim_seed=7)) as c:
        yield c


async def test_investigation_api(
    api: AsyncClient, worker: WorkerState, incident: Incident, client_factory: Callable[..., Any]
) -> None:
    async with client_factory(make_settings()) as no_key:
        r = await no_key.post("/api/v1/incidents/INC-1/investigations")
        assert r.status_code == 503 and r.json()["error"]["code"] == "agent_not_configured"

    assert (await api.get("/api/v1/incidents/INC-1/report")).status_code == 404
    r = await api.post("/api/v1/incidents/INC-1/investigations", json={"actor": "arjun"})
    assert r.status_code == 202
    inv = r.json()
    assert inv["status"] == "queued" and inv["requested_by"] == "arjun"
    assert f"inv-{inv['id']}" in await worker.redis.zrange("arq:queue", 0, -1)
    assert (await api.post("/api/v1/incidents/INC-1/investigations")).status_code == 409

    llm = ScriptedLLM(
        [
            turn(use("get_service_health", service="inventory-service")),
            turn(use(REPORT_TOOL, **report_input(["E1"]))),
        ]
    )
    import uuid

    await execute_investigation(worker.sessionmaker, uuid.UUID(inv["id"]), llm, worker.settings)

    detail = (await api.get(f"/api/v1/investigations/{inv['id']}")).json()
    assert detail["status"] == "completed" and len(detail["steps"]) == 4
    assert detail["steps"][1]["evidence_id"] == "E1"
    rep = (await api.get("/api/v1/incidents/INC-1/report")).json()
    assert rep["partial"] is False and rep["report"]["root_cause"]["status"] == "confirmed"
    assert [
        i["status"] for i in (await api.get("/api/v1/incidents/INC-1/investigations")).json()
    ] == ["completed"]


async def test_every_real_tool_returns_compact_json(
    worker: WorkerState, incident: Incident
) -> None:
    ctx = await build_context(worker.sessionmaker, incident.id, 5)
    calls = {
        "get_service_metrics": {"service": "inventory-service", "minutes": 15},
        "query_application_logs": {"min_level": "ERROR"},
        "get_service_health": {"service": "orders-service"},
        "inspect_database_connections": {},
        "get_recent_deployments": {"hours": 2},
        "identify_related_incidents": {},
    }
    for name, inp in calls.items():
        spec = agent_tools.TOOLS[name]
        data = await spec.handler(ctx, spec.input_model.model_validate(inp))  # type: ignore[misc]
        text = json.dumps(data, default=str)
        assert len(text) < 12_000, f"{name} output too large: {len(text)}"
    metrics = await agent_tools.get_service_metrics(
        ctx, agent_tools.MetricsInput(service="inventory-service")
    )
    assert metrics["signals"]["availability"]["latest"] == 0.0
    assert metrics["signals"]["availability"]["baseline_median"] == 1.0
    logs = await agent_tools.query_application_logs(
        ctx, agent_tools.LogsInput(min_level="CRITICAL")
    )
    assert any("Health check failed" in p["pattern"] for p in logs["top_patterns"])
    related = await agent_tools.identify_related_incidents(ctx, agent_tools.RelatedInput())
    assert any(a["service"] == "inventory-service" for a in related["this_incident"]["anomalies"])
