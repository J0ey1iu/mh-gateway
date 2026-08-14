from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from minimal_harness.tool.registry import ToolRegistry
from minimal_harness.types import MemoryUpdate, ToolStart
from pydantic import BaseModel

from mh_gateway.api.dependencies import (
    resolve_request_identity,
    resolve_request_permissions,
)
from mh_gateway.api.locale import parse_locale
from mh_gateway.adapters import SessionRepository, match_permission
from mh_gateway.builtin_agents.attachment_tools import ATTACHMENT_TOOL_NAMES
from mh_gateway.context import (
    clear_current_session_id,
    get_current_trace_id,
    set_current_session_id,
)
from mh_gateway.services.database import get_session_store
from minimal_harness.memory import ExtendedInputContentPart
from mh_gateway.services.runtime_service import (
    acquire_session_lock,
    create_runtime,
    format_sse,
    release_session_lock,
    serialize_harness_event,
)

logger = logging.getLogger("orchestration.chat")

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])

# ── Detached runs ────────────────────────────────────────────────────────────
#
# A chat run outlives the SSE connection that started it: closing the page
# must NOT stop the task (issue #63).  A plain client disconnect simply
# detaches the stream and the task keeps running to completion (see
# ``_finalize_run``); the only way to stop it is the explicit
# ``POST /{memory_id}/cancel`` endpoint, which writes a cancel marker into
# the shared session store that ``_watch_cancel`` polls — one mechanism,
# correct across single- and multi-process deployments.


async def _finalize_run(
    memory_id: str,
    task: asyncio.Task,
    session: Any,
    store: SessionRepository,
    lock: asyncio.Lock,
    message: str,
) -> None:
    """Wait for the run to finish, persist everything, then release the lock.

    Runs as an independent background task, so it survives the SSE
    connection being closed or cancelled.  The session lock is held for the
    whole run (not just the connection), keeping concurrent chats from
    racing on the same session.
    """
    try:
        try:
            # 超时兜底：任务若卡死，不能让它永久持有会话锁和 running 状态
            # —— 超时后强制 cancel，然后照常收尾（memory 内容已固定）。
            await asyncio.wait_for(task, timeout=RUN_FINALIZE_TIMEOUT)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            logger.error(
                "chat.run.timeout session=%s trace=%s — cancelling",
                memory_id,
                get_current_trace_id(),
            )
            task.cancel()
        except Exception:
            logger.exception("chat.run.error session=%s", memory_id)
        else:
            logger.info(
                "chat.run.finished session=%s trace=%s",
                memory_id,
                get_current_trace_id(),
            )
        try:
            if not session.title:
                session.title = message[:80]
            extra = {"title": session.title} if session.title else {}
            await store.save_memory(session.memory, memory_id, extra=extra)
        except Exception:
            logger.exception("chat.persist.error session=%s", memory_id)
        else:
            logger.info(
                "chat.persist.ok session=%s messages=%d trace=%s",
                memory_id,
                len(session.get_all_messages()),
                get_current_trace_id(),
            )
        try:
            # Mark AFTER save_memory: the list's "running" flag stays set until
            # everything is on disk, so the frontend's running→idle transition
            # can safely refetch the completed history.
            await store.mark_run_finished(memory_id)
        except Exception:
            logger.exception("chat.status.error session=%s phase=finished", memory_id)
    finally:
        await release_session_lock(memory_id, lock)


# Cancel-marker poll interval: a cancel request can land on any worker in a
# multi-POD deployment; the worker owning the run picks it up from the shared
# store within this window.  ponytail: fixed 1s, tune if cancel latency matters.
CANCEL_POLL_INTERVAL = 1.0
# After issuing task.cancel(), how long to wait for the run to actually end
# before re-cancelling.  asyncio cancellation is a request, not a guarantee:
# a run stuck in a synchronous section responds late, so the watcher insists
# until the task is done instead of giving up after one cancel.
CANCEL_GRACE_SECONDS = 5.0
# How long the finalizer waits for the run before force-cancelling and then
# releasing the session lock regardless — a stuck run must never hold the
# lock / running status forever.
RUN_FINALIZE_TIMEOUT = 60.0


async def _watch_cancel(
    memory_id: str,
    task: asyncio.Task,
    store: SessionRepository,
) -> None:
    """Poll the shared store for a cancel request and stop the run.

    Lives as long as the run task: cancelled via a done-callback once the
    task finishes, so it never needs explicit teardown.

    Scheduling note: ``asyncio.sleep`` is a cooperative “at least” timer —
    a busy event loop stretches the interval, but every await point in this
    codebase yields, so the watcher always runs periodically; only a
    permanently blocked loop would starve it (none here).
    """
    try:
        while not task.done():
            try:
                requested = await store.is_cancel_requested(memory_id)
            except Exception:
                logger.exception("chat.cancel.watch.error session=%s", memory_id)
                await asyncio.sleep(CANCEL_POLL_INTERVAL)
                continue
            if not requested:
                await asyncio.sleep(CANCEL_POLL_INTERVAL)
                continue

            logger.info(
                "chat.cancel.effective session=%s trace=%s",
                memory_id,
                get_current_trace_id(),
            )
            task.cancel()
            # Insist the run actually ends: re-cancel until the task is done.
            # (shield so a timeout on this watcher doesn't kill the run task.)
            while not task.done():
                try:
                    await asyncio.wait_for(
                        asyncio.shield(task), timeout=CANCEL_GRACE_SECONDS
                    )
                    break
                except asyncio.TimeoutError:
                    logger.warning(
                        "chat.cancel.slow session=%s trace=%s — re-cancelling",
                        memory_id,
                        get_current_trace_id(),
                    )
                    task.cancel()
            return
    except asyncio.CancelledError:
        pass


class ChatRequest(BaseModel):
    message: str
    # Controller 选择与运行时参数（per-request）。
    # controller_config 例如 {"max_goal_rounds": 5} 或 {"duration": "30m"}
    controller: str = "default"
    controller_config: dict[str, Any] = {}
    # 本次输入携带的上传附件（file_id/file_name/file_size/backend_type）。
    # 服务端校验归属并绑定到当前会话后，作为用户消息的 file content parts
    # 持久化，模型侧通过附件工具读取内容。
    attachments: list[AttachmentRef] | None = None


class AttachmentRef(BaseModel):
    file_id: str


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


@router.post("/{memory_id}/cancel")
async def cancel_chat(
    request: Request,
    memory_id: str,
    user_id: str = Depends(resolve_request_identity),
):
    """Explicitly stop a running chat.

    Closing the page only detaches the SSE stream — the run continues
    (issue #63).  This endpoint is the one way to actually stop it.
    """
    store = await get_session_store(request)
    session = await store.get_session(memory_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.user_id != user_id:
        raise HTTPException(status_code=403, detail="Access denied")

    logger.info("chat.cancel.request session=%s user=%s", memory_id, user_id)
    try:
        # 写入共享取消标记：任务所在 worker（任意 POD）的 watcher 会在一个
        # 轮询周期内发现并停止它（issue #63）。
        await store.request_cancel(memory_id)
    except Exception:
        logger.exception("chat.cancel.marker.error session=%s", memory_id)
    return {"ok": True}


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
        store = await get_session_store(request)
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

        # ── Attachments: validate ownership, bind to this session, and
        # enable the attachment tools for this run ──
        attachment_metas: list[dict[str, Any]] = []
        if body.attachments:
            attachment_store = getattr(request.app.state.adapters, "attachments", None)
            if attachment_store is None:
                raise HTTPException(
                    status_code=503,
                    detail="Attachments are not enabled in this deployment",
                )
            seen: set[str] = set()
            for ref in body.attachments:
                if ref.file_id in seen:
                    continue
                seen.add(ref.file_id)
                record = await attachment_store.get(ref.file_id)
                if record is None:
                    raise HTTPException(
                        status_code=404, detail=f"Attachment not found: {ref.file_id}"
                    )
                if record.user_id != user_id:
                    raise HTTPException(
                        status_code=403,
                        detail=f"Access denied to attachment: {ref.file_id}",
                    )
                await attachment_store.bind(ref.file_id, memory_id)
                attachment_metas.append(record.as_metadata())
            # 只有本次输入携带附件时才注入附件工具：不污染无关 agent 的
            # context window。
            tool_names.extend(
                t
                for t in ATTACHMENT_TOOL_NAMES
                if match_permission(user_perms, f"use:tool:{t}")
            )

        set_current_session_id(memory_id)

        async def _stream_with_lock():
            try:
                async for event in _stream_events(
                    request=request,
                    user_id=user_id,
                    message=body.message,
                    attachments=attachment_metas,
                    session=session,
                    memory_id=memory_id,
                    agent_name=agent_name,
                    tool_names=tool_names,
                    store=store,
                    locale=locale,
                    scenario_id=scenario_id,
                    trace_id=trace_id,
                    controller_type=body.controller,
                    controller_config=body.controller_config,
                    lock=lock,
                ):
                    yield event
            finally:
                # Session lock is owned by the run (released by
                # ``_finalize_run``), not by the connection: a client
                # disconnect must not unlock a still-running task.
                clear_current_session_id()

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
    attachments: list[dict[str, Any]],
    session: Any,
    memory_id: str,
    agent_name: str,
    tool_names: list[str],
    store: SessionRepository,
    lock: asyncio.Lock,
    locale: str = "",
    scenario_id: str = "",
    trace_id: str = "",
    controller_type: str = "default",
    controller_config: dict[str, Any] | None = None,
) -> AsyncIterator[str]:
    task = None
    finalizer: asyncio.Task | None = None

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

        # 用户消息：文本 + 附件 file parts。附件元数据随消息持久化
        # （前端刷新后从读侧拿回渲染），模型侧由 provider 投影为
        # “[File: name (id=…)]” 纯文本 + 附件工具读取内容。
        user_input: list[ExtendedInputContentPart] = [
            {"type": "text", "text": message}  # type: ignore[list-item]
        ]
        user_input.extend(
            {"type": "file", "file": meta}  # type: ignore[dict-item]
            for meta in attachments
        )

        task, _stop_event, queue = await runtime.run(
            user_input=user_input,
            agent_metadata_id=agent_name,
            memory_id=memory_id,
            tool_names=tool_names,
            context=(
                {"locale": locale, "user_id": user_id}
                if locale
                else {"user_id": user_id}
            ),
            controller_type=controller_type,
            controller_config=controller_config,
        )
        logger.info(
            "chat.run.start session=%s user=%s agent=%s trace=%s",
            memory_id,
            user_id,
            agent_name,
            trace_id,
        )

        # Detach the run's lifecycle from this SSE connection: a background
        # task waits for the run, persists the session and releases the lock
        # even if this generator is cancelled/closed (page closed).  The only
        # way to stop the run is the cancel endpoint, which writes a shared
        # marker that ``_watch_cancel`` polls.
        try:
            await store.mark_run_started(memory_id)
        except Exception:
            logger.exception("chat.status.error session=%s phase=started", memory_id)
        else:
            logger.info(
                "chat.run.status session=%s status=running trace=%s",
                memory_id,
                trace_id,
            )
        cancel_watcher = asyncio.create_task(_watch_cancel(memory_id, task, store))
        # Keep the watcher referenced until the run ends: the done-callback
        # cancels it, so it never outlives the task.
        task.add_done_callback(lambda _t: cancel_watcher.cancel())
        finalizer = asyncio.create_task(
            _finalize_run(
                memory_id,
                task,
                session,
                store,
                lock,
                message,
            )
        )

        while True:
            event = await queue.get()
            if event is None:
                break

            # MessageEvent carries the canonical id of every message as it
            # is produced (Memory.add_message stamps msg-{seq}); forwarding
            # it lets the frontend associate each rendered record with the
            # same id the session reload will return.
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
        logger.exception("chat.stream.error session=%s", memory_id)
        detail = str(exc) or type(exc).__name__
        yield format_sse(
            "Error",
            {"message": f"{type(exc).__name__}: {detail}"},
        )
    finally:
        exc = sys.exc_info()[1]
        # Normal end (or handled error): make sure everything is persisted
        # before sending ``done``, so a refresh right after done sees it.
        # On disconnect (GeneratorExit / CancelledError unwinding) skip the
        # await — the finalizer task completes on its own.
        if finalizer is not None:
            if exc is None:
                logger.info("chat.stream.done session=%s trace=%s", memory_id, trace_id)
                await finalizer
            else:
                # 连接断开（关页/刷新/断网）：任务 detach 继续后台跑完。
                logger.info(
                    "chat.stream.detached session=%s reason=%s trace=%s",
                    memory_id,
                    type(exc).__name__,
                    trace_id,
                )
        else:
            # The run never started (setup error): no background task owns
            # the lock, release it here.  (release_session_lock's lock
            # release is synchronous, so an in-flight cancel cannot leave
            # the lock permanently held.)
            if lock.locked():
                await release_session_lock(memory_id, lock)

    yield format_sse("done", {})
