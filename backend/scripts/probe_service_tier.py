"""Check that every model whose catalog entry declares a priority lane has one.

Run this when adding a provider, moving to a new model, or editing a `priority:`
block. It is the only check we have that the lane we bill a premium for is a lane
the provider actually gives us — billing charges the tier the agent ASKED for,
because the tier that served a turn is dropped before anything we meter, so
nothing at runtime would notice a provider quietly ignoring the parameter.

What it can and cannot catch:

- A provider that does not honour `service_tier` at all: caught. That is the
  failure that would cost us money on every turn, which is why this exists.
- One turn downgraded under load: NOT caught, and not catchable here. OpenAI
  downgrades only above 1M TPM with a >50% ramp inside 15 minutes; xAI bills the
  premium only when it confirms the tier.
- Anything on Gemini: NOT caught. Google validates `service_tier` (a bogus value
  400s) but returns no tier on the response, so entries declaring
  `confirms: false` are reported UNVERIFIABLE rather than passed or failed.

Usage (from backend/, with the venv):

    .venv/bin/python -m scripts.probe_service_tier

Spends a few hundred tokens per entry on Talqing's own platform keys.
"""

from __future__ import annotations

import asyncio
import sys

from openai import AsyncOpenAI

from services.catalog import LLMEntry, get_catalog
from settings import get_settings

# Short, cheap, and pointless to cache — the reply is irrelevant, only the
# `service_tier` the response reports back matters.
PROMPT = "Reply with the single word: ok"


async def probe(entry: LLMEntry, api_key: str) -> tuple[str, str]:
    """(status, detail) for one entry — status is PASS / FAIL / UNVERIFIABLE."""
    tier = entry.require_priority()
    client = AsyncOpenAI(api_key=api_key, base_url=entry.base_url, max_retries=0)
    try:
        if entry.api == "responses":
            resp = await client.responses.create(
                model=entry.model,
                input=PROMPT,
                store=False,
                service_tier=tier.value,
            )
        else:
            resp = await client.chat.completions.create(
                model=entry.model,
                messages=[{"role": "user", "content": PROMPT}],
                service_tier=tier.value,
            )
    except Exception as e:  # noqa: BLE001 — every failure mode is worth printing
        return "FAIL", f"{type(e).__name__}: {str(e)[:160]}"

    served = getattr(resp, "service_tier", None)
    if not tier.confirms:
        # Declared as reporting nothing back. Confirm that is still true: a
        # provider that starts reporting the tier is good news worth acting on,
        # because it means `confirms: true` and this check can start working.
        if served is not None:
            return "UNVERIFIABLE", f"reported {served!r} — the entry can drop `confirms: false`"
        return "UNVERIFIABLE", "provider reports no tier (expected for this entry)"
    if served == tier.value:
        return "PASS", f"served {served!r}"
    return "FAIL", f"asked for {tier.value!r}, served {served!r}"


async def main() -> int:
    keys = get_settings().provider_secrets
    entries = [e for e in get_catalog().llm if e.priority is not None]
    if not entries:
        print("no catalog entry declares a priority lane")
        return 0

    worst = 0
    for entry in entries:
        name = f"{entry.provider}/{entry.model}"
        key = keys.get(entry.provider, "")
        if not key:
            print(f"  SKIP         {name:32} no platform key for {entry.provider}")
            continue
        status, detail = await probe(entry, key)
        print(f"  {status:12} {name:32} {detail}")
        if status == "FAIL":
            worst = 1
    return worst


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
