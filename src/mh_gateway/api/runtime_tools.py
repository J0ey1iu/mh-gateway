from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse

from mh_gateway.api.dependencies import resolve_m2m_identity
from mh_gateway.api.locale import (
    parse_locale,
    resolve_description,
    resolve_display_name,
)
from mh_gateway.adapters import match_permission
from mh_gateway.services.runtime_service import _apply_permission_filter

logger = logging.getLogger("orchestration.runtime_tools")

router = APIRouter(prefix="/api/v1/tools", tags=["runtime_tools"])


def _sse_line(event_type: str, data: Any) -> str:
    return f"data: {json.dumps({'type': event_type, 'data': data}, ensure_ascii=False, default=str)}\n\n"


@router.post("/discover_agents/execute")
async def discover_agents_execute(
    request: Request,
    body: dict[str, Any],
    accept_language: str | None = Header(None, alias="Accept-Language"),
    user_id: str = Depends(resolve_m2m_identity),
):
    args = body.get("args", {})
    locale = args.get("locale") or parse_locale(accept_language)
    exclude = args.get("exclude")
    scenario_id = request.query_params.get("scenario_id", "")
    caller_agent_name = request.query_params.get("agent_name", "")

    async def event_stream():
        try:
            adapters = request.app.state.adapters

            user_perms: list[str] | None = None
            if adapters.authorization:
                user_perms = await adapters.authorization.get_permissions(user_id)

            scenario_agent_names: set[str] | None = None
            if scenario_id:
                scenario_data = await adapters.metadata.get_scenario(scenario_id)
                if scenario_data is not None:
                    scenario_agent_names = _apply_permission_filter(
                        {a["name"] for a in scenario_data.get("agents", [])},
                        user_perms,
                        "use:agent",
                    )
                else:
                    scenario_agent_names = set()

            agents = await adapters.metadata.list_agents()
            result = []
            for a in agents:
                name = a["name"]
                if caller_agent_name and name == caller_agent_name:
                    continue
                if exclude and name == exclude:
                    continue
                if (
                    scenario_agent_names is not None
                    and name not in scenario_agent_names
                ):
                    continue
                if (
                    scenario_agent_names is None
                    and user_perms is not None
                    and not match_permission(user_perms, f"use:agent:{name}")
                ):
                    continue
                result.append(
                    {
                        "name": a["name"],
                        "display_name": resolve_display_name(
                            a.get("display_name", a["name"]),
                            a.get("display_name_locale"),
                            locale,
                        ),
                        "description": resolve_description(
                            a.get("description", ""),
                            a.get("description_locale"),
                            locale,
                        ),
                    }
                )
            yield _sse_line("tool_end", {"status": "ok", "agents": result})
        except Exception:
            logger.exception("Discover agents execution error")
            try:
                yield _sse_line("error", {"message": "Internal server error"})
            except Exception:
                pass

    return StreamingResponse(event_stream(), media_type="text/event-stream")
