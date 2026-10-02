"""The one path from a region to the control plane.

Regions are independent: no region reads, writes or needs another region's data,
and no region talks to another region. Control is the single thing both share,
and it talks in both directions — a region asks control who a caller is and to
start a checkout; control pushes an applied credit or a reversal back. Nothing
here may grow a region-to-region call, and a change that wants one is a change to
that rule rather than an implementation detail.

Public surface::

    from services.control import client
"""

from __future__ import annotations

from . import client  # noqa: F401
from .client import ControlPlaneError, ControlRefused  # noqa: F401

__all__ = ["ControlPlaneError", "ControlRefused", "client"]
