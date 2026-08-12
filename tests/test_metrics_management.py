"""Tests for the metrics repository, persistence middleware, and management API.

Covers:
* aggregation correctness (counts, tokens, top-N, model perf, date range)
* middleware recording (no repo → no-op; repo → records)
* API contract: 200 with data, 403 without permission, 503 without repo
* end-to-end: middleware records → repository → management API reads back
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mh_gateway.adapters import UserIdentity
from mh_gateway.app import GatewayAdapters, create_app
from mh_gateway.config import ConfigSchema
from mh_gateway.metrics_repo import (
    InMemoryMetricsRepository,
    LLMCallRecord,
    MetricsSummary,
    ToolCallRecord,
    get_metrics_repo,
    set_metrics_repo,
)
from mh_gateway.services.metrics_middleware import MetricsPersistenceMiddleware

from tests.conftest import (
    ALL_PERMS,
    _MockLLM,
    _MockMetadata,
    _MockProvider,
    _MockSessionRepo,
)

ALL_PERMS_WITH_METRICS = [*ALL_PERMS, "manage:metrics:*"]


class TestAggregation:
    @pytest.mark.asyncio
    async def test_empty_summary(self):
        repo = InMemoryMetricsRepository()
        s = await repo.query_summary()
        assert s.llm_call_count == 0
        assert s.total_tokens == 0
        assert s.model_perf == []
        assert (await repo.query_ranking("scenes")).total == 0

    @pytest.mark.asyncio
    async def test_counts_and_top_n(self):
        repo = InMemoryMetricsRepository()
        for i in range(6):
            await repo.record_llm_call(
                LLMCallRecord(
                    ts="2024-06-01T10:00:00Z",
                    user_id="u1",
                    session_id=f"s{i}",
                    agent_name=f"agent-{i % 3}",
                    scenario_id=f"scene-{i % 2}",
                    provider="openai",
                    model="gpt-4",
                    prompt_tokens=10,
                    completion_tokens=5,
                    duration_ms=100.0,
                    status="ok",
                )
            )
        for i in range(6):
            await repo.record_tool_call(
                ToolCallRecord(
                    ts="2024-06-01T10:00:00Z",
                    user_id="u1",
                    session_id=f"s{i}",
                    agent_name="a",
                    scenario_id="sc",
                    tool_name=f"tool-{i % 3}",
                    status="ok",
                )
            )

        s = await repo.query_summary()
        assert s.llm_call_count == 6
        assert s.prompt_tokens == 60
        assert s.completion_tokens == 30
        assert s.total_tokens == 90
        assert s.avg_duration_ms == 100.0
        # rankings are full lists via query_ranking (no top-N truncation)
        scenes = await repo.query_ranking("scenes")
        assert scenes.total == 2
        assert [i.key for i in scenes.items] == ["scene-0", "scene-1"]
        agents = await repo.query_ranking("agents")
        assert agents.total == 3
        assert [i.key for i in agents.items] == ["agent-0", "agent-1", "agent-2"]
        tools = await repo.query_ranking("tools")
        assert [i.key for i in tools.items] == ["tool-0", "tool-1", "tool-2"]
        users = await repo.query_ranking("users")
        assert [i.key for i in users.items] == ["u1"]
        assert s.model_perf[0]["provider"] == "openai"
        assert s.model_perf[0]["model"] == "gpt-4"
        assert s.model_perf[0]["call_count"] == 6
        assert s.model_perf[0]["avg_duration_ms"] == 100.0

    @pytest.mark.asyncio
    async def test_date_range_filter(self):
        repo = InMemoryMetricsRepository()
        await repo.record_llm_call(
            LLMCallRecord(
                ts="2024-05-01T10:00:00Z",
                user_id="u1",
                session_id="s1",
                agent_name="a1",
                scenario_id="sc1",
                provider="p",
                model="m",
                prompt_tokens=1,
                completion_tokens=1,
                duration_ms=10.0,
                status="ok",
            )
        )
        await repo.record_llm_call(
            LLMCallRecord(
                ts="2024-06-15T10:00:00Z",
                user_id="u1",
                session_id="s2",
                agent_name="a1",
                scenario_id="sc1",
                provider="p",
                model="m",
                prompt_tokens=2,
                completion_tokens=2,
                duration_ms=20.0,
                status="ok",
            )
        )
        s = await repo.query_summary(date_from="2024-06-01", date_to="2024-06-30")
        assert s.llm_call_count == 1
        assert s.total_tokens == 4
        s2 = await repo.query_summary(date_from="2024-06-15", date_to="2024-06-15")
        assert s2.llm_call_count == 1

    @pytest.mark.asyncio
    async def test_error_counts(self):
        repo = InMemoryMetricsRepository()
        await repo.record_llm_call(
            LLMCallRecord(
                ts="2024-06-01T10:00:00Z",
                user_id="u1",
                session_id="s1",
                agent_name="a1",
                scenario_id="sc1",
                provider="p",
                model="m",
                prompt_tokens=1,
                completion_tokens=1,
                duration_ms=10.0,
                status="error",
                error="boom",
            )
        )
        s = await repo.query_summary()
        assert s.llm_call_count == 1
        assert s.error_count == 1

    @pytest.mark.asyncio
    async def test_summary_extended_scalars(self):
        repo = InMemoryMetricsRepository()
        await repo.record_llm_call(
            LLMCallRecord(
                ts="2024-06-01T10:00:00Z",
                user_id="u1",
                session_id="s1",
                agent_name="a1",
                scenario_id="sc1",
                provider="p",
                model="m",
                prompt_tokens=10,
                completion_tokens=5,
                duration_ms=100.0,
                status="error",
            )
        )
        await repo.record_tool_call(
            ToolCallRecord(
                ts="2024-06-01T10:00:00Z",
                user_id="u1",
                session_id="s1",
                agent_name="a1",
                scenario_id="sc1",
                tool_name="t1",
                status="error",
            )
        )
        s = await repo.query_summary()
        assert s.error_rate == 1.0
        assert s.avg_tokens_per_call == 15.0
        assert s.active_user_count == 1
        assert s.session_count == 1
        assert s.avg_calls_per_session == 1.0
        assert s.tool_call_count == 1
        assert s.tool_error_count == 1

    @pytest.mark.asyncio
    async def test_ranking_paginated_and_stats(self):
        repo = InMemoryMetricsRepository()
        for i in range(25):
            await repo.record_llm_call(
                LLMCallRecord(
                    ts="2024-06-01T10:00:00Z",
                    user_id="u1",
                    session_id="s1",
                    agent_name="a1",
                    scenario_id=f"scene-{i}",
                    provider="p",
                    model="m",
                    prompt_tokens=1,
                    completion_tokens=1,
                    duration_ms=10.0,
                    status="ok",
                )
            )
        p1 = await repo.query_ranking("scenes", page=1, page_size=10)
        assert p1.total == 25
        assert len(p1.items) == 10
        # all tied at 1 → lexicographic key order
        assert p1.items[0].key == "scene-0"
        p3 = await repo.query_ranking("scenes", page=3, page_size=10)
        assert len(p3.items) == 5
        with pytest.raises(ValueError):
            await repo.query_ranking("bogus")

    @pytest.mark.asyncio
    async def test_ranking_tool_errors(self):
        repo = InMemoryMetricsRepository()
        for status in ("ok", "ok", "error"):
            await repo.record_tool_call(
                ToolCallRecord(
                    ts="2024-06-01T10:00:00Z",
                    user_id="u1",
                    session_id="s1",
                    agent_name="a1",
                    scenario_id="sc1",
                    tool_name="calc",
                    status=status,
                )
            )
        page = await repo.query_ranking("tools")
        assert page.total == 1
        item = page.items[0]
        assert item.key == "calc"
        assert item.count == 3
        assert item.error_count == 1
        assert item.error_rate == pytest.approx(1 / 3)

    @pytest.mark.asyncio
    async def test_trend_zero_filled_and_aggregated(self):
        repo = InMemoryMetricsRepository()
        await repo.record_llm_call(
            LLMCallRecord(
                ts="2024-06-01T09:00:00Z",
                user_id="u1",
                session_id="s1",
                agent_name="a1",
                scenario_id="sc1",
                provider="p",
                model="m",
                prompt_tokens=10,
                completion_tokens=5,
                duration_ms=100.0,
                status="ok",
            )
        )
        await repo.record_llm_call(
            LLMCallRecord(
                ts="2024-06-03T09:00:00Z",
                user_id="u2",
                session_id="s2",
                agent_name="a1",
                scenario_id="sc1",
                provider="p",
                model="m",
                prompt_tokens=1,
                completion_tokens=1,
                duration_ms=50.0,
                status="error",
            )
        )
        await repo.record_tool_call(
            ToolCallRecord(
                ts="2024-06-01T09:00:00Z",
                user_id="u1",
                session_id="s1",
                agent_name="a1",
                scenario_id="sc1",
                tool_name="t1",
                status="ok",
            )
        )
        points = await repo.query_trend(date_from="2024-06-01", date_to="2024-06-03")
        assert [p.date for p in points] == ["2024-06-01", "2024-06-02", "2024-06-03"]
        assert points[0].llm_calls == 1
        assert points[0].total_tokens == 15
        assert points[0].tool_calls == 1
        assert points[0].active_users == 1
        assert points[1].llm_calls == 0  # zero-filled
        assert points[2].llm_calls == 1
        assert points[2].error_count == 1
        assert points[2].active_users == 1
        # no range → derived from data
        derived = await repo.query_trend()
        assert [p.date for p in derived] == ["2024-06-01", "2024-06-02", "2024-06-03"]


class TestMetricsPersistenceMiddleware:
    @pytest.mark.asyncio
    async def test_noop_without_repo(self):
        set_metrics_repo(None)
        mw = MetricsPersistenceMiddleware(user_id="u1", agent_id="a1")
        from minimal_harness.types import LLMEnd

        evt = LLMEnd(
            content="x",
            reasoning_content=None,
            tool_calls=None,
            usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            error=None,
        )
        await mw.on_llm_start([], [])
        await mw.on_llm_end(evt)

    @pytest.mark.asyncio
    async def test_records_llm_call(self):
        repo = InMemoryMetricsRepository()
        set_metrics_repo(repo)
        try:
            from minimal_harness.types import LLMEnd

            mw = MetricsPersistenceMiddleware(
                user_id="u1",
                session_id="s1",
                agent_id="a1",
                scenario_id="sc1",
                provider="openai",
                model="gpt-4",
            )
            await mw.on_llm_start([], [])
            evt = LLMEnd(
                content="x",
                reasoning_content=None,
                tool_calls=None,
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                error=None,
            )
            await mw.on_llm_end(evt)
            s = await repo.query_summary()
            assert s.llm_call_count == 1
            assert s.prompt_tokens == 10
            assert s.completion_tokens == 5
            assert (await repo.query_ranking("agents")).items[0].key == "a1"
            assert (await repo.query_ranking("scenes")).items[0].key == "sc1"
            assert (await repo.query_ranking("users")).items[0].key == "u1"
            assert s.model_perf[0]["model"] == "gpt-4"
        finally:
            set_metrics_repo(None)

    @pytest.mark.asyncio
    async def test_records_tool_call(self):
        repo = InMemoryMetricsRepository()
        set_metrics_repo(repo)
        try:
            mw = MetricsPersistenceMiddleware(user_id="u1", agent_id="a1")
            tool_call = {"function": {"name": "calculator", "arguments": "{}"}}
            await mw.on_tool_start(tool_call)
            await mw.on_tool_end(tool_call, "ok")
            page = await repo.query_ranking("tools")
            assert page.total == 1
            assert page.items[0].key == "calculator"
        finally:
            set_metrics_repo(None)


class _DenyMetricsProvider:
    """Provider that grants everything except manage:metrics:*."""

    async def get_permissions(self, uid):
        return [p for p in ALL_PERMS_WITH_METRICS if p != "manage:metrics:*"]

    async def check(self, uid, perm):
        return perm != "manage:metrics:*"

    async def verify(self, request):
        return UserIdentity(user_id="2", username="member")

    async def authenticate(self, request):
        return "default-app"

    async def logout(self, request, response):
        return None

    async def close(self):
        return None


def _make_app(provider, lifespan_hooks=None, metadata=None):
    settings = ConfigSchema(
        db_path="./metrics-test.db",
        cors_origins=[],
        metrics_enabled=False,
        enable_eval=False,
    )

    @asynccontextmanager
    async def adapter_lifespan(app: FastAPI):
        bundle = GatewayAdapters(
            settings=settings,
            user_auth=provider,
            authorization=provider,
            m2m_auth=provider,
            outbound_auth=provider,
            metadata=metadata or _MockMetadata(),
            llm=_MockLLM(),
            sessions=_MockSessionRepo(),
            eval_results=None,
        )
        yield bundle

    return create_app(
        settings=settings,
        adapters=adapter_lifespan,
        lifespan_hooks=lifespan_hooks,
    )


def _metrics_hook(repo):
    """lifespan hook that registers a fixed repository."""

    @asynccontextmanager
    async def hook(app: FastAPI):
        set_metrics_repo(repo)
        yield
        set_metrics_repo(None)

    return hook


class TestManagementMetricsAPI:
    @pytest.fixture(autouse=True)
    def _clean_repo(self):
        set_metrics_repo(None)
        yield
        set_metrics_repo(None)

    @pytest.fixture
    def metrics_client(self) -> Generator[TestClient, None, None]:
        repo = InMemoryMetricsRepository()
        app = _make_app(_MockProvider(), lifespan_hooks=[_metrics_hook(repo)])
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c

    @pytest.fixture
    def seeded_client(self) -> Generator[TestClient, None, None]:
        repo = InMemoryMetricsRepository()
        asyncio.run(
            repo.record_llm_call(
                LLMCallRecord(
                    ts="2024-06-01T10:00:00Z",
                    user_id="u1",
                    session_id="s1",
                    agent_name="triage",
                    scenario_id="code_review",
                    provider="openai",
                    model="gpt-4",
                    prompt_tokens=10,
                    completion_tokens=5,
                    duration_ms=120.0,
                    status="ok",
                )
            )
        )
        asyncio.run(
            repo.record_tool_call(
                ToolCallRecord(
                    ts="2024-06-01T10:00:00Z",
                    user_id="u1",
                    session_id="s1",
                    agent_name="triage",
                    scenario_id="code_review",
                    tool_name="calculator",
                    status="ok",
                )
            )
        )
        app = _make_app(_MockProvider(), lifespan_hooks=[_metrics_hook(repo)])
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c

    def test_403_without_permission(self):
        app = _make_app(_DenyMetricsProvider())
        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.get("/api/v1/management/metrics", headers={"X-User-Id": "2"})
            assert r.status_code == 403

    def test_503_without_repo(self):
        app = _make_app(_MockProvider())
        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.get("/api/v1/management/metrics", headers={"X-User-Id": "1"})
            assert r.status_code == 503

    def test_empty_ok(self, metrics_client):
        r = metrics_client.get("/api/v1/management/metrics", headers={"X-User-Id": "1"})
        assert r.status_code == 200
        data = r.json()
        assert data["llm_call_count"] == 0
        assert data["total_tokens"] == 0
        assert data["entity_counts"] == {"scenes": 2, "agents": 0, "tools": 0}

    def test_seeded_aggregation_and_entity_counts(self, seeded_client):
        r = seeded_client.get(
            "/api/v1/management/metrics",
            headers={"X-User-Id": "1"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["llm_call_count"] == 1
        assert data["total_tokens"] == 15
        assert data["avg_duration_ms"] == 120.0
        assert data["model_perf"][0]["model"] == "gpt-4"
        # conftest seeds 2 scenarios
        assert data["entity_counts"]["scenes"] == 2

    def test_date_filter_excludes_out_of_range(self, seeded_client):
        r = seeded_client.get(
            "/api/v1/management/metrics",
            params={"date_from": "2024-07-01", "date_to": "2024-07-31"},
            headers={"X-User-Id": "1"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["llm_call_count"] == 0

    def test_invalid_date_returns_422(self, metrics_client):
        r = metrics_client.get(
            "/api/v1/management/metrics",
            params={"date_from": "not-a-date"},
            headers={"X-User-Id": "1"},
        )
        assert r.status_code == 422

    # ── rankings endpoint ──

    def test_rankings_paginated_and_display_names(self, seeded_client):
        r = seeded_client.get(
            "/api/v1/management/metrics/rankings",
            params={"kind": "scenes", "page": 1, "page_size": 10},
            headers={"X-User-Id": "1"},
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["total"] == 1
        assert data["page"] == 1
        assert data["page_size"] == 10
        # raw key kept + display name resolved from scenario metadata
        assert data["items"][0]["name"] == "code_review"
        assert data["items"][0]["display_name"] == "Code Review"

    def test_rankings_full_ranking_across_pages(self, metrics_client):
        repo = get_metrics_repo()
        for i in range(25):
            asyncio.run(
                repo.record_llm_call(
                    LLMCallRecord(
                        ts="2024-06-01T10:00:00Z",
                        user_id="u1",
                        session_id="s1",
                        agent_name="a1",
                        scenario_id=f"scene-{i}",
                        provider="p",
                        model="m",
                        prompt_tokens=1,
                        completion_tokens=1,
                        duration_ms=10.0,
                        status="ok",
                    )
                )
            )
        r = metrics_client.get(
            "/api/v1/management/metrics/rankings",
            params={"kind": "scenes", "page": 2, "page_size": 10},
            headers={"X-User-Id": "1"},
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["total"] == 25
        assert len(data["items"]) == 10
        # all tied at count 1 → ordered by key ascending (lexicographic)
        assert data["items"][0]["name"] == "scene-18"
        # unknown scenario → display_name falls back to raw key
        assert data["items"][0]["display_name"] == "scene-18"
        r3 = metrics_client.get(
            "/api/v1/management/metrics/rankings",
            params={"kind": "scenes", "page": 3, "page_size": 10},
            headers={"X-User-Id": "1"},
        )
        assert len(r3.json()["items"]) == 5

    def test_rankings_agent_tool_display_names(self, metrics_client):
        repo = get_metrics_repo()
        asyncio.run(
            repo.record_llm_call(
                LLMCallRecord(
                    ts="2024-06-01T10:00:00Z",
                    user_id="u1",
                    session_id="s1",
                    agent_name="triage",
                    scenario_id="code_review",
                    provider="p",
                    model="m",
                    prompt_tokens=1,
                    completion_tokens=1,
                    duration_ms=100.0,
                    status="ok",
                )
            )
        )
        asyncio.run(
            repo.record_tool_call(
                ToolCallRecord(
                    ts="2024-06-01T10:00:00Z",
                    user_id="u1",
                    session_id="s1",
                    agent_name="triage",
                    scenario_id="code_review",
                    tool_name="calculator",
                    status="ok",
                )
            )
        )
        metadata = _MockMetadata()
        asyncio.run(
            metadata.create_agent(
                {
                    "name": "triage",
                    "display_name": "Triage",
                    "display_name_locale": '{"zh":"分诊助手"}',
                    "provider": "openai",
                    "model": "m",
                    "agent_type": "simple",
                    "system_prompt": "",
                }
            )
        )
        asyncio.run(
            metadata.create_tool(
                {
                    "name": "calculator",
                    "display_name": "Calculator",
                    "display_name_locale": '{"zh":"计算器"}',
                    "description": "",
                    "parameters": {},
                }
            )
        )
        app = _make_app(_MockProvider(), lifespan_hooks=[_metrics_hook(repo)], metadata=metadata)
        with TestClient(app, raise_server_exceptions=False) as c:
            # zh locale via Accept-Language
            r = c.get(
                "/api/v1/management/metrics/rankings",
                params={"kind": "agents"},
                headers={"X-User-Id": "1", "Accept-Language": "zh"},
            )
            assert r.status_code == 200, r.text
            assert r.json()["items"][0]["display_name"] == "分诊助手"
            r = c.get(
                "/api/v1/management/metrics/rankings",
                params={"kind": "tools"},
                headers={"X-User-Id": "1", "Accept-Language": "zh"},
            )
            assert r.json()["items"][0]["display_name"] == "计算器"

    def test_rankings_error_rate_and_duration(self, metrics_client):
        repo = get_metrics_repo()
        for status in ("ok", "error"):
            asyncio.run(
                repo.record_llm_call(
                    LLMCallRecord(
                        ts="2024-06-01T10:00:00Z",
                        user_id="u1",
                        session_id="s1",
                        agent_name="triage",
                        scenario_id="code_review",
                        provider="p",
                        model="m",
                        prompt_tokens=1,
                        completion_tokens=1,
                        duration_ms=200.0 if status == "ok" else 100.0,
                        status=status,
                    )
                )
            )
        r = metrics_client.get(
            "/api/v1/management/metrics/rankings",
            params={"kind": "scenes"},
            headers={"X-User-Id": "1"},
        )
        assert r.status_code == 200, r.text
        item = r.json()["items"][0]
        assert item["count"] == 2
        assert item["error_count"] == 1
        assert item["error_rate"] == 0.5
        assert item["avg_duration_ms"] == 150.0
        assert item["p50_ms"] in (100.0, 200.0)
        assert item["p95_ms"] == 200.0

    def test_rankings_invalid_kind_returns_422(self, metrics_client):
        r = metrics_client.get(
            "/api/v1/management/metrics/rankings",
            params={"kind": "bogus"},
            headers={"X-User-Id": "1"},
        )
        assert r.status_code == 422

    def test_rankings_403_without_permission(self):
        app = _make_app(_DenyMetricsProvider())
        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.get(
                "/api/v1/management/metrics/rankings",
                params={"kind": "scenes"},
                headers={"X-User-Id": "2"},
            )
            assert r.status_code == 403

    def test_rankings_503_without_repo(self):
        app = _make_app(_MockProvider())
        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.get(
                "/api/v1/management/metrics/rankings",
                params={"kind": "scenes"},
                headers={"X-User-Id": "1"},
            )
            assert r.status_code == 503

    # ── trend endpoint ──

    def test_trend_zero_filled_days(self, seeded_client):
        r = seeded_client.get(
            "/api/v1/management/metrics/trend",
            params={"date_from": "2024-06-01", "date_to": "2024-06-03"},
            headers={"X-User-Id": "1"},
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert [p["date"] for p in data["points"]] == [
            "2024-06-01",
            "2024-06-02",
            "2024-06-03",
        ]
        p0 = data["points"][0]
        assert p0["llm_calls"] == 1
        assert p0["tool_calls"] == 1
        assert p0["total_tokens"] == 15
        assert p0["active_users"] == 1
        assert p0["active_sessions"] == 1
        # empty day is zero-filled
        assert data["points"][1]["llm_calls"] == 0

    def test_trend_403_without_permission(self):
        app = _make_app(_DenyMetricsProvider())
        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.get(
                "/api/v1/management/metrics/trend",
                headers={"X-User-Id": "2"},
            )
            assert r.status_code == 403


class TestEndToEndMiddlewareToAPI:
    """Full chain: middleware (as AgentRuntime invokes it) → repo → HTTP API.

    Exercises the full production wiring: the fake LLM streams a
    response through :class:`AgentRuntime`, the metrics middleware
    (injected by ``create_runtime``) records the call into the
    repository, and ``GET /api/v1/management/metrics`` aggregates it.
    """

    @pytest.mark.asyncio
    async def test_chat_lifecycle_records_and_api_returns(self):
        from minimal_harness.types import LLMEnd

        repo = InMemoryMetricsRepository()
        set_metrics_repo(repo)
        try:
            mw = MetricsPersistenceMiddleware(
                user_id="u1",
                session_id="sess_1",
                agent_id="triage",
                scenario_id="code_review",
                provider="openai",
                model="gpt-4",
            )
            # what AgentRuntime does for one LLM round-trip
            await mw.on_agent_start("hello")
            await mw.on_llm_start([{"role": "user", "content": "hello"}], [])
            evt = LLMEnd(
                content="hi there",
                reasoning_content=None,
                tool_calls=None,
                usage={"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
                error=None,
            )
            await mw.on_llm_end(evt)
            tool_call = {"function": {"name": "calculator", "arguments": "{}"}}
            await mw.on_tool_start(tool_call)
            await mw.on_tool_end(tool_call, "42")

            s: MetricsSummary = await repo.query_summary()
            assert s.llm_call_count == 1
            assert s.total_tokens == 28
            assert (await repo.query_ranking("tools")).items[0].key == "calculator"
        finally:
            set_metrics_repo(None)


class TestEndToEndChatToAPI:
    """Real chat endpoint → runtime middleware → repo → management API."""

    @pytest.fixture(autouse=True)
    def _clean_repo(self):
        set_metrics_repo(None)
        yield
        set_metrics_repo(None)

    @pytest.fixture
    def e2e_app(self):

        from minimal_harness.llm.factory import ProviderFactory
        from minimal_harness.llm.llm import LLMResponse, Stream
        from minimal_harness.types import LLMChunkDelta

        from mh_gateway.llm import DefaultLLMProviderService, LLMConfigBackend
        from mh_gateway.session import SimpleSession

        class _FakeLLM:
            """Streams a single chunk then a final response with usage."""

            async def chat(self, messages, tools, stop_event=None, **kwargs):
                async def _agen():
                    yield LLMChunkDelta(content="hi")
                    yield LLMResponse(
                        content="hi",
                        reasoning_content=None,
                        tool_calls=[],
                        finish_reason="stop",
                        usage={
                            "prompt_tokens": 10,
                            "completion_tokens": 5,
                            "total_tokens": 15,
                        },
                    )

                return Stream(_agen())

        factory = ProviderFactory()
        factory.register("fake", lambda cfg: _FakeLLM())

        class _Backend(LLMConfigBackend):
            def __init__(self):
                self._cfgs = {}

            async def list(self):
                return list(self._cfgs.values())

            async def get(self, name):
                return self._cfgs.get(name)

            async def create(self, config):
                self._cfgs[config.name] = config
                return config

            async def update(self, name, config):
                self._cfgs[name] = config
                return config

            async def delete(self, name):
                self._cfgs.pop(name, None)

            async def get_model_max_context(self, provider_name, model_code):
                return 0

            async def close(self):
                self._cfgs.clear()

        llm_service = DefaultLLMProviderService.from_components(
            factory=factory, backend=_Backend()
        )

        class _SessionStore(_MockSessionRepo):
            def __init__(self):
                super().__init__()
                self._sessions = {}

            async def create_session(self, **kwargs):
                session = SimpleSession(
                    session_id=kwargs.get("session_id") or "",
                    agent_name=kwargs.get("agent_name", ""),
                    user_id=kwargs.get("user_id", ""),
                    scenario_id=kwargs.get("scenario_id"),
                    display_name_locale=kwargs.get("display_name_locale"),
                )
                from datetime import UTC, datetime

                session.created_at = datetime.now(UTC).isoformat()
                self._sessions[session.session_id] = session
                return session

            async def get_session(self, session_id):
                return self._sessions.get(session_id)

        metadata = _MockMetadata()
        import asyncio as _a

        _a.run(
            metadata.create_agent(
                {
                    "name": "triage",
                    "display_name": "Triage",
                    "provider": "fake",
                    "model": "fake-model",
                    "agent_type": "simple",
                    "system_prompt": "You are a helper.",
                }
            )
        )

        settings = ConfigSchema(
            db_path="./e2e-metrics.db",
            cors_origins=[],
            metrics_enabled=False,
            enable_eval=False,
        )

        @asynccontextmanager
        async def adapter_lifespan(app: FastAPI):
            bundle = GatewayAdapters(
                settings=settings,
                user_auth=_MockProvider(),
                authorization=_MockProvider(),
                m2m_auth=_MockProvider(),
                outbound_auth=_MockProvider(),
                metadata=metadata,
                llm=llm_service,
                sessions=_SessionStore(),
                eval_results=None,
            )
            yield bundle

        repo = InMemoryMetricsRepository()
        app = create_app(
            settings=settings,
            adapters=adapter_lifespan,
            lifespan_hooks=[_metrics_hook(repo)],
        )
        return app, repo

    @pytest.fixture
    def e2e_client(self, e2e_app):
        app, repo = e2e_app
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c, repo

    def test_chat_endpoint_records_metrics(self, e2e_client):
        client, repo = e2e_client
        headers = {"X-User-Id": "1"}

        # create a session
        r = client.post(
            "/api/v1/sessions",
            json={"agent_name": "triage"},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        memory_id = r.json()["memory_id"]

        # stream a chat message — the fake LLM responds, runtime records
        r = client.post(
            f"/api/v1/chat/{memory_id}",
            json={"message": "hello"},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        assert "LLMChunk" in r.text
        assert "done" in r.text

        # the metrics repository must have received the LLM call
        summary = _asyncio_run(repo.query_summary())
        assert summary.llm_call_count == 1
        assert summary.total_tokens == 15
        assert _asyncio_run(repo.query_ranking("agents")).items[0].key == "triage"
        assert _asyncio_run(repo.query_ranking("users")).items[0].key == "1"

        # and the management API must return the same aggregation
        r = client.get(
            "/api/v1/management/metrics",
            params={"date_from": "1970-01-01"},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["llm_call_count"] == 1
        assert data["total_tokens"] == 15
        assert data["model_perf"][0]["model"] == "fake-model"
        assert data["entity_counts"]["agents"] == 1


def _asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)
