"""Bounds shared by every CoPilot.

Who each CoPilot is — its sentinel id, what it edits, how it is prompted and
which tools it gets — lives in ``services.copilot.subjects``.
"""

from __future__ import annotations

# Hard bound on LLM round-trips per user turn (mirrors the old engine MAX_STEPS).
# The CoPilot's AgentSession is handed the same number, so LiveKit's own ceiling
# never trips first.
COPILOT_MAX_STEPS = 25
