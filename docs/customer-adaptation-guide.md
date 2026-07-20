# 企业适配开发指导

本文档面向 **客户企业的开发人员**，说明如何将 `mh-gateway` 集成到企业自己的环境中。

---

## 架构概述

`mh-gateway` 是一个 FastAPI 应用，通过 **LifespanHook 接口** 与企业的认证、权限、注册中心、配置中心等外部系统解耦。配置统一由 `ConfigManager` 管理，它是一个部署单元级别的配置工具，不从属于 `create_app`。

```
┌─────────────────────────────────────────────────┐
│                 uvicorn my_app:app               │
│  ┌───────────────────────────────────────────┐  │
│  │         mh_gateway          │  │
│  │  ┌─────────┐  ┌────────┐  ┌───────────┐  │  │
│  │  │ Chat API │  │ Agents │  │ Sessions  │  │  │
│  │  └────┬────┘  └───┬────┘  └─────┬─────┘  │  │
│  │       │           │             │         │  │
│  │  ┌────▼───────────▼─────────────▼──────┐  │  │
│  │  │         Adapter Layer               │  │  │
│  │  │  (通过 LifespanHook 注入你的实现)     │  │  │
│  │  └────▲───────────▲─────────────▲──────┘  │  │
│  └───────┼───────────┼─────────────┼─────────┘  │
│          │           │             │            │
│     ┌────┴───┐ ┌────┴────┐ ┌──────┴──────┐    │
│     │  企业   │ │ 企业权限 │ │ 企业配置中心 │    │
│     │  SSO   │ │ 系统     │ │ / Vault     │    │
│     └────────┘ └─────────┘ └─────────────┘    │
└─────────────────────────────────────────────────┘
```

---

## 开箱即用

安装后无需任何代码即可启动，内置默认 adapter 使用内存数据：

```bash
uvicorn mh_gateway.main:app --port 8005
```

内置的默认用户（通过 `X-User-Id` 请求头或 `x-user-id` cookie 指定用户标识）：

| 用户标识 | 角色 | 权限 |
|----------|------|------|
| `1` | admin | `use:*:*`、`manage:*:*` |
| `2` | member | `use:agent:triage`、`use:tool:calculator`、`use:scene:triage`、`manage:scene:*` |
| `3` | user | `use:agent:code-reviewer`、`use:agent:writer`、`use:scene:code_review`、`use:scene:writing` |
| `4` | scene-manager | `manage:scene:*` |
| `5` | agent-manager | `manage:agent:*` |
| `6` | tool-manager | `manage:tool:*` |

> 开发模式下可通过 `GET /api/v1/dev/login` 访问 Mock SSO 登录页快速切换用户身份（由 `mh-orch-app` 提供）。

内置的默认数据（`ORCH_DEV_MODE=true` 时可用）：
- **Agent**: `triage`、`code-reviewer`、`writer`
- **Tool**: `calculator`、`handoff`、`discover_agents`、`show_ui_meta`、`general_visualization`、`stop_agent`
- **Scenario**: `triage`、`code_review`、`writing`

### 内置 Agent 开关

内置 agent 通过 `ORCH_DEV_MODE=true` 环境变量控制（默认 `false`）：
- **开箱即用**：设置为 `true`，服务暴露 `triage`、`code-reviewer`、`writer` 三个样例 agent 以及开发调试用工具端点
- **生产环境**：设为 `false`，此时 `InMemoryManagementProvider` 不返回任何数据，由企业通过 `management_provider` LifespanHook 注入自己的注册中心

| Agent | 英文名 | 中文名 | 说明 |
|-------|--------|--------|------|
| `triage` | General Assistant | 通用助手 | 理解用户需求并路由到专业 agent。本地执行。 |
| `code-reviewer` | Code Reviewer | 代码审查 | 分析代码缺陷、风格、安全、性能问题。通过 M2M 端点执行。 |
| `writer` | Writing Assistant | 写作助手 | 撰写文章、邮件、报告等内容。通过 M2M 端点执行。 |

> 内置 agent 的 system_prompt 支持中英文，根据前端传来的 `Accept-Language` 自动适配。
>
> 内置 Tool 包括 `stop_agent` — 可用于在 Tool 执行完成后立即停止 Agent 循环，不再调用 LLM 生成后续回复。详见 `minimal-harness` 文档。

---

## 安装

### 前置条件

- Python ≥ 3.12
- pip 或 uv

### 安装包

从我们提供的交付包中安装：

```bash
pip install minimal_harness-0.6.2a17-py3-none-any.whl
pip install mh_gateway-0.1.2a14-py3-none-any.whl
```

验证安装：

```bash
python -c "from mh_gateway import create_app; print('OK')"
```

---

## 需要实现的 Adapter 接口

以下是所有可以注入的可选/必选接口。Adapter 实例需通过 **LifespanHook** 注入到 `create_app()`——每个 adapter 都有对应的命名 hook 参数（如 `token_verifier=my_hook`），hook 在应用启动时执行，负责创建 adapter 实例并挂载到 `app.state.adapters` 上。**未注入的接口会使用内置默认实现**（开发/演示友好，生产环境建议全部注入）。

### 1. UserAuthProvider（认证）

验证用户身份（JWT/SAML/OIDC/Cookie），返回用户身份。

```python
from mh_gateway.auth import UserAuthProvider, UserIdentity

class MyUserAuthProvider(UserAuthProvider):
    async def verify(self, request: Any) -> UserIdentity | None:
        cookie = request.cookies.get("sessionid")
        if not cookie:
            return None
        user_info = await self._call_auth_service(cookie)
        return UserIdentity(
            user_id=user_info["employee_id"],
            username=user_info["name"],
            roles=user_info.get("roles", []),
            extra_data=user_info,
        )
```

| 方法 | 返回 | 说明 |
|------|------|------|
| `verify(request)` | `UserIdentity \| None` | 收到完整 HTTP Request，可读 Cookie/Header/调用外部 API |

### 2. PermissionChecker（权限校验）

校验用户是否有权限执行某个操作。

```python
from mh_gateway.auth import PermissionChecker

class MyPermissionChecker(PermissionChecker):
    async def get_permissions(self, user_id: str) -> list[str]:
        return ["agent:code_review:execute", "tool:run:*"]

    async def check(self, user_id: str, permission: str) -> bool:
        perms = await self.get_permissions(user_id)
        from mh_gateway.auth import match_permission
        return match_permission(perms, permission)
```

> `UserAuthProvider` 和 `PermissionChecker` 可以用同一个类实现（参考内置 `_DefaultAuthProvider`）。

### 3. MetadataManager（Agent / Tool / Scenario 注册中心 + CRUD）

推荐的统一读写协议。提供 Agent 元数据、Tool 定义、Scenario 列表的读取和写入。

```python
from mh_gateway.adapters import MetadataManager

class MyRegistry(MetadataManager):
    # ── 只读（继承自 RegistryProvider） ─
    async def get_agent(self, name: str) -> dict | None: ...
    async def list_agents(self) -> list[dict]: ...
    async def get_tool(self, name: str) -> dict | None: ...
    async def list_tools(self) -> list[dict]: ...
    async def get_scenario(self, scenario_id: str) -> dict | None: ...
    async def list_scenarios(self) -> list[dict]: ...

    # ── 可选性能优化 ────────────────────
    # get_tools(names: list[str]) -> dict[str, dict | None]
    # 批量获取工具元数据，避免 N 次独立 get_tool 调用（运行时会自动检测并使用）
```

    # ── Agent CRUD ──────────────────────
    async def create_agent(self, agent: dict) -> dict: ...
    async def update_agent(self, name: str, agent: dict) -> dict: ...
    async def delete_agent(self, name: str) -> None: ...

    # ── Tool CRUD ───────────────────────
    async def create_tool(self, tool: dict) -> dict: ...
    async def update_tool(self, name: str, tool: dict) -> dict: ...
    async def delete_tool(self, name: str) -> None: ...

    # ── Scenario CRUD ───────────────────
    async def create_scenario(self, scenario: dict) -> dict: ...
    async def update_scenario(self, scenario_id: str, scenario: dict) -> dict: ...
    async def delete_scenario(self, scenario_id: str) -> None: ...

    # ── 关系管理 ────────────────────────
    async def add_scenario_agent(self, scenario_id, agent_name, tool_names=None) -> dict: ...
    async def remove_scenario_agent(self, scenario_id, agent_name) -> dict: ...
    async def add_agent_tool(self, scenario_id, agent_name, tool_name) -> dict: ...
    async def remove_agent_tool(self, scenario_id, agent_name, tool_name) -> dict: ...

    async def close(self) -> None: ...
```

返回的 dict 结构：

**Agent:**
```json
{
  "name": "code_review",
  "display_name": "代码审查助手",
  "display_name_locale": "{\"zh\":\"代码审查助手\",\"en\":\"Code Reviewer\"}",
  "description": "对 Git 提交进行代码审查",
  "description_locale": "{\"zh\":\"对 Git 提交进行代码审查\",\"en\":\"Review code changes\"}",
  "system_prompt": "You are a code reviewer...",
  "system_prompt_locale": "{\"zh\":\"你是一个代码审查助手...\",\"en\":\"You are a code reviewer...\"}",
  "endpoint_url": "/api/v1/agents/code_review/run",
  "provider": "openai",
  "model": "gpt-4o",
  "llm_config": {"temperature": 0.7, "max_tokens": 4096}
}
```

> `endpoint_url` 非空则视为远程 Agent（M2M 端点），空则本地执行。
> `display_name_locale`、`description_locale`、`system_prompt_locale` 是 JSON 字符串（非 dict），用于向后兼容。

**Tool:**
```json
{
  "name": "web_search",
  "display_name": "网络搜索",
  "display_name_locale": "{\"zh\":\"网络搜索\",\"en\":\"Web Search\"}",
  "description": "搜索网络信息",
  "description_locale": "{\"zh\":\"搜索网络信息\",\"en\":\"Search the web\"}",
  "parameters": {
    "type": "object",
    "properties": {
      "query": {"type": "string", "description": "搜索关键词"}
    },
    "required": ["query"]
  },
  "endpoint_url": "/api/v1/tools/web_search/execute",
  "source_code": "async def run(**kwargs): ..."
}
```

> `endpoint_url` 非空则远程执行，空则使用 `_fn`（本地异步函数）或 `source_code`（生成工具）执行。

**Scenario:**
```json
{
  "id": "code_review",
  "name": "代码审查",
  "name_locale": "{\"zh\":\"代码审查\",\"en\":\"Code Review\"}",
  "icon": "💻",
  "description": "对 MR 进行自动化代码审查",
  "description_locale": "{\"zh\":\"对 MR 进行自动化代码审查\",\"en\":\"Automated code review for MRs\"}",
  "agents": [{"name": "code-reviewer", "tool_names": ["discover_agents"]}]
}
```

### 4. OutboundAuthProvider（出站认证注入）

为远程 agent / tool 调用注入认证 header。当 mh-gateway 调用外部 agent 或 tool 的 HTTP 端点时，此 adapter 为出站请求添加身份凭证。

默认实现将当前请求的所有非 hop-by-hop header 透传给下游。客户可替换为自定义逻辑（如服务间 HMAC 签名、mTLS 客户端证书等）。

```python
from mh_gateway import OutboundAuthProvider


class MyOutboundAuthProvider(OutboundAuthProvider):
    async def get_headers(
        self,
        request: Any,
        target_url: str,
        target_type: str,  # "agent" | "tool"
    ) -> dict[str, str]:
        token = get_current_auth_token()
        # 替换为企业的服务间认证机制
        return {
            "Authorization": f"Bearer {token}",
            "X-Custom-Auth": await self._sign(request, target_url),
        }
```

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_headers(request, target_url, target_type)` | `dict[str, str]` | 返回要注入到出站请求中的 header 字典 |

### 5. M2MAuthProvider（机机接口鉴权）

验证 `POST /api/v1/agents/{name}/run` 和 `POST /api/v1/tools/*/execute`
等机机端点的调用方身份，并将身份信息注入出站 binding 的 HTTP 请求 header。

默认实现允许所有请求并返回 `"default"`。**生产环境必须替换为基于 SOA 或其他机制的鉴权实现**：

```python
from mh_gateway import M2MAuthProvider


class MyM2MAuthProvider(M2MAuthProvider):
    async def authenticate(self, request: Any) -> str | None:
        # 通过 SOA 验证 Authorization header
        auth = request.headers.get("Authorization", "")
        app_info = await self._soa_verify(auth)
        if app_info is None:
            return None
        return app_info.app_id

    async def get_identity_headers(
        self, request: Any, identity: str
    ) -> dict[str, str]:
        # 向下游出站绑定注入身份标识
        return {"X-SOA-Token": await self._soa_sign(identity)}

    async def close(self) -> None:
        pass
```

| 方法 | 返回 | 说明 |
|------|------|------|
| `authenticate(request)` | `str \| None` | 返回 app_id 表示鉴权通过，`None` 表示失败（401） |
| `get_identity_headers(request, identity)` | `dict[str, str]` | 返回注入到出站 binding 的身份 header，下游 M2MAuthProvider 通过 `authenticate` 接收 |

### 6. UserIdentity（用户身份扩展）

`UserIdentity` 支持 `extra_data: dict[str, Any]` 字段，可保留企业用户模型的全部字段：

```python
identity = UserIdentity(
    user_id="emp_12345",
    username="zhangsan",
    roles=["developer", "admin"],
    extra_data={
        "employee_id": "EMP-12345",
        "display_name": "张三",
        "email": "zhangsan@company.com",
        "department": "R&D",
        "avatar_url": "https://sso.company.com/avatar/12345",
    },
)
```

> 系统内部仅消费 `user_id` 字符串用于权限和 Session 归属。`extra_data` 可供未来扩展或自定义中间件使用。

### 7. ConfigProvider（外部配置 / 密钥管理，可选）

对接外部配置中心（Apollo / Nacos / Consul）或密钥管理（HashiCorp Vault / AWS Secrets Manager / 阿里云 KMS）。

`ConfigProvider` 是唯一的协议类型；`SecretResolver` 是其向后兼容的别名。
`ConfigManager` 接受两个 `ConfigProvider` 实例——分别用于普通配置和敏感配置（见下方代码示例）。

```python
from mh_gateway import ConfigProvider

class ApolloConfigProvider(ConfigProvider):
    async def get(self, key: str) -> str | None:
        return await apollo_client.get_value(key)

class VaultSecretResolver(ConfigProvider):
    async def get(self, key: str) -> str | None:
        return await vault_client.read_secret(key)
```

---

## Per-Request 上下文

Adapter 接口中的方法（如 `MetadataManager.list_agents()`、`PermissionChecker.check()`）默认不接收 HTTP 请求
对象。如果你的 Adapter 需要感知当前请求（例如按用户身份过滤数据、转发 token 给下游），可以通过
**`ContextVar`** 获取。

所有 per-request 上下文由中间件在请求进入时自动初始化，请求结束时自动
清理，无需手动管理。

### 可用上下文 API

所有函数都可以从 `mh_gateway` 顶层导入：

```python
from mh_gateway import (
    get_current_request,     # 获取完整 Request 对象
    get_current_cookies,     # 获取当前请求的 Cookie 字典
    get_current_auth_token,  # 获取认证凭证（Bearer token / cookie）
    get_current_user_id,     # 获取已认证的用户 ID
    get_current_locale,      # 获取 Accept-Language（"zh" / "en"）
    get_current_trace_id,    # 获取链路追踪 ID（X-Request-Id 或自动生成）
)
```

| 函数 | 返回 | 数据来源 | 说明 |
|------|------|----------|------|
| `get_current_request()` | `Request \| None` | 中间件 | 完整的 FastAPI Request 对象，可在 Adapter 中检查任意 header / body |
| `get_current_cookies()` | `dict[str, str]` | `request.cookies` | Cookie 字典，等价于 `get_current_request().cookies` |
| `get_current_auth_token()` | `str` | `Authorization` 头 → cookie 回退 | 优先取 `Bearer <token>`，无则取 `sessionid` / `sid` / `token` cookie |
| `get_current_user_id()` | `str \| None` | `get_user_id()` 认证后缓存 | 在 `get_user_id()` 调用后可用，`None` 表示未认证 |
| `get_current_locale()` | `str` | `Accept-Language` 头 | 默认返回 `"zh"`，完整的 locale 解析仍使用 `parse_locale()` |
| `get_current_trace_id()` | `str` | `X-Request-Id` / `X-Trace-Id` 头，或自动生成 | 用于日志和分布式追踪 |

> `get_current_user_id()` 仅在请求经过认证后（`get_user_id()` 被调用）才返回有效值。如果 Adapter
> 在未认证的请求路径中被调用，返回 `None`。

### Adapter 中使用示例

```python
from mh_gateway.adapters import MetadataManager
from mh_gateway import get_current_user_id, get_current_auth_token


class MyRegistry(MetadataManager):
    async def list_agents(self) -> list[dict]:
        user_id = get_current_user_id()
        token = get_current_auth_token()
        # 调用企业后端 API，带上用户认证信息
        return await self._http.get(
            "/api/agents",
            headers={"Authorization": f"Bearer {token}"},
            params={"user_id": user_id},
        )

    async def list_tools(self) -> list[dict]:
        locale = get_current_locale()
        # 按语言返回不同的 tool 描述
        ...
```

```python
from mh_gateway import get_current_request, get_current_trace_id


class MyPermissionChecker(PermissionChecker):
    async def check(self, user_id: str, permission: str) -> bool:
        request = get_current_request()
        trace_id = get_current_trace_id()
        # IP 白名单 + 链路追踪
        client_ip = request.client.host if request and request.client else "unknown"
        logger.info("perm_check", extra={"trace_id": trace_id, "user_id": user_id, "ip": client_ip})
        ...
```

---

## 统一配置管理（ConfigManager）

`ConfigManager` 是一个 **部署单元级别的配置管理工具**，不从属于 `create_app`。它提供一个统一的解析管道：

```
每个字段的解析优先级:
  1. {PREFIX}_{FIELD} 环境变量（最高优先级）
  2. 敏感字段 → secret_resolver.get()（如果配置了）
  3. 非敏感字段 → config_provider.get()（如果配置了）
  4. 可选字段（有默认值）仍缺失 → 使用模型默认值

> 注：`config_provider` 和 `secret_resolver` 均为 `ConfigProvider` 协议类型，可传入不同实例以区分配置源与密钥源。
```

> **重要：** 所有 Config 类（包括自定义的）**必须继承 `pydantic.BaseModel`**。`ConfigManager.resolve()` 在运行时通过 `model_fields` 读取字段定义，并使用 `**kwargs` 调用构造器。普通 Python 类会导致运行时错误。

### 基本用法

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from mh_gateway import ConfigManager, ConfigSchema, create_app
from pydantic import BaseModel


class MyRegistryConfig(BaseModel):
    api_url: str = "http://localhost:8080"
    api_key: str = ""


config_mgr = ConfigManager(
    config_provider=ApolloConfigProvider(),    # 可选
    secret_resolver=VaultSecretResolver(),     # 可选
)


@asynccontextmanager
async def my_management_provider(app: FastAPI):
    """lifespan hook: 应用启动时解析配置并挂载 adapter"""
    cfg = await config_mgr.resolve(
        MyRegistryConfig,
        prefix="my.registry",
        sensitive_fields={"api_key"},
    )
    app.state.adapters.management_provider = MyRegistry(
        api_url=cfg.api_url, api_key=cfg.api_key,
    )
    yield


settings = await config_mgr.resolve(ConfigSchema, prefix="ORCH")

app = create_app(
    settings=settings,
    management_provider=my_management_provider,
)
```

### `resolve()` 方法细节

```python
async def resolve(
    self,
    schema_cls: type[T],           # T 必须是 BaseModel 子类
    *,
    prefix: str = "ORCH",          # env 前缀 / 远程配置 key 前缀
    sensitive_fields: set[str] | None = None,
    key_mapping: dict[str, str] | None = None,   # 远程配置 key 重映射
) -> T:
```

### 开箱即用（仅环境变量）

不传 `config_provider` / `secret_resolver` 时，`ConfigManager` 只从环境变量读取：

```python
config_mgr = ConfigManager()   # 无需参数
settings = await config_mgr.resolve(ConfigSchema, prefix="ORCH")
# 只读 ORCH_DB_PATH, ORCH_DB_AUTO_SCHEMA 等 env var
```

### `ConfigSchema` 框架配置声明

`ConfigSchema` 继承自 `pydantic.BaseModel`。所有字段都有默认值，因此开箱即可启动。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `db_path` | str | `"./sessions.db"` | SQLite 数据库文件路径 |
| `db_auto_schema` | bool | `false` | 自动建表 |
| `cors_origins` | list[str] | `[]` | 跨域来源 |
| `dev_mode` | bool | `false` | 开发模式开关，开启后暴露内置 agent、开发工具端点、SSO 登录及前端静态文件 |
| `enable_eval` | bool | `true` | 是否暴露评测接口 |
| `eval_results_dir` | str | `"./eval_results"` | 评测结果存储目录 |
| `log_level` | str | `"INFO"` | 日志级别（ConfigSchema 声明字段，但日志配置通过 `MH_LOG_LEVEL` 环境变量或自行配置 root logger） |
| `verify_agent_tool_ssl` | bool | `false` | 调用远程 agent/tool 时是否验证 SSL 证书 |
| `metrics_enabled` | bool | `false` | 启用指标采集和 `/api/v1/metrics` 端点 |
| `metrics_push_interval` | int | `60` | 指标推送间隔（秒） |

> **LLM 配置不再在 ConfigSchema 中**。LLM 配置通过 `LLMProviderRegistry` + 环境变量 `ORCH_PROVIDER_{NAME}__{KEY}` 设置。例如 `ORCH_PROVIDER_OPENAI__API_KEY=sk-xxx`。

### 为什么需要 ConfigManager？

| 之前（各自为政） | 之后（统一管理） |
|---|---|
| 框架用 `ConfigProvider` 读自身 `Settings`，给出默认值 | 框架声明 `ConfigSchema`，部署方赋值 |
| Adapter 自己在构造函数里读 env / 硬编码 | Adapter 也通过 `ConfigManager.resolve()` 拿到配置 |
| 两个管道，两份配置来源 | 一个管道，同一份配置中心 |
| 配置有默认值，生产容易漏配 | 可选字段有明确默认值，必填（如果有）缺失直接报错 |

---

## 开箱默认 Adapter 的调测方式

内置默认 adapter 接受可选参数，无需实现完整接口即可调测。

### 调整管理员权限

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from mh_gateway.app import create_app
from mh_gateway.config import ConfigSchema
from mh_gateway.services.auth_client import _DefaultAuthProvider


@asynccontextmanager
async def my_auth_hook(app: FastAPI):
    auth = _DefaultAuthProvider(
        permissions={
            "1": ["*:*:*"],
            "2": ["use:agent:code-reviewer"],
        },
    )
    app.state.adapters.token_verifier = auth
    app.state.adapters.permission_checker = auth
    yield

settings = ConfigSchema(
    db_path="./test.db",
)
app = create_app(settings=settings, token_verifier=my_auth_hook)
```

### 调整注册中心数据

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from mh_gateway.app import create_app
from mh_gateway.config import ConfigSchema
from mh_gateway.services.management_provider import InMemoryManagementProvider


@asynccontextmanager
async def my_management_provider(app: FastAPI):
    mgmt = InMemoryManagementProvider(
        extra_agents=[{"name": "my-agent", "display_name": "My Agent", "description": "..."}],
        extra_tools=[{"name": "my-tool", "display_name": "My Tool", "description": "..."}],
        enable_builtin=False,
    )
    app.state.adapters.management_provider = mgmt
    yield

settings = ConfigSchema(
    db_path="./test.db",
)
app = create_app(settings=settings, management_provider=my_management_provider)
```

### 开箱即用

```bash
uvicorn mh_gateway.main:app --port 8005
```

配置从 `.env` 文件和环境变量读取。所有 ConfigSchema 字段都有默认值，因此不设置任何环境变量即可启动。

```python
from mh_gateway.app import create_app
# main.py 内部等价于:
#   config_mgr = ConfigManager()
#   settings = asyncio.run(config_mgr.resolve(ConfigSchema, prefix="ORCH"))
#   app = create_app(settings=settings)
```

---

## 编写启动文件

创建一个 Python 文件（例如 `my_app.py`），通过 LifespanHook 注入 adapter：

```python
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from mh_gateway import ConfigManager, ConfigSchema, create_app
from pydantic import BaseModel
from my_adapters import (
    MyUserAuthProvider,
    MyPermissionChecker,
    MyRegistry,
    MyRegistryConfig,
    ApolloConfigProvider,
    VaultSecretResolver,
)
from my_adapters import (
    MyOutboundAuthProvider,
    MyM2MAuthProvider,
)


# ── 1. 统一配置管理 ──────────────────────────────
config_mgr = ConfigManager(
    config_provider=ApolloConfigProvider(),
    secret_resolver=VaultSecretResolver(),
)


# ── 2. 定义 Adapter lifespan hooks ──────────────
@asynccontextmanager
async def my_token_verifier(app: FastAPI):
    app.state.adapters.token_verifier = MyUserAuthProvider()
    yield


@asynccontextmanager
async def my_permission_checker(app: FastAPI):
    app.state.adapters.permission_checker = MyPermissionChecker()
    yield


@asynccontextmanager
async def my_management_provider(app: FastAPI):
    cfg = await config_mgr.resolve(
        MyRegistryConfig,
        prefix="my.registry",
        sensitive_fields={"api_key"},
    )
    app.state.adapters.management_provider = MyRegistry(
        api_url=cfg.api_url,
        api_key=cfg.api_key,
    )
    yield


@asynccontextmanager
async def my_outbound_auth_provider(app: FastAPI):
    app.state.adapters.outbound_auth_provider = MyOutboundAuthProvider()
    yield


@asynccontextmanager
async def my_m2m_auth_provider(app: FastAPI):
    app.state.adapters.m2m_auth_provider = MyM2MAuthProvider()
    yield


# ── 3. 解析框架配置（仅此一处需要 asyncio.run） ──
settings = asyncio.run(config_mgr.resolve(ConfigSchema, prefix="ORCH"))


# ── 4. 配置 root logger（可选，不配置则使用 SDK 内置默认日志） ──
root = logging.getLogger()
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
))
root.addHandler(handler)
root.setLevel(logging.DEBUG)


# ── 5. 组装应用 ──────────────────────────────────
app = create_app(
    settings=settings,
    token_verifier=my_token_verifier,
    permission_checker=my_permission_checker,
    management_provider=my_management_provider,
    outbound_auth_provider=my_outbound_auth_provider,
    m2m_auth_provider=my_m2m_auth_provider,
)
```

当 Adapter 不需要额外配置时，直接在 hook 中实例化即可。Adapter 的配置解析延迟到应用启动时在 lifespan 中执行，无需在模块层为 adapter 调用 `asyncio.run()`。

---

## 启动服务

```bash
uvicorn my_app:app --host 0.0.0.0 --port 8005
```

生产环境建议：

```bash
uvicorn my_app:app \
  --host 0.0.0.0 \
  --port 8005 \
  --workers 4 \
  --log-level info \
  --no-access-log
```

---

## 配置管理

### 解析优先级

`ConfigManager.resolve()` 对每个字段独立按以下顺序查找：

1. **环境变量** `{PREFIX}_{FIELD}`（如 `ORCH_DB_PATH`）— 最高优先级
2. **外接配置** — 敏感字段走 `secret_resolver.get()`，非敏感字段走 `config_provider.get()`（两者均为 `ConfigProvider` 协议）
3. **字段默认值** — `ConfigSchema` 中定义的默认值

### 关键环境变量（prefix = `ORCH`）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ORCH_DB_PATH` | `./sessions.db` | SQLite 路径 |
| `ORCH_DB_AUTO_SCHEMA` | `false` | 启动时自动建表 |
| `ORCH_CORS_ORIGINS` | `[]` | 跨域来源（逗号分隔） |
| `ORCH_DEV_MODE` | `false` | 开发模式开关 |
| `ORCH_LOG_LEVEL` | `INFO` | 日志级别（ConfigSchema 声明字段，但日志通过 `MH_LOG_LEVEL` 环境变量或自行配置 root logger 控制） |
| `ORCH_ENABLE_EVAL` | `true` | 评测接口开关 |
| `ORCH_EVAL_RESULTS_DIR` | `./eval_results` | 评测结果目录 |
| `ORCH_VERIFY_AGENT_TOOL_SSL` | `false` | 远程 agent/tool SSL 验证 |
| `ORCH_METRICS_ENABLED` | `false` | 指标采集开关 |
| `ORCH_METRICS_PUSH_INTERVAL` | `60` | 指标推送间隔 |

### LLM Provider 环境变量

```bash
# 格式：ORCH_PROVIDER_{PROVIDER}__{KEY}
ORCH_PROVIDER_OPENAI__API_KEY=sk-xxx
ORCH_PROVIDER_OPENAI__BASE_URL=https://api.openai.com/v1
ORCH_PROVIDER_ANTHROPIC__API_KEY=sk-ant-xxx
```

内置 provider：`openai`、`anthropic`、`openai_viz`。Agent 元数据中 `provider` 和 `model` 字段控制 per-agent 的 provider 选择，默认使用 `openai`。

---

## 数据库

### 架构

内置 SQLite 实现。通过设置 `ORCH_DB_PATH` 指定文件路径，默认 `./sessions.db`。系统启动时自动创建（`db_auto_schema=true` 时自动建表）。

### 自定义数据库 Adapter

框架只定义 `SessionStoreProtocol` 作为内部流转结构，数据库实现由客户通过 factory 注入：

```python
from mh_gateway.services import database as db_svc

db_svc.set_session_store_factory(lambda: MySessionStore(my_db_conn))
```

工厂接受同步或异步 callable（`Callable[[], SessionStoreProtocol]` 或 `Callable[[], Awaitable[SessionStoreProtocol]]`）。

如需完全替换底层数据库（如 PostgreSQL），需同时替换 `DatabaseProtocol`：

```python
# 注入自定义数据库实现
db_svc.set_db(MyPostgresDatabase())

# 注入自定义 session store
db_svc.set_session_store_factory(lambda: MySessionStore(get_db()))
```

### `SessionStoreProtocol` 完整接口

```python
from minimal_harness.memory_store import SessionStoreProtocol
from minimal_harness.memory import Memory
from minimal_harness.session import Session, SessionSummary

class SessionStoreProtocol(Protocol):
    async def create_session(
        self,
        session_id: str | None = None,
        agent_name: str = "",
        user_id: str = "",
        scenario_id: str | None = None,
        transient: bool = False,
        display_name_locale: str | None = None,
    ) -> Session: ...

    async def get_session(self, session_id: str) -> Session | None: ...

    async def save_memory(
        self, memory: Memory, session_id: str,
        extra: dict[str, Any] | None = None
    ) -> None: ...

    async def delete_session(self, session_id: str) -> bool: ...

    async def list_sessions(self) -> list[SessionSummary]: ...

    async def list_user_sessions(
        self, user_id: str, scenario_id: str | None = None
    ) -> list[SessionSummary]: ...

    async def get_session_messages(self, session_id: str) -> list[dict]: ...

    def get_messages_as_items(
        self, session: Session
    ) -> list[dict]: ...
```

### Session / Message 内部类型

| 类型 | 来源 | 说明 |
|------|------|------|
| `Session` | `minimal_harness.session` | 具体 Session 实现，含 `db_id`、`memory`、`add_message()` 等 |
| `SessionSummary` | `minimal_harness.session` | 轻量摘要（TypedDict）：`session_id`、`agent_name`、`title`、`created_at`、`message_count`、`status` 等 |
| `Memory` | `minimal_harness.memory` | 消息内存协议，有 `get_new_messages()`、`mark_all_persisted()` 等持久化追踪方法 |
| `Message` | `minimal_harness.memory` | 消息联合类型 `SystemMessage \| UserMessage \| AssistantMessage \| ToolMessage \| ReasoningMessage` |

内置表的审计字段：

| 审计字段 | 类型 | 说明 |
|---------|------|------|
| `created_by` | TEXT | 创建人 |
| `last_updated_by` | TEXT | 最后更新人 |
| `creation_date` | TIMESTAMP(3) WITH TIME ZONE | 创建时间 |
| `last_update_date` | TIMESTAMP(3) WITH TIME ZONE | 最后更新时间 |
| `delete_flag` | CHAR(1) | 软删除标记（N/Y） |
| `last_update_trace_id` | TEXT | 分布式链路追踪 ID |

---

## API 清单

### 用户面 API

| 端点 | 方法 | 说明 |
|------|------|------|
| `GET /api/v1/auth/me` | GET | 当前用户信息 |
| `GET /api/v1/scenarios` | GET | 获取场景列表（按权限过滤） |
| `GET /api/v1/scenarios/{id}` | GET | 场景详情 |
| `POST /api/v1/chat/{memory_id}` | POST | SSE 流式聊天（支持 `session_id` 续传）<br/>*支持用户 Token 或 M2M 鉴权* |
| `GET /api/v1/sessions` | GET | 用户 Session 列表<br/>*支持用户 Token 或 M2M 鉴权* |
| `POST /api/v1/sessions` | POST | 创建 Session<br/>*支持用户 Token 或 M2M 鉴权* |
| `GET /api/v1/sessions/{id}` | GET | Session 详情<br/>*支持用户 Token 或 M2M 鉴权* |
| `GET /api/v1/sessions/{id}/messages` | GET | Session 消息历史<br/>*支持用户 Token 或 M2M 鉴权* |
| `DELETE /api/v1/sessions/{id}` | DELETE | 删除 Session<br/>*支持用户 Token 或 M2M 鉴权* |
| `GET /api/v1/agents` | GET | Agent 列表（按权限过滤，支持 `?scenario=` 过滤） |
| `GET /api/v1/tools` | GET | Tool 列表（按权限过滤） |
| `/docs` / `/redoc` | GET | Swagger / ReDoc 在线文档 |

### 管理面 API（需 `MetadataManager` + `manage:*:*` 权限）

| 端点 | 方法 | 说明 |
|------|------|------|
| `GET/POST /api/v1/management/scenarios` | GET/POST | 场景列表/创建 |
| `GET/PUT/DELETE /api/v1/management/scenarios/{id}` | GET/PUT/DELETE | 场景详情/更新/删除 |
| `POST/DELETE /api/v1/management/scenarios/{id}/agents` | POST/DELETE | 场景-Agent 关系管理 |
| `POST/DELETE /api/v1/management/scenarios/{id}/agents/{name}/tools` | POST/DELETE | Agent-Tool 关系管理 |
| `GET/POST /api/v1/management/agents` | GET/POST | Agent 列表/创建 |
| `GET/PUT/DELETE /api/v1/management/agents/{name}` | GET/PUT/DELETE | Agent 详情/更新/删除 |
| `GET/POST /api/v1/management/tools` | GET/POST | Tool 列表/创建 |
| `GET/PUT/DELETE /api/v1/management/tools/{name}` | GET/PUT/DELETE | Tool 详情/更新/删除 |
| `GET /api/v1/management/providers` | GET | LLM Provider 列表 |

### M2M 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `POST /api/v1/agents/{name}/run` | POST | 运行 Agent（M2M 鉴权） |

### AI 生成端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `POST /api/v1/tool-generator/generate` | POST | SSE 流式工具生成 |
| `GET/POST/PUT/DELETE /api/v1/tool-generator/tools[/{name}]` | 多种 | 用户已生成工具 CRUD |
| `POST /api/v1/tool-generator/tools/{name}/trial` | POST | 工具试运行（SSE） |
| `POST /api/v1/tools/generated/{name}/execute` | POST | M2M 鉴权的生成工具执行 |
| `POST /api/v1/agent-generator/generate` | POST | SSE 流式 Agent 生成 |
| `GET/POST/PUT/DELETE /api/v1/agent-generator/agents[/{name}]` | 多种 | 用户已生成 Agent CRUD |
| `POST /api/v1/agent-generator/agents/{name}/trial` | POST | Agent 试运行（SSE 流式聊天） |

---

## 最佳实践

1. **先实现 `MetadataManager`** — 它是核心数据来源，统一读写协议，管理面 API 和运行时 API 共享同一数据源
2. **使用 `key_mapping` 集中管理远程 key** — 通过 `ConfigManager.resolve(key_mapping=...)` 避免在代码中硬编码配置中心的 key
3. **生产环境务必注入所有 Adapter** — 不要依赖内置默认实现
4. **M2MAuthProvider 必须替换** — 默认实现允许所有请求，生产环境必须实现基于 SOA 的鉴权
5. **日志** — 如需自定义日志输出，在调用 `create_app()` 前自行配置 `logging.getLogger()`（root logger）。SDK 提供内置默认日志配置。
6. **审计** — 审计日志通过 `orchestration.audit` logger 输出（INFO 级别），可在日志系统中单独采集
7. **所有 Config 类必须继承 `pydantic.BaseModel`** — `ConfigManager` 在运行时依赖 `model_fields` 和 Pydantic 构造器
8. **LLM 配置通过 ORCH_PROVIDER** — 使用环境变量 `ORCH_PROVIDER_{NAME}__{KEY}` 而不是旧的 `ORCH_LLM_*` 变量
