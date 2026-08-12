"""Management-plane metrics API for the dashboard.

Endpoints (all require ``manage:metrics:*``):

* ``GET /api/v1/management/metrics`` — scalar totals + entity counts.
* ``GET /api/v1/management/metrics/rankings`` — **server-side paginated**
  full rankings (scenes / agents / tools / users).  The gateway keeps the
  protocol only; the pagination is pushed down to the
  :class:`~mh_gateway.metrics_repo.MetricsRepository` backend so external
  services are never asked for full lists.
* ``GET /api/v1/management/metrics/trend`` — daily time series for charts.

Readable display names are enriched at response time from the metadata
repository (never stored in the metrics records): scene id → scene name,
agent/tool id → display name (locale-aware), falling back to the raw key.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from mh_gateway.api.dependencies import require_permission
from mh_gateway.api.locale import parse_locale, resolve_display_name, resolve_locale
from mh_gateway.metrics_repo import (
    RANKING_KINDS,
    MetricsRepository,
    RankingPage,
    get_metrics_repo,
)

router = APIRouter(prefix="/api/v1/management", tags=["management"])


def _parse_date(value: str | None, name: str) -> str | None:
    if not value:
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(422, f"Invalid {name}: expected YYYY-MM-DD")
    return value


def _checked_dates(
    date_from: str | None, date_to: str | None
) -> tuple[str | None, str | None]:
    d_from = _parse_date(date_from, "date_from")
    d_to = _parse_date(date_to, "date_to")
    if d_from and d_to and d_from > d_to:
        raise HTTPException(422, "date_from must be <= date_to")
    return d_from, d_to


def _get_repo(request: Request) -> MetricsRepository:
    repo = get_metrics_repo()
    if repo is None:
        raise HTTPException(503, "Metrics repository not configured")
    return repo


async def _enrich_ranking(
    kind: str, page: RankingPage, request: Request, locale: str
) -> list[dict[str, Any]]:
    metadata = request.app.state.adapters.metadata
    names: dict[str, str] = {}
    if kind == "scenes":
        for s in await metadata.list_scenarios():
            sid = s.get("id")
            if not sid:
                continue
            names[sid] = resolve_locale(
                s.get("name") or sid, s.get("name_locale"), locale
            )
    else:
        entries = (
            await metadata.list_agents()
            if kind == "agents"
            else await metadata.list_tools()
        )
        for e in entries:
            name = e.get("name")
            if not name:
                continue
            names[name] = resolve_display_name(
                e.get("display_name") or name, e.get("display_name_locale"), locale
            )

    items: list[dict[str, Any]] = []
    for item in page.items:
        d = item.to_dict()
        d["display_name"] = names.get(item.key) or item.key
        items.append(d)
    return items


@router.get("/metrics")
async def get_management_metrics(
    request: Request,
    date_from: str | None = Query(
        None, description="Start date (inclusive, YYYY-MM-DD)"
    ),
    date_to: str | None = Query(None, description="End date (inclusive, YYYY-MM-DD)"),
    user_id: str = Depends(require_permission("manage:metrics:*")),
) -> dict[str, Any]:
    repo = _get_repo(request)
    d_from, d_to = _checked_dates(date_from, date_to)
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


@router.get("/metrics/rankings")
async def get_metrics_rankings(
    request: Request,
    kind: str = Query(..., description="Ranking kind: scenes|agents|tools|users"),
    page: int = Query(1, ge=1, description="1-based page number"),
    page_size: int = Query(10, ge=1, le=100, description="Items per page"),
    date_from: str | None = Query(
        None, description="Start date (inclusive, YYYY-MM-DD)"
    ),
    date_to: str | None = Query(None, description="End date (inclusive, YYYY-MM-DD)"),
    accept_language: str | None = Header(None, alias="Accept-Language"),
    user_id: str = Depends(require_permission("manage:metrics:*")),
) -> dict[str, Any]:
    if kind not in RANKING_KINDS:
        raise HTTPException(
            422, f"Invalid kind: expected one of {', '.join(RANKING_KINDS)}"
        )
    repo = _get_repo(request)
    d_from, d_to = _checked_dates(date_from, date_to)

    result = await repo.query_ranking(
        kind=kind,
        date_from=d_from,
        date_to=d_to,
        page=page,
        page_size=page_size,
    )
    locale = parse_locale(accept_language)
    items = await _enrich_ranking(kind, result, request, locale)
    return {
        "kind": kind,
        "items": items,
        "total": result.total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/metrics/trend")
async def get_metrics_trend(
    request: Request,
    date_from: str | None = Query(
        None, description="Start date (inclusive, YYYY-MM-DD)"
    ),
    date_to: str | None = Query(None, description="End date (inclusive, YYYY-MM-DD)"),
    user_id: str = Depends(require_permission("manage:metrics:*")),
) -> dict[str, Any]:
    repo = _get_repo(request)
    d_from, d_to = _checked_dates(date_from, date_to)
    points = await repo.query_trend(date_from=d_from, date_to=d_to)
    return {
        "date_from": d_from,
        "date_to": d_to,
        "points": [p.to_dict() for p in points],
    }
