"""One question, one set of answers: what does this start with?

The platform asks it in two places, and they must not answer it in two
vocabularies:

  * ``AgentConfig.conversation.context`` — what a new CALL starts with.
  * the ``handoff`` operation's ``context`` — what the next AGENT starts with.

A leaf module rather than a name on either side, because both sides need it and
they import each other: ``services.agents`` reaches ``services.conversations``
through the integrations providers, and ``services.conversations.refs`` needs
this to type ``open_conversation``. Owned by neither, so there is no cycle to
break with a TYPE_CHECKING guard that would only hide one.
"""

from __future__ import annotations

from typing import Literal, get_args

ConversationContext = Literal["none", "summary", "transcript"]

# For validators and pickers that need the values at runtime. `transcript` first
# because that is the handoff operation's default and the order the editors show.
CONVERSATION_CONTEXTS: tuple[str, ...] = get_args(ConversationContext)
