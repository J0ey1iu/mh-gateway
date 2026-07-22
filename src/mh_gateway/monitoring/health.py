from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger("orchestration.health")

health_router = APIRouter(tags=["health"])


@health_router.get("/health")
async def health():
    return {"status": "ok"}


@health_router.get("/ready")
async def ready(request: Request):
    adapters = getattr(request.app.state, "adapters", None)
    if adapters is None or not hasattr(adapters, "sessions"):
        raise HTTPException(status_code=503, detail="Adapters not initialised")
    try:
        await adapters.sessions.healthcheck()
    except Exception as e:
        logger.warning("Readiness check failed: %s", e)
        raise HTTPException(status_code=503, detail="Not ready") from e
    return {"status": "ready"}
