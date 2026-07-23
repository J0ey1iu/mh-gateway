from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from mh_gateway.api.dependencies import (
    resolve_request_identity,
    resolve_request_permissions,
)
from mh_gateway.adapters import LLMResolveSpec
from mh_gateway.api.locale import parse_locale, resolve_display_name
from minimal_harness.agent._compaction import build_chat_payload
from minimal_harness.types import AgentMetadata
from mh_gateway.services.database import get_session_store
from mh_gateway.services.runtime_service import (
    acquire_session_lock,
    format_sse,
    release_session_lock,
    resolve_model_max_context,
)

logger = logging.getLogger("orchestration.sessions")

router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])


class SessionCreateRequest(BaseModel):
    agent_name: str
    scenario_id: str | None = None


@router.get("")
async def list_sessions(
    request: Request,
    scenario_id: str | None = Query(None),
    user_id: str = Depends(resolve_request_identity),
):
    logger.debug("INBOUND list_sessions — user=%s scenario_id=%s", user_id, scenario_id)
    locale = parse_locale(request.headers.get("accept-language"))
    store = await get_session_store(request)
    sessions = await store.list_user_sessions(user_id, scenario_id)
    return [
        {
            "memory_id": s["session_id"],
            "title": s.get("title") or "Untitled",
            "created_at": s["created_at"],
            "message_count": s["message_count"],
            "agent_name": s["agent_name"],
            "user_id": s["user_id"],
            "scenario_id": s["scenario_id"],
            "display_name": resolve_display_name(
                s["agent_name"],
                s.get("display_name_locale"),
                locale,
            ),
        }
        for s in sessions
    ]


@router.post("")
async def create_session(
    request: Request,
    body: SessionCreateRequest,
    user_id: str = Depends(resolve_request_identity),
):
    logger.debug(
        "INBOUND create_session — user=%s agent=%s scenario_id=%s",
        user_id,
        body.agent_name,
        body.scenario_id,
    )
    locale = parse_locale(request.headers.get("accept-language"))

    display_name_locale: str | None = None
    adapters = request.app.state.adapters
    agent_meta = await adapters.metadata.get_agent(body.agent_name)
    if agent_meta is not None:
        display_name_locale = agent_meta.get("display_name_locale")

    max_context = await resolve_model_max_context(
        adapters.metadata, adapters.llm, body.agent_name
    )

    store = await get_session_store(request)
    session = await store.create_session(
        agent_name=body.agent_name,
        user_id=user_id,
        scenario_id=body.scenario_id,
        display_name_locale=display_name_locale,
    )
    return {
        "memory_id": session.session_id,
        "title": session.title or "New Chat",
        "created_at": session.created_at,
        "message_count": 0,
        "agent_name": session.agent_name,
        "user_id": session.user_id,
        "scenario_id": session.scenario_id,
        "display_name": resolve_display_name(
            session.agent_name,
            display_name_locale,
            locale,
        ),
        "max_context": max_context,
        "total_tokens": 0,
    }


@router.get("/{memory_id}")
async def get_session(
    request: Request,
    memory_id: str,
    user_id: str = Depends(resolve_request_identity),
):
    store = await get_session_store(request)
    session = await store.get_session(memory_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.user_id != user_id:
        raise HTTPException(status_code=403, detail="Access denied")
    locale = parse_locale(request.headers.get("accept-language"))
    adapters = request.app.state.adapters
    max_context = await resolve_model_max_context(
        adapters.metadata, adapters.llm, session.agent_name
    )
    total_tokens = session.memory.get_message_usage().get("total_tokens", 0)
    return {
        "memory_id": session.session_id,
        "title": session.title or "Untitled",
        "created_at": session.created_at,
        "message_count": len(session.get_all_messages()),
        "agent_name": session.agent_name,
        "user_id": session.user_id,
        "scenario_id": session.scenario_id,
        "display_name": resolve_display_name(
            session.agent_name,
            session.display_name_locale,
            locale,
        ),
        "compact_offset": session.memory.get_forward_offset(),
        "max_context": max_context,
        "total_tokens": total_tokens,
    }


@router.get("/{memory_id}/messages")
async def get_session_messages(
    request: Request,
    memory_id: str,
    user_id: str = Depends(resolve_request_identity),
):
    store = await get_session_store(request)
    session = await store.get_session(memory_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.user_id != user_id:
        raise HTTPException(status_code=403, detail="Access denied")
    items = store.get_messages_as_items(session)
    compact_offset = session.memory.get_forward_offset()
    adapters = request.app.state.adapters
    max_context = await resolve_model_max_context(
        adapters.metadata, adapters.llm, session.agent_name
    )
    total_tokens = session.memory.get_message_usage().get("total_tokens", 0)
    return {
        "items": items,
        "compact_offset": compact_offset,
        "total_tokens": total_tokens,
        "max_context": max_context,
    }


@router.delete("/{memory_id}", status_code=200)
async def delete_session(
    request: Request,
    memory_id: str,
    user_id: str = Depends(resolve_request_identity),
):
    store = await get_session_store(request)
    session = await store.get_session(memory_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.user_id != user_id:
        raise HTTPException(status_code=403, detail="Access denied")
    await store.delete_session(memory_id)
    return {"ok": True}


@router.post("/{memory_id}/compact")
async def compact_session(
    request: Request,
    memory_id: str,
    user_id: str = Depends(resolve_request_identity),
    user_perms: list[str] = Depends(resolve_request_permissions),
) -> StreamingResponse:
    logger.debug(
        "INBOUND compact request — memory_id=%s user=%s",
        memory_id,
        user_id,
    )
    lock = await acquire_session_lock(memory_id)
    try:
        store = await get_session_store(request)
        session = await store.get_session(memory_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        if session.user_id != user_id:
            raise HTTPException(status_code=403, detail="Access denied")
        if not session.agent_name:
            raise HTTPException(status_code=400, detail="Session has no agent assigned")

        adapters = request.app.state.adapters
        agent_name = session.agent_name
        agent_meta = await adapters.metadata.get_agent(agent_name)
        if agent_meta is None:
            raise HTTPException(status_code=404, detail="Agent not found")

        target_meta = AgentMetadata(
            name=agent_name,
            metadata_id=agent_name,
            agent_type=agent_meta.get("agent_type", "simple"),
            provider=agent_meta.get("provider", "openai"),
            model=agent_meta.get("model", ""),
            llm_config=agent_meta.get("llm_config", {}),
        )
        llm_provider = await adapters.llm.create_llm(
            LLMResolveSpec(agent=target_meta, user=user_id)
        )
        all_msgs = session.get_all_messages()
        system_prompt = agent_meta.get("system_prompt", "") or ""

        async def _stream_with_lock():
            content_accumulated = ""
            reasoning_accumulated = ""
            start_time = 0.0
            error_msg: str | None = None
            try:
                try:
                    yield format_sse(
                        "CompactionStart",
                        {
                            "dropped_message_count": len(all_msgs),
                            "existing_summary": None,
                            "keep_recent": 0,
                            "total_tokens": 0,
                        },
                    )
                    start_time = __import__("time").time()
                    payload = build_chat_payload(system_prompt, list(all_msgs), None)
                    response = await llm_provider.chat(messages=payload, tools=[])  # type: ignore[arg-type]
                    async for delta in response:
                        if not delta:
                            continue
                        if delta.reasoning:
                            reasoning_accumulated += delta.reasoning
                            yield format_sse(
                                "CompactionChunk",
                                {
                                    "type": "reasoning",
                                    "delta": delta.reasoning,
                                    "accumulated": reasoning_accumulated,
                                },
                            )
                        if delta.content:
                            content_accumulated += delta.content
                            yield format_sse(
                                "CompactionChunk",
                                {
                                    "type": "content",
                                    "delta": delta.content,
                                    "accumulated": content_accumulated,
                                },
                            )
                except Exception as exc:
                    logger.exception("Compaction failed")
                    error_msg = f"{type(exc).__name__}: {exc}"

                compact_offset = len(all_msgs)

                if error_msg is None and content_accumulated:
                    summary_message: dict = {
                        "role": "compaction",
                        "content": content_accumulated,
                        "meta": {"compact_offset": compact_offset},
                    }
                    if reasoning_accumulated:
                        summary_message["reasoning_content"] = reasoning_accumulated
                    await session.add_message(summary_message)
                    session.memory.set_forward_offset(compact_offset)
                    try:
                        await store.save_memory(
                            session.memory,
                            memory_id,
                            extra={"title": session.title} if session.title else None,
                        )
                    except Exception:
                        logger.exception("Failed to persist compacted session")

                yield format_sse(
                    "CompactionEnd",
                    {
                        "summary": content_accumulated,
                        "compact_offset": compact_offset,
                        "dropped_message_count": len(all_msgs),
                        "duration": __import__("time").time() - start_time
                        if start_time
                        else 0,
                        "error": error_msg,
                    },
                )
                yield format_sse("done", {})
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
