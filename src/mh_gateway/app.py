"""FastAPI app construction and adapter bundle wiring.

The gateway owns no concrete adapter.  Deployers supply an
*adapter lifespan* that returns a fully constructed
:class:`GatewayAdapters` instance, and :func:`create_app` weaves it
into the request lifecycle.

Compared to the previous 13-slot ``AppState`` design, this version
collapses everything into a single immutable bundle, a single
lifespan entry point, and one global ``get_collectors()`` accessor
for the metrics collector.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from mh_gateway.adapters import (
    AuthorizationProvider,
    ConfigProvider,
    EvalResultRepository,
    LLMProviderService,
    M2MAuthenticator,
    MetadataRepository,
    OutboundAuthProvider,
    SessionRepository,
    UserAuthenticator,
)
from mh_gateway.api.router import router
from mh_gateway.api.component_sources import component_sources_router
from mh_gateway.config import ConfigSchema
from mh_gateway.config_manager import setup_service_logging
from mh_gateway.context import (
    clear_current_user_id,
    ensure_trace_id,
    reset_current_request,
    reset_current_trace_id,
    set_current_request,
    set_current_trace_id,
)
from mh_gateway.monitoring.middleware import AccessLogMiddleware

__all__ = [
    "GatewayAdapters",
    "AdapterLifespan",
    "create_app",
]


# ── Public bundle ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class GatewayAdapters:
    """Immutable bundle of all gateway adapters.

    Built once during application startup by an
    :data:`AdapterLifespan` and exposed to route handlers through
    ``app.state.adapters``.  Field names are stable; downstream code
    may rely on the dataclass attribute names.

    :class:`EvalResultRepository` is optional only when the
    deployment disables the eval feature via
    ``settings.enable_eval=False``.
    """

    settings: ConfigSchema
    user_auth: UserAuthenticator
    authorization: AuthorizationProvider
    m2m_auth: M2MAuthenticator
    outbound_auth: OutboundAuthProvider
    metadata: MetadataRepository
    llm: LLMProviderService
    sessions: SessionRepository
    eval_results: EvalResultRepository | None = None


class AdapterLifespan(Protocol):
    """Async context manager that produces a :class:`GatewayAdapters`.

    Implementations typically wrap construction, async init
    (database connections, file locks, …) and teardown.  The
    function is called exactly once per :func:`create_app` invocation
    during FastAPI's ``lifespan`` event.
    """

    def __call__(
        self, app: FastAPI
    ) -> "AbstractAsyncContextManager[GatewayAdapters]": ...


# Re-export for typing convenience
from contextlib import AbstractAsyncContextManager  # noqa: E402


# ── Tag metadata ──────────────────────────────────────────────────────────────


TAGS_METADATA = [
    {
        "name": "auth",
        "description": "用户认证与身份信息。获取当前用户信息（含权限列表）及登出。",
    },
    {
        "name": "scenarios",
        "description": "场景查询。获取场景列表（按权限过滤）和场景详情（含关联 Agent/Tool）。",
    },
    {
        "name": "chat",
        "description": "流式聊天。通过 SSE (Server-Sent Events) 与 Agent 进行流式对话，支持 `session_id` 续传历史。",
    },
    {
        "name": "sessions",
        "description": "会话管理。用户 Session 的 CRUD，支持按 `scenario_id` 过滤，含消息历史查询。",
    },
    {
        "name": "agents",
        "description": "Agent 查询。获取 Agent 列表（按权限过滤，支持 `?scenario=` 过滤）以及通过 M2M 鉴权运行 Agent。",
    },
    {
        "name": "tools",
        "description": "Tool 列表查询。获取当前用户有权使用的 Tool 列表。",
    },
    {
        "name": "runtime_tools",
        "description": "运行时工具执行。Agent 运行时调用的内置工具，包括 `discover_agents`、`handoff` 等。",
    },
    {
        "name": "tool-generator",
        "description": "AI 工具生成。通过 LLM 动态生成自定义 Tool 的定义与实现代码，支持 CRUD 和试运行。",
    },
    {
        "name": "generated-tools",
        "description": "已生成工具执行。由 tool-generator 生成的 Tool 的 M2M 鉴权执行端点。",
    },
    {
        "name": "agent-generator",
        "description": "AI Agent 生成。通过 LLM 动态生成自定义 Agent 的定义与配置，支持 CRUD 和试运行（SSE 流式聊天）。",
    },
    {
        "name": "management",
        "description": "管理面 CRUD。管理 Scenario、Agent、Tool 资源的增删改查，需要 `manage:*` 权限。",
    },
    {
        "name": "health",
        "description": "健康检查与就绪检查。`/health` 返回服务存活状态，`/ready` 检查 Session 存储可用性。",
    },
    {
        "name": "metrics",
        "description": "运行时指标。返回内存中采集的实时指标快照（HTTP 请求数/耗时、LLM 调用数/token 用量、Agent 运行数、Tool 调用数、活跃会话数等）。需要 `metrics_enabled=true`。",
    },
    {
        "name": "eval",
        "description": "评测管理。批量运行评测任务（通过 M2M 鉴权），用于评估 Agent 在测试问题集上的表现。",
    },
    {
        "name": "component-sources",
        "description": "前端组件源配置（仅在 `dev_mode=true` 时可用）。返回 Tool UI 组件的 CDN/本地加载地址。",
    },
    {
        "name": "guide",
        "description": "智能体使用引导。根据用户输入推荐可用 Agent 场景。",
    },
]

_APP_DESCRIPTION = """
Orchestration Gateway — 一个基于 [minimal-harness](https://github.com/anomalyco/world-of-agents) SDK 构建的核心网关服务。

负责场景加载、用户权限校验、事件流归集，协调前端与各 worker 服务的通信。

## 核心能力

- **场景与 Agent 管理**：动态加载和管理场景 (Scenario)、Agent、Tool 的元数据
- **流式聊天**：通过 SSE 实现实时的多 Agent 对话，支持会话续传
- **AI 生成**：通过 LLM 动态生成自定义 Tool 和 Agent
- **权限与认证**：支持用户 Token 鉴权、M2M 机机鉴权、细粒度工具调用权限校验
- **可观测性**：结构化访问日志、审计日志、运行时指标采集

## 适配层架构

所有外部依赖（认证、权限、注册中心、LLM Provider 等）通过
``AdapterLifespan`` 接口注入为一个不可变的 :class:`GatewayAdapters`，
方便企业部署时对接自有的 SSO、配置中心、密钥管理系统。
"""


# ── create_app ────────────────────────────────────────────────────────────────


def create_app(
    *,
    settings: ConfigSchema,
    adapters: AdapterLifespan,
    lifespan_hooks: Sequence[Callable[[FastAPI], AbstractAsyncContextManager[None]]]
    | None = None,
    dev_routers: list[Any] | None = None,
) -> FastAPI:
    """Create a configured FastAPI app for the orchestration service.

    :param settings: 已解析的框架配置（由 ``ConfigManager.resolve()``
        或手动构建）。
    :param adapters: An async context manager factory that returns a
        :class:`GatewayAdapters` once the deployment's connectors are
        initialised.  It is entered during FastAPI's ``lifespan``
        startup and closed on shutdown.
    :param lifespan_hooks: Optional extra lifespan hooks, executed
        after the adapter lifespan.  Useful for cross-cutting
        concerns that need a startup/shutdown boundary but are not
        part of the adapter bundle.
    :param dev_routers: Routers mounted only when ``settings.dev_mode``
        is true (e.g. mock login).
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        setup_service_logging()

        if settings.metrics_enabled:
            from mh_gateway.monitoring.collector import (
                MetricsCollector,
                set_collector,
            )

            collector = MetricsCollector()
            set_collector(collector)
            collector.start_push(interval=settings.metrics_push_interval)
            logging.getLogger("orchestration.app").info(
                "Metrics collector started (push_interval=%ds)",
                settings.metrics_push_interval,
            )

        bundle: GatewayAdapters
        async with adapters(app) as bundle:
            if settings.enable_eval and bundle.eval_results is None:
                raise RuntimeError(
                    "settings.enable_eval=True but GatewayAdapters.eval_results "
                    "is None. Provide an EvalResultRepository in the adapter "
                    "lifespan."
                )
            app.state.adapters = bundle

            for hook in lifespan_hooks or []:
                async with hook(app):
                    pass

            yield

        if settings.metrics_enabled:
            from mh_gateway.monitoring.collector import (
                get_collector,
                set_collector,
            )

            collector = get_collector()
            if collector:
                await collector.stop_push()
                set_collector(None)

    app = FastAPI(
        title="MH Gateway",
        summary="编排网关 — 场景加载、Agent 路由、事件流归集",
        description=_APP_DESCRIPTION.strip(),
        version="0.2.0",
        openapi_tags=TAGS_METADATA,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.add_middleware(AccessLogMiddleware)

    @app.middleware("http")
    async def request_context_middleware(request, call_next):
        req_token = set_current_request(request)
        trace_id = ensure_trace_id(request)
        trace_token = set_current_trace_id(trace_id)
        try:
            return await call_next(request)
        finally:
            reset_current_request(req_token)
            reset_current_trace_id(trace_token)
            clear_current_user_id()

    app.include_router(router)

    if settings.enable_eval:
        from mh_gateway.eval import router as eval_router

        app.include_router(eval_router)

    if settings.dev_mode:
        app.include_router(component_sources_router)

        if dev_routers:
            for r in dev_routers:
                app.include_router(r)

        static_dir = Path(__file__).resolve().parent / "static"
        if static_dir.is_dir():
            app.mount(
                "/",
                StaticFiles(directory=str(static_dir), html=True),
                name="frontend",
            )

            @app.middleware("http")
            async def spa_fallback(request, call_next):
                response = await call_next(request)
                if (
                    response.status_code == 404
                    and request.method == "GET"
                    and request.url.path != "/api"
                    and not request.url.path.startswith("/api/")
                    and not request.url.path.startswith("/docs")
                    and not request.url.path.startswith("/openapi")
                    and not request.url.path.startswith("/redoc")
                ):
                    index_path = static_dir / "index.html"
                    if index_path.is_file():
                        return FileResponse(str(index_path))
                return response

    return app
