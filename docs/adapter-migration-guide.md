# Adapter 重构迁移指南（`mh-gateway` 0.2.0）

> 本文档面向**已经基于 `mh-gateway` 旧版本完成企业适配的客户开发团队**，帮助你们把现有代码迁移到新的统一 Adapter 体系（10 个协议 + 单一 `AdapterLifespan`）。
>
> 适用版本：`mh-gateway >= 0.2.0a1`（自本次重构起）。

---

## 0. 写给客户开发者

我们在 `mh-gateway` 上做了一次较大幅度的 Adapter 层重构，对外**只破坏 Python SDK 调用方式**：

- REST API 契约、HTTP 状态码、JSON 字段、SSE 事件 schema —— **全部保持向前兼容**；
- 持久化文件（SQLite、providers.json、registry.json、session 文件）—— **格式不变**；
- 前端能继续使用 OpenAPI 文档，`/docs` 路径仍是 37 条。

**需要你们做什么**：把"装配代码"从 14 个分散的 `LifespanHook` 合并为一个 `AdapterLifespan`，并在协议类名 / 字段名变更处统一改名。一个普通规模的适配项目，预计工作量在 **0.5 - 1.5 人天**。

> 如果你只想跟着默认实现走、不插任何自定义 adapter，请直接升级依赖到 `0.2.0a1`，业务侧无需改动 —— 我们已经为你和 `mh-orch-app` 的默认实现做过完整对齐。

---

## 1. 为什么要重构

旧版本的 Adapter 层在长期演化中出现了一些明显的问题：

| 问题 | 旧版本表现 | 影响 |
|------|-----------|------|
| **协议碎片化** | 13 个 `@runtime_checkable` Protocol，且 `MetadataManager`、`RegistryProvider` 互相继承；`LLMProviderFactory` / `LLMProviderRegistry` / `LLMProviderStore` 三个角色互相耦合 | 客户实现一次要写 6-8 个适配类，且内部状态难以共享 |
| **装配样板庞杂** | `create_app` 接受 14 个命名 hook：`token_verifier`、`permission_checker`、`management_provider`、`llm_provider_factory`、`outbound_auth_provider`、`m2m_auth_provider`、`llm_extra_headers_provider`、`llm_provider_registry`、`llm_provider_store`、`database_provider`、`session_store_provider`、`eval_result_storage`... | 装配代码冗长；某 hook 的报错在 lifespan 阶段才暴露，启动慢、调试难 |
| **状态存储在 `app.state.adapters` 上** | 类型为可变 `AppState` 对象，字段是 14 个 `Any \| None` | 类型提示几乎为零，IDE 不能跳转；测试时 monkey-patch 困难 |
| **职责重叠** | `OutboundAuthProvider` + `M2MAuthProvider.get_identity_headers` 各自返回 header map；`x-user-id` 兜底由网关侧"私塞" | 跨语言接入和"谁是身份来源"难以理解 |

重构后：

- **10 个协议**，每个单一职责，命名更接近工业界共识（`Authenticator`、`Provider`、`Repository`、`Service`）。
- **一个 `AdapterLifespan`** 取代 14 个 hook 槽。
- **不可变 `GatewayAdapters` bundle**，全程 `slots=True, frozen=True`，纯 dataclass 类型。
- **`OutboundAuthProvider`** 一次性收拢"请求身份 / 目标 URL / 场景"上下文，并发判断。

---

## 2. Breaking Changes 清单（先看这一张表）

按可观察性强弱排序。**重点建议先把第 1、2 行吃透，其他基本都是改名。**

| # | 你会看到的旧 API | 新 API | 影响 |
|---|-----------------|--------|------|
| 1 | `create_app(token_verifier=..., permission_checker=..., database_provider=..., session_store_provider=..., llm_provider_factory=..., llm_provider_registry=..., llm_provider_store=..., llm_extra_headers_provider=..., outbound_auth_provider=..., m2m_auth_provider=..., management_provider=..., eval_result_storage=..., logger=...)` | `create_app(adapters=...)` 一参收口 | **装配代码全部重写** |
| 2 | `app.state.adapters.<slot>` 字段是 14 个 `Any \| None` | `app.state.adapters` 是 `GatewayAdapters` 不可变 dataclass | **所有路由、中间件、依赖注入读取处需改字段名** |
| 3 | `UserAuthProvider`、`PermissionChecker`、`M2MAuthProvider`、`EvalResultStorage` | `UserAuthenticator`、`AuthorizationProvider`、`M2MAuthenticator`、`EvalResultRepository` | 类名改名 |
| 4 | `OutboundAuthProvider.get_headers(request, target_url, target_type)` | `OutboundAuthProvider.get_headers(OutboundRequestContext)` | **签名变化**，三个分散参数合并成 dataclass |
| 5 | `LLMProviderFactory` + `LLMProviderRegistry` + `LLMProviderStore` + `ExtraHeadersProvider`（4 个协议） | `LLMProviderService`（1 个协议 + 内部 dataclass `LLMResolveSpec`） | **整体替换**，详见 §4.4 |
| 6 | `RegistryProvider` + `MetadataManager`（互相继承的 2 个） | `MetadataRepository`（1 个，含全部 read + write + relationships） | 整体替换 |
| 7 | `SessionStoreProtocol` + `DatabaseProtocol` 各自暴露 | `SessionRepository`（合并），`DatabaseProtocol` 退化为内置默认实现的私有细节 | 客户很少直接实现 `DatabaseProtocol`，若有请改实现 `SessionRepository` |
| 8 | `from mh_gateway.adapters import AppState, SecretResolver, ExtraHeadersProvider, LifespanHook` 等 | 全部已移除，直接 `ImportError` | 见 §6.2 |
| 9 | `create_app(logger=...)` 形参 | 移除，请改用 `logging.getLogger()` 早期配置 | 仅一行删除 |
| 10 | 启动时 `AppState.registry_provider` 已 `DeprecationWarning` | 完全移除（请改用 `management_provider` → 现 `metadata`） | 仅一行删除 |

> **重要承诺**：上述改动**不涉及** HTTP 路径、请求方法、状态码、JSON 字段、SSE 事件名 / 事件字段。
> OpenAPI 路径总数（37 条）和现有 SSE 事件 schema 已用 `tests/baseline_openapi.json` + `tests/test_event_schema.py` 锁定。

---

## 3. 协议映射表（13 → 10）

| # | 旧协议（0.1.x） | 新协议（0.2.0） | 主要改动 |
|---|----------------|------------------|---------|
| 1 | `UserAuthProvider` | `UserAuthenticator` | 仅改名，签名同：`verify(request)` → `UserIdentity \| None`、`logout(request, response)` |
| 2 | `PermissionChecker` | `AuthorizationProvider` | 仅改名，签名同：`get_permissions` / `check` |
| 3 | `M2MAuthProvider` | `M2MAuthenticator` | 仅改名，签名同：`authenticate(request)` → `app_id \| None` |
| 4 | `OutboundAuthProvider` | `OutboundAuthProvider`（同名） | **签名变更**：详见 §4.1 |
| 5 | `RegistryProvider` + `MetadataManager` | `MetadataRepository` | 合并为一个，写侧方法签名合并（详见 §4.2） |
| 6 | `LLMProviderFactory` + `LLMProviderRegistry` + `LLMProviderStore` + `ExtraHeadersProvider` | `LLMProviderService` | 整体合并，详见 §4.4 |
| 7 | `SessionStoreProtocol` + `DatabaseProtocol` | `SessionRepository` | 合并；`DatabaseProtocol` 退入内部 |
| 8 | `EvalResultStorage` | `EvalResultRepository` | 仅改名 |
| 9 | `ConfigProvider` | `ConfigProvider` | **保持不变** |
| 10 | （新增） | `ToolScriptStore` | 上传型 tool 脚本存储；详见 §4.6 |

外加两个新增 dataclass：

- `OutboundRequestContext(request, target_url, target_type, identity, scenario_id, agent_name)` —— 替换旧 `OutboundAuthProvider.get_headers(request, target_url, target_type)` 的三个分散参数
- `LLMResolveSpec(agent, user)` —— `create_llm` 和 `build_resolver` 的入参

---

## 4. 关键 API 变化详解

### 4.1 `OutboundAuthProvider` — 签名变了

**旧（0.1.x）**：
```python
async def get_headers(
    self,
    request: Request,
    target_url: str,
    target_type: str,   # "tool" 或 "agent"
) -> dict[str, str]:
    ...
```

**新（0.2.0）**：
```python
@dataclass
class OutboundRequestContext:
    request: Any
    target_url: str
    target_type: str
    identity: str = ""           # 网关侧已解析（end-user 或 M2M app_id）
    scenario_id: str = ""        # 当前场景上下文（若有）
    agent_name: str = ""         # 当前 agent 上下文（若有）

async def get_headers(self, context: OutboundRequestContext) -> dict[str, str]:
    ...
```

迁移要点：

- 把方法体内的 `request` 参数改为 `context.request`。
- 把 `target_url` / `target_type` 改为 `context.target_url` / `context.target_type`。
- 新增的 `context.identity` / `scenario_id` / `agent_name` 是"网关侧已经解析好的上下文"，**不要重新解析鉴权**；直接读取即可。
- `M2MAuthProvider.get_identity_headers(request, identity)` 已经合并到这个方法里，删掉旧实现，把 header 合并逻辑搬到这里。

### 4.2 `MetadataRepository` — 读 + 写合并

旧版本你需要实现 `RegistryProvider`（只读）和 `MetadataManager`（读+写）两个类，互相继承。

**新版本**：只实现一个 `MetadataRepository`，包括：

- 读：`get_agent` / `list_agents` / `get_tool` / `list_tools` / `get_tools(names)`（**强制批量**）/ `get_scenario` / `list_scenarios`
- 写：`create_tool` / `update_tool` / `delete_tool` / `create_agent` / `update_agent` / `delete_agent` / `create_scenario` / `update_scenario` / `delete_scenario`
- 关系：`add_scenario_agent` / `remove_scenario_agent` / `add_agent_tool` / `remove_agent_tool`

> `get_tools(names: list[str]) -> dict[str, dict | None]` 是**强制批量方法**，必须实现。

### 4.3 `M2MAuthenticator` 不再返回 identity headers

**旧**：
```python
class M2MAuthProvider(Protocol):
    async def authenticate(self, request) -> str | None: ...
    async def get_identity_headers(self, request, identity) -> dict[str, str]: ...  # <-- 已删除
```

**新**：身份 header 在 `OutboundAuthProvider.get_headers` 里统一返回。`M2MAuthenticator` 只剩 `authenticate` + `close`。

### 4.4 `LLMProviderService` — 4 协议收 1

旧版本你需要分别实现：

| 旧协议 | 旧职责 | 新合并位置 |
|--------|--------|-----------|
| `LLMProviderFactory` | `list_provider_types()` + `create_llm(spec)` | `LLMProviderService.create_llm(LLMResolveSpec)` |
| `LLMProviderRegistry` | `create_resolver(agent)` 同步构造 | `LLMProviderService.build_resolver(specs)` 同步构造 |
| `LLMProviderStore` | provider config CRUD + `get_model_max_context` | `LLMProviderService` 上的同名方法 |
| `ExtraHeadersProvider` | 动态 HTTP header 回调 | `LLMProviderService.create_llm` 内部统一调用 |

入参 `LLMResolveSpec`：

```python
@dataclass
class LLMResolveSpec:
    agent: "AgentMetadata"   # minimal_harness.types.AgentMetadata
    user: str = ""           # 用户标识（用于租户级凭证）
```

**新方法签名**（与 `minimal_harness` 保持一致）：

```python
async def create_llm(self, spec: LLMResolveSpec) -> LLMProvider: ...

async def build_resolver(
    self, specs: list[LLMResolveSpec]
) -> Callable[[AgentMetadata], LLMProvider]:
    """必须同步返回闭包；所有网络 IO 在 `create_llm` 阶段完成。"""
```

如果你的旧实现里有一个独立的 `DefaultLLMProviderService` 或类似的封装类，保持它继承自 `LLMProviderService`、按上面的方法名重写即可。

### 4.5 `SessionRepository`

合并了 `SessionStoreProtocol` + `DatabaseProtocol`：

```python
class SessionRepository(Protocol):
    async def create_session(...) -> Any: ...
    async def get_session(self, session_id: str) -> Any | None: ...
    async def save_memory(self, memory, session_id, extra=None) -> None: ...
    async def update_usage(self, memory, session_id) -> None: ...
    async def delete_session(self, session_id: str) -> bool: ...
    async def list_sessions(self) -> list[Any]: ...
    async def list_user_sessions(self, user_id, scenario_id=None) -> list[Any]: ...
    async def get_session_messages(self, session_id) -> list[dict]: ...
    def get_messages_as_items(self, session) -> list[dict]: ...
    async def healthcheck(self) -> None: ...       # 新增；/ready 用
    async def close(self) -> None: ...
```

- 新增 `healthcheck()` —— 不需要做事的实现可以直接 `pass`，但请勿不实现（Pyright 会标红）。
- 如果你之前直接实现过 `DatabaseProtocol`，请把暴露的 SQL 接口藏起来，改为只实现 `SessionRepository`。

### 4.6 `ToolScriptStore` — 文件型 tool 脚本存储（新增）

0.2.0 引入"上传 `.py` 脚本生成 tool"的能力。`ToolScriptStore` 协议定义每个 app 怎么把脚本文件落到磁盘上：

```python
class ToolScriptStore(Protocol):
    async def save(self, name: str, content: bytes, overwrite: bool = False) -> str: ...
    async def read(self, name: str) -> bytes | None: ...
    async def delete(self, name: str) -> bool: ...
    async def exists(self, name: str) -> bool: ...
    async def close(self) -> None: ...
```

- `save()` 返回脚本落盘的绝对路径（用作 `ExternalScriptToolBinding.script_path`）。
- 不需要文件型 tool 的部署可以传 `None`：`create_app()` 在 `tool_script_store=None` 时会让 `/api/v1/management/tools/upload` 端点返回 `501 Not Implemented`。
- orch-app 默认实现：`LocalFileScriptStore`，存到 `./data/scripts/`；mh-local 默认实现：`LocalScriptStore`，存到 `~/.config/mh-local/scripts/`。

---

## 5. 迁移步骤（建议 5 步走）

> **强烈建议**按顺序执行。先做能独立验证的小步（grep + 替换 + 单测），再做需要联调的大步（lifespan）。

### Step 1 — 锁定影响面（预估工作量）

```bash
# 在你的项目里跑这三条 grep，看会命中多少文件
grep -r "create_app("             --include='*.py' . | wc -l
grep -r "LifespanHook"            --include='*.py' . | wc -l
grep -r "from mh_gateway.adapters" --include='*.py' . | wc -l
```

如果命中都是 `mh-local` / `mh-orch-app` 自己的代码，说明你没有自定义 Adapter 路径，可以直接升级依赖，**无需修改任何业务代码**。

### Step 2 — 新增"先 translate 再 wire"的 thin-shim 文件

这一节适合**还没准备好一次性重写装配代码**的客户。新建 `my_adapters_bridge.py`：

```python
"""把旧 14-hook 装配风格翻译成新的 AdapterLifespan，临时过渡。"""
from contextlib import asynccontextmanager
from typing import AsyncIterator

from mh_gateway.adapters import (
    AuthorizationProvider, ConfigProvider, EvalResultRepository,
    LLMProviderService, M2MAuthenticator, MetadataRepository,
    OutboundAuthProvider, SessionRepository, UserAuthenticator,
)
from mh_gateway.app import AdapterLifespan, GatewayAdapters


def build_lifespan_from_old_hooks(
    *,
    user_auth: UserAuthenticator,
    authorization: AuthorizationProvider,
    m2m_auth: M2MAuthenticator,
    outbound_auth: OutboundAuthProvider,
    metadata: MetadataRepository,
    llm: LLMProviderService,
    sessions: SessionRepository,
    eval_results: EvalResultRepository | None,
    config_provider: ConfigProvider,
) -> AdapterLifespan:
    @asynccontextmanager
    async def _lifespan(app) -> AsyncIterator[GatewayAdapters]:
        bundle = GatewayAdapters(
            settings=app.state.settings,
            user_auth=user_auth,
            authorization=authorization,
            m2m_auth=m2m_auth,
            outbound_auth=outbound_auth,
            metadata=metadata,
            llm=llm,
            sessions=sessions,
            eval_results=eval_results,
            config_provider=config_provider,
        )
        yield bundle
    return _lifespan
```

然后把旧的 14 个 hook 调用合并成一行：

```python
adapter_lifespan = build_lifespan_from_old_hooks(
    user_auth=MyUserAuthenticator(),
    authorization=MyAuthorizationProvider(),
    # ...
)
app = create_app(settings=settings, adapters=adapter_lifespan)
```

> 这步即可升级依赖并运行；之后有时间再合并自定义逻辑。

### Step 3 — 合并协议实现类

把以下旧类做最小化重命名/合并（可借助 IDE 的 "Rename Symbol"）：

| 旧类 | 操作 |
|------|------|
| `MyUserAuthProvider` | 改名 `MyUserAuthenticator` |
| `MyPermissionChecker` | 改名 `MyAuthorizationProvider` |
| `MyM2MAuthProvider` | 改名 `MyM2MAuthenticator`，**删除** `get_identity_headers` |
| `MyOutboundAuthProvider` | 改 `get_headers` 签名（见 §4.1）；把 `M2MAuthProvider.get_identity_headers` 的逻辑搬进来 |
| `MyRegistryProvider` + `MyMetadataManager` | 合并成 `MyMetadataRepository`，并 `implement`（强制）`get_tools(names)` 批量方法 |
| `MyLLMProviderFactory` + `MyLLMProviderRegistry` + `MyLLMProviderStore` + `MyExtraHeadersProvider` | 合并成 `MyLLMProviderService`，参照 §4.4 |
| `MySessionStoreProtocol` + `MyDatabaseProtocol` | 合并成 `MySessionRepository`，新增 `healthcheck()` |

`EvalResultStorage` → `EvalResultRepository`（仅改名）。`ConfigProvider` 保持原状。

### Step 4 — 写一个真实的 `AdapterLifespan`

如果你的项目结构允许直接组装（推荐），按 §6 模板实现 `build_my_lifespan()`。

### Step 5 — 业务路由 / 中间件里读 `app.state.adapters`

旧代码的 `app.state.adapters.token_verifier` 这类字段访问全部失效。请按 §7 表查找新字段名：

| 旧字段 | 新字段 |
|--------|--------|
| `token_verifier` | `user_auth` |
| `permission_checker` | `authorization` |
| `m2m_auth_provider` | `m2m_auth` |
| `outbound_auth_provider` | `outbound_auth` |
| `management_provider` | `metadata` |
| `registry_provider` | （删除——已合并入 `metadata`） |
| `llm_provider_factory` / `llm_provider_registry` / `llm_provider_store` / `llm_extra_headers_provider` | `llm` |
| `session_store_provider` | `sessions` |
| `database_provider` | （删除——由 `sessions` 内部使用） |
| `eval_result_storage` | `eval_results` |
| `secret_provider` | `config_provider`（如需） |
| （新增） | `tool_script_store`（上传型 tool 的脚本存储；不需要可传 `None`） |
| `settings` | `settings`（保持不变） |

> **强烈建议**：把 `app.state.adapters.<slot>` 全部封装到一个 `Depends(get_adapters)` 的依赖项里，统一入口，避免散落。

---

## 6. 可直接拷贝的 `AdapterLifespan` 模板

### 6.1 完整模板（推荐作为起点）

```python
"""myapp/adapters.py — 客户侧 AdapterLifespan 默认实现。"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from mh_gateway.adapters import (
    AuthorizationProvider,
    ConfigProvider,
    EvalResultRepository,
    LLMProviderService,
    LLMResolveSpec,
    M2MAuthenticator,
    MetadataRepository,
    OutboundAuthProvider,
    OutboundRequestContext,
    SessionRepository,
    ToolScriptStore,
    UserAuthenticator,
    UserIdentity,
)
from mh_gateway.app import AdapterLifespan, GatewayAdapters
from mh_gateway.config import ConfigSchema
from mh_gateway.llm import DefaultLLMProviderService

logger = logging.getLogger("myapp.adapters")


# ── 1. 客户自定义实现（按需实现，未列出的协议可以传 None，使用 orch-app 默认） ──


class MyUserAuthenticator(UserAuthenticator):
    """对接企业内部 SSO / OAuth2 / CAS."""

    async def verify(self, request) -> UserIdentity | None:
        ...

    async def logout(self, request, response) -> None:
        ...


class MyAuthorizationProvider(AuthorizationProvider):
    async def get_permissions(self, user_id: str) -> list[str]: ...

    async def check(self, user_id: str, permission: str) -> bool: ...


class MyM2MAuthenticator(M2MAuthenticator):
    async def authenticate(self, request) -> str | None: ...
    async def close(self) -> None: ...


class MyOutboundAuthProvider(OutboundAuthProvider):
    async def get_headers(self, ctx: OutboundRequestContext) -> dict[str, str]:
        headers: dict[str, str] = {}
        if ctx.identity:
            headers["x-user-id"] = ctx.identity
        # 如果是 M2M：
        # headers["x-app-id"] = ctx.identity
        return headers

    async def close(self) -> None: ...


class MyMetadataRepository(MetadataRepository):
    # 强制批量方法
    async def get_tools(self, names: list[str]) -> dict[str, dict | None]:
        # 必须实现：缺失填 None
        return {n: await self.get_tool(n) for n in names}

    # 其余 read / write / relationship 方法省略 ...
    async def close(self) -> None: ...


class MyLLMService(DefaultLLMProviderService):
    """继承 orch-app 提供的 DefaultLLMProviderService，按需 override。"""

    async def create_llm(self, spec: LLMResolveSpec):
        # 可在此注入租户级凭证
        return await super().create_llm(spec)


class MySessionStore(SessionRepository):
    async def healthcheck(self) -> None:
        # 自检数据库连接；若失败抛异常
        ...


class MyEvalStorage(EvalResultRepository):
    # 增量化评测结果保存
    ...


class MyConfigProvider(ConfigProvider):
    async def get(self, key: str) -> str | None: ...


class MyToolScriptStore(ToolScriptStore):
    """把上传的 tool 脚本存到企业私有的对象存储 / 配置中心。"""

    async def save(self, name: str, content: bytes, overwrite: bool = False) -> str:
        ...

    async def read(self, name: str) -> bytes | None: ...
    async def delete(self, name: str) -> bool: ...
    async def exists(self, name: str) -> bool: ...
    async def close(self) -> None: ...


# ── 2. 装配入口 ──────────────────────────────────────────────────────────────


def build_my_lifespan(
    settings: ConfigSchema,
    *,
    # ...你的自定义参数...
) -> AdapterLifespan:
    """构造 AdapterLifespan：每个 adapter 暴露一次"开启/关闭"语义。"""

    resources: list = []  # 需要 close 的资源

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[GatewayAdapters]:
        # ── 资源初始化（替换旧 14-hook 里的"yields before app.state.adapters"）──
        user_auth = MyUserAuthenticator()
        authorization = MyAuthorizationProvider()
        m2m_auth = MyM2MAuthenticator()
        outbound_auth = MyOutboundAuthProvider()
        metadata = MyMetadataRepository()
        await metadata.warmup() if hasattr(metadata, "warmup") else None
        llm = MyLLMService()
        sessions = MySessionStore()
        await sessions.warmup() if hasattr(sessions, "warmup") else None
        eval_results: EvalResultRepository | None = MyEvalStorage() if settings.enable_eval else None
        config_provider = MyConfigProvider()
        # tool_script_store 可选：None 时上传端点会返回 501
        tool_script_store: ToolScriptStore | None = MyToolScriptStore()

        resources.extend([
            user_auth, authorization, m2m_auth, outbound_auth,
            metadata, llm, sessions, eval_results, config_provider,
            tool_script_store,
        ])

        bundle = GatewayAdapters(
            settings=settings,
            user_auth=user_auth,
            authorization=authorization,
            m2m_auth=m2m_auth,
            outbound_auth=outbound_auth,
            metadata=metadata,
            llm=llm,
            sessions=sessions,
            eval_results=eval_results,
            config_provider=config_provider,
            tool_script_store=tool_script_store,
        )

        try:
            yield bundle
        finally:
            for r in reversed(resources):
                try:
                    close = getattr(r, "close", None)
                    if close is not None:
                        result = close()
                        if hasattr(result, "__await__"):
                            await result
                except Exception:
                    logger.exception("Failed to close %r", r)
```

### 6.2 接入 `create_app`

```python
# myapp/main.py
from fastapi import FastAPI
from mh_gateway.app import create_app
from mh_gateway.config import ConfigSchema

from myapp.adapters import build_my_lifespan


def create_my_app(settings: ConfigSchema) -> FastAPI:
    lifespan = build_my_lifespan(settings, some_arg="...")
    return create_app(settings=settings, adapters=lifespan)


app = create_my_app(settings)
```

---

## 7. `app.state.adapters` 字段名对照表

| 旧字段（0.1.x AppState） | 新字段（0.2.0 GatewayAdapters） |
|---------------------------|-----------------------------------|
| `settings` | `settings` |
| `token_verifier` | `user_auth` |
| `permission_checker` | `authorization` |
| `m2m_auth_provider` | `m2m_auth` |
| `outbound_auth_provider` | `outbound_auth` |
| `management_provider` | `metadata` |
| `registry_provider`（已 deprecate） | （删除，访问 `metadata`） |
| `llm_provider_factory` | `llm` |
| `llm_provider_registry` | `llm` |
| `llm_provider_store` | `llm` |
| `llm_extra_headers_provider` | `llm`（内部 use） |
| `session_store_provider` | `sessions` |
| `database_provider` | （访问 `sessions`，数据库是它的私有细节） |
| `eval_result_storage` | `eval_results` |
| `secret_provider` | `config_provider` |
| （新增） | `tool_script_store`（上传型 tool 脚本存储；不需要可传 `None`） |

> `app.state.adapters` 从可变对象变为 `@dataclass(frozen=True, slots=True)` 的不可变 bundle —— **运行时不可再赋新值**；只能整体替换。

---

## 8. 常见陷阱

### 陷阱 1：`EvalResultRepository` 是可选的，但有条件

```python
# 当 settings.enable_eval=True 时，必须提供 eval_results
# 否则 create_app 内会抛 RuntimeError
if settings.enable_eval and bundle.eval_results is None:
    raise RuntimeError("settings.enable_eval=True but eval_results is None")
```

**修正**：

```python
eval_results: EvalResultRepository | None = None
if settings.enable_eval:
    eval_results = MyEvalStorage(...)
```

### 陷阱 2：`OutboundAuthProvider` 不再单独接 `request`

如果你实现的旧版本里用到了 `await request.headers.get(...)`：

```python
# 旧：async def get_headers(self, request, target_url, target_type):
#     token = request.headers.get("authorization")
# 新：
async def get_headers(self, ctx: OutboundRequestContext) -> dict[str, str]:
    token = ctx.request.headers.get("authorization")
```

### 陷阱 3：`MetadataRepository.get_tools(names)` 必须实现为批量

```python
# 错误：仍然走 N+1
async def get_tools(self, names):
    return [await self.get_tool(n) for n in names]

# 正确：返回 dict，缺失填 None
async def get_tools(self, names: list[str]) -> dict[str, dict | None]:
    return {n: await self.get_tool(n) for n in names}
```

如果你的后端是关系数据库，这一步通常是用 `WHERE name IN (...)` 的批量 SQL 一次取回。

### 陷阱 4：`LLMProviderService.build_resolver` 必须异步做完全部网络 IO

```python
async def build_resolver(self, specs):
    # 全部 LLM 凭证必须在 await 阶段预先加载
    llms = {spec.agent.name: await self.create_llm(spec) for spec in specs}

    # 返回同步闭包：禁止再做 await
    def resolver(agent: AgentMetadata) -> LLMProvider:
        return llms[agent.name]
    return resolver
```

运行时会在异步框架下调用 resolver 同步路径；任何这里残留的 `await` 都会触发 `RuntimeError`。

### 陷阱 5：`app.state.adapters` 是不可变 bundle

```python
# 旧：能赋值
app.state.adapters.sessions = my_new_store

# 新：失败（FrozenInstanceError）
app.state.adapters.sessions = my_new_store

# 正确：替换 lifespan，重新启动；或在测试里 monkey-patch GatewayAdapters 上的方法
```

### 陷阱 6：忘了把 `httpx.AsyncClient` 之类的资源 close

`UserAuthenticator` / `M2MAuthenticator` / `OutboundAuthProvider` 等协议都暴露了 `async def close(self) -> None`，是 lifespan 关闭阶段的钩子。如果你的实现里持有 HTTP 客户端、连接池、Redis 连接等，请**实现 `close()` 并在 lifespan finally 块里调用**（如 §6.1 模板）。

---

## 9. 验证你的迁移

### 9.1 静态检查

```bash
# 找出所有旧命名残留
grep -rn "AppState\|UserAuthProvider\|PermissionChecker\|M2MAuthProvider" \
    --include='*.py' .
grep -rn "LifespanHook\|ExtraHeadersProvider\|SecretResolver\|DatabaseProtocol" \
    --include='*.py' .

# 找出旧字段访问
grep -rn "adapters\.token_verifier\|adapters\.permission_checker\|adapters\.m2m_auth_provider\|adapters\.outbound_auth_provider\|adapters\.llm_provider_\|adapters\.session_store_provider\|adapters\.database_provider\|adapters\.eval_result_storage\|adapters\.registry_provider\|adapters\.management_provider\|adapters\.secret_provider" \
    --include='*.py' .
```

以上 grep 应该返回**空**（或只命中迁移期临时 shim）。

### 9.2 跑 mh-gateway 自带测试

```bash
# 在客户项目根的 Python 环境
pip install 'mh-gateway>=0.2.0a1'

# OpenAPI 路径钉子：客户业务侧无关，但能立刻发现 SDK 是否悄悄改了 HTTP 表面
pytest tests/test_openapi_baseline.py -v

# SSE 事件 schema 钉子
pytest tests/test_event_schema.py -v
```

### 9.3 端到端冒烟测试（最少 5 项）

| # | 用例 | 期望 |
|---|------|------|
| 1 | `GET /api/v1/scenarios` | 200，返回默认场景或客户自定义场景列表 |
| 2 | `POST /api/v1/sessions`（携带客户自己的鉴权 token） | 200，返回新 session；title 字段不再是 `"Untitled"` |
| 3 | `POST /api/v1/chat/{session_id}` | 200，SSE 流正常，事件 `LLMChunk` / `LLMEnd` 顶层含 `content` / `reasoning_content` |
| 4 | `GET /api/v1/sessions` | 200，message_count 正确 |
| 5 | 关闭服务（SIGTERM） | 所有 adapter 的 `close()` 被调用，无悬空连接日志 |

### 9.4 性能回归

迁移前后跑一次相同的 chat 压测，关注：

- **首次 LLM 调用前时间** 应该下降（旧版本在 `runtime_service` 热路径里同步请求数据库，新版本在 lifespan 阶段 / 第一次进入前用 `build_resolver` 预加载）。
- **数据库连接数** 应该下降（不再每个 agent run 都直连，而是 `sessions` 内部的池）。
- **/ready 响应时间** 不应退化（`healthcheck()` 默认实现是 noop）。

---

## 10. 附录

### 10.1 已被移除的导入符号

```python
# 这些导入符号在新版本统一 ImportError，请全局替换：

from mh_gateway.adapters import AppState              # ← 删除
from mh_gateway.adapters import UserAuthProvider      # ← 改 UserAuthenticator
from mh_gateway.adapters import PermissionChecker     # ← 改 AuthorizationProvider
from mh_gateway.adapters import M2MAuthProvider       # ← 改 M2MAuthenticator
from mh_gateway.adapters import RegistryProvider      # ← 删除（合并入 metadata）
from mh_gateway.adapters import MetadataManager       # ← 改 MetadataRepository
from mh_gateway.adapters import LLMProviderFactory    # ← 删除（合并入 llm）
from mh_gateway.adapters import LLMProviderRegistry   # ← 删除（合并入 llm）
from mh_gateway.adapters import LLMProviderStore      # ← 删除（合并入 llm）
from mh_gateway.adapters import DatabaseProtocol      # ← 删除（私有化）
from mh_gateway.adapters import SessionStoreProtocol  # ← 改 SessionRepository
from mh_gateway.adapters import EvalResultStorage     # ← 改 EvalResultRepository
from mh_gateway.adapters import SecretResolver        # ← 改 ConfigProvider
from mh_gateway.adapters import ExtraHeadersProvider  # ← 删除（合并入 llm）
from mh_gateway.app import LifespanHook               # ← 删除（直接用 AdapterLifespan）
from mh_gateway.llm import ExtraHeadersProvider       # ← 删除（同上）
from mh_gateway.adapters import ToolScriptStore       # ← 新增；上传型 tool 脚本存储
```

### 10.2 OpenAPI 兼容性保证

`tests/baseline_openapi.json` 钉死了 **37 条** HTTP 路径（前缀、`/api/v1/...` 路由 ID、HTTP 方法），
只要此测试通过，REST 客户端无需做任何调整。同样的，`tests/test_event_schema.py` 钉死了 SSE 事件
15 项字段契约（含 `LLMChunk.content` / `LLMEnd.usage` 等），前端可直接消费。

### 10.3 主要 commit（供回溯）

```
refactor/adapters 分支，按时间顺序：

  c6b6ed2  refactor(adapters): collapse 13 protocols into 9 unified adapters
  5b14b03  chore(gateway): lint, type, version bump, CHANGELOG entry
  b51be4c  refactor(orch-app): align with gateway's unified adapter surface
  f54d653  refactor(mh-local): align with gateway's unified adapter surface
```

### 10.4 升级到下一个 minor（0.3.x）前的预备

- 把 `app.state.adapters.<slot>` 访问收敛到一处（建议 `Depends(get_adapters)` 模式）。
- 把 `OutboundAuthProvider` 的调试日志结构化，便于跨服务追踪身份传播。
- 把自定义 adapter 的连接资源统一登记到 lifespan 的 `resources` 列表，便于 §10.1 中提到的 `close()` 钩子回归。

---

## 反馈与协助

迁移过程中如遇到：

- **`ImportError` 找不到旧符号** → 用 §10.1 列表查新符号名。
- **`FrozenInstanceError`** → 你的代码可能试图 mutate `app.state.adapters.<field>`；用 §5 Step 5 替换字段访问方式。
- **`create_app` 启动时 `RuntimeError("eval_results is None")`** → 参考陷阱 1。
- **签名不匹配的 `get_headers`** → 参考 §4.1。

可在企业内 Slack/GitLab issues 同步给我们，提供：

```bash
python -c "import mh_gateway; print(mh_gateway.__version__)"
pip freeze | grep -E 'mh-gateway|minimal-harness|mh-service-kit'
```

— `mh-gateway` 团队
