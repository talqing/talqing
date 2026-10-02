"""Agents service — public surface for config, language, media, validation, CRUD.

Import what you need from here::

    from services.agents import AgentConfig, create_agent, language_name

Submodules are package-internal; external callers should not import them.
"""

from __future__ import annotations

from .language import language_name, validate_agent_language  # noqa: F401
from .media import (  # noqa: F401
    handoff_media_error,
    media_signature,
    recording_handoff_warning,
)
from .models import (  # noqa: F401
    DEFAULT_MAX_STEPS,
    DEFAULT_SUMMARY_PROMPT,
    DEFAULT_SUMMARY_RECENT_TURNS,
    FINISH_WITHOUT_RESULT_TOOL,
    MAX_CALL_DURATION_SECONDS,
    SUBMIT_RESULT_TOOL,
    AgentBase,
    AgentConfig,
    AgentResponse,
    AgentVersionDetailResponse,
    AgentVersionResponse,
    AvatarSpec,
    BackgroundAudioSpec,
    BuiltinToolSpec,
    ConversationContext,
    ConversationSpec,
    CreateAgentRequest,
    EndpointingSpec,
    FaqSelection,
    HandoffTarget,
    InlineMcpServer,
    InlineTool,
    InterruptionSpec,
    KeypadInputSpec,
    LLMModelSpec,
    LLMSpec,
    McpSelection,
    NoiseCancellationSpec,
    PinnedTask,
    PreemptiveGenerationSpec,
    PublishAgentResponse,
    RealtimeSpec,
    RecordingSpec,
    SilenceSpec,
    STTModelSpec,
    STTSpec,
    TaskConfig,
    TaskOutputField,
    TaskSelection,
    ToolSelection,
    TTSModelSpec,
    TTSSpec,
    TurnHandlingSpec,
    VarDeclaration,
    VisionInputSpec,
    VisionSourceSpec,
    VoicemailDetectionSpec,
    handoff_tool_name,
    missing_required_vars,
)
from .override import AgentOverride, UpdateAgentRequest  # noqa: F401
from .service import (  # noqa: F401
    create_agent,
    delete_agent,
    get_agent,
    get_agent_version,
    list_agents,
    publish_agent,
    rollback_agent_version,
    update_agent,
    validate_agent,
)
from .validate import (  # noqa: F401
    CONSENT_TOKEN,
    task_handoff_errors,
    validate_agent_config,
    validate_agent_draft,
    validate_base_draft,
    validate_session_config,
)
