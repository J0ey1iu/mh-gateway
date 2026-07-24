from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from mh_service_kit.sse import serialize_event
from minimal_harness.agent.factory import AgentFactory
from minimal_harness.memory import ConversationMemory
from minimal_harness.tool.factory import DefaultToolFactory
from minimal_harness.types import AgentMetadata, ToolMetadata

from mh_gateway.api.dependencies import (
    get_current_permissions,
    get_current_user,
    resolve_m2m_identity,
)
from mh_gateway.api.locale import (
    parse_locale,
    resolve_description,
    resolve_display_name,
)
from mh_gateway.adapters import (
    LLMResolveSpec,
    has_broad_permission,
    match_permission,
)
from mh_gateway.services.runtime_service import _tool_binding

logger = logging.getLogger("orchestration.agents")

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])


@router.get("")
async def list_agents(
    request: Request,
    scenario: str | None = Query(None),
    accept_language: str | None = Header(None, alias="Accept-Language"),
    user_id: str = Depends(get_current_user),
    user_perms: list[str] = Depends(get_current_permissions),
):
    logger.debug(
        "INBOUND list_agents request — scenario=%s user=%s locale=%s",
        scenario,
        user_id,
        accept_language,
    )
    adapters = request.app.state.adapters
    locale = parse_locale(accept_language)
    agents = await adapters.metadata.list_agents()

    if scenario:
        from mh_gateway.api.scenarios import (
            _enrich_agents_for_scenario,
            _get_scenario,
        )

        s = await _get_scenario(request, scenario)
        if s is None:
            return []
        return await _enrich_agents_for_scenario(request, s, user_perms, locale)

    if has_broad_permission(user_perms, "use:agent"):
        return [
            {
                "name": a["name"],
                "display_name": resolve_display_name(
                    a.get("display_name", a["name"]),
                    a.get("display_name_locale"),
                    locale,
                ),
                "description": resolve_description(
                    a.get("description", ""),
                    a.get("description_locale"),
                    locale,
                ),
                "tool_names": [],
                "tools": [],
                "provider": a.get("provider", "openai"),
                "model": a.get("model", ""),
            }
            for a in agents
        ]
    result = []
    for a in agents:
        if not match_permission(user_perms, f"use:agent:{a['name']}"):
            continue
        result.append(
            {
                "name": a["name"],
                "display_name": resolve_display_name(
                    a.get("display_name", a["name"]),
                    a.get("display_name_locale"),
                    locale,
                ),
                "description": resolve_description(
                    a.get("description", ""),
                    a.get("description_locale"),
                    locale,
                ),
                "tool_names": [],
                "tools": [],
                "provider": a.get("provider", "openai"),
                "model": a.get("model", ""),
            }
        )
    return result


@router.post("/{agent_name}/run")
async def run_agent(
    agent_name: str,
    request: Request,
    body: dict[str, Any],
    identity: str = Depends(resolve_m2m_identity),
):
    logger.debug(
        "INBOUND run_agent request — agent=%s identity=%s user_input_count=%d tools_count=%d memory_count=%d",
        agent_name,
        identity,
        len(body.get("user_input", [])),
        len(body.get("tools", [])),
        len(body.get("memory", [])),
    )
    adapters = request.app.state.adapters

    agent_meta = await adapters.metadata.get_agent(agent_name)
    if agent_meta is None:
        raise HTTPException(404, f"Agent '{agent_name}' not found")

    memory = ConversationMemory()
    for msg in body.get("memory", []):
        await memory.add_message(msg)

    tools = []
    tool_schemas: dict[str, dict] = {}
    for schema in body.get("tools", []):
        func = schema.get("function", schema)
        tool_name = func.get("name", "")
        if tool_name:
            tool_schemas[tool_name] = func

    tools_map = await adapters.metadata.get_tools(list(tool_schemas))

    for tool_name, tool_meta in tools_map.items():
        if tool_meta is None:
            continue
        func = tool_schemas[tool_name]
        tm = ToolMetadata(
            name=tool_name,
            display_name=tool_meta.get("display_name", tool_name),
            description=tool_meta.get("description", ""),
            parameters=tool_meta.get("parameters", func.get("parameters", {})),
            binding=await _tool_binding(
                tool_meta,
                tool_name,
                request,
                identity=identity or "",
                outbound_auth=adapters.outbound_auth,
                verify_agent_tool_ssl=getattr(
                    adapters.settings, "verify_agent_tool_ssl", False
                ),
            ),
        )
        try:
            tools.append(DefaultToolFactory().create(tm))
        except ValueError:
            logger.warning(
                "Failed to create tool '%s': missing endpoint_url or local implementation",
                tool_name,
            )

    target_meta = AgentMetadata(
        name=agent_name,
        agent_type="simple",
        provider=agent_meta.get("provider", "openai"),
        model=agent_meta.get("model", ""),
        llm_config=agent_meta.get("llm_config", {}),
    )
    _agent_llm_resolver = await adapters.llm.build_resolver(
        [LLMResolveSpec(agent=target_meta, user=identity or "")]
    )

    factory = AgentFactory(
        llm_provider_resolver=_agent_llm_resolver,
    )
    agent = factory.create(
        AgentMetadata(
            name=agent_name,
            agent_type="simple",
            provider=agent_meta.get("provider", "openai"),
            model=agent_meta.get("model", ""),
            llm_config=agent_meta.get("llm_config", {}),
        ),
        max_iterations=10,
        emit_message_events=True,
    )

    system_prompt = body.get("system_prompt", "") or agent_meta.get("system_prompt", "")

    async def event_stream():
        async for event in agent.run(
            user_input=body.get("user_input", []),
            stop_event=None,
            memory=memory,
            tools=tools,
            system_prompt=system_prompt,
            context=body.get("context", {}),
        ):
            serialized = serialize_event(event)
            logger.debug(
                "OUTBOUND event — agent=%s event_type=%s serialized_keys=%s",
                agent_name,
                type(event).__name__,
                list(serialized.keys()) if isinstance(serialized, dict) else [],
            )
            yield serialized

    return StreamingResponse(event_stream(), media_type="text/event-stream")
