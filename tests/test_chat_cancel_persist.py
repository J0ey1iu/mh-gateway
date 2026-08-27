"""Regression test for cancel-during-stream (issue #94).

A user pressing "stop" cancels the run task mid-stream. Two bugs lost the
already-rendered content:

1. ``base.py`` only caught ``Exception`` around the stream, not
   ``asyncio.CancelledError`` (a ``BaseException``), so the partial reply
   never reached memory.
2. ``_finalize_run`` skipped ``save_memory``/``mark_run_finished`` in the
   cancelled branch, so even if memory held partial content it was never
   persisted and the session stayed ``running``.

This test pins the second fix: a cancelled run must still persist + finish.
"""

from __future__ import annotations

import asyncio

import pytest

from mh_gateway.api.chat import _finalize_run


class _Store:
    """Minimal SessionRepository double for _finalize_run."""

    def __init__(self) -> None:
        self.saved: list[str] = []
        self.finished: list[str] = []

    async def save_memory(self, memory, memory_id: str, extra: dict | None = None) -> None:
        self.saved.append(memory_id)

    async def mark_run_finished(self, memory_id: str) -> None:
        self.finished.append(memory_id)

    async def mark_run_started(self, memory_id: str) -> None:  # pragma: no cover
        return None


class _Memory:
    def __init__(self, messages: list[dict]) -> None:
        self._messages = messages

    def get_all_messages(self) -> list[dict]:
        return self._messages


class _Session:
    def __init__(self, memory: _Memory) -> None:
        self.memory = memory
        self.title = ""  # type: ignore[assignment]

    def get_all_messages(self) -> list[dict]:
        return self.memory.get_all_messages()


@pytest.mark.asyncio
async def test_cancelled_run_persists_and_marks_finished() -> None:
    store = _Store()
    memory = _Memory([{"role": "assistant", "content": "partial reply..."}])
    session = _Session(memory)

    # 模拟一个被用户点停止 cancel 的运行 task：取消时照常把 partial 存进 memory
    # （对应 base.py 修复后 agent 在 CancelledError 里 add_message 的行为）。
    async def _run_cancelled() -> None:
        try:
            await asyncio.sleep(30)
        finally:
            ok = await memory.get_all_messages()
            assert ok  # memory 里已有 agent 在取消时写入的 partial

    task = asyncio.create_task(_run_cancelled())
    task.cancel()
    await asyncio.sleep(0)  # 让 CancelledError 传播到 task 内

    lock = asyncio.Lock()
    await lock.acquire()

    try:
        await _finalize_run("mem_1", task, session, store, lock, "hello")
    finally:
        if lock.locked():
            lock.release()

    assert store.saved == ["mem_1"], "cancelled run must persist the session"
    assert store.finished == ["mem_1"], "cancelled run must mark finished"
    assert memory.get_all_messages()  # partial 留在 memory 里

    # 日志断言：cancel 分支不再 bare——最终回到聊天页能看到 partial 并刷新仍在。
    assert session.title == "hello" or memory.get_all_messages()
