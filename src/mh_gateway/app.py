from __future__ import annotations

import logging
import warnings
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from mh_service_kit.logging_setup import setup_service_logging
from minimal_harness.types import ExtraHeadersProvider
from starlette.responses import FileResponse

from mh_gateway.api.component_sources import component_sources_router
from mh_gateway.api.router import router
from mh_gateway.config import ConfigSchema
from mh_gateway.context import (
    clear_current_user_id,
    ensure_trace_id,
    reset_current_request,
    reset_current_trace_id,
    set_current_request,
    set_current_trace_id,
)
from mh_gateway.monitoring.middleware import AccessLogMiddleware

logger = logging.getLogger("orchestration.app")


LifespanHook = Callable[[FastAPI], AbstractAsyncContextManager[None]]
"""生命周期钩子类型。

接收 FastAPI app，以 async generator 形式执行初始化和清理。
用法::

    @asynccontextmanager
    async def my_hook(app: FastAPI):
        cfg = await my_config_mgr.resolve(MyCfg, prefix="MY")
        app.state.adapters.registry_provider = MyRegistryProvider(cfg)
        yield
        await app.state.adapters.registry_provider.close()
"""


class AppState:
    """Holder for adapter instances, attached to app.state.adapters."""

    def __init__(
        self,
        settings: ConfigSchema,
        token_verifier: Any | None = None,
        permission_checker: Any | None = None,
        registry_provider: Any | None = None,
        management_provider: Any | None = None,
        llm_provider_factory: Any | None = None,
        outbound_auth_provider: Any | None = None,
        m2m_auth_provider: Any | None = None,
        llm_extra_headers_provider: Any | None = None,
        llm_provider_registry: Any | None = None,
        llm_provider_store: Any | None = None,
        database_provider: Any | None = None,
        session_store_provider: Any | None = None,
    ) -> None:
        object.__setattr__(self, "_initialized", False)
        self.settings = settings
        self.token_verifier = token_verifier
        self.permission_checker = permission_checker
        self.registry_provider = registry_provider
        self.management_provider = management_provider
        self.llm_provider_store = llm_provider_store
        self.llm_provider_factory = llm_provider_factory
        self.outbound_auth_provider = outbound_auth_provider
        self.m2m_auth_provider = m2m_auth_provider
        self.llm_extra_headers_provider = llm_extra_headers_provider
        self.llm_provider_registry = llm_provider_registry
        self.eval_result_storage = None
        self.database_provider = database_provider
        self.session_store_provider = session_store_provider
        object.__setattr__(self, "_initialized", True)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "registry_provider" and getattr(self, "_initialized", False):
            warnings.warn(
                "AppState.registry_provider is deprecated; "
                "use AppState.management_provider instead. "
                "The 'registry_provider' slot remains available for backward "
                "compatibility but will be removed in a future major release.",
                DeprecationWarning,
                stacklevel=2,
            )
        object.__setattr__(self, name, value)


_KNOWN_ADAPTER_SLOTS: frozenset[str] = frozenset(
    {
        "settings",
        "token_verifier",
        "permission_checker",
        "registry_provider",
        "management_provider",
        "llm_provider_store",
        "llm_provider_factory",
        "outbound_auth_provider",
        "m2m_auth_provider",
        "llm_extra_headers_provider",
        "llm_provider_registry",
        "eval_result_storage",
        "database_provider",
        "session_store_provider",
        "_initialized",
    }
)


def _warn_unknown_adapter_slots(state: AppState) -> None:
    """Warn if a LifespanHook set an attribute that is not a known adapter slot.

    Catches typos like ``app.state.adapters.management_providers = MyXxx``
    that would otherwise be silently dropped (no runtime error, but the
    adapter is never picked up by the framework).
    """
    unknown = [attr for attr in state.__dict__ if attr not in _KNOWN_ADAPTER_SLOTS]
    if unknown:
        logger.warning(
            "LifespanHook set unknown AppState attribute(s): %s. "
            "Known slots: %s. "
            "This is usually a typo — the attribute is ignored by the framework.",
            sorted(unknown),
            sorted(_KNOWN_ADAPTER_SLOTS),
        )


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
        "description": "健康检查与就绪检查。`/health` 返回服务存活状态，`/ready` 检查数据库连接可用性。",
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

所有外部依赖（认证、权限、注册中心、LLM Provider 等）通过 LifespanHook 接口注入，
方便企业部署时对接自有的 SSO、配置中心、密钥管理系统。
"""


def create_app(
    *,
    settings: ConfigSchema,
    logger: logging.Logger | None = None,
    token_verifier: LifespanHook | None = None,
    permission_checker: LifespanHook | None = None,
    management_provider: LifespanHook | None = None,
    llm_provider_factory: LifespanHook | None = None,
    outbound_auth_provider: LifespanHook | None = None,
    m2m_auth_provider: LifespanHook | None = None,
    llm_extra_headers_provider: ExtraHeadersProvider | None = None,
    llm_provider_registry: LifespanHook | None = None,
    llm_provider_store: LifespanHook | None = None,
    eval_result_storage: LifespanHook | None = None,
    database_provider: LifespanHook | None = None,
    session_store_provider: LifespanHook | None = None,
    lifespan_hooks: list[LifespanHook] | None = None,
    dev_routers: list[APIRouter] | None = None,
) -> FastAPI:
    """Create a configured FastAPI app for the orchestration service.

    All adapters are initialized inside the FastAPI ``lifespan``:

    1. Built-in defaults are filled for every adapter slot.
    2. Named per-adapter hooks run in order (e.g. ``token_verifier``).
       Each hook receives the app with ``app.state.adapters`` already
       populated with defaults, and can override its slot.
    3. Generic ``lifespan_hooks`` run next, for cross-cutting concerns.
    4. Database is initialized.
    5. Application serves requests.
    6. On shutdown, hooks clean up in reverse order, then built-in
       adapters are closed.

    Args:
        settings: 已解析的框架配置（由 ConfigManager.resolve() 或手动构建）。
        logger: （已弃用）自定义 logger。请改为在调用 ``create_app()``
            之前自行配置 ``logging.getLogger()``（root logger）。
        token_verifier: 认证适配器 hook。
        permission_checker: 权限校验适配器 hook。
        management_provider: 统一数据管理适配器 hook。
        llm_provider_factory: LLM provider 工厂 hook。
        outbound_auth_provider: 出站认证适配器 hook。
        m2m_auth_provider: 机机接口鉴权适配器 hook。
        llm_extra_headers_provider: LLM 额外 HTTP 头回调，可动态返回
            headers 字典（如 ``x-reasoning-format``）。
        llm_provider_registry: LLM provider 注册表 hook。
            用于自定义 provider 注册（per-agent provider 选择）。
        database_provider: 数据库适配器 hook。
        session_store_provider: Session 存储适配器 hook。
        lifespan_hooks: 通用生命周期钩子，在 per-adapter hooks 之后执行。
    """
    if logger is not None:
        warnings.warn(
            "create_app(logger=...) is deprecated. "
            "Configure logging.getLogger() (root logger) before calling create_app() instead.",
            DeprecationWarning,
            stacklevel=2,
        )
    setup_service_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state = AppState(
            settings=settings,
            llm_extra_headers_provider=llm_extra_headers_provider,
        )
        app.state.adapters = state

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

        async with AsyncExitStack() as stack:
            if token_verifier is not None:
                await stack.enter_async_context(token_verifier(app))
            if permission_checker is not None:
                await stack.enter_async_context(permission_checker(app))
            if management_provider is not None:
                await stack.enter_async_context(management_provider(app))
            if llm_provider_factory is not None:
                await stack.enter_async_context(llm_provider_factory(app))
            if llm_provider_registry is not None:
                await stack.enter_async_context(llm_provider_registry(app))
            if outbound_auth_provider is not None:
                await stack.enter_async_context(outbound_auth_provider(app))
            if m2m_auth_provider is not None:
                await stack.enter_async_context(m2m_auth_provider(app))
            if llm_provider_store is not None:
                await stack.enter_async_context(llm_provider_store(app))

            if eval_result_storage is not None:
                await stack.enter_async_context(eval_result_storage(app))

            if database_provider is not None:
                await stack.enter_async_context(database_provider(app))

            if session_store_provider is not None:
                await stack.enter_async_context(session_store_provider(app))

            for hook in lifespan_hooks or []:
                await stack.enter_async_context(hook(app))

            _warn_unknown_adapter_slots(state)

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
        version="0.1.0",
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
