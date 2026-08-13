"""Tests for the split file tools: chunked write progress + read pagination.

Covers the two pieces of logic that replaced the old mega local_file_operator:
- write_file/edit_file stream bounded \"Writing: N/M chars\" progress events
  for large payloads (so the frontend accumulates visible progress).
- read_file pages by line range (offset/limit) and reports truncation for
  large files instead of dumping everything into the context.
"""

from __future__ import annotations

import pytest

from mh_gateway.builtin_agents.local_tools import (
    edit_file_fn,
    read_file_fn,
    write_file_fn,
)


async def _collect(fn, **kwargs) -> list[dict]:
    return [chunk async for chunk in fn(**kwargs)]


@pytest.mark.asyncio
async def test_write_yields_bounded_progress_and_writes_everything(tmp_path) -> None:
    path = tmp_path / "big.txt"
    content = "x" * 50_000
    chunks = await _collect(write_file_fn, path=str(path), content=content)

    progress = [c for c in chunks if c.get("status") == "progress"]
    ok = [c for c in chunks if c.get("status") == "ok"]
    assert len(progress) >= 3, "large writes must stream progress"
    assert len(progress) <= 25, "progress events must stay bounded"
    assert any(c["message"].startswith("Writing: ") for c in progress)
    assert progress[-1]["message"].endswith(f"{len(content)} chars")
    assert len(ok) == 1
    assert ok[0]["size"] == len(content)
    assert path.read_text("utf-8") == content


@pytest.mark.asyncio
async def test_small_write_single_shot(tmp_path) -> None:
    path = tmp_path / "small.txt"
    chunks = await _collect(write_file_fn, path=str(path), content="hello")
    assert path.read_text("utf-8") == "hello"
    # Small payloads stay single-shot: start progress + ok only.
    assert len(chunks) == 2
    assert chunks[0]["status"] == "progress"
    assert chunks[1]["status"] == "ok"


@pytest.mark.asyncio
async def test_edit_writes_chunked(tmp_path) -> None:
    path = tmp_path / "edit.txt"
    path.write_text("old " * 20_000, encoding="utf-8")
    chunks = await _collect(
        edit_file_fn,
        path=str(path),
        old_string="old",
        new_string="new",
    )
    assert "new " * 20_000 == path.read_text("utf-8")
    progress = [c for c in chunks if c.get("status") == "progress"]
    assert any("Writing:" in c["message"] for c in progress)


@pytest.mark.asyncio
async def test_read_pages_by_line_range(tmp_path) -> None:
    path = tmp_path / "paged.txt"
    path.write_text("".join(f"line {i}\n" for i in range(1, 101)), encoding="utf-8")

    chunks = await _collect(read_file_fn, path=str(path), offset=1, limit=10)
    ok = [c for c in chunks if c.get("status") == "ok"][0]
    assert ok["content"] == "".join(f"line {i}\n" for i in range(1, 11))
    assert ok["total_lines"] == 100
    assert ok["start_line"] == 1
    assert ok["end_line"] == 10
    assert ok["truncated"] is True

    # second page continues where the first stopped
    chunks = await _collect(read_file_fn, path=str(path), offset=11, limit=10)
    ok = [c for c in chunks if c.get("status") == "ok"][0]
    assert ok["content"].startswith("line 11\n")
    assert ok["start_line"] == 11
    assert ok["end_line"] == 20
