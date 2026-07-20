from __future__ import annotations

import json
import logging
import time
from typing import Any

from minimal_harness.agent.middleware import Middleware
from minimal_harness.types import AgentEnd, LLMEnd, ToolCall

from mh_gateway.monitoring.collector import get_collector

logger = logging.getLogger("orchestration.audit")


def _log_entry(data: dict[str, Any]) -> None:
    logger.info(json.dumps(data, ensure_ascii=False, default=str))


class AuditMiddleware(Middleware):
    def __init__(
        self,
        user_id: str,
        session_id: str,
        agent_id: str = "",
        scenario_id: str = "",
        provider: str = "",
        model: str = "",
        trace_id: str = "",
    ) -> None:
        self._user_id = user_id
        self._session_id = session_id
        self._agent_id = agent_id
        self._scenario_id = scenario_id
        self._provider = provider
        self._model = model
        self._trace_id = trace_id
        self._llm_start_ts: float | None = None

    def _base(self) -> dict[str, Any]:
        return {
            "user_id": self._user_id,
            "session_id": self._session_id,
            "agent_id": self._agent_id,
            "scenario_id": self._scenario_id,
            "trace_id": self._trace_id,
            "ts": round(time.time(), 3),
        }

    async def on_agent_start(self, user_input: Any) -> None:
        self._llm_start_ts = None
        entry = self._base()
        entry["event"] = "agent_start"
        entry["input"] = str(user_input)[:200]
        _log_entry(entry)

        collector = get_collector()
        if collector is not None:
            collector.agent_runs_total.inc(
                {"agent_id": self._agent_id, "status": "started"}
            )

    async def on_agent_end(self, event: AgentEnd) -> None:
        entry = self._base()
        entry["event"] = "agent_end"
        entry["time_taken"] = event.time_taken
        entry["error"] = event.error or ""
        _log_entry(entry)

        status = "error" if event.error else "ok"
        collector = get_collector()
        if collector is not None:
            collector.agent_runs_total.inc(
                {"agent_id": self._agent_id, "status": status}
            )

    async def on_llm_start(self, messages: list[dict[str, Any]], tools: Any) -> None:
        self._llm_start_ts = time.monotonic()

        tool_count = len(tools) if tools else 0
        message_count = len(messages)
        total_chars = 0
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        total_chars += len(part.get("text", "") or "")

        entry = self._base()
        entry["event"] = "llm_start"
        entry["provider"] = self._provider
        entry["model"] = self._model
        entry["tool_count"] = tool_count
        entry["message_count"] = message_count
        entry["total_chars"] = total_chars
        _log_entry(entry)

    async def on_llm_end(self, event: LLMEnd) -> None:
        duration_ms = 0.0
        if self._llm_start_ts is not None:
            duration_ms = round((time.monotonic() - self._llm_start_ts) * 1000, 2)
            self._llm_start_ts = None

        usage = event.usage
        prompt_tokens = usage.get("prompt_tokens", 0) if usage else 0
        completion_tokens = usage.get("completion_tokens", 0) if usage else 0

        entry = self._base()
        entry["event"] = "llm_end"
        entry["provider"] = self._provider
        entry["model"] = self._model
        entry["prompt_tokens"] = prompt_tokens
        entry["completion_tokens"] = completion_tokens
        entry["total_tokens"] = prompt_tokens + completion_tokens
        entry["duration_ms"] = duration_ms
        entry["error"] = event.error or ""
        _log_entry(entry)

        collector = get_collector()
        if collector is not None:
            labels = {"provider": self._provider, "model": self._model}
            collector.llm_requests_total.inc(
                {**labels, "status": "error" if event.error else "ok"}
            )
            collector.llm_tokens_total.inc({**labels, "type": "prompt"}, prompt_tokens)
            collector.llm_tokens_total.inc(
                {**labels, "type": "completion"}, completion_tokens
            )
            collector.llm_request_duration_ms.observe(labels, duration_ms)

    async def on_tool_start(self, tool_call: ToolCall) -> None:
        tool_name = tool_call.get("function", {}).get("name", "unknown")

        entry = self._base()
        entry["event"] = "tool_start"
        entry["tool_name"] = tool_name
        _log_entry(entry)

    async def on_tool_end(self, tool_call: ToolCall, result: Any) -> None:
        tool_name = tool_call.get("function", {}).get("name", "unknown")
        status = "error" if isinstance(result, Exception) else "ok"

        entry = self._base()
        entry["event"] = "tool_end"
        entry["tool_name"] = tool_name
        entry["result"] = str(result)[:100]
        entry["status"] = status
        _log_entry(entry)

        collector = get_collector()
        if collector is not None:
            collector.tool_calls_total.inc({"tool_name": tool_name, "status": status})

    async def on_tool_error(self, tool_call: ToolCall, error: Exception) -> None:
        tool_name = tool_call.get("function", {}).get("name", "unknown")

        entry = self._base()
        entry["event"] = "tool_error"
        entry["tool_name"] = tool_name
        entry["error"] = str(error)
        _log_entry(entry)

        collector = get_collector()
        if collector is not None:
            collector.tool_calls_total.inc({"tool_name": tool_name, "status": "error"})
