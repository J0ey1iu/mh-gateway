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
    chunks = await _collect(cmd, timeout_ms=60_000)

    stream = [c for c in chunks if c.get("type") == "stream"]
    ok = [c for c in chunks if c.get("status") == "ok"]
    assert len(ok) == 1, "command must complete successfully"
    assert len(stream) < 500, f"5000 lines produced {len(stream)} events"
    assert ok[0]["exit_code"] == 0


@pytest.mark.asyncio
async def test_partial_and_final_output_stay_within_window() -> None:
    cmd = "1..5000 | ForEach-Object { $_ }" if _POWERSHELL else "seq 1 5000"
    chunks = await _collect(cmd, timeout_ms=60_000)

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
    chunks = await _collect(cmd, timeout_ms=60_000)

    ok = [c for c in chunks if c.get("status") == "ok"]
    assert len(ok) == 1
    assert ok[0].get("truncated") is True
    assert len(ok[0]["stdout"]) <= _WINDOW_LIMIT
    assert ok[0]["stdout"].rstrip().endswith("x" * 100), (
        "must keep the tail, not drop everything"
    )
    assert ok[0]["total_output_bytes"] >= 70000
