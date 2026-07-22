"""Public adapter contracts for ``mh-gateway``.

The gateway is a thin orchestration layer: it owns no authentication,
no permission data, no agent registry, and no LLM credentials.  All of
those concerns are exposed as :class:`runtime_checkable` protocols.

A deployment supplies a single :class:`~mh_gateway.app.GatewayAdapters`
bundle — produced by a user-provided *adapter lifespan* — and the
gateway wires its routers, runtime, and middleware around it.

The previous release exposed 13 distinct adapter protocols.  This
release consolidates them into 9 protocols, each with a single
responsibility:

* :class:`UserAuthenticator` — end-user authentication.
* :class:`AuthorizationProvider` — fine-grained permission checks.
* :class:`M2MAuthenticator` — inbound machine-to-machine auth.
* :class:`OutboundAuthProvider` — outbound request header injection.
* :class:`MetadataRepository` — scenario / agent / tool metadata
  (read + write, including relationships).
* :class:`LLMProviderService` — driver registry, provider config
  store, dynamic header resolver, and synchronous resolver builder.
* :class:`SessionRepository` — session and message persistence.
* :class:`EvalResultRepository` — incremental eval result storage.
* :class:`ConfigProvider` — startup-only external configuration
  (no longer re-exported as ``SecretResolver``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from minimal_harness.llm.llm import LLMProvider
    from minimal_harness.types import AgentMetadata

    from mh_gateway.llm import LLMProviderConfig


# ── Identity & Auth ───────────────────────────────────────────────────────────


@dataclass
class UserIdentity:
    """Standard identity object returned by token verification."""

    user_id: str
    username: str = ""
    roles: list[str] = field(default_factory=list)
    extra_data: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class UserAuthenticator(Protocol):
    """Validates authentication requests and resolves them to a UserIdentity.

    Customer deployment: implement this protocol to integrate with
    the customer's SSO / token introspection endpoint.

    The ``request`` argument is the raw HTTP request (e.g. FastAPI
    ``Request``).  Implementations may read headers, cookies, query
    parameters, or call external auth services to determine the
    caller's identity.
    """

    async def verify(self, request: Any) -> UserIdentity | None:
        """Validate *request* and return the user identity, or None if invalid."""
        ...

    async def logout(self, request: Any, response: Any) -> None:
        """Clear authentication state on explicit logout.

        Called when a user explicitly logs out.  Implementations
        **must** clear any cookies, tokens, or session state on the
        *response* that was used to authenticate the *request*.
        """
        ...


@runtime_checkable
class AuthorizationProvider(Protocol):
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

    ``has_broad_permission(perms, "use:tool")`` returns True if any
    permission matches ``use:tool:*``, ``*:*:*``, ``*:tool:*``, or
    ``use:*:*``.

    This is a fast-path helper for list endpoints — when it returns
    True, per-item permission checks can be skipped entirely.
    """
    for p in user_permissions:
        if p in ("*", "*:*:*", "*:*"):
            return True
        if p == f"{prefix}:*":
            return True
    return False


# ── M2M & Outbound ────────────────────────────────────────────────────────────


@runtime_checkable
class M2MAuthenticator(Protocol):
    """Inbound machine-to-machine authentication.

    Used for the M2M-only endpoints (``/api/v1/agents/{n}/run``,
    ``/api/v1/tools/*/execute``, ``/api/v1/eval/batches``) to verify
    the calling application identity.

    Implementations receive the raw ``Request`` object and may inspect
    headers, cookies, or mTLS material.  The returned ``app_id`` is
    used as the effective identity when the request did not pass a
    user token.

    Identity propagation to outbound calls is the responsibility of
    :class:`OutboundAuthProvider`, not this protocol.
    """

    async def authenticate(self, request: Any) -> str | None:
        """Return the calling application id, or ``None`` to reject."""
        ...

    async def close(self) -> None: ...


@dataclass
class OutboundRequestContext:
    """All information the gateway makes available for outbound auth decisions.

    Consolidates the original HTTP request, the target URL/type, the
    resolved identity (end-user or M2M app), and the scenario/agent
    context of the originating call.  The deployment-provided
    :class:`OutboundAuthProvider` returns a header map that is merged
    into the downstream request.

    ``target_type`` is one of:

    * ``"tool"`` — remote tool HTTP call.
    * ``"agent"`` — remote agent HTTP call.
    """

    request: Any
    target_url: str
    target_type: str
    identity: str = ""
    scenario_id: str = ""
    agent_name: str = ""


@runtime_checkable
class OutboundAuthProvider(Protocol):
    """Inject authentication headers into outbound HTTP calls.

    The gateway calls this provider exactly once per outbound request
    to gather the final header set.  Implementations may add bearer
    tokens, HMAC signatures, end-user identity headers (``x-user-id``),
    or any other header required by the downstream service.

    Replace the old split between ``OutboundAuthProvider.get_headers``
    and ``M2MAuthProvider.get_identity_headers``: both concerns now
    live here, evaluated together.
    """

    async def get_headers(self, context: OutboundRequestContext) -> dict[str, str]:
        """Return the header map for the outbound request.

        May return an empty dictionary if no additional headers are
        required.
        """
        ...

    async def close(self) -> None: ...


# ── Metadata ──────────────────────────────────────────────────────────────────


@runtime_checkable
class MetadataRepository(Protocol):
    """Scenario, agent, and tool metadata, with full read/write.

    Read methods return plain ``dict``; write methods accept plain
    ``dict`` and echo back the persisted entity.  The orchestrator
    does not impose a fixed schema beyond the keys listed in the
    doc-string of each method.

    ``get_tools`` is a required batch method — it replaces the old
    optional ``get_tools(names)`` optimisation.  Implementations are
    expected to honour the batch contract.
    """

    # ── Read ──

    async def get_agent(self, name: str) -> dict[str, Any] | None: ...
    async def list_agents(self) -> list[dict[str, Any]]: ...
    async def get_tool(self, name: str) -> dict[str, Any] | None: ...
    async def list_tools(self) -> list[dict[str, Any]]: ...
    async def get_tools(
        self, names: list[str]
    ) -> dict[str, dict[str, Any] | None]:
        """Batch fetch tool metadata; missing tools are returned as ``None``."""
        ...

    async def get_scenario(self, scenario_id: str) -> dict[str, Any] | None: ...
    async def list_scenarios(self) -> list[dict[str, Any]]: ...

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

    async def create_scenario(
        self, scenario: dict[str, Any]
    ) -> dict[str, Any]: ...
    async def update_scenario(
        self, scenario_id: str, scenario: dict[str, Any]
    ) -> dict[str, Any]: ...
    async def delete_scenario(self, scenario_id: str) -> None: ...

    # ── Scenario–Agent–Tool relationships ──

    async def add_scenario_agent(
        self,
        scenario_id: str,
        agent_name: str,
        tool_names: list[str] | None = None,
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


# ── LLM Provider Service ──────────────────────────────────────────────────────


@dataclass
class LLMResolveSpec:
    """Input to :meth:`LLMProviderService.build_resolver`.

    Describes which LLM instance a given agent should use.  ``agent``
    is the agent metadata the runtime is about to invoke, and
    ``user`` is the requesting identity (used for tenant-aware
    credentials).
    """

    agent: "AgentMetadata"
    user: str = ""


@runtime_checkable
class LLMProviderService(Protocol):
    """Unified LLM access for the gateway runtime.

    Combines four responsibilities that were previously split across
    ``LLMProviderFactory``, ``LLMProviderRegistry``, ``LLMProviderStore``,
    and the implicit ``llm_extra_headers_provider``:

    * **Driver registry** — list and instantiate LLM provider drivers
      (e.g. openai, anthropic) by name.
    * **Config backend** — CRUD on per-deployment provider
      configurations (api_key, base_url, default_model, models list).
    * **Header resolver** — dynamic per-call HTTP headers (bearer
      token rotation, tracing ids, etc.).
    * **Resolver builder** — produces a synchronous
      ``Callable[[AgentMetadata], LLMProvider]`` matching the
      ``minimal_harness`` contract, eagerly resolving credentials at
      runtime construction time.

    Production code MUST obtain LLM instances from
    :meth:`create_llm` or :meth:`build_resolver`; calling the legacy
    factory / registry directly is no longer supported.
    """

    # ── LLM creation ──

    def list_provider_types(self) -> list[str]:
        """Return the names of all registered LLM driver types."""
        ...

    def create_llm(self, spec: LLMResolveSpec) -> "LLMProvider":
        """Build a single ``LLMProvider`` instance.

        Resolves provider credentials from the config backend and
        consults the header resolver, then delegates to the matching
        driver.  Raises ``ValueError`` if the driver is unknown or
        the spec cannot be satisfied.
        """
        ...

    def build_resolver(
        self, specs: list[LLMResolveSpec]
    ) -> "Callable[[AgentMetadata], LLMProvider]":
        """Build a synchronous per-agent resolver.

        Pre-loads all referenced provider configs so that the
        returned closure does not perform network IO.  This is the
        contract that ``minimal_harness``'s ``AgentRuntime`` expects.
        """
        ...

    # ── Config backend ──

    async def list_configs(self) -> list["LLMProviderConfig"]: ...
    async def get_config(self, name: str) -> "LLMProviderConfig | None": ...
    async def create_config(
        self, config: "LLMProviderConfig"
    ) -> "LLMProviderConfig": ...
    async def update_config(
        self, name: str, config: "LLMProviderConfig"
    ) -> "LLMProviderConfig": ...
    async def delete_config(self, name: str) -> None: ...
    async def get_model_max_context(
        self, provider_name: str, model_code: str
    ) -> int: ...

    async def close(self) -> None: ...


# ── Session ───────────────────────────────────────────────────────────────────


@runtime_checkable
class SessionRepository(Protocol):
    """Persistent session and message storage.

    Replaces the previous ``SessionStoreProtocol`` plus the implicit
    ``DatabaseProtocol`` shim.  Implementations are responsible for
    any underlying database; the gateway never sees a raw SQL
    connection.
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
    async def healthcheck(self) -> None:
        """Raise if the underlying store is not healthy.

        Used by the ``/ready`` endpoint.  Default implementation is
        a no-op for stores that do not require a connectivity check.
        """
        ...

    async def close(self) -> None: ...


# ── Eval ──────────────────────────────────────────────────────────────────────


@runtime_checkable
class EvalResultRepository(Protocol):
    """Incremental eval result storage.

    All methods are invoked at the corresponding runtime event so
    that data lands progressively; a mid-run crash does not lose
    the already-completed question results.
    """

    async def on_batch_started(self, batch_id: str, request: Any) -> None: ...
    async def on_question_started(self, batch_id: str, question: Any) -> None: ...
    async def on_llm_call_recorded(self, batch_id: str, record: Any) -> None: ...
    async def on_question_completed(self, batch_id: str, result: Any) -> None: ...
    async def on_batch_completed(self, batch_id: str, summary: Any) -> None: ...
    async def get_batch(self, batch_id: str) -> Any | None: ...
    async def list_batches(self) -> list[Any]: ...
    async def close(self) -> None: ...


# ── Config ────────────────────────────────────────────────────────────────────


@runtime_checkable
class ConfigProvider(Protocol):
    """Startup-only external configuration / secret provider.

    Used by :class:`~mh_gateway.config_manager.ConfigManager` to
    resolve sensitive values at process start.  The gateway no
    longer re-exports this as ``SecretResolver`` — that name was
    removed in this release.
    """

    async def get(self, key: str) -> str | None:
        """Return the config/secret value for *key*, or None if not found."""
        ...


__all__ = [
    "AuthorizationProvider",
    "ConfigProvider",
    "EvalResultRepository",
    "LLMProviderService",
    "LLMResolveSpec",
    "M2MAuthenticator",
    "MetadataRepository",
    "OutboundAuthProvider",
    "OutboundRequestContext",
    "SessionRepository",
    "UserAuthenticator",
    "UserIdentity",
    "has_broad_permission",
    "match_permission",
]
