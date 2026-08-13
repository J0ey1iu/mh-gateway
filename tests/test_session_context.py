"""Regression test for cross-context session-id cleanup.

``set_current_session_id`` runs in the FastAPI endpoint's context; the
streaming generator's ``finally`` block executes in a *different* context
under real uvicorn (Starlette streams the response body inside an anyio task
group).  A token-based ``ContextVar.reset`` therefore raised
``ValueError: Token was created in a different Context`` after every chat
request on the live server (TestClient never reproduced it because it
consumes the body in the same context).

The cleanup must be a plain ``set(default)`` — like ``clear_current_user_id`` —
which is safe regardless of which context it runs in.
"""

from __future__ import annotations

import contextvars
import asyncio

from mh_gateway.context import (
    clear_current_session_id,
    get_current_session_id,
    set_current_session_id,
)


def _run_in_snapshot(coro) -> None:
    """Run *coro* in a context snapshot taken *before* the session id is set
    — the same shape as uvicorn streaming the response body in a task that
    started before the endpoint's set."""
    snapshot = contextvars.copy_context()
    snapshot.run(asyncio.new_event_loop().run_until_complete, coro)


def test_clear_session_id_is_safe_across_contexts() -> None:
    set_current_session_id("mem_x")

    async def cleanup():
        # 模拟流式 generator 的 finally：在别的 context 里清理，不能抛 ValueError
        clear_current_session_id()
        assert get_current_session_id() == ""

    _run_in_snapshot(cleanup())


def test_session_id_readable_in_child_context() -> None:
    """工具在流式任务里读取 session id（附件归属校验的前提）。

    流式任务创建于 set 之后，继承 endpoint context 的拷贝，因此能看到值。
    """
    set_current_session_id("mem_abc")

    async def read_it():
        assert get_current_session_id() == "mem_abc"

    snapshot = contextvars.copy_context()
    snapshot.run(asyncio.new_event_loop().run_until_complete, read_it())
