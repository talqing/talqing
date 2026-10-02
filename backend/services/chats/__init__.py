"""Chats: a text session from its first message to its explicit end.

A chat is to text what a call is to voice — `chat.id` is a `sessions.id`. It
starts with `POST /v1/chats` or with a channel message on a contact that has no
open chat, and ends only when the agent's `end_call` tool or `POST …/end` says so.

Import the submodule you need: ``open`` (starting and joining, shared with the
channel adapters), ``service`` (the API use-cases), ``models``.
"""
