from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, TYPE_CHECKING, runtime_checkable

if TYPE_CHECKING:
    from minimal_harness.llm.llm import LLMProvider


# ── Identity & Auth ───────────────────────────────────────────────────────────


@dataclass
class UserIdentity:
    """Standard identity object returned by token verification."""

    user_id: str
    username: str = ""
    roles: list[str] = field(default_factory=list)
    extra_data: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class UserAuthProvider(Protocol):
    """Validates authentication requests and resolves them to a UserIdentity.

    Customer deployment: implement this protocol to integrate with
    the customer's SSO / token introspection endpoint.

    The ``request`` argument is the raw HTTP request (e.g. FastAPI ``Request``).
    Implementations may read headers, cookies, query parameters, or call
    external auth services to determine the caller's identity.
    """

    async def verify(self, request: Any) -> UserIdentity | None:
        """Validate *request* and return the user identity, or None if invalid."""
        ...

    async def logout(self, request: Any, response: Any) -> None:
        """Clear authentication state on explicit logout.

        Called when a user explicitly logs out. Implementations **must** clear
        any cookies, tokens, or session state on the *response* that was used
        to authenticate the *request*.
        """
        ...


@runtime_checkable
class PermissionChecker(Protocol):
    """Checks permissions for a given user.

    Customer deployment: implement this protocol to integrate with
    the customer's permission system (RBAC, OPA, OpenFGA, etc.).
    """

    async def get_permissions(self, user_id: str) -> list[str]:
        """Return all permission strings for the given user."""
        ...

    async def check(self, user_id: str, permission: str) -> bool:
        """Check whether *user_id* has a specific *permission*.

        Permission strings follow the format ``action:resource:target``
        and support ``*`` wildcards at any segment.
        """
        ...


def match_permission(user_permissions: list[str], target: str) -> bool:
    """Wildcard-aware permission matching.

    Each permission in *user_permissions* is a ``:``-separated triple.
    ``*`` in any segment acts as a wildcard.
    """
    target_parts = target.split(":", maxsplit=2)
    for p in user_permissions:
        parts = p.split(":", maxsplit=2)
        if len(parts) == 3 and len(target_parts) == 3:
            if all(parts[i] == target_parts[i] or parts[i] == "*" for i in range(3)):
                return True
    return False


def has_broad_permission(user_permissions: list[str], prefix: str) -> bool:
    """Check if *user_permissions* grants access to all resources under *prefix*.

    ``has_broad_permission(perms, "use:tool")`` returns True if any permission
    matches ``use:tool:*``, ``*:*:*``, ``*:tool:*``, or ``use:*:*``.

    This is a fast-path helper for list endpoints — when it returns True,
    per-item permission checks can be skipped entirely.
    """
    for p in user_permissions:
        if p in ("*", "*:*:*", "*:*"):
            return True
        if p == f"{prefix}:*":
            return True
    return False


# ── M2M Auth ──────────────────────────────────────────────────────────────────


@runtime_checkable
class M2MAuthProvider(Protocol):
    """机机接口鉴权提供者。

    在 agent 调用端点（``POST /api/v1/agents/{name}/run``）和
    runtime tool 端点（``POST /api/v1/tools/*/execute``）被调用时
    验证调用方身份。

    客户企业部署时实现此 protocol，通过 SOA 或其他机制验证调用方
    应用身份。接收原始 ``Request`` 对象，可自行决定检查方式（header、
    cookie、mTLS 证书等）。

    返回 ``app_id`` 表示鉴权通过（拥有所有权限），返回 ``None`` 表示失败。
    """

    async def authenticate(self, request: Any) -> str | None: ...

    async def get_identity_headers(
        self, request: Any, identity: str
    ) -> dict[str, str]: ...

    async def close(self) -> None: ...


# ── Outbound Auth ─────────────────────────────────────────────────────────────


@runtime_checkable
class OutboundAuthProvider(Protocol):
    """为出站远程 agent / tool 调用注入认证 header。

    客户企业部署时实现此 protocol，将当前请求的身份凭证传递给下游
    agent/tool 服务，例如转发 ``Authorization: Bearer <token>``、
    添加服务间 HMAC 签名等。

    此 protocol 在 ``runtime_service._tool_binding()`` 中调用，
    返回值会合并到 ``RemoteToolBinding.headers`` 中，
    最终由 ``SSEToolExecutor`` 设置在出站 HTTP 请求的 header 中。
    """

    async def get_headers(
        self,
        request: Any,
        target_url: str,
        target_type: str,
    ) -> dict[str, str]: ...


# ── Registry & Metadata ───────────────────────────────────────────────────────


@runtime_checkable
class RegistryProvider(Protocol):
    """Registry metadata provider (agents + tools + scenarios).

    Customer deployment: implement this protocol to query your own
    registry system instead of the built-in registry-service.

    Optional performance optimization — implement ``get_tools(names)``
    returning ``dict[str, dict[str, Any] | None]`` to replace N+1
    ``get_tool`` calls with a single batch operation. The runtime
    auto-detects and uses this method when available.
    """

    async def get_agent(self, name: str) -> dict[str, Any] | None: ...
    async def list_agents(self) -> list[dict[str, Any]]: ...
    async def get_tool(self, name: str) -> dict[str, Any] | None: ...
    async def list_tools(self) -> list[dict[str, Any]]: ...
    async def get_scenario(self, scenario_id: str) -> dict[str, Any] | None: ...
    async def list_scenarios(self) -> list[dict]: ...


@runtime_checkable
class MetadataManager(RegistryProvider, Protocol):
    """Unified metadata provider for agents, tools, and scenarios.

    Combines read (from ``RegistryProvider``) and write operations so that
    customer deployments only need to implement a single protocol instead of
    two separate ones.

    All methods accept/return plain ``dict`` — the orchestration layer does
    not impose a fixed schema beyond the keys listed in the doc-string of
    each method.
    """

    # ── Tool CRUD ──

    async def create_tool(self, tool: dict[str, Any]) -> dict[str, Any]: ...
    async def update_tool(self, name: str, tool: dict[str, Any]) -> dict[str, Any]: ...
    async def delete_tool(self, name: str) -> None: ...

    # ── Agent CRUD ──

    async def create_agent(self, agent: dict[str, Any]) -> dict[str, Any]: ...
    async def update_agent(
        self, name: str, agent: dict[str, Any]
    ) -> dict[str, Any]: ...
    async def delete_agent(self, name: str) -> None: ...

    # ── Scenario CRUD ──

    async def create_scenario(self, scenario: dict[str, Any]) -> dict[str, Any]: ...
    async def update_scenario(
        self, scenario_id: str, scenario: dict[str, Any]
    ) -> dict[str, Any]: ...
    async def delete_scenario(self, scenario_id: str) -> None: ...

    # ── Scenario-Agent-Tool relationships ──

    async def add_scenario_agent(
        self, scenario_id: str, agent_name: str, tool_names: list[str] | None = None
    ) -> dict[str, Any]: ...
    async def remove_scenario_agent(
        self, scenario_id: str, agent_name: str
    ) -> dict[str, Any]: ...
    async def add_agent_tool(
        self, scenario_id: str, agent_name: str, tool_name: str
    ) -> dict[str, Any]: ...
    async def remove_agent_tool(
        self, scenario_id: str, agent_name: str, tool_name: str
    ) -> dict[str, Any]: ...

    async def close(self) -> None: ...


# ── LLM Provider ──────────────────────────────────────────────────────────────


@runtime_checkable
class LLMProviderFactory(Protocol):
    """Zero-argument factory that returns an LLMProvider instance."""

    def __call__(self) -> LLMProvider: ...


@runtime_checkable
class LLMProviderRegistry(Protocol):
    """Registry for creating LLM providers by name.

    Customer deployment: implement this protocol to integrate with
    the customer's own LLM provider management system.
    """

    def create(self, provider: str, cfg: dict[str, Any]) -> LLMProvider: ...
    def list_providers(self) -> list[str]: ...
    def is_registered(self, name: str) -> bool: ...


@runtime_checkable
class LLMProviderStore(Protocol):
    """CRUD for user-configured LLM provider credentials.

    Customer deployment: implement this protocol to store provider
    credentials (api_key, base_url) in your own secret store.
    All methods accept/return plain ``dict``.
    """

    async def list_providers(self) -> list[dict[str, Any]]: ...
    async def get_provider(self, name: str) -> dict[str, Any] | None: ...
    async def create_provider(self, provider: dict[str, Any]) -> dict[str, Any]: ...
    async def update_provider(
        self, name: str, provider: dict[str, Any]
    ) -> dict[str, Any]: ...
    async def delete_provider(self, name: str) -> None: ...
    async def get_model_max_context(
        self, provider_name: str, model_code: str
    ) -> int: ...
    async def close(self) -> None: ...


# ── Database ──────────────────────────────────────────────────────────────────


@runtime_checkable
class DatabaseProtocol(Protocol):
    """SQL database interface for the orchestration service.

    Customer deployment: implement this protocol to connect to your
    own database system (PostgreSQL, MySQL, etc.) instead of SQLite.
    """

    async def init(self, dsn: str) -> None: ...
    async def close(self) -> None: ...
    async def execute(self, sql: str, params: list | None = None) -> Any: ...
    async def execute_write(self, sql: str, params: list | None = None) -> int: ...
    async def fetch_one(self, sql: str, params: list | None = None) -> dict | None: ...
    async def fetch_all(self, sql: str, params: list | None = None) -> list[dict]: ...
    async def begin(self) -> None: ...
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...
    async def executemany(self, sql: str, params_list: list[list]) -> None: ...


# ── Session Store ─────────────────────────────────────────────────────────────


@runtime_checkable
class SessionStoreProtocol(Protocol):
    """Protocol for persistent session (memory) storage.

    All methods operate on ``Session`` instances, which carry identity
    information (``user_id``, ``scenario_id``) alongside message data.
    """

    async def create_session(
        self,
        session_id: str | None = None,
        agent_name: str = "",
        user_id: str = "",
        scenario_id: str | None = None,
        transient: bool = False,
        display_name_locale: str | None = None,
    ) -> Any: ...

    async def get_session(self, session_id: str) -> Any | None: ...

    async def save_memory(
        self, memory: Any, session_id: str, extra: dict[str, Any] | None = None
    ) -> None: ...

    async def update_usage(self, memory: Any, session_id: str) -> None: ...

    async def delete_session(self, session_id: str) -> bool: ...

    async def list_sessions(self) -> list[Any]: ...

    async def list_user_sessions(
        self, user_id: str, scenario_id: str | None = None
    ) -> list[Any]: ...

    async def get_session_messages(self, session_id: str) -> list[dict]: ...

    def get_messages_as_items(self, session: Any) -> list[dict]: ...


# ── Eval Storage ──────────────────────────────────────────────────────────────


@runtime_checkable
class EvalResultStorage(Protocol):
    """评测结果存储适配器 —— 客户自行实现。

    所有方法在相应事件发生时即被调用，确保数据增量写入，
    即使中途崩溃也不会丢失已完成的 question 结果。
    """

    async def on_batch_started(self, batch_id: str, request: Any) -> None: ...

    async def on_question_started(self, batch_id: str, question: Any) -> None: ...

    async def on_llm_call_recorded(self, batch_id: str, record: Any) -> None: ...

    async def on_question_completed(self, batch_id: str, result: Any) -> None: ...

    async def on_batch_completed(self, batch_id: str, summary: Any) -> None: ...

    async def get_batch(self, batch_id: str) -> Any | None: ...

    async def list_batches(self) -> list[Any]: ...


# ── Config Provider ───────────────────────────────────────────────────────────


@runtime_checkable
class ConfigProvider(Protocol):
    """External configuration / secret provider.

    Customer deployment: implement this protocol to load configuration
    and/or secrets from your own config center / vault (e.g. Apollo,
    Nacos, Consul, HashiCorp Vault, AWS Secrets Manager) instead of
    environment variables.

    ``SecretResolver`` is a backward-compatible alias.
    """

    async def get(self, key: str) -> str | None:
        """Return the config/secret value for *key*, or None if not found."""
        ...


SecretResolver = ConfigProvider
