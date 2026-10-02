"""Agent config → the provider's own server-side tools.

A built-in tool is not something Talqing runs. The provider searches, executes
or retrieves during its own turn and folds the result into the reply it streams
back, so there is no dispatcher here and nothing to meter per call — only a JSON
object to put in the request.

On the Responses API that object is always `{"type": ..., **options}`, which is
why there is no class per tool there. The catalog entry says which tools a model
has and what each option is called on the wire; the agent config says which are
on and with what values.

There IS a class per *provider*, though, and that is load-bearing. An agent with
a fallback hands one tool list to both models, and LiveKit serializes from it by
matching each tool against the running LLM's `_provider_tool_type`
(`llm/_provider_format/openai.py::to_responses_fnc_ctx`). A class per provider is
therefore what keeps one provider's tools out of the other's request — which
matters because the same `type` string is a different object at each vendor:
xAI's code interpreter takes no fields and OpenAI's requires a `container`, so
sending xAI's to OpenAI is a 400 on every turn after a failover, exactly when
the fallback was supposed to save the call.

A `native` entry breaks the "no class per tool" half of that. Gemini's tools are
typed protos rather than JSON, so each one has to be constructed by name — hence
`_GEMINI_TOOLS` below. The separation half it gets for free: the google plugin
picks its tools out of the shared list with an isinstance check of its own
(`livekit/plugins/google/utils.py::create_tools_config`), so a Gemini/OpenAI
failover pair is clean in both directions.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from copy import deepcopy
from typing import Any

from livekit.agents import llm
from livekit.plugins.google import tools as google_tools

from services.agents import LLMModelSpec
from services.catalog import BuiltinTool, BuiltinToolOption, LLMEntry, get_catalog


class TalqingProviderTool(llm.ProviderTool):
    """One server-side tool, already resolved to the object it goes out as.

    Never instantiated directly — `provider_tool_type` makes the per-provider
    subclass that `to_responses_fnc_ctx` matches on.
    """

    def __init__(self, *, provider: str, tool: BuiltinTool, config: dict[str, Any]) -> None:
        super().__init__(id=f"{provider}_{tool.type}")
        self._payload = _wire_payload(tool, config)

    def to_dict(self) -> dict[str, Any]:
        return self._payload


# One subclass per provider, made once and reused. `to_responses_fnc_ctx` does an
# isinstance check against the running LLM's `_provider_tool_type`, so these have
# to be stable identities rather than fresh classes per compile.
_PROVIDER_TOOL_TYPES: dict[str, type[TalqingProviderTool]] = {}


def provider_tool_type(provider: str) -> type[TalqingProviderTool]:
    """The ProviderTool subclass this provider's tools are instances of."""
    provider = provider.strip().lower()
    existing = _PROVIDER_TOOL_TYPES.get(provider)
    if existing is not None:
        return existing
    created = type(f"{provider.capitalize()}ProviderTool", (TalqingProviderTool,), {})
    _PROVIDER_TOOL_TYPES[provider] = created
    return created


# Gemini's tools are typed objects, so each is constructed by name rather than
# spelled out as JSON. The catalog's option keys are the constructor's own
# parameter names, which is what lets the configured values splat straight in.
_GEMINI_TOOLS: dict[str, Callable[..., google_tools.GeminiTool]] = {
    "google_search": google_tools.GoogleSearch,
    "url_context": google_tools.URLContext,
    "code_execution": google_tools.ToolCodeExecution,
}


def build_provider_tools(spec: LLMModelSpec | None) -> list[llm.Tool]:
    """The provider tools ONE model has switched on, ready to hand to `Agent`.

    Takes a single model rather than the whole spec, because the primary and the
    fallback each carry their own: the options a tool takes belong to the
    provider, not to the tool's name.

    Empty for a realtime agent (which has no LLM spec at all) and for any model
    with nothing switched on. Agent validation has already checked each one
    against this same catalog entry, so anything wrong here means the config and
    the catalog drifted apart — a raise, never a silent drop.
    """
    if spec is None or not spec.builtin_tools:
        return []
    provider = spec.provider.strip().lower()
    entry = get_catalog().require_entry("llm", provider, spec.model)
    if not isinstance(entry, LLMEntry):
        raise TypeError(f"expected LLMEntry for {provider}/{spec.model}")
    tools: list[llm.Tool] = []
    for builtin in spec.builtin_tools:
        tool = entry.builtin_tool(builtin.type)
        if tool is None:
            raise ValueError(
                f"{provider}/{spec.model} has no builtin tool '{builtin.type}' in the catalog"
            )
        if entry.api == "native":
            tools.append(_native_tool(provider, tool, builtin.config))
        else:
            cls = provider_tool_type(provider)
            tools.append(cls(provider=provider, tool=tool, config=builtin.config))
    return tools


def _native_tool(provider: str, tool: BuiltinTool, config: dict[str, Any]) -> llm.Tool:
    """The tool object a provider's own plugin takes, built by name.

    `api: native` says which client class, never which vendor, so the provider is
    matched here too — a native entry for a provider with no tool table is a
    catalog edit that was never finished, not something to serialize blindly.
    """
    if provider != "gemini":
        raise ValueError(f"provider {provider} has no native builtin tools here")
    factory = _GEMINI_TOOLS.get(tool.type)
    if factory is None:
        raise ValueError(f"gemini has no native builtin tool '{tool.type}'")
    kwargs: dict[str, Any] = {}
    for option, value in _configured(tool, config):
        if len(option.path) != 1:
            raise ValueError(
                f"builtin tool '{tool.type}' option '{option.key}' declares a nested path, but a "
                "native tool takes constructor arguments — declare the parameter name alone"
            )
        kwargs[option.path[0]] = value
    return factory(**kwargs)


def _wire_payload(tool: BuiltinTool, config: dict[str, Any]) -> dict[str, Any]:
    """The tool object the provider receives.

    Starts from whatever the catalog fixes for us, then writes each configured
    option at the path its entry declares, creating the intermediate objects on
    the way — `filters.allowed_domains` becomes
    `{"filters": {"allowed_domains": [...]}}`. Options the author left unset are
    simply absent, so the provider applies its own default rather than ours.
    """
    payload: dict[str, Any] = {"type": tool.type, **deepcopy(tool.fixed)}
    for option, value in _configured(tool, config):
        target = payload
        for segment in option.path[:-1]:
            target = target.setdefault(segment, {})
        target[option.path[-1]] = value
    return payload


def _configured(
    tool: BuiltinTool, config: dict[str, Any]
) -> Iterator[tuple[BuiltinToolOption, Any]]:
    """The options the author actually set, in the order the config lists them."""
    for key, value in config.items():
        option = tool.option(key)
        if option is None:
            raise ValueError(f"builtin tool '{tool.type}' has no option '{key}'")
        if _is_unset(option, value):
            continue
        yield option, value


def _is_unset(option: BuiltinToolOption, value: Any) -> bool:
    """Whether the editor sent this option as "not configured".

    A cleared text box arrives as `""` and a cleared list as `[]`; both mean the
    author wants the provider's default, not an empty filter that would match
    nothing. `False` is a real value for a switch and stays.
    """
    if value is None:
        return True
    if option.type in ("string", "string_list") and not value:
        return True
    return False
