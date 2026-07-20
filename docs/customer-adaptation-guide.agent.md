# Customer Adaptation Guide (for Code Agent)

This document specifies the exact implementation contract for adapting `mh-gateway` into a customer environment. Follow these instructions precisely �?any deviation will cause runtime failures.

---

## 1. Repository Structure & Dependencies

Two packages must be installed (not on PyPI; delivered as `.whl`):

```
minimal_harness          # SDK: protocols, agent runtime, tool system, database, LLM
mh_gateway # FastAPI web gateway, depends on minimal_harness
```

All customer adapter code lives in a **single Python file** (e.g. `my_app.py`) that constructs and exposes a FastAPI app via `create_app()`.

---

## 2. What You MUST Implement

### 2.1 UserAuthProvider (import: `from mh_gateway.auth import UserAuthProvider`)

```python
@runtime_checkable
class UserAuthProvider(Protocol):
    async def verify(self, request: Any) -> UserIdentity | None:
        ...

    async def logout(self, request: Any, response: Any) -> None:
        ...
```

**Contract:**

**`verify`:**
- Input: raw FastAPI `Request` object
- Output: `UserIdentity` on success, `None` on invalid/missing credentials
- Read from `request.headers`, `request.cookies`, or call external auth API as needed

**`logout`:**
- Called when the user explicitly logs out via `POST /api/v1/auth/logout`
- **Must** clear any cookies, tokens, or session state on *response* that was used to authenticate *request*
- Example: clearing a session cookie via `response.set_cookie(key="session", ..., max_age=0)`, or revoking a token via external API and removing the cookie

```python
@dataclass
class UserIdentity:
    user_id: str            # REQUIRED �?used for permission checks & session ownership
    username: str = ""
    roles: list[str] = field(default_factory=list)
    extra_data: dict[str, Any] = field(default_factory=dict)
```

### 2.2 PermissionChecker (import: `from mh_gateway.auth import PermissionChecker`)

```python
@runtime_checkable
class PermissionChecker(Protocol):
    async def get_permissions(self, user_id: str) -> list[str]:
        ...
    async def check(self, user_id: str, permission: str) -> bool:
        ...
```

**Contract:**
- Permission format: `action:resource:target` (e.g. `"use:agent:code-reviewer"`)
- Wildcard `*` supported at any segment (e.g. `"use:agent:*"`)
- Use `match_permission()` from `mh_gateway.auth` to evaluate wildcards
- Return `True` if permission string matches user's permissions

### 2.3 MetadataManager (import: `from mh_gateway.adapters import MetadataManager`)

This is the **recommended** unified read/write protocol. It extends `RegistryProvider` with CRUD methods.

```python
@runtime_checkable
class MetadataManager(RegistryProvider, Protocol):
    # ── Read (inherited from RegistryProvider) ──
    async def get_agent(self, name: str) -> dict[str, Any] | None: ...
    async def list_agents(self) -> list[dict[str, Any]]: ...
    async def get_tool(self, name: str) -> dict[str, Any] | None: ...
    async def list_tools(self) -> list[dict[str, Any]]: ...
    async def get_scenario(self, scenario_id: str) -> dict[str, Any] | None: ...
    async def list_scenarios(self) -> list[dict]: ...

    # Optional optimization:
    # async def get_tools(self, names: list[str]) -> dict[str, dict[str, Any] | None]
    # Batch-fetch tool metadata. Runtime auto-detects and uses this to avoid N+1 queries.
```

    # ── Agent CRUD ──
    async def create_agent(self, agent: dict[str, Any]) -> dict[str, Any]: ...
    async def update_agent(self, name: str, agent: dict[str, Any]) -> dict[str, Any]: ...
    async def delete_agent(self, name: str) -> None: ...

    # ── Tool CRUD ──
    async def create_tool(self, tool: dict[str, Any]) -> dict[str, Any]: ...
    async def update_tool(self, name: str, tool: dict[str, Any]) -> dict[str, Any]: ...
    async def delete_tool(self, name: str) -> None: ...

    # ── Scenario CRUD ──
    async def create_scenario(self, scenario: dict[str, Any]) -> dict[str, Any]: ...
    async def update_scenario(self, scenario_id: str, scenario: dict[str, Any]) -> dict[str, Any]: ...
    async def delete_scenario(self, scenario_id: str) -> None: ...

    # ── Relationships ──
    async def add_scenario_agent(self, scenario_id, agent_name, tool_names=None) -> dict: ...
    async def remove_scenario_agent(self, scenario_id, agent_name) -> dict: ...
    async def add_agent_tool(self, scenario_id, agent_name, tool_name) -> dict: ...
    async def remove_agent_tool(self, scenario_id, agent_name, tool_name) -> dict: ...

    async def close(self) -> None: ...
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
  "system_prompt_locale": "{\"zh\":\"你是一�?..\",\"en\":\"You are...\"}",
  "endpoint_url": "/api/v1/agents/agent-id/run",
  "provider": "openai",
  "model": "gpt-4o",
  "llm_config": {"temperature": 0.7, "max_tokens": 4096}
}
```

> `endpoint_url` non-empty = remote agent (M2M endpoint); empty/absent = local execution.
> `display_name_locale`, `description_locale`, `system_prompt_locale` are JSON-encoded strings (not dicts) for backward compatibility.

**Tool dict shape:**
```json
{
  "name": "tool-id",
  "display_name": "Human Name",
  "display_name_locale": "{\"zh\":\"...\",\"en\":\"...\"}",
  "description": "What this tool does",
  "description_locale": "{\"zh\":\"...\",\"en\":\"...\"}",
  "parameters": {"type": "object", "properties": {...}, "required": [...]},
  "endpoint_url": "/api/v1/tools/tool-id/execute",
  "source_code": "async def run(**kwargs): ..."
}
```

> `endpoint_url` non-empty = remote execution; empty = use `_fn` (local async function) or `source_code` (generated tool).

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

### 2.4 OutboundAuthProvider (import: `from mh_gateway import OutboundAuthProvider`)

```python
@runtime_checkable
class OutboundAuthProvider(Protocol):
    async def get_headers(self, request: Any, target_url: str, target_type: str) -> dict[str, str]: ...
```

- Called when orchestration makes outbound HTTP calls to remote agent/tool endpoints
- Default: forwards all request headers except hop-by-hop headers
- Implement to inject service-mesh auth (HMAC, mTLS, etc.)

### 2.5 M2MAuthProvider (import: `from mh_gateway import M2MAuthProvider`)

```python
@runtime_checkable
class M2MAuthProvider(Protocol):
    async def authenticate(self, request: Any) -> str | None: ...
    async def get_identity_headers(self, request: Any, identity: str) -> dict[str, str]: ...
    async def close(self) -> None: ...
```

- Protects `POST /api/v1/agents/{name}/run` and `POST /api/v1/tools/*/execute`
- Returns `app_id` (str) on success, `None` for 401
- Default: allows all requests, returns `"default"` (development only �?MUST replace in production)
### 2.6 ConfigProvider (optional; import: `from mh_gateway import ConfigProvider`)

```python
@runtime_checkable
class ConfigProvider(Protocol):
    async def get(self, key: str) -> str | None:
        ...
```

**Contract:**
- Input: remote config key (e.g. `"woa.orchestration.db.path"`)
- Output: config value string, or `None` if not found
- Used for both non-sensitive configuration (Apollo, Nacos, Consul) and secrets (Vault, AWS Secrets Manager)
- `SecretResolver` is a backward-compatible alias for `ConfigProvider`
- `ConfigManager` accepts two `ConfigProvider` instances �?one for config, one for secrets �?differentiated by usage, not by type

---

## 3. Configuration System

### 3.1 ConfigSchema (import: `from mh_gateway.config import ConfigSchema`)

```python
class ConfigSchema(BaseModel):
    db_path: str = "./sessions.db"            # SQLite file path
    db_auto_schema: bool = False              # Auto-create tables
    cors_origins: list[str] = Field(default_factory=list)
    dev_mode: bool = False                    # Dev mode: built-in agents, dev routes, SPA static files
    enable_eval: bool = True                  # Eval endpoints
    eval_results_dir: str = "./eval_results"   # Eval results directory
    log_level: str = "INFO"                   # Declared field. Logging is controlled via MH_LOG_LEVEL or manual setup
    verify_agent_tool_ssl: bool = False       # SSL verification for remote agent/tool calls
    metrics_enabled: bool = False             # Enable metrics collection
    metrics_push_interval: int = 60           # Metrics push interval (seconds)
```

**All fields have defaults** �?the service starts without any environment variables.

**Resolution order** (per field, independent):
1. Environment variable `{PREFIX}_{FIELD}` (highest priority, e.g. `ORCH_DB_PATH`)
2. `secret_resolver` (a `ConfigProvider` instance, if field in `sensitive_fields`)
3. `config_provider` (a `ConfigProvider` instance, if field NOT in `sensitive_fields`)
4. Model default value

Both `config_provider` and `secret_resolver` use the same `ConfigProvider` protocol; `SecretResolver` is a backward-compatible alias.

> **LLM config is NOT in ConfigSchema.** LLM configuration uses `LLMProviderRegistry` with env vars `ORCH_PROVIDER_{NAME}__{KEY}` (e.g. `ORCH_PROVIDER_OPENAI__API_KEY=sk-xxx`).

### 3.2 ConfigManager (import: `from mh_gateway.config_manager import ConfigManager`)

```python
class ConfigManager:
    def __init__(
        self,
        config_provider: ConfigProvider | None = None,
        secret_resolver: ConfigProvider | None = None,
    ) -> None: ...

    async def resolve(
        self,
        schema_cls: type[T],                    # BaseModel subclass
        *,
        prefix: str = "ORCH",
        sensitive_fields: set[str] | None = None,
        key_mapping: dict[str, str] | None = None,
    ) -> T: ...
```

Both parameters accept `ConfigProvider` instances. `SecretResolver` is a backward-compatible alias.

**Usage pattern for custom adapter config:**

```python
class MyAdapterConfig(BaseModel):
    api_url: str = "http://localhost:8080"
    api_key: str = ""

config_mgr = ConfigManager(
    config_provider=MyConfigProvider(),
    secret_resolver=MySecretResolver(),  # same protocol type, different instance
)
cfg = await config_mgr.resolve(
    MyAdapterConfig,
    prefix="my.adapter",
    sensitive_fields={"api_key"},
)
```

---

## 4. App Assembly (import: `from mh_gateway.app import create_app`)

### 4.1 create_app() signature

```python
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
    generated_tool_provider: LifespanHook | None = None,
    generated_agent_provider: LifespanHook | None = None,
    llm_provider_registry: LifespanHook | None = None,
    lifespan_hooks: list[LifespanHook] | None = None,
) -> FastAPI:
```

**Parameter rules:**
- `settings` is REQUIRED
- All adapter hooks are LifespanHooks (`Callable[[FastAPI], AbstractAsyncContextManager[None]]`) �?they receive the app and override their slot on `app.state.adapters`
- `llm_extra_headers_provider` is NOT a LifespanHook �?it's a direct `Callable[[], Awaitable[dict[str, str]]]`
- `management_provider` is the primary hook for agent/tool/scenario data (replaces the old `registry_provider` parameter)
- `logger` is deprecated �?configure `logging.getLogger()` (root logger) before calling `create_app()` instead
- Any `None` adapter gets a built-in default

### 4.2 AppState (import: `from mh_gateway.app import AppState`)

Accessible at runtime via `request.app.state.adapters`:

```python
class AppState:
    settings: ConfigSchema
    token_verifier: UserAuthProvider
    permission_checker: PermissionChecker
    registry_provider: RegistryProvider | None
    management_provider: MetadataManager | None
    llm_provider_factory: Callable[[], LLMProvider] | None
    outbound_auth_provider: OutboundAuthProvider | None
    m2m_auth_provider: M2MAuthProvider | None
    llm_extra_headers_provider: ExtraHeadersProvider | None
    generated_tool_provider: ToolGenerator | None
    generated_agent_provider: AgentGenerator | None
    llm_provider_registry: LLMProviderRegistry | None
```

---

## 5. Assembly Procedure (Code Agent MUST follow this order)

### Step 1: Create ConfigManager

```python
config_mgr = ConfigManager(                     # no args = env-only mode
    config_provider=MyConfigProvider(),          # optional
    secret_resolver=MySecretResolver(),          # optional
)
```

### Step 2: Resolve adapter-specific config (if needed, inside lifespan hooks)

```python
class MyRegistryConfig(BaseModel):
    api_url: str = ""
    api_key: str = ""
```

### Step 3: Define LifespanHooks for each adapter

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI

@asynccontextmanager
async def my_management_provider(app: FastAPI):
    cfg = await config_mgr.resolve(
        MyRegistryConfig, prefix="my.registry", sensitive_fields={"api_key"}
    )
    app.state.adapters.management_provider = MyRegistry(
        api_url=cfg.api_url, api_key=cfg.api_key
    )
    yield
```

### Step 4: Resolve framework config

```python
settings = asyncio.run(config_mgr.resolve(ConfigSchema, prefix="ORCH"))
```

### Step 5: Assemble app

```python
app = create_app(
    settings=settings,
    token_verifier=my_token_verifier,
    permission_checker=my_permission_checker,
    management_provider=my_management_provider,
    m2m_auth_provider=my_m2m_auth_provider,
)
```

### Step 6: Expose `app` at module level (for uvicorn)

```python
# my_app.py �?uvicorn my_app:app
```

---

## 6. Startup Behavior

- On module import, `asyncio.run()` resolves config
- `create_app()` sets up FastAPI with CORS, routers, middleware
- On server startup (lifespan): `init_db(settings.database_url, auto_schema=settings.db_auto_schema)`
- On server shutdown: close built-in adapters, close database connection
- All ConfigSchema fields have defaults �?no env vars needed for basic startup

### Custom Database (Adapter)

Built-in SQLite only. For PostgreSQL/MySQL etc., implement `SessionStoreProtocol` and inject:

```python
from mh_gateway.services import database as db_svc

db_svc.set_session_store_factory(lambda: MySessionStore(my_db_conn))
```

The factory accepts sync or async callables. To fully replace the database layer, also call `db_svc.set_db()`.

---

## 7. Public API Surface (routes available after startup)

### User-facing

| Method | Path | Requires | Notes |
|--------|------|----------|-------|
| GET | `/api/v1/auth/me` | Valid token | Returns UserIdentity + permissions |
| GET | `/api/v1/scenarios` | Valid token | Permission-filtered |
| GET | `/api/v1/scenarios/{id}` | Valid token | Scenario detail |
| POST | `/api/v1/chat/{memory_id}` | Valid token or M2M auth, session ownership | SSE streaming |
| GET | `/api/v1/sessions` | Valid token or M2M auth | User's sessions |
| POST | `/api/v1/sessions` | Valid token or M2M auth | Create session |
| GET | `/api/v1/sessions/{id}` | Valid token or M2M auth | Session detail |
| GET | `/api/v1/sessions/{id}/messages` | Valid token or M2M auth | Session messages |
| DELETE | `/api/v1/sessions/{id}` | Valid token or M2M auth | Soft delete |
| GET | `/api/v1/agents` | Valid token | Permission-filtered |
| GET | `/api/v1/tools` | Valid token | Permission-filtered |

### Management (requires MetadataManager + manage:*:*)

| Method | Path | Permission |
|--------|------|------------|
| GET/POST | `/api/v1/management/scenarios` | `manage:scene:*` |
| GET/PUT/DELETE | `/api/v1/management/scenarios/{id}` | `manage:scene:*` |
| POST/DELETE | `/api/v1/management/scenarios/{id}/agents` | `manage:scene:*` |
| POST/DELETE | `/api/v1/management/scenarios/{id}/agents/{n}/tools` | `manage:scene:*` |
| GET/POST | `/api/v1/management/agents` | `manage:agent:*` |
| GET/PUT/DELETE | `/api/v1/management/agents/{n}` | `manage:agent:*` |
| GET/POST | `/api/v1/management/tools` | `manage:tool:*` |
| GET/PUT/DELETE | `/api/v1/management/tools/{n}` | `manage:tool:*` |
| GET | `/api/v1/management/providers` | `manage:agent:*` |

### M2M

| Method | Path | Auth |
|--------|------|------|
| POST | `/api/v1/agents/{name}/run` | M2MAuthProvider |

### AI Generation

| Method | Path | Notes |
|--------|------|-------|
| POST | `/api/v1/tool-generator/generate` | SSE stream |
| GET/POST | `/api/v1/tool-generator/tools` | User's generated tools |
| PUT/DELETE | `/api/v1/tool-generator/tools/{name}` | Update/delete |
| POST | `/api/v1/tool-generator/tools/{name}/trial` | SSE trial |
| POST | `/api/v1/tools/generated/{name}/execute` | M2M execute |
| POST | `/api/v1/agent-generator/generate` | SSE stream |
| GET/POST | `/api/v1/agent-generator/agents` | User's generated agents |
| PUT/DELETE | `/api/v1/agent-generator/agents/{name}` | Update/delete |
| POST | `/api/v1/agent-generator/agents/{name}/trial` | SSE trial |

---

## 8. Error Handling

| Error Type | Source | Behavior |
|---|---|---|
| `ConfigError` | `ConfigManager.resolve()` | Only if required fields missing (all ConfigSchema fields have defaults, so this is rare for framework config) |
| `RuntimeError` | `get_db()` | Database accessed before `init_db()` |
| 401 | Auth middleware | Invalid/missing token |
| 403 | PermissionMiddleware | Missing required permission for tool/scenario |
| 403 | Chat endpoint | Session doesn't belong to user |
| 404 | Various | Session/agent/tool/scenario not found |
| 501 | Management API | `management_provider` is None (not injected) |
