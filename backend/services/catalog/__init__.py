"""Provider catalog — yaml index, public dropdowns, live voice/avatar galleries.

Import what you need from here::

    from services.catalog import Catalog, get_catalog, Pricing
    from services import catalog
    await catalog.get_public_catalog()
    await catalog.list_voices(provider="xai")

Submodules are package-internal; external callers should not import them.
"""

from __future__ import annotations

from .languages import (  # noqa: F401
    AUTO_LANGUAGE_CODES,
    TURN_DETECTOR_LANGUAGES,
    LanguageAwareEntry,
    agent_language_options,
    known_languages,
    resolve_entry_language,
    turn_detector_language_options,
)
from .loader import get_catalog  # noqa: F401
from .models import (  # noqa: F401
    AddVoiceRequest,
    AddVoiceResponse,
    AvatarCatalogEntryResponse,
    AvatarEntry,
    AvatarItemResponse,
    AvatarPricing,
    AvatarsResponse,
    BaseCatalogEntryResponse,
    BuiltinTool,
    BuiltinToolOption,
    Catalog,
    CatalogEntry,
    CatalogLanguageOption,
    CatalogResponse,
    CopilotSpec,
    CostEstimateUsagePerMinute,
    CostEstimateUsagePerTextMessage,
    ExpressiveDialect,
    LanguageOption,
    LLMCatalogEntryResponse,
    LLMEntry,
    LLMModelSearchResponse,
    LLMPricing,
    ModelHostResponse,
    ModelHostsResponse,
    ModelSearchResponse,
    NoiseCancellationCatalogEntryResponse,
    NoiseCancellationEntry,
    PlatformFeePerMessage,
    PlatformFeePerMinute,
    Pricing,
    PriorityTier,
    ProviderEntry,
    ProviderEntryResponse,
    RealtimeCatalogEntryResponse,
    RealtimeEntry,
    RealtimePricing,
    ReasoningEffort,
    SearchedLLMs,
    SearchedModels,
    SearchedSTTs,
    STTCatalogEntryResponse,
    STTEntry,
    STTModelSearchResponse,
    STTPricing,
    SystemVarResponse,
    TTSCatalogEntryResponse,
    TTSEntry,
    TTSPricing,
    VoiceItemResponse,
    VoiceOption,
    VoiceOptionResponse,
    VoiceSettingsResponse,
    VoicesResponse,
)
from .service import (  # noqa: F401
    add_elevenlabs_shared_voice,
    elevenlabs_voice_settings,
    get_public_catalog,
    list_avatars,
    list_model_hosts,
    list_voices,
    search_models,
    start_model_registry,
)
from .voices.common import VoiceKind  # noqa: F401
