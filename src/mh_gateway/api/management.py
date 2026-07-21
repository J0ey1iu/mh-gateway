from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from mh_gateway.api.dependencies import require_permission

logger = logging.getLogger("orchestration.management")

router = APIRouter(prefix="/api/v1/management", tags=["management"])


def _strip(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if not k.startswith("_")}


def _filter_and_page(
    items: list[dict[str, Any]],
    q: str | None = None,
    page: int = 1,
    page_size: int = 0,
    search_fields: list[str] | None = None,
) -> dict[str, Any]:
    if q:
        q_lower = q.lower()
        if search_fields:
            items = [
                item
                for item in items
                if any(q_lower in str(item.get(f, "")).lower() for f in search_fields)
            ]
    total = len(items)
    if page_size > 0:
        start = (page - 1) * page_size
        end = start + page_size
        items = items[start:end]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


class ListResponse(BaseModel):
    items: list[dict[str, Any]]
    total: int
    page: int
    page_size: int


# ── Pydantic request models ──


class ScenarioCreate(BaseModel):
    id: str
    name: str
    name_locale: str = ""
    icon: str = ""
    description: str = ""
    description_locale: str = ""
    agents: list[dict[str, Any]] = []


class ScenarioUpdate(BaseModel):
    name: str | None = None
    name_locale: str | None = None
    icon: str | None = None
    description: str | None = None
    description_locale: str | None = None
    agents: list[dict[str, Any]] | None = None


class AgentCreate(BaseModel):
    name: str
    display_name: str = ""
    display_name_locale: str = ""
    description: str = ""
    description_locale: str = ""
    system_prompt: str = ""
    system_prompt_locale: str = ""
    provider: str = "openai"
    provider_name: str = ""
    model: str = ""
    llm_config: dict[str, Any] = {}
    agent_type: str = "simple"
    compaction: dict[str, Any] = {}


class AgentUpdate(BaseModel):
    display_name: str | None = None
    display_name_locale: str | None = None
    description: str | None = None
    description_locale: str | None = None
    system_prompt: str | None = None
    system_prompt_locale: str | None = None
    provider: str | None = None
    provider_name: str | None = None
    model: str | None = None
    llm_config: dict[str, Any] | None = None
    agent_type: str | None = None
    compaction: dict[str, Any] | None = None


class ToolCreate(BaseModel):
    name: str
    display_name: str = ""
    display_name_locale: str = ""
    description: str = ""
    description_locale: str = ""
    parameters: dict[str, Any] = {}
    endpoint_url: str = ""
    source_code: str = ""


class ToolUpdate(BaseModel):
    display_name: str | None = None
    display_name_locale: str | None = None
    description: str | None = None
    description_locale: str | None = None
    parameters: dict[str, Any] | None = None
    endpoint_url: str | None = None
    source_code: str | None = None


class ModelInfo(BaseModel):
    id: str
    code: str = ""
    display_name: str = ""
    max_context: int = 0


class ProviderCreate(BaseModel):
    name: str
    provider_type: str = "openai"
    api_key: str = ""
    base_url: str = ""
    default_model: str = ""
    description: str = ""
    models: list[ModelInfo] = []


class ProviderUpdate(BaseModel):
    provider_type: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    default_model: str | None = None
    description: str | None = None
    models: list[ModelInfo] | None = None


class AddScenarioAgentRequest(BaseModel):
    agent_name: str
    tool_names: list[str] = []


class AgentToolRequest(BaseModel):
    tool_name: str


# ── Scenarios ──


@router.get("/scenarios")
async def list_scenarios(
    request: Request,
    q: str | None = Query(None, description="Search query"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(0, ge=0, description="Items per page (0 = all)"),
    user_id: str = Depends(require_permission("manage:scene:*")),
) -> ListResponse:
    adapters = request.app.state.adapters
    items = [_strip(s) for s in await adapters.management_provider.list_scenarios()]
    return ListResponse(
        **_filter_and_page(
            items,
            q=q,
            page=page,
            page_size=page_size,
            search_fields=["id", "name", "description", "icon"],
        )
    )


@router.get("/scenarios/{scenario_id}")
async def get_scenario(
    request: Request,
    scenario_id: str,
    user_id: str = Depends(require_permission("manage:scene:*")),
) -> dict[str, Any]:
    adapters = request.app.state.adapters
    s = await adapters.management_provider.get_scenario(scenario_id)
    if s is None:
        raise HTTPException(404, "Scenario not found")
    return _strip(s)


@router.post("/scenarios", status_code=201)
async def create_scenario(
    request: Request,
    body: ScenarioCreate,
    user_id: str = Depends(require_permission("manage:scene:*")),
) -> dict[str, Any]:
    mgmt = request.app.state.adapters.management_provider
    if mgmt is None:
        raise HTTPException(501, "Management provider not configured")
    try:
        payload = body.model_dump()
        payload["created_by"] = user_id
        result = await mgmt.create_scenario(payload)
        logger.info("Scenario created id=%s by user=%s", result.get("id"), user_id)
        return result
    except ValueError as e:
        logger.warning("Create scenario conflict by user=%s: %s", user_id, e)
        raise HTTPException(409, str(e)) from None


@router.put("/scenarios/{scenario_id}")
async def update_scenario(
    request: Request,
    scenario_id: str,
    body: ScenarioUpdate,
    user_id: str = Depends(require_permission("manage:scene:*")),
) -> dict[str, Any]:
    mgmt = request.app.state.adapters.management_provider
    if mgmt is None:
        raise HTTPException(501, "Management provider not configured")
    payload = {k: v for k, v in body.model_dump().items() if v is not None}
    payload["updated_by"] = user_id
    try:
        result = await mgmt.update_scenario(scenario_id, payload)
        logger.info("Scenario updated id=%s by user=%s", scenario_id, user_id)
        return result
    except ValueError as e:
        logger.warning(
            "Update scenario not found id=%s by user=%s: %s", scenario_id, user_id, e
        )
        raise HTTPException(404, str(e)) from None


@router.delete("/scenarios/{scenario_id}")
async def delete_scenario(
    request: Request,
    scenario_id: str,
    user_id: str = Depends(require_permission("manage:scene:*")),
) -> dict[str, str]:
    mgmt = request.app.state.adapters.management_provider
    if mgmt is None:
        raise HTTPException(501, "Management provider not configured")
    try:
        await mgmt.delete_scenario(scenario_id)
        logger.info("Scenario deleted id=%s by user=%s", scenario_id, user_id)
        return {"status": "deleted", "id": scenario_id}
    except ValueError as e:
        logger.warning(
            "Delete scenario not found id=%s by user=%s: %s", scenario_id, user_id, e
        )
        raise HTTPException(404, str(e)) from None


# ── Scenario Agent relationship ──


@router.post("/scenarios/{scenario_id}/agents", status_code=201)
async def add_scenario_agent(
    request: Request,
    scenario_id: str,
    body: AddScenarioAgentRequest,
    user_id: str = Depends(require_permission("manage:scene:*")),
) -> dict[str, Any]:
    mgmt = request.app.state.adapters.management_provider
    if mgmt is None:
        raise HTTPException(501, "Management provider not configured")
    try:
        result = await mgmt.add_scenario_agent(
            scenario_id, body.agent_name, body.tool_names
        )
        logger.info(
            "Agent %s added to scenario %s by user=%s",
            body.agent_name,
            scenario_id,
            user_id,
        )
        return result
    except ValueError as e:
        logger.warning(
            "Add agent to scenario conflict %s/%s by user=%s: %s",
            scenario_id,
            body.agent_name,
            user_id,
            e,
        )
        raise HTTPException(409, str(e)) from None


@router.delete("/scenarios/{scenario_id}/agents/{agent_name}")
async def remove_scenario_agent(
    request: Request,
    scenario_id: str,
    agent_name: str,
    user_id: str = Depends(require_permission("manage:scene:*")),
) -> dict[str, Any]:
    mgmt = request.app.state.adapters.management_provider
    if mgmt is None:
        raise HTTPException(501, "Management provider not configured")
    try:
        result = await mgmt.remove_scenario_agent(scenario_id, agent_name)
        logger.info(
            "Agent %s removed from scenario %s by user=%s",
            agent_name,
            scenario_id,
            user_id,
        )
        return result
    except ValueError as e:
        logger.warning(
            "Remove agent from scenario not found %s/%s by user=%s: %s",
            scenario_id,
            agent_name,
            user_id,
            e,
        )
        raise HTTPException(404, str(e)) from None


@router.post("/scenarios/{scenario_id}/agents/{agent_name}/tools", status_code=201)
async def add_agent_tool(
    request: Request,
    scenario_id: str,
    agent_name: str,
    body: AgentToolRequest,
    user_id: str = Depends(require_permission("manage:scene:*")),
) -> dict[str, Any]:
    mgmt = request.app.state.adapters.management_provider
    if mgmt is None:
        raise HTTPException(501, "Management provider not configured")
    try:
        result = await mgmt.add_agent_tool(scenario_id, agent_name, body.tool_name)
        logger.info(
            "Tool %s added to agent %s in scenario %s by user=%s",
            body.tool_name,
            agent_name,
            scenario_id,
            user_id,
        )
        return result
    except ValueError as e:
        logger.warning(
            "Add tool to agent conflict %s/%s/%s by user=%s: %s",
            scenario_id,
            agent_name,
            body.tool_name,
            user_id,
            e,
        )
        raise HTTPException(409, str(e)) from None


@router.delete("/scenarios/{scenario_id}/agents/{agent_name}/tools/{tool_name}")
async def remove_agent_tool(
    request: Request,
    scenario_id: str,
    agent_name: str,
    tool_name: str,
    user_id: str = Depends(require_permission("manage:scene:*")),
) -> dict[str, Any]:
    mgmt = request.app.state.adapters.management_provider
    if mgmt is None:
        raise HTTPException(501, "Management provider not configured")
    try:
        result = await mgmt.remove_agent_tool(scenario_id, agent_name, tool_name)
        logger.info(
            "Tool %s removed from agent %s in scenario %s by user=%s",
            tool_name,
            agent_name,
            scenario_id,
            user_id,
        )
        return result
    except ValueError as e:
        logger.warning(
            "Remove tool from agent not found %s/%s/%s by user=%s: %s",
            scenario_id,
            agent_name,
            tool_name,
            user_id,
            e,
        )
        raise HTTPException(404, str(e)) from None


# ── Agents ──


@router.get("/agents")
async def list_agents(
    request: Request,
    q: str | None = Query(None, description="Search query"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(0, ge=0, description="Items per page (0 = all)"),
    user_id: str = Depends(require_permission("manage:agent:*")),
) -> ListResponse:
    adapters = request.app.state.adapters
    items = [_strip(a) for a in await adapters.management_provider.list_agents()]
    return ListResponse(
        **_filter_and_page(
            items,
            q=q,
            page=page,
            page_size=page_size,
            search_fields=["name", "display_name", "description"],
        )
    )


@router.get("/agents/{name}")
async def get_agent(
    request: Request,
    name: str,
    user_id: str = Depends(require_permission("manage:agent:*")),
) -> dict[str, Any]:
    adapters = request.app.state.adapters
    a = await adapters.management_provider.get_agent(name)
    if a is None:
        raise HTTPException(404, "Agent not found")
    return _strip(a)


@router.post("/agents", status_code=201)
async def create_agent(
    request: Request,
    body: AgentCreate,
    user_id: str = Depends(require_permission("manage:agent:*")),
) -> dict[str, Any]:
    mgmt = request.app.state.adapters.management_provider
    if mgmt is None:
        raise HTTPException(501, "Management provider not configured")
    try:
        payload = body.model_dump()
        payload["created_by"] = user_id
        result = await mgmt.create_agent(payload)
        logger.info("Agent created name=%s by user=%s", body.name, user_id)
        return result
    except ValueError as e:
        logger.warning(
            "Create agent conflict name=%s by user=%s: %s", body.name, user_id, e
        )
        raise HTTPException(409, str(e)) from None


@router.put("/agents/{name}")
async def update_agent(
    request: Request,
    name: str,
    body: AgentUpdate,
    user_id: str = Depends(require_permission("manage:agent:*")),
) -> dict[str, Any]:
    mgmt = request.app.state.adapters.management_provider
    if mgmt is None:
        raise HTTPException(501, "Management provider not configured")
    payload = {k: v for k, v in body.model_dump().items() if v is not None}
    payload["updated_by"] = user_id
    try:
        result = await mgmt.update_agent(name, payload)
        logger.info("Agent updated name=%s by user=%s", name, user_id)
        return result
    except ValueError as e:
        logger.warning(
            "Update agent not found name=%s by user=%s: %s", name, user_id, e
        )
        raise HTTPException(404, str(e)) from None


@router.delete("/agents/{name}")
async def delete_agent(
    request: Request,
    name: str,
    user_id: str = Depends(require_permission("manage:agent:*")),
) -> dict[str, str]:
    mgmt = request.app.state.adapters.management_provider
    if mgmt is None:
        raise HTTPException(501, "Management provider not configured")
    try:
        await mgmt.delete_agent(name)
        logger.info("Agent deleted name=%s by user=%s", name, user_id)
        return {"status": "deleted", "name": name}
    except ValueError as e:
        logger.warning(
            "Delete agent not found name=%s by user=%s: %s", name, user_id, e
        )
        raise HTTPException(404, str(e)) from None


@router.get("/providers")
async def list_providers(
    request: Request,
    user_id: str = Depends(require_permission("manage:agent:*")),
) -> list[str]:
    adapters = request.app.state.adapters
    registry = getattr(adapters, "llm_provider_registry", None)
    if registry is None:
        return []
    return registry.list_providers()


# ── Provider Configs ──


@router.get("/provider-configs")
async def list_provider_configs(
    request: Request,
    q: str | None = Query(None, description="Search query"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(0, ge=0, description="Items per page (0 = all)"),
    user_id: str = Depends(require_permission("manage:agent:*")),
) -> ListResponse:
    adapters = request.app.state.adapters
    store = getattr(adapters, "llm_provider_store", None)
    if store is None:
        return ListResponse(items=[], total=0, page=page, page_size=page_size)
    items = await store.list_providers()
    return ListResponse(
        **_filter_and_page(
            items,
            q=q,
            page=page,
            page_size=page_size,
            search_fields=["name", "provider_type", "description"],
        )
    )


@router.get("/provider-configs/{name}")
async def get_provider_config(
    request: Request,
    name: str,
    user_id: str = Depends(require_permission("manage:agent:*")),
) -> dict[str, Any]:
    adapters = request.app.state.adapters
    store = getattr(adapters, "llm_provider_store", None)
    if store is None:
        raise HTTPException(501, "Provider store not configured")
    p = await store.get_provider(name)
    if p is None:
        raise HTTPException(404, "Provider config not found")
    return p


@router.post("/provider-configs", status_code=201)
async def create_provider_config(
    request: Request,
    body: ProviderCreate,
    user_id: str = Depends(require_permission("manage:agent:*")),
) -> dict[str, Any]:
    adapters = request.app.state.adapters
    store = getattr(adapters, "llm_provider_store", None)
    if store is None:
        raise HTTPException(501, "Provider store not configured")
    try:
        payload = body.model_dump()
        payload["created_by"] = user_id
        result = await store.create_provider(payload)
        logger.info("Provider config created name=%s by user=%s", body.name, user_id)
        return result
    except ValueError as e:
        logger.warning(
            "Create provider config conflict name=%s by user=%s: %s",
            body.name,
            user_id,
            e,
        )
        raise HTTPException(409, str(e)) from None


@router.put("/provider-configs/{name}")
async def update_provider_config(
    request: Request,
    name: str,
    body: ProviderUpdate,
    user_id: str = Depends(require_permission("manage:agent:*")),
) -> dict[str, Any]:
    adapters = request.app.state.adapters
    store = getattr(adapters, "llm_provider_store", None)
    if store is None:
        raise HTTPException(501, "Provider store not configured")
    payload = {k: v for k, v in body.model_dump().items() if v is not None}
    payload["updated_by"] = user_id
    try:
        result = await store.update_provider(name, payload)
        logger.info("Provider config updated name=%s by user=%s", name, user_id)
        return result
    except ValueError as e:
        logger.warning(
            "Update provider config not found name=%s by user=%s: %s",
            name,
            user_id,
            e,
        )
        raise HTTPException(404, str(e)) from None


@router.delete("/provider-configs/{name}")
async def delete_provider_config(
    request: Request,
    name: str,
    user_id: str = Depends(require_permission("manage:agent:*")),
) -> dict[str, str]:
    adapters = request.app.state.adapters
    store = getattr(adapters, "llm_provider_store", None)
    if store is None:
        raise HTTPException(501, "Provider store not configured")
    try:
        await store.delete_provider(name)
        logger.info("Provider config deleted name=%s by user=%s", name, user_id)
        return {"status": "deleted", "name": name}
    except ValueError as e:
        logger.warning(
            "Delete provider config not found name=%s by user=%s: %s",
            name,
            user_id,
            e,
        )
        raise HTTPException(404, str(e)) from None


# ── Provider Model CRUD ──


def _get_models(provider: dict[str, Any]) -> list[dict[str, Any]]:
    return provider.get("models", [])


@router.get("/provider-configs/{name}/models")
async def list_provider_models(
    request: Request,
    name: str,
    user_id: str = Depends(require_permission("manage:agent:*")),
) -> list[dict[str, Any]]:
    adapters = request.app.state.adapters
    store = getattr(adapters, "llm_provider_store", None)
    if store is None:
        raise HTTPException(501, "Provider store not configured")
    p = await store.get_provider(name)
    if p is None:
        raise HTTPException(404, "Provider config not found")
    return _get_models(p)


@router.post("/provider-configs/{name}/models", status_code=201)
async def create_provider_model(
    request: Request,
    name: str,
    body: ModelInfo,
    user_id: str = Depends(require_permission("manage:agent:*")),
) -> dict[str, Any]:
    adapters = request.app.state.adapters
    store = getattr(adapters, "llm_provider_store", None)
    if store is None:
        raise HTTPException(501, "Provider store not configured")
    p = await store.get_provider(name)
    if p is None:
        raise HTTPException(404, "Provider config not found")
    models = _get_models(p)
    if any(m.get("id") == body.id for m in models):
        raise HTTPException(409, f"Model '{body.id}' already exists")
    model_dict = body.model_dump()
    models.append(model_dict)
    await store.update_provider(name, {"models": models, "updated_by": user_id})
    logger.info("Model %s added to provider %s by user=%s", body.id, name, user_id)
    return model_dict


@router.put("/provider-configs/{name}/models/{model_id}")
async def update_provider_model(
    request: Request,
    name: str,
    model_id: str,
    body: ModelInfo,
    user_id: str = Depends(require_permission("manage:agent:*")),
) -> dict[str, Any]:
    adapters = request.app.state.adapters
    store = getattr(adapters, "llm_provider_store", None)
    if store is None:
        raise HTTPException(501, "Provider store not configured")
    p = await store.get_provider(name)
    if p is None:
        raise HTTPException(404, "Provider config not found")
    if body.id != model_id:
        raise HTTPException(422, "Model id in body does not match path parameter")
    models = _get_models(p)
    for i, m in enumerate(models):
        if m.get("id") == model_id:
            model_dict = body.model_dump()
            models[i] = model_dict
            await store.update_provider(name, {"models": models, "updated_by": user_id})
            logger.info(
                "Model %s updated for provider %s by user=%s",
                model_id,
                name,
                user_id,
            )
            return model_dict
    raise HTTPException(404, f"Model '{model_id}' not found")


@router.delete("/provider-configs/{name}/models/{model_id}")
async def delete_provider_model(
    request: Request,
    name: str,
    model_id: str,
    user_id: str = Depends(require_permission("manage:agent:*")),
) -> dict[str, str]:
    adapters = request.app.state.adapters
    store = getattr(adapters, "llm_provider_store", None)
    if store is None:
        raise HTTPException(501, "Provider store not configured")
    p = await store.get_provider(name)
    if p is None:
        raise HTTPException(404, "Provider config not found")
    models = _get_models(p)
    new_models = [m for m in models if m.get("id") != model_id]
    if len(new_models) == len(models):
        raise HTTPException(404, f"Model '{model_id}' not found")
    await store.update_provider(name, {"models": new_models, "updated_by": user_id})
    logger.info("Model %s deleted from provider %s by user=%s", model_id, name, user_id)
    return {"status": "deleted", "model_id": model_id}


# ── Tools ──


@router.get("/tools")
async def list_tools(
    request: Request,
    q: str | None = Query(None, description="Search query"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(0, ge=0, description="Items per page (0 = all)"),
    user_id: str = Depends(require_permission("manage:tool:*")),
) -> ListResponse:
    adapters = request.app.state.adapters
    items = [_strip(t) for t in await adapters.management_provider.list_tools()]
    return ListResponse(
        **_filter_and_page(
            items,
            q=q,
            page=page,
            page_size=page_size,
            search_fields=["name", "display_name", "description", "endpoint_url"],
        )
    )


@router.get("/tools/{name}")
async def get_tool(
    request: Request,
    name: str,
    user_id: str = Depends(require_permission("manage:tool:*")),
) -> dict[str, Any]:
    adapters = request.app.state.adapters
    t = await adapters.management_provider.get_tool(name)
    if t is None:
        raise HTTPException(404, "Tool not found")
    return _strip(t)


@router.post("/tools", status_code=201)
async def create_tool(
    request: Request,
    body: ToolCreate,
    user_id: str = Depends(require_permission("manage:tool:*")),
) -> dict[str, Any]:
    mgmt = request.app.state.adapters.management_provider
    if mgmt is None:
        raise HTTPException(501, "Management provider not configured")
    try:
        payload = body.model_dump()
        payload["created_by"] = user_id
        result = await mgmt.create_tool(payload)
        logger.info("Tool created name=%s by user=%s", body.name, user_id)
        return result
    except ValueError as e:
        logger.warning(
            "Create tool conflict name=%s by user=%s: %s", body.name, user_id, e
        )
        raise HTTPException(409, str(e)) from None


@router.put("/tools/{name}")
async def update_tool(
    request: Request,
    name: str,
    body: ToolUpdate,
    user_id: str = Depends(require_permission("manage:tool:*")),
) -> dict[str, Any]:
    mgmt = request.app.state.adapters.management_provider
    if mgmt is None:
        raise HTTPException(501, "Management provider not configured")
    payload = {k: v for k, v in body.model_dump().items() if v is not None}
    payload["updated_by"] = user_id
    try:
        result = await mgmt.update_tool(name, payload)
        logger.info("Tool updated name=%s by user=%s", name, user_id)
        return result
    except ValueError as e:
        logger.warning("Update tool not found name=%s by user=%s: %s", name, user_id, e)
        raise HTTPException(404, str(e)) from None


@router.delete("/tools/{name}")
async def delete_tool(
    request: Request,
    name: str,
    user_id: str = Depends(require_permission("manage:tool:*")),
) -> dict[str, str]:
    mgmt = request.app.state.adapters.management_provider
    if mgmt is None:
        raise HTTPException(501, "Management provider not configured")
    try:
        await mgmt.delete_tool(name)
        logger.info("Tool deleted name=%s by user=%s", name, user_id)
        return {"status": "deleted", "name": name}
    except ValueError as e:
        logger.warning("Delete tool not found name=%s by user=%s: %s", name, user_id, e)
        raise HTTPException(404, str(e)) from None
