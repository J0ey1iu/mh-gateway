"""开箱即用入口。

直接 ``uvicorn mh_gateway.main:app`` 即可启动（仅供体验）。
配置从环境变量（或 ``.env`` 文件）读取。

The default in-memory adapters shipped here are deliberately
minimal — they let the gateway boot for exploration, but every
request that touches auth, the LLM, or eval storage will return
an error response.  Production deployments should use the
:mod:`mh_orch_app` (batteries-included) entry point or supply
their own adapter lifespan.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from minimal_harness.llm.factory import register_builtin_providers
from minimal_harness.llm.llm import LLMProvider, ProviderFactory

from mh_gateway.adapters import (
    AuthorizationProvider,
    EvalResultRepository,
    M2MAuthenticator,
    MetadataRepository,
    OutboundAuthProvider,
    OutboundRequestContext,
    SessionRepository,
    UserAuthenticator,
    UserIdentity,
)
from mh_gateway.app import GatewayAdapters, create_app
from mh_gateway.config import ConfigSchema
from mh_gateway.config_manager import ConfigManager
from mh_gateway.llm import DefaultLLMProviderService, LLMConfigBackend, LLMProviderConfig
from mh_gateway.session import SimpleSession


class _RejectAuth(UserAuthenticator):
    async def verify(self, request: Any) -> UserIdentity | None:
        return None

    async def logout(self, request: Any, response: Any) -> None:  # noqa: ARG002
        return None


class _DenyAuthz(AuthorizationProvider):
    async def get_permissions(self, user_id: str) -> list[str]:  # noqa: ARG002
        return []

    async def check(self, user_id: str, permission: str) -> bool:  # noqa: ARG002
        return False


class _NoM2M(M2MAuthenticator):
    async def authenticate(self, request: Any) -> str | None:  # noqa: ARG002
        return None

    async def close(self) -> None:  # noqa: ARG002
        return None


class _NoOpOutbound(OutboundAuthProvider):
    async def get_headers(self, context: OutboundRequestContext) -> dict[str, str]:  # noqa: ARG002
        return {}

    async def close(self) -> None:  # noqa: ARG002
        return None


class _EmptyMetadata(MetadataRepository):
    async def get_agent(self, name: str) -> dict[str, Any] | None:  # noqa: ARG002
        return None

    async def list_agents(self) -> list[dict[str, Any]]:
        return []

    async def get_tool(self, name: str) -> dict[str, Any] | None:  # noqa: ARG002
        return None

    async def list_tools(self) -> list[dict[str, Any]]:
        return []

    async def get_tools(  # noqa: ARG002
        self, names: list[str]
    ) -> dict[str, dict[str, Any] | None]:
        return {n: None for n in names}

    async def get_scenario(self, scenario_id: str) -> dict[str, Any] | None:  # noqa: ARG002
        return None

    async def list_scenarios(self) -> list[dict[str, Any]]:
        return []

    async def create_tool(self, tool: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        raise ValueError("create_tool is not supported by the default main app")

    async def update_tool(self, name: str, tool: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        raise ValueError("update_tool is not supported by the default main app")

    async def delete_tool(self, name: str) -> None:  # noqa: ARG002
        raise ValueError("delete_tool is not supported by the default main app")

    async def create_agent(self, agent: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        raise ValueError("create_agent is not supported by the default main app")

    async def update_agent(self, name: str, agent: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        raise ValueError("update_agent is not supported by the default main app")

    async def delete_agent(self, name: str) -> None:  # noqa: ARG002
        raise ValueError("delete_agent is not supported by the default main app")

    async def create_scenario(self, scenario: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        raise ValueError("create_scenario is not supported by the default main app")

    async def update_scenario(self, scenario_id: str, scenario: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        raise ValueError("update_scenario is not supported by the default main app")

    async def delete_scenario(self, scenario_id: str) -> None:  # noqa: ARG002
        raise ValueError("delete_scenario is not supported by the default main app")

    async def add_scenario_agent(  # noqa: ARG002
        self, scenario_id: str, agent_name: str, tool_names=None
    ) -> dict[str, Any]:
        raise ValueError("add_scenario_agent is not supported by the default main app")

    async def remove_scenario_agent(  # noqa: ARG002
        self, scenario_id: str, agent_name: str
    ) -> dict[str, Any]:
        raise ValueError("remove_scenario_agent is not supported by the default main app")

    async def add_agent_tool(  # noqa: ARG002
        self, scenario_id: str, agent_name: str, tool_name: str
    ) -> dict[str, Any]:
        raise ValueError("add_agent_tool is not supported by the default main app")

    async def remove_agent_tool(  # noqa: ARG002
        self, scenario_id: str, agent_name: str, tool_name: str
    ) -> dict[str, Any]:
        raise ValueError("remove_agent_tool is not supported by the default main app")

    async def close(self) -> None:
        return None


class _MemoryLLMConfigBackend(LLMConfigBackend):
    """In-memory LLMProviderConfig backend used by the main app.

    Seeds openai/anthropic with empty credentials.  Real deployments
    use the file-backed, SQLite, or vault-backed implementations
    from the orch-app / mh-local packages.
    """

    def __init__(self) -> None:
        self._configs: dict[str, LLMProviderConfig] = {}
        for name in ("openai", "anthropic"):
            self._configs[name] = LLMProviderConfig(
                name=name,
                provider_type=name,
                created_at=datetime.now(UTC).isoformat(),
                updated_at=datetime.now(UTC).isoformat(),
            )

    async def list(self) -> list[LLMProviderConfig]:
        return list(self._configs.values())

    async def get(self, name: str) -> LLMProviderConfig | None:
        return self._configs.get(name)

    async def create(self, config: LLMProviderConfig) -> LLMProviderConfig:
        if not config.name:
            raise ValueError("Provider name is required")
        if config.name in self._configs:
            raise ValueError(f"Provider '{config.name}' already exists")
        now = datetime.now(UTC).isoformat()
        saved = LLMProviderConfig(
            **{**config.__dict__, "created_at": now, "updated_at": now}
        )
        self._configs[config.name] = saved
        return saved

    async def update(
        self, name: str, config: LLMProviderConfig
    ) -> LLMProviderConfig:
        existing = self._configs.get(name)
        if existing is None:
            raise ValueError(f"Provider '{name}' not found")
        merged = LLMProviderConfig(
            **{**existing.__dict__, **config.__dict__, "name": name, "updated_at": datetime.now(UTC).isoformat()}
        )
        self._configs[name] = merged
        return merged

    async def delete(self, name: str) -> None:
        if name not in self._configs:
            raise ValueError(f"Provider '{name}' not found")
        del self._configs[name]

    async def get_model_max_context(
        self, provider_name: str, model_code: str
    ) -> int:
        cfg = self._configs.get(provider_name)
        if not cfg:
            return 0
        for m in cfg.models:
            if m.get("code", "") == model_code:
                return m.get("max_context", 0)
        return 0

    async def close(self) -> None:
        self._configs.clear()


class _MemorySessionStore(SessionRepository):
    """Volatile session store used by the main app's dev experience."""

    async def create_session(
        self,
        session_id: str | None = None,
        agent_name: str = "",
        user_id: str = "",
        scenario_id: str | None = None,
        transient: bool = False,
        display_name_locale: str | None = None,
    ) -> Any:
        session = SimpleSession(
            session_id=session_id or "",
            agent_name=agent_name,
            user_id=user_id,
            scenario_id=scenario_id,
            display_name_locale=display_name_locale,
        )
        session.created_at = datetime.now(UTC).isoformat()
        return session

    async def get_session(self, session_id: str) -> Any | None:  # noqa: ARG002
        return None

    async def save_memory(
        self, memory: Any, session_id: str, extra: dict[str, Any] | None = None  # noqa: ARG002
    ) -> None:
        return None

    async def update_usage(self, memory: Any, session_id: str) -> None:  # noqa: ARG002
        return None

    async def delete_session(self, session_id: str) -> bool:  # noqa: ARG002
        return False

    async def list_sessions(self) -> list[Any]:
        return []

    async def list_user_sessions(  # noqa: ARG002
        self, user_id: str, scenario_id: str | None = None
    ) -> list[Any]:
        return []

    async def get_session_messages(self, session_id: str) -> list[dict]:  # noqa: ARG002
        return []

    def get_messages_as_items(self, session: Any) -> list[dict]:
        return []

    async def healthcheck(self) -> None:
        return None

    async def close(self) -> None:
        return None


class _NoopEvalStorage(EvalResultRepository):
    async def on_batch_started(self, batch_id: str, request: Any) -> None:  # noqa: ARG002
        return None

    async def on_question_started(self, batch_id: str, question: Any) -> None:  # noqa: ARG002
        return None

    async def on_llm_call_recorded(self, batch_id: str, record: Any) -> None:  # noqa: ARG002
        return None

    async def on_question_completed(self, batch_id: str, result: Any) -> None:  # noqa: ARG002
        return None

    async def on_batch_completed(self, batch_id: str, summary: Any) -> None:  # noqa: ARG002
        return None

    async def get_batch(self, batch_id: str) -> Any | None:  # noqa: ARG002
        return None

    async def list_batches(self) -> list[Any]:
        return []

    async def close(self) -> None:  # noqa: ARG002
        return None


def _default_adapter_lifespan():
    """Build the default in-memory :class:`AdapterLifespan`."""

    @asynccontextmanager
    async def _lifespan(app: Any) -> AsyncIterator[GatewayAdapters]:
        factory = ProviderFactory()
        register_builtin_providers(factory)
        llm_backend = _MemoryLLMConfigBackend()
        llm_service = DefaultLLMProviderService.from_components(
            factory=factory, backend=llm_backend
        )
        bundle = GatewayAdapters(
            settings=app.state.adapters_settings,
            user_auth=_RejectAuth(),
            authorization=_DenyAuthz(),
            m2m_auth=_NoM2M(),
            outbound_auth=_NoOpOutbound(),
            metadata=_EmptyMetadata(),
            llm=llm_service,
            sessions=_MemorySessionStore(),
            eval_results=None if not app.state.adapters_settings.enable_eval else _NoopEvalStorage(),
        )
        yield bundle

    return _lifespan


_config_mgr = ConfigManager()
_settings = asyncio.run(_config_mgr.resolve(ConfigSchema, prefix="ORCH"))
app = create_app(
    settings=_settings,
    adapters=_default_adapter_lifespan(),
)
