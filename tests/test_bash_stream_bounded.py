"""Tests that bash streaming stays bounded for long/huge output.

A naive implementation yields one event per output line and ships the
whole accumulated buffer on every event — O(n²) copying that saturates
the server event loop and balloons memory on big outputs (folder
traversals etc.). The tool must batch events and keep a rolling window.
"""

from __future__ import annotations

import sys

import pytest

from mh_gateway.builtin_agents.local_tools import bash_fn

_WINDOW_LIMIT = 64 * 1024

_POWERSHELL = sys.platform == "win32" or sys.platform == "cygwin"


async def _collect(command: str, **kwargs) -> list[dict]:
    return [c async for c in bash_fn(command=command, **kwargs)]


@pytest.mark.asyncio
async def test_many_lines_are_batched_into_few_events() -> None:
    # 5000 行输出：绝不允许 5000 个进度事件（批量冲刷 ≤ ~100 个）。
    cmd = "1..5000 | ForEach-Object { $_ }" if _POWERSHELL else "seq 1 5000"
    chunks = await _collect(cmd, timeout=60)

    stream = [c for c in chunks if c.get("type") == "stream"]
    ok = [c for c in chunks if c.get("status") == "ok"]
    assert len(ok) == 1, "command must complete successfully"
    assert len(stream) < 500, f"5000 lines produced {len(stream)} events"
    assert ok[0]["exit_code"] == 0


@pytest.mark.asyncio
async def test_partial_and_final_output_stay_within_window() -> None:
    cmd = "1..5000 | ForEach-Object { $_ }" if _POWERSHELL else "seq 1 5000"
    chunks = await _collect(cmd, timeout=60)

    stream = [c for c in chunks if c.get("type") == "stream"]
    ok = [c for c in chunks if c.get("status") == "ok"][0]
    for c in stream:
        if c.get("partial_stdout"):
            assert len(c["partial_stdout"]) <= _WINDOW_LIMIT
    assert len(ok["stdout"]) <= _WINDOW_LIMIT
    # 5000 行 × ~5 字节 < 64KB，不应截断
    assert ok.get("truncated") is False
    assert "5000" in ok["stdout"]


@pytest.mark.asyncio
async def test_huge_single_line_output_is_truncated_not_lost() -> None:
    # 单行 70000 字节 > 64KB 窗口：截断为尾部，且不能丢光。
    cmd = 'Write-Output ("x" * 70000)' if _POWERSHELL else "printf 'x%.0s' {1..70000}"
    chunks = await _collect(cmd, timeout=60)

    ok = [c for c in chunks if c.get("status") == "ok"]
    assert len(ok) == 1
    assert ok[0].get("truncated") is True
    assert len(ok[0]["stdout"]) <= _WINDOW_LIMIT
    assert ok[0]["stdout"].rstrip().endswith("x" * 100), (
        "must keep the tail, not drop everything"
    )
    assert ok[0]["total_output_bytes"] >= 70000


@pytest.mark.asyncio
async def test_background_child_does_not_block_completion() -> None:
    """后台子进程持有管道时,前台命令完成即返回,不再误报超时。

    旧逻辑以双管道 EOF 判结束:后台子进程(Start-Process / sleep &)继承
    管道写端时管道永不关闭 → 已完成的前台命令死等 timeout → 误报超时。
    新逻辑以 shell 直接子进程退出为结束信号,应立即返回 ok 并标记后台残留。
    """
    cmd = (
        "cmd /c start /b powershell -NoProfile -Command Start-Sleep 60"
        if _POWERSHELL
        else "sleep 60 &"
    )
    chunks = await _collect(cmd, timeout=5)

    ok = [c for c in chunks if c.get("status") == "ok"]
    assert len(ok) == 1, f"must finish fast, got: {chunks[-1] if chunks else None}"
    assert ok[0].get("background_processes") is True
