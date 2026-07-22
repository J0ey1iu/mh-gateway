from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from minimal_harness.tool.registry import ToolRegistry
from minimal_harness.types import MemoryUpdate, MessageEvent, ToolStart
from pydantic import BaseModel

from mh_gateway.api.dependencies import (
    resolve_request_identity,
    resolve_request_permissions,
)
from mh_gateway.api.locale import parse_locale
from mh_gateway.adapters import SessionRepository, match_permission
from mh_gateway.context import get_current_trace_id
from mh_gateway.services.database import get_session_store
from mh_gateway.services.runtime_service import (
    acquire_session_lock,
    create_runtime,
    format_sse,
    release_session_lock,
    serialize_harness_event,
)

logger = logging.getLogger("orchestration.chat")

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])


class ChatRequest(BaseModel):
    message: str


async def _resolve_tool_display_name(
    func_name: str, locale: str, tool_registry: ToolRegistry | None
) -> str:
    if not locale or not func_name or not tool_registry:
        return func_name
    tool_meta = await tool_registry.get(func_name)
    if tool_meta:
        return tool_meta.resolve_display_name(locale)
    return func_name


async def _get_scenario_for_session(
    request: Request,
    session,
) -> dict[str, Any] | None:
    scenario_id = session.scenario_id
    if not scenario_id:
        return None
    from mh_gateway.api.scenarios import _get_scenario

    return await _get_scenario(request, scenario_id)


@router.post("/{memory_id}")
async def chat(
    request: Request,
    memory_id: str,
    body: ChatRequest,
    accept_language: str | None = Header(None, alias="Accept-Language"),
    user_id: str = Depends(resolve_request_identity),
    user_perms: list[str] = Depends(resolve_request_permissions),
) -> StreamingResponse:
    logger.debug(
        "INBOUND chat request — memory_id=%s user=%s locale=%s message_len=%d",
        memory_id,
        user_id,
        accept_language,
        len(body.message),
    )
    locale = parse_locale(accept_language)

    # Acquire per-session lock BEFORE loading session — this serialises
    # all concurrent requests targeting the same memory_id.
    lock = await acquire_session_lock(memory_id)
    try:
        store = await get_session_store()
        session = await store.get_session(memory_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        if session.user_id != user_id:
            raise HTTPException(status_code=403, detail="Access denied")

        scenario = await _get_scenario_for_session(request, session)
        if not session.agent_name:
            raise HTTPException(status_code=400, detail="Session has no agent assigned")
        agent_name = session.agent_name
        tool_names: list[str] = []

        if scenario:
            found = False
            for a in scenario.get("agents", []):
                if a["name"] == agent_name:
                    tool_names = a.get("tool_names", [])
                    found = True
                    break
            if not found:
                first = scenario["agents"][0]
                tool_names = first.get("tool_names", [])
                agent_name = first["name"]
        tool_names = [
            t for t in tool_names if match_permission(user_perms, f"use:tool:{t}")
        ]

        scenario_id = session.scenario_id or ""
        trace_id = get_current_trace_id()

        async def _stream_with_lock():
            try:
                async for event in _stream_events(
                    request=request,
                    user_id=user_id,
                    message=body.message,
                    session=session,
                    memory_id=memory_id,
                    agent_name=agent_name,
                    tool_names=tool_names,
                    store=store,
                    locale=locale,
                    scenario_id=scenario_id,
                    trace_id=trace_id,
                ):
                    yield event
            finally:
                await release_session_lock(memory_id, lock)

        return StreamingResponse(
            _stream_with_lock(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    except Exception:
        await release_session_lock(memory_id, lock)
        raise


async def _stream_events(
    request: Request,
    user_id: str,
    message: str,
    session: Any,
    memory_id: str,
    agent_name: str,
    tool_names: list[str],
    store: SessionRepository,
    locale: str = "",
    scenario_id: str = "",
    trace_id: str = "",
) -> AsyncIterator[str]:
    task = None
    stop_event = None

    try:
        runtime, agent_registry, tool_registry, _ = await create_runtime(
            request=request,
            user_id=user_id,
            agent_name=agent_name,
            tool_names=tool_names,
            session_store=store,
            session_id=memory_id,
            scenario_id=scenario_id,
            trace_id=trace_id,
        )

        task, stop_event, queue = await runtime.run(
            user_input=[{"type": "text", "text": message}],
            agent_metadata_id=agent_name,
            memory_id=memory_id,
            tool_names=tool_names,
        )

        while True:
            event = await queue.get()
            if event is None:
                break

            if isinstance(event, MessageEvent):
                continue

            if isinstance(event, MemoryUpdate):
                try:
                    await store.update_usage(session.memory, memory_id)
                except Exception:
                    logger.exception("Failed to persist token usage")

            event_type = type(event).__name__
            payload = serialize_harness_event(event)
            if isinstance(event, ToolStart) and locale and tool_registry:
                func_name = (
                    event.tool_call.get("function", {}).get("name", "")
                    if isinstance(event.tool_call, dict)
                    else ""
                )
                payload["display_name"] = await _resolve_tool_display_name(
                    func_name, locale, tool_registry
                )
            logger.debug(
                "OUTBOUND event — event_type=%s memory_id=%s payload_keys=%s",
                event_type,
                memory_id,
                list(payload.keys()),
            )
            yield format_sse(event_type, payload)
    except Exception as exc:
        logger.exception("Chat stream error")
        detail = str(exc) or type(exc).__name__
        yield format_sse(
            "Error",
            {"message": f"{type(exc).__name__}: {detail}"},
        )
    finally:
        if stop_event is not None:
            stop_event.set()
        if task is not None:
            await task

        if not session.title:
            session.title = message[:80]

        extra = {"title": session.title} if session.title else {}
        try:
            await store.save_memory(session.memory, memory_id, extra=extra)
        except Exception:
            logger.exception("Failed to persist session messages")

    yield format_sse("done", {})
