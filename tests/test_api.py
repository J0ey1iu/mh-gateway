from unittest.mock import AsyncMock


class TestManagementAPI:
    """CRUD management API tests using the in-memory management provider."""

    def test_create_scenario(self, client, auth_header):
        resp = client.post(
            "/api/v1/management/scenarios",
            headers=auth_header,
            json={
                "id": "test-scene",
                "name": "Test Scene",
                "description": "A test scenario",
            },
        )
        assert resp.status_code == 201, resp.json()
        data = resp.json()
        assert data["id"] == "test-scene"
        assert data["name"] == "Test Scene"
        assert "created_at" in data
        assert data.get("created_by") == "1"

    def test_create_scenario_duplicate(self, client, auth_header):
        client.post(
            "/api/v1/management/scenarios",
            headers=auth_header,
            json={"id": "dup", "name": "Dup"},
        )
        resp = client.post(
            "/api/v1/management/scenarios",
            headers=auth_header,
            json={"id": "dup", "name": "Dup Again"},
        )
        assert resp.status_code == 409

    def test_update_scenario(self, client, auth_header):
        client.post(
            "/api/v1/management/scenarios",
            headers=auth_header,
            json={"id": "upd", "name": "Old Name"},
        )
        resp = client.put(
            "/api/v1/management/scenarios/upd",
            headers=auth_header,
            json={"name": "New Name", "description": "Updated desc"},
        )
        assert resp.status_code == 200, resp.json()
        data = resp.json()
        assert data["name"] == "New Name"
        assert data["description"] == "Updated desc"
        assert data.get("updated_by") == "1"

    def test_update_scenario_not_found(self, client, auth_header):
        resp = client.put(
            "/api/v1/management/scenarios/nonexistent",
            headers=auth_header,
            json={"name": "Nope"},
        )
        assert resp.status_code == 404

    def test_delete_scenario(self, client, auth_header):
        client.post(
            "/api/v1/management/scenarios",
            headers=auth_header,
            json={"id": "del", "name": "To Delete"},
        )
        resp = client.delete("/api/v1/management/scenarios/del", headers=auth_header)
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"

    def test_delete_scenario_not_found(self, client, auth_header):
        resp = client.delete(
            "/api/v1/management/scenarios/nonexistent", headers=auth_header
        )
        assert resp.status_code == 404

    def test_create_agent(self, client, auth_header):
        resp = client.post(
            "/api/v1/management/agents",
            headers=auth_header,
            json={
                "name": "test-agent",
                "display_name": "Test Agent",
                "description": "An agent for testing",
                "system_prompt": "You are a test agent.",
            },
        )
        assert resp.status_code == 201, resp.json()
        data = resp.json()
        assert data["name"] == "test-agent"
        assert data["display_name"] == "Test Agent"
        assert data.get("created_by") == "1"

    def test_create_agent_duplicate(self, client, auth_header):
        client.post(
            "/api/v1/management/agents",
            headers=auth_header,
            json={"name": "dup-agent", "display_name": "Dup"},
        )
        resp = client.post(
            "/api/v1/management/agents",
            headers=auth_header,
            json={"name": "dup-agent", "display_name": "Dup Again"},
        )
        assert resp.status_code == 409

    def test_update_agent(self, client, auth_header):
        client.post(
            "/api/v1/management/agents",
            headers=auth_header,
            json={"name": "upd-agent", "display_name": "Old"},
        )
        resp = client.put(
            "/api/v1/management/agents/upd-agent",
            headers=auth_header,
            json={"display_name": "New", "system_prompt": "New prompt"},
        )
        assert resp.status_code == 200, resp.json()
        assert resp.json()["display_name"] == "New"
        assert resp.json().get("updated_by") == "1"

    def test_delete_agent(self, client, auth_header):
        client.post(
            "/api/v1/management/agents",
            headers=auth_header,
            json={"name": "del-agent", "display_name": "To Delete"},
        )
        resp = client.delete("/api/v1/management/agents/del-agent", headers=auth_header)
        assert resp.status_code == 200

    def test_create_tool(self, client, auth_header):
        resp = client.post(
            "/api/v1/management/tools",
            headers=auth_header,
            json={
                "name": "test-tool",
                "display_name": "Test Tool",
                "description": "A tool for testing",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "input": {"type": "string", "description": "Input text"}
                    },
                },
                "endpoint_url": "/api/v1/tools/test-tool/execute",
            },
        )
        assert resp.status_code == 201, resp.json()
        data = resp.json()
        assert data["name"] == "test-tool"
        assert data["endpoint_url"] == "/api/v1/tools/test-tool/execute"
        assert data.get("created_by") == "1"

    def test_update_tool(self, client, auth_header):
        client.post(
            "/api/v1/management/tools",
            headers=auth_header,
            json={
                "name": "upd-tool",
                "display_name": "Old Tool",
                "parameters": {"type": "object", "properties": {}},
            },
        )
        resp = client.put(
            "/api/v1/management/tools/upd-tool",
            headers=auth_header,
            json={"display_name": "New Tool"},
        )
        assert resp.status_code == 200, resp.json()
        assert resp.json()["display_name"] == "New Tool"
        assert resp.json().get("updated_by") == "1"

    def test_delete_tool(self, client, auth_header):
        client.post(
            "/api/v1/management/tools",
            headers=auth_header,
            json={
                "name": "del-tool",
                "display_name": "To Delete",
                "parameters": {"type": "object", "properties": {}},
            },
        )
        resp = client.delete("/api/v1/management/tools/del-tool", headers=auth_header)
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"

    def test_management_api_unauthorized(self, client):
        original = client.app.state.adapters.user_auth.verify
        client.app.state.adapters.user_auth.verify = AsyncMock(return_value=None)
        resp = client.post(
            "/api/v1/management/scenarios", json={"id": "x", "name": "x"}
        )
        client.app.state.adapters.user_auth.verify = original
        assert resp.status_code == 401

    def test_management_api_scene_forbidden(self, client, auth_header):
        original = client.app.state.adapters.authorization.check
        client.app.state.adapters.authorization.check = AsyncMock(
            side_effect=lambda uid, perm: perm != "manage:scene:*"
        )
        try:
            resp = client.get("/api/v1/management/scenarios", headers=auth_header)
            assert resp.status_code == 403

            resp2 = client.post(
                "/api/v1/management/scenarios",
                headers=auth_header,
                json={"id": "x", "name": "x"},
            )
            assert resp2.status_code == 403

            resp3 = client.delete(
                "/api/v1/management/scenarios/x",
                headers=auth_header,
            )
            assert resp3.status_code == 403
        finally:
            client.app.state.adapters.authorization.check = original

    def test_management_api_agent_forbidden(self, client, auth_header):
        original = client.app.state.adapters.authorization.check
        client.app.state.adapters.authorization.check = AsyncMock(
            side_effect=lambda uid, perm: perm != "manage:agent:*"
        )
        try:
            resp = client.get("/api/v1/management/agents", headers=auth_header)
            assert resp.status_code == 403
        finally:
            client.app.state.adapters.authorization.check = original

    def test_management_api_tool_forbidden(self, client, auth_header):
        original = client.app.state.adapters.authorization.check
        client.app.state.adapters.authorization.check = AsyncMock(
            side_effect=lambda uid, perm: perm != "manage:tool:*"
        )
        try:
            resp = client.get("/api/v1/management/tools", headers=auth_header)
            assert resp.status_code == 403
        finally:
            client.app.state.adapters.authorization.check = original


class TestScenariosAPI:
    def test_list_scenarios(self, client, auth_header):
        resp = client.get("/api/v1/scenarios", headers=auth_header)
        assert resp.status_code == 200, resp.json()
        scenarios = resp.json()
        assert len(scenarios) == 2
        ids = [s["id"] for s in scenarios]
        assert "code_review" in ids
        assert "writing" in ids

    def test_list_scenarios_unauthorized(self, client):
        original = client.app.state.adapters.user_auth.verify
        client.app.state.adapters.user_auth.verify = AsyncMock(return_value=None)
        resp = client.get("/api/v1/scenarios")
        client.app.state.adapters.user_auth.verify = original
        assert resp.status_code == 401

    def test_list_scenarios_no_perms(self, client, auth_header):
        original = client.app.state.adapters.authorization.get_permissions
        client.app.state.adapters.authorization.get_permissions = AsyncMock(
            return_value=["use:scene:nonexistent"]
        )
        resp = client.get("/api/v1/scenarios", headers=auth_header)
        client.app.state.adapters.authorization.get_permissions = original
        assert resp.status_code == 200
        assert resp.json() == []
