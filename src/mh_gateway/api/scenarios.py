from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from mh_gateway.api.dependencies import (
    get_current_permissions,
    get_current_user,
)
from mh_gateway.api.locale import (
    parse_locale,
    resolve_description,
    resolve_display_name,
    resolve_locale,
)
from mh_gateway.adapters import has_broad_permission, match_permission

router = APIRouter(prefix="/api/v1/scenarios", tags=["scenarios"])


async def _load_scenarios(request: Request) -> list[dict]:
    adapters = request.app.state.adapters
    return await adapters.management_provider.list_scenarios()


async def _get_scenario(request: Request, scenario_id: str) -> dict | None:
    adapters = request.app.state.adapters
    return await adapters.management_provider.get_scenario(scenario_id)


async def _enrich_agents_for_scenario(
    request: Request,
    scenario: dict,
    user_perms: list[str],
    locale: str,
) -> list[dict]:
    adapters = request.app.state.adapters
    scenario_agents = list(dict.fromkeys(a["name"] for a in scenario.get("agents", [])))
    scenario_tools: dict[str, list[str]] = {}
    all_tool_names: set[str] = set()
    for a in scenario.get("agents", []):
        tools = a.get("tool_names", [])
        scenario_tools[a["name"]] = tools
        all_tool_names.update(tools)

    # Only fetch agents in this scenario
    batch_get_agents = getattr(adapters.management_provider, "get_agents", None)
    if batch_get_agents:
        agents_map = await batch_get_agents(list(scenario_agents))
        agents = [v for v in agents_map.values() if v is not None]
    else:
        agents = [
            a
            for a in [
                await adapters.management_provider.get_agent(name)
                for name in scenario_agents
            ]
            if a is not None
        ]

    # Only fetch tools referenced by scenario agents
    batch_get_tools = getattr(adapters.management_provider, "get_tools", None)
    if batch_get_tools:
        tool_map = await batch_get_tools(list(all_tool_names))
    else:
        tool_map = {}
        for tname in all_tool_names:
            meta = await adapters.management_provider.get_tool(tname)
            if meta is not None:
                tool_map[tname] = meta

    result = []
    for agent in agents:
        name = agent["name"]
        if not match_permission(user_perms, f"use:agent:{name}"):
            continue
        tools = [
            t
            for t in scenario_tools.get(name, [])
            if match_permission(user_perms, f"use:tool:{t}")
        ]
        result.append(
            {
                "name": name,
                "display_name": resolve_display_name(
                    agent.get("display_name", name),
                    agent.get("display_name_locale"),
                    locale,
                ),
                "description": resolve_description(
                    agent.get("description", ""),
                    agent.get("description_locale"),
                    locale,
                ),
                "tool_names": tools,
                "provider": agent.get("provider", "openai"),
                "model": agent.get("model", ""),
                "tools": [
                    {
                        "name": t,
                        "display_name": resolve_display_name(
                            tool_map[t].get("display_name", t),
                            tool_map[t].get("display_name_locale"),
                            locale,
                        )
                        if t in tool_map
                        else t,
                    }
                    for t in tools
                ],
            }
        )
    return result


@router.get("")
async def list_scenarios(
    request: Request,
    accept_language: str | None = Header(None, alias="Accept-Language"),
    user_id: str = Depends(get_current_user),
    user_perms: list[str] = Depends(get_current_permissions),
):
    locale = parse_locale(accept_language)

    result = []
    for s in await _load_scenarios(request):
        if not has_broad_permission(user_perms, "use:scene") and not match_permission(
            user_perms, f"use:scene:{s['id']}"
        ):
            continue
        agent_names = [a["name"] for a in s.get("agents", [])]
        if has_broad_permission(user_perms, "use:agent"):
            visible_agents = agent_names
        else:
            visible_agents = [
                name
                for name in agent_names
                if match_permission(user_perms, f"use:agent:{name}")
            ]
        if not visible_agents:
            continue
        result.append(
            {
                "id": s["id"],
                "name": resolve_locale(
                    s.get("name", s["id"]), s.get("name_locale"), locale
                ),
                "icon": s.get("icon", ""),
                "description": resolve_locale(
                    s.get("description", ""), s.get("description_locale"), locale
                ),
                "agents": visible_agents,
            }
        )
    return result


@router.get("/{scenario_id}")
async def get_scenario_detail(
    scenario_id: str,
    request: Request,
    accept_language: str | None = Header(None, alias="Accept-Language"),
    user_id: str = Depends(get_current_user),
    user_perms: list[str] = Depends(get_current_permissions),
):
    locale = parse_locale(accept_language)

    s = await _get_scenario(request, scenario_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Scenario not found")
    if not match_permission(user_perms, f"use:scene:{scenario_id}"):
        raise HTTPException(status_code=403, detail="Access denied")

    enriched = await _enrich_agents_for_scenario(request, s, user_perms, locale)
    return {
        "id": s["id"],
        "name": resolve_locale(s.get("name", s["id"]), s.get("name_locale"), locale),
        "icon": s.get("icon", ""),
        "description": resolve_locale(
            s.get("description", ""), s.get("description_locale"), locale
        ),
        "agents": enriched,
    }
