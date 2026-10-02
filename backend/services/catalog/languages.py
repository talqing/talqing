"""One language per agent, resolved to each provider's own code.

An agent carries a single ``AgentConfig.language``. Every STT/TTS/realtime entry
in the catalog declares languages in *its provider's* spelling — Sarvam says
``hi-IN``, xAI says ``hi``, Deepgram ships both ``zh`` and ``zh-CN``. This module
owns the mapping between the two, so the dashboard, the validator and the
compiler all agree on what "Hindi" means for a given model.

``None`` is a real answer: it means "this service auto-detects", which is what
the agent's Auto setting compiles to and what an entry that publishes no
languages at all (OpenAI TTS, Deepgram TTS) always gets.
"""

from __future__ import annotations

from livekit.agents.inference.eot.languages import LOCAL_LANGUAGES

from .models import Catalog, LanguageOption, RealtimeEntry, STTEntry, TTSEntry

# Codes that mean "no specific language". Providers spell it differently —
# xAI TTS declares `auto`, Deepgram declares `multi` — and both are the entry's
# own way of saying what `None` says here, so they never become agent languages.
AUTO_LANGUAGE_CODES = frozenset({"auto", "unknown", "multi", "multilingual"})

# The catalog kinds that carry a language. LLMs don't (they follow the prompt)
# and avatars don't (Anam renders whatever audio it is handed).
LanguageAwareEntry = STTEntry | TTSEntry | RealtimeEntry

# The languages LiveKit's local audio end-of-turn model is calibrated for. Read
# from the framework rather than copied, so a version bump that changes the set
# is picked up — and one that moves the module fails at import instead of
# leaving us offering a mode for languages the model cannot judge.
TURN_DETECTOR_LANGUAGES = frozenset(LOCAL_LANGUAGES)


def _clean(value: str | None) -> str | None:
    """Lowercased code, or None when it is empty or an auto sentinel."""
    code = (value or "").strip().lower()
    return None if not code or code in AUTO_LANGUAGE_CODES else code


# Primary subtags that are one language under two spellings, mapped onto the
# ISO 639-1 code. Only for a provider that is demonstrably the odd one out —
# Sarvam calls Odia `od-IN` where the standard (and Raya, and ISO) says `or`, so
# without this the picker's "Odia" reaches exactly one of the two providers and
# the other reports that it does not speak the language. Not a general synonym
# table: every entry here is a vendor departing from the standard, and the pair
# has to be checked before it is added.
_PRIMARY_ALIASES = {"od": "or"}


def _primary(code: str) -> str:
    """The primary subtag: `pt-BR` → `pt`, `hi` → `hi`, `od-IN` → `or`."""
    primary = code.split("-", 1)[0]
    return _PRIMARY_ALIASES.get(primary, primary)


def known_languages(catalog: Catalog) -> dict[str, LanguageOption]:
    """Every language code any provider publishes, keyed by the lowercased code.

    The set an agent language is validated against. Wider than the picker below:
    ``hi-IN`` is a perfectly good way to ask for Hindi even though the picker
    offers ``hi``, and a config written by CoPilot or the MCP should not be
    refused for spelling it the way Sarvam does.

    Two entries that use the same code must label it the same way, or the
    dropdown would show one language twice under two names. That is a
    catalog.yaml bug, so it raises at load rather than picking a winner.
    """
    # keyed by the lowercased code so `pt-BR` and `pt-br` are one language;
    # the value keeps the catalog's own casing, which is what users see.
    by_code: dict[str, LanguageOption] = {}
    entries: list[LanguageAwareEntry] = [*catalog.stt, *catalog.tts, *catalog.realtime]
    for entry in entries:
        for option in entry.languages:
            code = _clean(option.code)
            if code is None:
                continue
            name = option.name.strip()
            claimed = by_code.setdefault(code, LanguageOption(code=option.code.strip(), name=name))
            if claimed.name != name:
                raise ValueError(
                    f"catalog language {code!r} is labelled both {claimed.name!r} and {name!r} "
                    f"(seen on {entry.provider}/{entry.model}) — pick one spelling"
                )
    return by_code


def agent_language_options(catalog: Catalog) -> list[LanguageOption]:
    """The languages to offer in the dashboard picker.

    The union across providers rather than an intersection: the user picks a
    language for the agent, then sees per-service what it resolved to.
    Restricting the list to the currently-selected models would silently
    invalidate the language every time a model changes.

    Two codes with one name — Sarvam's ``hi-IN`` beside Deepgram's ``hi`` — are
    a spelling difference, not two languages, and they are interchangeable here:
    ``resolve_entry_language`` widens and narrows between them, so either reaches
    both providers. Only the more general code is offered, because a picker
    listing "Hindi" twice is a worse answer than picking for the user.
    """
    by_name: dict[str, LanguageOption] = {}
    for option in known_languages(catalog).values():
        held = by_name.get(option.name)
        # shortest code wins, then alphabetical so the choice never depends on
        # the order entries happen to appear in catalog.yaml
        if held is None or (len(option.code), option.code) < (len(held.code), held.code):
            by_name[option.name] = option
    return sorted(by_name.values(), key=lambda option: option.name)


def turn_detector_language_options(catalog: Catalog) -> list[LanguageOption]:
    """The agent languages LiveKit's turn detector can judge, named.

    The model keys off the primary subtag, so every regional variant of a
    covered language qualifies — `pt-BR` counts because `pt` does.
    """
    return [
        option
        for option in agent_language_options(catalog)
        if _primary(option.code.lower()) in TURN_DETECTOR_LANGUAGES
    ]


def resolve_entry_language(agent_language: str | None, entry: LanguageAwareEntry) -> str | None:
    """The provider's code for the agent's language, or None to auto-detect.

    Three ordered rules, and nothing beyond them:

    1. exact — the entry publishes that code.
    2. widen — the agent asked for `en-GB` and the entry only has bare `en`.
    3. narrow — the agent asked for `hi` and the entry has exactly one variant
       of it (Sarvam's `hi-IN`). When several variants share the primary
       (xAI TTS has `pt-BR` and `pt-PT`) the entry's own default breaks the tie
       if it fits; otherwise this returns None rather than guessing which
       regional accent the caller wanted.
    """
    wanted = _clean(agent_language)
    if wanted is None:
        return None

    # lowercased code → the entry's own spelling, which is what the plugin wants
    codes = {code: option.code for option in entry.languages if (code := _clean(option.code))}
    if wanted in codes:
        return codes[wanted]

    primary = _primary(wanted)
    if primary in codes:
        return codes[primary]

    variants = [spelling for code, spelling in codes.items() if _primary(code) == primary]
    if len(variants) == 1:
        return variants[0]

    default = _clean(entry.default_language)
    if default is not None and default in codes and _primary(default) == primary:
        return codes[default]
    return None
