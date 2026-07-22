from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mh_gateway.adapters import UserIdentity
from mh_gateway.app import GatewayAdapters, create_app
from mh_gateway.config import ConfigSchema

TEST_SCENARIOS = [
    {
        "id": "code_review",
        "name": "Code Review",
        "name_locale": "{}",
        "icon": "\U0001f4bb",
        "description": "Review code changes",
        "description_locale": "{}",
        "agents": [{"name": "code-reviewer", "tool_names": []}],
    },
    {
        "id": "writing",
        "name": "Writing Assistant",
        "name_locale": "{}",
        "icon": "\U0001f4dd",
        "description": "Help with writing",
        "description_locale": "{}",
        "agents": [{"name": "writer", "tool_names": []}],
    },
]

ALL_PERMS = [
    "use:agent:*",
    "use:tool:*",
    "use:scene:*",
    "use:eval:*",
    "manage:scene:*",
    "manage:agent:*",
    "manage:tool:*",
]


class TestAccessLogContext:
    """Verify trace_id and user_id are correctly captured in access logs."""

    @pytest.fixture(autouse=True)
    def _capture_logs(self):
        self.access_logs: list[dict] = []
        _test_instance = self

        class _Handler(logging.Handler):
            def emit(self, record):
                _test_instance.access_logs.append(json.loads(record.getMessage()))

        handler = _Handler()
        logger = logging.getLogger("orchestration.access")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        logger.propagate = False
        yield
        logger.removeHandler(handler)

    @staticmethod
    def _get_auth_app(tmp_path):

        settings = ConfigSchema(
            db_path=str(tmp_path / "test.db"),
            metrics_enabled=False,
            db_auto_schema=True,
        )

        metadata = AsyncMock()
        metadata.list_scenarios = AsyncMock(return_value=TEST_SCENARIOS)
        metadata.list_agents = AsyncMock(return_value=[])
        metadata.list_tools = AsyncMock(return_value=[])
        metadata.get_tools = AsyncMock(return_value={})

        provider = AsyncMock()
        provider.verify = AsyncMock(
            return_value=UserIdentity(user_id="u-42", username="test-user")
        )
        provider.get_permissions = AsyncMock(return_value=ALL_PERMS)
        provider.check = AsyncMock(side_effect=lambda uid, perm: True)
        provider.authenticate = AsyncMock(return_value="default-app")
        provider.get_identity_headers = AsyncMock(return_value={})
        provider.get_headers = AsyncMock(return_value={})
        provider.close = AsyncMock()

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
                sessions=_NoopSessionRepo(),
                eval_results=None,
            )

        return create_app(settings=settings, adapters=adapter_lifespan)

    def test_trace_id_in_access_log(self, tmp_path):
        app = self._get_auth_app(tmp_path)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/health", headers={"X-Request-Id": "my-trace-001"})
        assert resp.status_code == 200
        assert len(self.access_logs) >= 1
        entry = self.access_logs[0]
        assert entry["trace_id"] == "my-trace-001", (
            f"Expected trace_id='my-trace-001', got {entry!r}"
        )
        assert entry["path"] == "/health"

    def test_trace_id_generated_when_missing(self, tmp_path):
        app = self._get_auth_app(tmp_path)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert len(self.access_logs) >= 1
        entry = self.access_logs[0]
        assert entry["trace_id"] != ""

    def test_user_id_field_exists(self, tmp_path):
        app = self._get_auth_app(tmp_path)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert len(self.access_logs) >= 1
        entry = self.access_logs[0]
        assert isinstance(entry["user_id"], str)

    def test_access_log_structure(self, tmp_path):
        app = self._get_auth_app(tmp_path)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert len(self.access_logs) >= 1
        entry = self.access_logs[0]
        assert isinstance(entry["duration_ms"], float)
        assert entry["status"] == 200
        assert entry["method"] == "GET"
        assert entry["path"] == "/health"


class _NoopLLM:
    def list_provider_types(self):
        return []

    def create_llm(self, spec):
        raise NotImplementedError

    def build_resolver(self, specs):
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


class _NoopSessionRepo:
    async def create_session(self, **kwargs):
        return None

    async def get_session(self, session_id):
        return None

    async def save_memory(self, memory, session_id, extra=None):
        return None

    async def update_usage(self, memory, session_id):
        return None

    async def delete_session(self, session_id):
        return False

    async def list_sessions(self):
        return []

    async def list_user_sessions(self, user_id, scenario_id=None):
        return []

    async def get_session_messages(self, session_id):
        return []

    def get_messages_as_items(self, session):
        return []

    async def healthcheck(self):
        return None

    async def close(self):
        return None
