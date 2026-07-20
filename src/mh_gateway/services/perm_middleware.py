from __future__ import annotations

from typing import Any

from minimal_harness.agent.middleware import Middleware
from minimal_harness.types import ToolCall

from mh_gateway.adapters import PermissionChecker, match_permission


class PermissionMiddleware(Middleware):
    def __init__(self, user_id: str, permission_checker: PermissionChecker) -> None:
        self._user_id = user_id
        self._permission_checker = permission_checker
        self._user_perms: list[str] | None = None

    async def should_allow_tool(
        self, tool_call: ToolCall, *args: Any, **kwargs: Any
    ) -> bool | str:
        tool_name = tool_call["function"]["name"]
        required_perm = f"use:tool:{tool_name}"

        # Lazy-load permissions on first tool call (avoids blocking constructor)
        if self._user_perms is None:
            self._user_perms = await self._permission_checker.get_permissions(
                self._user_id
            )

        if match_permission(self._user_perms, required_perm):
            return True

        # Fallback: dynamic permission check for time-sensitive permissions
        allowed = await self._permission_checker.check(self._user_id, required_perm)
        if not allowed:
            return f"Permission denied: missing {required_perm}"
        return True
