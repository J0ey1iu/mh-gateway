from __future__ import annotations

from fastapi import APIRouter, HTTPException

from mh_gateway.monitoring.collector import get_collector

metrics_router = APIRouter(prefix="/api/v1", tags=["metrics"])


@metrics_router.get("/metrics")
async def get_metrics():
    collector = get_collector()
    if collector is None:
        raise HTTPException(status_code=404, detail="Metrics not enabled")
    return collector.live_snapshot()
