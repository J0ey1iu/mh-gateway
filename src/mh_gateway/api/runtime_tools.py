from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse
from minimal_harness.types import (
    AgentEnd,
    AgentStart,
    ExecutionEnd,
    ExecutionStart,
    LLMChunk,
    LLMEnd,
    LLMStart,
    ToolEnd,
    ToolStart,
)

from mh_gateway.api.dependencies import resolve_m2m_identity
from mh_gateway.api.locale import (
    parse_locale,
    resolve_description,
    resolve_display_name,
)
from mh_gateway.adapters import match_permission
from mh_gateway.services.database import get_session_store
from mh_gateway.services.runtime_service import (
    _apply_permission_filter,
    acquire_session_lock,
    create_runtime,
    release_session_lock,
)

logger = logging.getLogger("orchestration.runtime_tools")

router = APIRouter(prefix="/api/v1/tools", tags=["runtime_tools"])


def _sse_line(event_type: str, data: Any) -> str:
    return f"data: {json.dumps({'type': event_type, 'data': data}, ensure_ascii=False, default=str)}\n\n"


@router.post("/discover_agents/execute")
async def discover_agents_execute(
    request: Request,
    body: dict[str, Any],
    accept_language: str | None = Header(None, alias="Accept-Language"),
    user_id: str = Depends(resolve_m2m_identity),
):
    args = body.get("args", {})
    locale = args.get("locale") or parse_locale(accept_language)
    exclude = args.get("exclude")
    scenario_id = request.query_params.get("scenario_id", "")
    caller_agent_name = request.query_params.get("agent_name", "")

    async def event_stream():
        try:
            adapters = request.app.state.adapters

            user_perms: list[str] | None = None
            if adapters.permission_checker:
                user_perms = await adapters.permission_checker.get_permissions(user_id)

            scenario_agent_names: set[str] | None = None
            if scenario_id:
                scenario_data = await adapters.management_provider.get_scenario(
                    scenario_id
                )
                if scenario_data is not None:
                    scenario_agent_names = _apply_permission_filter(
                        {a["name"] for a in scenario_data.get("agents", [])},
                        user_perms,
                        "use:agent",
                    )
                else:
                    scenario_agent_names = set()

            agents = await adapters.management_provider.list_agents()
            result = []
            for a in agents:
                name = a["name"]
                if caller_agent_name and name == caller_agent_name:
                    continue
                if exclude and name == exclude:
                    continue
                if (
                    scenario_agent_names is not None
                    and name not in scenario_agent_names
                ):
                    continue
                if (
                    scenario_agent_names is None
                    and user_perms is not None
                    and not match_permission(user_perms, f"use:agent:{name}")
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
            yield _sse_line("tool_end", {"status": "ok", "agents": result})
        except Exception:
            logger.exception("Discover agents execution error")
            try:
                yield _sse_line("error", {"message": "Internal server error"})
            except Exception:
                pass

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/handoff/execute")
async def handoff_execute(
    request: Request,
    body: dict[str, Any],
    accept_language: str | None = Header(None, alias="Accept-Language"),
    user_id: str = Depends(resolve_m2m_identity),
):
    args = body.get("args", {})
    target_agent_name = args.get("target_agent_name", "")
    context_summary = args.get("context_summary", "")
    task_description = args.get("task_description", "")
    locale = args.get("locale") or parse_locale(accept_language)

    if not target_agent_name:

        async def _error_stream():
            yield _sse_line("error", {"message": "target_agent_name is required"})

        return StreamingResponse(_error_stream(), media_type="text/event-stream")

    scenario_id = request.query_params.get("scenario_id", "")

    async def event_stream():
        try:
            adapters = request.app.state.adapters

            agent_meta = await adapters.management_provider.get_agent(target_agent_name)
            if agent_meta is None:
                yield _sse_line(
                    "error",
                    {"message": f"Handoff target '{target_agent_name}' not found"},
                )
                return

            combined = f"Context: {context_summary}\n\nTask: {task_description}"

            handoff_session_id = f"mem_{uuid.uuid4().hex[:12]}"
            store = await get_session_store()
            await store.create_session(
                session_id=handoff_session_id,
                agent_name=target_agent_name,
                user_id=user_id,
                scenario_id=scenario_id,
                display_name_locale=agent_meta.get("display_name_locale"),
                transient=True,
            )

            lock = await acquire_session_lock(handoff_session_id)
            sub_task = None
            sub_stop_event = None
            result_text = ""
            llm_content = ""
            last_chunk_send = 0.0
            last_sent_len = 0
            CHUNK_INTERVAL = 0.5
            CHUNK_SIZE_THRESHOLD = 100

            try:
                runtime, _agent_registry, _tool_registry, _ = await create_runtime(
                    request=request,
                    user_id=user_id,
                    agent_name=target_agent_name,
                    tool_names=[],
                    session_store=store,
                    session_id=handoff_session_id,
                    scenario_id=scenario_id,
                )

                sub_task, sub_stop_event, queue = await runtime.run(
                    user_input=[{"type": "text", "text": combined}],
                    agent_metadata_id=target_agent_name,
                    memory_id=handoff_session_id,
                    context={"locale": locale, "agent_name": target_agent_name},
                )

                yield _sse_line(
                    "tool_progress",
                    {
                        "status": "handoff_started",
                        "type": "handoff_started",
                        "message": f"Starting delegated task to {target_agent_name}...",
                        "target_agent": target_agent_name,
                        "task": task_description,
                        "context": context_summary,
                    },
                )

                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=0.5)
                    except asyncio.TimeoutError:
                        if sub_stop_event.is_set():
                            yield _sse_line(
                                "tool_progress",
                                {
                                    "status": "error",
                                    "type": "interrupted",
                                    "message": "Delegated task was interrupted",
                                },
                            )
                            break
                        continue

                    if event is None:
                        break

                    if isinstance(event, AgentStart):
                        yield _sse_line(
                            "tool_progress",
                            {
                                "status": "progress",
                                "type": "agent_start",
                                "message": "Agent started processing the delegated task",
                            },
                        )
                        llm_content = ""
                        last_chunk_send = 0.0
                        last_sent_len = 0

                    elif isinstance(event, LLMChunk):
                        if event.chunk:
                            delta = event.chunk.content or ""
                            if delta:
                                llm_content += delta
                            now = time.monotonic()
                            if (
                                len(llm_content) - last_sent_len >= CHUNK_SIZE_THRESHOLD
                                or now - last_chunk_send >= CHUNK_INTERVAL
                            ):
                                yield _sse_line(
                                    "tool_progress",
                                    {
                                        "status": "progress",
                                        "type": "llm_generating",
                                        "message": "Generating...",
                                        "content": llm_content,
                                        "char_count": len(llm_content),
                                    },
                                )
                                last_chunk_send = now
                                last_sent_len = len(llm_content)

                    elif isinstance(event, LLMStart):
                        llm_content = ""
                        last_chunk_send = 0.0
                        last_sent_len = 0
                        yield _sse_line(
                            "tool_progress",
                            {
                                "status": "progress",
                                "type": "llm_start",
                                "message": "LLM generating...",
                                "tool_count": (
                                    len(event.tools)
                                    if isinstance(event.tools, list)
                                    else 0
                                ),
                                "message_count": (
                                    len(event.messages)
                                    if isinstance(event.messages, list)
                                    else 0
                                ),
                            },
                        )
                    elif isinstance(event, LLMEnd):
                        if event.content:
                            result_text = str(event.content)
                        msg = (event.content or "LLM response generated")[:200]
                        if event.error:
                            msg = f"[Error] {event.error}: {msg}"
                        tool_call_names = (
                            [tc["function"]["name"] for tc in event.tool_calls]
                            if event.tool_calls
                            else []
                        )
                        yield _sse_line(
                            "tool_progress",
                            {
                                "status": "progress",
                                "type": "llm_end",
                                "message": msg,
                                "reasoning": event.reasoning_content,
                                "tool_calls": tool_call_names,
                                "usage": event.usage,
                                "error": event.error,
                            },
                        )
                    elif isinstance(event, ExecutionStart):
                        names = ", ".join(
                            tc["function"]["name"] for tc in event.tool_calls
                        )
                        yield _sse_line(
                            "tool_progress",
                            {
                                "status": "progress",
                                "type": "execution_start",
                                "message": f"Executing: {names}",
                                "tools": [
                                    {
                                        "name": tc["function"]["name"],
                                        "args": tc["function"]["arguments"],
                                    }
                                    for tc in event.tool_calls
                                ],
                            },
                        )
                    elif isinstance(event, ExecutionEnd):
                        parts = []
                        result_details = []
                        for tc, result in event.results:
                            name = tc["function"]["name"]
                            r = (str(result) if result is not None else "")[:500]
                            parts.append(f"{name} => {r}")
                            result_details.append({"name": name, "result": r})
                        msg = " | ".join(parts) if parts else "Tool execution complete"
                        if event.error:
                            msg = f"[Error] {event.error}: {msg}"
                        yield _sse_line(
                            "tool_progress",
                            {
                                "status": "progress",
                                "type": "execution_end",
                                "message": msg,
                                "results": result_details,
                                "error": event.error,
                            },
                        )
                    elif isinstance(event, ToolStart):
                        name = event.tool_call["function"]["name"]
                        yield _sse_line(
                            "tool_progress",
                            {
                                "status": "progress",
                                "type": "tool_start",
                                "message": f"Tool started: {name}",
                                "tool_name": name,
                                "tool_args": event.tool_call["function"]["arguments"],
                            },
                        )
                    elif isinstance(event, ToolEnd):
                        name = event.tool_call["function"]["name"]
                        result_str = (
                            str(event.result) if event.result is not None else ""
                        )[:500]
                        is_error = isinstance(event.result, Exception) or (
                            isinstance(event.result, str)
                            and event.result.startswith("[Error]")
                        )
                        yield _sse_line(
                            "tool_progress",
                            {
                                "status": "progress",
                                "type": "tool_end",
                                "message": (
                                    f"Tool {name} failed: {result_str}"
                                    if is_error
                                    else f"Tool {name} completed: {result_str}"
                                ),
                                "tool_name": name,
                                "tool_result": result_str,
                                "is_error": is_error,
                            },
                        )
                    elif isinstance(event, AgentEnd):
                        result_text = event.response or result_text
                        yield _sse_line(
                            "tool_progress",
                            {
                                "status": "progress",
                                "type": "agent_end",
                                "message": (event.response or "Agent completed")[:200],
                                "time_taken": event.time_taken,
                                "exceeded": event.exceeded,
                                "interrupted": event.interrupted,
                                "error": event.error,
                            },
                        )

                session = await store.get_session(handoff_session_id)
                if session:
                    title = (
                        task_description[:80]
                        if task_description
                        else f"Handoff to {target_agent_name}"
                    )
                    await store.save_memory(
                        session.memory,
                        handoff_session_id,
                        extra={"title": title},
                    )

                yield _sse_line(
                    "tool_end",
                    {
                        "status": "handoff_complete",
                        "type": "handoff_complete",
                        "message": "Delegated task completed",
                        "result": result_text,
                        "target_agent": target_agent_name,
                    },
                )

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

        except Exception:
            logger.exception("Handoff execution error")
            try:
                yield _sse_line(
                    "error", {"message": "An internal error occurred during handoff"}
                )
            except Exception:
                pass

    return StreamingResponse(event_stream(), media_type="text/event-stream")
