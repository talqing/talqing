"""The one path from the control plane out to a region.

The mirror of ``services.control``, and each package is named for who it talks
TO. Control calls a region for exactly one thing — applying a credit issuance or
reversing one — because the balance is regional and the payment processor is not.

Control plane only. A region importing this would be the region-to-region path
this deployment forbids.
"""

from __future__ import annotations

from . import client  # noqa: F401
from .client import RegionUnreachable  # noqa: F401

__all__ = ["RegionUnreachable", "client"]
