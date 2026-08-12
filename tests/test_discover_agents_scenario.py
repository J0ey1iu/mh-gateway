"""``discover_agents`` must only list the current scenario's agents.

The local fn path (``_discover_agents_fn``) previously returned every agent
the user had a ``use:agent`` permission for, ignoring the scenario — a
multi-scenario deployment handed the orchestrator a full agent list even
when the session belonged to one scene (issue #47). The remote endpoint
path already filtered by scenario; this test locks in the same behaviour
for the local fn.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mh_gateway.context import (
    clear_current_user_id,
    reset_current_request,
    set_current_request,
    set_current_user_id,
)
from mh_gateway.builtin_agents.registry import _discover_agents_fn


def _mock_adapters(
    agents: list[dict],
    scenarios: list[dict],
    user_perms: list[str] | None,
) -> SimpleNamespace:
    metadata = SimpleNamespace(
        list_agents=AsyncMock(side_effect=lambda: list(agents)),
        get_scenario=AsyncMock(
            side_effect=lambda sid: next(
                (s for s in scenarios if s.get("id") == sid), None
            )
        ),
    )
    authorization = (
        SimpleNamespace(get_permissions=AsyncMock(side_effect=lambda uid: user_perms))
        if user_perms is not None
        else None
    )
    return SimpleNamespace(metadata=metadata, authorization=authorization)


@pytest.mark.asyncio
async def test_discover_agents_filters_by_scenario_and_permissions() -> None:
    agents = [
        {"name": "code-reviewer", "display_name": "Code Reviewer"},
        {"name": "writer", "display_name": "Writer"},
        {"name": "general", "display_name": "General"},
    ]
    scenarios = [
        {
            "id": "code_review",
            "name": "Code Review",
            "agents": [{"name": "code-reviewer", "tool_names": []}],
        },
        {
            "id": "writing",
            "name": "Writing",
            "agents": [{"name": "writer", "tool_names": []}],
        },
    ]
    adapters = _mock_adapters(
        agents,
        scenarios,
        user_perms=["use:agent:code-reviewer", "use:agent:writer", "use:agent:general"],
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(adapters=adapters))
    )

    req_token = set_current_request(request)  # type: ignore[arg-type]
    set_current_user_id("user-1")
    try:
        # scenario_id given → only that scenario's agents, minus excluded.
        result = [
            chunk
            async for chunk in _discover_agents_fn(
                scenario_id="code_review", exclude=""
            )
        ]
        names = [a["name"] for a in result[-1]["agents"]]
        assert names == ["code-reviewer"]

        # permission-restricted scenario → intersection of scenario + perms.
        restricted = _mock_adapters(
            agents,
            scenarios,
            user_perms=["use:agent:writer"],
        )
        restricted_request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(adapters=restricted))
        )
        reset_current_request(req_token)
        req_token = set_current_request(restricted_request)  # type: ignore[arg-type]
        result = [
            chunk async for chunk in _discover_agents_fn(scenario_id="code_review")
        ]
        assert [a["name"] for a in result[-1]["agents"]] == []

        # unknown scenario → empty list.
        result = [chunk async for chunk in _discover_agents_fn(scenario_id="nope")]
        assert [a["name"] for a in result[-1]["agents"]] == []

        # switch back to the full-permissions request
        reset_current_request(req_token)
        req_token = set_current_request(request)  # type: ignore[arg-type]

        # no scenario_id → fall back to permission-only filtering.
        result = [chunk async for chunk in _discover_agents_fn()]
        assert {a["name"] for a in result[-1]["agents"]} == {
            "code-reviewer",
            "writer",
            "general",
        }
    finally:
        reset_current_request(req_token)
        clear_current_user_id()
