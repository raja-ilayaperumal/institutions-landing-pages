"""LLM wrapper — supports Anthropic (preferred) and OpenAI (fallback).

Default models:
  - judge:   Claude Haiku 4.5      → fallback OpenAI gpt-4o-mini
  - extract: Claude Sonnet 4.6     → fallback OpenAI gpt-4o

Both providers return an LLMResult with parsed JSON when possible + cost.
Graceful degradation when no key set: returns empty LLMResult.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog

from ..config import settings

log = structlog.get_logger(__name__)

Tier = Literal["judge", "extract"]

# Anthropic models
MODEL_ANTHROPIC_JUDGE = "claude-haiku-4-5-20251001"
MODEL_ANTHROPIC_EXTRACT = "claude-sonnet-4-6"
# OpenAI fallbacks
MODEL_OPENAI_JUDGE = "gpt-4o-mini"
MODEL_OPENAI_EXTRACT = "gpt-4o"

# Pricing per million tokens (input, output)
_PRICES = {
    MODEL_ANTHROPIC_JUDGE:   (1.00, 5.00),
    MODEL_ANTHROPIC_EXTRACT: (3.00, 15.00),
    MODEL_OPENAI_JUDGE:      (0.15, 0.60),
    MODEL_OPENAI_EXTRACT:    (2.50, 10.00),
}


@dataclass
class LLMResult:
    text: str
    data: Any  # parsed JSON when expect_json=True
    model: str
    provider: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)


def _cost(model: str, inp: int, out: int) -> float:
    inp_p, out_p = _PRICES.get(model, (3.0, 15.0))
    return (inp / 1_000_000) * inp_p + (out / 1_000_000) * out_p


def _extract_json(text: str) -> Any | None:
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    candidate = (fenced.group(1) if fenced else text).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        for opener, closer in (("{", "}"), ("[", "]")):
            start = candidate.find(opener)
            end = candidate.rfind(closer)
            if start != -1 and end > start:
                try:
                    return json.loads(candidate[start:end + 1])
                except json.JSONDecodeError:
                    pass
    return None


def _pick_provider() -> str:
    if settings.has_anthropic:
        return "anthropic"
    if settings.has_openai:
        return "openai"
    return ""


def _model_for(tier: Tier, provider: str) -> str:
    if provider == "anthropic":
        return MODEL_ANTHROPIC_JUDGE if tier == "judge" else MODEL_ANTHROPIC_EXTRACT
    return MODEL_OPENAI_JUDGE if tier == "judge" else MODEL_OPENAI_EXTRACT


async def call(
    *,
    system: str,
    user: str,
    tier: Tier = "judge",
    model: str | None = None,
    max_tokens: int = 1024,
    expect_json: bool = True,
    json_schema_name: str | None = None,
) -> LLMResult:
    """Make an LLM call. Returns LLMResult with parsed JSON when possible."""
    provider = _pick_provider()
    if not provider:
        log.warning("llm.no_api_key")
        return LLMResult(text="", data=None, model="", provider="")
    if model is None:
        model = _model_for(tier, provider)

    if provider == "anthropic":
        return await _call_anthropic(
            system=system, user=user, model=model,
            max_tokens=max_tokens, expect_json=expect_json,
        )
    return await _call_openai(
        system=system, user=user, model=model,
        max_tokens=max_tokens, expect_json=expect_json,
        json_schema_name=json_schema_name,
    )


# Backward-compat constants used by existing connectors
MODEL_JUDGE = MODEL_ANTHROPIC_JUDGE
MODEL_EXTRACT = MODEL_ANTHROPIC_EXTRACT


async def _call_anthropic(*, system: str, user: str, model: str,
                          max_tokens: int, expect_json: bool) -> LLMResult:
    from anthropic import Anthropic

    def _run() -> Any:
        client = Anthropic(api_key=settings.anthropic_api_key)
        return client.messages.create(
            model=model, max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
        )

    msg = await asyncio.to_thread(_run)
    text = "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text")
    data = _extract_json(text) if expect_json else None
    usage = msg.usage
    inp = (
        getattr(usage, "input_tokens", 0)
        + getattr(usage, "cache_creation_input_tokens", 0)
        + getattr(usage, "cache_read_input_tokens", 0)
    )
    out = getattr(usage, "output_tokens", 0)
    cost = _cost(model, inp, out)
    log.debug("llm.call", provider="anthropic", model=model, inp=inp, out=out, cost_usd=cost)
    return LLMResult(
        text=text, data=data, model=model, provider="anthropic",
        input_tokens=inp, output_tokens=out, cost_usd=cost,
        raw={"id": msg.id, "stop_reason": msg.stop_reason},
    )


async def _call_openai(*, system: str, user: str, model: str,
                       max_tokens: int, expect_json: bool,
                       json_schema_name: str | None) -> LLMResult:
    from openai import OpenAI

    def _run() -> Any:
        client = OpenAI(api_key=settings.openai_api_key)
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
        }
        if expect_json:
            kwargs["response_format"] = {"type": "json_object"}
        return client.chat.completions.create(**kwargs)

    resp = await asyncio.to_thread(_run)
    choice = resp.choices[0]
    text = choice.message.content or ""
    data = _extract_json(text) if expect_json else None
    usage = resp.usage
    inp = getattr(usage, "prompt_tokens", 0) or 0
    out = getattr(usage, "completion_tokens", 0) or 0
    cost = _cost(model, inp, out)
    log.debug("llm.call", provider="openai", model=model, inp=inp, out=out, cost_usd=cost)
    return LLMResult(
        text=text, data=data, model=model, provider="openai",
        input_tokens=inp, output_tokens=out, cost_usd=cost,
        raw={"id": resp.id, "finish_reason": choice.finish_reason},
    )
