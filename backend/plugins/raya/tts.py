"""Bakbak text-to-speech: one sentence per request, streamed back as SSE.

  POST https://hub.getraya.app/v1/text-to-speech/stream  → text/event-stream of
       {type: chunk, data: <base64 pcm_f32le>} … {type: done}

Raya publishes their own LiveKit plugin and we ran on it until 2026-08-13, when
it turned out to report synthesis that had *fully succeeded* as a failure. The
call that exposed it was clean end to end — the caller heard every word — and it
still logged four `tts_error` events, which surfaced in the dashboard as "4
provider errors during the call".

**The bug.** `livekit-plugins-raya` sets `sock_read=conn_options.timeout` (10s)
on the synthesis request. In aiohttp that is not a per-request deadline: it arms
a read timeout on the *connection*, which then goes back into the shared pool
still armed. After ten idle seconds — a perfectly ordinary gap while the caller
is talking — the timer expires against a socket nobody is reading. The next turn
picks that connection out of the pool and the stale timer fires immediately,
raising `APITimeoutError` against a request that had not yet left the machine.
The SDK then retries on a fresh connection and succeeds, which is why the audio
was never actually late. Reproduced deterministically: synthesize, idle 15s,
synthesize again — the second turn raises, every time.

So the fix is not a bigger timeout, it is not arming one on the connection at
all. `sock_connect` and `connect` are safe (they bound establishment, not an
idle socket); the deadline that matters — first byte, then each chunk after it —
is enforced here with `asyncio.timeout`, which lives and dies with this request
and cannot outlive it into the pool.

We keep depending on `livekit-plugins-raya` for `IndicSentenceTokenizer`: Raya's
model wants one sentence at a time, and their tokenizer knows the Devanagari
danda and the code-mixed Hinglish our agents actually speak. That part of their
plugin is good and there is no reason to rewrite it.

API reference: https://docs.litwizlabs.com/documentation/tts-overview
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from typing import Any

import aiohttp
import numpy as np
from livekit.agents import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    tokenize,
    tts,
    utils,
)
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions
from livekit.plugins.raya import IndicSentenceTokenizer

DEFAULT_BASE_URL = "https://hub.getraya.app"
API_KEY_HEADER = "X-API-Key"
_STREAM_PATH = "/v1/text-to-speech/stream"

NUM_CHANNELS = 1
# LiveKit consumes 16-bit little-endian PCM; this tells the emitter that the
# bytes we push are raw pcm_s16le rather than a container it has to parse.
_OUTPUT_MIME_TYPE = "audio/pcm"

SUPPORTED_SAMPLE_RATES = frozenset({8000, 16000, 22050, 24000})
MIN_SPEED = 0.5
MAX_SPEED = 1.5


@dataclass
class _TTSOptions:
    api_key: str
    base_url: str
    model: str
    voice_id: str
    language: str
    sample_rate: int
    speed: float
    codec: str


class _ChunkDecoder:
    """Base64 pcm_f32le off the wire, pcm_s16le into the emitter.

    A chunk boundary can land mid-sample, so the trailing partial float is held
    back and prepended to the next one — four bytes misread as a sample is a
    click in the caller's ear.
    """

    def __init__(self) -> None:
        self._remainder = b""

    def decode(self, b64_data: str) -> bytes:
        raw = self._remainder + base64.b64decode(b64_data)
        usable = len(raw) - (len(raw) % 4)
        self._remainder = raw[usable:]
        if not usable:
            return b""
        samples = np.frombuffer(raw[:usable], dtype="<f4")
        return (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


async def _aiter_sse(resp: aiohttp.ClientResponse) -> AsyncIterator[dict[str, Any]]:
    """Yield decoded JSON payloads from a ``text/event-stream`` response."""
    data_lines: list[str] = []
    async for raw_line in resp.content:
        line = raw_line.decode("utf-8").rstrip("\r\n")
        if line == "":
            if data_lines:
                payload = "\n".join(data_lines)
                data_lines = []
                try:
                    yield json.loads(payload)
                except json.JSONDecodeError as e:
                    raise APIError(f"Raya TTS sent an undecodable SSE payload: {payload}") from e
            continue
        if line.startswith(":"):
            continue  # SSE comment / keepalive
        if line.startswith("data:"):
            data_lines.append(line[len("data:") :].lstrip())
        # event:, id: and retry: are ignored — the payload carries its own type


class TTS(tts.TTS):
    """Raya Bakbak TTS. Streaming, one request per sentence."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        voice_id: str,
        language: str,
        speed: float = 1.0,
        sample_rate: int = 24000,
        codec: str = "pcm",
        base_url: str = DEFAULT_BASE_URL,
        http_session: aiohttp.ClientSession | None = None,
    ) -> None:
        """
        Args:
            api_key: Raya API key, sent as ``X-API-Key``.
            model: ``m1`` or ``standard``. Travels with `voice_id` — every Raya
                voice belongs to exactly one model and the other rejects it.
            voice_id: Voice from ``GET /v1/voices``, scoped to `model`.
            language: Required. The API 422s without one, and the code decides
                the phonetics the text is read through, not just a label on it.
            speed: 0.5–1.5 multiplier.
            sample_rate: One of 8000, 16000, 22050, 24000.
            codec: Forwarded to the API. The streaming endpoint returns raw PCM
                whatever this says; it is sent because the field is required.
            base_url: Override for the API host.
            http_session: Optional shared session; defaults to the job's.
        """
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=True, aligned_transcript=False),
            sample_rate=sample_rate,
            num_channels=NUM_CHANNELS,
        )
        if sample_rate not in SUPPORTED_SAMPLE_RATES:
            raise ValueError(
                f"sample_rate must be one of {sorted(SUPPORTED_SAMPLE_RATES)}, got {sample_rate}"
            )
        if not MIN_SPEED <= speed <= MAX_SPEED:
            raise ValueError(f"speed must be between {MIN_SPEED} and {MAX_SPEED}, got {speed}")

        self._opts = _TTSOptions(
            api_key=api_key,
            base_url=base_url.rstrip("/"),
            model=model,
            voice_id=voice_id,
            language=language,
            sample_rate=sample_rate,
            speed=speed,
            codec=codec,
        )
        self._session = http_session
        self._sentence_tokenizer: tokenize.SentenceTokenizer = IndicSentenceTokenizer()

    @property
    def provider(self) -> str:
        # Lowercase to match the catalog and plugins/raya/stt.py. The metering
        # tap canonicalizes either way, but agreeing with the catalog means it
        # never has to.
        return "raya"

    @property
    def model(self) -> str:
        return self._opts.model

    def _ensure_session(self) -> aiohttp.ClientSession:
        if not self._session:
            self._session = utils.http_context.http_session()
        return self._session

    def synthesize(
        self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> ChunkedStream:
        return ChunkedStream(tts=self, input_text=text, conn_options=conn_options)

    def stream(
        self, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> SynthesizeStream:
        return SynthesizeStream(tts=self, conn_options=conn_options)

    async def _synthesize_sentence(
        self,
        text: str,
        *,
        opts: _TTSOptions,
        conn_options: APIConnectOptions,
        output_emitter: tts.AudioEmitter,
    ) -> None:
        """Send one sentence and push its audio as it arrives."""
        body = {
            "text": text,
            "voice_id": opts.voice_id,
            "language": opts.language,
            "model": opts.model,
            "codec": opts.codec,
            "sample_rate": opts.sample_rate,
            "speed": opts.speed,
        }
        decoder = _ChunkDecoder()
        loop = asyncio.get_running_loop()

        try:
            # The deadline is scoped to this block, not to the socket: it covers
            # the wait for the first byte and is pushed forward on every chunk,
            # so a long sentence is fine and a stalled one is not. See the module
            # docstring for why `sock_read` must not be used for this.
            async with asyncio.timeout(conn_options.timeout) as deadline:
                async with self._ensure_session().post(
                    f"{opts.base_url}{_STREAM_PATH}",
                    headers={
                        API_KEY_HEADER: opts.api_key,
                        "Content-Type": "application/json",
                        "Accept": "text/event-stream",
                    },
                    json=body,
                    timeout=aiohttp.ClientTimeout(
                        total=None,
                        connect=conn_options.timeout,
                        sock_connect=conn_options.timeout,
                    ),
                ) as resp:
                    if resp.status != 200:
                        error_body = await resp.text()
                        raise APIStatusError(
                            message=f"Raya TTS returned {resp.status}: {error_body}",
                            status_code=resp.status,
                            body=error_body,
                        )

                    async for evt in _aiter_sse(resp):
                        deadline.reschedule(loop.time() + conn_options.timeout)
                        match evt.get("type"):
                            case "chunk":
                                if data := evt.get("data"):
                                    if pcm := decoder.decode(data):
                                        output_emitter.push(pcm)
                            case "done":
                                return
                            case "error":
                                raise APIError(f"Raya TTS returned an error: {evt}")

            # Falling out of the loop means the body ended without `done`. The
            # audio may well be complete, but nothing here can tell that from a
            # connection cut mid-sentence, and silently returning a truncated
            # reply is the worse of the two.
            raise APIError("Raya TTS stream ended without a `done` event")
        except TimeoutError as e:
            raise APITimeoutError("Raya TTS synthesis timed out") from e
        except (APIError, APIStatusError, APITimeoutError, APIConnectionError):
            raise
        except aiohttp.ClientError as e:
            raise APIConnectionError(f"Raya TTS connection error: {e}") from e


class ChunkedStream(tts.ChunkedStream):
    """One-shot synthesis: split the whole input, then stream it sentence by sentence."""

    def __init__(self, *, tts: TTS, input_text: str, conn_options: APIConnectOptions) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._tts: TTS = tts
        self._opts = replace(tts._opts)

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        output_emitter.initialize(
            request_id=utils.shortuuid(),
            sample_rate=self._opts.sample_rate,
            num_channels=NUM_CHANNELS,
            mime_type=_OUTPUT_MIME_TYPE,
        )

        sentences = self._tts._sentence_tokenizer.tokenize(self._input_text)
        # A input the tokenizer finds no boundary in is still something to say.
        if not sentences and self._input_text.strip():
            sentences = [self._input_text.strip()]

        for sentence in sentences:
            await self._tts._synthesize_sentence(
                sentence,
                opts=self._opts,
                conn_options=self._conn_options,
                output_emitter=output_emitter,
            )

        output_emitter.flush()


class SynthesizeStream(tts.SynthesizeStream):
    """Streaming synthesis: one segment per flush, one request per sentence."""

    def __init__(self, *, tts: TTS, conn_options: APIConnectOptions) -> None:
        super().__init__(tts=tts, conn_options=conn_options)
        self._tts: TTS = tts
        self._opts = replace(tts._opts)

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        output_emitter.initialize(
            request_id=utils.shortuuid(),
            sample_rate=self._opts.sample_rate,
            num_channels=NUM_CHANNELS,
            mime_type=_OUTPUT_MIME_TYPE,
            stream=True,
        )

        segments_ch = utils.aio.Chan[tokenize.SentenceStream]()

        async def _tokenize_input() -> None:
            sent_stream: tokenize.SentenceStream | None = None
            async for data in self._input_ch:
                if isinstance(data, str):
                    if sent_stream is None:
                        sent_stream = self._tts._sentence_tokenizer.stream()
                        segments_ch.send_nowait(sent_stream)
                    sent_stream.push_text(data)
                elif isinstance(data, self._FlushSentinel):
                    if sent_stream is not None:
                        sent_stream.end_input()
                    sent_stream = None

            if sent_stream is not None:
                sent_stream.end_input()
            segments_ch.close()

        async def _process_segments() -> None:
            async for sent_stream in segments_ch:
                output_emitter.start_segment(segment_id=utils.shortuuid())
                async for ev in sent_stream:
                    if sentence := ev.token.strip():
                        self._mark_started()
                        await self._tts._synthesize_sentence(
                            sentence,
                            opts=self._opts,
                            conn_options=self._conn_options,
                            output_emitter=output_emitter,
                        )
                output_emitter.end_segment()

        tasks = [
            asyncio.create_task(_tokenize_input()),
            asyncio.create_task(_process_segments()),
        ]
        try:
            await asyncio.gather(*tasks)
        except (APIError, APIStatusError, APITimeoutError, APIConnectionError):
            raise
        except aiohttp.ClientError as e:
            raise APIConnectionError(f"Raya TTS connection error: {e}") from e
        finally:
            await utils.aio.gracefully_cancel(*tasks)
            output_emitter.end_input()
