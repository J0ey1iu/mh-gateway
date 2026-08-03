"""Tests for the local file operator's chunked write progress.

Large single-shot ``f.write()`` calls give the UI nothing to show while
the tool runs — the card just spins. Chunked writes yield bounded
``progress`` events (\"Writing: N/M chars\") so the frontend accumulates
visible progress during write/append/edit operations.
"""

from __future__ import annotations


import pytest

from mh_gateway.builtin_agents.local_tools import local_file_operator_fn


async def _collect(op: str, **kwargs) -> list[dict]:
    return [chunk async for chunk in local_file_operator_fn(operation=op, **kwargs)]


@pytest.mark.asyncio
async def test_write_yields_bounded_progress_and_writes_everything(tmp_path) -> None:
    path = tmp_path / "big.txt"
    content = "x" * 50_000
    chunks = await _collect("write", path=str(path), content=content)

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
    chunks = await _collect("write", path=str(path), content="hello")
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
        "edit",
        path=str(path),
        old_string="old",
        new_string="new",
    )
    assert "new " * 20_000 == path.read_text("utf-8")
    progress = [c for c in chunks if c.get("status") == "progress"]
    assert any("Writing:" in c["message"] for c in progress)
