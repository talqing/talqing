"""Post-call analysis: summary, outcome and tenant-defined extractions.

One LLM call reads a finished transcript at the end of a voice or video call
and fills in whatever the agent's `analysis` spec asks for. It runs in-process
inside the worker's finalize path — between sealing the session and pricing it,
so its token usage is priced in the same pass as the call's own.

    from services import analysis

    outcome = await analysis.run_for_session(
        tenant, session_id, cfg, transcript=transcript, close_reason=reason
    )

Nothing here raises into a call. A failed analysis leaves `analysis_status` at
'failed' and the call ends normally.
"""

from .models import (
    SKIP_CALL_FAILED,
    SKIP_PRODUCES_NOTHING,
    SKIP_TOO_SHORT,
    AnalysisOutcome,
    AnalysisResponse,
    AnalysisStatus,
    Outcome,
)
from .run import (
    MIN_USER_TURNS,
    TIMEOUT_S,
    analyze,
    persist,
    resolve_model,
    run_for_session,
    skip_reason_for,
)

__all__ = [
    "MIN_USER_TURNS",
    "SKIP_CALL_FAILED",
    "SKIP_PRODUCES_NOTHING",
    "SKIP_TOO_SHORT",
    "TIMEOUT_S",
    "AnalysisOutcome",
    "AnalysisResponse",
    "AnalysisStatus",
    "Outcome",
    "analyze",
    "persist",
    "resolve_model",
    "run_for_session",
    "skip_reason_for",
]
