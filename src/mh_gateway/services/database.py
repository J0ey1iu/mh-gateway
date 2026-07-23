"""Session repository accessor.

The previous design carried two global backings (``_db`` and
``_session_store_factory``) as a transitional fallback.  In this
release there is exactly one source of truth — the
:class:`~mh_gateway.app.GatewayAdapters` bundle hanging off
``app.state.adapters`` — and the helper functions here simply read
from it.

Legacy code paths (``set_db`` / ``set_session_store_factory``) were
removed alongside the rest of the deprecated public surface.
"""

from __future__ import annotations

from fastapi import Request

from mh_gateway.adapters import SessionRepository


def get_adapters(request: Request):
    """Return the immutable adapter bundle for *request*.

    Imported lazily to avoid a circular dependency between
    :mod:`mh_gateway.app` and :mod:`mh_gateway.services`.
    """
    from mh_gateway.app import GatewayAdapters

    bundle = getattr(request.app.state, "adapters", None)
    if not isinstance(bundle, GatewayAdapters):
        raise RuntimeError(
            "GatewayAdapters not initialised. The FastAPI app was not built "
            "with mh_gateway.app.create_app()."
        )
    return bundle


def get_session_repo(request: Request) -> SessionRepository:
    """Return the active :class:`SessionRepository` for the request."""
    return get_adapters(request).sessions


async def get_session_store(request: Request) -> SessionRepository:
    """Async alias kept for API parity with the previous release."""
    return get_session_repo(request)
