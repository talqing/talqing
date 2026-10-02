"""The `_talqing` prefix on `session.userdata`: the runtime's own keys.

`session.userdata` has two writers. The tenant fills it — the call token, an
outbound dial, a batch row, a text conversation, the browser's `set_userdata`
RPC, a tool's `userdata` store — and the runtime keeps its session bookkeeping
in it under this prefix: the handoff counter, the end-call flag, the
reported-cost collector, the recording withdrawal. Every tenant door refuses the
prefix, and everything that carries userdata out of the session strips it.

A leaf module, stdlib only, so every one of those doors can import it.
"""

from __future__ import annotations

RESERVED_PREFIX = "_talqing"
RESERVED_KEY_ERROR = f"userdata keys starting with {RESERVED_PREFIX} are reserved"

# Set by the text worker on a session it is only parking — a chat's warm window
# closing while the chat itself stays open — so `on_exit` knows it is not the end.
WINDOW_PARKING_KEY = f"{RESERVED_PREFIX}_window_parking"


def is_reserved_key(key: object) -> bool:
    return str(key).startswith(RESERVED_PREFIX)
