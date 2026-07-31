"""Metrics repository protocol and singleton accessor.

The gateway defines the *contract* (:class:`MetricsRepository`) and a
process-wide singleton accessor (:func:`get_metrics_repo` /
:func:`set_metrics_repo`) — mirroring the existing
``mh_gateway.monitoring.collector`` pattern.  The concrete storage
backend (SQLite, PostgreSQL, file, …) is supplied by the deployment
via ``create_app(..., lifespan_hooks=[...])``; the gateway itself
never sees a raw connection.

This lives *outside* :class:`~mh_gateway.app.GatewayAdapters`
deliberately: ``GatewayAdapters`` is frozen with ``slots=True``, so
deployments cannot add fields to it.  A separate singleton keeps the
new feature a pure additive change.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class LLMCallRecord:
    """One finished LLM chat call, captured by the metrics middleware."""

    ts: str  # ISO-8601 timestamp (UTC)
    user_id: str
    session_id: str
    agent_name: str
    scenario_id: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    duration_ms: float
    status: str  # "ok" | "error"
    error: str = ""


@dataclass
class ToolCallRecord:
    """One finished tool execution."""

    ts: str
    user_id: str
    session_id: str
    agent_name: str
    scenario_id: str
    tool_name: str
    status: str  # "ok" | "error"


@dataclass
class MetricsSummary:
    """Aggregated metrics for a date range, as consumed by the dashboard API."""

    llm_call_count: int = 0
    error_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    avg_duration_ms: float = 0.0
    top_scenes: list[dict[str, Any]] = field(default_factory=list)
    top_agents: list[dict[str, Any]] = field(default_factory=list)
    top_tools: list[dict[str, Any]] = field(default_factory=list)
    top_users: list[dict[str, Any]] = field(default_factory=list)
    model_perf: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "llm_call_count": self.llm_call_count,
            "error_count": self.error_count,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "avg_duration_ms": round(self.avg_duration_ms, 2),
            "top_scenes": self.top_scenes,
            "top_agents": self.top_agents,
            "top_tools": self.top_tools,
            "top_users": self.top_users,
            "model_perf": self.model_perf,
        }


class MetricsRepository(Protocol):
    """Persistent storage for usage metrics.

    Deployments implement this protocol with any backend.  The
    default reference implementation is :class:`InMemoryMetricsRepository`
    (used by tests); file/SQLite implementations ship in deployment
    packages (e.g. ``mh-local``).

    ``date_from`` / ``date_to`` are inclusive ``YYYY-MM-DD`` strings
    (local day boundaries are converted to UTC by the caller).
    """

    async def record_llm_call(self, record: LLMCallRecord) -> None: ...
    async def record_tool_call(self, record: ToolCallRecord) -> None: ...
    async def query_summary(
        self, date_from: str | None = None, date_to: str | None = None
    ) -> MetricsSummary: ...
    async def close(self) -> None: ...


# ── Singleton ────────────────────────────────────────────────────────────────

_REPO: MetricsRepository | None = None
_REPO_LOCK = threading.Lock()


def get_metrics_repo() -> MetricsRepository | None:
    """Return the process-wide metrics repository, or ``None`` if not set."""
    return _REPO


def set_metrics_repo(repo: MetricsRepository | None) -> None:
    """Set (or clear) the process-wide metrics repository."""
    global _REPO
    with _REPO_LOCK:
        _REPO = repo


# ── Reference implementation (in-memory, used by tests) ─────────────────────


class InMemoryMetricsRepository:
    """Thread-safe in-memory :class:`MetricsRepository`.

    Reference implementation / test double.  Deployment backends
    should implement the same protocol with real persistence.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._llm: list[LLMCallRecord] = []
        self._tools: list[ToolCallRecord] = []

    async def record_llm_call(self, record: LLMCallRecord) -> None:
        with self._lock:
            self._llm.append(record)

    async def record_tool_call(self, record: ToolCallRecord) -> None:
        with self._lock:
            self._tools.append(record)

    async def query_summary(
        self, date_from: str | None = None, date_to: str | None = None
    ) -> MetricsSummary:
        with self._lock:
            llm = self._llm
            tools = self._tools
        return _aggregate(llm, tools, date_from, date_to)

    async def close(self) -> None:
        return None


def _within_range(ts: str, date_from: str | None, date_to: str | None) -> bool:
    if not ts:
        return True
    day = ts[:10]
    if date_from and day < date_from:
        return False
    if date_to and day > date_to:
        return False
    return True


def _aggregate(
    llm: list[LLMCallRecord],
    tools: list[ToolCallRecord],
    date_from: str | None = None,
    date_to: str | None = None,
) -> MetricsSummary:
    """Pure aggregation shared by every backend implementation."""
    summary = MetricsSummary()

    scene_counts: dict[str, int] = {}
    agent_counts: dict[str, int] = {}
    user_counts: dict[str, int] = {}
    model_stats: dict[tuple[str, str], list[float]] = {}
    tool_counts: dict[str, int] = {}

    duration_sum = 0.0

    for r in llm:
        if not _within_range(r.ts, date_from, date_to):
            continue
        summary.llm_call_count += 1
        summary.prompt_tokens += r.prompt_tokens
        summary.completion_tokens += r.completion_tokens
        duration_sum += r.duration_ms
        if r.status == "error":
            summary.error_count += 1

        if r.scenario_id:
            scene_counts[r.scenario_id] = scene_counts.get(r.scenario_id, 0) + 1
        if r.agent_name:
            agent_counts[r.agent_name] = agent_counts.get(r.agent_name, 0) + 1
        if r.user_id:
            user_counts[r.user_id] = user_counts.get(r.user_id, 0) + 1

        key = (r.provider or "unknown", r.model or "unknown")
        model_stats.setdefault(key, []).append(r.duration_ms)

    for r in tools:
        if not _within_range(r.ts, date_from, date_to):
            continue
        if r.tool_name:
            tool_counts[r.tool_name] = tool_counts.get(r.tool_name, 0) + 1

    summary.total_tokens = summary.prompt_tokens + summary.completion_tokens
    if summary.llm_call_count:
        summary.avg_duration_ms = duration_sum / summary.llm_call_count

    summary.top_scenes = _top_n(scene_counts, 3)
    summary.top_agents = _top_n(agent_counts, 3)
    summary.top_tools = _top_n(tool_counts, 3)
    summary.top_users = _top_n(user_counts, 5)

    for (provider, model), durs in model_stats.items():
        n = len(durs)
        total = sum(durs)
        sorted_d = sorted(durs)
        summary.model_perf.append(
            {
                "provider": provider,
                "model": model,
                "call_count": n,
                "avg_duration_ms": round(total / n, 2),
                "p50_ms": sorted_d[n // 2],
                "p95_ms": sorted_d[min(int(n * 0.95), n - 1)],
            }
        )
    summary.model_perf.sort(key=lambda m: -m["call_count"])
    return summary


def _top_n(counts: dict[str, int], n: int) -> list[dict[str, Any]]:
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"name": k, "count": v} for k, v in ranked[:n]]


__all__ = [
    "InMemoryMetricsRepository",
    "LLMCallRecord",
    "MetricsRepository",
    "MetricsSummary",
    "ToolCallRecord",
    "get_metrics_repo",
    "set_metrics_repo",
]
