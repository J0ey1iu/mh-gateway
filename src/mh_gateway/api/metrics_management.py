"""Management-plane metrics API for the dashboard.

``GET /api/v1/management/metrics`` aggregates usage stats from the
:class:`~mh_gateway.metrics_repo.MetricsRepository` over a date
range (inclusive ``YYYY-MM-DD``), plus live entity counts from the
metadata repository.  Requires ``manage:metrics:*``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from mh_gateway.api.dependencies import require_permission
from mh_gateway.metrics_repo import MetricsRepository, get_metrics_repo

router = APIRouter(prefix="/api/v1/management", tags=["management"])


def _parse_date(value: str | None, name: str) -> str | None:
    if not value:
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(422, f"Invalid {name}: expected YYYY-MM-DD")
    return value


@router.get("/metrics")
async def get_management_metrics(
    request: Request,
    date_from: str | None = Query(
        None, description="Start date (inclusive, YYYY-MM-DD)"
    ),
    date_to: str | None = Query(None, description="End date (inclusive, YYYY-MM-DD)"),
    user_id: str = Depends(require_permission("manage:metrics:*")),
) -> dict[str, Any]:
    repo: MetricsRepository | None = get_metrics_repo()
    if repo is None:
        raise HTTPException(503, "Metrics repository not configured")

    d_from = _parse_date(date_from, "date_from")
    d_to = _parse_date(date_to, "date_to")
    if d_from and d_to and d_from > d_to:
        raise HTTPException(422, "date_from must be <= date_to")

    summary = await repo.query_summary(date_from=d_from, date_to=d_to)

    adapters = request.app.state.adapters
    metadata = adapters.metadata
    scenes = await metadata.list_scenarios()
    agents = await metadata.list_agents()
    tools = await metadata.list_tools()

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "date_from": d_from,
        "date_to": d_to,
        "entity_counts": {
            "scenes": len(scenes),
            "agents": len(agents),
            "tools": len(tools),
        },
        **summary.to_dict(),
    }
