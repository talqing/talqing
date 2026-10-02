"""Validation for the agent's single language setting.

The agent carries one ``language``; ``services.catalog.resolve_entry_language``
translates it into each provider's own code. This module checks that the
translation actually lands for every model the agent will run, so a user who
picks Tamil never discovers at call time that their voice model doesn't speak it.
"""

from __future__ import annotations

from services.catalog import (
    LanguageAwareEntry,
    get_catalog,
    known_languages,
    resolve_entry_language,
)

from .models import AgentConfig

_KIND_LABELS = {"stt": "speech-to-text", "tts": "text-to-speech", "realtime": "realtime"}


def language_name(code: str | None) -> str | None:
    """The display name for an agent language code, or None if unknown.

    Checked against every code the catalog publishes, not just the ones the
    picker offers — `hi-IN` and `hi` are both valid ways to ask for Hindi.
    """
    if not code:
        return None
    option = known_languages(get_catalog()).get(code.strip().lower())
    return option.name if option else None


def validate_agent_language(config: AgentConfig) -> list[str]:
    """Errors describing where the agent's language does not land.

    Three ways it can fail, and nothing is inferred beyond them:

    - the code is not one the catalog publishes at all;
    - a model that does speak specific languages cannot speak this one;
    - a model with no auto-detect mode is asked to run on Auto.

    A model that publishes no languages (OpenAI TTS is multilingual, Deepgram
    encodes it in the voice) is left alone — the language genuinely does not
    apply to it, and the dashboard says so beside the picker.
    """
    errors: list[str] = []
    catalog = get_catalog()

    name = language_name(config.language)
    if config.language and name is None:
        errors.append(
            f"unknown language '{config.language}' - pick one from the agent's language list"
        )
        return errors

    specs = [("stt", config.stt), ("tts", config.tts), ("realtime", config.realtime)]
    specs += [(kind, spec.fallback) for kind, spec in specs[:2] if spec is not None]

    for kind, spec in specs:
        if spec is None:
            continue
        entry = catalog.entry(kind, spec.provider, spec.model)
        # An unknown model is already reported by the caller's catalog check;
        # a second error about its languages would just be noise.
        if not isinstance(entry, LanguageAwareEntry) or not entry.languages:
            continue
        model = f"{spec.provider}/{spec.model}"
        label = _KIND_LABELS[kind]
        if config.language is None:
            if entry.language_required:
                errors.append(
                    f"{label} model '{model}' does not detect languages on its own - "
                    "set the agent's language instead of leaving it on Auto"
                )
        elif resolve_entry_language(config.language, entry) is None:
            errors.append(f"{label} model '{model}' does not support {name}")

    return errors
