from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, Request

from mh_gateway.api.auth import get_user_id as _verify_user
from mh_gateway.context import set_current_user_id

logger = logging.getLogger(__name__)

_X_USER_ID_HEADER = "x-user-id"


async def get_current_user(request: Request) -> str:
    """FastAPI Depends-compatible: returns authenticated user ID from the request."""
    return await _verify_user(request)


async def get_current_permissions(
    request: Request,
    user_id: str = Depends(get_current_user),
) -> list[str]:
    """FastAPI Depends-compatible: returns permission list for the current user."""
    adapters = request.app.state.adapters
    return await adapters.authorization.get_permissions(user_id)


async def verify_m2m_request(request: Request) -> str:
    """FastAPI Depends-compatible: authenticates M2M callers (tools, agent runs)."""
    adapters = request.app.state.adapters
    app_id = await adapters.m2m_auth.authenticate(request)
    if app_id is None:
        raise HTTPException(status_code=401, detail="M2M authentication required")
    return app_id


async def resolve_m2m_identity(request: Request) -> str:
    """Resolve effective user identity for M2M requests.

    Authenticates the M2M caller, then checks for the ``x-user-id`` header
    injected by the upstream outbound auth provider.  When present, the user
    ID carried by that header is used for permission checks, session ownership,
    and downstream identity propagation — matching the end-user's permission
    set rather than the calling application's.

    Falls back to *app_id* when ``x-user-id`` is absent (backwards compatible).

    Sets the ``current_user_id`` ContextVar so ``get_current_user_id()`` works
    in the M2M context as well.
    """
    adapters = request.app.state.adapters
    app_id = await adapters.m2m_auth.authenticate(request)
    if app_id is None:
        raise HTTPException(status_code=401, detail="M2M authentication required")

    x_user_id = request.headers.get(_X_USER_ID_HEADER, "").strip()
    if x_user_id:
        logger.debug(
            "M2M identity resolved via x-user-id: app_id=%s user_id=%s",
            app_id,
            x_user_id,
        )
        set_current_user_id(x_user_id)
        return x_user_id

    logger.debug("M2M identity resolved to app_id: app_id=%s", app_id)
    return app_id


def require_permission(permission: str):
    """FastAPI Depends factory: require a specific permission for the current user.

    Usage::

        @router.get("/scenarios")
        async def list_scenarios(
            user_id: str = Depends(require_permission("manage:scene:*")),
        ):
            ...
    """

    async def _check(
        request: Request,
        user_id: str = Depends(get_current_user),
    ) -> str:
        adapters = request.app.state.adapters
        ok = await adapters.authorization.check(user_id, permission)
        if not ok:
            raise HTTPException(
                status_code=403,
                detail=f"Permission denied: {permission}",
            )
        return user_id

    return _check


async def resolve_request_identity(request: Request) -> str:
    """Try user auth first, fall back to M2M auth.

    Returns the authenticated identity (user_id or app_id).
    Raises 401 only if both user auth and M2M auth fail.
    """
    try:
        return await _verify_user(request)
    except HTTPException:
        pass
    adapters = request.app.state.adapters
    app_id = await adapters.m2m_auth.authenticate(request)
    if app_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return app_id


async def resolve_request_permissions(
    request: Request,
    identity: str = Depends(resolve_request_identity),
) -> list[str]:
    """Get permissions for the resolved identity (user or M2M app)."""
    adapters = request.app.state.adapters
    return await adapters.authorization.get_permissions(identity)
