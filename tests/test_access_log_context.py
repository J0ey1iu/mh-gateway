from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mh_gateway.app import create_app
from mh_gateway.adapters import UserIdentity
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

        @asynccontextmanager
        async def hook(app: FastAPI):
            adapters = app.state.adapters
            adapters.token_verifier = AsyncMock()
            adapters.token_verifier.verify = AsyncMock(
                return_value=UserIdentity(user_id="u-42", username="test-user")
            )
            adapters.permission_checker = AsyncMock()
            adapters.permission_checker.get_permissions = AsyncMock(
                return_value=ALL_PERMS
            )
            adapters.permission_checker.check = AsyncMock(
                side_effect=lambda uid, perm: True
            )
            adapters.management_provider = AsyncMock()
            adapters.management_provider.list_scenarios = AsyncMock(
                return_value=TEST_SCENARIOS
            )
            adapters.management_provider.list_agents = AsyncMock(return_value=[])
            adapters.management_provider.list_tools = AsyncMock(return_value=[])
            yield

        return create_app(settings=settings, lifespan_hooks=[hook])

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
