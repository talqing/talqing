"""One OpenAI-compatible Chat Completions client for the platform's own LLM calls.

This is for work Talqing does *around* a session — today, analysing a finished
call or chat — not for the live agent, which LiveKit's ``openai.LLM`` plugin
owns (see ``compiler.factories``).

Every catalog LLM speaks the OpenAI Chat Completions API, so the vendor is a
`catalog.yaml` choice and never a code change. The caller resolves the model
*and the key it runs on* into an ``LLMTarget`` — post-call analysis spends the
tenant's own key (strict BYOK) — and this module never guesses.

Each call retries on its own: 408/429/5xx and connection errors back off
exponentially, any other 4xx fails at once. Nothing is shared between calls —
every one spends some tenant's own key, so one tenant's rate limit or dead key
must never slow another tenant's analysis down.

No max_tokens cap anywhere — capping output risks silently truncated JSON.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass, field
from decimal import Decimal

import openai
from openai import AsyncOpenAI

from services.catalog import LLMEntry, get_catalog, openrouter
from services.metering import billable_output_tokens

logger = logging.getLogger("talqing.llm")

_MAX_ATTEMPTS = 5
_BASE_BACKOFF = 2.0  # s; doubles per retry attempt

_JSON_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class LLMTarget:
    """A resolved model plus the credential it runs on."""

    provider: str
    model: str
    api_key: str
    base_url: str | None = None
    # What this model thinks by default (catalog `reasoning.default`), or None
    # for a model with no such knob. A caller that wants more says so per call.
    default_reasoning_effort: str | None = None
    # False for an endpoint that rejects the `prompt_cache_key` body parameter —
    # Gemini's, which validates the body against its own proto and 400s the whole
    # call on a field it does not know. Nothing is lost: it caches implicitly.
    supports_prompt_cache_key: bool = True
    extra: dict[str, object] = field(default_factory=dict)

    @classmethod
    def resolve(
        cls, provider: str, model: str, api_key: str, *, hosts: list[str] | None = None
    ) -> LLMTarget:
        """Build a target from the catalog entry for ``provider``/``model``.

        Fails loudly on an unknown entry, a missing key, or a non-OpenAI
        provider with no base_url — each of those would otherwise surface as a
        confusing 401/404 from somebody else's API.

        A ``browse: "search"`` provider takes no entry at all, for the reason
        `_build_llm_one` does not: post-call analysis runs inside the voice job
        process, which holds no model registry. Everything needed is on the
        provider block, and the effort was frozen onto the config at publish.
        ``hosts`` is that spec's own OpenRouter host set, meaningless elsewhere.
        """
        provider = provider.strip().lower()
        catalog = get_catalog()
        meta = catalog.providers.get(provider)
        searched = meta.models.llm if meta is not None and meta.models is not None else None
        if searched is not None:
            if not api_key:
                raise ValueError(f"missing API key for llm provider {provider!r}")
            return cls(
                provider=provider,
                model=model,
                api_key=api_key,
                base_url=searched.base_url.rstrip("/"),
                # Never a default here. On a listed entry this is the catalog's
                # latency-safe choice for that model; a searched provider has no
                # such list in this process, so unset means send nothing and let
                # the model think as it does by default — which for a one-shot
                # analysis of a finished call is a fair trade.
                default_reasoning_effort=None,
                supports_prompt_cache_key=searched.supports_prompt_cache_key,
                # Spread into `chat.completions.create`. The routing block is the
                # same one live turns send — a data-retention promise we make on
                # every request, not just the ones a caller can hear.
                extra={
                    **searched.extra,
                    "extra_body": {"provider": openrouter.routing(hosts)},
                    "extra_headers": dict(openrouter.ATTRIBUTION_HEADERS),
                },
            )
        entry = catalog.require_entry("llm", provider, model)
        if not isinstance(entry, LLMEntry):
            raise TypeError(f"expected LLMEntry for {provider}/{model}")
        if not api_key:
            raise ValueError(f"missing API key for llm provider {provider!r}")
        if not entry.base_url and provider != "openai":
            raise ValueError(
                f"llm provider {provider} has no base_url in catalog.yaml — "
                "required for non-OpenAI OpenAI-compatible providers"
            )
        return cls(
            provider=provider,
            model=entry.model,
            api_key=api_key,
            base_url=entry.base_url.rstrip("/") if entry.base_url else None,
            default_reasoning_effort=entry.effort_for(None),
            supports_prompt_cache_key=entry.supports_prompt_cache_key,
            extra=dict(entry.extra),
        )


@dataclass(frozen=True)
class TokenUsage:
    """What one call consumed, in the quantities billing prices.

    Mirrors ``llm_usage``'s columns rather than the provider's reply shape, so a
    caller that meters this can insert it without a second translation.
    """

    input_tokens: int = 0
    input_cached_tokens: int = 0
    # Input tokens spent WRITING the prompt cache, charged at a premium by the
    # providers that charge for them at all. Split out rather than left inside
    # `input_tokens` because `price_llm_usage` rates the three separately.
    input_cache_write_tokens: int = 0
    output_tokens: int = 0
    # What the provider charged for this one request, where it says so. Only a
    # gateway does; `price_llm_usage` bills that figure and never the rate card,
    # and quarantines a metered row that arrives without one — so a caller that
    # drops this silently un-prices every session it touches.
    reported_cost: Decimal | None = None


@dataclass(frozen=True)
class ChatResult:
    text: str
    usage: TokenUsage


# One client per endpoint+credential, so connections are pooled per tenant
# rather than rebuilt per call. Bounded by (providers × tenants with a key),
# which is small; if that ever stops being true this wants an LRU, not a
# per-call client.
_clients: dict[tuple[str | None, str], AsyncOpenAI] = {}


def _client_for(target: LLMTarget) -> AsyncOpenAI:
    key = (target.base_url, target.api_key)
    client = _clients.get(key)
    if client is None:
        client = AsyncOpenAI(base_url=target.base_url, api_key=target.api_key, max_retries=0)
        _clients[key] = client
    return client


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, openai.APIStatusError):
        status = exc.status_code
        return status in (408, 429) or status >= 500
    return isinstance(exc, (openai.APIConnectionError, openai.APITimeoutError))


async def call_with_retries(fn):
    """Run one LLM call, retrying transient failures with jittered backoff."""
    last: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            return await fn()
        except Exception as e:  # noqa: BLE001 — classified below
            last = e
            if not _is_retryable(e):
                raise
        await asyncio.sleep(_BASE_BACKOFF * (2**attempt) * (0.5 + random.random()))
    raise last  # type: ignore[misc]


def _strip_fence(text: str) -> str:
    return _JSON_FENCE.sub("", text.strip())


def extract_json(text: str):
    """Parse a JSON object from an LLM reply, tolerating ``` fences / preamble."""
    cleaned = _strip_fence(text)
    try:
        return json.loads(cleaned)
    except Exception:
        pass
    # last resort: grab the outermost {...} or [...]
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = cleaned.find(opener), cleaned.rfind(closer)
        if i != -1 and j > i:
            try:
                return json.loads(cleaned[i : j + 1])
            except Exception:
                continue
    raise ValueError(f"could not parse JSON from LLM reply: {text[:200]!r}")


def _assistant_text(resp) -> str:
    choice = resp.choices[0] if resp.choices else None
    if choice is None or choice.message is None:
        return ""
    return choice.message.content or ""


def _usage_of(resp) -> TokenUsage:
    """Read the provider's usage block into the shape billing stores.

    Absent usage means zero, not an error: a provider that reports nothing is
    metered as nothing, and pricing then has nothing to charge for. That is the
    honest outcome — inventing token counts would bill for a guess.
    """
    usage = getattr(resp, "usage", None)
    if usage is None:
        return TokenUsage()
    details = getattr(usage, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", 0) if details is not None else 0
    # Reported only where the provider CHARGES for writing the cache — OpenAI
    # from the GPT-5.6 family onward, Anthropic always — which is what lets
    # pricing quarantine a metered write with no rate rather than bill it at
    # zero. This client sends a `prompt_cache_key`, so it makes them.
    written = getattr(details, "cache_write_tokens", 0) if details is not None else 0
    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    # An extra field on the SDK's typed usage object: present on the providers
    # that price a request themselves, absent on the rest.
    cost = getattr(usage, "cost", None)
    return TokenUsage(
        input_tokens=prompt_tokens,
        input_cached_tokens=int(cached or 0),
        input_cache_write_tokens=int(written or 0),
        reported_cost=None if cost is None else Decimal(str(cost)),
        # Not `completion_tokens` directly: on Gemini's compatibility layer that
        # number leaves out thinking the provider still bills for.
        output_tokens=billable_output_tokens(
            prompt_tokens=prompt_tokens,
            completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            total_tokens=int(getattr(usage, "total_tokens", 0) or 0),
        ),
    )


async def chat(
    target: LLMTarget,
    messages: list[dict],
    *,
    json_mode: bool = False,
    response_format: dict | None = None,
    temperature: float = 0.2,
    timeout: float = 180.0,
    reasoning_effort: str | None = None,
    cache_key: str | None = None,
) -> ChatResult:
    """One Chat Completions call → the assistant's text and what it consumed.

    `response_format` takes precedence over `json_mode` — pass a json_schema
    format when the reply has to match a schema, `json_mode=True` when any JSON
    object will do.

    `reasoning_effort` is only used by explicit offline calls. Unset means the
    model's catalog default, which is Talqing's latency-safe answer ("none"
    wherever the model accepts it).
    """

    async def go() -> ChatResult:
        kwargs = dict(target.extra)
        kwargs.update(
            {
                "model": target.model,
                "messages": messages,
                "temperature": temperature,
                "timeout": timeout,
            }
        )
        if response_format is not None:
            kwargs["response_format"] = response_format
        elif json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if cache_key and target.supports_prompt_cache_key:
            kwargs["prompt_cache_key"] = cache_key
            if target.provider == "xai":
                kwargs["extra_headers"] = {"x-grok-conv-id": cache_key}
        effort = reasoning_effort or target.default_reasoning_effort
        if effort:
            kwargs["reasoning_effort"] = effort
        resp = await _client_for(target).chat.completions.create(**kwargs)
        return ChatResult(text=_assistant_text(resp), usage=_usage_of(resp))

    return await call_with_retries(go)
