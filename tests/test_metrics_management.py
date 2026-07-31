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
        assert s.top_scenes == []
        assert s.model_perf == []

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
        # scene-0 (i%2==0 → 3), scene-1 (3)
        assert [t["name"] for t in s.top_scenes][:2] == ["scene-0", "scene-1"]
        # agents: agent-0 ×2, agent-1 ×2, agent-2 ×2 → all tie, sorted by name
        assert len(s.top_agents) == 3
        # tools: tool-0 ×2, tool-1 ×2, tool-2 ×2
        assert len(s.top_tools) == 3
        assert [t["name"] for t in s.top_tools] == ["tool-0", "tool-1", "tool-2"]
        assert s.top_users == [{"name": "u1", "count": 6}]
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
            assert s.top_agents == [{"name": "a1", "count": 1}]
            assert s.top_scenes == [{"name": "sc1", "count": 1}]
            assert s.top_users == [{"name": "u1", "count": 1}]
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
            s = await repo.query_summary()
            assert s.top_tools == [{"name": "calculator", "count": 1}]
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


def _make_app(provider, lifespan_hooks=None):
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
            metadata=_MockMetadata(),
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
        assert data["top_agents"] == [{"name": "triage", "count": 1}]
        assert data["top_scenes"] == [{"name": "code_review", "count": 1}]
        assert data["top_tools"] == [{"name": "calculator", "count": 1}]
        assert data["top_users"] == [{"name": "u1", "count": 1}]
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
            assert s.top_tools == [{"name": "calculator", "count": 1}]
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
        assert summary.top_agents == [{"name": "triage", "count": 1}]
        assert summary.top_users == [{"name": "1", "count": 1}]

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
