from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from mh_gateway.adapters import Feedback, LLMResolveSpec

from mh_gateway.api.dependencies import get_current_user, require_permission
from mh_gateway.api.imports import (
    ImportFileResult,
    ImportIssue,
    build_export_payload,
    get_template,
    sanitize_filename,
    validate_import_content,
)
from mh_gateway.llm import LLMProviderConfig
from mh_gateway.services.runtime_service import build_controller_registry
from minimal_harness.agent.factory import get_builtin_agent_type_schemas
from minimal_harness.tool.script_parser import parse_tool_script

logger = logging.getLogger("orchestration.management")

# UTF-8 BOM so Excel opens the CSV with correct Chinese encoding.
_csv_bom = "\ufeff"

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
    show_on_homepage: bool = True


class ScenarioUpdate(BaseModel):
    name: str | None = None
    name_locale: str | None = None
    icon: str | None = None
    description: str | None = None
    description_locale: str | None = None
    agents: list[dict[str, Any]] | None = None
    show_on_homepage: bool | None = None


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
    tool_compaction: dict[str, Any] = {}


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
    tool_compaction: dict[str, Any] | None = None


class ToolCreate(BaseModel):
    name: str
    display_name: str = ""
    display_name_locale: str = ""
    description: str = ""
    description_locale: str = ""
    parameters: dict[str, Any] = {}
    endpoint_url: str = ""
    source_code: str = ""
    script_path: str = ""


class ToolUpdate(BaseModel):
    display_name: str | None = None
    display_name_locale: str | None = None
    description: str | None = None
    description_locale: str | None = None
    parameters: dict[str, Any] | None = None
    endpoint_url: str | None = None
    source_code: str | None = None
    script_path: str | None = None


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


class ProviderTestRequest(BaseModel):
    model: str = ""


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
    items = [_strip(s) for s in await adapters.metadata.list_scenarios()]
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
    s = await adapters.metadata.get_scenario(scenario_id)
    if s is None:
        raise HTTPException(404, "Scenario not found")
    return _strip(s)


@router.post("/scenarios", status_code=201)
async def create_scenario(
    request: Request,
    body: ScenarioCreate,
    user_id: str = Depends(require_permission("manage:scene:*")),
) -> dict[str, Any]:
    mgmt = request.app.state.adapters.metadata
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
    mgmt = request.app.state.adapters.metadata
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
    mgmt = request.app.state.adapters.metadata
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
    mgmt = request.app.state.adapters.metadata
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
    mgmt = request.app.state.adapters.metadata
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
    mgmt = request.app.state.adapters.metadata
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
    mgmt = request.app.state.adapters.metadata
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
    items = [_strip(a) for a in await adapters.metadata.list_agents()]
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
    a = await adapters.metadata.get_agent(name)
    if a is None:
        raise HTTPException(404, "Agent not found")
    return _strip(a)


@router.post("/agents", status_code=201)
async def create_agent(
    request: Request,
    body: AgentCreate,
    user_id: str = Depends(require_permission("manage:agent:*")),
) -> dict[str, Any]:
    mgmt = request.app.state.adapters.metadata
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
    mgmt = request.app.state.adapters.metadata
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
    mgmt = request.app.state.adapters.metadata
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


@router.get("/agent-types")
async def list_agent_types(
    user_id: str = Depends(require_permission("manage:agent:*")),
) -> list[dict[str, Any]]:
    return get_builtin_agent_type_schemas()


# ── Controllers ────────────────────────────────────────────────────────────


@router.get("/controllers")
async def list_controllers(
    request: Request,
) -> list[dict[str, Any]]:
    """ChatRequest.controller 可选的 Controller 目录（无需权限）。

    类型列表与展示元数据来自 controller registry 的 ``catalog()``
    （``build_controller_registry`` 注册时一并登记，单一来源）。
    """
    settings = request.app.state.adapters.settings
    return build_controller_registry(settings).catalog()


@router.get("/providers")
async def list_providers(
    request: Request,
    user_id: str = Depends(require_permission("manage:agent:*")),
) -> list[str]:
    return request.app.state.adapters.llm.list_provider_types()


# ── Provider Configs ──


@router.get("/provider-configs")
async def list_provider_configs(
    request: Request,
    q: str | None = Query(None, description="Search query"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(0, ge=0, description="Items per page (0 = all)"),
    user_id: str = Depends(require_permission("manage:agent:*")),
) -> ListResponse:
    items = [c.to_dict() for c in await request.app.state.adapters.llm.list_configs()]
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
    cfg = await request.app.state.adapters.llm.get_config(name)
    if cfg is None:
        raise HTTPException(404, "Provider config not found")
    return cfg.to_dict()


@router.post("/provider-configs", status_code=201)
async def create_provider_config(
    request: Request,
    body: ProviderCreate,
    user_id: str = Depends(require_permission("manage:agent:*")),
) -> dict[str, Any]:
    try:
        payload = body.model_dump()
        payload["created_by"] = user_id
        result = await request.app.state.adapters.llm.create_config(
            LLMProviderConfig.from_dict(payload)
        )
        logger.info("Provider config created name=%s by user=%s", body.name, user_id)
        return result.to_dict()
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
    payload = {k: v for k, v in body.model_dump().items() if v is not None}
    payload["updated_by"] = user_id
    try:
        cfg = await request.app.state.adapters.llm.get_config(name)
        if cfg is None:
            raise ValueError(f"Provider '{name}' not found")
        merged = LLMProviderConfig.from_dict({**cfg.to_dict(), **payload, "name": name})
        result = await request.app.state.adapters.llm.update_config(name, merged)
        logger.info("Provider config updated name=%s by user=%s", name, user_id)
        return result.to_dict()
    except ValueError as e:
        logger.warning(
            "Update provider config not found name=%s by user=%s: %s",
            name,
            user_id,
            e,
        )
        raise HTTPException(404, str(e)) from None


@router.post("/provider-configs/{name}/test")
async def test_provider_config(
    request: Request,
    name: str,
    body: ProviderTestRequest,
    user_id: str = Depends(require_permission("manage:agent:*")),
) -> dict[str, Any]:
    """验证已保存的 provider + 绑定模型能否联通（真实发起一次最小 LLM 调用）。

    用存储的 api_key/base_url + 指定 model（或 default_model）构造 provider，
    发送一条 "ping"，完整消费响应流——连接失败、鉴权失败、模型无效都会抛异常。
    """
    llm = request.app.state.adapters.llm
    cfg = await llm.get_config(name)
    if cfg is None:
        raise HTTPException(404, "Provider config not found")
    model = (body.model or cfg.default_model or "").strip()
    if not model:
        raise HTTPException(
            400, "No model to test: pass one or configure default_model"
        )
    if not cfg.api_key:
        raise HTTPException(400, "Provider has no api_key configured")

    started = time.monotonic()
    try:
        provider = await llm.create_llm(
            LLMResolveSpec(
                agent=SimpleNamespace(provider_name=name, provider="", model=model),
                user=user_id,
            )
        )

        async def _probe() -> None:
            stream = await provider.chat(
                [{"role": "user", "content": "ping"}], tools=[]
            )
            async for _ in stream:
                pass  # 完整消费：连接/鉴权/模型错误在此抛出

        await asyncio.wait_for(_probe(), timeout=15)
    except asyncio.TimeoutError:
        return {"ok": False, "model": model, "message": "Connection timed out (15s)"}
    except Exception as e:
        logger.warning("Provider connection test failed name=%s: %s", name, e)
        return {"ok": False, "model": model, "message": str(e)[:300]}
    duration_ms = round((time.monotonic() - started) * 1000)
    logger.info(
        "Provider connection test ok name=%s model=%s (%d ms)", name, model, duration_ms
    )
    return {"ok": True, "model": model, "message": f"Connected ({duration_ms} ms)"}


@router.delete("/provider-configs/{name}")
async def delete_provider_config(
    request: Request,
    name: str,
    user_id: str = Depends(require_permission("manage:agent:*")),
) -> dict[str, str]:
    try:
        await request.app.state.adapters.llm.delete_config(name)
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
    cfg = await request.app.state.adapters.llm.get_config(name)
    if cfg is None:
        raise HTTPException(404, "Provider config not found")
    return _get_models(cfg.to_dict())


@router.post("/provider-configs/{name}/models", status_code=201)
async def create_provider_model(
    request: Request,
    name: str,
    body: ModelInfo,
    user_id: str = Depends(require_permission("manage:agent:*")),
) -> dict[str, Any]:
    cfg = await request.app.state.adapters.llm.get_config(name)
    if cfg is None:
        raise HTTPException(404, "Provider config not found")
    models = _get_models(cfg.to_dict())
    if any(m.get("id") == body.id for m in models):
        raise HTTPException(409, f"Model '{body.id}' already exists")
    model_dict = body.model_dump()
    models.append(model_dict)
    merged = LLMProviderConfig.from_dict(
        {**cfg.to_dict(), "models": models, "updated_by": user_id}
    )
    await request.app.state.adapters.llm.update_config(name, merged)
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
    cfg = await request.app.state.adapters.llm.get_config(name)
    if cfg is None:
        raise HTTPException(404, "Provider config not found")
    if body.id != model_id:
        raise HTTPException(422, "Model id in body does not match path parameter")
    models = _get_models(cfg.to_dict())
    for i, m in enumerate(models):
        if m.get("id") == model_id:
            model_dict = body.model_dump()
            models[i] = model_dict
            merged = LLMProviderConfig.from_dict(
                {**cfg.to_dict(), "models": models, "updated_by": user_id}
            )
            await request.app.state.adapters.llm.update_config(name, merged)
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
    cfg = await request.app.state.adapters.llm.get_config(name)
    if cfg is None:
        raise HTTPException(404, "Provider config not found")
    models = _get_models(cfg.to_dict())
    new_models = [m for m in models if m.get("id") != model_id]
    if len(new_models) == len(models):
        raise HTTPException(404, f"Model '{model_id}' not found")
    merged = LLMProviderConfig.from_dict(
        {**cfg.to_dict(), "models": new_models, "updated_by": user_id}
    )
    await request.app.state.adapters.llm.update_config(name, merged)
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
    items = [_strip(t) for t in await adapters.metadata.list_tools()]
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
    t = await adapters.metadata.get_tool(name)
    if t is None:
        raise HTTPException(404, "Tool not found")
    return _strip(t)


@router.post("/tools", status_code=201)
async def create_tool(
    request: Request,
    body: ToolCreate,
    user_id: str = Depends(require_permission("manage:tool:*")),
) -> dict[str, Any]:
    mgmt = request.app.state.adapters.metadata
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
    mgmt = request.app.state.adapters.metadata
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
    force: bool = Query(
        False, description="Also remove tool from all scenarios/agents"
    ),
    user_id: str = Depends(require_permission("manage:tool:*")),
) -> dict[str, str]:
    mgmt = request.app.state.adapters.metadata
    if mgmt is None:
        raise HTTPException(501, "Management provider not configured")
    try:
        tool = await mgmt.get_tool(name)
        if tool is None:
            raise HTTPException(404, f"Tool '{name}' not found")

        # ── check scenario/agent relationships ──
        usages: list[dict[str, str]] = []
        for s in await mgmt.list_scenarios():
            for a in s.get("agents", []):
                if name in a.get("tool_names", []):
                    usages.append({"scenario_id": s["id"], "agent_name": a["name"]})
        if usages and not force:
            raise HTTPException(
                409,
                detail={
                    "message": (
                        f"Tool '{name}' is still referenced by {len(usages)} "
                        "scenario/agent(s). Use ?force=true to auto-remove "
                        "these bindings and proceed."
                    ),
                    "usages": usages,
                },
            )

        if usages and force:
            for u in usages:
                try:
                    await mgmt.remove_agent_tool(
                        u["scenario_id"], u["agent_name"], name
                    )
                except Exception:
                    logger.warning(
                        "Failed to remove tool %s from scenario %s agent %s",
                        name,
                        u["scenario_id"],
                        u["agent_name"],
                    )

        script_path = tool.get("script_path", "") if tool else ""
        await mgmt.delete_tool(name)
        logger.info("Tool deleted name=%s by user=%s", name, user_id)
        if script_path:
            script_store = request.app.state.adapters.tool_script_store
            if script_store is not None:
                script_name = Path(script_path).name
                await script_store.delete(script_name)
        return {"status": "deleted", "name": name}
    except HTTPException:
        raise
    except ValueError as e:
        logger.warning("Delete tool not found name=%s by user=%s: %s", name, user_id, e)
        raise HTTPException(404, str(e)) from None


# ── Tool Upload ──


class UploadToolResult(BaseModel):
    tool: dict[str, Any]
    name: str
    script_path: str


class UploadToolsResponse(BaseModel):
    created: list[UploadToolResult]
    errors: list[dict[str, str]]


async def _process_uploaded_script(
    mgmt: Any,
    script_store: Any,
    filename: str,
    content: bytes,
    overwrite: bool,
    user_id: str,
) -> UploadToolResult:
    if not overwrite and await script_store.exists(filename):
        raise ValueError(f"Script '{filename}' already exists")

    script_path = await script_store.save(filename, content, overwrite=overwrite)

    tmp_dir = Path(script_path).parent
    tmp_file = tmp_dir / filename
    tmp_file.write_bytes(content)

    parse_result = parse_tool_script(tmp_file)
    if not parse_result.is_valid:
        await script_store.delete(filename)
        raise ValueError(f"Invalid tool script: {'; '.join(parse_result.errors)}")

    existing = await mgmt.get_tool(parse_result.name)
    if existing:
        await script_store.delete(filename)
        raise ValueError(
            f"Tool '{parse_result.name}' already exists "
            "(name must be unique across all bindings)"
        )

    tool_payload = {
        "name": parse_result.name,
        "display_name": parse_result.display_name,
        "display_name_locale": (
            json.dumps(parse_result.display_name_locale, ensure_ascii=False)
            if parse_result.display_name_locale
            else ""
        ),
        "description": parse_result.description,
        "description_locale": (
            json.dumps(parse_result.description_locale, ensure_ascii=False)
            if parse_result.description_locale
            else ""
        ),
        "parameters": parse_result.parameters,
        "script_path": script_path,
        "created_by": user_id,
    }
    created = await mgmt.create_tool(tool_payload)
    logger.info(
        "Tool uploaded from script name=%s path=%s by user=%s",
        parse_result.name,
        script_path,
        user_id,
    )
    return UploadToolResult(
        tool=created,
        name=parse_result.name,
        script_path=script_path,
    )


@router.post("/tools/upload", status_code=201)
async def upload_tool_script(
    request: Request,
    file: UploadFile,
    overwrite: bool = Query(False),
    user_id: str = Depends(require_permission("manage:tool:*")),
) -> UploadToolResult:
    adapters = request.app.state.adapters
    mgmt = adapters.metadata
    script_store = adapters.tool_script_store
    if mgmt is None or script_store is None:
        raise HTTPException(501, "Tool script upload not configured")

    if not file.filename or not file.filename.endswith(".py"):
        raise HTTPException(422, "Only .py files are accepted")

    content = await file.read()

    try:
        return await _process_uploaded_script(
            mgmt, script_store, file.filename, content, overwrite, user_id
        )
    except ValueError as e:
        raise HTTPException(422, str(e)) from None


# ── JSON Import / Templates ──


_IMPORT_PERMISSIONS = {
    "scene": "manage:scene:*",
    "agent": "manage:agent:*",
    "tool": "manage:tool:*",
}


async def _require_import_permissions(
    request: Request, user_id: str, entity_types: set[str]
) -> None:
    """Require the manage:* permission for every entity type being imported.

    A single import batch may mix scene / agent / tool files, so the
    permission is checked per type instead of one global gate.
    """
    adapters = request.app.state.adapters
    for t in sorted(entity_types):
        perm = _IMPORT_PERMISSIONS[t]
        ok = await adapters.authorization.check(user_id, perm)
        if not ok:
            raise HTTPException(status_code=403, detail=f"Permission denied: {perm}")


@router.get("/import/templates/{entity_type}")
async def download_import_template(
    request: Request,
    entity_type: str,
    user_id: str = Depends(get_current_user),
) -> Response:
    """Download a JSON import template for scene / agent / tool."""
    template = get_template(entity_type)
    if template is None:
        raise HTTPException(404, f"Unknown entity type: {entity_type}")
    await _require_import_permissions(request, user_id, {entity_type})
    return _export_response(
        json.dumps(template, ensure_ascii=False, indent=2) + "\n",
        f"{entity_type}-template.json",
    )


@router.post("/import", status_code=201)
async def import_entities(
    request: Request,
    files: list[UploadFile],
    user_id: str = Depends(get_current_user),
) -> dict[str, Any]:
    """Import scene / agent / tool entities from JSON files (batch, atomic).

    Every file must pass syntax + structure validation, otherwise the
    whole batch is rejected and nothing is created.  Relationship fields
    (scene.agents, agent.tool_names) are not importable.
    """
    mgmt = request.app.state.adapters.metadata
    if mgmt is None:
        raise HTTPException(501, "Management provider not configured")
    if not files:
        raise HTTPException(422, "No files provided")

    results: list[ImportFileResult] = []
    for f in files:
        filename = f.filename or "(unknown)"
        if not filename.lower().endswith(".json"):
            r = ImportFileResult(filename=filename, ok=False)
            r.issues.append(
                ImportIssue(
                    path="$",
                    message="仅接受 .json 文件",
                    code="bad_extension",
                )
            )
            results.append(r)
            continue
        # A file may be a single entity object or an array bundle
        # (the batch-export format); every element is validated
        # independently and the batch stays atomic.
        results.extend(validate_import_content(filename, await f.read()))

    # Permission check: require manage:* for every entity type present.
    await _require_import_permissions(
        request, user_id, {r.entity_type for r in results if r.entity_type}
    )

    # Conflict checks: against existing entities and within the batch.
    existing: dict[str, set[str]] = {
        "scene": {s["id"] for s in await mgmt.list_scenarios()},
        "agent": {a["name"] for a in await mgmt.list_agents()},
        "tool": {t["name"] for t in await mgmt.list_tools()},
    }
    seen: dict[tuple[str, str], str] = {}
    for r in results:
        if not r.ok or not r.entity_type or not r.entity_key:
            continue
        t, key = r.entity_type, r.entity_key
        if key in existing[t]:
            r.ok = False
            label = "场景" if t == "scene" else "Agent" if t == "agent" else "工具"
            r.issues.append(
                ImportIssue(
                    path="$",
                    message=f"{label}「{key}」已存在，无法导入（名称需全局唯一）",
                    code="conflict",
                )
            )
        elif (t, key) in seen:
            r.ok = False
            r.issues.append(
                ImportIssue(
                    path="$",
                    message=(
                        f"与文件「{seen[(t, key)]}」中的实体重复"
                        f"（{key}），请只保留其中一个"
                    ),
                    code="duplicate",
                )
            )
        else:
            seen[(t, key)] = r.filename

    bad = [r for r in results if not r.ok]
    if bad:
        raise HTTPException(
            422,
            detail={
                "message": (
                    f"导入失败：{len(bad)} 个文件存在错误，未导入任何实体，请按提示修改后重试"
                ),
                "files": [r.model_dump() for r in results],
            },
        )

    created: list[dict[str, Any]] = []
    for r in results:
        payload = {**(r.payload or {}), "created_by": user_id}
        if r.entity_type == "scene":
            created.append(await mgmt.create_scenario(payload))
        elif r.entity_type == "agent":
            created.append(await mgmt.create_agent(payload))
        else:
            created.append(await mgmt.create_tool(payload))
        logger.info(
            "Imported %s key=%s by user=%s", r.entity_type, r.entity_key, user_id
        )
    return {"created": created, "file_count": len(created)}


# ── JSON Export ──


def _accessors(mgmt: Any, entity_type: str) -> tuple[str, Any, Any]:
    """Return (key field, single-getter, list-getter) for an entity type."""
    if entity_type == "scene":
        return "id", mgmt.get_scenario, mgmt.list_scenarios
    if entity_type == "agent":
        return "name", mgmt.get_agent, mgmt.list_agents
    return "name", mgmt.get_tool, mgmt.list_tools


def _export_response(content: str, filename: str) -> Response:
    return Response(
        content=content,
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export/{entity_type}")
async def export_entities(
    request: Request,
    entity_type: str,
    ids: str | None = Query(None, description="Comma-separated keys; empty = all"),
    user_id: str = Depends(get_current_user),
) -> Response:
    """Batch-export scene / agent / tool metadata as a JSON array file.

    ``ids`` selects specific entities (by scene id / agent or tool name);
    without it every entity of that type is exported.  The file is the
    batch-import format: a JSON array of importable entity objects.
    """
    if entity_type not in _IMPORT_PERMISSIONS:
        raise HTTPException(404, f"Unknown entity type: {entity_type}")
    await _require_import_permissions(request, user_id, {entity_type})
    mgmt = request.app.state.adapters.metadata
    if mgmt is None:
        raise HTTPException(501, "Management provider not configured")

    if ids:
        keys = [k for k in (k.strip() for k in ids.split(",")) if k]
        key_field, _, list_all = _accessors(mgmt, entity_type)
        entities = [e for e in await list_all() if str(e.get(key_field, "")) in keys]
    else:
        _, _, list_all = _accessors(mgmt, entity_type)
        entities = await list_all()

    payloads = [build_export_payload(entity_type, e) for e in entities]
    content = json.dumps(payloads, ensure_ascii=False, indent=2) + "\n"
    logger.info(
        "Exported %d %s entity(ies) by user=%s ids=%s",
        len(payloads),
        entity_type,
        user_id,
        ids or "(all)",
    )
    return _export_response(content, f"{entity_type}-batch-export.json")


@router.get("/export/{entity_type}/{key}")
async def export_entity(
    request: Request,
    entity_type: str,
    key: str,
    user_id: str = Depends(get_current_user),
) -> Response:
    """Export a single scene / agent / tool as one importable JSON file."""
    if entity_type not in _IMPORT_PERMISSIONS:
        raise HTTPException(404, f"Unknown entity type: {entity_type}")
    await _require_import_permissions(request, user_id, {entity_type})
    mgmt = request.app.state.adapters.metadata
    if mgmt is None:
        raise HTTPException(501, "Management provider not configured")

    entity = await _accessors(mgmt, entity_type)[1](key)
    if entity is None:
        raise HTTPException(404, f"{entity_type.capitalize()} '{key}' not found")
    payload = build_export_payload(entity_type, entity)
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    logger.info("Exported %s %s by user=%s", entity_type, key, user_id)
    return _export_response(content, f"{entity_type}-{sanitize_filename(key)}.json")


@router.post("/tools/upload-batch", status_code=201)
async def upload_tool_scripts_batch(
    request: Request,
    files: list[UploadFile],
    overwrite: bool = Query(False),
    user_id: str = Depends(require_permission("manage:tool:*")),
) -> UploadToolsResponse:
    adapters = request.app.state.adapters
    mgmt = adapters.metadata
    script_store = adapters.tool_script_store
    if mgmt is None or script_store is None:
        raise HTTPException(501, "Tool script upload not configured")

    created: list[UploadToolResult] = []
    errors: list[dict[str, str]] = []

    for file in files:
        if not file.filename or not file.filename.endswith(".py"):
            errors.append(
                {
                    "filename": file.filename or "(unknown)",
                    "error": "Only .py files are accepted",
                }
            )
            continue
        try:
            content = await file.read()
            result = await _process_uploaded_script(
                mgmt, script_store, file.filename, content, overwrite, user_id
            )
            created.append(result)
        except ValueError as e:
            errors.append(
                {
                    "filename": file.filename or "(unknown)",
                    "error": str(e),
                }
            )
        except Exception as e:
            errors.append(
                {
                    "filename": file.filename or "(unknown)",
                    "error": str(e),
                }
            )

    return UploadToolsResponse(created=created, errors=errors)


# ── Feedback Management ──


class FeedbackResponse(BaseModel):
    feedback_id: str
    session_id: str
    target_type: str
    target_id: str
    user_id: str
    feedback_type: str
    comment: str | None
    category: str | None
    source: str
    agent_name: str
    metadata: dict[str, Any]
    created_at: str


@router.get("/feedback", response_model=ListResponse)
async def list_feedback(
    request: Request,
    q: str | None = Query(None, description="Search by user ID"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    feedback_type: str | None = Query(
        None, description="Filter by type: thumbs_up / thumbs_down"
    ),
    source: str | None = Query(
        None, description="Filter by source: ui_button / agent_tool"
    ),
    date_from: str | None = Query(None, description="Start date (ISO)"),
    date_to: str | None = Query(None, description="End date (ISO)"),
    status: str | None = Query(
        None, description="Filter by status: new / analyzing / optimized / deployed"
    ),
    user_id: str = Depends(require_permission("manage:feedback:*")),
) -> ListResponse:
    adapters = request.app.state.adapters
    if adapters.feedback is None:
        return ListResponse(items=[], total=0, page=page, page_size=page_size)
    items, total = await adapters.feedback.list(
        page=page,
        page_size=page_size,
        q=q,
        feedback_type=feedback_type,
        source=source,
        status=status,
        date_from=date_from,
        date_to=date_to,
    )
    return ListResponse(
        items=[_feedback_to_dict(fb) for fb in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/feedback/{feedback_id}/session")
async def get_feedback_session(
    request: Request,
    feedback_id: str,
    user_id: str = Depends(require_permission("manage:feedback:*")),
) -> dict[str, Any]:
    adapters = request.app.state.adapters
    if adapters.feedback is None:
        raise HTTPException(501, "Feedback storage not configured")

    fb = await adapters.feedback.get(feedback_id)
    if fb is None:
        raise HTTPException(404, "Feedback not found")

    # get session info
    session = await adapters.sessions.get_session(fb.session_id)
    session_dict = {
        "session_id": fb.session_id,
        "user_id": fb.user_id,
        "agent_name": fb.agent_name,
    }
    if session:
        session_dict["title"] = getattr(session, "title", "")
        session_dict["created_at"] = getattr(session, "created_at", "")

    # get messages (enriched items) so IDs match frontend convention
    messages = list(adapters.sessions.get_messages_as_items(session))

    # find which message matches the feedback target
    highlight_message_id = None
    for m in messages:
        if fb.target_type == "message" and fb.target_id:
            if m.get("id") == fb.target_id:
                highlight_message_id = m.get("id")
                break
        elif fb.target_type == "tool_call" and fb.target_id:
            # highlight the tool RESULT message (role="tool"), not the
            # assistant message that initiated the call
            if m.get("role") == "tool" and m.get("tool_call_id") == fb.target_id:
                highlight_message_id = m.get("id")
                break

    return {
        "session": session_dict,
        "messages": messages,
        "highlight_target_type": fb.target_type,
        "highlight_target_id": fb.target_id,
        "highlight_message_id": highlight_message_id or fb.target_id,
        "feedback": _feedback_to_dict(fb),
    }


def _feedback_to_dict(fb: Feedback) -> dict[str, Any]:
    return {
        "feedback_id": fb.feedback_id,
        "session_id": fb.session_id,
        "target_type": fb.target_type,
        "target_id": fb.target_id,
        "user_id": fb.user_id,
        "feedback_type": fb.feedback_type,
        "comment": fb.comment,
        "category": fb.category,
        "source": fb.source,
        "status": fb.status,
        "agent_name": fb.agent_name,
        "metadata": fb.metadata,
        "created_at": fb.created_at,
    }


class BatchDeleteRequest(BaseModel):
    ids: list[str]


class StatusUpdateRequest(BaseModel):
    status: str


@router.delete("/feedback/{feedback_id}")
async def delete_feedback(
    request: Request,
    feedback_id: str,
    user_id: str = Depends(require_permission("manage:feedback:*")),
) -> dict[str, str]:
    adapters = request.app.state.adapters
    if adapters.feedback is None:
        raise HTTPException(501, "Feedback storage not configured")
    deleted = await adapters.feedback.delete(feedback_id)
    if not deleted:
        raise HTTPException(404, "Feedback not found")
    logger.info("Feedback deleted id=%s by user=%s", feedback_id, user_id)
    return {"status": "deleted", "feedback_id": feedback_id}


@router.post("/feedback/batch-delete")
async def batch_delete_feedback(
    request: Request,
    body: BatchDeleteRequest,
    user_id: str = Depends(require_permission("manage:feedback:*")),
) -> dict[str, int]:
    adapters = request.app.state.adapters
    if adapters.feedback is None:
        raise HTTPException(501, "Feedback storage not configured")
    count = await adapters.feedback.delete_many(body.ids)
    logger.info("Feedback batch-deleted %d items by user=%s", count, user_id)
    return {"deleted": count}


@router.patch("/feedback/{feedback_id}/status")
async def update_feedback_status(
    request: Request,
    feedback_id: str,
    body: StatusUpdateRequest,
    user_id: str = Depends(require_permission("manage:feedback:*")),
) -> dict[str, Any]:
    adapters = request.app.state.adapters
    if adapters.feedback is None:
        raise HTTPException(501, "Feedback storage not configured")
    fb = await adapters.feedback.update_status(feedback_id, body.status)
    if fb is None:
        raise HTTPException(404, "Feedback not found")
    logger.info(
        "Feedback status updated id=%s status=%s by user=%s",
        feedback_id,
        body.status,
        user_id,
    )
    return _feedback_to_dict(fb)


@router.get("/feedback/export")
async def export_feedback(
    request: Request,
    q: str | None = Query(None, description="Search query"),
    feedback_type: str | None = Query(
        None, description="Filter by type: thumbs_up / thumbs_down"
    ),
    source: str | None = Query(
        None, description="Filter by source: ui_button / agent_tool"
    ),
    status: str | None = Query(
        None, description="Filter by status: new / analyzing / optimized / deployed"
    ),
    date_from: str | None = Query(None, description="Start date (ISO)"),
    date_to: str | None = Query(None, description="End date (ISO)"),
    user_id: str = Depends(require_permission("manage:feedback:*")),
) -> Response:
    adapters = request.app.state.adapters
    if adapters.feedback is None:
        return Response(
            content=_csv_bom
            + "feedback_id,session_id,user_id,feedback_type,source,status,comment,created_at\n",
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=feedback.csv"},
        )
    items, _ = await adapters.feedback.list(
        page=1,
        page_size=999999,
        q=q,
        feedback_type=feedback_type,
        source=source,
        status=status,
        date_from=date_from,
        date_to=date_to,
    )
    lines = [
        "feedback_id,session_id,user_id,agent_name,feedback_type,source,status,comment,created_at"
    ]
    for fb in items:
        comment = (fb.comment or "").replace('"', '""')
        lines.append(
            f"{fb.feedback_id},{fb.session_id},{fb.user_id},{fb.agent_name},"
            f"{fb.feedback_type},{fb.source},{fb.status},"
            f'"{comment}",{fb.created_at}'
        )
    return Response(
        content=_csv_bom + "\n".join(lines),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=feedback.csv"},
    )
