from __future__ import annotations

import inspect
import warnings
from collections.abc import Awaitable, Callable

from fastapi import Request

from mh_gateway.adapters import DatabaseProtocol, SessionStoreProtocol
from mh_gateway.context import get_current_request

_db: DatabaseProtocol | None = None
_session_store_factory: (
    Callable[[], Awaitable[SessionStoreProtocol]]
    | Callable[[], SessionStoreProtocol]
    | None
) = None


def set_db(provider: DatabaseProtocol) -> None:
    """Set the global database provider (deprecated; prefer AppState injection)."""
    warnings.warn(
        "set_db() is deprecated. Use app.state.adapters.database_provider instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    global _db
    _db = provider


def get_db() -> DatabaseProtocol:
    """Return the database provider.

    Prefers ``request.app.state.adapters.database_provider`` (via the
    current request context), falling back to the deprecated global.
    """
    req = get_current_request()
    if req is not None:
        provider = getattr(req.app.state.adapters, "database_provider", None)
        if provider is not None:
            return provider
    if _db is not None:
        return _db
    raise RuntimeError("Database not initialized. Did you call init_db() in lifespan?")


def get_db_from_request(request: Request) -> DatabaseProtocol:
    """Return the database provider from the FastAPI request's AppState.

    This is the preferred way to access the database adapter in route handlers.
    """
    provider = getattr(request.app.state.adapters, "database_provider", None)
    if provider is not None:
        return provider
    raise RuntimeError(
        "Database not initialized. "
        "Set app.state.adapters.database_provider in a lifespan hook."
    )


def set_session_store_factory(
    factory: (
        Callable[[], Awaitable[SessionStoreProtocol]]
        | Callable[[], SessionStoreProtocol]
    ),
) -> None:
    """Set the global session store factory (deprecated; prefer AppState injection)."""
    warnings.warn(
        "set_session_store_factory() is deprecated. "
        "Use app.state.adapters.session_store_provider instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    global _session_store_factory
    _session_store_factory = factory


async def get_session_store() -> SessionStoreProtocol:
    """Return a session store instance.

    Prefers ``request.app.state.adapters.session_store_provider`` (via the
    current request context), falling back to the deprecated global factory.
    """
    req = get_current_request()
    if req is not None:
        provider = getattr(req.app.state.adapters, "session_store_provider", None)
        if provider is not None:
            return provider
    if _session_store_factory is not None:
        result = _session_store_factory()
        if inspect.isawaitable(result):
            return await result
        return result
    raise RuntimeError(
        "No session store configured. "
        "Set app.state.adapters.session_store_provider in a lifespan hook."
    )


async def get_session_store_from_request(
    request: Request,
) -> SessionStoreProtocol:
    """Return the session store from the FastAPI request's AppState.

    This is the preferred way to access the session store adapter in route handlers.
    """
    provider = getattr(request.app.state.adapters, "session_store_provider", None)
    if provider is not None:
        return provider
    raise RuntimeError(
        "No session store configured. "
        "Set app.state.adapters.session_store_provider in a lifespan hook."
    )
