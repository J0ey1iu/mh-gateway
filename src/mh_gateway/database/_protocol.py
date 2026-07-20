from __future__ import annotations

from datetime import datetime, timezone

from mh_gateway.adapters import DatabaseProtocol


def _ts_ms() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


__all__ = [
    "DatabaseProtocol",
    "_ts_ms",
]
