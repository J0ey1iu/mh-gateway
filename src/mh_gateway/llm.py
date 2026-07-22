"""LLM configuration DTO and a default :class:`LLMProviderService`.

The :class:`DefaultLLMProviderService` bundles three collaborators:

* A driver registry — knows how to build an ``LLMProvider`` for each
  provider type (e.g. ``"openai"``, ``"anthropic"``).
* A config backend — persists the deployment's provider configurations
  (api_key, base_url, default_model, models, ...).
* A header resolver — supplies per-call dynamic HTTP headers.

The two convenience entry points are :meth:`create_llm` (single
instance) and :meth:`build_resolver` (a pre-loaded synchronous
resolver suitable for ``minimal_harness``'s ``AgentRuntime``).
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Protocol, runtime_checkable

from minimal_harness.llm.llm import LLMProvider, ProviderFactory

from mh_gateway.adapters import LLMResolveSpec

if TYPE_CHECKING:  # pragma: no cover
    from minimal_harness.types import AgentMetadata


# ── Config DTO ────────────────────────────────────────────────────────────────


@dataclass
class LLMProviderConfig:
    """Persistent configuration for a single LLM provider entry.

    The schema is intentionally flat: the management REST API and the
    default config backend (orch-app, mh-local) all share the same
    field names.
    """

    name: str
    provider_type: str
    api_key: str = ""
    base_url: str = ""
    default_model: str = ""
    description: str = ""
    models: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    created_by: str = ""
    updated_by: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LLMProviderConfig":
        return cls(
            name=data.get("name", ""),
            provider_type=data.get("provider_type", "openai"),
            api_key=data.get("api_key", ""),
            base_url=data.get("base_url", ""),
            default_model=data.get("default_model", ""),
            description=data.get("description", ""),
            models=list(data.get("models", [])),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            created_by=data.get("created_by", ""),
            updated_by=data.get("updated_by", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Backend Protocols (used by the service) ───────────────────────────────────


@runtime_checkable
class LLMConfigBackend(Protocol):
    """Persistence layer for :class:`LLMProviderConfig`."""

    async def list(self) -> list[LLMProviderConfig]: ...
    async def get(self, name: str) -> LLMProviderConfig | None: ...
    async def create(self, config: LLMProviderConfig) -> LLMProviderConfig: ...
    async def update(
        self, name: str, config: LLMProviderConfig
    ) -> LLMProviderConfig: ...
    async def delete(self, name: str) -> None: ...
    async def get_model_max_context(
        self, provider_name: str, model_code: str
    ) -> int: ...
    async def close(self) -> None: ...


@runtime_checkable
class LLMHeaderResolver(Protocol):
    """Supplies per-call HTTP headers for outbound LLM requests.

    The runtime calls ``headers()`` at every LLM request, so
    implementations can rotate tokens or inject trace ids.  The
    returned dictionary is merged into the LLM client's per-request
    headers.
    """

    async def headers(self) -> dict[str, str]:
        """Return headers to attach to the next LLM call."""
        ...


# ── Default Service ───────────────────────────────────────────────────────────


class DefaultLLMProviderService:
    """Default :class:`~mh_gateway.adapters.LLMProviderService` implementation.

    Construct via :meth:`from_components` when the deployment supplies
    its own backend or header resolver, or via :meth:`in_memory` for
    tests and the no-credential experience.

    The :meth:`create_llm` method:

    1. Resolves the provider config (by ``provider_name`` reference or
       the inline ``provider`` field on the agent).
    2. Reads the header resolver result.
    3. Delegates to the :class:`ProviderFactory` driver.

    The :meth:`build_resolver` method pre-loads configs for every
    spec so that the returned closure is synchronous — matching the
    contract expected by ``minimal_harness.agent.runtime.AgentRuntime``.
    """

    def __init__(
        self,
        factory: ProviderFactory,
        backend: LLMConfigBackend,
        header_resolver: LLMHeaderResolver | None = None,
    ) -> None:
        self._factory = factory
        self._backend = backend
        self._header_resolver = header_resolver

    # ── Factory helpers ──────────────────────────────────────────────────────

    @classmethod
    def from_components(
        cls,
        factory: ProviderFactory,
        backend: LLMConfigBackend,
        header_resolver: LLMHeaderResolver | None = None,
    ) -> "DefaultLLMProviderService":
        return cls(factory, backend, header_resolver)

    @classmethod
    def in_memory(
        cls,
        backend: LLMConfigBackend,
        header_resolver: LLMHeaderResolver | None = None,
    ) -> "DefaultLLMProviderService":
        from minimal_harness.llm.factory import register_builtin_providers

        factory = ProviderFactory()
        register_builtin_providers(factory)
        return cls(factory, backend, header_resolver)

    # ── LLM creation ─────────────────────────────────────────────────────────

    def list_provider_types(self) -> list[str]:
        return self._factory.list_providers()

    def create_llm(self, spec: LMMAccessor) -> LLMProvider:
        """Build a single LLM instance for *spec*.

        Performs async I/O (config fetch, header resolver) by reaching
        into the running event loop.  If called from a sync context
        without a running loop, raises ``RuntimeError``.
        """
        return _create_llm_sync(
            spec=spec,
            factory=self._factory,
            backend=self._backend,
            header_resolver=self._header_resolver,
        )

    def build_resolver(
        self, specs: list[LLMResolveSpec]
    ) -> Callable[["AgentMetadata"], LLMProvider]:
        """Build a pre-loaded sync resolver for *specs*.

        Loads every referenced config in parallel before the agent
        runtime begins; the returned closure is fully synchronous.
        """
        return _build_resolver_sync(
            specs=specs,
            factory=self._factory,
            backend=self._backend,
            header_resolver=self._header_resolver,
        )

    # ── Config backend (thin pass-through) ───────────────────────────────────

    async def list_configs(self) -> list[LLMProviderConfig]:
        return await self._backend.list()

    async def get_config(self, name: str) -> LLMProviderConfig | None:
        return await self._backend.get(name)

    async def create_config(self, config: LLMProviderConfig) -> LLMProviderConfig:
        return await self._backend.create(config)

    async def update_config(
        self, name: str, config: LLMProviderConfig
    ) -> LLMProviderConfig:
        return await self._backend.update(name, config)

    async def delete_config(self, name: str) -> None:
        await self._backend.delete(name)

    async def get_model_max_context(
        self, provider_name: str, model_code: str
    ) -> int:
        return await self._backend.get_model_max_context(provider_name, model_code)

    async def close(self) -> None:
        await self._backend.close()


# Alias used in the public method signature above.  Defined here so
# the source order is friendlier to readers.
LMMAccessor = LLMResolveSpec


# ── Sync helpers ──────────────────────────────────────────────────────────────


def _resolve_config_sync(
    backend: LLMConfigBackend,
    provider_ref: str,
) -> LLMProviderConfig | None:
    return _run_async(backend.get(provider_ref))


def _resolve_headers_sync(
    header_resolver: LLMHeaderResolver | None,
) -> dict[str, str]:
    if header_resolver is None:
        return {}
    return _run_async(header_resolver.headers())


def _create_llm_sync(
    *,
    spec: LLMResolveSpec,
    factory: ProviderFactory,
    backend: LLMConfigBackend,
    header_resolver: LLMHeaderResolver | None,
) -> LLMProvider:
    agent = spec.agent
    provider_type = getattr(agent, "provider", "openai") or "openai"
    provider_ref = (
        getattr(agent, "provider_name", "") or provider_type
    )
    model = getattr(agent, "model", "") or ""

    cfg: dict[str, Any] = {"model": model}
    if header_resolver is not None:
        cfg["_extra_headers_provider"] = header_resolver

    llm_config = getattr(agent, "llm_config", None) or {}
    if isinstance(llm_config, dict):
        cfg.update(llm_config)

    if provider_ref:
        entity = _resolve_config_sync(backend, provider_ref)
        if entity is not None:
            provider_type = entity.provider_type or provider_type
            if not model and entity.default_model:
                cfg["model"] = entity.default_model
                model = entity.default_model
            if entity.api_key:
                cfg["api_key"] = entity.api_key
            if entity.base_url:
                cfg["base_url"] = entity.base_url

    return factory.create(provider_type, cfg)


def _build_resolver_sync(
    *,
    specs: list[LLMResolveSpec],
    factory: ProviderFactory,
    backend: LLMConfigBackend,
    header_resolver: LLMHeaderResolver | None,
) -> Callable[["AgentMetadata"], LLMProvider]:
    """Pre-load configs and return a sync resolver closure.

    ``AgentMetadata`` objects are matched to their pre-loaded config
    by ``metadata_id``; specs without a corresponding agent lookup
    fall back to the inline agent fields.
    """
    by_id: dict[str, LLMProviderConfig | None] = {}
    if specs:
        by_id = dict(
            zip(
                [s.agent.metadata_id for s in specs],
                _run_async(_gather_configs(specs, backend)),
            )
        )

    def _resolver(agent: "AgentMetadata") -> LLMProvider:
        provider_type = getattr(agent, "provider", "openai") or "openai"
        model = getattr(agent, "model", "") or ""

        cfg: dict[str, Any] = {"model": model}
        if header_resolver is not None:
            cfg["_extra_headers_provider"] = header_resolver
        llm_config = getattr(agent, "llm_config", None) or {}
        if isinstance(llm_config, dict):
            cfg.update(llm_config)

        entity = by_id.get(getattr(agent, "metadata_id", "") or "")
        if entity is not None:
            provider_type = entity.provider_type or provider_type
            if not model and entity.default_model:
                cfg["model"] = entity.default_model
            if entity.api_key:
                cfg["api_key"] = entity.api_key
            if entity.base_url:
                cfg["base_url"] = entity.base_url

        return factory.create(provider_type, cfg)

    return _resolver


async def _gather_configs(
    specs: list[LLMResolveSpec],
    backend: LLMConfigBackend,
) -> list[LLMProviderConfig | None]:
    coros: list[Any] = []
    for s in specs:
        agent = s.agent
        provider_ref = (
            getattr(agent, "provider_name", "")
            or getattr(agent, "provider", "")
        )
        if not provider_ref:
            coros.append(_empty_config())
        else:
            coros.append(backend.get(provider_ref))
    return await asyncio.gather(*coros)


async def _empty_config() -> None:
    return None


def _run_async(coro: Any) -> Any:
    """Run a coroutine in the currently running event loop.

    Mirrors ``asyncio.run`` semantics for use inside sync helper
    methods that must be called from a thread that already has a
    loop (which is always the case inside the agent runtime).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError as e:
        raise RuntimeError(
            "DefaultLLMProviderService sync methods must be called from "
            "an async context. Await llm.list_configs/etc. instead."
        ) from e
    return coro
