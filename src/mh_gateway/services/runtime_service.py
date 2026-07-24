from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from collections.abc import Awaitable, Sequence
from typing import Any, Callable
from urllib.parse import quote

from fastapi import Request
from mh_service_kit.sse.tool_executor import SSEToolExecutor
from minimal_harness.agent.middleware import Middleware
from minimal_harness.agent.registry import AgentRegistry
from minimal_harness.agent.runtime import AgentRuntime
from minimal_harness.llm.llm import LLMProvider
from minimal_harness.tool.factory import DefaultToolFactory
from minimal_harness.tool.registry import ToolRegistry
from minimal_harness.types import (
    AgentMetadata,
    CompactionSettings,
    ExternalScriptToolBinding,
    LLMStart,
    LocalToolBinding,
    RemoteToolBinding,
    ToolMetadata,
)

from mh_gateway.adapters import (
    LLMResolveSpec,
    OutboundAuthProvider,
    OutboundRequestContext,
    SessionRepository,
    match_permission,
)
from mh_gateway.api.locale import parse_locale_json
from mh_gateway.services.audit_middleware import AuditMiddleware
from mh_gateway.services.database import get_session_store
from mh_gateway.services.perm_middleware import PermissionMiddleware

logger = logging.getLogger("orchestration.runtime")


# ── Event / SSE serialization ─────────────────────────────────────────────────


def format_sse(event: str, data: dict[str, Any]) -> str:
    return (
        f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
    )


def serialize_harness_event(event: Any) -> dict[str, Any]:
    """Serialise a minimal-harness runtime event to a plain dict.

    The returned dict is the *body* of the SSE ``data:`` line emitted
    by the chat endpoint (see :func:`format_sse`).  The schema
    intentionally matches what the web-frontend's chat store reads
    on each event:

    * ``LLMChunk`` exposes ``content`` and ``reasoning`` at the top
      level (NOT nested under ``chunk``).
    * ``LLMEnd`` exposes ``content`` and ``reasoning_content`` at
      the top level.
    * ``LLMStart`` exposes the ``_compute_llm_start_info`` summary
      (tool names, message count, total chars).
    * ``AgentEnd`` / ``ToolStart`` / ``ToolEnd`` expose their
      business fields flat.

    The dict deliberately omits a ``"type"`` discriminator; the SSE
    ``event:`` line carries the type, and the frontend switches on
    that.  A previous attempt at this function wrapped every event
    in ``{"type": ..., ...}`` and nested LLMChunk fields under
    ``"chunk"``; the frontend then read ``undefined`` for
    ``data.content`` and the assistant message only rendered the
    reasoning block.  This implementation restores the original
    flat schema.
    """
    from minimal_harness.types import (
        AgentEnd,
        AgentStart,
        CompactionChunk,
        CompactionEnd,
        CompactionStart,
        ExecutionEnd,
        ExecutionStart,
        LLMChunk,
        LLMEnd,
        LLMStart,
        MemoryUpdate,
        MessageEvent,
        ToolEnd,
        ToolProgress,
        ToolResult,
        ToolStart,
    )

    def _serialize_chunk(chunk: Any) -> Any:
        if isinstance(chunk, dict):
            return {k: v for k, v in chunk.items() if not k.startswith("_")}
        return str(chunk)

    def _serialize_result(result: Any) -> Any:
        if isinstance(result, dict):
            return {k: v for k, v in result.items() if not k.startswith("_")}
        if isinstance(result, Exception):
            return f"[Error] {result}"
        if not isinstance(result, str):
            return str(result)
        return result

    if isinstance(event, AgentStart):
        return {}
    if isinstance(event, AgentEnd):
        return {
            "response": event.response,
            "time_taken": event.time_taken,
            "exceeded": event.exceeded,
            "interrupted": event.interrupted,
            "error": event.error,
        }
    if isinstance(event, LLMStart):
        return _compute_llm_start_info(event)
    if isinstance(event, LLMChunk):
        if event.chunk:
            return {
                "content": event.chunk.content,
                "reasoning": event.chunk.reasoning,
                "tool_calls": event.chunk.tool_calls,
            }
        return {}
    if isinstance(event, LLMEnd):
        return {
            "content": event.content,
            "reasoning_content": event.reasoning_content,
            "tool_calls": event.tool_calls,
            "usage": event.usage,
            "error": event.error,
        }
    if isinstance(event, ExecutionStart):
        return {"tool_calls": event.tool_calls}
    if isinstance(event, ExecutionEnd):
        return {
            "results": event.results,
            "error": event.error,
            "should_stop": event.should_stop,
            "response_text": event.response_text,
        }
    if isinstance(event, ToolStart):
        return {
            "tool_call": event.tool_call,
            "display_name": (
                event.tool_call.get("function", {}).get("name", "")
                if isinstance(event.tool_call, dict)
                else ""
            ),
        }
    if isinstance(event, ToolProgress):
        return {
            "tool_call": event.tool_call,
            "chunk": _serialize_chunk(event.chunk),
        }
    if isinstance(event, ToolEnd):
        # ``event.result`` is sometimes a ``ToolResult`` dataclass
        # (with .content / .meta / .stop), sometimes a raw value
        # (str / dict / Exception).  Flatten to the schema the
        # frontend reads.
        from minimal_harness.types import ToolResult

        if isinstance(event.result, ToolResult):
            return {
                "tool_call": event.tool_call,
                "result": _serialize_result(event.result.content),
                "meta": event.result.meta,
                "stop": event.result.stop,
            }
        return {
            "tool_call": event.tool_call,
            "result": _serialize_result(event.result),
        }
    if isinstance(event, MemoryUpdate):
        return {"usage": event.usage}
    if isinstance(event, MessageEvent):
        return {"message": event.message}
    if isinstance(event, CompactionStart):
        return {
            "dropped_message_count": event.dropped_message_count,
            "existing_summary": event.existing_summary,
            "keep_recent": event.keep_recent,
            "total_tokens": event.total_tokens,
        }
    if isinstance(event, CompactionChunk):
        return {
            "delta": event.delta,
            "accumulated": event.accumulated,
        }
    if isinstance(event, CompactionEnd):
        return {
            "summary": event.summary,
            "dropped_message_count": event.dropped_message_count,
            "new_offset": event.new_offset,
            "duration": event.duration,
            "error": event.error,
        }
    return {}


def _compute_llm_start_info(event: LLMStart) -> dict[str, Any]:
    total_chars = 0
    for msg in event.messages:
        role = msg.get("role", "")
        if role == "system":
            total_chars += len(msg.get("content", "") or "")
        elif role == "user":
            parts = msg.get("content", [])
            if isinstance(parts, list):
                for part in parts:
                    if isinstance(part, dict) and part.get("type") == "text":
                        total_chars += len(part.get("text", "") or "")
        elif role == "assistant":
            total_chars += len(msg.get("content", "") or "")
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                total_chars += len(json.dumps(tool_calls, ensure_ascii=False))
        elif role == "tool":
            total_chars += len(msg.get("content", "") or "")
        elif role == "reasoning":
            total_chars += len(msg.get("content", "") or "")
    return {
        "tool_names": [
            t.get("function", {}).get("name")
            if isinstance(t, dict)
            else getattr(t, "name", str(t))
            for t in event.tools
        ],
        "message_count": len(event.messages),
        "total_chars": total_chars,
    }


# ── Public helpers ───────────────────────────────────────────────────────────


async def resolve_model_max_context(
    metadata: Any,
    llm: Any,
    agent_name: str,
) -> int:
    """Look up the max_context for an agent's model via the LLM service."""
    if not llm or not metadata:
        return 0
    agent_dict = await metadata.get_agent(agent_name)
    if not agent_dict:
        return 0
    provider_ref = agent_dict.get("provider_name", "") or agent_dict.get("provider", "")
    model_code = agent_dict.get("model", "")
    if not provider_ref or not model_code:
        return 0
    return await llm.get_model_max_context(provider_ref, model_code)


class _SSEToolExecutorFactory:
    def create(self, binding: RemoteToolBinding) -> SSEToolExecutor:
        return SSEToolExecutor(binding)


# ── Per-session concurrency lock ──────────────────────────────────────────────


_SESSION_LOCKS: dict[str, asyncio.Lock] = {}
_SESSION_LOCKS_MUTEX = asyncio.Lock()


async def acquire_session_lock(session_id: str) -> asyncio.Lock:
    """Get (or create) and acquire a per-session lock.

    This serialises writes to the same ``session_id`` so that concurrent
    chat requests cannot race on ``save_memory``.
    """
    async with _SESSION_LOCKS_MUTEX:
        if session_id not in _SESSION_LOCKS:
            _SESSION_LOCKS[session_id] = asyncio.Lock()
        lock = _SESSION_LOCKS[session_id]
    await lock.acquire()
    return lock


async def release_session_lock(session_id: str, lock: asyncio.Lock) -> None:
    """Release a per-session lock and prune it from the dictionary."""
    lock.release()
    async with _SESSION_LOCKS_MUTEX:
        _SESSION_LOCKS.pop(session_id, None)


def _resolve_compaction_settings(agent_dict: dict) -> CompactionSettings | None:
    raw = agent_dict.get("compaction")
    if not raw or not isinstance(raw, dict):
        return None
    result: CompactionSettings = {}
    if "prompt_token_threshold" in raw:
        result["prompt_token_threshold"] = int(raw["prompt_token_threshold"])
    if "keep_recent" in raw:
        result["keep_recent"] = int(raw["keep_recent"])
    return result if result else None


def _make_extra_headers_provider(
    provider: OutboundAuthProvider,
    request: Request,
    target_url: str,
    target_type: str,
    identity: str,
    scenario_id: str,
    agent_name: str,
) -> Callable[[], Awaitable[dict[str, str]]]:
    """Return a closure that calls the provider at execution time."""

    async def _inner() -> dict[str, str]:
        return await provider.get_headers(
            OutboundRequestContext(
                request=request,
                target_url=target_url,
                target_type=target_type,
                identity=identity,
                scenario_id=scenario_id,
                agent_name=agent_name,
            )
        )

    return _inner


async def _tool_binding(
    meta: dict,
    name: str,
    request: Request | None = None,
    identity: str = "",
    outbound_auth: OutboundAuthProvider | None = None,
    scenario_id: str = "",
    agent_name: str = "",
    verify_agent_tool_ssl: bool = False,
) -> RemoteToolBinding | LocalToolBinding | ExternalScriptToolBinding:
    if "script_path" in meta and meta["script_path"]:
        return ExternalScriptToolBinding(script_path=meta["script_path"])
    if "_fn" in meta and meta["_fn"]:
        return LocalToolBinding(fn=meta["_fn"])
    if "endpoint_url" in meta and meta["endpoint_url"]:
        url = meta["endpoint_url"]
        if request and url.startswith("/"):
            url = str(request.base_url).rstrip("/") + url
        if scenario_id and name in ("discover_agents", "handoff"):
            url = f"{url}?scenario_id={scenario_id}"
        if name == "discover_agents" and agent_name:
            url = f"{url}{'&' if '?' in url else '?'}agent_name={quote(agent_name, safe='')}"
        extra_provider = None
        if outbound_auth is not None and request is not None:
            extra_provider = _make_extra_headers_provider(
                outbound_auth,
                request,
                url,
                "tool",
                identity=identity,
                scenario_id=scenario_id,
                agent_name=agent_name,
            )
        return RemoteToolBinding(
            url=url,
            headers={},
            extra_headers_provider=extra_provider,
            timeout=60.0,
            verify_ssl=verify_agent_tool_ssl,
        )
    raise ValueError(
        f"No valid binding for tool '{name}': missing script_path, _fn, or endpoint_url"
    )


def _apply_permission_filter(
    names: set[str],
    user_perms: list[str] | None,
    prefix: str,
) -> set[str]:
    """Filter *names* by matching each against *user_perms* with the given permission prefix."""
    if user_perms is None:
        return names
    return {n for n in names if match_permission(user_perms, f"{prefix}:{n}")}


async def _get_permitted_scenario_agents(
    metadata: Any,
    authorization: Any,
    scenario_id: str,
    user_id: str,
) -> set[str] | None:
    """Resolve scenario agents and intersect with user permissions.

    Returns ``None`` when *scenario_id* is empty (no filtering).
    Returns ``set[str]`` of agent names the user can access within the scenario.
    """
    if not scenario_id:
        return None

    scenario = await metadata.get_scenario(scenario_id)
    if scenario is None:
        return set()

    agent_names = {a["name"] for a in scenario.get("agents", [])}
    if authorization is not None:
        user_perms = await authorization.get_permissions(user_id)
        agent_names = _apply_permission_filter(agent_names, user_perms, "use:agent")
    return agent_names


async def create_runtime(
    request: Request,
    user_id: str,
    agent_name: str,
    tool_names: list[str],
    session_store: SessionRepository | None = None,
    session_id: str = "",
    scenario_id: str = "",
    trace_id: str = "",
    provider: str = "",
    model: str = "",
    emit_message_events: bool = True,
    extra_middleware: Sequence[Middleware] | None = None,
) -> tuple[AgentRuntime, AgentRegistry, ToolRegistry, SessionRepository]:
    adapters = request.app.state.adapters
    metadata = adapters.metadata
    authorization = adapters.authorization
    outbound_auth = adapters.outbound_auth
    llm_service = adapters.llm
    verify_agent_tool_ssl = getattr(adapters.settings, "verify_agent_tool_ssl", False)

    agent_registry = AgentRegistry()

    # ── Single permission fetch (one network call instead of N) ──
    user_perms: list[str] | None = None
    if authorization is not None:
        user_perms = await authorization.get_permissions(user_id)

    # ── Resolve scenario data (single lookup, not full scan) ──
    scenario_tool_names: dict[str, set[str]] = defaultdict(set)
    scenario_agent_names: set[str] | None = None

    if scenario_id:
        scenario_data = await metadata.get_scenario(scenario_id)
        if scenario_data is not None:
            scenario_agent_names = _apply_permission_filter(
                {a["name"] for a in scenario_data.get("agents", [])},
                user_perms,
                "use:agent",
            )
            for a in scenario_data.get("agents", []):
                scenario_tool_names[a["name"]].update(a.get("tool_names", []))
        else:
            scenario_agent_names = set()
    else:
        # No scenario filter: build agent→tool_names from all scenarios
        for s in await metadata.list_scenarios():
            for a in s.get("agents", []):
                scenario_tool_names[a["name"]].update(a.get("tool_names", []))

    # Register agents — filtered by scenario + permissions
    all_agents: list[AgentMetadata] = []
    for a in await metadata.list_agents():
        name = a["name"]
        if scenario_agent_names is not None:
            if name not in scenario_agent_names:
                continue
        elif user_perms is not None and not match_permission(
            user_perms, f"use:agent:{name}"
        ):
            continue
        provider_type = a.get("provider", "openai")
        agent_meta = AgentMetadata(
            name=a["name"],
            display_name=a.get("display_name", a["name"]),
            display_name_locale=parse_locale_json(a.get("display_name_locale")),
            description=a.get("description", ""),
            description_locale=parse_locale_json(a.get("description_locale")),
            system_prompt=a.get("system_prompt", ""),
            system_prompt_locale=parse_locale_json(a.get("system_prompt_locale")),
            metadata_id=a["name"],
            agent_type=a.get("agent_type", "simple"),
            tool_names=list(scenario_tool_names.get(a["name"], [])),
            provider=provider_type,
            model=a.get("model", ""),
            llm_config=a.get("llm_config", {}),
            compaction=_resolve_compaction_settings(a),
        )
        await agent_registry.register(agent_meta)
        all_agents.append(agent_meta)

    all_tool_names = set(tool_names)
    all_tool_names.update(scenario_tool_names.get(agent_name, set()))

    # Batch-fetch tool metadata (one call instead of N)
    tools_map = await metadata.get_tools(list(all_tool_names))

    tool_registry = ToolRegistry()
    for tname, tool_meta in tools_map.items():
        if tool_meta is None:
            continue
        params = tool_meta.get("parameters", {"type": "object", "properties": {}})
        await tool_registry.register(
            ToolMetadata(
                name=tool_meta["name"],
                display_name=tool_meta.get("display_name", tool_meta["name"]),
                display_name_locale=parse_locale_json(
                    tool_meta.get("display_name_locale")
                ),
                description=tool_meta.get("description", ""),
                description_locale=parse_locale_json(
                    tool_meta.get("description_locale")
                ),
                parameters=params,
                binding=await _tool_binding(
                    tool_meta,
                    tname,
                    request,
                    identity=user_id,
                    outbound_auth=outbound_auth,
                    scenario_id=scenario_id,
                    agent_name=agent_name,
                    verify_agent_tool_ssl=verify_agent_tool_ssl,
                ),
            )
        )

    if session_store is None:
        session_store = await get_session_store(request)

    resolved_provider = provider
    resolved_model = model
    if not resolved_provider or not resolved_model:
        target_agent_meta = await agent_registry.get(agent_name)
        if target_agent_meta:
            if not resolved_provider:
                resolved_provider = target_agent_meta.provider
            if not resolved_model:
                resolved_model = target_agent_meta.model

    middleware: list[Middleware] = [
        PermissionMiddleware(user_id, authorization),  # type: ignore[arg-type]
        AuditMiddleware(
            user_id=user_id,
            session_id=session_id,
            agent_id=agent_name,
            scenario_id=scenario_id,
            provider=resolved_provider,
            model=resolved_model,
            trace_id=trace_id,
        ),
    ]
    if extra_middleware:
        middleware.extend(extra_middleware)

    # Build a per-agent resolver that delegates credential resolution
    # to the unified LLM service.  ``build_resolver`` is the public
    # entry point; it pre-loads configs and returns a sync closure.
    specs = [LLMResolveSpec(agent=meta, user=user_id) for meta in all_agents]
    llm_provider_resolver: Callable[
        [AgentMetadata], LLMProvider
    ] = await llm_service.build_resolver(specs)

    runtime = AgentRuntime(
        agent_registry=agent_registry,
        session_store=session_store,
        tool_registry=tool_registry,
        middleware=middleware,
        llm_provider_resolver=llm_provider_resolver,
        tool_factory=DefaultToolFactory(
            executor_factories={"default": _SSEToolExecutorFactory()},
        ),
        emit_message_events=emit_message_events,
        default_compaction_settings=CompactionSettings(
            prompt_token_threshold=8000,
            keep_recent=6,
        ),
    )

    return runtime, agent_registry, tool_registry, session_store
