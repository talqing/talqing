"""The provider catalog, rendered for a CoPilot that picks models.

Only AgentCoPilot needs this — a tool has no models to choose.
"""

from __future__ import annotations

from services.catalog import get_catalog


def _channels(entry) -> str:
    return ", ".join(entry.channel) if entry.channel else "all channels"


def _pricing(entry) -> str | None:
    """One entry's rates, in units a person would quote.

    The catalog stores speech rates per audio-second and per character, which are
    exact but unreadable at a glance — nothing chooses between two STT models by
    comparing 5.56e-05 to 3.33e-05. These are scaled to the hour and the thousand
    characters, which is how the providers price them and how a user will ask.

    Rates are the one thing an author asks about that the rest of this block
    could not answer, which is why they are here rather than a lookup away.
    """
    pricing = getattr(entry, "pricing", None)
    if pricing is None:
        return None
    p = pricing.model_dump()
    parts: list[str] = []
    if (sec := p.get("per_audio_second")) is not None:
        parts.append(f"${sec * 3600:.2f}/audio-hour")
    if (char := p.get("per_character")) is not None:
        parts.append(f"${char * 1000:.4f}/1k chars")
    if (minute := p.get("per_session_minute")) is not None:
        parts.append(f"${minute:g}/session-minute")
    # Everything else is already per 1M tokens; the key names the stream.
    for key, value in p.items():
        if value is None or key in ("per_audio_second", "per_character", "per_session_minute"):
            continue
        parts.append(f"${value:g}/1M {key.removesuffix('_per_1m').replace('_', ' ')}")
    return ", ".join(parts) if parts else None


def _voice_source(entry) -> str | None:
    if not hasattr(entry, "voices"):
        return None
    if entry.voices:
        return f"static voices in catalog_voices ({len(entry.voices)} total)"
    return "voices from catalog_voices live catalog"


def _catalog_block() -> str:
    c = get_catalog()
    lines = []
    for kind in ("llm", "stt", "tts", "realtime", "avatar"):
        entries = getattr(c, kind)
        if not entries:
            continue
        lines.append(f"{kind.upper()}:")
        for e in entries:
            bits = [f"{e.provider}/{e.model}", f"channels {_channels(e)}"]
            if e.label:
                bits.append(f'label "{e.label}"')

            if kind in ("stt", "tts", "realtime"):
                # config.language is agent-level; these are the codes this entry
                # can be asked to speak, which is what decides whether a chosen
                # language survives validation.
                if e.languages:
                    # Codes only. `config.language` takes the code, never the
                    # name, so spelling out "en=English" once per entry was most
                    # of this block and bought nothing.
                    bits.append("language codes " + " ".join(x.code for x in e.languages))
                    if getattr(e, "language_required", False):
                        bits.append("cannot auto-detect; config.language must be set")
                else:
                    bits.append("no language setting; ignores config.language")

            if kind == "stt" and not e.streaming:
                # Two rules turn on this flag and neither is inferable from the
                # model id: a batch model can only fail over to another batch
                # model, and it is the only pipeline the end-of-turn model runs on.
                bits.append(
                    "batch: one request per turn; fails over only to another batch model; "
                    "gets LiveKit's end-of-turn model when config.language is one it knows"
                )

            if kind == "tts":
                if e.default_voice:
                    bits.append(f"default voice {e.default_voice}")
                source = _voice_source(e)
                if source:
                    bits.append(source)
                if e.supports_speed:
                    bits.append(f"speed {e.speed_min:g}-{e.speed_max:g}")
                else:
                    bits.append("speed not supported; keep 1.0")
                # Stated per entry because it decides whether the voice's own
                # settings must be read and carried — an agent set up without
                # them sounds wrong rather than failing, so the CoPilot has to
                # know before it picks.
                if e.supports_voice_settings:
                    bits.append(
                        "carry the voice's own settings: elevenlabs_voice_settings "
                        "-> tts.stability / tts.similarity_boost"
                    )
            elif kind == "realtime":
                bits.append("replaces stt+llm+tts; set config.realtime and clear all three")
                if e.default_voice:
                    bits.append(f"default voice {e.default_voice}")
                source = _voice_source(e)
                if source:
                    bits.append(f"{source} with kind=realtime")
                if e.supports_speed:
                    bits.append(f"speed {e.speed_min:g}-{e.speed_max:g}")
                else:
                    bits.append("speed not supported; keep 1.0")

            # Last, and verbatim: a note is the one thing on an entry that asks
            # the author to write the prompt differently, so it has to survive
            # into the prompt rather than be summarized away.
            if price := _pricing(e):
                bits.append(price)

            if e.note:
                bits.append(f"NOTE: {' '.join(e.note.split())}")
            lines.append("  - " + " — ".join(bits))
    return "\n".join(lines)


def provider_prompt() -> str:
    return (
        "PROVIDER CATALOG (the only valid models — pick by provider/model)\n"
        f"{_catalog_block()}\n"
        "Use Sarvam or Raya STT/TTS for Indian languages — Raya is far cheaper per character, "
        "Sarvam covers more of them and needs no script discipline in the prompt. "
        "Soniox is the cheapest streaming STT here and covers 60 languages, so it is the "
        "default worth reaching for outside India unless the user asks for something else. "
        "An entry's NOTE is a caveat the agent's prompt has to answer: act on it and say so. "
        "The workspace's configured provider keys "
        "are in the state block below as providers_with_api_key; prefer models from those, "
        "and if the user wants one that is not listed, tell them to add that key on the "
        "BYOK page — publishing will fail without it."
    )
