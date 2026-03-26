"""LLM Router — unified interface via litellm with role-based model selection."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.config import LLMConfig, LLMModelConfig

logger = logging.getLogger(__name__)


@dataclass
class LLMResponse:
    """Standardized LLM response."""
    text: str
    model: str
    role: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    raw: Any = None


@dataclass
class CostTracker:
    """Track cumulative LLM costs."""
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    total_calls: int = 0
    calls_by_role: dict[str, int] = field(default_factory=dict)

    def record(self, response: LLMResponse):
        self.total_input_tokens += response.input_tokens
        self.total_output_tokens += response.output_tokens
        self.total_cost_usd += response.cost_usd
        self.total_calls += 1
        self.calls_by_role[response.role] = self.calls_by_role.get(response.role, 0) + 1


class LLMRouter:
    """Route LLM calls to the right model based on role (classification/extraction/search)."""

    def __init__(self, config: LLMConfig, db=None):
        self.config = config
        self.cost_tracker = CostTracker()
        self._db = db  # optional Database for persistent usage tracking

    def _get_model_config(self, role: str) -> LLMModelConfig:
        if role not in self.config.models:
            raise ValueError(f"Unknown LLM role: {role}. Available: {list(self.config.models)}")
        return self.config.models[role]

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((ConnectionError, TimeoutError)),
        reraise=True,
    )
    async def complete(
        self,
        role: str,
        messages: list[dict[str, str]],
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        **kwargs,
    ) -> LLMResponse:
        """Send a completion request using the model assigned to the given role."""
        import litellm

        model_cfg = self._get_model_config(role)
        model = model_cfg.model
        max_tok = max_tokens or model_cfg.max_tokens
        temp = temperature if temperature is not None else model_cfg.temperature

        full_messages = []
        if system:
            full_messages.append({"role": "system", "content": system})
        full_messages.extend(messages)

        # Custom endpoint support (e.g. openai-oauth proxy)
        extra = {}
        if model_cfg.api_base:
            extra["api_base"] = model_cfg.api_base
        if model_cfg.api_key:
            extra["api_key"] = model_cfg.api_key

        # Reasoning models (gpt-5+, o1, o3) don't support temperature
        is_reasoning = any(tag in model.lower() for tag in ("gpt-5", "o1", "o3"))

        start = time.monotonic()
        try:
            call_kwargs = {
                "model": model,
                "messages": full_messages,
                "max_tokens": max_tok,
                **extra,
                **kwargs,
            }
            if not is_reasoning:
                call_kwargs["temperature"] = temp

            response = await litellm.acompletion(**call_kwargs)
        except Exception as e:
            logger.error(f"LLM call failed (role={role}, model={model}): {e}")
            raise

        latency_ms = int((time.monotonic() - start) * 1000)

        text = response.choices[0].message.content or ""
        usage = response.usage
        input_tokens = usage.prompt_tokens if usage else 0
        output_tokens = usage.completion_tokens if usage else 0

        # Cost estimation via litellm
        cost = 0.0
        try:
            cost = litellm.completion_cost(completion_response=response)
        except Exception:
            pass

        result = LLMResponse(
            text=text,
            model=model,
            role=role,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
            raw=response,
        )

        self.cost_tracker.record(result)
        logger.info(
            f"LLM [{role}] {model}: {input_tokens}+{output_tokens} tokens, "
            f"{latency_ms}ms, ${cost:.5f}"
        )

        # Persist to DB if available
        if self._db:
            try:
                await self._db.log_llm_usage(
                    role=role, model=model,
                    input_tokens=input_tokens, output_tokens=output_tokens,
                    cost_usd=cost, latency_ms=latency_ms,
                )
            except Exception as e:
                logger.debug(f"Failed to log LLM usage to DB: {e}")

        return result

    async def classify(self, text: str, system: str = "") -> LLMResponse:
        """Shortcut for classification calls."""
        return await self.complete(
            role="classification",
            messages=[{"role": "user", "content": text}],
            system=system,
        )

    async def extract(self, text: str, system: str = "") -> LLMResponse:
        """Shortcut for extraction calls."""
        return await self.complete(
            role="extraction",
            messages=[{"role": "user", "content": text}],
            system=system,
        )

    async def search_answer(self, messages: list[dict], system: str = "") -> LLMResponse:
        """Shortcut for search/RAG calls."""
        return await self.complete(
            role="search",
            messages=messages,
            system=system,
        )

    def get_stats(self) -> dict:
        ct = self.cost_tracker
        return {
            "total_calls": ct.total_calls,
            "total_input_tokens": ct.total_input_tokens,
            "total_output_tokens": ct.total_output_tokens,
            "total_cost_usd": round(ct.total_cost_usd, 5),
            "calls_by_role": ct.calls_by_role,
        }
