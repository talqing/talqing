"""The attached FAQs as a prompt block and the one tool that reads them.

The questions go in the system prompt, where the model can match what the user
asked against them; the answers stay out of it and are fetched by id. A long FAQ
then costs its questions on every turn rather than its answers, and what the
agent says comes from the author's text rather than from the model's
recollection of it.
"""

from __future__ import annotations

from collections.abc import Sequence

from livekit.agents import llm
from livekit.agents.llm import function_tool

from services.faqs import FaqForPrompt

# Reserved workspace-wide in `services.tools.RESERVED_TOOL_NAMES`.
FAQ_TOOL_NAME = "get_faq_answers"

# No "refuse anything that is not listed" line, deliberately: an FAQ is one
# source beside the author's prompt and tools, and a blanket refusal here would
# override the prompt they wrote.
_BLOCK_HEADER = (
    "## FAQ\n"
    "The business has written answers to the questions below. When the user asks one of "
    f"them, or the same thing in other words, call `{FAQ_TOOL_NAME}` with its id(s) and "
    "answer from what it returns. Never answer these from memory."
)

_DESCRIPTION = "Read the written answers for FAQ questions by id (q1, q2…)."
# LiveKit speaks the text that arrives with a tool call before it runs the tool,
# so this line is what the caller hears instead of silence. A chat has no
# silence to cover, and "let me check" there is a wasted bubble.
_SPOKEN_DESCRIPTION = (
    f"{_DESCRIPTION} In the same turn as this call, briefly tell the user you are checking."
)


def build_faqs(faqs: Sequence[FaqForPrompt], *, spoken: bool) -> tuple[str, llm.Tool | None]:
    """The prompt block and the tool for ``faqs``; ``("", None)`` when none is attached.

    Ids run `q1…qN` across every attached FAQ in order. They are positions, so
    they are only meaningful for the session they were built for — which is why
    the answers are captured here, at the same moment, rather than read when
    the tool runs: the tool can never return an answer for a different question
    than the prompt showed under that id.

    Nothing here is templated. FAQ text is the tenant's data, and a `{{` inside
    an answer is punctuation, not a variable.
    """
    entries: dict[str, tuple[str, str]] = {}
    sections: list[str] = []
    for faq in faqs:
        if not faq.entries:
            continue
        lines = [f"### {faq.name}"]
        for question, answer in faq.entries:
            qid = f"q{len(entries) + 1}"
            entries[qid] = (question, answer)
            lines.append(f"- {qid} | {question}")
        sections.append("\n".join(lines))
    if not entries:
        return "", None

    async def get_faq_answers(raw_arguments: dict[str, object]) -> str:
        ids = raw_arguments.get("ids")
        if not isinstance(ids, list) or not ids:
            return "Pass the ids of the questions to read, e.g. q1."
        found: list[str] = []
        for qid in ids:
            entry = entries.get(str(qid))
            # Said, never dropped: a model that invented an id has to learn it
            # got nothing, or it answers as if it had read something.
            found.append(
                f"Q: {entry[0]}\nA: {entry[1]}" if entry else f"No FAQ entry with id {qid}."
            )
        return "\n\n".join(found)

    tool = function_tool(
        get_faq_answers,
        raw_schema={
            "name": FAQ_TOOL_NAME,
            "description": _SPOKEN_DESCRIPTION if spoken else _DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "ids": {"type": "array", "items": {"type": "string"}, "minItems": 1}
                },
                "required": ["ids"],
            },
        },
    )
    return "\n\n".join([_BLOCK_HEADER, *sections]), tool
