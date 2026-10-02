"""Data retention: one policy per organization, enforced by a job per call.

`tenants.retention_days` is the policy (NULL = keep forever, the default) and
`session.purge` is the whole enforcement mechanism — there is no bucket
lifecycle rule and no sweep. Session finalize schedules one job per call for
`ended_at + retention_days`, so an unlimited workspace costs zero rows.
"""

from __future__ import annotations

from .purge import purge_session  # noqa: F401
from .schedule import schedule_session_purge  # noqa: F401

__all__ = ["purge_session", "schedule_session_purge"]
