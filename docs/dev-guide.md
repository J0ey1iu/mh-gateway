# mh-gateway 服务开发指南

本文档面向 **基于 mh-gateway 框架开发自定义网关服务** 的开发人员。详细说明如何集成适配器、管理配置、扩展功能。

---

## 架构概述

```
┌──────────────────────────────────────────────────────────┐
│                   uvicorn my_app:app                       │
│  ┌────────────────────────────────────────────────────┐   │
│  │              mh_gateway                │   │
│  │  ┌──────────┐ ┌───────────┐ ┌──────────────────┐   │   │
│  │  │ Chat API │ │ Agents    │ │ Sessions / Auth   │   │   │
│  │  └────┬─────┘ └─────┬─────┘ └────────┬─────────┘   │   │
│  │       │              │                │             │   │
│  │  ┌────▼──────────────▼────────────────▼──────────┐  │   │
│  │  │              Adapter Layer                     │  │   │
│  │  │  (通过 LifespanHook 注入自定义实现)              │  │   │
│  │  └────▲──────────────▲────────────────▲──────────┘  │   │
│  └───────┼──────────────┼────────────────┼────────────┘   │
│          │              │                │                  │
│    ┌─────┴────┐  ┌──────┴──────┐  ┌──────┴──────┐        │
│    │  企业 SSO │  │ 企业权限系统  │  │ 企业配置中心   │        │
│    └──────────┘  └─────────────┘  └─────────────┘        │
└──────────────────────────────────────────────────────────┘
```

### 核心概念

| 概念 | 说明 |
|------|------|
| `create_app()` | 框架唯一入口工厂函数，接收 `settings` + Adapter LifespanHook |
| `LifespanHook` | async context manager, 在 FastAPI 生命周期内初始化和清理 Adapter |
| `AppState` | 运行时 Adapter 容器，挂载在 `app.state.adapters` |
| `ConfigManager` | 配置管理工具，支持环境变量 + 配置中心 + 密钥管理三层回退 |
| `Per-request Context` | 通过 ContextVar 暴露当前请求信息，Adapter 内可直接获取 |

---

## 开发步骤

### 1. 创建启动文件

`my_app.py` 是唯一的入口文件：

```python
import asyncio
import logging
from contextlib import asynccontextmanager
from mh_gateway import ConfigManager, ConfigSchema, create_app
from pydantic import BaseModel


# ── 1. 定义 Adapter 配置（可选） ───────────────
class MyRegistryConfig(BaseModel):
    api_url: str = ""
    api_key: str = ""


# ── 2. 创建 ConfigManager ─────────────────────
config_mgr = ConfigManager()  # 仅从环境变量读取


# ── 3. 定义 LifespanHook ──────────────────────
@asynccontextmanager
async def my_management_provider(app):
    app.state.adapters.registry_provider = MyRegistry()
    app.state.adapters.management_provider = MyRegistry()  # 如果也实现了 MetadataManager
    yield


# ── 4. 解析框架配置 ───────────────────────────
settings = asyncio.run(config_mgr.resolve(ConfigSchema, prefix="ORCH"))


# ── 5. 组装应用 ───────────────────────────────
app = create_app(
    settings=settings,
    management_provider=my_management_provider,  # 代替旧的 registry_provider
)
```

### 2. 实现 Adapter 接口

#### UserAuthProvider — 用户认证

```python
from mh_gateway.auth import UserAuthProvider, UserIdentity

class MyAuth(UserAuthProvider):
    async def verify(self, request) -> UserIdentity | None:
        token = request.headers.get("authorization", "")
        user_info = await self._call_sso(token)
        if not user_info:
            return None
        return UserIdentity(
            user_id=user_info["uid"],
            username=user_info["name"],
            roles=user_info.get("roles", []),
            extra_data=user_info,
        )
```

#### PermissionChecker — 权限校验

```python
from mh_gateway.auth import PermissionChecker, match_permission

class MyPerms(PermissionChecker):
    async def get_permissions(self, user_id: str) -> list[str]:
        return ["use:agent:*", "use:tool:calculator"]

    async def check(self, user_id: str, permission: str) -> bool:
        perms = await self.get_permissions(user_id)
        return match_permission(perms, permission)
```

权限格式: `action:resource:target`，支持 `*` 通配符。

#### RegistryProvider — 注册中心

```python
from mh_gateway.adapters import RegistryProvider

class MyRegistry(RegistryProvider):
    async def list_agents(self) -> list[dict]:
        return [{"name": "my-agent", "display_name": "My Agent", "description": "..."}]

    async def get_agent(self, name: str) -> dict | None: ...
    async def list_tools(self) -> list[dict]: ...
    async def get_tool(self, name: str) -> dict | None: ...
    async def list_scenarios(self) -> list[dict]: ...
    async def get_scenario(self, s_id: str) -> dict | None: ...

    # ── 可选性能优化 ────────────────────
    # get_tools(names: list[str]) -> dict[str, dict | None]
    # 批量获取工具元数据（运行时会自动检测并使用，无需额外配置）
```

返回数据结构:

| 对象 | 键 | 说明 |
|------|-----|------|
| Agent | `name`, `display_name`, `description`, `display_name_locale`, `description_locale`, `system_prompt`, `system_prompt_locale`, `endpoint_url` | `endpoint_url` 非空则视为远程 Agent |
| Tool | `name`, `display_name`, `description`, `parameters`, `display_name_locale`, `description_locale`, `endpoint_url`, `_fn` | `parameters` 为 JSON Schema；`endpoint_url` 非空则远程执行，否则本地执行 `_fn` |
| Scenario | `id`, `name`, `description`, `agents`, `name_locale`, `icon` | `agents` 为 `[{name, tool_names}]` |

> **MetadataManager** 是推荐的统一读写协议，继承自 `RegistryProvider` 并扩展了 CRUD 方法：
> `create_agent/update_agent/delete_agent`、`create_tool/update_tool/delete_tool`、
> `create_scenario/update_scenario/delete_scenario` 以及场景-Agent-Tool 关系管理方法。
> `management_provider` AppState 槽位优先期望 `MetadataManager` 实现。
>
> ```python
> from mh_gateway.adapters import MetadataManager
> # MetadataManager 继承了 RegistryProvider 所有读方法 + 上述 CRUD 方法
> ```

#### OutboundAuthProvider — 出站认证注入

```python
from mh_gateway import OutboundAuthProvider

class MyOutboundAuth(OutboundAuthProvider):
    async def get_headers(self, request, target_url: str, target_type: str) -> dict[str, str]:
        return {"Authorization": request.headers.get("authorization", "")}
```

默认实现将当前请求的所有 header（不含 hop-by-hop）透传给下游。

#### M2MAuthProvider — 机机鉴权

```python
from mh_gateway import M2MAuthProvider

class MyM2MAuth(M2MAuthProvider):
    async def authenticate(self, request) -> str | None:
        auth = request.headers.get("Authorization", "")
        app_id = await self._verify_service_token(auth)
        return app_id  # None → 401

    async def get_identity_headers(self, request, identity: str) -> dict[str, str]:
        return {"X-SOA-Token": await self._sign(identity)}

    async def close(self): ...
```

#### ConfigProvider — 外部配置 / 密钥管理

`ConfigProvider` 是唯一的协议类型；`SecretResolver` 是其向后兼容别名。
`ConfigManager` 接受两个 `ConfigProvider` 实例，分别用于普通配置和敏感配置。

```python
from mh_gateway import ConfigProvider

class ApolloConfigProvider(ConfigProvider):
    async def get(self, key: str) -> str | None:
        return await apollo_client.get_value(key)

class VaultSecretResolver(ConfigProvider):
    async def get(self, key: str) -> str | None:
        return await vault_client.read_secret(key)
```

#### ToolGenerator — 工具自动生成

由自然语言描述生成可执行工具的元数据和源码，适配器由 `app.state.adapters.generated_tool_provider` 注入。

```python
import asyncio
from mh_gateway.services.generated_tool_provider import ToolGenerator
from collections.abc import AsyncGenerator

class MyToolGenerator(ToolGenerator):
    def generate_stream(self, natural_description: str, stop_event: asyncio.Event | None = None) -> AsyncGenerator[dict[str, Any], None]:
        ...
```

默认实现使用 LLM 生成。持久化由 `MetadataManager` 负责。

#### AgentGenerator — Agent 自动生成

由自然语言描述生成 Agent 元数据和 system prompt。

```python
from mh_gateway.services.generated_agent_provider import AgentGenerator

class MyAgentGenerator(AgentGenerator):
    def generate_stream(self, natural_description: str, stop_event: asyncio.Event | None = None) -> AsyncGenerator[dict[str, Any], None]:
        ...
```

#### ExtraHeadersProvider — LLM 额外 HTTP 头

**注意：这不是 LifespanHook，而是直接传给 `create_app()` 的 `Callable`。**

```python
from minimal_harness.types import ExtraHeadersProvider
# 实际是 Callable[[], Awaitable[dict[str, str]]]

async def my_extra_headers() -> dict[str, str]:
    return {"x-reasoning-format": "deepseek"}
```

用于向 LLM API 调用注入动态 HTTP 头（如推理格式标记）。通过 `llm_extra_headers_provider` 参数传入 `create_app()`。

#### LLMProviderRegistry — LLM Provider 注册表

支持 per-agent 选择不同 LLM Provider / Model。`create_app()` 通过 `llm_provider_registry` LifespanHook 注入。

```python
from minimal_harness.llm.llm import LLMProviderRegistry

@asynccontextmanager
async def my_provider_registry(app: FastAPI):
    registry = LLMProviderRegistry()
    registry.register("my_llm", lambda cfg: MyCustomLLM(cfg))
    registry.set_default_config("my_llm", {"api_key": "xxx"})
    app.state.adapters.llm_provider_registry = registry
    yield
```

内置已注册 `openai`、`anthropic`，并克隆了 `openai_viz`。Provider 默认配置通过环境变量 `ORCH_PROVIDER_{NAME}__{KEY}` 设置。

### 3. 使用 Per-request Context

Adapter 内可直接获取当前请求上下文：

```python
from mh_gateway import (
    get_current_request,     # 完整 Request 对象
    get_current_cookies,     # Cookie 字典
    get_current_auth_token,  # Bearer token / cookie 回退
    get_current_user_id,     # 已认证用户 ID
    get_current_locale,      # Accept-Language
    get_current_trace_id,    # 追踪 ID
)

class MyRegistry(RegistryProvider):
    async def list_agents(self) -> list[dict]:
        user_id = get_current_user_id()
        token = get_current_auth_token()
        return await self._http.get("/api/agents", headers={"Authorization": f"Bearer {token}"})
```

### 4. 完整集成示例

```python
import asyncio, logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from pydantic import BaseModel
from mh_gateway import (
    ConfigManager, ConfigSchema, LifespanHook, create_app,
    OutboundAuthProvider, M2MAuthProvider, ConfigProvider,
    MetadataManager, UserAuthProvider, PermissionChecker,
)


# ── 日志 ────────────────────────────────────────
root = logging.getLogger()
root.addHandler(logging.StreamHandler())
root.setLevel(logging.DEBUG)


# ── 配置中心 ────────────────────────────────────
class MyConfigProvider(ConfigProvider):
    async def get(self, key: str) -> str | None: ...

config_mgr = ConfigManager(
    config_provider=MyConfigProvider(),
    secret_resolver=MyConfigProvider(),  # 同一协议类型，可传不同实例
)


# ── 自定义 Adapter 配置 ────────────────────────
class CorpRegistryConfig(BaseModel):
    api_url: str = "http://registry:8080"
    api_token: str = ""


# ── Adapter 实现 ────────────────────────────────
class CorpAuth(UserAuthProvider):
    async def verify(self, request): ...

class CorpPerms(PermissionChecker):
    async def get_permissions(self, user_id): ...

class CorpRegistry(MetadataManager):  # 实现统一读写协议
    def __init__(self, api_url, api_token): ...
    async def list_agents(self): ...
    async def create_agent(self, agent): ...


# ── Lifespan Hooks ──────────────────────────────
@asynccontextmanager
async def token_verifier(app: FastAPI):
    app.state.adapters.token_verifier = CorpAuth()
    yield

@asynccontextmanager
async def permission_checker(app: FastAPI):
    app.state.adapters.permission_checker = CorpPerms()
    yield

@asynccontextmanager
async def management_provider(app: FastAPI):
    cfg = await config_mgr.resolve(CorpRegistryConfig, prefix="CORP.REGISTRY", sensitive_fields={"api_token"})
    registry = CorpRegistry(cfg.api_url, cfg.api_token)
    app.state.adapters.registry_provider = registry
    app.state.adapters.management_provider = registry
    yield

@asynccontextmanager
async def outbound_auth(app: FastAPI):
    app.state.adapters.outbound_auth_provider = MyOutboundAuth()
    yield

@asynccontextmanager
async def my_provider_registry(app: FastAPI):
    from minimal_harness.llm.llm import LLMProviderRegistry
    from minimal_harness.llm.factory import register_builtin_providers
    registry = LLMProviderRegistry()
    register_builtin_providers(registry)
    registry.register("my_llm", lambda cfg: MyLLM(cfg))
    app.state.adapters.llm_provider_registry = registry
    yield

async def llm_extra_headers() -> dict[str, str]:
    return {"x-reasoning-format": "deepseek"}


# ── 组装 ────────────────────────────────────────
settings = asyncio.run(config_mgr.resolve(ConfigSchema, prefix="ORCH"))
app = create_app(
    settings=settings,
    token_verifier=token_verifier,
    permission_checker=permission_checker,
    management_provider=management_provider,  # 替换旧的 registry_provider
    outbound_auth_provider=outbound_auth,
    llm_extra_headers_provider=llm_extra_headers,  # Callable，非 LifespanHook
    llm_provider_registry=my_provider_registry,     # LifespanHook
)
```

### 5. 启动

```bash
uvicorn my_app:app --host 0.0.0.0 --port 8005 --workers 4
```

---

## 配置管理

### ConfigManager 解析优先级

每个字段独立按以下顺序查找：

1. 环境变量 `{PREFIX}_{FIELD}`（最高优先级）
2. 敏感字段 → `secret_resolver.get()`
3. 非敏感字段 → `config_provider.get()`
4. 模型默认值（仅可选字段）
5. 必填字段仍缺失 → `ConfigError`，启动失败

### ConfigSchema 关键字段

| 字段 | 环境变量 | 必填 | 说明 |
|------|---------|------|------|
| `db_path` | `ORCH_DB_PATH` | 否 | SQLite 数据库文件路径（默认 `./sessions.db`） |
| `db_auto_schema` | `ORCH_DB_AUTO_SCHEMA` | 否 | 默认 false，自动建表 |
| `cors_origins` | `ORCH_CORS_ORIGINS` | 否 | JSON 数组格式 |
| `dev_mode` | `ORCH_DEV_MODE` | 否 | 默认 false，开发模式（内置 agent、前端、SSO） |
| `enable_eval` | `ORCH_ENABLE_EVAL` | 否 | 默认 true，是否暴露评测接口 |
| `eval_results_dir` | `ORCH_EVAL_RESULTS_DIR` | 否 | 默认 `./eval_results` |
| `log_level` | `ORCH_LOG_LEVEL` | 否 | 默认 `INFO`（ConfigSchema 声明字段，但日志通过 `MH_LOG_LEVEL` 环境变量或自行配置 root logger 控制） |

> `llm_api_key`/`llm_base_url`/`llm_model` 已从 `ConfigSchema` 移除。LLM 配置改为通过 `LLMProviderRegistry` + 环境变量 `ORCH_PROVIDER_{NAME}__{KEY}` 设置。例如 `ORCH_PROVIDER_OPENAI__API_KEY=sk-xxx`。

---

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/auth/me` | 当前用户信息 |
| GET | `/api/v1/scenarios` | 场景列表（按权限过滤） |
| GET | `/api/v1/scenarios/{id}` | 场景详情 |
| POST | `/api/v1/chat/{memory_id}` | SSE 流式聊天（用户 Token 或 M2M 鉴权） |
| GET | `/api/v1/sessions` | 用户 Session 列表（用户 Token 或 M2M 鉴权） |
| POST | `/api/v1/sessions` | 创建 Session（用户 Token 或 M2M 鉴权） |
| GET | `/api/v1/sessions/{id}` | Session 详情（用户 Token 或 M2M 鉴权） |
| DELETE | `/api/v1/sessions/{id}` | 删除 Session（用户 Token 或 M2M 鉴权） |
| GET | `/api/v1/agents` | Agent 列表（按权限过滤） |
| POST | `/api/v1/agents/{name}/run` | M2M 鉴权的 Agent 执行 |
| GET | `/api/v1/tools` | Tool 列表（按权限过滤） |
| POST | `/api/v1/tools/{name}/execute` | M2M 鉴权的 Tool 执行 |
| | **管理面 CRUD**（`management_provider`/`MetadataManager` 提供） | |
| GET/POST | `/api/v1/management/scenarios` | 场景 CRUD |
| GET/PUT/DELETE | `/api/v1/management/scenarios/{id}` | 场景详情/更新/删除 |
| POST/DELETE | `/api/v1/management/scenarios/{id}/agents` | 场景-Agent 关系管理 |
| POST/DELETE | `/api/v1/management/scenarios/{id}/agents/{name}/tools` | Agent-Tool 关系管理 |
| GET/POST | `/api/v1/management/agents` | Agent CRUD |
| GET/PUT/DELETE | `/api/v1/management/agents/{name}` | Agent 详情/更新/删除 |
| GET/POST | `/api/v1/management/tools` | Tool CRUD |
| GET/PUT/DELETE | `/api/v1/management/tools/{name}` | Tool 详情/更新/删除 |
| GET | `/api/v1/management/providers` | LLM Provider 列表 |
| | **AI 生成**（`ToolGenerator`/`AgentGenerator` 提供） | |
| POST | `/api/v1/tool-generator/generate` | SSE 流式工具生成 |
| POST/PUT/DELETE | `/api/v1/tool-generator/tools[/{name}]` | 用户已生成工具 CRUD |
| POST | `/api/v1/tool-generator/tools/{name}/trial` | 工具试运行（SSE） |
| POST | `/api/v1/tools/generated/{name}/execute` | M2M 鉴权的生成工具执行 |
| POST | `/api/v1/agent-generator/generate` | SSE 流式 Agent 生成 |
| POST/PUT/DELETE | `/api/v1/agent-generator/agents[/{name}]` | 用户已生成 Agent CRUD |
| POST | `/api/v1/agent-generator/agents/{name}/trial` | Agent 试运行（SSE 流式聊天） |

---

## 数据库

内置 SQLite 实现。通过设置 `ORCH_DB_PATH` 指定文件路径，默认 `./sessions.db`。

### Adapter 模式

框架只定义 `SessionStoreProtocol` 作为内部流转结构，数据库实现由客户通过 adapter 注入：

```python
from mh_gateway.services import database as db_svc

db_svc.set_session_store_factory(lambda: MySessionStore(my_db_conn))
```

工厂接受同步或异步 callable。不注入时默认使用 `BuiltinSessionStore` + `SqliteDatabase`。

`SessionStoreProtocol` 完整方法（定义在 `minimal_harness.memory_store`）：

| 方法 | 说明 |
|------|------|
| `create_session(session_id, agent_name, user_id, scenario_id, transient, display_name_locale) -> Session` | 创建新 Session |
| `get_session(session_id) -> Session \| None` | 获取 Session（含消息历史） |
| `save_memory(memory, session_id, extra) -> None` | 持久化新消息 |
| `delete_session(session_id) -> bool` | 软删除 Session |
| `list_sessions() -> list[SessionSummary]` | 全部 Session 列表 |
| `list_user_sessions(user_id, scenario_id) -> list[SessionSummary]` | 按用户过滤 |
| `get_session_messages(session_id) -> list[dict]` | 获取消息原始数据 |
| `get_messages_as_items(session) -> list[dict]` | 转换为展示格式 |

参考实现见 `mh_gateway.database.BuiltinSessionStore`，完整 PostgreSQL 示例见 [customer-adaptation-guide.md](customer-adaptation-guide.md)。

内置表的审计字段：`created_by`, `last_updated_by`, `creation_date`, `last_update_date`, `delete_flag`(N/Y), `last_update_trace_id`。

`ORCH_DB_AUTO_SCHEMA` 控制是否自动建表，生产环境推荐由 DBA 管理。

---

## 最佳实践

1. **先实现 MetadataManager** — 统一读写协议，管理面 API 和运行时 API 共享同一数据源
2. **所有 Adapter 都用 LifespanHook 注入** — 不要依赖内置默认实现上生产
3. **Config 类必须继承 pydantic.BaseModel** — ConfigManager 依赖 `model_fields`
4. **生产环境务必关闭 dev_mode** — 默认 `false`，内置 agent 仅用于演示
5. **SSE 流协议** — Chat API 使用 SSE 流式推送事件，前端监听 `message` 事件
6. **日志审计** — 审计日志通过 `orchestration.audit` logger（INFO 级别）输出
7. **LLM 配置通过 ORCH_PROVIDER** — 使用环境变量 `ORCH_PROVIDER_{NAME}__{KEY}` 而不是旧的 `ORCH_LLM_*` 变量
8. **ToolGenerator/AgentGenerator 共用 llm_provider_factory** — 如果替换了 LLM，自定义生成器需调用 `set_llm_factory()`
