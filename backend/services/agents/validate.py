"""Deterministic validation for agent drafts.

Tool-tree validation lives in ``services.tools.validate``. This module validates
agent config shape and attached resources (tools, tasks, integrations).
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

import httpx

from services import analysis
from services.byok import list_configured_providers
from services.catalog import (
    BuiltinToolOption,
    LLMEntry,
    RealtimeEntry,
    STTEntry,
    TTSEntry,
    get_catalog,
    openrouter,
)
from services.integrations import (
    TOOL_PROVIDER_SET,
    validate_integration_definition,
)
from services.integrations.models import INTEGRATION_COLUMNS, Integration
from services.secrets import list_secret_names
from services.system_vars import (
    CALL_KEYS,
    CLOCK_KEYS,
    SYSTEM_KEY_LIST,
    SYSTEM_KEYS,
    SYSTEM_VARS_ROOT,
    VARS_ROOT,
    declared_vars_clause,
)
from services.tools import (
    RESERVED_TOOL_NAMES,
    ValidationResult,
    check_url,
    operation_from_request,
    template_refs,
    tree_kinds,
    unknown_template_tokens,
    validate_operation_tree,
    validate_tool_definition,
)
from services.user import Context

from .language import validate_agent_language
from .media import handoff_media_error, recording_handoff_warning
from .models import (
    AgentBase,
    AgentConfig,
    HandoffTarget,
    TaskConfig,
    TaskSelection,
    handoff_tool_name,
)


def _models(kind: str) -> str:
    cat = get_catalog()
    return ", ".join(f"{e.provider}/{e.model}" for e in getattr(cat, kind))


def _unknown_model(kind: str, provider: str, model: str, noun: str = "") -> str:
    """Why this model was refused, in terms the author can act on.

    A `browse: "search"` provider's models are not in the lists `_models` reads,
    so naming those would send someone looking for an OpenRouter slug among
    sixteen OpenAI and xAI ones. It also has a second cause the others do not:
    the model may be perfectly real and this process's registry still cold or
    stale, which is a wait rather than a typo.
    """
    label = f"unknown {noun or kind.upper()} model '{provider}/{model}'"
    meta = get_catalog().providers.get(provider.strip().lower())
    if meta is not None and meta.searched(kind) is not None:
        return (
            f"{label} - {meta.label} does not currently list it. Search the model "
            "picker for a slug it does."
        )
    return label + (f" - pick from: {_models(kind)}" if _models(kind) else "")


def _is_batch_stt(cat: Any, spec: Any) -> bool:
    """True only for a known batch model — an unknown one is already an error."""
    entry = cat.entry("stt", spec.provider, spec.model)
    return isinstance(entry, STTEntry) and not entry.streaming


CONSENT_TOKEN = "{{consent.notice}}"


def _validate_recording(config: AgentConfig) -> list[str]:
    """Check that a disclosing agent actually discloses.

    Recording is on by default and the caller never hears about it unless the
    author asks for a notice — so once they do, the notice has to have somewhere
    to be spoken. Every rule here fails at publish rather than mid-call, where
    the failure would be a caller who was not told.

    The greeting is the notice's home on both ways in. An agent reached by a
    handoff speaks its greeting like any other, and the token in it resolves to
    the sentence exactly when that leg owes one — recording live under this agent,
    and the agent handing over not already disclosing
    (`compiler.compile.CompiledAgent._follow_recording_policy` settles that, and
    is the only thing that can). Which is why the token is required here and not
    merely suggested: a version published without one has nowhere to put the
    sentence, and the runtime falls back to reading it out on its own.
    """
    if config.recording.consent != "disclosure":
        return []

    errors: list[str] = []
    if not config.recording.consent_notice.strip():
        errors.append("recording consent is on but the consent notice is empty")
    if config.realtime is not None:
        # A realtime agent has no TTS to hand exact words to, so `compiler.speech`
        # can only *ask* the model to say the greeting — it may reword or trim it
        # (see the module docstring there). That is fine for an opening line and
        # not fine for a disclosure, which is the one sentence that has to be
        # spoken as written. Same reason `{{consent.notice}}` is banned from the
        # prompt.
        errors.append(
            "recording consent needs a cascade pipeline (STT + LLM + TTS): a realtime "
            "model rephrases what it is asked to say, and a recording notice has to be "
            "spoken word for word"
        )
    if not config.greeting:
        errors.append(
            "recording consent needs a greeting to speak the notice in - "
            f"add a greeting containing {CONSENT_TOKEN}"
        )
    elif CONSENT_TOKEN not in config.greeting:
        errors.append(
            f"recording consent is on, so the greeting must contain {CONSENT_TOKEN} "
            "to say where the notice is spoken"
        )
    return errors


def _validate_noise_cancellation(config: AgentConfig) -> list[str]:
    """Check the enhancement model exists and runs on this agent's channel.

    Only while it is enabled: the model and strength stay stored when the toggle
    is off, so an agent that once used a model we have since retired must still
    be publishable with it turned off.
    """
    spec = config.noise_cancellation
    if not spec.enabled:
        return []

    entry = get_catalog().entry("noise_cancellation", spec.provider, spec.model)
    if not entry:
        return [
            f"unknown noise cancellation model '{spec.provider}/{spec.model}'"
            + (
                f" - pick from: {_models('noise_cancellation')}"
                if _models("noise_cancellation")
                else ""
            )
        ]
    if entry.channel and config.channel not in entry.channel:
        return [
            f"noise cancellation model '{spec.provider}/{spec.model}' does not support "
            f"channel '{config.channel}'"
        ]
    return []


def _validate_image_input(config: AgentConfig) -> list[str]:
    """Warn when this agent's model cannot read an image people may attach.

    Warnings, never errors: the agent is perfectly valid, it just cannot do this
    one thing, and there is no setting to turn off (image input is a model
    capability, not an agent preference).

    Two cases, because they surprise in different ways. A primary that cannot
    see is discovered by a caller trying to attach something. A *fallback* that
    cannot see is worse: the agent reads images until the day the primary fails
    over, and then quietly stops mid-conversation.
    """
    cat = get_catalog()
    spec = config.realtime if config.realtime is not None else config.llm
    kind = "realtime" if config.realtime is not None else "llm"
    if spec is None:
        return []
    entry = cat.entry(kind, spec.provider, spec.model)
    if entry is None:
        return []  # `supports` already reported the unknown model
    warnings: list[str] = []
    if not getattr(entry, "vision", False):
        warnings.append(
            f"'{spec.provider}/{spec.model}' cannot read images, so nobody on this agent can "
            "attach one - pick a model that can if that matters"
        )
        return warnings
    fallback = getattr(spec, "fallback", None)
    if fallback is not None:
        fallback_entry = cat.entry(kind, fallback.provider, fallback.model)
        if fallback_entry is not None and not getattr(fallback_entry, "vision", False):
            warnings.append(
                f"'{spec.provider}/{spec.model}' reads images but the fallback "
                f"'{fallback.provider}/{fallback.model}' cannot - a failover would stop the "
                "agent seeing images mid-conversation, including ones already sent"
            )
    return warnings


def _validate_vision_input(config: AgentConfig) -> list[str]:
    """Check the agent's model can actually see the screen it is being shown.

    Errors rather than warnings, which is where this parts company with
    `_validate_image_input` above. That one is about an image a caller *may*
    attach to an agent that has no setting for it; here the author turned a
    feature on, and an agent that cannot see should not publish with it. Checked
    only while it is enabled, and the fallback counts for the same reason it does
    there: a failover mid-interview would stop the agent seeing the screen it has
    been discussing.

    `normalize_channel_media` has already refused realtime and cleared this on
    text, so anything reaching here is a voice or video cascade.
    """
    if not config.vision_input.screenshare.enabled:
        return []
    spec = config.llm
    if spec is None:
        return []
    cat = get_catalog()
    entry = cat.entry("llm", spec.provider, spec.model)
    if entry is None:
        return []  # `supports` already reported the unknown model
    errors: list[str] = []
    if not getattr(entry, "vision", False):
        errors.append(
            f"'{spec.provider}/{spec.model}' cannot read images, so it cannot see a screen "
            "share - pick a model that can, or turn screen share off"
        )
        return errors
    fallback = spec.fallback
    if fallback is not None:
        fallback_entry = cat.entry("llm", fallback.provider, fallback.model)
        if fallback_entry is not None and not getattr(fallback_entry, "vision", False):
            errors.append(
                f"the LLM fallback '{fallback.provider}/{fallback.model}' cannot read images, "
                "so a failover would leave the agent blind to the screen mid-call - pick a "
                "fallback that can, or turn screen share off"
            )
    return errors


def _validate_keypad_input(config: AgentConfig) -> list[str]:
    """Refuse a keypad buffer that can never flush.

    `timeout == 0` means "wait for the terminator", and an empty terminator means
    "only the quiet timer ends an entry". Together they are a buffer with no exit:
    every digit a caller types disappears for ever, and the agent hears nothing at
    all. This is exactly the class of misconfiguration that is invisible until a
    real caller hits it, so it is refused at publish rather than warned about.
    """
    keypad = config.keypad_input
    if keypad.enabled and keypad.timeout == 0 and keypad.terminator == "":
        return [
            "keypad input has no way to finish an entry - with the quiet timeout at 0 and no "
            "terminator key, nothing a caller types would ever reach the agent. Set a timeout, "
            "or pick # or * as the terminator"
        ]
    return []


def _validate_analysis(config: AgentConfig) -> list[str]:
    """Check the post-call analysis spec names a model and asks for something.

    Field names, duplicates and reserved names are enforced by ``AnalysisSpec``
    itself — they are structural, so a config in that state should not parse at
    all. What is left here is the pair of states an author can legitimately be
    in mid-edit, which belong in the errors list rather than in a 422.
    """
    spec = config.analysis
    if not spec.enabled:
        return []

    errors: list[str] = []
    if spec.produces_nothing():
        errors.append(
            "call analysis is on but nothing is switched on to produce - turn on the "
            "summary, define what a successful call is, or add a field to extract"
        )
    if spec.model is not None and not get_catalog().entry(
        "llm", spec.model.provider, spec.model.model
    ):
        errors.append(_unknown_model("llm", spec.model.provider, spec.model.model, "analysis"))
    elif analysis.resolve_model(config) is None:
        # No named model and nothing to inherit one from. Caught here so it is a
        # save-time error rather than a call that quietly analyses nothing.
        errors.append(
            "call analysis has no model to run on - this agent has no language model to "
            "inherit one from, so pick one for analysis or turn analysis off"
        )
    # No channel check, unlike the pipeline slots: analysis reads a finished
    # transcript, so no model is unsuitable for the channel it ran on.
    return errors


def _validate_conversation(config: AgentConfig) -> list[str]:
    """Check that 'summary' has something to summarize with.

    The other two modes need nothing: 'none' points the call at a brand-new
    conversation and 'transcript' replays what is already stored. 'summary'
    reads `sessions.summary`, which only post-call analysis writes — so an agent
    asking for it with analysis summaries switched off would start every call
    clean and look broken.

    The agent editor turns analysis and its summary on when the author picks
    this mode, so in practice only configs arriving through the API, the MCP
    surface or a CoPilot reach here. It stays a save-time error rather than an
    invariant the worker may assume: analysis can be switched off again after
    publishing, and the run-time loader treats "no summaries" as an ordinary
    empty result.
    """
    if config.conversation.context != "summary":
        return []
    if config.analysis.enabled and config.analysis.summary:
        return []
    return [
        "'include summaries of past conversations' needs call analysis to be writing "
        "summaries - turn on analysis and its summary, or choose a different conversation "
        "context"
    ]


def _option_value_error(option: BuiltinToolOption, value: Any, label: str) -> str | None:
    """Whether a configured value fits the option its catalog entry declares.

    Checked here rather than at the wire so a bad value is a save-time field
    error instead of a provider 400 on someone's first live turn.
    """
    where = f"{label} option '{option.key}'"
    if option.type == "boolean":
        return None if isinstance(value, bool) else f"{where} must be true or false"
    if option.type == "integer":
        # bool is an int in Python, and "web search: true results" is not a count.
        if isinstance(value, bool) or not isinstance(value, int):
            return f"{where} must be a whole number"
        return None
    if option.type == "string":
        return None if isinstance(value, str) else f"{where} must be text"
    if option.type == "enum":
        if value not in option.choices:
            return f"{where} must be one of: {', '.join(option.choices)}"
        return None
    # string_list
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return f"{where} must be a list of text values"
    if option.max_items is not None and len(value) > option.max_items:
        return f"{where} takes at most {option.max_items} values"
    return None


def _validate_builtin_tools(config: AgentBase) -> list[str]:
    """Provider built-in tools, each checked against the model that would run it.

    The primary and the failover are validated separately and independently.
    That is not a shortcut — the same tool name is a different object at each
    vendor (xAI's code interpreter takes no fields, OpenAI's requires a
    container), so a model can only be judged against its own catalog entry, and
    there is nothing to say about the pair. A failover that offers fewer tools is
    allowed on purpose: losing web search for the rest of a call is a far better
    outcome than losing the call, and failover matters more than the tool does.
    """
    spec = config.llm
    if spec is None:
        return []
    errors = _model_builtin_tool_errors(spec, "")
    if spec.fallback:
        errors += _model_builtin_tool_errors(spec.fallback, "failover ")
    return errors


def _reasoning_effort_errors(specs: Sequence[tuple[Any, str]]) -> list[str]:
    """Every model that names a thinking effort must be offered that one.

    The accepted values are per model and genuinely irregular — "minimal" is the
    floor on xAI and Gemini and a 400 on every gpt-5.6 — so each spec is checked
    against its own catalog entry, primary, failover and analysis model alike.

    Takes the specs rather than a config: the language model belongs to the agent
    (and to a task), the analysis model to the session it owns, and the two are
    checked from different places.
    """
    errors: list[str] = []
    for spec, role in specs:
        if spec is None or spec.reasoning_effort is None:
            continue
        entry = get_catalog().entry("llm", spec.provider, spec.model)
        if not isinstance(entry, LLMEntry):
            continue  # the unknown-model error already says this
        if not entry.reasoning_efforts:
            errors.append(
                f"the {role}model '{spec.provider}/{spec.model}' has no thinking setting - "
                "leave the reasoning effort unset"
            )
        elif spec.reasoning_effort not in entry.reasoning_efforts:
            errors.append(
                f"the {role}model '{spec.provider}/{spec.model}' does not support thinking "
                f"effort '{spec.reasoning_effort}' - it offers: "
                f"{', '.join(entry.reasoning_efforts)}"
            )
    return errors


def _priority_tier_errors(specs: Sequence[tuple[Any, str]]) -> list[str]:
    """Only models that sell a priority lane may be asked to run in one.

    Support is per model rather than per provider — OpenAI sells one on eight of
    our nine entries and none on gpt-5.4-nano — so, as with thinking effort, each
    spec is checked against its own catalog entry. Takes the specs for the same
    reason `_reasoning_effort_errors` does.
    """
    errors: list[str] = []
    for spec, role in specs:
        if spec is None or not spec.priority:
            continue
        if role == "call analysis ":
            # Analysis runs once the caller has already hung up. The lane buys
            # nothing there and would quietly double that spend — the same
            # judgement `analysis/run.py` makes in refusing to inherit the live
            # model's reasoning effort.
            errors.append(
                "the call analysis model cannot use the priority lane - analysis runs "
                "after the call has ended, so it would cost more and save nobody any wait"
            )
            continue
        entry = get_catalog().entry("llm", spec.provider, spec.model)
        if not isinstance(entry, LLMEntry):
            continue  # the unknown-model error already says this
        if entry.priority is None:
            errors.append(
                f"the {role}model '{spec.provider}/{spec.model}' has no priority lane - "
                "turn it off, or pick a model that offers one"
            )
    return errors


def _searches_llms(provider: str) -> bool:
    meta = get_catalog().providers.get(provider.strip().lower())
    return meta is not None and meta.searched("llm") is not None


def _host_errors(specs: Sequence[tuple[Any, str]]) -> list[str]:
    """The shape of each model spec's OpenRouter host set.

    Empty is refused rather than read as Automatic, so each meaning has one
    spelling (null), the rule integration `allowed_tools` follows. Whether the
    hosts still serve the model is a publish-grade check (`_host_availability`).
    """
    errors: list[str] = []
    for spec, path in specs:
        if spec is None or spec.hosts is None:
            continue
        if not _searches_llms(spec.provider):
            errors.append(f"`{path}` is only for OpenRouter models - leave it unset")
        elif not spec.hosts:
            errors.append(f"`{path}` cannot be empty - leave it unset to allow any host")
        elif duplicates := sorted(h for h, n in Counter(spec.hosts).items() if n > 1):
            errors.append(f"`{path}` lists {', '.join(duplicates)} more than once")
    return errors


async def _host_availability(specs: Sequence[tuple[Any, str]]) -> ValidationResult:
    """Each host set against the hosts that serve its model today.

    Mirrors how OpenRouter treats the set: a host that stopped serving is
    skipped at request time (measured), so it is only a warning while another
    one remains, and an error once none does. Only specs that set hosts touch the
    network, so an agent without any never waits on OpenRouter to publish. When
    OpenRouter cannot be reached the publish is refused rather than the check
    skipped — the agent could not run then anyway.
    """
    out = ValidationResult()
    for spec, path in specs:
        if spec is None or not spec.hosts or not _searches_llms(spec.provider):
            continue
        if openrouter.entry("llm", spec.model) is None:
            continue  # the unknown-model error already says this
        try:
            served = {h.host for h in await openrouter.list_hosts(spec.model, None)}
        except httpx.HTTPError as exc:
            out.errors.append(
                f"could not check `{path}` because OpenRouter did not answer ({exc}) - "
                "try publishing again"
            )
            continue
        gone = [h for h in spec.hosts if h not in served]
        if len(gone) == len(spec.hosts):
            out.errors.append(
                f"None of the hosts chosen for `{spec.model}` serves it on OpenRouter any "
                f"more (`{path}`). Pick others, or Automatic."
            )
        else:
            out.warnings.extend(
                f"`{host}` no longer serves `{spec.model}`, so it's skipped (`{path}`)."
                for host in gone
            )
    return out


def _model_builtin_tool_errors(spec: Any, role: str) -> list[str]:
    """One model's built-in tools against its own catalog entry."""
    if not spec.builtin_tools:
        return []
    entry = get_catalog().entry("llm", spec.provider, spec.model)
    if not isinstance(entry, LLMEntry):
        return []  # the unknown-model error already says this

    errors: list[str] = []
    seen: set[str] = set()

    for builtin in spec.builtin_tools:
        tool = entry.builtin_tool(builtin.type)
        if tool is None:
            offered = ", ".join(t.type for t in entry.builtin_tools) or "none"
            errors.append(
                f"the {role}model '{spec.provider}/{spec.model}' has no built-in tool "
                f"'{builtin.type}' - it offers: {offered}"
            )
            continue
        if builtin.type in seen:
            errors.append(f"{role}built-in tool '{builtin.type}' is switched on twice")
            continue
        seen.add(builtin.type)

        label = f"{role}built-in tool '{builtin.type}'"
        for key, value in builtin.config.items():
            option = tool.option(key)
            if option is None:
                declared = ", ".join(o.key for o in tool.options) or "none"
                errors.append(f"{label} has no option '{key}' - it takes: {declared}")
                continue
            message = _option_value_error(option, value, label)
            if message:
                errors.append(message)
        for option in tool.options:
            if option.required and _option_value_error(
                option, builtin.config.get(option.key), label
            ):
                errors.append(f"{label} needs '{option.key}' ({option.label})")
            # An allow-list and a block-list of the same thing: the provider
            # rejects the request outright, so catch it at save time.
            if not _is_blank(builtin.config.get(option.key)):
                for other in option.conflicts_with:
                    if not _is_blank(builtin.config.get(other)) and option.key < other:
                        errors.append(
                            f"{label} cannot set both '{option.key}' and '{other}' - "
                            "the provider rejects a request that carries both"
                        )

    return errors


def _is_blank(value: Any) -> bool:
    """Whether an option was left unset. `False` is a real value for a switch."""
    return value is None or (isinstance(value, str | list) and not value)


def _fallback(out: ValidationResult, kind: str, spec: Any, channel: str | None) -> None:
    """A failover model has to stand on its own: same catalog and channel
    checks as the primary. Language is agent-level, so
    ``validate_agent_language`` already covers both."""
    cat = get_catalog()
    fallback = getattr(spec, "fallback", None)
    if fallback is None:
        return
    if (fallback.provider, fallback.model) == (spec.provider, spec.model):
        out.errors.append(
            f"{kind.upper()} fallback is the same model as the primary "
            f"('{spec.provider}/{spec.model}') - pick a different one, or remove it"
        )
        return
    _supports(out, kind, fallback, channel)
    # Only meaningful once both sides name a real entry — otherwise `_supports`
    # has already reported the unknown model and _is_batch_stt would read the
    # miss as "streaming", inventing a second, misleading error.
    if kind == "stt" and all(cat.entry("stt", s.provider, s.model) for s in (spec, fallback)):
        # One VAD serves both models for the whole call, and only a batch model
        # needs it as its end-of-turn detector. A mixed pair would force that
        # VAD to be tuned for a model that may never run, shifting the live
        # model's turn-taking; matching the pair keeps end-of-turn identical
        # before and after a failover.
        if _is_batch_stt(cat, spec) != _is_batch_stt(cat, fallback):
            streaming, batch = (
                (spec, fallback) if _is_batch_stt(cat, fallback) else (fallback, spec)
            )
            out.errors.append(
                f"speech-to-text fallback must match the primary: "
                f"'{streaming.provider}/{streaming.model}' is streaming but "
                f"'{batch.provider}/{batch.model}' transcribes in batches - "
                "pick a fallback of the same kind"
            )


def _supports(out: ValidationResult, kind: str, spec: Any | None, channel: str | None) -> None:
    """One model slot against the catalog: the entry exists, and it runs here.

    ``channel`` is None for a task, which declares no media and therefore no
    channel — nothing to compare an entry's own channel list against. Only the
    language model reaches this that way; every other slot belongs to a session.
    """
    cat = get_catalog()
    if spec is None:
        label = (
            "speech-to-text"
            if kind == "stt"
            else "text-to-speech"
            if kind == "tts"
            else kind.upper()
        )
        out.errors.append(f"{channel} agents need a {label} model")
        return
    entry = cat.entry(kind, spec.provider, spec.model)
    if not entry:
        out.errors.append(_unknown_model(kind, spec.provider, spec.model))
    elif entry.channel and channel is not None and channel not in entry.channel:
        out.errors.append(
            f"{kind.upper()} model '{spec.provider}/{spec.model}' does not support channel '{channel}'"
        )
    _fallback(out, kind, spec, channel)


def _validate_prose(
    fields: Sequence[tuple[str, str | None]],
    *,
    timezone: str | None,
    declared_vars: set[str],
    channel: str | None,
) -> ValidationResult:
    """Every authored sentence that reaches a model or a caller, token by token.

    The prompt, the greeting and each handoff destination's three prose fields
    are all resolved by `compiler.compile` against session userdata, and most
    template roots have nothing to read there: no tool arguments exist outside a
    tool call, and a workspace secret is a credential for a tool to send, not
    text to hand the model. Rejected rather than left to resolve empty (or, for
    a secret, to raise mid-call).

    `{{consent.notice}}` is the exception, and only in the greeting. The prompt
    is model input, so a disclosure placed there would be paraphrased or dropped
    at the model's discretion — a recording notice has to be spoken verbatim,
    which only the greeting guarantees.

    The two unwritable roots are allowed everywhere, and they differ in what a
    name nobody recognizes means. `{{system_vars.*}}` has a closed key space —
    the platform fills it, not a tool — so a name the catalog does not have is a
    typo, refused here rather than resolving to silence on a live call.
    `{{vars.*}}` is open by design: values arrive on the request that starts the
    session, and an agent or a tool defined inline in that same request may
    legitimately read one this config never declared. So an undeclared name is a
    warning — nothing writes `vars` mid-session, which is what makes "nothing
    declares this" a real signal rather than a maybe.

    ``channel`` is None for a task, which has none. The only rule that reads it
    is the phone-number one, and a task's answer is different in kind: it takes
    those values from the call that entered it, and has none on a standalone run.
    """
    out = ValidationResult()
    for field, text in fields:
        # A token whose root is not a template root at all — `{{customer.number}}`,
        # a bare `{{name}}` pasted from another platform, a typo. `template_refs`
        # is built from the known roots, so it silently skips these, and the
        # literal braces reach the model on every call.
        vocabulary = (
            f"A {field} can read "
            + "{{userdata.…}}, {{system_vars.…}} and {{vars.…}}"
            + (f", plus {CONSENT_TOKEN}" if field == "greeting" else "")
        )
        for clause in unknown_template_tokens(text or "", vocabulary=vocabulary):
            out.errors.append(f"the {field} {clause}")
        for root, key in template_refs(text or ""):
            if not key:
                # `_TOKEN`'s key path is optional, so `{{userdata}}` parses and
                # resolves to the whole bag — the model would be handed a
                # stringified dict, session bookkeeping and all.
                names = (
                    f"one of: {SYSTEM_KEY_LIST}"
                    if root == SYSTEM_VARS_ROOT
                    else "the name of the one you want"
                )
                out.errors.append(
                    f"the {field} reads {{{{{root}}}}}, which is the whole {root} bag rather "
                    f"than one variable - add a dot and {names}"
                )
                continue
            if root == "userdata":
                continue
            if root == "consent" and field == "greeting":
                continue
            if root == SYSTEM_VARS_ROOT:
                if key not in SYSTEM_KEYS:
                    out.errors.append(
                        f"the {field} reads {{{{system_vars.{key}}}}}, which is not a system "
                        f"variable - the system variables are: {SYSTEM_KEY_LIST}"
                    )
                elif key in CLOCK_KEYS and timezone is None:
                    # The one moment an author needs to know the field exists, so
                    # it names it rather than quietly running the clock on UTC.
                    out.errors.append(
                        f"the {field} reads {{{{system_vars.{key}}}}}, but this has no "
                        f'timezone - set one so it knows what "now" means (for example '
                        f"'Asia/Kolkata')"
                    )
                elif key in CALL_KEYS and channel is None:
                    # A task takes these from the call that entered it, so they
                    # are real there and empty on a standalone run.
                    out.warnings.append(
                        f"the {field} reads {{{{system_vars.{key}}}}}, which a task only has "
                        "when a voice agent enters it during a call - it is empty on a "
                        "standalone run"
                    )
                elif key in CALL_KEYS and channel != "voice":
                    # Only a `voice` agent is ever dispatched onto a SIP call
                    # (`workers/voice/sip.py` allows that channel and no other),
                    # so on text and video this reads as nothing, every time.
                    out.warnings.append(
                        f"the {field} reads {{{{system_vars.{key}}}}}, but a {channel} "
                        f"agent never takes a phone call - this will always be empty"
                    )
                elif key == "now" and field in ("greeting", "voicemail message"):
                    # Both are spoken word for word, and "2026-08-19T19:26:59+05:30"
                    # read aloud is not a sentence.
                    out.warnings.append(
                        f"the {field} reads {{{{system_vars.now}}}}, which is a machine timestamp a "
                        "text-to-speech voice cannot say - use {{system_vars.time}} or "
                        "{{system_vars.date}} in a spoken line"
                    )
                continue
            if root == VARS_ROOT:
                if key not in declared_vars:
                    out.warnings.append(
                        f"the {field} reads {{{{vars.{key}}}}}, which is not declared - "
                        f"it will be empty unless the request that starts the "
                        f"session supplies it. {declared_vars_clause(declared_vars)}"
                    )
                continue
            reason = (
                "a workspace secret is a credential for tools to send, never text for the model"
                if root == "secrets"
                else "a voicemail message is left only when nobody answered - the recording "
                "notice belongs in the greeting"
                if root == "consent" and field == "voicemail message"
                else "the model would paraphrase or drop it, and a recording notice has to be "
                "spoken word for word - put it in the greeting instead"
                if root == "consent"
                else "tooldata exists only while the tool that wrote it is running - publish the "
                "value to userdata instead"
                if root == "tooldata"
                else "tool arguments exist only inside a tool call"
            )
            out.errors.append(f"the {field} cannot use {{{{{root}.{key}}}}} - {reason}")
    return out


def validate_agent_config(config: AgentBase, *, channel: str | None = None) -> ValidationResult:
    """The deterministic checks an agent and a task both get.

    Everything here is true of an `Agent` as LiveKit defines one: its name, its
    prompt, its variables, its language model and the namespace its tools share.
    What belongs to the *session* an agent owns — the media stack, the greeting,
    recording, analysis, the avatar — is `validate_session_config`, and a task
    has none of it.

    ``channel`` is the agent's, and None for a task. It is passed rather than
    read off the config because that is the whole difference between the two.
    """
    out = ValidationResult()

    # AgentConfig strips + requires name; re-check so draft validation surfaces
    # a clear field error even if callers mutated the model after parse.
    name = config.name.strip()
    if not name:
        out.errors.append("config must include a name")
    else:
        config.name = name

    # A realtime agent has no separate text model, which is why this is skipped
    # rather than reported: `validate_session_config` is what knows whether an
    # absent LLM is the realtime pipeline or a missing model.
    if config.llm is not None:
        _supports(out, "llm", config.llm, channel)

    # Its own declarations, for the `{{vars.*}}` branch below and for the
    # duplicate check. A name declared twice is two answers to one question, and
    # the second silently wins at merge time.
    declared = Counter(v.name for v in config.vars)
    declared_vars = set(declared)
    if duplicates := sorted(name for name, n in declared.items() if n > 1):
        out.errors.append(
            f"these variables are declared more than once: {', '.join(duplicates)} - "
            "each name gets one description and one default, and the later entry would "
            "silently win"
        )

    out.extend(
        _validate_prose(
            [("system prompt", config.prompt)],
            timezone=config.timezone,
            declared_vars=declared_vars,
            channel=channel,
        )
    )

    llm_specs = [(config.llm, ""), (config.llm and config.llm.fallback, "failover ")]
    out.errors.extend(_validate_builtin_tools(config))
    out.errors.extend(_reasoning_effort_errors(llm_specs))
    out.errors.extend(_priority_tier_errors(llm_specs))
    out.errors.extend(
        _host_errors(
            [(config.llm, "llm.hosts"), (config.llm and config.llm.fallback, "llm.fallback.hosts")]
        )
    )

    # A server attached twice is a mistake, not a request to attach it twice.
    # Only named ones: an inline server has no id, and two of those reaching here
    # as a pair of `None`s used to read as "integration None is attached twice".
    seen: set[UUID] = set()
    for ref in config.mcps:
        if ref.integration_id is None:
            continue
        if ref.integration_id in seen:
            out.errors.append(f"integration {ref.integration_id} is attached twice")
        seen.add(ref.integration_id)

    return out


def validate_session_config(config: AgentConfig) -> ValidationResult:
    """The checks that are about the session this agent owns, not the agent.

    The media stack, the greeting it speaks, the face it wears, what it records,
    what it analyses and what it remembers of earlier conversations. Every rule
    here reads `channel` or a session-level field, which is exactly the boundary
    — and exactly why a task, which runs inside somebody else's session, is not
    put through any of it.
    """
    out = ValidationResult()
    cat = get_catalog()

    def speed_setting(kind: str, spec: Any) -> None:
        """Speech rate, checked against the entry that actually renders it —
        the TTS model, or the realtime model that speaks for itself."""
        entry = cat.entry(kind, spec.provider, spec.model)
        if not entry:
            return
        label = kind.upper() if kind == "tts" else kind
        speed = spec.speed
        if not math.isfinite(speed):
            out.errors.append(f"{label} speed must be a finite number")
        elif not entry.supports_speed and abs(speed - 1.0) > 1e-9:
            out.errors.append(
                f"{label} model '{spec.provider}/{spec.model}' does not support speed"
            )
        elif entry.supports_speed and not (entry.speed_min <= speed <= entry.speed_max):
            out.errors.append(
                f"{label} speed for '{spec.provider}/{spec.model}' must be between "
                f"{entry.speed_min:g} and {entry.speed_max:g}"
            )

    def voice_settings(spec: Any) -> None:
        """The knobs copied off the picked voice, checked against its entry.

        Refused rather than dropped on a model that reads no such knob: the
        compiler would simply not send them, and a setting that silently does
        nothing is worse than one that fails at publish. The numeric range is
        already enforced by the field itself.
        """
        entry = cat.entry("tts", spec.provider, spec.model)
        if not isinstance(entry, TTSEntry) or entry.supports_voice_settings:
            return
        named = [
            name
            for name in ("stability", "similarity_boost")
            if getattr(spec, name, None) is not None
        ]
        if not named:
            return
        supported = ", ".join(
            f"{e.provider}/{e.model}" for e in cat.tts if e.supports_voice_settings
        )
        out.errors.append(
            f"text-to-speech model '{spec.provider}/{spec.model}' has no "
            f"{' or '.join(named)} setting - clear it, or pick one of: {supported}"
        )

    def tts_settings(spec: Any | None) -> None:
        """Per-voice settings, checked against that voice's own catalog entry —
        a fallback carries its own speed and voice, so it gets the same pass."""
        if spec is None:
            return
        speed_setting("tts", spec)
        voice_settings(spec)
        entry = cat.entry("tts", spec.provider, spec.model)
        # A toggle that silently changes nothing is the failure this feature
        # exists to avoid, so the model has to be one that speaks a dialect.
        if spec.expressive and isinstance(entry, TTSEntry) and entry.expressive is None:
            expressive_models = ", ".join(
                f"{e.provider}/{e.model}" for e in cat.tts if e.expressive is not None
            )
            out.errors.append(
                f"text-to-speech model '{spec.provider}/{spec.model}' cannot speak delivery "
                f"tags, so expressive delivery would change nothing - turn it off, or pick "
                f"one of: {expressive_models}"
            )

    # `validate_agent_config` has already checked the language model against the
    # catalog; what is left here is whether this channel needs one at all.
    if config.realtime is not None:
        _supports(out, "realtime", config.realtime, config.channel)
        speed_setting("realtime", config.realtime)
        realtime_entry = cat.entry("realtime", config.realtime.provider, config.realtime.model)
        if (
            config.handoffs
            and isinstance(realtime_entry, RealtimeEntry)
            and not realtime_entry.supports_instruction_update
        ):
            # A handoff hands the target agent's prompt to the open session
            # (`AgentSession.update_agent`), and this model refuses new
            # instructions once its session has started — it raises, mid-call,
            # on the turn the caller asked to be transferred. Caught here so the
            # author sees it while editing rather than on a live conversation.
            out.errors.append(
                f"{config.realtime.model} cannot be handed off from - it refuses new "
                "instructions once a call is connected. Remove the handoff destinations, "
                "or pick another speech-to-speech model"
            )
        if config.on_user_turn_completed:
            # LiveKit skips the hook entirely when the LLM detects turns
            # server-side (AgentActivity._on_user_turn_completed), and a hook
            # that never runs is worse than one the user knows they cannot have.
            out.errors.append(
                "realtime models detect the end of a turn themselves, so the "
                "'after each user turn' hook would never run - remove the hook, or use a "
                "speech-to-text / LLM / text-to-speech pipeline"
            )
        if not config.greeting_interruptible:
            # The model's own server VAD cuts the reply off on the caller's
            # voice, and LiveKit drops `allow_interruptions=False` with a warning
            # (`AgentActivity.say`), so the setting would silently do nothing.
            out.errors.append(
                "realtime models detect interruptions themselves, so the greeting cannot "
                "be protected from them - make the greeting interruptible, or use a "
                "speech-to-text / LLM / text-to-speech pipeline"
            )
    elif config.llm is None:
        out.errors.append(f"{config.channel} agents need an LLM model")

    if config.channel != "text" and config.realtime is None:
        _supports(out, "stt", config.stt, config.channel)
        _supports(out, "tts", config.tts, config.channel)
        tts_settings(config.tts)
        if config.tts:
            tts_settings(config.tts.fallback)
            # The dialect is taught once, at compile time, from the primary
            # voice — so a failover must not land tagged text on a
            # voice that treats tags as words. Closed here rather than by
            # rewriting the prompt mid-call: the failover is detected while
            # synthesizing text the model has already produced, so the turn that
            # triggers it is tagged whatever we do, the agent's own earlier turns
            # go on few-shotting it, and each flip costs a cache miss exactly
            # when the call is already degrading.
            if config.tts.fallback and config.tts.expressive != config.tts.fallback.expressive:
                on, off = (
                    (config.tts, config.tts.fallback)
                    if config.tts.expressive
                    else (config.tts.fallback, config.tts)
                )
                out.errors.append(
                    f"expressive delivery is on for '{on.provider}/{on.model}' but off for "
                    f"'{off.provider}/{off.model}' - a fallback takes over mid-call, and the "
                    "agent keeps writing delivery tags either way, so set both the same"
                )
        if (
            config.stt
            and config.turn_handling.interruption.min_words > 0
            and _is_batch_stt(cat, config.stt)
        ):
            out.warnings.append(
                "word-count interruption needs interim transcripts, which batch speech-to-text "
                "models do not produce - the agent will only notice the interruption once the "
                "caller stops speaking"
            )

    if config.channel == "video":
        if not config.avatar or not config.avatar.avatar_id:
            out.errors.append("video agents need an avatar")
        if config.avatar:
            provider = config.avatar.provider or "anam"
            model = (config.avatar.model or "").strip()
            if not model:
                out.errors.append("video agents need an avatar model (e.g. anam)")
            else:
                avatar_entry = cat.entry("avatar", provider, model)
                if not avatar_entry:
                    out.errors.append(
                        f"unknown avatar model '{provider}/{model}'"
                        + (f" - pick from: {_models('avatar')}" if _models("avatar") else "")
                    )
                elif avatar_entry.channel and config.channel not in avatar_entry.channel:
                    out.errors.append(
                        f"avatar model '{provider}/{model}' does not support channel '{config.channel}'"
                    )

    # The greeting and each handoff destination's three prose fields. The
    # description and the summary prompt are baked into the schema the model is
    # shown and the message is spoken, so all three are substituted at build time
    # and a token no root covers would reach the model — or the caller — as
    # literal braces. A task attachment's two prose fields go through the same
    # walk, from `_validate_tasks`, where the task they belong to is resolved.
    out.extend(
        _validate_prose(
            [
                ("greeting", config.greeting),
                ("voicemail message", config.voicemail_detection.message),
                *[
                    (f"handoff to '{t.name}' {label}", text)
                    for t in config.handoffs
                    for label, text in (
                        ("description", t.description),
                        ("summary prompt", t.summary_prompt),
                        ("message", t.message),
                    )
                ],
            ],
            timezone=config.timezone,
            declared_vars={v.name for v in config.vars},
            channel=config.channel,
        )
    )

    out.errors.extend(_validate_recording(config))
    out.errors.extend(_validate_noise_cancellation(config))
    out.warnings.extend(_validate_image_input(config))
    out.errors.extend(_validate_vision_input(config))
    out.errors.extend(_validate_keypad_input(config))
    out.errors.extend(_validate_analysis(config))
    out.errors.extend(_validate_conversation(config))
    out.errors.extend(_reasoning_effort_errors([(config.analysis.model, "call analysis ")]))
    out.errors.extend(_priority_tier_errors([(config.analysis.model, "call analysis ")]))
    out.errors.extend(_host_errors([(config.analysis.model, "analysis.model.hosts")]))
    out.errors.extend(validate_agent_language(config))

    # Nothing to validate about turn detection itself: `resolve_turn_detection`
    # derives it from the models and the language, so there is no combination a
    # user can ask for and be refused. Only the two numbers can disagree.
    endpointing = config.turn_handling.endpointing
    if endpointing.max_silence_duration < endpointing.min_silence_duration:
        out.errors.append(
            "maximum silence must be at least the minimum - the agent waits the maximum "
            "only when the turn detector judges the caller is mid-thought"
        )

    return out


def _builtin_tool_names(config: AgentConfig) -> set[str]:
    """Names the model runs itself — its provider built-ins, plus ours.

    A tenant tool, or a generated handoff tool, sharing one of these puts two
    things with one meaning in front of the model, and `ToolContext` raises on
    the duplicate at session start and kills the call. So it has to be caught
    here.
    """
    names = set(RESERVED_TOOL_NAMES)
    if config.llm:
        names |= {b.type for b in config.llm.builtin_tools}
        if config.llm.fallback:
            names |= {b.type for b in config.llm.fallback.builtin_tools}
    return names


def _presented_tool_names(
    config: AgentBase, tool_name_of: Mapping[str, str]
) -> list[tuple[str, str]]:
    """(name, where it came from) for every tool this puts a name on.

    Attached tools, hook tools and the tools generated for handoff destinations
    and task attachments share one namespace, because they end up in one
    `ToolContext`. The generated ones are added by `validate_agent_draft`, which
    is the only caller that has any — a task attaches neither.

    A tool id that did not resolve is skipped — "tool X not found" is already the
    error, and a second one about its missing name would be noise.
    """
    named: list[tuple[str, str]] = []
    for sel in config.tools:
        if sel.tool:
            named.append((sel.tool.name, "an inline tool"))
        elif sel.tool_id in tool_name_of:
            named.append((tool_name_of[sel.tool_id], "an attached tool"))
    for hook in ("on_enter", "on_exit", "on_user_turn_completed"):
        sel = getattr(config, hook)
        if sel is None:
            continue
        if sel.tool:
            named.append((sel.tool.name, f"the {hook} hook"))
        elif sel.tool_id in tool_name_of:
            named.append((tool_name_of[sel.tool_id], f"the {hook} hook"))
    return named


def _validate_tool_namespaces(
    config: AgentBase, by_id: Mapping[UUID, Integration]
) -> ValidationResult:
    """Two MCP servers on this config presenting their tools under one prefix.

    Every name they share then reaches one `ToolContext` twice and the session
    dies on the duplicate before the agent speaks. Hosted providers default their
    namespace to the provider key, so two connections to the same provider
    collide on every tool — the ordinary case, not an edge one.

    A blank namespace is skipped, because both ways of arriving at one are the
    author saying they want the server's own names: it is the default for a
    server they brought themselves, and the opt-out for any server. Two blank
    servers that do collide end the call as `agent_start_failed`, with the
    duplicate named on the call detail page.
    """
    named: list[tuple[str, str]] = []
    for sel in config.mcps:
        if sel.mcp is not None:
            named.append((sel.mcp.name, sel.mcp.tools_namespace))
        elif sel.integration_id in by_id:
            integration = by_id[sel.integration_id]
            named.append((integration.display_name, integration.tools_namespace))

    out = ValidationResult()
    by_namespace: dict[str, str] = {}
    for label, namespace in named:
        if not namespace:
            continue
        if namespace in by_namespace and by_namespace[namespace] != label:
            out.errors.append(
                f"'{label}' and '{by_namespace[namespace]}' both present their tools under "
                f"'{namespace}' - every tool name they share would reach the model twice. "
                "Give one of them a different tool namespace."
            )
        by_namespace[namespace] = label
    return out


def _validate_tool_names(
    named: Sequence[tuple[str, str]],
    builtin_names: set[str],
) -> ValidationResult:
    """One namespace, checked before the runtime discovers it the hard way.

    The same stored tool attached and wired to a hook is one tool and one name;
    `tool_selections()` has already collapsed those, so anything colliding here
    is genuinely two.
    """
    out = ValidationResult()
    seen: dict[str, str] = {}
    for name, source in named:
        if name in builtin_names:
            out.errors.append(
                f"{source} is named '{name}', which is what the model's own built-in tool is "
                "called - rename it, or switch the built-in one off"
            )
        elif name in seen and seen[name] != source:
            out.errors.append(
                f"two tools on this agent are named '{name}' ({seen[name]} and {source}) - "
                "the model would have no way to tell them apart"
            )
        else:
            seen.setdefault(name, source)
    return out


async def _validate_handoffs(
    ctx: Context,
    config: AgentConfig,
    roster: Mapping[str, AgentConfig] | None,
) -> ValidationResult:
    """The agent's outgoing handoff edges: names, targets, media compatibility.

    Every edge gets `handoff_media_error`, because a roster is a graph and every
    edge in it has to be walkable. `recording_handoff_warning` stays a warning
    for the reason it always has: whether the edge is a problem depends on which
    agent answered, which no per-agent check can know.
    """
    out = ValidationResult()
    if not config.handoffs:
        return out

    by_name: dict[str, HandoffTarget] = {}
    for target in config.handoffs:
        if target.name in by_name:
            out.errors.append(
                f"two handoff destinations are named '{target.name}' - "
                "names must be unique within an agent"
            )
            continue
        by_name[target.name] = target
        if handoff_tool_name(target.name) == "handoff_to_":
            out.errors.append(
                f"handoff destination '{target.name}' has no letters or digits in its name, "
                "so there is no tool name to give the model - rename it"
            )

    stored_ids = _ids(
        [t.agent_id for t in by_name.values() if t.agent_id], "handoff target agent", out
    )
    targets: dict[str, Any] = {}
    if stored_ids:
        pool = await ctx.tenant_pool()
        rows = await pool.fetch(
            """
            SELECT a.id, a.name, a.published_version, av.config
            FROM agents a
            LEFT JOIN agent_versions av
                ON av.agent_id = a.id
                AND av.version = a.published_version
                AND av.tenant_id = a.tenant_id
            WHERE a.id = ANY($1::uuid[]) AND a.tenant_id = $2
            """,
            stored_ids,
            ctx.tenant.id,
        )
        targets = {str(r["id"]): r for r in rows}

    for target in by_name.values():
        edge = f"the handoff to '{target.name}'"
        target_config: AgentConfig | None = None
        if not target.agent_id:
            if roster is None:
                # A stored agent's roster is unknowable at publish: it depends on
                # the call. Say what has to be true rather than refusing.
                out.warnings.append(
                    f"{edge} resolves only when this agent runs in a team that defines "
                    f"'{target.name}'"
                )
                continue
            target_config = roster.get(target.name)
            if target_config is None:
                out.errors.append(f"{edge} is neither a team member nor a published agent")
                continue
        else:
            row = targets.get(target.agent_id)
            if row is None:
                out.errors.append(f"{edge}: agent {target.agent_id} not found")
                continue
            if not row["published_version"] or not isinstance(row["config"], dict):
                out.errors.append(
                    f"{edge}: agent '{row['name']}' has no published version - "
                    "publish it before handing calls to it"
                )
                continue
            target_config = AgentConfig.model_validate(row["config"])
        # Every edge, whichever kind of target it names: a roster is a graph, and
        # a graph is only walkable if each edge is.
        if err := handoff_media_error(config, target_config):
            out.errors.append(f"{edge}: {err}")
        if warning := recording_handoff_warning(config, target_config):
            out.warnings.append(warning)
    return out


async def task_handoff_errors(ctx: Context, cfg: TaskConfig) -> list[str]:
    """The tools this task attaches whose tree contains a `handoff` operation.

    A task hands control back to the agent that entered it, so a handoff from
    inside one strands that return: `AgentTask.__await_impl` finds the agent has
    changed, logs, and never resumes the caller, whose `await` is then left for
    `timeout_seconds` to resolve. Refused at publish rather than at runtime,
    because a tool containing a handoff is a perfectly good tool on an agent.

    Checked on both sides of an attachment — here for the task's own publish, and
    again from `_validate_tasks` for an agent attaching a task published before
    this rule existed — which is why it is one function and not two.

    Attached tools and hook tools alike: `tool_selections()` is everything the
    task can run. An inline tool carries its tree already; a stored one's comes
    from its published version.
    """
    named: list[str] = []
    stored = [sel for sel in cfg.tool_selections() if sel.tool_id]
    definitions: dict[str, Any] = {}
    if stored:
        pool = await ctx.tenant_pool()
        rows = await pool.fetch(
            """
            SELECT t.id AS tool_id, tv.definition
            FROM tools t
            JOIN tool_versions tv
                ON tv.tool_id = t.id AND tv.tenant_id = t.tenant_id
                AND tv.version = t.published_version
            WHERE t.id = ANY($1::uuid[]) AND t.tenant_id = $2 AND t.published_version IS NOT NULL
            """,
            _ids([sel.tool_id for sel in stored], "tool", ValidationResult()),
            ctx.tenant.id,
        )
        definitions = {str(r["tool_id"]): r["definition"] for r in rows}
    for sel in cfg.tool_selections():
        if sel.tool is not None:
            tree = [operation_from_request(node) for node in sel.tool.operations]
            if "handoff" in tree_kinds(tree):
                named.append(sel.tool.name)
            continue
        definition = definitions.get(sel.tool_id or "")
        if definition and "handoff" in tree_kinds(definition.get("operations") or []):
            named.append(str(definition.get("name") or sel.tool_id))
    return [
        f"tool '{name}' contains a handoff - a task hands control back to the agent that "
        "entered it, so it cannot hand the conversation to a third agent"
        for name in named
    ]


async def _validate_tasks(ctx: Context, config: AgentConfig) -> tuple[ValidationResult, set[str]]:
    """The agent's task attachments: the tool each becomes, and what it points at.

    Also returns the providers the attached stored tasks need a key for. Their
    own publish checked that once, but a key deleted since would fail the task
    mid-call while this agent published clean. An inline task is left out: it is
    held to the full publish bar below, BYOK included.

    Name collisions are already covered — `validate_agent_draft` folds every
    attachment's `name` into the one tool namespace. What is left is the target:
    it has to exist and be published, its prose has to resolve, and the variables
    it declares have to be answerable by somebody.

    That last one is the most valuable check here. A task var the calling agent
    does not declare becomes a *model argument* on the generated tool, and a
    model asked for `order_id` will sometimes invent one. The fix is almost
    always in the task: read the value in its prompt (`{{userdata.last_order_id}}`)
    rather than declaring it.
    """
    # Local import: `services.tasks.validate` is built on this module, so
    # importing it at module scope is a cycle. An inline task is held to the
    # task's own publish bar, and that bar lives there.
    from services.tasks.validate import validate_task_draft

    out = ValidationResult()
    providers: set[str] = set()
    if not config.tasks:
        return out, providers

    by_name: dict[str, TaskSelection] = {}
    for sel in config.tasks:
        if sel.name in by_name:
            out.errors.append(
                f"two tasks on this agent are named '{sel.name}' - "
                "names must be unique within an agent"
            )
            continue
        by_name[sel.name] = sel

    # A row the author has not finished picking. Reported as what to do rather
    # than as `task '' is not a valid id`, which is what `_ids` would say.
    for sel in by_name.values():
        if sel.task is None and not (sel.task_id or "").strip():
            out.errors.append(f"the task '{sel.name}' has no task to run - pick one")
    stored_ids = _ids(
        [s.task_id for s in by_name.values() if (s.task_id or "").strip()], "task", out
    )
    rows: dict[str, Any] = {}
    if stored_ids:
        pool = await ctx.tenant_pool()
        fetched = await pool.fetch(
            """
            SELECT t.id, t.name, t.published_version, v.config
            FROM agent_tasks t
            LEFT JOIN agent_task_versions v
                ON v.task_id = t.id AND v.version = t.published_version
                AND v.tenant_id = t.tenant_id
            WHERE t.id = ANY($1::uuid[]) AND t.tenant_id = $2
            """,
            stored_ids,
            ctx.tenant.id,
        )
        rows = {str(r["id"]): r for r in fetched}

    declared = {v.name for v in config.vars}
    for sel in by_name.values():
        edge = f"the task '{sel.name}'"
        if sel.task is None and not (sel.task_id or "").strip():
            continue  # already reported above
        if sel.task is not None:
            target = sel.task
            # Held to every rule its stored equivalent publishes under, the way
            # an inline tool is — an inline task becomes a real row on the next
            # agent write, so anything unpublishable here is unpublishable there.
            inline = await validate_task_draft(ctx, target, for_publish=True)
            out.errors.extend(f"{edge}: {e}" for e in inline.errors)
            out.warnings.extend(f"{edge}: {w}" for w in inline.warnings)
        else:
            row = rows.get(sel.task_id or "")
            if row is None:
                out.errors.append(f"{edge}: task {sel.task_id} not found")
                continue
            if not row["published_version"] or not isinstance(row["config"], dict):
                out.errors.append(
                    f"{edge}: task '{row['name']}' has no published version - "
                    "publish it before attaching"
                )
                continue
            target = TaskConfig.model_validate(row["config"])
            providers |= target.required_providers()
            out.errors.extend(f"{edge}: {e}" for e in await task_handoff_errors(ctx, target))

        # The two prose fields on the attachment: the description is baked into
        # the schema the model is shown and the message is spoken, so both are
        # substituted at build time and an unknown token would reach the model —
        # or the caller — as literal braces. Resolved against the CALLING agent,
        # whose userdata and vars they are written beside.
        out.extend(
            _validate_prose(
                [
                    (f"task '{sel.name}' description", sel.description),
                    (f"task '{sel.name}' message", sel.message),
                ],
                timezone=config.timezone,
                declared_vars=declared,
                channel=config.channel,
            )
        )

        for var in target.vars:
            if var.name in declared or var.default is not None or not var.required:
                continue
            out.warnings.append(
                f"{edge} requires a variable '{var.name}' that this agent does not declare, "
                "so the model will be asked to supply it and may invent one. Declare it on "
                "this agent, give it a default, or - if the value is already in the "
                f"conversation - read it in the task's prompt instead of declaring it"
            )
    return out, providers


async def _validate_inline_tools(
    ctx: Context, config: AgentBase, secret_names: set[str]
) -> ValidationResult:
    """Every inline tool, by the rules its stored equivalent publishes under.

    Not transpiled here: compiling a `code` op is part of validating it, but a
    call plan compiles every op across the whole roster in one round trip and
    folds those errors in itself.
    """
    out = ValidationResult()
    for sel in config.tool_selections():
        tool = sel.tool
        if tool is None:
            continue
        if tool.name in RESERVED_TOOL_NAMES:
            out.errors.append(f"'{tool.name}' is reserved by the platform")
            continue
        errors, warnings = await validate_tool_definition(
            ctx,
            name=tool.name,
            json_schema=tool.json_schema,
            tree=[operation_from_request(node) for node in tool.operations],
            long_running_task=tool.long_running_task,
            silent=tool.silent,
            disable_interruptions=tool.disable_interruptions,
            secret_names=secret_names,
            source_config=config,
        )
        if not tool.operations:
            errors = ["add at least one operation", *errors]
        out.errors.extend(errors)
        out.warnings.extend(warnings)
    return out


def _validate_inline_mcps(config: AgentBase, secret_names: set[str]) -> ValidationResult:
    """An inline MCP server's URL and headers, checked the way a stored one's are.

    The URL goes through the same SSRF guard the compiler runs, and every
    `{{secrets.NAME}}` in the URL or a header has to name a secret that exists —
    otherwise the server is unreachable on the first tool call, mid-call.
    """
    out = ValidationResult()
    for sel in config.mcps:
        server = sel.mcp
        if server is None:
            continue
        label = f"MCP server '{server.name}'"
        try:
            check_url(server.url)
        except ValueError as e:
            out.errors.append(f"{label}: {e}")
        for value in (server.url, *server.headers.values()):
            for root, key in template_refs(value):
                if root != "secrets":
                    out.errors.append(
                        f"{label}: {{{{{root}.{key}}}}} is not available here - "
                        "only {{secrets.NAME}} is"
                    )
                elif key not in secret_names:
                    out.errors.append(f"{label}: secret '{key}' does not exist")
    return out


def _ids(values: list[Any], label: str, out: ValidationResult) -> list[str]:
    ids: list[str] = []
    for v in values:
        try:
            ids.append(str(UUID(str(v))))
        except (ValueError, TypeError):
            out.errors.append(f"{label} '{v}' is not a valid id")
    return ids


# Every status other than `active`, so a new one fails loudly here rather than
# reading as the raw enum.
_INTEGRATION_NOT_ACTIVE = {
    "needs_reconnect": ("needs reconnecting", "reconnect it"),
    "disabled": ("is disabled", "enable it"),
    "error": ("has an error", "fix it"),
}


async def validate_base_draft(
    ctx: Context,
    config: AgentBase,
    *,
    for_publish: bool = False,
    channel: str | None = None,
    extra_providers: set[str] = frozenset(),
    extra_tool_names: Sequence[tuple[str, str]] = (),
) -> ValidationResult:
    """Everything an agent and a task are both held to, database included.

    `validate_agent_config` plus the checks that need rows: the attached tools
    exist and are published, the MCP integrations and FAQs exist, nothing collides in
    the tool namespace, and — at publish grade — the workspace has a key for
    every provider, every operation tree holds up, and an integration that is
    not active is warned about.

    ``extra_providers`` are providers this config needs that it cannot name
    itself: an agent's post-call analysis model, whose default lives in
    catalog.yaml, which the domain model deliberately cannot read, and the
    models of the stored tasks it attaches.
    ``extra_tool_names`` are the tools the compiler *generates* — a handoff
    destination's, a task attachment's — which share the same namespace.
    """
    pool = await ctx.tenant_pool()
    tenant_id = ctx.tenant.id
    out = validate_agent_config(config, channel=channel)

    hook_refs = {
        "on_enter": config.on_enter,
        "on_exit": config.on_exit,
        "on_user_turn_completed": config.on_user_turn_completed,
    }
    hook_ids = [h.tool_id for h in hook_refs.values() if h and h.tool_id]
    attached_tool_ids = _ids([sel.tool_id for sel in config.tools if sel.tool_id], "tool", out)
    hook_tool_ids = _ids(hook_ids, "hook tool", out)
    tool_ids = list(dict.fromkeys(attached_tool_ids + hook_tool_ids))

    tool_rows = []
    tool_found = {}
    if tool_ids:
        tool_rows = await pool.fetch(
            "SELECT id, name, json_schema, published_version FROM tools "
            "WHERE id = ANY($1::uuid[]) AND tenant_id = $2",
            tool_ids,
            tenant_id,
        )
        tool_found = {str(r["id"]): r for r in tool_rows}
        for tid in tool_ids:
            r = tool_found.get(tid)
            if not r:
                out.errors.append(f"tool {tid} not found")
            elif not r["published_version"]:
                out.errors.append(
                    f"tool '{r['name']}' has no published version - publish it before attaching"
                )

    attached_integrations: list[Integration] = []
    by_id: dict[UUID, Integration] = {}
    mcp_ids = [sel.integration_id for sel in config.mcps if sel.integration_id]
    if mcp_ids:
        integration_rows = await pool.fetch(
            f"SELECT {INTEGRATION_COLUMNS} FROM integrations "
            "WHERE id = ANY($1::uuid[]) AND tenant_id = $2",
            mcp_ids,
            tenant_id,
        )
        by_id = {r["id"]: Integration.from_row(r) for r in integration_rows}
        for iid in mcp_ids:
            integration = by_id.get(iid)
            if integration is None:
                out.errors.append(f"integration {iid} not found")
                continue
            attached_integrations.append(integration)
            if integration.provider not in TOOL_PROVIDER_SET:
                out.errors.append(
                    f"integration '{integration.display_name}' is not MCP-capable and cannot be attached as a tool"
                )

    # Existence is all there is to check: FAQ content is live and an FAQ has no
    # readiness state, so one that exists is usable.
    empty_faqs: list[str] = []
    faq_ids = [sel.faq_id for sel in config.faqs if sel.faq_id]
    if faq_ids:
        faq_rows = await pool.fetch(
            "SELECT f.id, f.name, EXISTS (SELECT 1 FROM faq_entries e "
            "WHERE e.faq_id = f.id AND e.tenant_id = f.tenant_id) AS has_entries "
            "FROM faqs f WHERE f.id = ANY($1::uuid[]) AND f.tenant_id = $2",
            faq_ids,
            tenant_id,
        )
        faq_found = {r["id"]: r for r in faq_rows}
        for faq_id, count in Counter(faq_ids).items():
            faq = faq_found.get(faq_id)
            if faq is None:
                out.errors.append(f"FAQ {faq_id} not found")
                continue
            if count > 1:
                out.errors.append(f"FAQ '{faq['name']}' is attached twice")
            if not faq["has_entries"]:
                empty_faqs.append(faq["name"])

    builtin_names = _builtin_tool_names(config)
    tool_name_of = {str(r["id"]): r["name"] for r in tool_rows}
    out.extend(
        _validate_tool_names(
            [*_presented_tool_names(config, tool_name_of), *extra_tool_names], builtin_names
        )
    )
    out.extend(_validate_tool_namespaces(config, by_id))

    # Inline definitions, checked by exactly the rules their stored equivalents
    # publish under — on every write, not only at publish, because there is no
    # later moment: a call plan runs them immediately, and an agent write
    # materializes them into real rows. Guarded so a config with none pays
    # nothing for the secret lookup.
    if any(sel.tool for sel in config.tool_selections()) or any(sel.mcp for sel in config.mcps):
        inline_secrets = await list_secret_names(ctx.tenant)
        out.extend(await _validate_inline_tools(ctx, config, inline_secrets))
        out.extend(_validate_inline_mcps(config, inline_secrets))

    if not for_publish:
        return out

    # An integration's status is runtime state, not something the author got
    # wrong: a dead refresh token moves it with nobody editing anything. A
    # session reads only the active ones (`workers.session.load`) and runs
    # without the rest, so here it is a warning — an error would refuse every
    # call started through the API while the same agent kept answering its
    # phone number — and a draft write does not look at it at all.
    # Not an error: an empty FAQ attaches nothing and the agent runs without
    # it, which is a state the author passes through while filling one in.
    out.warnings.extend(f"FAQ '{name}' has no questions yet" for name in empty_faqs)

    for integration in attached_integrations:
        if integration.provider in TOOL_PROVIDER_SET and integration.status != "active":
            state, remedy = _INTEGRATION_NOT_ACTIVE[integration.status]
            out.warnings.append(
                f"integration '{integration.display_name}' {state} - "
                f"its tools are left out until you {remedy}"
            )

    # Strict BYOK: Talqing runs agents on the tenant's own provider keys
    # and has none of its own to fall back on.
    configured = await list_configured_providers(ctx)
    catalog = get_catalog()
    for provider in sorted((config.required_providers() | extra_providers) - configured):
        label = catalog.provider_label(provider) if provider in catalog.providers else provider
        # Reached only at publish grade — for an agent and, since tasks gained
        # the same lifecycle, for a task too. The reader already knows what they
        # were trying to do, so the sentence only has to say what is missing and
        # where.
        #
        # The opening clause is matched by the dashboard (frontend/lib/byok.ts)
        # to offer the key box for `label`'s provider without leaving the
        # editor: `errors` is a list of strings, so this sentence is the only
        # place the provider survives. Reword the tail freely; keep the
        # "no <provider label> API key for this workspace" opening.
        out.errors.append(f"no {label} API key for this workspace - add one under BYOK")

    out.extend(
        await _host_availability(
            [
                (config.llm, "llm.hosts"),
                (config.llm and config.llm.fallback, "llm.fallback.hosts"),
            ]
        )
    )

    secret_names = await list_secret_names(ctx.tenant)
    definitions = {}
    tool_names = {}
    if tool_ids:
        version_rows = await pool.fetch(
            """
            SELECT t.id AS tool_id, tv.definition
            FROM tools t
            JOIN tool_versions tv
                ON tv.tool_id = t.id
                AND tv.tenant_id = t.tenant_id
                AND tv.version = t.published_version
            WHERE t.id = ANY($1::uuid[]) AND t.tenant_id = $2
                AND t.published_version IS NOT NULL
            """,
            tool_ids,
            tenant_id,
        )
        definitions = {str(r["tool_id"]): r["definition"] for r in version_rows}
        tool_names = {str(r["id"]): r["name"] for r in tool_rows}
        schemas = {str(r["id"]): (r["json_schema"] or {}) for r in tool_rows}
        for tid in tool_ids:
            tool = tool_found.get(tid)
            if tool and tool["published_version"] and tid not in definitions:
                out.errors.append(
                    f"tool '{tool['name']}' published version v{tool['published_version']} is missing"
                )
        for tid in attached_tool_ids:
            definition = definitions.get(tid)
            if not definition:
                continue
            schema = definition.get("json_schema") or schemas.get(tid) or {}
            arg_names = set((schema.get("properties") or {}).keys())
            out.extend(
                await validate_operation_tree(
                    definition.get("operations") or [],
                    arg_names=arg_names,
                    secret_names=secret_names,
                    tool_name=tool_names.get(tid),
                    ctx=ctx,
                    source_config=config,
                )
            )

    for integration in attached_integrations:
        if integration.provider not in TOOL_PROVIDER_SET or integration.status != "active":
            continue
        for err in validate_integration_definition(integration, secret_names=secret_names):
            out.errors.append(f"integration '{integration.display_name}': {err}")

    hook_args = {
        "on_enter": set(),
        "on_exit": set(),
        "on_user_turn_completed": {"user_message"},
    }
    for hook, ref in hook_refs.items():
        if not ref or not ref.tool_id:
            continue
        tid = ref.tool_id
        definition = definitions.get(tid)
        if not definition:
            continue
        operations = definition.get("operations") or []
        if "transfer" in tree_kinds(operations):
            # A hook runs outside an LLM turn: `on_enter` is fired into its own
            # asyncio task, and `_HookRunContext.wait_for_playout` is a
            # deliberate no-op — so the transfer's wait for outstanding speech
            # would silently do nothing and the caller would be handed over
            # mid-word. Checked at agent publish, because a hook tool is a
            # perfectly valid tool on its own.
            out.errors.append(
                f"the '{hook}' hook cannot use tool "
                f"'{tool_names.get(tid, tid)}': it contains a transfer, and a hook runs "
                "outside a turn, so the agent cannot finish speaking before the handover. "
                "Attach it as a normal tool the model can call instead."
            )
        out.extend(
            await validate_operation_tree(
                operations,
                arg_names=hook_args[hook],
                secret_names=secret_names,
                tool_name=f"{hook} hook {tool_names.get(tid, tid)}",
                ctx=ctx,
                source_config=config,
            )
        )

    return out


async def validate_agent_draft(
    ctx: Context,
    config: AgentConfig,
    *,
    for_publish: bool = False,
    roster: Mapping[str, AgentConfig] | None = None,
) -> ValidationResult:
    """Validate an agent draft.

    Three layers: what an agent and a task are both held to
    (`validate_base_draft`), what belongs to the session an agent owns
    (`validate_session_config`), and the two kinds of edge only an agent has —
    handoff destinations and task attachments.

    The base config/reference checks run for every draft write. The deeper
    checks — attached-tool definitions and BYOK provider keys — run only for
    publish, because they can surface cross-tool orchestration warnings and
    runtime dependency errors. A draft is allowed to reference a provider the
    tenant has not brought a key for yet; a published agent is not, since it
    would fail on its first turn.

    This is the ONE validator, and that is the whole safety story for the inline
    and overridden configs a call can carry: they are checked by the same code
    that guards publish, so nothing new had to be written to make them
    trustworthy and there is no second validator to drift.

    ``roster`` is the cast of the call this config will run on, by member name. A
    handoff destination with no `agent_id` resolves against it — an error when
    the name is missing, and the same media check every other edge gets when it
    is there. Left None (a stored agent, whose roster is unknowable at publish)
    such a destination is a warning instead: loud at the point where it can
    actually be checked.
    """
    tasks, extra_providers = await _validate_tasks(ctx, config)
    # Only ever adds a provider when the agent names an analysis model of its
    # own. Left unset, analysis runs on the agent's own LLM (or, for a realtime
    # agent, that provider's LLM), so the key is already required — which is the
    # point of that default: turning analysis on can never demand a key the agent
    # did not already need.
    if config.analysis.enabled:
        target = analysis.resolve_model(config)
        if target is not None:
            extra_providers.add(target.provider.strip().lower())

    generated = [
        *[(handoff_tool_name(t.name), f"the handoff to '{t.name}'") for t in config.handoffs],
        *[(sel.name, f"the task '{sel.name}'") for sel in config.tasks],
    ]
    out = await validate_base_draft(
        ctx,
        config,
        for_publish=for_publish,
        channel=config.channel,
        extra_providers=extra_providers,
        extra_tool_names=generated,
    )
    out.extend(validate_session_config(config))
    if for_publish and config.analysis.enabled:
        out.extend(await _host_availability([(config.analysis.model, "analysis.model.hosts")]))
    out.extend(await _validate_handoffs(ctx, config, roster))
    out.extend(tasks)
    return out
