# mh-gateway Developer Guide (for Coding Agent)

This document specifies the exact implementation contract for building a custom gateway service using the `mh-gateway` framework. Follow these instructions precisely.

---

## 1. Entry Point

The entire service is assembled by calling `create_app()` (import: `from mh_gateway import create_app`). Your code lives in a single Python file (e.g. `my_app.py`) that uvicorn loads.

```python
import asyncio
from mh_gateway import ConfigManager, ConfigSchema, create_app

config_mgr = ConfigManager()
settings = asyncio.run(config_mgr.resolve(ConfigSchema, prefix="ORCH"))
app = create_app(settings=settings)
```

---

## 2. Adapter Protocol Contracts

### 2.1 UserAuthProvider (`from mh_gateway.auth import UserAuthProvider`)

```python
@runtime_checkable
class UserAuthProvider(Protocol):
    async def verify(self, request: Any) -> UserIdentity | None: ...
```

- Input: raw FastAPI `Request` object
- Output: `UserIdentity` (dataclass with `user_id`, `username`, `roles`, `extra_data`) or `None`
- Read from `request.headers`, `request.cookies`, or call external auth API
- `user_id` is REQUIRED — used for permission checks and session ownership

### 2.2 PermissionChecker (`from mh_gateway.auth import PermissionChecker`)

```python
@runtime_checkable
class PermissionChecker(Protocol):
    async def get_permissions(self, user_id: str) -> list[str]: ...
    async def check(self, user_id: str, permission: str) -> bool: ...
```

- Permission format: `action:resource:target` (e.g. `"use:agent:code-reviewer"`)
- Wildcard `*` supported at any segment
- Use `match_permission()` from `mh_gateway.auth` for wildcard evaluation

### 2.3 RegistryProvider (`from mh_gateway.adapters import RegistryProvider`)

```python
@runtime_checkable
class RegistryProvider(Protocol):
    async def get_agent(self, name: str) -> dict[str, Any] | None: ...
    async def list_agents(self) -> list[dict[str, Any]]: ...
    async def get_tool(self, name: str) -> dict[str, Any] | None: ...
    async def list_tools(self) -> list[dict[str, Any]]: ...
    async def get_scenario(self, scenario_id: str) -> dict[str, Any] | None: ...
    async def list_scenarios(self) -> list[dict]: ...

    # Optional optimization:
    # async def get_tools(self, names: list[str]) -> dict[str, dict[str, Any] | None]
    # Returns batch tool metadata. Runtime auto-detects and uses this to avoid N+1 queries.
```

**Agent dict shape:**
```json
{
  "name": "agent-id",
  "display_name": "Human Name",
  "display_name_locale": "{\"zh\":\"中文名\",\"en\":\"English\"}",
  "description": "What this agent does",
  "description_locale": "{\"zh\":\"...\",\"en\":\"...\"}",
  "system_prompt": "You are an agent that...",
  "system_prompt_locale": "{\"zh\":\"你是一个...\",\"en\":\"You are...\"}",
  "endpoint_url": "http://other-service/agent/run"  // omit or empty for local execution
}
```

**Tool dict shape:**
```json
{
  "name": "tool-id",
  "display_name": "Human Name",
  "display_name_locale": "{\"zh\":\"...\",\"en\":\"...\"}",
  "description": "What this tool does",
  "description_locale": "{\"zh\":\"...\",\"en\":\"...\"}",
  "parameters": {"type": "object", "properties": {...}, "required": [...]},
  "endpoint_url": "http://other-service/tool/execute",  // omit for local + _fn
  "_fn": async_callable  // only for local execution
}
```

**Scenario dict shape:**
```json
{
  "id": "scenario-id",
  "name": "Scenario Name",
  "name_locale": "{\"zh\":\"...\",\"en\":\"...\"}",
  "icon": "💻",
  "description": "What this scenario does",
  "description_locale": "{\"zh\":\"...\",\"en\":\"...\"}",
  "agents": [{"name": "agent-id", "tool_names": ["tool-a", "tool-b"]}]
}
```

`display_name_locale`, `description_locale`, `system_prompt_locale` are JSON-encoded strings (not dicts) for backward compatibility.

### 2.4 OutboundAuthProvider (`from mh_gateway import OutboundAuthProvider`)

```python
@runtime_checkable
class OutboundAuthProvider(Protocol):
    async def get_headers(self, request: Any, target_url: str, target_type: str) -> dict[str, str]: ...
```

- Called when orchestration makes outbound HTTP calls to remote agent/tool endpoints
- Default: forwards all request headers except hop-by-hop headers
- Implement to inject service-mesh auth (HMAC, mTLS, etc.)

### 2.5 M2MAuthProvider (`from mh_gateway import M2MAuthProvider`)

```python
@runtime_checkable
class M2MAuthProvider(Protocol):
    async def authenticate(self, request: Any) -> str | None: ...
    async def get_identity_headers(self, request: Any, identity: str) -> dict[str, str]: ...
    async def close(self) -> None: ...
```

- Protects `POST /api/v1/agents/{name}/run` and `POST /api/v1/tools/*/execute`
- Returns `app_id` (str) on success, `None` for 401
- Default: allows all requests, returns `"default"` (development only — MUST replace in production)

### 2.6 ConfigProvider (`from mh_gateway import ConfigProvider`)

```python
@runtime_checkable
class ConfigProvider(Protocol):
    async def get(self, key: str) -> str | None: ...
```

- Covers both normal config (Apollo/Nacos) and secrets (Vault/AWS Secrets Manager)
- `SecretResolver` is a backward-compatible alias; `from mh_gateway import SecretResolver` still works
- `ConfigManager` accepts two `ConfigProvider` instances — `config_provider` and `secret_resolver` — differentiated by the `sensitive_fields` parameter

### 2.7 MetadataManager (`from mh_gateway.adapters import MetadataManager`)

```python
@runtime_checkable
class MetadataManager(RegistryProvider, Protocol):
    # Inherits all RegistryProvider read methods:
    #   get_agent, list_agents, get_tool, list_tools,
    #   get_scenario, list_scenarios

    # Tool CRUD
    async def create_tool(self, tool: dict[str, Any]) -> dict[str, Any]: ...
    async def update_tool(self, name: str, tool: dict[str, Any]) -> dict[str, Any]: ...
    async def delete_tool(self, name: str) -> None: ...

    # Agent CRUD
    async def create_agent(self, agent: dict[str, Any]) -> dict[str, Any]: ...
    async def update_agent(self, name: str, agent: dict[str, Any]) -> dict[str, Any]: ...
    async def delete_agent(self, name: str) -> None: ...

    # Scenario CRUD
    async def create_scenario(self, scenario: dict[str, Any]) -> dict[str, Any]: ...
    async def update_scenario(self, scenario_id: str, scenario: dict[str, Any]) -> dict[str, Any]: ...
    async def delete_scenario(self, scenario_id: str) -> None: ...

    # Scenario-Agent-Tool relationships
    async def add_scenario_agent(self, scenario_id, agent_name, tool_names=None) -> dict: ...
    async def remove_scenario_agent(self, scenario_id, agent_name) -> dict: ...
    async def add_agent_tool(self, scenario_id, agent_name, tool_name) -> dict: ...
    async def remove_agent_tool(self, scenario_id, agent_name, tool_name) -> dict: ...

    async def close(self) -> None: ...
```

- Extends `RegistryProvider` with write operations
- Injected via `management_provider` LifespanHook (not `registry_provider`)
- Powers the management CRUD API endpoints at `GET/POST/PUT/DELETE /api/v1/management/*`

### 2.8 ToolGenerator (`from mh_gateway.services.generated_tool_provider import ToolGenerator`)

```python
@runtime_checkable
class ToolGenerator(Protocol):
    def generate_stream(self, natural_description: str, stop_event: asyncio.Event | None = None) -> AsyncGenerator[dict[str, Any], None]: ...
```

- Yields SSE events: `{"type": "generating", "data": {...}}` → `{"type": "generated", "data": {tool_dict}}`
- `stop_event`: when set, the implementation should cancel the LLM call as soon as possible
- Default: `DefaultToolGenerator` (LLM-based, reuses `llm_provider_factory`)
- Injected via `generated_tool_provider` LifespanHook

### 2.9 AgentGenerator (`from mh_gateway.services.generated_agent_provider import AgentGenerator`)

```python
@runtime_checkable
class AgentGenerator(Protocol):
    def generate_stream(self, natural_description: str, stop_event: asyncio.Event | None = None) -> AsyncGenerator[dict[str, Any], None]: ...
```

- Symmetrical to `ToolGenerator`; same event protocol and `stop_event` parameter
- Default: `DefaultAgentGenerator` (LLM-based)
- Injected via `generated_agent_provider` LifespanHook

### 2.10 ExtraHeadersProvider (`from minimal_harness.types import ExtraHeadersProvider`)

```python
ExtraHeadersProvider = Callable[[], Awaitable[dict[str, str]]]
```

- **NOT a LifespanHook** — passed directly to `create_app()` as `llm_extra_headers_provider`
- Called before each LLM API request to inject dynamic HTTP headers (e.g. `x-reasoning-format`)
- Stored in `app.state.adapters.llm_extra_headers_provider`

### 2.11 LLMProviderRegistry (`from minimal_harness.llm.llm import LLMProviderRegistry`)

```python
class LLMProviderRegistry:
    def register(self, name: str, factory: Callable[[dict[str, Any]], LLMProvider]) -> None: ...
    def set_default_config(self, name: str, cfg: dict[str, Any]) -> None: ...
    def create(self, provider: str, cfg: dict[str, Any]) -> LLMProvider: ...
    def list_providers(self) -> list[str]: ...
```

- Injected via `llm_provider_registry` LifespanHook
- Enables per-agent provider/model selection via Agent's `provider` + `model` fields
- Built-in providers: `openai`, `anthropic`, `openai_viz`
- Default configs from env vars: `ORCH_PROVIDER_{NAME}__{KEY}` (e.g. `ORCH_PROVIDER_OPENAI__API_KEY`)

---

## 3. Adapter Injection via LifespanHook

Every adapter is injected as a `LifespanHook` (`Callable[[FastAPI], AbstractAsyncContextManager[None]]`, exported as `LifespanHook` from `mh_gateway`):

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from mh_gateway.adapters import MetadataManager

@asynccontextmanager
async def my_hook(app: FastAPI):
    # on startup
    mgmt = MyMetadataManager()
    app.state.adapters.registry_provider = mgmt
    app.state.adapters.management_provider = mgmt
    yield
    # on shutdown — close resources
    await mgmt.close()
```

Pass named hooks to `create_app()`:

```python
app = create_app(
    settings=settings,

    # Named adapter hooks (run in this order)
    token_verifier=my_token_verifier_hook,
    permission_checker=my_permission_checker_hook,
    management_provider=my_management_provider_hook,  # replaces legacy registry_provider
    llm_provider_factory=my_llm_factory_hook,
    llm_provider_registry=my_provider_registry_hook,
    outbound_auth_provider=my_outbound_auth_hook,
    m2m_auth_provider=my_m2m_auth_hook,
    generated_tool_provider=my_tool_generator_hook,
    generated_agent_provider=my_agent_generator_hook,

    # Direct callable (NOT a LifespanHook)
    llm_extra_headers_provider=my_extra_headers_fn,

    # Generic hooks run last
    lifespan_hooks=[my_cross_cutting_hook],

    logger=my_logger,
)
```

**Execution order within lifespan:**
1. `AppState` created with `settings`
2. Built-in defaults filled for any `None` adapter
3. Named hooks run (in parameter order)
4. `lifespan_hooks` run
5. Database initialized
6. Server serves requests
7. On shutdown: hooks unwind in reverse order, then built-in adapters close

---

## 4. Configuration System

### 4.1 ConfigManager (`from mh_gateway import ConfigManager`)

```python
class ConfigManager:
    def __init__(self, config_provider: ConfigProvider | None = None, secret_resolver: ConfigProvider | None = None): ...
    async def resolve(self, schema_cls: type[T], *, prefix: str = "ORCH",
                      sensitive_fields: set[str] | None = None,
                      key_mapping: dict[str, str] | None = None) -> T: ...
```

**Resolution order per field:**
1. Env var `{PREFIX}_{FIELD}` (e.g. `ORCH_DB_PATH`) — highest
2. `secret_resolver.get()` (a `ConfigProvider` instance, if field in `sensitive_fields`)
3. `config_provider.get()` (a `ConfigProvider` instance, if field NOT in `sensitive_fields`)
4. Model default value (optional fields only)
5. `ConfigError` raised (required fields with no default)

Both `config_provider` and `secret_resolver` use the same `ConfigProvider` protocol; `SecretResolver` is a backward-compatible alias.

### 4.2 ConfigSchema (`from mh_gateway import ConfigSchema`)

All fields have defaults:

| Field | Type | Default | Env var |
|-------|------|---------|---------|
| `db_path` | str | `"./sessions.db"` | `ORCH_DB_PATH` |
| `db_auto_schema` | bool | `false` | `ORCH_DB_AUTO_SCHEMA` |
| `cors_origins` | list[str] | `[]` | `ORCH_CORS_ORIGINS` |
| `dev_mode` | bool | `false` | `ORCH_DEV_MODE` |
| `enable_eval` | bool | `true` | `ORCH_ENABLE_EVAL` |
| `eval_results_dir` | str | `"./eval_results"` | `ORCH_EVAL_RESULTS_DIR` |
| `log_level` | str | `"INFO"` | `ORCH_LOG_LEVEL` (declared; logging via `MH_LOG_LEVEL` or manual setup) |
| `verify_agent_tool_ssl` | bool | `false` | `ORCH_VERIFY_AGENT_TOOL_SSL` |
| `metrics_enabled` | bool | `false` | `ORCH_METRICS_ENABLED` |
| `metrics_push_interval` | int | `60` | `ORCH_METRICS_PUSH_INTERVAL` |

> **Note:** `llm_api_key`, `llm_base_url`, `llm_model` were removed from `ConfigSchema`. LLM configuration is now handled by `LLMProviderRegistry` with per-provider defaults via env vars `ORCH_PROVIDER_{NAME}__{KEY}` (e.g. `ORCH_PROVIDER_OPENAI__API_KEY=sk-xxx`).

### 4.3 Remote key remapping

`ConfigManager.resolve()` accepts a `key_mapping: dict[str, str]` kwarg that maps internal field names to the customer's config-center key. The same field naming as the env var (uppercased prefix + field) is used when no mapping is supplied.

```python
cfg = await config_mgr.resolve(
    MyConfig,
    prefix="my.registry",
    key_mapping={"db_path": "woa.orchestration.db.path"},
    sensitive_fields={"api_key"},
)
```

---

## 5. Per-Request Context API

Import from `mh_gateway`:

```python
from mh_gateway import (
    get_current_request,     # -> Request | None
    get_current_cookies,     # -> dict[str, str]
    get_current_auth_token,  # -> str (Bearer token or cookie fallback)
    get_current_user_id,     # -> str | None (after auth)
    get_current_locale,      # -> str ("zh" / "en")
    get_current_trace_id,    # -> str (from X-Request-Id or auto-generated)
)
```

Context is initialized by middleware at request start, cleaned at request end. Use these in any adapter method that needs request awareness.

---

## 6. Default Built-in Behavior

When an adapter is not injected:

| Adapter | Default | Behavior |
|---------|---------|----------|
| `token_verifier` | `_DefaultAuthProvider` | Reads `X-User-Id` header or `x-user-id` cookie; no signature check |
| `permission_checker` | `_DefaultAuthProvider` (shared) | Built-in permission table for `admin`/`member`/`user` |
| `registry_provider` | `InMemoryManagementProvider` | Empty when `dev_mode=false`, demo data when true |
| `management_provider` | `InMemoryManagementProvider` (same instance as `registry_provider`) | CRUD API available only when `registry_provider` implements `MetadataManager` |
| `outbound_auth_provider` | `_DefaultOutboundAuthProvider` | Forwards all non-hop-by-hop headers |
| `m2m_auth_provider` | `_DefaultM2MAuthProvider` | Allows all requests — only for dev |
| `llm_provider_factory` | `OpenAILLMProvider` | Created from `llm_provider_registry.create("openai", {})` |
| `llm_provider_registry` | `LLMProviderRegistry` | Built-in `openai`, `anthropic`, `openai_viz` registered; defaults from `ORCH_PROVIDER_*` env vars |
| `llm_extra_headers_provider` | `None` | No extra headers injected |
| `generated_tool_provider` | `DefaultToolGenerator` | LLM-based; reuses `llm_provider_factory` |
| `generated_agent_provider` | `DefaultAgentGenerator` | LLM-based; reuses `llm_provider_factory` |

---

## 7. AppState Schema

Accessible at runtime via `request.app.state.adapters`:

```python
adapters = request.app.state.adapters
adapters.settings                # ConfigSchema
adapters.token_verifier          # UserAuthProvider
adapters.permission_checker      # PermissionChecker
adapters.registry_provider       # RegistryProvider
adapters.management_provider     # MetadataManager | None
adapters.llm_provider_factory    # Callable[[], LLMProvider]
adapters.llm_provider_registry   # LLMProviderRegistry
adapters.llm_extra_headers_provider  # ExtraHeadersProvider | None
adapters.outbound_auth_provider  # OutboundAuthProvider
adapters.m2m_auth_provider       # M2MAuthProvider
adapters.generated_tool_provider # ToolGenerator | None
adapters.generated_agent_provider # AgentGenerator | None
adapters.logger                  # logging.Logger
```

---

## 8. Chat SSE Stream Protocol

`POST /api/v1/chat/{memory_id}`（支持用户 Token 或 M2M 鉴权）returns SSE events with `event:` and `data:` fields:

```
event: AgentStart\ndata: {}\n\n
event: LLMStart\ndata: {"tool_names":[...],"message_count":N,"total_chars":N}\n\n
event: LLMChunk\ndata: {"content":"...","reasoning":"...","tool_calls":[...]}\n\n
event: LLMEnd\ndata: {"content":"...","reasoning_content":"...","tool_calls":[...],"usage":{...},"error":null}\n\n
event: ExecutionStart\ndata: {"tool_calls":[...]}\n\n
event: ToolStart\ndata: {"tool_call":{...},"display_name":"..."}\n\n
event: ToolProgress\ndata: {"tool_call":{...},"chunk":...}\n\n
event: ToolEnd\ndata: {"tool_call":{...},"result":"...","meta":{...}}\n\n
event: ExecutionEnd\ndata: {"results":[...],"error":null}\n\n
event: AgentEnd\ndata: {"response":"...","time_taken":N,"exceeded":false,"interrupted":false,"error":null}\n\n
event: MemoryUpdate\ndata: {"usage":{...}}\n\n
event: done\ndata: {}\n\n
```

---

## 9. Database

- `init_db()` called in lifespan with `settings.database_url` and `settings.db_auto_schema`
- Built-in SQLite via `aiosqlite`. Custom backends via `set_session_store_factory()`:

  ```python
  from mh_gateway.services import database as db_svc
  db_svc.set_session_store_factory(lambda: MySessionStore(my_db_conn))
  ```

- `db_auto_schema=False` (default) — set to `True` to call `init_schema()` automatically
- All tables have audit columns: `created_by`, `last_updated_by`, `creation_date`, `last_update_date`, `delete_flag` (N/Y), `last_update_trace_id`
- Primary keys are snowflake-like BIGINTs, not auto-increment
- Full PostgreSQL example in [customer-adaptation-guide.md](customer-adaptation-guide.md)

---

## 10. Error Handling

| Error | Source | Behavior |
|-------|--------|----------|
| `ConfigError` | `ConfigManager.resolve()` | Process exits with missing field list |
| `RuntimeError` | `get_db()` | E.g. database accessed before `init_db()` |
| 401 | Auth middleware | Invalid/missing token |
| 403 | PermissionMiddleware | Missing permission for tool/agent/scenario |
| 403 | Chat endpoint | Session doesn't belong to user |
| 404 | Various | Session/agent/tool/scenario not found |
| 501 | Management API | `management_provider` is None (not injected) |
