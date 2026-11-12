"""Run an investigation from the terminal and watch it think.

    docker compose exec api python -m app.agent.cli INC-1

Uses the same code path as the worker job (request → execute → persist), so the
result also appears in the API and on the incident timeline. Prints every
model turn and tool call as it happens, then the report.
"""

import argparse
import asyncio
import json
import sys
import textwrap
from typing import Any

from app.agent.llm import AnthropicLLM, LLMResponse
from app.agent.service import execute_investigation, request_investigation
from app.core.config import get_settings
from app.core.errors import ConflictError
from app.db.session import build_engine, build_sessionmaker
from app.repositories import incidents as incident_repo

DIM, BOLD, RED, GREEN, RESET = "\033[2m", "\033[1m", "\033[31m", "\033[32m", "\033[0m"


class PrintRecorder:
    async def llm_call(self, seq: int, response: LLMResponse, latency_ms: float) -> None:
        u = response.usage
        print(
            f"{DIM}── model turn {seq}: {response.stop_reason}, {latency_ms / 1000:.1f}s, "
            f"in {u.input_tokens} (+{u.cache_read_tokens} cached) / out {u.output_tokens}{RESET}"
        )
        for b in response.content:
            if b.get("type") == "text" and b.get("text", "").strip():
                print(textwrap.indent(textwrap.fill(b["text"], 100), "   "))

    async def tool_call(
        self,
        seq: int,
        name: str,
        tool_input: Any,
        output: str,
        evidence_id: str | None,
        is_error: bool,
        latency_ms: float,
    ) -> None:
        tag = f"{RED}ERROR{RESET}" if is_error else f"{GREEN}{evidence_id or 'ok'}{RESET}"
        args = json.dumps(tool_input, separators=(",", ":"))[:120] if tool_input else ""
        print(f"   → {BOLD}{name}{RESET}({args}) [{tag}] {latency_ms:.0f}ms")
        if is_error:
            print(textwrap.indent(output[:600], "     "))


def print_report(report: dict[str, Any]) -> None:
    rc = report["root_cause"]
    print(f"\n{BOLD}Summary{RESET}\n{textwrap.fill(report['summary'], 100)}")
    print(
        f"\n{BOLD}Root cause{RESET} ({rc['status']}, {rc['confidence']} confidence) "
        f"{DIM}{rc['evidence']}{RESET}\n{textwrap.fill(rc['statement'], 100)}"
    )
    for title, key in (("Confirmed findings", "confirmed_findings"), ("Hypotheses", "hypotheses")):
        if report[key]:
            print(f"\n{BOLD}{title}{RESET}")
            for f in report[key]:
                conf = f" ({f['confidence']})" if "confidence" in f else ""
                print(f" • {f['statement']}{conf} {DIM}{f['evidence']}{RESET}")
    if report["recommended_remediations"]:
        print(f"\n{BOLD}Recommended remediation (requires approval){RESET}")
        for r in report["recommended_remediations"]:
            reps = f" → {r['replicas']} replicas" if r.get("replicas") else ""
            print(
                f" • {r['action']} {r['target_service']}{reps} [risk {r['risk']}]"
                f" — {r['rationale']}"
            )
    for issue in report.get("separate_issues", []):
        print(
            f"\n{BOLD}Separate issue{RESET}: {issue['statement']} ({', '.join(issue['services'])})"
        )


async def main(ref: str, actor: str) -> int:
    settings = get_settings()
    key = settings.anthropic_api_key
    if not settings.agent_configured or key is None:
        print("ANTHROPIC_API_KEY is not set in .env (restart the stack after adding it).")
        return 2
    engine = build_engine(settings)
    sessionmaker = build_sessionmaker(engine)
    llm = AnthropicLLM(
        key.get_secret_value(),
        timeout_s=settings.agent_request_timeout_s,
        max_retries=settings.agent_max_retries,
    )
    try:
        async with sessionmaker() as s:
            incident = await incident_repo.get_incident(s, ref)
            if incident is None:
                print(f"Incident {ref} not found.")
                return 2
            try:
                inv = await request_investigation(
                    s, incident, trigger="manual", requested_by=actor, settings=settings
                )
            except ConflictError as exc:
                print(str(exc))
                return 1
            await s.commit()
            print(
                f"{BOLD}Investigating {incident.ref}: {incident.title}{RESET} "
                f"{DIM}(model {settings.anthropic_model}, effort {settings.agent_effort}){RESET}\n"
            )
        outcome = await execute_investigation(
            sessionmaker, inv.id, llm, settings, echo=PrintRecorder()
        )
    finally:
        await llm.close()
        await engine.dispose()

    assert outcome is not None
    u = outcome.usage
    cost = u.cost_usd(settings.anthropic_model)
    print(
        f"\n{BOLD}Status: {outcome.status}{RESET}  {outcome.llm_calls} model calls, "
        f"{outcome.tool_calls} tool calls, {u.input_tokens + u.cache_read_tokens} input / "
        f"{u.output_tokens} output tokens" + (f", ≈${cost:.3f}" if cost is not None else "")
    )
    if outcome.error:
        print(f"{RED}{outcome.error}{RESET}")
    if outcome.report:
        print_report(outcome.report.model_dump(mode="json"))
    print(
        f"\n{DIM}Full report: GET /api/v1/incidents/{ref}/report · steps: "
        f"GET /api/v1/investigations/{inv.id}{RESET}"
    )
    return 0 if outcome.status == "completed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run an AI investigation on an incident.")
    parser.add_argument("incident", help="INC-12, 12 or the incident UUID")
    parser.add_argument("--actor", default="cli")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.incident, args.actor)))
