from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

auth_router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _user_info(identity, permissions: list[str] | None = None) -> dict:
    roles = [{"name": r} for r in getattr(identity, "roles", [])]
    if not roles:
        roles = [{"name": identity.user_id}]
    result: dict = {
        "id": identity.user_id,
        "username": identity.username or identity.user_id,
        "is_active": True,
        "roles": roles,
    }
    if permissions is not None:
        result["permissions"] = permissions
    return result


@auth_router.get("/me")
async def me(request: Request):
    adapters = request.app.state.adapters
    identity = await adapters.user_auth.verify(request)
    if identity is None or not identity.user_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    perms = await adapters.authorization.get_permissions(identity.user_id)
    return _user_info(identity, permissions=perms)


@auth_router.post("/logout")
async def auth_logout(request: Request):
    adapters = request.app.state.adapters
    response = JSONResponse({"success": True})
    await adapters.user_auth.logout(request, response)
    return response
