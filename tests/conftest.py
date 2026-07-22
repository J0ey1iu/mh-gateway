from collections.abc import Generator
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
        "agents": [
            {
                "name": "code-reviewer",
                "tool_names": [],
            }
        ],
    },
    {
        "id": "writing",
        "name": "Writing Assistant",
        "name_locale": "{}",
        "icon": "\U0001f4dd",
        "description": "Help with writing",
        "description_locale": "{}",
        "agents": [
            {
                "name": "writer",
                "tool_names": [],
            }
        ],
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


class _MockMetadata:
    """In-memory metadata for tests."""

    def __init__(self):
        self._scenarios: list[dict] = list(TEST_SCENARIOS)
        self._agents: list[dict] = []
        self._tools: list[dict] = []
        self.get_agent = AsyncMock(
            side_effect=lambda name: next(
                (a for a in self._agents if a.get("name") == name), None
            )
        )
        self.get_tool = AsyncMock(
            side_effect=lambda name: next(
                (t for t in self._tools if t.get("name") == name), None
            )
        )
        self.get_tools = AsyncMock(
            side_effect=lambda names: {
                n: next((t for t in self._tools if t.get("name") == n), None)
                for n in names
            }
        )
        self.get_scenario = AsyncMock(
            side_effect=lambda sid: next(
                (s for s in self._scenarios if s.get("id") == sid), None
            )
        )
        self.list_scenarios = AsyncMock(side_effect=lambda: list(self._scenarios))
        self.list_agents = AsyncMock(side_effect=lambda: list(self._agents))
        self.list_tools = AsyncMock(side_effect=lambda: list(self._tools))
        self.create_scenario = AsyncMock(side_effect=self._create_scenario)
        self.update_scenario = AsyncMock(side_effect=self._update_scenario)
        self.delete_scenario = AsyncMock(side_effect=self._delete_scenario)
        self.create_agent = AsyncMock(side_effect=self._create_agent)
        self.update_agent = AsyncMock(side_effect=self._update_agent)
        self.delete_agent = AsyncMock(side_effect=self._delete_agent)
        self.create_tool = AsyncMock(side_effect=self._create_tool)
        self.update_tool = AsyncMock(side_effect=self._update_tool)
        self.delete_tool = AsyncMock(side_effect=self._delete_tool)
        self.add_scenario_agent = AsyncMock(return_value={})
        self.remove_scenario_agent = AsyncMock(return_value={})
        self.add_agent_tool = AsyncMock(return_value={})
        self.remove_agent_tool = AsyncMock(return_value={})
        self.close = AsyncMock()

    async def _create_scenario(self, scenario: dict) -> dict:
        sid = scenario.get("id", "")
        if any(s.get("id") == sid for s in self._scenarios):
            raise ValueError(f"Scenario '{sid}' already exists")
        import datetime

        now = datetime.datetime.now(datetime.UTC).isoformat()
        entry = {**scenario, "created_at": now, "updated_at": now, "created_by": "1"}
        self._scenarios.append(entry)
        return dict(entry)

    async def _update_scenario(self, scenario_id: str, scenario: dict) -> dict:
        for i, s in enumerate(self._scenarios):
            if s.get("id") == scenario_id:
                import datetime

                now = datetime.datetime.now(datetime.UTC).isoformat()
                merged = {
                    **s,
                    **scenario,
                    "id": scenario_id,
                    "updated_at": now,
                    "updated_by": "1",
                }
                self._scenarios[i] = merged
                return dict(merged)
        raise ValueError(f"Scenario '{scenario_id}' not found")

    async def _delete_scenario(self, scenario_id: str) -> None:
        for i, s in enumerate(self._scenarios):
            if s.get("id") == scenario_id:
                self._scenarios.pop(i)
                return
        raise ValueError(f"Scenario '{scenario_id}' not found")

    async def _create_agent(self, agent: dict) -> dict:
        name = agent.get("name", "")
        if any(a.get("name") == name for a in self._agents):
            raise ValueError(f"Agent '{name}' already exists")
        import datetime

        now = datetime.datetime.now(datetime.UTC).isoformat()
        entry = {**agent, "created_at": now, "updated_at": now, "created_by": "1"}
        self._agents.append(entry)
        return dict(entry)

    async def _update_agent(self, name: str, agent: dict) -> dict:
        for i, a in enumerate(self._agents):
            if a.get("name") == name:
                import datetime

                now = datetime.datetime.now(datetime.UTC).isoformat()
                merged = {
                    **a,
                    **agent,
                    "name": name,
                    "updated_at": now,
                    "updated_by": "1",
                }
                self._agents[i] = merged
                return dict(merged)
        raise ValueError(f"Agent '{name}' not found")

    async def _delete_agent(self, name: str) -> None:
        for i, a in enumerate(self._agents):
            if a.get("name") == name:
                self._agents.pop(i)
                return
        raise ValueError(f"Agent '{name}' not found")

    async def _create_tool(self, tool: dict) -> dict:
        tname = tool.get("name", "")
        if any(t.get("name") == tname for t in self._tools):
            raise ValueError(f"Tool '{tname}' already exists")
        import datetime

        now = datetime.datetime.now(datetime.UTC).isoformat()
        entry = {**tool, "created_at": now, "updated_at": now, "created_by": "1"}
        self._tools.append(entry)
        return dict(entry)

    async def _update_tool(self, name: str, tool: dict) -> dict:
        for i, t in enumerate(self._tools):
            if t.get("name") == name:
                import datetime

                now = datetime.datetime.now(datetime.UTC).isoformat()
                merged = {
                    **t,
                    **tool,
                    "name": name,
                    "updated_at": now,
                    "updated_by": "1",
                }
                self._tools[i] = merged
                return dict(merged)
        raise ValueError(f"Tool '{name}' not found")

    async def _delete_tool(self, name: str) -> None:
        for i, t in enumerate(self._tools):
            if t.get("name") == name:
                self._tools.pop(i)
                return
        raise ValueError(f"Tool '{name}' not found")


class _MockProvider:
    """Bundled mocks for user_auth + authorization + m2m + outbound."""

    def __init__(self):
        self.get_permissions = AsyncMock(return_value=ALL_PERMS)
        self.check = AsyncMock(side_effect=lambda uid, perm: True)
        self.verify = AsyncMock(
            return_value=UserIdentity(user_id="1", username="admin")
        )
        self.authenticate = AsyncMock(return_value="default-app")
        self.get_identity_headers = AsyncMock(return_value={})
        self.get_headers = AsyncMock(return_value={})
        self.logout = AsyncMock()
        self.close = AsyncMock()


class _MockLLM:
    def list_provider_types(self) -> list[str]:
        return ["openai", "anthropic"]

    async def create_llm(self, spec):
        from minimal_harness.llm.llm import LLMProvider

        return LLMProvider()  # type: ignore[abstract]

    async def build_resolver(self, specs):
        from minimal_harness.llm.llm import LLMProvider

        def _resolver(meta):
            return LLMProvider()  # type: ignore[abstract]

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


class _MockSessionRepo:
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


@pytest.fixture
def mock_metadata():
    return _MockMetadata()


@pytest.fixture
def mock_provider():
    return _MockProvider()


@pytest.fixture
def test_app(tmp_path, mock_metadata, mock_provider):
    settings = ConfigSchema(
        db_path=str(tmp_path / "test.db"),
        cors_origins=[],
        metrics_enabled=False,
        enable_eval=False,
    )

    @asynccontextmanager
    async def adapter_lifespan(app: FastAPI):
        bundle = GatewayAdapters(
            settings=settings,
            user_auth=mock_provider,
            authorization=mock_provider,
            m2m_auth=mock_provider,
            outbound_auth=mock_provider,
            metadata=mock_metadata,
            llm=_MockLLM(),
            sessions=_MockSessionRepo(),
            eval_results=None,
        )
        yield bundle

    return create_app(
        settings=settings,
        adapters=adapter_lifespan,
    )


@pytest.fixture
def client(test_app) -> Generator[TestClient, None, None]:
    with TestClient(test_app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def auth_header() -> dict[str, str]:
    return {"X-User-Id": "1"}
