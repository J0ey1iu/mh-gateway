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

The protocol is split into three query surfaces so external backends
can push work down (SQL ``GROUP BY``/``LIMIT``/``OFFSET``) instead of
the gateway ever pulling full raw data:

* ``query_summary`` — scalar totals for the dashboard cards.
* ``query_ranking`` — **server-side paginated** rankings (full list,
  not a fixed top-N).
* ``query_trend`` — daily time series for charts.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Protocol

KIND_SCENES = "scenes"
KIND_AGENTS = "agents"
KIND_TOOLS = "tools"
KIND_USERS = "users"

RANKING_KINDS = (KIND_SCENES, KIND_AGENTS, KIND_TOOLS, KIND_USERS)


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
class RankedItem:
    """One ranked entity with usage + quality stats (per kind)."""

    key: str  # raw identifier: scenario_id / agent / tool / user id
    count: int = 0
    error_count: int = 0
    error_rate: float = 0.0
    avg_duration_ms: float = 0.0
    p50_ms: float = 0.0
    p95_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.key,
            "count": self.count,
            "error_count": self.error_count,
            "error_rate": round(self.error_rate, 4),
            "avg_duration_ms": round(self.avg_duration_ms, 2),
            "p50_ms": round(self.p50_ms, 2),
            "p95_ms": round(self.p95_ms, 2),
        }


@dataclass
class RankingPage:
    """One page of a ranking, sliced server-side."""

    items: list[RankedItem]
    total: int


@dataclass
class TrendPoint:
    """One day of aggregated usage, for the trend charts."""

    date: str  # YYYY-MM-DD
    llm_calls: int = 0
    error_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    avg_duration_ms: float = 0.0
    active_users: int = 0
    active_sessions: int = 0
    tool_calls: int = 0
    tool_error_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "llm_calls": self.llm_calls,
            "error_count": self.error_count,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "avg_duration_ms": round(self.avg_duration_ms, 2),
            "active_users": self.active_users,
            "active_sessions": self.active_sessions,
            "tool_calls": self.tool_calls,
            "tool_error_count": self.tool_error_count,
        }


@dataclass
class MetricsSummary:
    """Aggregated metrics for a date range, as consumed by the dashboard API."""

    llm_call_count: int = 0
    error_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    avg_duration_ms: float = 0.0
    error_rate: float = 0.0
    avg_tokens_per_call: float = 0.0
    active_user_count: int = 0
    session_count: int = 0
    avg_calls_per_session: float = 0.0
    tool_call_count: int = 0
    tool_error_count: int = 0
    model_perf: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "llm_call_count": self.llm_call_count,
            "error_count": self.error_count,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "avg_duration_ms": round(self.avg_duration_ms, 2),
            "error_rate": round(self.error_rate, 4),
            "avg_tokens_per_call": round(self.avg_tokens_per_call, 2),
            "active_user_count": self.active_user_count,
            "session_count": self.session_count,
            "avg_calls_per_session": round(self.avg_calls_per_session, 2),
            "tool_call_count": self.tool_call_count,
            "tool_error_count": self.tool_error_count,
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

    ``query_ranking`` must apply pagination **itself** (slice or
    ``LIMIT``/``OFFSET``) — the gateway never fetches a full ranking
    from the backend.  ``query_trend`` returns one point per day in
    the requested range, zero-filled for days without records.
    """

    async def record_llm_call(self, record: LLMCallRecord) -> None: ...
    async def record_tool_call(self, record: ToolCallRecord) -> None: ...
    async def query_summary(
        self, date_from: str | None = None, date_to: str | None = None
    ) -> MetricsSummary: ...
    async def query_ranking(
        self,
        kind: str,
        date_from: str | None = None,
        date_to: str | None = None,
        page: int = 1,
        page_size: int = 10,
    ) -> RankingPage: ...
    async def query_trend(
        self, date_from: str | None = None, date_to: str | None = None
    ) -> list[TrendPoint]: ...
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

    async def query_ranking(
        self,
        kind: str,
        date_from: str | None = None,
        date_to: str | None = None,
        page: int = 1,
        page_size: int = 10,
    ) -> RankingPage:
        if kind not in RANKING_KINDS:
            raise ValueError(f"unknown ranking kind: {kind!r}")
        with self._lock:
            llm = self._llm
            tools = self._tools
        return _paginate(
            _rank_items(kind, llm, tools, date_from, date_to), page, page_size
        )

    async def query_trend(
        self, date_from: str | None = None, date_to: str | None = None
    ) -> list[TrendPoint]:
        with self._lock:
            llm = self._llm
            tools = self._tools
        return _daily_points(llm, tools, date_from, date_to)

    async def close(self) -> None:
        return None


# ── Pure aggregation helpers (shared by every backend) ──────────────────────


def _within_range(ts: str, date_from: str | None, date_to: str | None) -> bool:
    if not ts:
        return True
    day = ts[:10]
    if date_from and day < date_from:
        return False
    if date_to and day > date_to:
        return False
    return True


def _iter_days(date_from: str, date_to: str) -> list[str]:
    from datetime import date, timedelta

    try:
        start = date.fromisoformat(date_from)
        end = date.fromisoformat(date_to)
    except ValueError:
        return [date_from]
    days: list[str] = []
    d = start
    while d <= end:
        days.append(d.isoformat())
        d += timedelta(days=1)
    return days


def _aggregate(
    llm: list[LLMCallRecord],
    tools: list[ToolCallRecord],
    date_from: str | None = None,
    date_to: str | None = None,
) -> MetricsSummary:
    """Pure aggregation shared by every backend implementation."""
    summary = MetricsSummary()

    model_stats: dict[tuple[str, str], list[float]] = {}
    user_ids: set[str] = set()
    session_ids: set[str] = set()
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
        if r.user_id:
            user_ids.add(r.user_id)
        if r.session_id:
            session_ids.add(r.session_id)

        key = (r.provider or "unknown", r.model or "unknown")
        model_stats.setdefault(key, []).append(r.duration_ms)

    for r in tools:
        if not _within_range(r.ts, date_from, date_to):
            continue
        summary.tool_call_count += 1
        if r.status == "error":
            summary.tool_error_count += 1

    summary.total_tokens = summary.prompt_tokens + summary.completion_tokens
    if summary.llm_call_count:
        summary.avg_duration_ms = duration_sum / summary.llm_call_count
        summary.error_rate = summary.error_count / summary.llm_call_count
        summary.avg_tokens_per_call = summary.total_tokens / summary.llm_call_count
    summary.active_user_count = len(user_ids)
    summary.session_count = len(session_ids)
    if summary.session_count:
        summary.avg_calls_per_session = summary.llm_call_count / summary.session_count

    for (provider, model), durs in model_stats.items():
        n = len(durs)
        sorted_d = sorted(durs)
        summary.model_perf.append(
            {
                "provider": provider,
                "model": model,
                "call_count": n,
                "avg_duration_ms": round(sum(durs) / n, 2),
                "p50_ms": _percentile(sorted_d, 0.50),
                "p95_ms": _percentile(sorted_d, 0.95),
            }
        )
    summary.model_perf.sort(key=lambda m: -m["call_count"])
    return summary


def _percentile(sorted_durations: list[float], pct: float) -> float:
    if not sorted_durations:
        return 0.0
    return sorted_durations[
        min(int(len(sorted_durations) * pct), len(sorted_durations) - 1)
    ]


def _rank_items(
    kind: str,
    llm: list[LLMCallRecord],
    tools: list[ToolCallRecord],
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[RankedItem]:
    """Full ranking (sorted, unpaginated) for one kind.

    ``scenes`` / ``agents`` / ``users`` aggregate LLM records; ``tools``
    aggregate tool records (which carry no duration).
    """
    if kind == KIND_TOOLS:
        counts: dict[str, int] = {}
        errors: dict[str, int] = {}
        for r in tools:
            if not _within_range(r.ts, date_from, date_to):
                continue
            if not r.tool_name:
                continue
            counts[r.tool_name] = counts.get(r.tool_name, 0) + 1
            if r.status == "error":
                errors[r.tool_name] = errors.get(r.tool_name, 0) + 1
        items = [
            _make_ranked_item(key, counts[key], errors.get(key, 0), [])
            for key in counts
        ]
    else:
        key_field = {
            KIND_SCENES: "scenario_id",
            KIND_AGENTS: "agent_name",
            KIND_USERS: "user_id",
        }[kind]
        counts = {}
        errors = {}
        durations: dict[str, list[float]] = {}
        for r in llm:
            if not _within_range(r.ts, date_from, date_to):
                continue
            key = getattr(r, key_field)
            if not key:
                continue
            counts[key] = counts.get(key, 0) + 1
            if r.status == "error":
                errors[key] = errors.get(key, 0) + 1
            durations.setdefault(key, []).append(r.duration_ms)
        items = [
            _make_ranked_item(key, counts[key], errors.get(key, 0), durations[key])
            for key in counts
        ]

    items.sort(key=lambda i: (-i.count, i.key))
    return items


def _make_ranked_item(
    key: str, count: int, error_count: int, durations: list[float]
) -> RankedItem:
    item = RankedItem(key=key, count=count, error_count=error_count)
    if count:
        item.error_rate = error_count / count
    if durations:
        sorted_d = sorted(durations)
        item.avg_duration_ms = sum(durations) / len(durations)
        item.p50_ms = _percentile(sorted_d, 0.50)
        item.p95_ms = _percentile(sorted_d, 0.95)
    return item


def _paginate(
    items: list[RankedItem], page: int = 1, page_size: int = 10
) -> RankingPage:
    """Slice a ranked list server-side.  ``page`` is 1-based."""
    total = len(items)
    start = (page - 1) * page_size
    end = start + page_size
    return RankingPage(items=items[start:end], total=total)


def _daily_points(
    llm: list[LLMCallRecord],
    tools: list[ToolCallRecord],
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[TrendPoint]:
    """One :class:`TrendPoint` per day in range, zero-filled for empty days."""
    day_llm: dict[str, list[LLMCallRecord]] = {}
    day_tools: dict[str, list[ToolCallRecord]] = {}
    for r in llm:
        if not _within_range(r.ts, date_from, date_to):
            continue
        day_llm.setdefault(r.ts[:10], []).append(r)
    for r in tools:
        if not _within_range(r.ts, date_from, date_to):
            continue
        day_tools.setdefault(r.ts[:10], []).append(r)

    days = sorted(set(day_llm) | set(day_tools))
    if not days:
        return []
    start = date_from or days[0]
    end = date_to or days[-1]
    if start > end:
        return []

    users: dict[str, set[str]] = {}
    sessions: dict[str, set[str]] = {}

    points: list[TrendPoint] = []
    for day in _iter_days(start, end):
        p = TrendPoint(date=day)
        for r in day_llm.get(day, []):
            p.llm_calls += 1
            if r.status == "error":
                p.error_count += 1
            p.prompt_tokens += r.prompt_tokens
            p.completion_tokens += r.completion_tokens
            p.avg_duration_ms += r.duration_ms
            if r.user_id:
                users.setdefault(day, set()).add(r.user_id)
            if r.session_id:
                sessions.setdefault(day, set()).add(r.session_id)
        if p.llm_calls:
            p.avg_duration_ms /= p.llm_calls
        p.total_tokens = p.prompt_tokens + p.completion_tokens
        p.active_users = len(users.get(day, set()))
        p.active_sessions = len(sessions.get(day, set()))
        for t in day_tools.get(day, []):
            p.tool_calls += 1
            if t.status == "error":
                p.tool_error_count += 1
        points.append(p)
    return points


__all__ = [
    "InMemoryMetricsRepository",
    "KIND_AGENTS",
    "KIND_SCENES",
    "KIND_TOOLS",
    "KIND_USERS",
    "LLMCallRecord",
    "MetricsRepository",
    "MetricsSummary",
    "RANKING_KINDS",
    "RankedItem",
    "RankingPage",
    "ToolCallRecord",
    "TrendPoint",
    "get_metrics_repo",
    "set_metrics_repo",
]
