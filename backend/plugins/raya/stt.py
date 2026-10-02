"""Bakbak speech-to-text: one utterance in, one transcript out.

Raya offers this transcription two ways, and they are the same request twice:

  POST  https://hub.getraya.app/transcribe   multipart WAV      → {transcript, status}
  WSS   wss://hub.getraya.app/transcribe     base64 WAV as JSON → {transcript, status}

Neither is a streaming protocol. There are no interim results, no server-side
endpointing, and no utterance boundaries — the caller decides what one utterance
is and sends the whole of it. So this plugin declares `streaming=False` and the
compiler wraps it in LiveKit's `stt.StreamAdapter`, which cuts utterances with
the session's own Silero VAD and posts each one. That is the same shape the xAI
and ElevenLabs batch entries already run in.

**Why HTTP and not the websocket**, which Raya's docs call "low latency": the
socket saves a TCP+TLS handshake, and aiohttp's shared session already keeps the
HTTP connection alive across utterances, so there is no handshake left to save.
Against that, the socket carries the audio base64-encoded — a third more bytes on
the wire for the same clip — and its protocol has no request id, so two
concurrent recognitions on one connection cannot be told apart. Nothing about
`StreamAdapter` promises to serialize them. Paying 33% more bytes for a demux
hazard is the wrong trade, so the multipart endpoint carries this.
"""

from __future__ import annotations

import aiohttp
from livekit import rtc
from livekit.agents import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    stt,
    utils,
)
from livekit.agents.types import (
    DEFAULT_API_CONNECT_OPTIONS,
    NOT_GIVEN,
    APIConnectOptions,
    NotGivenOr,
)
from livekit.agents.utils import AudioBuffer, is_given

DEFAULT_BASE_URL = "https://hub.getraya.app"
_TRANSCRIBE_PATH = "/transcribe"
API_KEY_HEADER = "X-API-Key"


class STT(stt.STT):
    """Raya Bakbak STT. Batch only — see the module docstring."""

    def __init__(
        self,
        *,
        api_key: str,
        catalog_model: str,
        language: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        http_session: aiohttp.ClientSession | None = None,
    ) -> None:
        """
        Args:
            api_key: Raya API key (``raya_…``), sent as ``X-API-Key``.
            catalog_model: The catalog's model id for this entry. Raya's STT
                names no model on the wire, so metering has nothing to read off
                a response — `model` below is where the billing id comes from.
            language: One of Raya's 23 codes, or None to let Raya detect. The
                field is optional on the wire and is simply omitted when unset.
            base_url: Override for the API host.
            http_session: Optional shared session; defaults to the job's.
        """
        super().__init__(capabilities=stt.STTCapabilities(streaming=False, interim_results=False))
        self._api_key = api_key
        self._catalog_model = catalog_model
        self._language = language
        self._base_url = base_url.rstrip("/")
        self._session = http_session

    @property
    def provider(self) -> str:
        return "raya"

    @property
    def model(self) -> str:
        return self._catalog_model

    def _ensure_session(self) -> aiohttp.ClientSession:
        if not self._session:
            self._session = utils.http_context.http_session()
        return self._session

    async def _recognize_impl(
        self,
        buffer: AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.SpeechEvent:
        lang = language if is_given(language) else self._language

        form = aiohttp.FormData()
        form.add_field(
            "file",
            rtc.combine_audio_frames(buffer).to_wav_bytes(),
            filename="audio.wav",
            content_type="audio/wav",
        )
        if lang:
            form.add_field("language", lang)

        try:
            async with self._ensure_session().post(
                f"{self._base_url}{_TRANSCRIBE_PATH}",
                data=form,
                headers={API_KEY_HEADER: self._api_key},
                timeout=aiohttp.ClientTimeout(
                    total=conn_options.timeout, sock_connect=conn_options.timeout
                ),
            ) as res:
                body = await res.text()
                if res.status != 200:
                    raise APIStatusError(
                        message=f"Raya STT returned {res.status}: {body}",
                        status_code=res.status,
                        body=body,
                    )
                payload = await res.json()
                # Raya can report failure inside a 200 — `status` is an enum of
                # success|error — so the code alone does not mean there is a
                # transcript. Raised rather than returned as an empty turn: to
                # the agent, a silent drop is indistinguishable from the caller
                # saying nothing, and it would keep waiting for a reply.
                if payload.get("status") != "success":
                    raise APIStatusError(
                        message=f"Raya STT failed: {payload.get('detail') or body}",
                        status_code=res.status,
                        body=body,
                    )
        except TimeoutError as e:
            raise APITimeoutError("Raya STT request timed out") from e
        except (APIStatusError, APIConnectionError, APITimeoutError):
            raise
        except aiohttp.ClientError as e:
            raise APIConnectionError(f"Raya STT connection error: {e}") from e

        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            # Raya returns no request id; the base class only uses it for metrics
            # correlation, and an invented one would correlate with nothing.
            request_id="",
            alternatives=[
                stt.SpeechData(
                    # The response carries no detected language, so on Auto this
                    # is what the agent asked for and nothing more precise.
                    language=lang or "",
                    text=payload["transcript"],
                    # Raya publishes no confidence. 1.0 is the value every other
                    # batch plugin here uses for "the provider did not say".
                    confidence=1.0,
                )
            ],
        )
