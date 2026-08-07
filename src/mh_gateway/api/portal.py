"""User-facing portal APIs.

Kept deliberately separate from the management surface (``/api/v1/management/*``):
the portal serves the ordinary user's landing experience (scenario browse /
entry), while management serves admins.  Reused endpoints (auth/me, chat, …)
stay shared; anything homepage-specific lives here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request

from mh_gateway.api.dependencies import get_current_permissions
from mh_gateway.api.locale import parse_locale, resolve_locale

router = APIRouter(prefix="/api/v1/portal", tags=["portal"])


def _scenario_heat(sessions: list[Any]) -> dict[str, int]:
    """Aggregate per-scenario heat from all sessions (cross-user).

    Recent sessions weigh more than old ones: <=30 days x2, older x1.
    Sessions are :class:`SessionSummary` TypedDicts from
    ``list_sessions()``.
    """
    now = datetime.now(UTC)
    heat: dict[str, int] = {}
    for s in sessions:
        sid = s["scenario_id"]
        if not sid:
            continue
        weight = 1
        try:
            created = datetime.fromisoformat(s["created_at"].replace("Z", "+00:00"))
            if (now - created).days <= 30:
                weight = 2
        except (ValueError, TypeError):
            pass  # unparseable timestamp counts as old
        heat[sid] = heat.get(sid, 0) + weight
    return heat


@router.get("/scenarios")
async def list_portal_scenarios(
    request: Request,
    page: int = Query(1, ge=1, description="Page number (1-based)"),
    page_size: int = Query(12, ge=0, le=100, description="Items per page (0 = all)"),
    accept_language: str | None = Header(None, alias="Accept-Language"),
    user_perms: list[str] = Depends(get_current_permissions),
):
    """Scenario list for the portal homepage.

    Every scenario is returned (accessible or not); permission annotation
    is done by the provider's ``list_portal_scenarios`` implementation.
    Accessible scenarios come first, then by heat desc.
    """
    adapters = request.app.state.adapters
    locale = parse_locale(accept_language)

    scenarios = await adapters.metadata.list_portal_scenarios(user_perms, locale)

    sessions = []
    try:
        sessions = await adapters.sessions.list_sessions()
    except Exception:
        # heat is best-effort; a failing session store must not break the list
        sessions = []

    heat = _scenario_heat(sessions)
    for s in scenarios:
        s["heat"] = heat.get(s.get("id", ""), 0)
        s["name"] = resolve_locale(s.get("name", s["id"]), s.get("name_locale"), locale)
        s["description"] = resolve_locale(
            s.get("description", ""), s.get("description_locale"), locale
        )
        # agents：provider 按契约返回 dict（name/display_name(+locale)/description(+locale)），
        # 这里按 Accept-Language 解析出当前语言的展示名与描述
        agents = []
        for a in s.get("agents", []):
            name = str(a.get("name", ""))
            agents.append(
                {
                    "name": name,
                    "display_name": resolve_locale(
                        a.get("display_name", name),
                        a.get("display_name_locale"),
                        locale,
                    ),
                    "description": resolve_locale(
                        a.get("description", ""),
                        a.get("description_locale"),
                        locale,
                    ),
                }
            )
        s["agents"] = agents

    # accessible first, then by heat desc, then name asc for stability
    scenarios.sort(
        key=lambda s: (
            not s.get("accessible", False),
            -s.get("heat", 0),
            s.get("name", ""),
        )
    )

    total = len(scenarios)
    if page_size > 0:
        start = (page - 1) * page_size
        scenarios = scenarios[start : start + page_size]
    return {
        "items": scenarios,
        "total": total,
        "page": page,
        "page_size": page_size,
    }
