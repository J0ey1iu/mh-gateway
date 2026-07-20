from __future__ import annotations

import json
import logging
from collections.abc import Generator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mh_gateway.app import create_app
from mh_gateway.adapters import UserIdentity
from mh_gateway.config import ConfigSchema
from mh_gateway.monitoring.collector import (
    MetricsCollector,
    get_collector,
    set_collector,
)
from mh_gateway.services.audit_middleware import AuditMiddleware

ALL_PERMS = [
    "use:agent:*",
    "use:tool:*",
    "use:scene:*",
    "manage:scene:*",
    "manage:agent:*",
    "manage:tool:*",
]


class TestMetricsCollector:
    def test_counter_inc_and_get(self):
        c = MetricsCollector()
        c.http_requests_total.inc({"method": "GET", "path": "/test", "status": "200"})
        c.http_requests_total.inc({"method": "GET", "path": "/test", "status": "200"})
        snap = c.http_requests_total.snapshot()
        assert len(snap) == 1
        assert snap[0]["value"] == 2

    def test_counter_multiple_labels(self):
        c = MetricsCollector()
        c.http_requests_total.inc({"method": "GET", "path": "/a", "status": "200"})
        c.http_requests_total.inc({"method": "POST", "path": "/b", "status": "201"})
        snap = c.http_requests_total.snapshot()
        assert len(snap) == 2

    def test_histogram_percentiles(self):
        c = MetricsCollector()
        for i in range(100):
            c.http_request_duration_ms.observe(
                {"method": "GET", "path": "/test"}, float(i + 1)
            )
        snap = c.http_request_duration_ms.snapshot()
        assert len(snap) == 1
        s = snap[0]
        assert s["count"] == 100
        assert s["sum"] == 5050.0
        assert s["p50"] == 51.0
        assert s["p99"] == 100.0
        assert s["min"] == 1.0
        assert s["max"] == 100.0

    def test_histogram_snapshot_clears(self):
        c = MetricsCollector()
        c.http_request_duration_ms.observe({"method": "GET", "path": "/test"}, 10.0)
        snap1 = c.http_request_duration_ms.snapshot()
        assert snap1[0]["count"] == 1
        snap2 = c.http_request_duration_ms.snapshot()
        assert snap2 == []

    def test_live_snapshot(self):
        c = MetricsCollector()
        snap = c.live_snapshot()
        assert "uptime_seconds" in snap
        assert "instance_id" in snap

    def test_set_get_collector(self):
        c = MetricsCollector()
        set_collector(c)
        assert get_collector() is c
        set_collector(None)
        assert get_collector() is None


class TestAuditMiddlewareStructuredLogging:
    @pytest.fixture(autouse=True)
    def _setup(self):
        self.audit_logs: list[dict] = []
        _test_instance = self

        class _Handler(logging.Handler):
            def emit(self, record):
                _test_instance.audit_logs.append(json.loads(record.getMessage()))

        self.audit_handler = _Handler()
        self.logger = logging.getLogger("orchestration.audit")
        self.logger.setLevel(logging.INFO)
        self.logger.addHandler(self.audit_handler)
        self.logger.propagate = False
        yield
        self.logger.removeHandler(self.audit_handler)

    def _get_log_by_event(self, event: str) -> dict:
        for log in self.audit_logs:
            if log.get("event") == event:
                return log
        return {}

    @pytest.mark.asyncio
    async def test_agent_start_logs_structured_json(self):
        m = AuditMiddleware(
            user_id="u1",
            session_id="s1",
            agent_id="agent",
            scenario_id="sc",
            trace_id="tr1",
        )
        await m.on_agent_start("hello world")
        log = self._get_log_by_event("agent_start")
        assert log["user_id"] == "u1"
        assert log["session_id"] == "s1"
        assert log["agent_id"] == "agent"
        assert log["scenario_id"] == "sc"
        assert log["trace_id"] == "tr1"
        assert "hello world" in log["input"]
        assert "ts" in log

    @pytest.mark.asyncio
    async def test_llm_end_includes_token_counts(self):
        from minimal_harness.types import LLMEnd

        m = AuditMiddleware(
            user_id="u1",
            session_id="s1",
            agent_id="agent",
            provider="openai",
            model="gpt-4o",
        )
        event = LLMEnd(
            content="result",
            reasoning_content="",
            tool_calls=None,
            usage={"prompt_tokens": 1000000, "completion_tokens": 500000},
            error=None,
        )
        await m.on_llm_end(event)
        log = self._get_log_by_event("llm_end")
        assert log["prompt_tokens"] == 1000000
        assert log["completion_tokens"] == 500000
        assert log["total_tokens"] == 1500000
        assert "cost_dollars" not in log
        assert log["provider"] == "openai"
        assert log["model"] == "gpt-4o"

    @pytest.mark.asyncio
    async def test_llm_start_logs_structured_json(self):
        m = AuditMiddleware(
            user_id="u1",
            session_id="s1",
            provider="openai",
            model="deepseek",
        )
        await m.on_llm_start([{"role": "user", "content": "hi"}], [{"name": "calc"}])
        log = self._get_log_by_event("llm_start")
        assert log["provider"] == "openai"
        assert log["model"] == "deepseek"
        assert log["tool_count"] == 1
        assert log["message_count"] == 1

    @pytest.mark.asyncio
    async def test_tool_start_and_end(self):
        from minimal_harness.types import ToolCall

        m = AuditMiddleware(user_id="u1", session_id="s1")
        tc: ToolCall = {"function": {"name": "calculator"}}
        await m.on_tool_start(tc)
        start_log = self._get_log_by_event("tool_start")
        assert start_log["tool_name"] == "calculator"

        await m.on_tool_end(tc, "42")
        end_log = self._get_log_by_event("tool_end")
        assert end_log["tool_name"] == "calculator"
        assert end_log["result"] == "42"
        assert end_log["status"] == "ok"

    @pytest.mark.asyncio
    async def test_tool_error(self):
        from minimal_harness.types import ToolCall

        m = AuditMiddleware(user_id="u1", session_id="s1")
        tc: ToolCall = {"function": {"name": "calculator"}}
        await m.on_tool_error(tc, ValueError("bad input"))
        log = self._get_log_by_event("tool_error")
        assert log["tool_name"] == "calculator"
        assert "bad input" in log["error"]

    @pytest.mark.asyncio
    async def test_agent_end_error(self):
        from minimal_harness.types import AgentEnd

        m = AuditMiddleware(user_id="u1", session_id="s1")
        event = AgentEnd(
            response="",
            time_taken=1.5,
            exceeded=False,
            interrupted=False,
            error="something went wrong",
        )
        await m.on_agent_end(event)
        log = self._get_log_by_event("agent_end")
        assert log["error"] == "something went wrong"
        assert log["time_taken"] == 1.5

    @pytest.mark.asyncio
    async def test_metrics_collector_integration(self):
        set_collector(MetricsCollector())
        m = AuditMiddleware(
            user_id="u1",
            session_id="s1",
            agent_id="agent",
            provider="openai",
            model="deepseek",
        )
        from minimal_harness.types import AgentEnd, LLMEnd, ToolCall

        await m.on_agent_start("test")
        await m.on_agent_end(
            AgentEnd(
                response="ok",
                time_taken=0.1,
                exceeded=False,
                interrupted=False,
                error=None,
            )
        )

        llm_end = LLMEnd(
            content="ok",
            reasoning_content="",
            tool_calls=None,
            usage={"prompt_tokens": 100, "completion_tokens": 50},
            error=None,
        )
        await m.on_llm_end(llm_end)

        tc: ToolCall = {"function": {"name": "calc"}}
        await m.on_tool_end(tc, "result")

        collector = get_collector()
        assert collector is not None

        agent_snap = collector.agent_runs_total.snapshot()
        agent_started = [
            s for s in agent_snap if any(v == "started" for v in s["labels"].values())
        ]
        agent_ok = [
            s for s in agent_snap if any(v == "ok" for v in s["labels"].values())
        ]
        assert len(agent_started) >= 1
        assert len(agent_ok) >= 1

        llm_snap = collector.llm_tokens_total.snapshot()
        assert len(llm_snap) >= 2

        tool_snap = collector.tool_calls_total.snapshot()
        assert len(tool_snap) >= 1

        set_collector(None)


class TestHealthEndpoints:
    @pytest.fixture
    def metrics_app(self, tmp_path):
        settings = ConfigSchema(
            db_path=str(tmp_path / "test.db"),
            metrics_enabled=True,
            db_auto_schema=True,
            dev_mode=False,
        )

        @asynccontextmanager
        async def mock_hook(app: FastAPI):
            adapters = app.state.adapters
            adapters.token_verifier = AsyncMock()
            adapters.token_verifier.verify = AsyncMock(
                return_value=UserIdentity(user_id="1", username="admin")
            )
            adapters.permission_checker = AsyncMock()
            adapters.permission_checker.get_permissions = AsyncMock(
                return_value=ALL_PERMS
            )
            adapters.permission_checker.check = AsyncMock(
                side_effect=lambda uid, perm: True
            )
            adapters.management_provider = AsyncMock()
            adapters.management_provider.list_scenarios = AsyncMock(return_value=[])
            adapters.management_provider.list_agents = AsyncMock(return_value=[])
            adapters.management_provider.list_tools = AsyncMock(return_value=[])
            yield

        return create_app(settings=settings, lifespan_hooks=[mock_hook])

    @pytest.fixture
    def metrics_client(self, metrics_app) -> Generator[TestClient, None, None]:
        with TestClient(metrics_app, raise_server_exceptions=False) as c:
            yield c

    def test_health_returns_ok(self, metrics_client):
        response = metrics_client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_ready_returns_ready(self, metrics_client):
        response = metrics_client.get("/ready")
        # Without a database initialized, readiness check returns 503.
        assert response.status_code == 503

    def test_metrics_endpoint(self, metrics_client):
        response = metrics_client.get("/api/v1/metrics")
        assert response.status_code == 200
        data = response.json()
        assert "instance_id" in data
        assert "uptime_seconds" in data
        assert "http_requests_total" in data


class TestMetricsDisabled:
    """When metrics_enabled=False, /api/v1/metrics should return 404."""

    @pytest.fixture(autouse=True)
    def _clean_collector(self):
        set_collector(None)
        yield
        set_collector(None)

    @pytest.fixture
    def no_metrics_app(self, tmp_path):
        settings = ConfigSchema(
            db_path=str(tmp_path / "test.db"),
            metrics_enabled=False,
            db_auto_schema=True,
            dev_mode=False,
        )

        @asynccontextmanager
        async def mock_hook(app: FastAPI):
            adapters = app.state.adapters
            adapters.token_verifier = AsyncMock()
            adapters.token_verifier.verify = AsyncMock(
                return_value=UserIdentity(user_id="1", username="admin")
            )
            adapters.permission_checker = AsyncMock()
            adapters.permission_checker.get_permissions = AsyncMock(
                return_value=ALL_PERMS
            )
            adapters.management_provider = AsyncMock()
            adapters.management_provider.list_scenarios = AsyncMock(return_value=[])
            adapters.management_provider.list_agents = AsyncMock(return_value=[])
            adapters.management_provider.list_tools = AsyncMock(return_value=[])
            yield

        return create_app(settings=settings, lifespan_hooks=[mock_hook])

    @pytest.fixture
    def no_metrics_client(self, no_metrics_app) -> Generator[TestClient, None, None]:
        with TestClient(no_metrics_app, raise_server_exceptions=False) as c:
            yield c

    def test_health_still_works(self, no_metrics_client):
        response = no_metrics_client.get("/health")
        assert response.status_code == 200

    def test_metrics_returns_404(self, no_metrics_client):
        response = no_metrics_client.get("/api/v1/metrics")
        assert response.status_code == 404
