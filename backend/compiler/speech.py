"""Speaking fixed text, on either pipeline.

A cascade agent reads a script: `session.say()` hands exact words to the TTS.
A realtime speech-to-speech agent cannot — LiveKit raises without a TTS unless
the model advertises `RealtimeCapabilities.supports_say`, and no realtime plugin
does today (checked against livekit-agents 1.6.5). The closest available thing
is to ask the model to say it, which it usually will, in roughly those words.
"""

from __future__ import annotations

from typing import Any

from livekit.agents import llm
from livekit.agents.llm import ToolError
from livekit.agents.types import NOT_GIVEN, NotGivenOr
from livekit.agents.voice import AgentSession, SpeechHandle

# The ceiling on every wait for speech to finish playing: the `wait_for_playback`
# flag on a say/generate_reply operation, and the two waits `end_call` performs
# before hanging up. A stalled TTS provider must not hold a turn open forever —
# without a bound, `end_call`'s `finally` is never reached and the session lives
# until `max_call_duration`. Deriving a ceiling from text length would be more
# precision than this needs; it exists to break a hang, not to pace a sentence.
SPEECH_WAIT_TIMEOUT = 60.0


def disable_interruptions(run_ctx: Any) -> None:
    """Pin the speech handle that owns the running tool so a barge-in cannot kill it.

    An interrupted handle is not merely silenced. `SpeechHandle._cancel` starts a
    5s timer (`INTERRUPTION_TIMEOUT`, livekit-agents 1.6.5) and on expiry marks
    the handle done — after which the tool's own result, however it turns out,
    has no handle left to be spoken through. Any tool that outlives that 5s and
    has something the caller must hear needs this; a caller sitting through a
    slow tool says "hello?" into the silence, and that alone would cost them the
    answer.

    `run_ctx` is duck-typed because two shapes reach here: LiveKit's own
    `RunContext`, and the hook/dry-run contexts of `compiler/compile.py`, which
    satisfy `OperationRunContext` without a `disallow_interruptions`.

    Raises `ToolError` when the handle was *already* interrupted, which is the
    honest outcome: the handle is dying either way, and failing now — inside the
    5s window, while a reply can still be generated — tells the caller something,
    where carrying on would spend the tool's runtime on a result nobody hears.
    """
    disable = getattr(run_ctx, "disallow_interruptions", None)
    try:
        if callable(disable):
            disable()
            return
        speech_handle = getattr(run_ctx, "speech_handle", None)
        if speech_handle is not None:
            speech_handle.allow_interruptions = False
    except RuntimeError as e:
        raise ToolError("Sorry, I can't complete that action after an interruption.") from e


def speak(
    session: AgentSession,
    text: str,
    *,
    allow_interruptions: NotGivenOr[bool] = NOT_GIVEN,
) -> SpeechHandle:
    """Say `text` verbatim, or instruct a realtime model to.

    Every fixed line the agent speaks goes through here: the greeting, the
    no-code `say` operation, and the line spoken while a handoff target loads.
    `allow_interruptions` is LiveKit's own, and unset means the agent's
    interruption setting. A realtime model's server VAD cannot be overruled, so
    LiveKit ignores `False` there — validation refuses the one config that asks.

    The realtime path costs exactness — the model may reword, reorder or trim.
    That is inherent to the pipeline, not a bug to work around; an agent whose
    script has to be exact (a disclosure, a quoted price, a legal line) belongs
    on the cascade. Dispatching on the live session rather than on AgentConfig
    keeps callers from having to thread the pipeline choice down to every
    operation.
    """
    if isinstance(session.llm, llm.RealtimeModel):
        return session.generate_reply(
            instructions=(
                f'Say the following to the user, word for word, and say nothing else: "{text}"'
            ),
            allow_interruptions=allow_interruptions,
        )
    return session.say(text, allow_interruptions=allow_interruptions)
