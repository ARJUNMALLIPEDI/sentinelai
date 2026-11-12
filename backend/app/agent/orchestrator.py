"""The investigation loop: Claude decides which tools to call; this code enforces
the rules around it.

    brief ─► Claude ─► tool_use blocks ─► validate ─► run (timeout) ─► tool_result + evidence ID
                ▲                                                          │
                └──────────────────────────────────────────────────────────┘
          … until Claude submits generate_incident_report and it passes validation.

What the loop guarantees, whatever the model does:

* **Bounded.** Hard limits on tool calls, turns (model calls), wall-clock time,
  output tokens per call and report attempts. Exhausting any of them ends the
  investigation as ``incomplete`` with a partial report of the evidence
  gathered, never a hang or a silent failure.
* **Validated in, validated out.** Tool inputs are re-validated with Pydantic;
  the report must pass schema and grounding checks. Failures go back to the
  model as ``is_error`` tool results it can correct.
* **Protocol-correct.** The assistant turn is echoed verbatim (adaptive
  thinking blocks must come back unmodified). Every ``tool_use`` gets exactly
  one ``tool_result``, results come first in the user message, and steering
  text comes after them.
* **Auditable.** Every model call and tool call goes to the ``Recorder`` as it
  happens (the worker persists them; the CLI prints them).

Forced ``tool_choice`` is not used: with adaptive thinking on Sonnet 5.5 the
API rejects it. The model is steered to the report tool by prompt and, if it
stops without one, by a single nudge.
"""

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import ValidationError

from app.agent.llm import LLMClient, LLMError, LLMResponse, Usage
from app.agent.prompts import SYSTEM_PROMPT
from app.agent.report import IncidentReport, grounding_errors
from app.agent.tools import REPORT_TOOL, TOOLS, ToolContext, ToolInputError, tool_definitions

logger = logging.getLogger("sentinel.agent")


@dataclass(frozen=True)
class AgentLimits:
    max_tool_calls: int = 20
    max_turns: int = 14
    max_wall_s: float = 300.0
    tool_timeout_s: float = 15.0
    max_report_attempts: int = 3
    max_nudges: int = 1
    max_tokens: int = 16_000
    max_tool_output_chars: int = 12_000


class Recorder(Protocol):
    async def llm_call(self, seq: int, response: LLMResponse, latency_ms: float) -> None: ...

    async def tool_call(
        self,
        seq: int,
        name: str,
        tool_input: Any,
        output: str,
        evidence_id: str | None,
        is_error: bool,
        latency_ms: float,
    ) -> None: ...


class NullRecorder:
    async def llm_call(self, seq: int, response: LLMResponse, latency_ms: float) -> None:
        return None

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
        return None


@dataclass
class Outcome:
    status: str  # completed | incomplete | failed
    report: IncidentReport | None = None
    partial: dict[str, Any] | None = None
    error: str | None = None
    usage: Usage = field(default_factory=Usage)
    llm_calls: int = 0
    tool_calls: int = 0
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)
    stop_reason: str | None = None


class _Loop:
    def __init__(
        self,
        ctx: ToolContext,
        llm: LLMClient,
        model: str,
        effort: str,
        limits: AgentLimits,
        recorder: Recorder,
        clock: Callable[[], float],
    ) -> None:
        self.ctx, self.llm, self.model, self.effort = ctx, llm, model, effort
        self.limits, self.recorder, self.clock = limits, recorder, clock
        self.out = Outcome(status="running")
        self.seq = 0
        self.report_attempts = 0
        self.nudges = 0
        self.last_text = ""
        self._report_feedback = ""

    # ---- helpers -------------------------------------------------------------------------

    def _next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def _request(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_tokens": self.limits.max_tokens,
            "system": SYSTEM_PROMPT,
            "tools": tool_definitions(sorted(self.ctx.services)),
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self.effort},
            # Automatic prompt caching: the system prompt, tool definitions and the
            # growing conversation prefix are re-read at the cache rate each turn.
            "cache_control": {"type": "ephemeral"},
            "messages": messages,
        }

    def _finish(self, status: str, error: str | None = None) -> Outcome:
        self.out.status = status
        self.out.error = error
        if status != "completed":
            self.out.partial = {
                "partial": True,
                "reason": error,
                "evidence_collected": [
                    {"evidence_id": eid, **meta} for eid, meta in self.out.evidence.items()
                ],
                "last_model_text": self.last_text[:2000] or None,
            }
        return self.out

    def _result(self, tool_use_id: str, content: str, is_error: bool = False) -> dict[str, Any]:
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": content,
        }
        if is_error:
            block["is_error"] = True
        return block

    # ---- tools -----------------------------------------------------------------------------

    async def _execute(
        self, block: dict[str, Any], allowed: bool
    ) -> tuple[dict[str, Any] | None, str | None, float]:
        """Run one non-report tool. Returns (data, error, latency_ms); exactly one is set."""
        name, started = block["name"], time.perf_counter()
        spec = TOOLS.get(name)
        if spec is None or spec.handler is None:
            return None, f"Unknown tool '{name}'.", 0.0
        if not allowed:
            return (
                None,
                (
                    f"Tool budget of {self.limits.max_tool_calls} calls is exhausted. "
                    f"Call {REPORT_TOOL} now with the evidence you have."
                ),
                0.0,
            )
        try:
            inp = spec.input_model.model_validate(block.get("input") or {})
            data = await asyncio.wait_for(spec.handler(self.ctx, inp), self.limits.tool_timeout_s)
        except ValidationError as exc:
            return None, f"Invalid input: {_validation_summary(exc)}", _ms(started)
        except ToolInputError as exc:
            return None, str(exc), _ms(started)
        except TimeoutError:
            return (
                None,
                (
                    f"{name} timed out after {self.limits.tool_timeout_s:.0f}s; try a "
                    "narrower window or another tool."
                ),
                _ms(started),
            )
        except Exception as exc:  # a tool bug must not kill the investigation
            logger.exception("tool_failed", extra={"ctx": {"tool": name}})
            return (
                None,
                f"{name} failed with an internal error ({type(exc).__name__}).",
                _ms(started),
            )
        return data, None, _ms(started)

    async def _handle_tool_turn(self, content: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
        """Process a tool_use turn. Returns the user message content, or None when done."""
        uses = [b for b in content if b.get("type") == "tool_use"]
        others = [b for b in uses if b["name"] != REPORT_TOOL]
        reports = [b for b in uses if b["name"] == REPORT_TOOL]

        # Budget is decided in block order, before running anything, so it is deterministic.
        allowed = []
        for b in others:
            ok = b["name"] in TOOLS and self.out.tool_calls < self.limits.max_tool_calls
            if ok:
                self.out.tool_calls += 1
            allowed.append(ok)
        ran = await asyncio.gather(
            *(self._execute(b, ok) for b, ok in zip(others, allowed, strict=True))
        )

        results: dict[str, dict[str, Any]] = {}
        for b, (data, error, latency) in zip(others, ran, strict=True):
            eid = None
            if data is not None:
                eid = f"E{len(self.out.evidence) + 1}"
                self.out.evidence[eid] = {"tool": b["name"], "input": b.get("input")}
                text = json.dumps({"evidence_id": eid, **data}, default=str, separators=(",", ":"))
                if len(text) > self.limits.max_tool_output_chars:
                    text = (
                        text[: self.limits.max_tool_output_chars]
                        + " …[truncated: narrow the query]"
                    )
            else:
                text = error or "unknown error"
            await self.recorder.tool_call(
                self._next_seq(), b["name"], b.get("input"), text, eid, data is None, latency
            )
            results[b["id"]] = self._result(b["id"], text, data is None)

        for b in reports:
            accepted = await self._check_report(b)
            if accepted:
                return None
            results[b["id"]] = self._result(b["id"], self._report_feedback, True)

        user_content = [results[b["id"]] for b in uses]  # one result per tool_use, in order
        steer = self._steering()
        if steer:
            user_content.append({"type": "text", "text": steer})
        return user_content

    async def _check_report(self, block: dict[str, Any]) -> bool:
        self.report_attempts += 1
        errors: list[str]
        try:
            report = IncidentReport.model_validate(block.get("input") or {})
            errors = grounding_errors(report, self.out.evidence, self.ctx.services)
        except ValidationError as exc:
            report, errors = None, [_validation_summary(exc)]
        if report is not None and not errors:
            self.out.report = report
            await self.recorder.tool_call(
                self._next_seq(), REPORT_TOOL, block.get("input"), "accepted", None, False, 0.0
            )
            return True
        self._report_feedback = (
            "Report rejected — fix these problems and call generate_incident_report again:\n- "
            + "\n- ".join(errors)
        )
        await self.recorder.tool_call(
            self._next_seq(),
            REPORT_TOOL,
            block.get("input"),
            self._report_feedback,
            None,
            True,
            0.0,
        )
        return False

    def _steering(self) -> str | None:
        turns_left = self.limits.max_turns - self.out.llm_calls
        if self.report_attempts >= self.limits.max_report_attempts:
            return None
        if turns_left <= 1 or self.out.tool_calls >= self.limits.max_tool_calls:
            return f"Budget nearly exhausted: call {REPORT_TOOL} now with the evidence you have."
        return None

    # ---- main loop ------------------------------------------------------------------------------

    async def run(self, brief: str) -> Outcome:
        started = self.clock()
        messages: list[dict[str, Any]] = [{"role": "user", "content": brief}]
        while self.out.llm_calls < self.limits.max_turns:
            if self.clock() - started > self.limits.max_wall_s:
                return self._finish(
                    "incomplete", f"time budget of {self.limits.max_wall_s:.0f}s exhausted"
                )
            t0 = time.perf_counter()
            try:
                resp = await self.llm.create(**self._request(messages))
            except LLMError as exc:
                return self._finish("failed", str(exc))
            self.out.llm_calls += 1
            self.out.usage.add(resp.usage)
            self.out.stop_reason = resp.stop_reason
            await self.recorder.llm_call(self._next_seq(), resp, _ms(t0))
            text = "\n".join(b.get("text", "") for b in resp.content if b.get("type") == "text")
            if text.strip():
                self.last_text = text
            messages.append({"role": "assistant", "content": resp.content})  # verbatim echo

            if resp.stop_reason == "tool_use":
                user_content = await self._handle_tool_turn(resp.content)
                if user_content is None:
                    return self._finish("completed")
                if self.report_attempts >= self.limits.max_report_attempts:
                    return self._finish(
                        "incomplete", f"report rejected {self.report_attempts} times"
                    )
                messages.append({"role": "user", "content": user_content})
            elif resp.stop_reason == "end_turn":
                if self.nudges >= self.limits.max_nudges:
                    return self._finish("incomplete", "model stopped without submitting a report")
                self.nudges += 1
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"You stopped without calling {REPORT_TOOL}. Submit the report now; "
                            "if the evidence is insufficient, say so and mark the root cause "
                            "undetermined."
                        ),
                    }
                )
            elif resp.stop_reason == "max_tokens":
                # Any tool_use in a truncated turn still needs a result to keep the protocol valid.
                cut = [
                    self._result(
                        b["id"], "Your response was cut off before this call completed.", True
                    )
                    for b in resp.content
                    if b.get("type") == "tool_use"
                ]
                if self.nudges >= self.limits.max_nudges:
                    return self._finish("incomplete", "model output exceeded max_tokens")
                self.nudges += 1
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            *cut,
                            {
                                "type": "text",
                                "text": (
                                    "Your last response hit the output limit. Be more concise."
                                ),
                            },
                        ],
                    }
                )
            elif resp.stop_reason == "refusal":
                return self._finish("failed", "the model declined to continue (refusal)")
            else:
                return self._finish("failed", f"unexpected stop_reason '{resp.stop_reason}'")
        return self._finish(
            "incomplete", f"turn budget of {self.limits.max_turns} model calls exhausted"
        )


async def run_investigation(
    ctx: ToolContext,
    llm: LLMClient,
    brief: str,
    *,
    model: str,
    effort: str = "medium",
    limits: AgentLimits | None = None,
    recorder: Recorder | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Outcome:
    loop = _Loop(
        ctx, llm, model, effort, limits or AgentLimits(), recorder or NullRecorder(), clock
    )
    return await loop.run(brief)


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def _validation_summary(exc: ValidationError, limit: int = 8) -> str:
    parts = [f"{'.'.join(str(p) for p in e['loc']) or 'input'}: {e['msg']}" for e in exc.errors()]
    more = f" (+{len(parts) - limit} more)" if len(parts) > limit else ""
    return "; ".join(parts[:limit]) + more
