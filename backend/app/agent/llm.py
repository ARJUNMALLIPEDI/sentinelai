"""The model behind an interface.

The orchestrator talks to ``LLMClient``, not to the Anthropic SDK directly, so
tests drive the whole investigation loop with a scripted model (no API key, no
network, deterministic) and the real client is a thin adapter.

Responses are normalised to plain dicts. The assistant turn must be sent back
*exactly* as received (thinking blocks with their signatures included), and
dicts make that echo, persistence and test fixtures the same thing.

Retries and timeouts: the SDK retries 429s, 5xx and connection errors with
exponential backoff (``max_retries``) and enforces a per-request ``timeout``.
Anything still failing surfaces as ``LLMError`` and fails the investigation
cleanly instead of crashing the worker.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol

import anthropic
import httpx2

# Base prices (USD per million tokens) from the public model table. Cache reads
# are billed at 5% of input on these models. Used for a cost *estimate* only.
PRICES: dict[str, tuple[float, float]] = {
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-opus-5-5": (4.0, 20.0),
}
CACHE_READ_FACTOR = 0.05


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens

    def cost_usd(self, model: str) -> float | None:
        if model not in PRICES:
            return None
        inp, out = PRICES[model]
        # Cache writes are priced at the base input rate here: an estimate, slightly low.
        billed_input = (
            self.input_tokens + self.cache_write_tokens + self.cache_read_tokens * CACHE_READ_FACTOR
        )
        return round((billed_input * inp + self.output_tokens * out) / 1_000_000, 4)


@dataclass
class LLMResponse:
    content: list[dict[str, Any]]
    stop_reason: str
    usage: Usage = field(default_factory=Usage)
    model: str = ""


class LLMError(Exception):
    """The model call failed after the SDK's own retries (or can't be retried)."""


class LLMClient(Protocol):
    async def create(self, **request: Any) -> LLMResponse: ...


class AnthropicLLM:
    def __init__(
        self,
        api_key: str,
        *,
        timeout_s: float,
        max_retries: int,
        http_client: httpx2.AsyncClient | None = None,  # tests inject a mock transport
    ) -> None:
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key, timeout=timeout_s, max_retries=max_retries, http_client=http_client
        )

    async def create(self, **request: Any) -> LLMResponse:
        try:
            msg = await self._client.messages.create(**request)
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Claude API returned {exc.status_code}: {_short(exc.message)}") from exc
        except anthropic.APITimeoutError as exc:
            raise LLMError("Claude API request timed out") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"could not reach the Claude API: {_short(str(exc))}") from exc
        u = msg.usage
        return LLMResponse(
            content=[b.model_dump(mode="json", exclude_none=True) for b in msg.content],
            stop_reason=msg.stop_reason or "unknown",
            usage=Usage(
                input_tokens=u.input_tokens,
                output_tokens=u.output_tokens,
                cache_read_tokens=getattr(u, "cache_read_input_tokens", None) or 0,
                cache_write_tokens=getattr(u, "cache_creation_input_tokens", None) or 0,
            ),
            model=msg.model,
        )

    async def close(self) -> None:
        await self._client.close()


def _short(text: str, limit: int = 300) -> str:
    return text if len(text) <= limit else text[:limit] + "…"
