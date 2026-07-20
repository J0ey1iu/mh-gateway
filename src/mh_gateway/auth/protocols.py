from __future__ import annotations

# Re-export all adapter protocols from the central adapters module.
from mh_gateway.adapters import (
    PermissionChecker,
    UserAuthProvider,
    UserIdentity,
    match_permission,
)

__all__ = [
    "PermissionChecker",
    "UserAuthProvider",
    "UserIdentity",
    "match_permission",
]
