"""No-progress stall detection for chat runs (issue #68).

A chat run must only be force-cancelled when it stops producing events —
a fixed total-duration cap killed long reasoning calls mid-stream.
"""

import asyncio
import time

from mh_gateway.api import chat


async def _stuck(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def _heartbeat_run(heartbeat: dict, ticks: int, interval: float) -> None:
    for _ in range(ticks):
        await asyncio.sleep(interval)
        heartbeat["last"] = time.monotonic()


async def test_stuck_run_is_cancelled(monkeypatch) -> None:
    monkeypatch.setattr(chat, "RUN_IDLE_TIMEOUT", 0.2)
    monkeypatch.setattr(chat, "RUN_IDLE_POLL", 0.01)
    monkeypatch.setattr(chat, "CANCEL_GRACE_SECONDS", 1.0)

    task = asyncio.create_task(_stuck(60))
    task.progress = {"last": time.monotonic() - 10.0}  # stale: silent too long

    await chat._await_run_no_stall(task, "m-stuck")

    assert task.cancelled()


async def test_active_run_is_never_cancelled(monkeypatch) -> None:
    monkeypatch.setattr(chat, "RUN_IDLE_TIMEOUT", 0.2)
    monkeypatch.setattr(chat, "RUN_IDLE_POLL", 0.01)
    monkeypatch.setattr(chat, "CANCEL_GRACE_SECONDS", 1.0)

    heartbeat = {"last": time.monotonic()}
    task = asyncio.create_task(_heartbeat_run(heartbeat, ticks=10, interval=0.02))
    task.progress = heartbeat

    await chat._await_run_no_stall(task, "m-active")

    assert task.done()
    assert not task.cancelled()
