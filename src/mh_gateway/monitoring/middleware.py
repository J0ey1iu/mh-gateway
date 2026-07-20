from __future__ import annotations

import json
import logging
import time
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from mh_gateway.context import get_current_trace_id, get_current_user_id
from mh_gateway.monitoring.collector import get_collector

logger = logging.getLogger("orchestration.access")


class AccessLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        start = time.monotonic()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            duration_ms = round((time.monotonic() - start) * 1000, 2)
            route = request.scope.get("route")
            path = route.path if route else request.url.path
            method = request.method
            trace_id = get_current_trace_id()
            user_id = request.scope.get("_user_id", get_current_user_id() or "") or ""

            if request.method != "OPTIONS":
                collector = get_collector()
                if collector is not None:
                    collector.http_requests_total.inc(
                        {"method": method, "path": path, "status": str(status_code)}
                    )
                    collector.http_request_duration_ms.observe(
                        {"method": method, "path": path}, duration_ms
                    )

            entry = {
                "logger": "orchestration.access",
                "method": method,
                "path": path,
                "status": status_code,
                "duration_ms": duration_ms,
                "user_id": user_id,
                "trace_id": trace_id,
            }
            logger.info(json.dumps(entry, ensure_ascii=False, default=str))
