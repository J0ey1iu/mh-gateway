from __future__ import annotations

from typing import Callable

from minimal_harness.memory import Memory

from mh_gateway.adapters import SessionStoreProtocol
from mh_gateway.database._session import Session, SessionSummary

MemoryFactory = Callable[[], Memory]

__all__ = [
    "MemoryFactory",
    "Session",
    "SessionStoreProtocol",
    "SessionSummary",
]
