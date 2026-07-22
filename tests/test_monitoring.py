from __future__ import annotations

import asyncio
import json
from collections.abc import Generator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mh_gateway.adapters import UserIdentity
from mh_gateway.app import GatewayAdapters, create_app
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
        c.http_requests_total.inc({"method": "GET", "path": "/a", "status": "200"})
        snap = c.live_snapshot()
        assert "instance_id" in snap
        assert "uptime_seconds" in snap
        assert snap["http_requests_total"]

    def test_set_get_collector(self):
        c = MetricsCollector()
        set_collector(c)
        assert get_collector() is c
        set_collector(None)


def _make_provider_bundle(settings: ConfigSchema):
    """Return an adapter_lifespan that satisfies the gateway with mocks."""
    metadata = AsyncMock()
    metadata.list_scenarios = AsyncMock(return_value=[])
    metadata.list_agents = AsyncMock(return_value=[])
    metadata.list_tools = AsyncMock(return_value=[])
    metadata.get_tools = AsyncMock(return_value={})

    provider = AsyncMock()
    provider.verify = AsyncMock(
        return_value=UserIdentity(user_id="1", username="admin")
    )
    provider.get_permissions = AsyncMock(return_value=ALL_PERMS)
    provider.check = AsyncMock(side_effect=lambda uid, perm: True)
    provider.authenticate = AsyncMock(return_value="default-app")
    provider.get_identity_headers = AsyncMock(return_value={})
    provider.get_headers = AsyncMock(return_value={})
    provider.close = AsyncMock()

    sessions = AsyncMock()
    sessions.healthcheck = AsyncMock(side_effect=RuntimeError("not ready"))

    @asynccontextmanager
    async def adapter_lifespan(app: FastAPI):
        yield GatewayAdapters(
            settings=settings,
            user_auth=provider,
            authorization=provider,
            m2m_auth=provider,
            outbound_auth=provider,
            metadata=metadata,
            llm=_NoopLLM(),
            sessions=sessions,
            eval_results=None,
        )

    return adapter_lifespan


class _NoopLLM:
    def list_provider_types(self):
        return []

    async def create_llm(self, spec):
        raise NotImplementedError

    async def build_resolver(self, specs):
        def _resolver(meta):
            raise NotImplementedError

        return _resolver

    async def list_configs(self):
        return []

    async def get_config(self, name):
        return None

    async def create_config(self, config):
        return config

    async def update_config(self, name, config):
        return config

    async def delete_config(self, name):
        return None

    async def get_model_max_context(self, provider_name, model_code):
        return 0

    async def close(self):
        return None


class TestAuditMiddlewareStructuredLogging:
    def test_agent_start_logs_structured_json(self, caplog):
        mw = AuditMiddleware(
            user_id="u1",
            session_id="s1",
            agent_id="a1",
            scenario_id="sc1",
            provider="openai",
            model="gpt",
            trace_id="t1",
        )
        with caplog.at_level("INFO", logger="orchestration.audit"):
            asyncio.run(mw.on_agent_start("hi"))
        entries = [json.loads(r.getMessage()) for r in caplog.records]
        assert any(e["event"] == "agent_start" for e in entries)

    def test_llm_end_includes_token_counts(self, caplog):
        from minimal_harness.types import LLMEnd

        mw = AuditMiddleware(
            user_id="u1",
            session_id="s1",
            agent_id="a1",
            scenario_id="sc1",
            provider="openai",
            model="gpt",
            trace_id="t1",
        )
        evt = LLMEnd(
            content="x",
            reasoning_content=None,
            tool_calls=None,
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            error=None,
        )
        with caplog.at_level("INFO", logger="orchestration.audit"):
            asyncio.run(mw.on_llm_end(evt))
        entries = [json.loads(r.getMessage()) for r in caplog.records]
        llm_end = [e for e in entries if e["event"] == "llm_end"]
        assert llm_end, "expected llm_end entry"
        e = llm_end[0]
        assert e["prompt_tokens"] == 10
        assert e["completion_tokens"] == 5
        assert e["total_tokens"] == 15

    def test_llm_start_logs_structured_json(self, caplog):
        mw = AuditMiddleware(
            user_id="u1",
            session_id="s1",
            agent_id="a1",
            scenario_id="sc1",
            provider="openai",
            model="gpt",
            trace_id="t1",
        )
        with caplog.at_level("INFO", logger="orchestration.audit"):
            asyncio.run(mw.on_llm_start([{"role": "user", "content": "hi"}], []))
        entries = [json.loads(r.getMessage()) for r in caplog.records]
        assert any(e["event"] == "llm_start" for e in entries)

    def test_tool_start_and_end(self, caplog):
        mw = AuditMiddleware(
            user_id="u1",
            session_id="s1",
            agent_id="a1",
            scenario_id="sc1",
            provider="openai",
            model="gpt",
            trace_id="t1",
        )
        tool_call = {"function": {"name": "t1", "arguments": "{}"}}
        with caplog.at_level("INFO", logger="orchestration.audit"):
            asyncio.run(mw.on_tool_start(tool_call))
            asyncio.run(mw.on_tool_end(tool_call, "ok"))
        events = [json.loads(r.getMessage())["event"] for r in caplog.records]
        assert "tool_start" in events
        assert "tool_end" in events

    def test_tool_error(self, caplog):
        mw = AuditMiddleware(
            user_id="u1",
            session_id="s1",
            agent_id="a1",
            scenario_id="sc1",
            provider="openai",
            model="gpt",
            trace_id="t1",
        )
        tool_call = {"function": {"name": "t1", "arguments": "{}"}}
        with caplog.at_level("INFO", logger="orchestration.audit"):
            asyncio.run(mw.on_tool_error(tool_call, RuntimeError("boom")))
        events = [json.loads(r.getMessage())["event"] for r in caplog.records]
        assert "tool_error" in events

    def test_agent_end_error(self, caplog):
        from minimal_harness.types import AgentEnd

        mw = AuditMiddleware(
            user_id="u1",
            session_id="s1",
            agent_id="a1",
            scenario_id="sc1",
            provider="openai",
            model="gpt",
            trace_id="t1",
        )
        evt = AgentEnd(
            response="",
            time_taken=0.0,
            exceeded=False,
            interrupted=False,
            error="boom",
        )
        with caplog.at_level("INFO", logger="orchestration.audit"):
            asyncio.run(mw.on_agent_end(evt))
        entries = [json.loads(r.getMessage()) for r in caplog.records]
        assert any(e["event"] == "agent_end" and e["error"] == "boom" for e in entries)

    def test_metrics_collector_integration(self):
        set_collector(MetricsCollector())
        c = get_collector()
        c.llm_requests_total.inc({"provider": "openai", "model": "gpt", "status": "ok"})
        c.llm_tokens_total.inc(
            {"provider": "openai", "model": "gpt", "type": "prompt"}, 5
        )
        c.llm_tokens_total.inc(
            {"provider": "openai", "model": "gpt", "type": "completion"}, 7
        )
        snap = c.live_snapshot()
        assert snap["llm_requests_total"][0]["value"] == 1
        total = sum(s["value"] for s in snap["llm_tokens_total"])
        assert total == 12
        set_collector(None)


class TestHealthEndpoints:
    @pytest.fixture
    def metrics_app(self, tmp_path):
        settings = ConfigSchema(
            db_path=str(tmp_path / "test.db"),
            metrics_enabled=True,
            db_auto_schema=True,
            dev_mode=False,
            enable_eval=False,
        )
        return create_app(settings=settings, adapters=_make_provider_bundle(settings))

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
        assert response.status_code == 503

    def test_metrics_endpoint(self, metrics_client):
        response = metrics_client.get("/api/v1/metrics")
        assert response.status_code == 200
        data = response.json()
        assert "instance_id" in data
        assert "uptime_seconds" in data
        assert "http_requests_total" in data


class TestMetricsDisabled:
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
            enable_eval=False,
        )
        return create_app(settings=settings, adapters=_make_provider_bundle(settings))

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
