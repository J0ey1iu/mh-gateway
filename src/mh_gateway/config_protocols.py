from __future__ import annotations

# Re-export from the central adapters module.
from mh_gateway.adapters import ConfigProvider, SecretResolver

__all__ = [
    "ConfigProvider",
    "SecretResolver",
]
