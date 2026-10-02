"""Reading a provider's token counts honestly.

Providers disagree about what "output tokens" means when a model thinks first.
OpenAI and xAI on the Responses API count reasoning tokens inside `output_tokens`
— measured 2026-08-07: gpt-5.6-luna reported 105 output of which 94 were
reasoning, grok-4.3 reported 3419 of which 3416 were. Gemini's OpenAI
compatibility layer does not: the same probe reported `completion_tokens: 5`,
left `completion_tokens_details` null, and hid 2306 thinking tokens in the gap
between `total_tokens` and prompt + completion.

Google bills those 2306 at the output rate. Metering them as 5 would have the
platform pay for thinking it never charged for — so the gap is closed here,
once, for every path that records LLM usage.
"""

from __future__ import annotations


def billable_output_tokens(*, prompt_tokens: int, completion_tokens: int, total_tokens: int) -> int:
    """The output tokens a provider will actually bill for.

    `total_tokens - prompt_tokens` is the only place a hidden thinking count
    survives, so it wins whenever it is larger than what the provider called
    completion. For a provider that reports honestly the two are equal and this
    changes nothing.

    A provider that omits `total_tokens` or `prompt_tokens` (both are optional on
    the wire, and a 0 there is indistinguishable from an absent field) gets its
    reported count back unchanged: guessing from a half-filled usage object would
    invent tokens, which is the opposite of the problem being fixed.
    """
    if total_tokens <= 0 or prompt_tokens <= 0:
        return completion_tokens
    return max(completion_tokens, total_tokens - prompt_tokens)
