"""Digits the caller types, delivered as a turn the agent can answer.

**Out-of-band only.** We receive the platform's keypad *events* — RFC 4733 or SIP
INFO on a phone call, a `dtmf` message on a stream — and never detect tones in
the audio. Everyone in this market draws the same line, and it has to be said in
the docs, because "I pressed 1 and nothing happened" on a carrier that only sends
in-band tones is otherwise unexplainable.

**A property of that: out-of-band digits are not in the recording.** They never
entered the audio path, so a PIN typed on a keypad is absent from the stored
stereo file by construction. That is what makes leaving recording on the agent's
own setting safe, and it is worth stating as a positive property rather than
leaving every customer to work it out.

One collector for both channels, deliberately: the transport underneath a phone
call and a media stream is the same LiveKit primitive
(`room.on("sip_dtmf_received")`), and building this twice is how the two channels
end up behaving differently.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from livekit import rtc
from livekit.agents import AgentSession
from livekit.agents.utils.aio.debounce import Debounced

from services.agents import KeypadInputSpec

logger = logging.getLogger("talqing.workers.voice.keypad")

# A stuck key must not be able to grow a buffer that never resolves. At the
# ceiling the entry flushes immediately rather than being truncated silently.
MAX_ENTRY_LENGTH = 50

# What a telephone keypad can produce. `A`-`D` are the fourth column, which no
# consumer handset has and which private systems still use.
_KEYPAD = frozenset("0123456789*#ABCD")

# What the transcript says a keypad entry is. Deliberately labelled: a bare
# `1234` is indistinguishable from the caller *saying* "one two three four", and
# those are different facts to anyone auditing a call or writing an analysis
# prompt.
ENTRY_PREFIX = "Keypad entry: "


class KeypadCollector:
    """Buffers a caller's keypresses and commits them as one user turn.

    Attached to the room by whichever worker path built it; nothing here knows
    whether the digits came off a trunk or off a partner's socket.
    """

    def __init__(
        self,
        session: AgentSession,
        spec: KeypadInputSpec,
        *,
        on_digit: Callable[[str], None] | None = None,
    ) -> None:
        self._session = session
        self._spec = spec
        self._on_digit = on_digit
        self._buffer: list[str] = []
        self._closed = False
        # `timeout == 0` means "wait for the terminator, however long that
        # takes", so there is no timer at all on that setting — and validation
        # refuses `timeout == 0` together with an empty terminator, which would
        # be a buffer that can never flush.
        self._quiet: Debounced[None] | None = (
            Debounced(self._flush_async, spec.timeout) if spec.timeout > 0 else None
        )

    def attach(self, room: rtc.Room) -> None:
        """Listen for keypresses, and stop when the session does.

        Both wired here rather than left to each worker path, because they are
        one decision: a collector that outlives its session is a quiet timer that
        fires into a closed `AgentSession`, and the exception lands in a bare task
        where nobody sees it.
        """

        @room.on("sip_dtmf_received")
        def _on_dtmf(event: rtc.SipDTMF) -> None:
            self.feed(event.digit)

        @self._session.on("close")
        def _on_close(_: object) -> None:
            self.close()

    def feed(self, digit: str) -> None:
        """One keypress. Synchronous, because it is a LiveKit event callback."""
        if self._closed:
            return
        key = (digit or "").strip().upper()
        if key not in _KEYPAD:
            logger.warning("ignoring unreadable keypad digit %r", digit)
            return
        if self._on_digit is not None:
            self._on_digit(key)

        if not self._buffer:
            # The first digit interrupts. A caller who starts typing during a
            # prompt has stopped listening, and this is the half of the feature
            # that makes it feel responsive rather than laggy.
            try:
                self._session.interrupt()
            except Exception:
                logger.debug("keypad interrupt had nothing to interrupt", exc_info=True)

        if self._spec.terminator and key == self._spec.terminator:
            # The terminator flushes now and is not part of the entry. On an
            # empty buffer it is ignored, so a caller finishing a previous entry
            # with `#` does not open an empty turn.
            if self._buffer:
                self._flush()
            return

        self._buffer.append(key)
        if len(self._buffer) >= MAX_ENTRY_LENGTH:
            self._flush()
            return
        if self._quiet is not None:
            self._quiet.schedule()

    def _flush(self) -> None:
        if self._quiet is not None:
            self._quiet.cancel()
        digits, self._buffer = "".join(self._buffer), []
        if not digits or self._closed:
            return
        try:
            self._session.generate_reply(user_input=f"{ENTRY_PREFIX}{digits}")
        except Exception:
            # A caller can type before the session is running — a SIP participant
            # is in the room before `session.start` returns — and this runs from a
            # debounce task, where a raise is an unhandled exception nobody reads.
            # Losing the entry is the right outcome: there is no agent to answer
            # it yet, and the caller will type again.
            logger.warning("keypad entry %r had no running session to answer it", digits)

    async def _flush_async(self) -> None:
        self._flush()

    def close(self) -> None:
        """Stop the quiet timer. A half-typed entry at hangup is not a turn."""
        self._closed = True
        if self._quiet is not None:
            self._quiet.cancel()
        self._buffer.clear()
