from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request

from mh_gateway.api.dependencies import (
    get_current_permissions,
    get_current_user,
)
from mh_gateway.api.locale import (
    parse_locale,
    resolve_description,
    resolve_display_name,
)
from mh_gateway.adapters import has_broad_permission, match_permission

router = APIRouter(prefix="/api/v1/tools", tags=["tools"])


@router.get("")
async def list_tools(
    request: Request,
    accept_language: str | None = Header(None, alias="Accept-Language"),
    user_id: str = Depends(get_current_user),
    user_perms: list[str] = Depends(get_current_permissions),
):
    adapters = request.app.state.adapters
    locale = parse_locale(accept_language)
    tools = await adapters.management_provider.list_tools()
    if has_broad_permission(user_perms, "use:tool"):
        return [
            {
                "name": t["name"],
                "display_name": resolve_display_name(
                    t.get("display_name", t["name"]),
                    t.get("display_name_locale"),
                    locale,
                ),
                "description": resolve_description(
                    t.get("description", ""),
                    t.get("description_locale"),
                    locale,
                ),
            }
            for t in tools
        ]
    result = []
    for t in tools:
        if not match_permission(user_perms, f"use:tool:{t['name']}"):
            continue
        result.append(
            {
                "name": t["name"],
                "display_name": resolve_display_name(
                    t.get("display_name", t["name"]),
                    t.get("display_name_locale"),
                    locale,
                ),
                "description": resolve_description(
                    t.get("description", ""),
                    t.get("description_locale"),
                    locale,
                ),
            }
        )
    return result
