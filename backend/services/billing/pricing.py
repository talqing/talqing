"""Price a session's aggregate usage from the catalog.

provider_cost = sum over provider/model usage at catalog rates (cached tokens
priced cheaper) — the tenant's own spend on their own key, reported back to them,
not money of ours. platform_fee is the only money Talqing earns from a run under
BYOK, and its unit follows the channel: a call is charged its duration at
`catalog.platform_fee_per_minute`, a chat its answered messages at
`catalog.platform_fee_per_message`. A chat has no duration worth billing — it
stays open until somebody ends it. total = sum.

Call-end reason drives policy: failed/stale calls still carry the provider
passthrough (we paid the provider regardless) but are not charged the platform
fee — a crashed call shouldn't earn us our margin. A chat has no such waiver: a
message that failed is already not an answered one.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal

from services.catalog import Pricing, get_catalog

from .models import (
    AvatarPriceLine,
    LLMPriceLine,
    LLMUsage,
    PriceBreakdown,
    PriceLine,
    RealtimePriceLine,
    SessionUsage,
    STTPriceLine,
    TTSPriceLine,
)

# kind → the per-kind PriceLine subclass that carries that kind's quantity fields
_LINE_CLS = {
    "llm": LLMPriceLine,
    "stt": STTPriceLine,
    "tts": TTSPriceLine,
    "realtime": RealtimePriceLine,
    "avatar": AvatarPriceLine,
}

# Explicit failures waive the platform fee (provider passthrough still applies).
# Anything else — including missing/empty close_reason — is a normal completion
# and is charged the fee. No substring heuristics ("error" in reason, etc.).
FAILED_CLOSE_REASONS = frozenset(
    {
        "error",
        "stale",
        "job_shutdown",
        "job crashed",
        "orphaned",
        "unknown",
        "avatar_start_failed",
        "invalid_dispatch_metadata",
        "missing_dispatch_metadata",
        "unknown_tenant",
        "missing_published_definition",
        "rejected",
        # A call we refused for want of credit. Circular if it is missed: the
        # workspace would be charged the very fee it was refused for lacking,
        # which is also the fee that would push the balance further negative.
        "insufficient_credits",
    }
)


def normalize_close_reason(close_reason: str | None) -> str:
    """Lowercased reason, or empty string when unset."""
    return str(close_reason or "").strip().lower()


def is_failed_close_reason(close_reason: str | None) -> bool:
    """True only for explicit failure reasons (waives platform fee).

    Missing/empty reasons are normal completions so successful text turns
    (and any path that forgets to set a reason) still charge the platform fee.
    ``*_failed`` is the only naming convention kept for worker-specific failures.
    """
    reason = normalize_close_reason(close_reason)
    if not reason:
        return False
    if reason in FAILED_CLOSE_REASONS:
        return True
    return reason.endswith("_failed")


def session_status_for_close_reason(close_reason: str | None) -> str:
    return "failed" if is_failed_close_reason(close_reason) else "completed"


# all money math is done in Decimal (no float drift) and quantized to 6 dp,
# matching the NUMERIC(12,6) billing columns.
_CENTS = Decimal("0.000001")
_PER_MILLION = Decimal(1_000_000)
_PER_MINUTE = Decimal(60)


class UnpriceableUsageError(Exception):
    """Usage cannot be priced: missing catalog entry or a required rate is unset.

    We fail loudly rather than silently bill at $0 — an unpriced provider
    corrupts billing and the admin cost analytics the control plane exists to
    serve.
    """


def _require_entry(cat, kind: str, provider: str, model: str):
    e = cat.entry(kind, provider, model)
    if e is None:
        raise UnpriceableUsageError(
            f"no catalog entry for {kind} usage provider={provider!r} model={model!r} — "
            "metering must store canonical, catalog-matched names"
        )
    return e


def _d(x: float | int | Decimal) -> Decimal:
    """Exact Decimal for a catalog rate / count (via str() so a float like 0.16
    becomes Decimal('0.16'), not its binary-float expansion)."""
    return Decimal(str(x))


def _q(amount: Decimal) -> Decimal:
    return amount.quantize(_CENTS, rounding=ROUND_HALF_UP)


class _RowRates:
    """Catalog rates for one usage row, and whether any of it was billable.

    A usage row records every dimension the provider *metered*; a catalog entry
    declares only the ones it *charges on*. Those sets differ routinely and
    legitimately: LiveKit reports audio seconds for every TTS provider, but
    nine of our eleven bill per character — those seconds feed talk time, not
    the bill. So a metered dimension with no rate prices at zero.

    What is not legitimate is a row that metered something and matched no rate
    at all. That is a catalog omission, and billing $0 would hide it, so
    ``require_billable`` quarantines the session instead.
    """

    def __init__(self, kind: str, provider: str, model: str) -> None:
        self._kind = kind
        self._provider = provider
        self._model = model
        self._metered: list[str] = []
        self._billable = False

    def of(self, quantity: float | int, rate: float | None, field: str) -> Decimal:
        """The rate to multiply ``quantity`` by — zero when unused or unpriced."""
        if not quantity:
            return Decimal(0)
        self._metered.append(field)
        if rate is None:
            return Decimal(0)
        # A rate of 0.0 is a decision (a free model), not a missing rate.
        self._billable = True
        return _d(rate)

    def require_billable(self) -> None:
        if self._metered and not self._billable:
            raise UnpriceableUsageError(
                f"{self._kind} {self._provider}/{self._model}: metered "
                f"{', '.join(self._metered)} but the catalog entry prices none of it"
            )


def price_llm_usage(lines: Sequence[LLMUsage]) -> tuple[Decimal, list[LLMPriceLine]]:
    """What a bag of LLM token counts costs, at catalog rates.

    The one implementation of "what does a token cost", shared by
    ``price_session`` and by ``services.tasks.run`` — a task run has no duration
    to bill and no platform fee, but its tokens are priced by exactly this.
    Raises ``UnpriceableUsageError`` when a row metered something the catalog
    prices none of, so the caller quarantines it rather than billing $0.

    A row carrying ``reported_cost`` skips all of that: the provider already
    priced the request and said so, which is the only correct answer for a
    gateway that routes each call to a different host at that host's price.
    """
    cat = get_catalog()
    total = Decimal(0)
    priced: list[LLMPriceLine] = []
    for u in lines:
        # A provider whose models come from a live registry is a gateway rather
        # than a vendor — that is WHY its list is too large and too volatile to
        # write down — and a gateway's published rate is the cheapest host's, not
        # what this call cost. OpenRouter routes each request to one of ~106
        # upstreams at that upstream's price (two consecutive requests for one
        # model measured 2.4x apart) and reports the exact charge back, so that
        # figure is the line and the rate card never touches the bill.
        if (meta := cat.providers.get(u.provider)) is not None and meta.searched("llm"):
            metered = u.input_tokens or u.output_tokens
            if metered and u.reported_cost is None:
                # A hole in the metering, not a free session. Pricing it off the
                # rate card would invent a number the tenant cannot reconcile
                # against their own provider dashboard, which is the one property
                # this whole path exists to keep.
                raise UnpriceableUsageError(
                    f"llm {u.provider}/{u.model} bills from the cost {meta.label} reports per "
                    "request, and this session metered tokens without one"
                )
            cost = _q(u.reported_cost or Decimal(0))
            total += cost
            priced.append(
                LLMPriceLine(
                    provider=u.provider,
                    model=u.model,
                    cost=cost,
                    # Null on purpose: there were no rates. `provider_reported`
                    # is what says the number came from the gateway itself, and
                    # a tenant reading the breakdown needs to know which of the
                    # two kinds of line they are looking at.
                    rates=None,
                    provider_reported=True,
                    uncached_input_tokens=max(
                        u.input_tokens - u.input_cached_tokens - u.input_cache_write_tokens, 0
                    ),
                    input_cached_tokens=u.input_cached_tokens,
                    input_cache_write_tokens=u.input_cache_write_tokens,
                    output_tokens=u.output_tokens,
                    purpose=u.purpose,
                    priority=u.priority,
                )
            )
            continue
        entry = _require_entry(cat, "llm", u.provider, u.model)
        if u.priority and entry.priority is None:
            # The agent ran in a lane the catalog no longer prices — the entry's
            # `priority:` block was removed after this agent was published.
            # Quarantine the session rather than silently billing it as standard,
            # which is the same call `_RowRates.require_billable` makes.
            raise UnpriceableUsageError(
                f"llm {u.provider}/{u.model} ran in the priority lane but the catalog "
                "entry no longer declares one — restore its `priority:` rates"
            )
        p = entry.rates_for(u.priority)
        rates = _RowRates("llm", u.provider, u.model)
        # Input splits three ways, not two: read from the cache, WRITTEN to it,
        # and everything else at the plain rate.
        fresh = max(u.input_tokens - u.input_cached_tokens - u.input_cache_write_tokens, 0)
        input_rate = rates.of(fresh, p.input_per_1m, "input_per_1m")
        # A cache rate falls back to the full input rate: a model that caches
        # but publishes no discount still bills something.
        cached_rate = rates.of(
            u.input_cached_tokens,
            p.cached_input_per_1m if p.cached_input_per_1m is not None else p.input_per_1m,
            "cached_input_per_1m",
        )
        if u.input_cache_write_tokens and p.cache_write_per_1m is None:
            # No fallback to the plain input rate here, unlike the cached one
            # above, and the difference is what the two silences mean. Every
            # provider reports cached tokens; only the ones that CHARGE for cache
            # writes report those (measured: gpt-5.6-luna reports 1691 on a cold
            # call, gpt-5.4-nano and gpt-4.1-mini report zero on the same
            # prompt). So a metered write with no rate is a catalog omission, and
            # billing it at the plain rate — or at zero, which is the bug this
            # replaces — would hide it for the next model family too.
            raise UnpriceableUsageError(
                f"llm {u.provider}/{u.model} wrote {u.input_cache_write_tokens} tokens to the "
                "prompt cache but the catalog entry has no `cache_write_per_1m` — transcribe "
                "it from the provider's pricing page"
            )
        write_rate = rates.of(
            u.input_cache_write_tokens, p.cache_write_per_1m, "cache_write_per_1m"
        )
        output_rate = rates.of(u.output_tokens, p.output_per_1m, "output_per_1m")
        cost = _q(
            _d(fresh) / _PER_MILLION * input_rate
            + _d(u.input_cached_tokens) / _PER_MILLION * cached_rate
            + _d(u.input_cache_write_tokens) / _PER_MILLION * write_rate
            + _d(u.output_tokens) / _PER_MILLION * output_rate
        )
        rates.require_billable()
        total += cost
        priced.append(
            LLMPriceLine(
                provider=u.provider,
                model=u.model,
                cost=cost,
                rates=p,
                uncached_input_tokens=fresh,
                input_cached_tokens=u.input_cached_tokens,
                input_cache_write_tokens=u.input_cache_write_tokens,
                output_tokens=u.output_tokens,
                purpose=u.purpose,
                priority=u.priority,
            )
        )
    return total, priced


def price_session(
    usage: SessionUsage,
    duration_s: int | None,
    close_reason: str | None,
    channel: str,
    answered_messages: int | None,
) -> PriceBreakdown:
    """Price one session. ``channel`` picks the platform fee and is required: a
    caller that forgets it must fail type-checking, not price a video call at the
    voice rate.

    ``answered_messages`` is the billable unit of a chat — the inbound messages
    whose turn finished ``done`` — and ``None`` for a call, which is billed on
    ``duration_s`` instead."""
    cat = get_catalog()
    lines: dict[str, list[PriceLine]] = {kind: [] for kind in _LINE_CLS}
    provider_cost = Decimal(0)

    def add(kind: str, provider: str, model: str, p: Pricing, cost: Decimal, **fields) -> None:
        nonlocal provider_cost
        cost = _q(cost)
        provider_cost += cost
        # `kind` is set by the subclass's Literal default; **fields are that
        # kind's quantity fields (uncached_input_tokens / audio_duration / …)
        lines[kind].append(
            _LINE_CLS[kind](provider=provider, model=model, cost=cost, rates=p, **fields)
        )

    # Already quantized per line by `price_llm_usage`, which is what `add` would
    # have done — so the total it returns is the same number, arrived at once.
    llm_cost, lines["llm"] = price_llm_usage(usage.llm)
    provider_cost += llm_cost

    for u in usage.stt:
        # The gateway branch `price_llm_usage` takes, for the same reason: the
        # charge OpenRouter reported is the line, and the rate card — the
        # cheapest host's, in a unit it does not name — never touches the bill.
        if (meta := cat.providers.get(u.provider)) is not None and meta.searched("stt"):
            if u.audio_duration and u.reported_cost is None:
                # A hole in the metering, not a free call.
                raise UnpriceableUsageError(
                    f"stt {u.provider}/{u.model} bills from the cost {meta.label} reports per "
                    "request, and this session metered audio without one"
                )
            cost = _q(u.reported_cost or Decimal(0))
            provider_cost += cost
            lines["stt"].append(
                STTPriceLine(
                    provider=u.provider,
                    model=u.model,
                    cost=cost,
                    rates=None,
                    provider_reported=True,
                    audio_duration=u.audio_duration,
                    input_tokens=u.input_tokens,
                    output_tokens=u.output_tokens,
                )
            )
            continue
        p = _require_entry(cat, "stt", u.provider, u.model).pricing
        rates = _RowRates("stt", u.provider, u.model)
        cost = (
            _d(u.audio_duration)
            * rates.of(u.audio_duration, p.per_audio_second, "per_audio_second")
            + _d(u.input_tokens)
            / _PER_MILLION
            * rates.of(u.input_tokens, p.input_per_1m, "input_per_1m")
            + _d(u.output_tokens)
            / _PER_MILLION
            * rates.of(u.output_tokens, p.output_per_1m, "output_per_1m")
        )
        rates.require_billable()
        add(
            "stt",
            u.provider,
            u.model,
            p,
            cost,
            audio_duration=u.audio_duration,
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
        )

    for u in usage.tts:
        p = _require_entry(cat, "tts", u.provider, u.model).pricing
        rates = _RowRates("tts", u.provider, u.model)
        cost = (
            _d(u.characters_count) * rates.of(u.characters_count, p.per_character, "per_character")
            + _d(u.audio_duration)
            * rates.of(u.audio_duration, p.per_audio_second, "per_audio_second")
            + _d(u.input_tokens)
            / _PER_MILLION
            * rates.of(u.input_tokens, p.input_per_1m, "input_per_1m")
            + _d(u.output_tokens)
            / _PER_MILLION
            * rates.of(
                u.output_tokens,
                p.output_per_1m,
                "output_per_1m",
            )
        )
        rates.require_billable()
        add(
            "tts",
            u.provider,
            u.model,
            p,
            cost,
            characters_count=u.characters_count,
            audio_duration=u.audio_duration,
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
        )

    for u in usage.realtime:
        # A speech-to-speech model bills every modality at its own rate, and the
        # audio ones run several times the text ones — so each is priced from its
        # own quantity rather than a summed input_tokens. Providers that meter
        # the audio stream by the minute instead report only session_seconds and
        # carry explicit 0.00 token rates, which is why the token terms below
        # cost nothing for them rather than raising.
        p = _require_entry(cat, "realtime", u.provider, u.model).pricing
        rates = _RowRates("realtime", u.provider, u.model)
        fresh_text_in = max(u.input_text_tokens - u.input_cached_text_tokens, 0)
        fresh_audio_in = max(u.input_audio_tokens - u.input_cached_audio_tokens, 0)
        # A cached rate falls back to the full rate, as it does for the LLM kind:
        # a model that caches but publishes no discount still bills something.
        # (quantity, catalog rate, rate name, divisor) — seven terms is too many
        # to spell out one at a time, and they all price the same way.
        terms: list[tuple[float | int, float | None, str, Decimal]] = [
            (fresh_text_in, p.text_input_per_1m, "text_input_per_1m", _PER_MILLION),
            (
                u.input_cached_text_tokens,
                p.cached_text_input_per_1m
                if p.cached_text_input_per_1m is not None
                else p.text_input_per_1m,
                "cached_text_input_per_1m",
                _PER_MILLION,
            ),
            (fresh_audio_in, p.audio_input_per_1m, "audio_input_per_1m", _PER_MILLION),
            (
                u.input_cached_audio_tokens,
                p.cached_audio_input_per_1m
                if p.cached_audio_input_per_1m is not None
                else p.audio_input_per_1m,
                "cached_audio_input_per_1m",
                _PER_MILLION,
            ),
            (u.output_text_tokens, p.text_output_per_1m, "text_output_per_1m", _PER_MILLION),
            (u.output_audio_tokens, p.audio_output_per_1m, "audio_output_per_1m", _PER_MILLION),
            (u.session_seconds, p.per_session_minute, "per_session_minute", _PER_MINUTE),
        ]
        cost = sum(
            (
                _d(quantity) / divisor * rates.of(quantity, catalog_rate, field)
                for quantity, catalog_rate, field, divisor in terms
            ),
            Decimal(0),
        )
        rates.require_billable()
        add(
            "realtime",
            u.provider,
            u.model,
            p,
            cost,
            uncached_input_text_tokens=fresh_text_in,
            input_cached_text_tokens=u.input_cached_text_tokens,
            uncached_input_audio_tokens=fresh_audio_in,
            input_cached_audio_tokens=u.input_cached_audio_tokens,
            output_text_tokens=u.output_text_tokens,
            output_audio_tokens=u.output_audio_tokens,
            session_seconds=u.session_seconds,
        )

    for u in usage.avatar:
        # avatar minutes are WALL-CLOCK (idle included), billed per second
        p = _require_entry(cat, "avatar", u.provider, u.model).pricing
        rates = _RowRates("avatar", u.provider, u.model)
        cost = _d(u.seconds) / _PER_MINUTE * rates.of(u.seconds, p.per_minute, "per_minute")
        rates.require_billable()
        add("avatar", u.provider, u.model, p, cost, seconds=u.seconds)

    per_minute: float | None = None
    per_message: float | None = None
    if channel == "text":
        if answered_messages is None:
            raise ValueError("a text session is priced per answered message, and none was given")
        per_message = cat.platform_fee_per_message.text
        platform_fee = _q(_d(answered_messages) * _d(per_message))
    else:
        per_minute = cat.platform_fee_per_minute.rate_for(channel)
        minutes = _d(duration_s if duration_s is not None else 0) / _PER_MINUTE
        platform_fee = (
            Decimal(0) if is_failed_close_reason(close_reason) else _q(minutes * _d(per_minute))
        )
    provider_cost = _q(provider_cost)
    return PriceBreakdown(
        provider_cost=provider_cost,
        platform_fee=platform_fee,
        total_charge=provider_cost + platform_fee,
        duration_s=duration_s,
        platform_fee_per_minute=per_minute,
        platform_fee_per_message=per_message,
        answered_messages=answered_messages,
        llm=lines["llm"],
        stt=lines["stt"],
        tts=lines["tts"],
        realtime=lines["realtime"],
        avatar=lines["avatar"],
    )
