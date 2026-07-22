"""Public session DTOs.

The previous release kept :class:`Session`, :class:`SimpleSession`,
and :class:`SessionSummary` under ``mh_gateway.database._session``,
which forced downstream packages to import private modules.  This
module re-exports them as the canonical public surface.
"""

from __future__ import annotations

from mh_gateway.database._session import (
    Session,
    SessionSummary,
    SimpleSession,
)

__all__ = [
    "Session",
    "SessionSummary",
    "SimpleSession",
]
