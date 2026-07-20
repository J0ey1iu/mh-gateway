from __future__ import annotations

from fastapi import HTTPException, Request

from mh_gateway.context import get_current_user_id, set_current_user_id


async def get_user_id(request: Request) -> str:
    """Extract the authenticated user ID from *request*.

    Delegates to the configured ``UserAuthProvider`` which receives the full
    ``Request`` object and may inspect headers, cookies, or call external
    auth services to determine the caller's identity.

    The result is cached in a ``ContextVar`` so that adapters called later
    in the same request can obtain the user ID via
    ``get_current_user_id()``.

    Raises ``401`` if the request is not authenticated.
    """
    cached = get_current_user_id()
    if cached is not None:
        return cached
    adapters = request.app.state.adapters
    identity = await adapters.token_verifier.verify(request)
    if identity is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    if not identity.user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")
    set_current_user_id(identity.user_id)
    request.scope["_user_id"] = identity.user_id
    return identity.user_id
