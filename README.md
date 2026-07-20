# mh-gateway — 编排网关

核心网关服务，依赖 [minimal-harness](https://github.com/J0ey1iu/minimal-harness) SDK。负责场景加载、用户权限校验、事件流归集，协调前端与各 worker 服务的通信。

- 版本：**0.2.0a1**

- 端口：`8005`
- Swagger：`http://localhost:8005/docs`

> **开发者指南**：[docs/dev-guide.md](./docs/dev-guide.md)（中文） · [docs/dev-guide.agent.md](./docs/dev-guide.agent.md)（英文，面向 Coding Agent）
>
> **企业适配指导**：[docs/customer-adaptation-guide.md](./docs/customer-adaptation-guide.md)（中文） · [docs/customer-adaptation-guide.agent.md](./docs/customer-adaptation-guide.agent.md)（英文，面向 Coding Agent）
>
> **构建分发**：[docs/build-guide.md](./docs/build-guide.md)

## 在 mh 生态中的位置

| 包 | 角色 | 仓库 |
|---|---|---|
| [minimal-harness](https://github.com/J0ey1iu/minimal-harness) | 核心 SDK（类型、协议、Agent 运行时、LLM 抽象、Memory/Session）。本服务依赖它。 | [J0ey1iu/minimal-harness](https://github.com/J0ey1iu/minimal-harness) |
| [mh-service-kit](https://github.com/J0ey1iu/mh-service-kit) | FastAPI 服务工具包。本服务使用 `ServiceApp` 来托管 in-cluster Agent（如 dev-mode 下的 `triage`），并通过它提供的 SSE 客户端调用远程 Agent。 | [J0ey1iu/mh-service-kit](https://github.com/J0ey1iu/mh-service-kit) |
| [mh-tui](https://github.com/J0ey1iu/mh-tui) | 本地 Textual TUI。本服务是它的云端多租户对等形态；二者共享 `minimal-harness` 的 Agent / Tool / Memory 抽象。 | [J0ey1iu/mh-tui](https://github.com/J0ey1iu/mh-tui) |
| [agent-tool-service](https://github.com/J0ey1iu/mh-incubator/tree/main/packages/agent-tool-service) | 内置于 umbrella 仓的示例 Agent & Tool 服务，可被本服务通过 M2M 端点调用。 | [J0ey1iu/mh-incubator](https://github.com/J0ey1iu/mh-incubator) |
| [mh-incubator](https://github.com/J0ey1iu/mh-incubator) | umbrella 工作区，串联本服务、agent-tool-service、web-frontend、`minimal-harness` 一起做端到端演示。 | [J0ey1iu/mh-incubator](https://github.com/J0ey1iu/mh-incubator) |

## 适配层架构

orchestration 通过 LifespanHook 接口与外部系统解耦。所有适配器通过 `create_app()` 参数注入：

| 接口 | 默认实现 | 企业部署替换 |
|------|----------|----------|
| `UserAuthProvider` | `_DefaultAuthProvider` — 提取 `X-User-Id` header/cookie | 实现 `verify(request) → UserIdentity` |
| `PermissionChecker` | `_DefaultAuthProvider` — 内置权限表 | 实现 `check/get_permissions` |
| `MetadataManager` | `InMemoryManagementProvider` — 内存数据（受 `dev_mode` 控制） | 实现读 (`get_agent/list_agents/...`) + CRUD (`create_agent/update_agent/...`) |
| `OutboundAuthProvider` | `_DefaultOutboundAuthProvider` — 透传请求 header | 实现 `get_headers(request, url, type) → dict` |
| `M2MAuthProvider` | `_DefaultM2MAuthProvider` — 允许所有请求 | 实现 `authenticate(request) → str\|None`（Chat/Sessions 端点也支持 M2M 鉴权回退） |
| `ConfigProvider` | 无（仅环境变量） | 实现 `get(key) → str` 对接 Apollo/Nacos/Vault 等 |
| `LLMProvider` | 通过 `LLMProviderRegistry` + 环境变量 `ORCH_PROVIDER_*` 配置 | 注入自定义 `llm_provider_factory` 或 `llm_provider_registry` LifespanHook |

`UserAuthProvider`、`PermissionChecker`、`MetadataManager` Protocol 定义在 `mh_gateway` 内。`OutboundAuthProvider`、`M2MAuthProvider`、`ConfigProvider` Protocol 也定义在 `mh_gateway` 内。

## `create_app()` 工厂函数（客户部署入口）

`create_app()` 是 mh-gateway 的唯一入口，所有适配器通过 LifespanHook 参数注入：

```python
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from mh_gateway import (
    ConfigManager, ConfigSchema, create_app,
)
from my_adapters import (
    CorpUserAuthProvider, CorpPermissionChecker, CorpRegistry,
)

# 1. 解析配置（env → 可选配置中心 → 报错）
config_mgr = ConfigManager()
settings = asyncio.run(config_mgr.resolve(ConfigSchema, prefix="ORCH"))

# 2. 配置 root logger（可选，不配置则使用 SDK 内置默认日志）
root = logging.getLogger()
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
))
root.addHandler(handler)
root.setLevel(logging.DEBUG)

# 3. 定义 Adapter LifespanHook（应用启动时注入）
@asynccontextmanager
async def token_verifier(app: FastAPI):
    app.state.adapters.token_verifier = CorpUserAuthProvider()
    yield

@asynccontextmanager
async def permission_checker(app: FastAPI):
    app.state.adapters.permission_checker = CorpPermissionChecker()
    yield

@asynccontextmanager
async def management_provider(app: FastAPI):
    app.state.adapters.management_provider = CorpRegistry()
    yield

# 4. 注入你的企业适配器
app = create_app(
    settings=settings,
    token_verifier=token_verifier,
    permission_checker=permission_checker,
    management_provider=management_provider,
)
```

省略的适配器参数会使用内置默认实现，适合开发和演示。

部署后以 `uvicorn my_app:app` 启动。

## AppState — 运行时可访问适配器

所有注入的适配器实例通过 `request.app.state.adapters` 访问：

```python
from mh_gateway import AppState

adapters: AppState = request.app.state.adapters
identity = await adapters.token_verifier.verify(request)
perms = await adapters.permission_checker.get_permissions(user_id)
agents = await adapters.management_provider.list_agents()
```

## API

### 用户面 API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/v1/auth/me` | GET | 当前用户信息（含权限列表） |
| `/api/v1/scenarios` | GET | 场景列表（按权限过滤） |
| `/api/v1/scenarios/{id}` | GET | 场景详情（含 Agent/Tool） |
| `/api/v1/chat/{memory_id}` | POST | SSE 流式聊天（支持 `session_id` 续传）<br/>*支持用户 Token 或 M2M 鉴权* |
| `/api/v1/sessions` | GET | 当前用户的 Session 列表（支持 `?scenario_id=` 过滤）<br/>*支持用户 Token 或 M2M 鉴权* |
| `/api/v1/sessions` | POST | 创建 Session<br/>*支持用户 Token 或 M2M 鉴权* |
| `/api/v1/sessions/{id}` | GET | Session 详情（含消息数）<br/>*支持用户 Token 或 M2M 鉴权* |
| `/api/v1/sessions/{id}/messages` | GET | Session 消息历史<br/>*支持用户 Token 或 M2M 鉴权* |
| `/api/v1/sessions/{id}` | DELETE | 删除 Session<br/>*支持用户 Token 或 M2M 鉴权* |
| `/api/v1/agents` | GET | Agent 列表（按权限过滤，支持 `?scenario=` 过滤） |
| `/api/v1/tools` | GET | Tool 列表（按权限过滤） |
| `/api/v1/auth/logout` | POST | 用户登出（清除认证态） |
| `/health` | GET | 健康检查（始终返回 `{"status":"ok"}`） |
| `/ready` | GET | 就绪检查（检查数据库连接） |
| `/api/v1/metrics` | GET | 运行时指标快照（仅 `metrics_enabled=true` 时可用） |

### 管理面 API（需 `MetadataManager` + 对应资源管理权限：`manage:scene:*` / `manage:agent:*` / `manage:tool:*`）

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/v1/management/scenarios` | GET/POST | 场景列表/创建 |
| `/api/v1/management/scenarios/{id}` | GET/PUT/DELETE | 场景详情/更新/删除 |
| `/api/v1/management/scenarios/{id}/agents` | POST/DELETE | 场景-Agent 关系管理 |
| `/api/v1/management/scenarios/{id}/agents/{name}/tools` | POST/DELETE | Agent-Tool 关系管理 |
| `/api/v1/management/agents` | GET/POST | Agent 列表/创建 |
| `/api/v1/management/agents/{name}` | GET/PUT/DELETE | Agent 详情/更新/删除 |
| `/api/v1/management/tools` | GET/POST | Tool 列表/创建 |
| `/api/v1/management/tools/{name}` | GET/PUT/DELETE | Tool 详情/更新/删除 |
| `/api/v1/management/providers` | GET | LLM Provider 列表 |

### M2M 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/v1/agents/{name}/run` | POST | 运行 Agent（M2M 鉴权） |

### AI 生成端点

| 端点 | 方法 | 说明 |
|------|------|------|


### 开发模式端点（仅 `dev_mode=true`）

| 端点 | 方法 | 说明 |
|------|------|------|

## AuditMiddleware

每个 Agent 执行周期自动记录审计日志（包括 `agent_start/end`、`llm_start/end`、`tool_start/end/error`、token 用量）。
日志级别为 `INFO`，可通过 `orchestration.audit` logger 配置。

## AccessLogMiddleware

每个 HTTP 请求自动输出一条结构化 JSON 访问日志，包含 `method`、`path`、`status`、`duration_ms`、`trace_id`、`user_id` 等字段。
日志级别为 `INFO`，可通过 `orchestration.access` logger 配置。

## 监控指标

当 `metrics_enabled=true` 时，服务会自动注册 `MetricsCollector`，在内存中采集如下指标并通过后台定时任务推送到日志：

| 指标 | 类型 | 标签 |
|------|------|------|
| `http_requests_total` | Counter | method, path, status |
| `http_request_duration_ms` | Histogram | method, path |
| `llm_requests_total` | Counter | provider, model, status |
| `llm_tokens_total` | Counter | provider, model, type (prompt/completion) |
| `llm_request_duration_ms` | Histogram | provider, model |
| `agent_runs_total` | Counter | agent_id, status |
| `tool_calls_total` | Counter | tool_name, status |
| `sessions_active` | Gauge | — |

指标通过 AuditMiddleware 的生命周期钩子自动采集。可通过 `/api/v1/metrics` 获取实时快照。

## PermissionMiddleware

每个 Agent 运行时自动校验工具调用权限。可通过 `check(user_id, perm)` 返回 `bool` 实现自定义逻辑。

## 环境变量

所有环境变量以 `ORCH_` 为前缀：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ORCH_DB_PATH` | `./sessions.db` | SQLite 数据库文件路径 |
| `ORCH_DB_AUTO_SCHEMA` | `false` | 启动时自动建表（生产环境建议设为 `false`） |
| `ORCH_CORS_ORIGINS` | `[]` | 跨域源（逗号分隔，如 `http://localhost:5173,http://localhost:3000`） |
| `ORCH_DEV_MODE` | `false` | 开发模式开关，开启后暴露内置 agent、前端 SPA、开发调试工具及 SSO 登录页 |
| `ORCH_LOG_LEVEL` | `INFO` | 日志级别（ConfigSchema 字段，但日志通过 `MH_LOG_LEVEL` 环境变量或自行配置 root logger 控制） |
| `ORCH_ENABLE_EVAL` | `true` | 是否暴露评测接口 |
| `ORCH_EVAL_RESULTS_DIR` | `./eval_results` | 评测结果存储目录 |
| `ORCH_VERIFY_AGENT_TOOL_SSL` | `false` | 调用远程 agent/tool 时是否验证 SSL 证书 |
| `ORCH_METRICS_ENABLED` | `false` | 启用指标采集（计数器/直方图/仪表盘）及 `/api/v1/metrics` 端点 |
| `ORCH_METRICS_PUSH_INTERVAL` | `60` | 指标推送间隔（秒），仅 `ORCH_METRICS_ENABLED=true` 时生效 |

### LLM Provider 配置

LLM 配置通过 `ORCH_PROVIDER_{NAME}__{KEY}` 环境变量设置，不再使用旧的 `ORCH_LLM_*` 变量：

```bash
# 配置 OpenAI
export ORCH_PROVIDER_OPENAI__API_KEY=sk-xxx
export ORCH_PROVIDER_OPENAI__BASE_URL=https://api.openai.com/v1

# 配置 Anthropic
export ORCH_PROVIDER_ANTHROPIC__API_KEY=sk-ant-xxx
```

内置 provider：`openai`、`anthropic`、`openai_viz`（openai 的克隆）。

Agent 元数据的 `provider` 和 `model` 字段控制 per-agent 的 provider 选择。默认使用 `openai`。

## 外部配置对接

当客户有自己的配置中心（Apollo/Nacos/Consul）和密钥系统（HashiCorp Vault/AWS Secrets Manager）时，可通过 `ConfigProvider` 协议对接。

### 解析优先级

配置值按以下优先级解析（高 → 低）：

1. **环境变量**（`ORCH_*`）— 最高优先级，运维可临时覆盖
2. **外接配置**（`ConfigProvider` 实例，敏感与非敏感通过不同实例区分）— 来自配置中心
3. **代码默认值** — 若以上均未设置，使用 `ConfigSchema` 中的默认值

### 远程 key 重映射

`ConfigManager.resolve()` 支持 `key_mapping` 参数，将内部字段名重映射为客户配置中心的 key：

```python
cfg = await config_mgr.resolve(
    MyConfig,
    prefix="my.registry",
    key_mapping={
        "db_path": "woa.orchestration.db.path",
    },
    sensitive_fields={"api_key"},
)
```

### 实现自定义 UserAuthProvider（对接企业 SSO）

`verify()` 收到的是完整的 FastAPI `Request`，可读 Cookie/Header/调外部 API：

```python
from typing import Any
from mh_gateway.auth import UserAuthProvider, UserIdentity

class CorpSSOVerifier(UserAuthProvider):
    async def verify(self, request: Any) -> UserIdentity | None:
        # 1. 从 Cookie 中提取会话标识
        session_id = request.cookies.get("sessionid")
        if not session_id:
            return None
        # 2. 调用企业认证 API（request 还可读其他 header/query）
        user_info = await self._call_auth_api(session_id)
        if not user_info:
            return None
        # 3. 返回标准身份（extra_data 保留完整信息）
        return UserIdentity(
            user_id=user_info["employee_id"],
            username=user_info["name"],
            roles=user_info.get("roles", []),
            extra_data=user_info,
        )
```

> HTTP Bearer token 由内置 `_DefaultAuthProvider` 从 `request.headers["authorization"]` 提取，客户使用 Cookie 时直接在 `verify()` 中读取 `request.cookies` 即可。

### 实现其他 Provider

```python
from mh_gateway import ConfigProvider

class ApolloConfigProvider(ConfigProvider):
    async def get(self, key: str) -> str | None:
        return await apollo_client.get_value(key)

class VaultSecretResolver(ConfigProvider):
    async def get(self, key: str) -> str | None:
        return await vault_client.read_secret(key)
```

## 内置 Agent 样例 & 开发模式

设置 `ORCH_DEV_MODE=true` 后，服务会暴露 3 个内置样例 agent 以及开发调试用工具端点：

| Agent | 英文名 | 中文名 | 说明 |
|-------|--------|--------|------|
| `triage` | General Assistant | 通用助手 | 理解用户需求并路由到专业 agent（code-reviewer / writer）。本地执行。 |
| `code-reviewer` | Code Reviewer | 代码审查 | 分析代码变更中的缺陷、风格、安全和性能问题。通过 M2M 端点执行。 |
| `writer` | Writing Assistant | 写作助手 | 辅助撰写文章、邮件、报告等。通过 M2M 端点执行。 |

内置 Tool 包括 `calculator`、`handoff`、`discover_agents`、`show_ui_meta`、`general_visualization`、`stop_agent`。

`triage` agent 在进程内本地执行（无 `endpoint_url`），`code-reviewer` 和 `writer` 通过 M2M 端点执行。内置 agent 的 system_prompt 支持中英文，根据前端传来的 `Accept-Language` 自动适配。

> **生产环境**请确保 `ORCH_DEV_MODE` 为 `false`（默认值），并通过 `management_provider` LifespanHook 注入企业自己的注册中心实现。

## 内置前端 UI（一站式部署）

设置 `ORCH_DEV_MODE=true` 后，FastAPI 会在 `/` 直接 serve 编译后的 SPA（单页应用），前提是 `static/` 目录存在。

```bash
# 构建前端（SPA + 组件 bundle → 复制到 static/）
bash scripts/build-frontend.sh

# 启动（前端在 http://localhost:8005）
ORCH_DEV_MODE=true uv run uvicorn mh_gateway.main:app --port 8005
```

> **注意**：前端静态文件需预先构建并放入 `static/` 目录。`ORCH_DEV_MODE=true` 时服务会自动挂载前端并处理 SPA fallback 路由。

## 本地开发

```bash
# 带前端（先构建前端 SPA + 复制到 static/）
bash scripts/dev-standalone.sh

# 或仅后端（前端由 Vite 开发服务器提供热更新）
uv run uvicorn mh_gateway.main:app --port 8005
cd web-frontend && npm run dev
```

或使用项目根目录的 `bash scripts/dev.sh` 一键启动所有服务。

## 构建分发

```bash
cd packages/mh-gateway
uv build
# 产出 dist/mh_gateway-*.whl
```

客户 pip install 后，编写自己的启动文件注入适配器即可。

## 测试

```bash
uv run pytest packages/mh-gateway/tests -v
```
