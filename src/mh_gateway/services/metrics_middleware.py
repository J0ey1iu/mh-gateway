"""Metrics persistence middleware.

Single responsibility: persist usage metrics (LLM calls, tool calls)
to the process-wide :class:`~mh_gateway.metrics_repo.MetricsRepository`.

This is deliberately separate from ``AuditMiddleware`` (audit logs)
so each concern can evolve independently.  The middleware is a no-op
when no repository has been registered (``set_metrics_repo(None)``),
matching the optional nature of the feature.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from minimal_harness.agent.middleware import Middleware
from minimal_harness.types import AgentEnd, LLMEnd, ToolCall

from mh_gateway.metrics_repo import (
    LLMCallRecord,
    ToolCallRecord,
    get_metrics_repo,
)

logger = logging.getLogger("orchestration.metrics")


def _utc_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class MetricsPersistenceMiddleware(Middleware):
    """Write LLM / tool usage records to the metrics repository."""

    def __init__(
        self,
        user_id: str = "",
        session_id: str = "",
        agent_id: str = "",
        scenario_id: str = "",
        provider: str = "",
        model: str = "",
    ) -> None:
        self._user_id = user_id
        self._session_id = session_id
        self._agent_id = agent_id
        self._scenario_id = scenario_id
        self._provider = provider
        self._model = model
        self._llm_start_ts: float | None = None

    async def on_llm_start(self, messages: list[dict[str, Any]], tools: Any) -> None:
        self._llm_start_ts = time.monotonic()

    async def on_llm_end(self, event: LLMEnd) -> None:
        repo = get_metrics_repo()
        if repo is None:
            return

        duration_ms = 0.0
        if self._llm_start_ts is not None:
            duration_ms = round((time.monotonic() - self._llm_start_ts) * 1000, 2)
            self._llm_start_ts = None

        usage = event.usage
        prompt_tokens = usage.get("prompt_tokens", 0) if usage else 0
        completion_tokens = usage.get("completion_tokens", 0) if usage else 0

        try:
            await repo.record_llm_call(
                LLMCallRecord(
                    ts=_utc_now(),
                    user_id=self._user_id,
                    session_id=self._session_id,
                    agent_name=self._agent_id,
                    scenario_id=self._scenario_id,
                    provider=self._provider,
                    model=self._model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    duration_ms=duration_ms,
                    status="error" if event.error else "ok",
                    error=event.error or "",
                )
            )
        except Exception:
            logger.exception("Failed to persist LLM metrics record")

    # 注意：不维护任何 per-tool 实例状态。同一回合的多个工具调用由
    # harness 并发执行（asyncio.create_task per call），共享状态会互相
    # 串名/串时长（issue #62 的 "unknown" 幽灵条目）。名字从
    # on_tool_end/on_tool_error 收到的 tool_call 直接取，天然并发安全。
    async def on_tool_end(self, tool_call: ToolCall, result: Any) -> None:
        await self._record_tool(tool_call, "ok")

    async def on_tool_error(self, tool_call: ToolCall, error: Exception) -> None:
        await self._record_tool(tool_call, "error")

    async def _record_tool(self, tool_call: ToolCall, status: str) -> None:
        repo = get_metrics_repo()
        if repo is None:
            return
        try:
            await repo.record_tool_call(
                ToolCallRecord(
                    ts=_utc_now(),
                    user_id=self._user_id,
                    session_id=self._session_id,
                    agent_name=self._agent_id,
                    scenario_id=self._scenario_id,
                    tool_name=tool_call.get("function", {}).get("name", "")
                    or "unknown",
                    status=status,
                )
            )
        except Exception:
            logger.exception("Failed to persist tool metrics record")

    async def on_agent_end(self, event: AgentEnd) -> None:
        # No-op reserved for future agent-level stats; hook exists so
        # subclasses can extend without touching the base class.
        return None

    async def on_error(self, error: BaseException) -> None:
        """补记 LLM 调用失败：失败时 ``on_llm_end`` 不会触发（异常直接抛出），
        否则错误率只统计到工具失败、漏掉 LLM 失败（mh-incubator #85）。

        仅当 ``on_llm_start`` 后未收到 ``on_llm_end``（即 ``_llm_start_ts``
        仍非空）时才补记，避免把 LLM 成功之后的其他阶段异常误算成 LLM 失败。
        """
        if self._llm_start_ts is None:
            return
        repo = get_metrics_repo()
        duration_ms = round((time.monotonic() - self._llm_start_ts) * 1000, 2)
        self._llm_start_ts = None
        if repo is None:
            return
        try:
            await repo.record_llm_call(
                LLMCallRecord(
                    ts=_utc_now(),
                    user_id=self._user_id,
                    session_id=self._session_id,
                    agent_name=self._agent_id,
                    scenario_id=self._scenario_id,
                    provider=self._provider,
                    model=self._model,
                    prompt_tokens=0,
                    completion_tokens=0,
                    duration_ms=duration_ms,
                    status="error",
                    error=str(error),
                )
            )
        except Exception:
            logger.exception("Failed to persist LLM error metrics record")
