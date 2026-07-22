from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

from mh_gateway.context import get_current_request, get_current_user_id


async def _discover_agents_fn(
    exclude: str = "", locale: str = ""
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

    result = []
    for a in agents:
        name = a["name"]
        if exclude and name == exclude:
            continue
        if user_perms is not None and not match_permission(
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
    store = await get_session_store()

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
                ToolProgress,
            )

            if isinstance(event, AgentEnd):
                result_text = event.response or result_text
            elif isinstance(event, ToolProgress):
                yield {
                    "status": "progress",
                    "type": "tool_progress",
                    "tool_call": event.tool_call,
                    "chunk": event.chunk,
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
