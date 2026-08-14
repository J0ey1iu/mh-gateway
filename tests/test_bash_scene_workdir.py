"""Scene-cwd injection: the bash tool runs in the current scene's folder.

``create_runtime`` wraps the bash built-in with a scene-level default
``workdir`` when the scene carries a ``cwd`` (mh-local: one scene == one
folder).  This tests the wrapper's contract: the scene cwd is the default,
and an explicit ``workdir`` from the LLM always wins.
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator

import pytest

from mh_gateway.builtin_agents.local_tools import bash_fn
from mh_gateway.services.runtime_service import (
    _resolve_scene_workdir,
    _with_scene_workdir,
)

_POWERSHELL = sys.platform == "win32" or sys.platform == "cygwin"
_PWD_CMD = "Write-Output (Get-Location).Path" if _POWERSHELL else "pwd"


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p))


async def _no_workdir_fn() -> AsyncIterator[str]:
    yield ""


@pytest.mark.asyncio
async def test_scene_workdir_resolution() -> None:
    cwd = "/scene/folder"
    scene = {"id": "s1", "cwd": cwd}
    assert _resolve_scene_workdir(bash_fn, scene) == cwd
    # fns without a workdir param never get a scene default
    assert _resolve_scene_workdir(_no_workdir_fn, scene) is None
    # no scene / no cwd / no fn → no default
    assert _resolve_scene_workdir(bash_fn, None) is None
    assert _resolve_scene_workdir(bash_fn, {"id": "s1"}) is None
    assert _resolve_scene_workdir(None, scene) is None


async def _collect(wrapped, **kwargs) -> list[dict]:
    return [c async for c in wrapped(command=_PWD_CMD, timeout=60, **kwargs)]


@pytest.mark.asyncio
async def test_scene_cwd_is_default_workdir(tmp_path) -> None:
    wrapped = _with_scene_workdir(bash_fn, str(tmp_path))
    chunks = await _collect(wrapped)

    ok = [c for c in chunks if c.get("status") == "ok"]
    assert len(ok) == 1, chunks
    assert _norm(str(tmp_path)) in _norm(ok[0]["stdout"]), ok[0]["stdout"]


@pytest.mark.asyncio
async def test_explicit_workdir_wins_over_scene_cwd(tmp_path, tmp_path_factory) -> None:
    scene_cwd = str(tmp_path)
    other = tmp_path_factory.mktemp("other")
    wrapped = _with_scene_workdir(bash_fn, scene_cwd)
    chunks = await _collect(wrapped, workdir=str(other))

    ok = [c for c in chunks if c.get("status") == "ok"]
    assert len(ok) == 1, chunks
    stdout = _norm(ok[0]["stdout"])
    assert _norm(str(other)) in stdout
    assert _norm(scene_cwd) not in stdout
