"""FAQ shapes: what the API takes and returns, and what a session is handed.

Pure pydantic, and importing nothing from ``services.agents`` — an agent's
config embeds `InlineFaq`, so the dependency runs the other way.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

MAX_ENTRIES = 500

FaqName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
Question = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
Answer = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


def question_key(question: str) -> str:
    """What makes two questions the same one. Matches `uq_faq_entries_question`."""
    return question.strip().lower()


def duplicate_questions(questions: list[str]) -> list[str]:
    """Each question that repeats an earlier one in the list, as written."""
    seen: set[str] = set()
    repeated: list[str] = []
    for question in questions:
        key = question_key(question)
        if key in seen:
            repeated.append(question)
        seen.add(key)
    return repeated


class FaqEntryInput(BaseModel):
    question: Question
    answer: Answer

    model_config = ConfigDict(extra="forbid")


# One model for `create_faq`'s body and for an FAQ defined inline in a config,
# so an inline FAQ is held to exactly the rules a stored one is.
class FaqDefinition(BaseModel):
    """An FAQ: a named list of question/answer pairs."""

    name: FaqName
    entries: list[FaqEntryInput] = Field(default_factory=list, max_length=MAX_ENTRIES)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _no_duplicate_questions(self) -> FaqDefinition:
        if repeated := duplicate_questions([e.question for e in self.entries]):
            raise ValueError(f"duplicate question: '{repeated[0]}'")
        return self


class InlineFaq(FaqDefinition):
    """An FAQ defined here rather than named.

    On the call endpoints it is used for that call only; on the agent and task
    endpoints it is created as a real FAQ.
    """

    # The one difference from a stored FAQ: the dashboard creates an empty one
    # and fills it in, but an inline FAQ with nothing in it attaches nothing.
    entries: list[FaqEntryInput] = Field(min_length=1, max_length=MAX_ENTRIES)


class UpdateFaqRequest(BaseModel):
    name: FaqName

    model_config = ConfigDict(extra="forbid")


class CreateFaqEntriesRequest(BaseModel):
    entries: list[FaqEntryInput] = Field(min_length=1, max_length=MAX_ENTRIES)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _no_duplicate_questions(self) -> CreateFaqEntriesRequest:
        if repeated := duplicate_questions([e.question for e in self.entries]):
            raise ValueError(f"duplicate question: '{repeated[0]}'")
        return self


class UpdateFaqEntryRequest(BaseModel):
    question: Question | None = None
    answer: Answer | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _changes_something(self) -> UpdateFaqEntryRequest:
        if self.question is None and self.answer is None:
            raise ValueError("set question, answer or both")
        return self


class FaqEntry(BaseModel):
    id: UUID
    question: str
    answer: str
    created_at: datetime
    updated_at: datetime


class FaqSummary(BaseModel):
    id: UUID
    name: str
    entry_count: int
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime


class FaqDetail(FaqSummary):
    entries: list[FaqEntry]


class FaqEntriesResponse(BaseModel):
    entries: list[FaqEntry]


class FaqForPrompt(BaseModel):
    """One attached FAQ as a session reads it: its name and (question, answer) pairs."""

    name: str
    entries: list[tuple[str, str]]
