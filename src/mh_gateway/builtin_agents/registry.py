from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

from mh_gateway.context import get_current_request, get_current_user_id
from minimal_harness.types import ToolResult

# Throttle for forwarding the sub-agent's streamed LLM content as
# ``llm_generating`` chunks: emit at most one chunk per N chars so long
# responses stay bounded (the frontend collapses consecutive chunks).
LLM_STREAM_INTERVAL = 200
# Cap on a single forwarded tool result / agent response inside a chunk.
TOOL_RESULT_LIMIT = 2000


def _short_result(result: Any, limit: int = TOOL_RESULT_LIMIT) -> str:
    """Flatten a sub-agent tool result to a bounded string for display."""
    if isinstance(result, ToolResult):
        result = result.content
    if isinstance(result, Exception):
        return f"[Error] {result}"
    if isinstance(result, str):
        return result[:limit]
    return json.dumps(result, ensure_ascii=False, default=str)[:limit]


def _tool_call_name(tool_call: Any) -> str:
    if not isinstance(tool_call, dict):
        return ""
    fn = tool_call.get("function")
    return fn.get("name", "") if isinstance(fn, dict) else ""


def _tool_call_args(tool_call: Any) -> str:
    if not isinstance(tool_call, dict):
        return ""
    fn = tool_call.get("function")
    return fn.get("arguments", "") if isinstance(fn, dict) else ""


def _tool_call_id(tool_call: Any) -> str:
    """Stable per-call id so the frontend can group start/progress/end of the
    same tool invocation — even when multiple tools run in parallel and their
    events interleave."""
    if not isinstance(tool_call, dict):
        return ""
    return str(tool_call.get("id", "") or "")


async def _discover_agents_fn(
    exclude: str = "", locale: str = "", scenario_id: str = ""
) -> AsyncIterator[Any]:
    from mh_gateway.api.locale import (
        resolve_description,
        resolve_display_name,
    )
    from mh_gateway.adapters import match_permission

    request = get_current_request()
    if request is None:
        yield {"status": "ok", "agents": []}
        return
    adapters = request.app.state.adapters
    identity = get_current_user_id() or ""

    agents = await adapters.metadata.list_agents()
    user_perms: list[str] | None = None
    if adapters.authorization is not None:
        user_perms = await adapters.authorization.get_permissions(identity)

    # 当前场景下的 agent 集合（再按用户权限过滤）；``scenario_id`` 为空时
    # 只按权限过滤 —— 与 ``runtime_tools`` 的 HTTP 端点行为保持一致。
    scenario_agent_names: set[str] | None = None
    if scenario_id:
        scenario_data = await adapters.metadata.get_scenario(scenario_id)
        if scenario_data is not None:
            scenario_agent_names = {a["name"] for a in scenario_data.get("agents", [])}
            if user_perms is not None:
                scenario_agent_names = {
                    n
                    for n in scenario_agent_names
                    if match_permission(user_perms, f"use:agent:{n}")
                }
        else:
            scenario_agent_names = set()

    result = []
    for a in agents:
        name = a["name"]
        if exclude and name == exclude:
            continue
        if scenario_agent_names is not None:
            if name not in scenario_agent_names:
                continue
        elif user_perms is not None and not match_permission(
            user_perms, f"use:agent:{name}"
        ):
            continue
        result.append(
            {
                "name": a["name"],
                "display_name": resolve_display_name(
                    a.get("display_name", a["name"]),
                    a.get("display_name_locale"),
                    locale,
                ),
                "description": resolve_description(
                    a.get("description", ""),
                    a.get("description_locale"),
                    locale,
                ),
            }
        )
    yield {"status": "ok", "agents": result}


async def _handoff_fn(
    target_agent_name: str = "",
    context_summary: str = "",
    task_description: str = "",
    locale: str = "",
) -> AsyncIterator[Any]:
    if not target_agent_name:
        yield {"status": "error", "message": "target_agent_name is required"}
        return

    from mh_gateway.services.database import get_session_store
    from mh_gateway.services.runtime_service import (
        acquire_session_lock,
        create_runtime,
        release_session_lock,
    )

    request = get_current_request()
    if request is None:
        yield {"status": "error", "message": "No request context"}
        return

    identity = get_current_user_id() or ""
    store = await get_session_store(request)

    import uuid

    handoff_session_id = f"mem_{uuid.uuid4().hex[:12]}"
    await store.create_session(
        session_id=handoff_session_id,
        agent_name=target_agent_name,
        user_id=identity,
        transient=True,
    )

    lock = await acquire_session_lock(handoff_session_id)
    sub_task = None
    sub_stop_event = None
    result_text = ""
    llm_buf: list[str] = []
    llm_emitted = 0
    reasoning_buf: list[str] = []
    reasoning_emitted = 0
    try:
        runtime, _agent_registry, _tool_registry, _ = await create_runtime(
            request=request,
            user_id=identity,
            agent_name=target_agent_name,
            tool_names=[],
            session_store=store,
            session_id=handoff_session_id,
        )

        combined = f"Context: {context_summary}\n\nTask: {task_description}"

        sub_task, sub_stop_event, queue = await runtime.run(
            user_input=[{"type": "text", "text": combined}],
            agent_metadata_id=target_agent_name,
            memory_id=handoff_session_id,
            context={"locale": locale, "agent_name": target_agent_name},
        )

        yield {
            "status": "progress",
            "type": "handoff_started",
            "message": f"Starting delegated task to {target_agent_name}...",
            "target_agent": target_agent_name,
            "task": task_description,
        }

        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                if sub_stop_event and sub_stop_event.is_set():
                    yield {"status": "error", "type": "interrupted"}
                    break
                continue

            if event is None:
                break

            from minimal_harness.types import (
                AgentEnd,
                AgentStart,
                ExecutionEnd,
                ExecutionStart,
                LLMChunk,
                LLMEnd,
                LLMStart,
                ToolEnd,
                ToolProgress,
                ToolStart,
            )

            if isinstance(event, AgentEnd):
                result_text = event.response or result_text
                yield {
                    "status": "progress",
                    "type": "agent_end",
                    "message": (event.response or "")[:TOOL_RESULT_LIMIT],
                    "time_taken": event.time_taken,
                    "exceeded": event.exceeded,
                    "interrupted": event.interrupted,
                    "error": event.error,
                }
            elif isinstance(event, AgentStart):
                yield {"status": "progress", "type": "agent_start"}
            elif isinstance(event, LLMStart):
                llm_buf = []
                llm_emitted = 0
                reasoning_buf = []
                reasoning_emitted = 0
                yield {
                    "status": "progress",
                    "type": "llm_start",
                    "tool_count": len(event.tools) if event.tools else 0,
                    "message_count": len(event.messages) if event.messages else 0,
                }
            elif isinstance(event, LLMChunk):
                content = event.chunk.content if event.chunk else None
                reasoning = event.chunk.reasoning if event.chunk else None
                if content:
                    llm_buf.append(content)
                if reasoning:
                    reasoning_buf.append(reasoning)
                if (
                    len(llm_buf) - llm_emitted >= LLM_STREAM_INTERVAL
                    or len(reasoning_buf) - reasoning_emitted >= LLM_STREAM_INTERVAL
                ):
                    yield {
                        "status": "progress",
                        "type": "llm_generating",
                        "content": "".join(llm_buf),
                        "reasoning": "".join(reasoning_buf),
                        "char_count": len(llm_buf),
                    }
                    llm_emitted = len(llm_buf)
                    reasoning_emitted = len(reasoning_buf)
            elif isinstance(event, LLMEnd):
                if (llm_buf and len(llm_buf) > llm_emitted) or (
                    reasoning_buf and len(reasoning_buf) > reasoning_emitted
                ):
                    yield {
                        "status": "progress",
                        "type": "llm_generating",
                        "content": "".join(llm_buf),
                        "reasoning": "".join(reasoning_buf),
                        "char_count": len(llm_buf),
                    }
                llm_buf = []
                llm_emitted = 0
                reasoning_buf = []
                reasoning_emitted = 0
                yield {
                    "status": "progress",
                    "type": "llm_end",
                    "tool_calls": [
                        tc["function"].get("name", "")
                        for tc in (event.tool_calls or [])
                        if isinstance(tc, dict) and isinstance(tc.get("function"), dict)
                    ],
                    "usage": event.usage,
                }
            elif isinstance(event, ExecutionStart):
                yield {
                    "status": "progress",
                    "type": "execution_start",
                    "tools": [
                        {
                            "name": tc["function"].get("name", ""),
                            "args": tc["function"].get("arguments", ""),
                        }
                        for tc in (event.tool_calls or [])
                        if isinstance(tc, dict) and isinstance(tc.get("function"), dict)
                    ],
                }
            elif isinstance(event, ToolStart):
                yield {
                    "status": "progress",
                    "type": "tool_start",
                    "tool_name": _tool_call_name(event.tool_call),
                    "tool_args": _tool_call_args(event.tool_call),
                    "tool_call_id": _tool_call_id(event.tool_call),
                }
            elif isinstance(event, ToolProgress):
                yield {
                    "status": "progress",
                    "type": "tool_progress",
                    "tool_call": event.tool_call,
                    "chunk": event.chunk,
                }
            elif isinstance(event, ToolEnd):
                is_error = (
                    isinstance(event.result, Exception)
                    or (
                        isinstance(event.result, dict)
                        and event.result.get("status") == "error"
                    )
                    or (
                        isinstance(event.result, str)
                        and event.result.startswith("[Error]")
                    )
                )
                yield {
                    "status": "progress",
                    "type": "tool_end",
                    "tool_name": _tool_call_name(event.tool_call),
                    "tool_result": _short_result(event.result),
                    "is_error": is_error,
                    "tool_call_id": _tool_call_id(event.tool_call),
                }
            elif isinstance(event, ExecutionEnd):
                yield {
                    "status": "progress",
                    "type": "execution_end",
                    "results": [
                        {"name": _tool_call_name(tc), "result": _short_result(r)}
                        for tc, r in (event.results or [])
                    ],
                }

        if result_text:
            yield {
                "status": "handoff_complete",
                "type": "handoff_complete",
                "message": "Delegated task completed",
                "result": result_text,
                "target_agent": target_agent_name,
            }
        else:
            yield {
                "status": "handoff_complete",
                "type": "handoff_complete",
                "message": "Delegated task completed",
                "target_agent": target_agent_name,
            }
    finally:
        if sub_stop_event is not None:
            sub_stop_event.set()
        if sub_task is not None:
            sub_task.cancel()
            try:
                await sub_task
            except (asyncio.CancelledError, Exception):
                pass
        await release_session_lock(handoff_session_id, lock)
