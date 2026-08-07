"""Portal API tests: user-facing scenario list (pagination, permission flags, heat)."""

from __future__ import annotations

from unittest.mock import AsyncMock

from mh_gateway.database._session import SessionSummary


def _session(
    scenario_id: str, created_at: str, message_count: int = 1
) -> SessionSummary:
    return SessionSummary(
        session_id=f"mem_{scenario_id}_{created_at}",
        agent_name="triage",
        user_id="1",
        scenario_id=scenario_id,
        title=None,
        created_at=created_at,
        message_count=message_count,
        status="idle",
        display_name_locale=None,
    )


def _set_perms(client, perms: list[str]) -> None:
    client.app.state.adapters.authorization.get_permissions = AsyncMock(
        return_value=perms
    )


def _set_sessions(client, sessions: list[SessionSummary]) -> None:
    client.app.state.adapters.sessions.list_sessions = AsyncMock(return_value=sessions)


TEST_AGENTS = [
    {
        "name": "code-reviewer",
        "display_name": "Code Reviewer",
        "display_name_locale": '{"zh":"代码审查","en":"Code Reviewer"}',
        "description": "Reviews code changes",
        "description_locale": '{"zh":"审查代码变更，确保质量","en":"Reviews code changes"}',
    },
    {
        "name": "writer",
        "display_name": "Writing Assistant",
        "display_name_locale": '{"zh":"写作助手","en":"Writing Assistant"}',
        "description": "Help with writing",
        "description_locale": '{"zh":"协助撰写内容","en":"Help with writing"}',
    },
]


class TestPortalScenarios:
    def test_all_accessible_with_all_perms(self, client, auth_header):
        resp = client.get("/api/v1/portal/scenarios", headers=auth_header)
        assert resp.status_code == 200, resp.json()
        body = resp.json()
        assert body["total"] == 2
        assert body["page"] == 1
        assert body["page_size"] == 12
        assert {s["id"] for s in body["items"]} == {"code_review", "writing"}
        assert all(s["accessible"] for s in body["items"])
        assert all("heat" in s for s in body["items"])

    def test_partial_permissions_flagged_not_filtered(self, client, auth_header):
        _set_perms(client, ["use:scene:code_review"])
        resp = client.get("/api/v1/portal/scenarios", headers=auth_header)
        assert resp.status_code == 200, resp.json()
        body = resp.json()
        # both scenarios still present — the inaccessible one is flagged, not dropped
        by_id = {s["id"]: s for s in body["items"]}
        assert by_id["code_review"]["accessible"] is True
        assert by_id["writing"]["accessible"] is False

    def test_no_perms_all_locked_but_returned(self, client, auth_header):
        _set_perms(client, [])
        resp = client.get("/api/v1/portal/scenarios", headers=auth_header)
        assert resp.status_code == 200, resp.json()
        body = resp.json()
        assert body["total"] == 2
        assert all(s["accessible"] is False for s in body["items"])

    def test_accessible_first_then_heat_desc(self, client, auth_header):
        _set_perms(client, ["use:scene:code_review"])
        _set_sessions(
            client,
            [
                _session("writing", "2025-01-01T00:00:00.000Z"),
                _session("writing", "2025-01-02T00:00:00.000Z"),
                _session("writing", "2025-01-03T00:00:00.000Z"),
                _session("code_review", "2025-01-04T00:00:00.000Z"),
            ],
        )
        resp = client.get("/api/v1/portal/scenarios", headers=auth_header)
        assert resp.status_code == 200, resp.json()
        items = resp.json()["items"]
        assert items[0]["id"] == "code_review"
        assert items[1]["id"] == "writing"
        heat = {s["id"]: s["heat"] for s in items}
        # 2025-01-01..04 vs "now": all older than 30 days → weight 1 each
        assert heat["writing"] == 3
        assert heat["code_review"] == 1

    def test_recent_sessions_weighted_higher(self, client, auth_header):
        _set_sessions(
            client,
            [
                _session("code_review", "2025-01-01T00:00:00.000Z"),
                _session("writing", _now_iso(days_ago=2)),
            ],
        )
        resp = client.get("/api/v1/portal/scenarios", headers=auth_header)
        assert resp.status_code == 200, resp.json()
        heat = {s["id"]: s["heat"] for s in resp.json()["items"]}
        assert heat["writing"] == 2  # <=30 days
        assert heat["code_review"] == 1  # old

    def test_pagination(self, client, auth_header):
        for i in range(3, 12):
            client.app.state.adapters.metadata._scenarios.append(
                {
                    "id": f"scene_{i}",
                    "name": f"Scene {i}",
                    "icon": "\U0001f4bb",
                    "description": "d",
                    "agents": [],
                }
            )
        resp = client.get(
            "/api/v1/portal/scenarios?page=2&page_size=5", headers=auth_header
        )
        assert resp.status_code == 200, resp.json()
        body = resp.json()
        assert body["total"] == 11
        assert body["page"] == 2
        assert len(body["items"]) == 5

    def test_page_size_zero_returns_all(self, client, auth_header):
        resp = client.get("/api/v1/portal/scenarios?page_size=0", headers=auth_header)
        assert resp.status_code == 200, resp.json()
        assert resp.json()["total"] == 2
        assert len(resp.json()["items"]) == 2

    def test_heat_survives_session_store_failure(self, client, auth_header):
        client.app.state.adapters.sessions.list_sessions = AsyncMock(
            side_effect=RuntimeError("store down")
        )
        resp = client.get("/api/v1/portal/scenarios", headers=auth_header)
        assert resp.status_code == 200, resp.json()
        assert all(s["heat"] == 0 for s in resp.json()["items"])

    def test_agents_enriched_with_metadata(self, client, auth_header):
        client.app.state.adapters.metadata._agents = list(TEST_AGENTS)
        resp = client.get("/api/v1/portal/scenarios", headers=auth_header)
        assert resp.status_code == 200, resp.json()
        by_id = {s["id"]: s for s in resp.json()["items"]}
        agents = by_id["code_review"]["agents"]
        assert agents == [
            {
                "name": "code-reviewer",
                "display_name": "代码审查",
                "description": "审查代码变更，确保质量",
            }
        ]

    def test_agents_localized_by_accept_language(self, client, auth_header):
        client.app.state.adapters.metadata._agents = list(TEST_AGENTS)
        resp = client.get(
            "/api/v1/portal/scenarios",
            headers={**auth_header, "Accept-Language": "en"},
        )
        assert resp.status_code == 200, resp.json()
        by_id = {s["id"]: s for s in resp.json()["items"]}
        agents = by_id["code_review"]["agents"]
        assert agents[0]["display_name"] == "Code Reviewer"
        assert agents[0]["description"] == "Reviews code changes"

    def test_scenario_name_localized_by_header(self, client, auth_header):
        resp = client.get(
            "/api/v1/portal/scenarios?page_size=0",
            headers={**auth_header, "Accept-Language": "en"},
        )
        assert resp.status_code == 200, resp.json()
        names = {s["id"]: s["name"] for s in resp.json()["items"]}
        assert names["code_review"] == "Code Review"

    def test_hidden_scenarios_not_returned(self, client, auth_header):
        client.app.state.adapters.metadata._scenarios.append(
            {
                "id": "admin_only",
                "name": "Admin Console",
                "icon": "\U0001f6e1\ufe0f",
                "description": "For admins",
                "agents": [],
                "show_on_homepage": False,
            }
        )
        resp = client.get("/api/v1/portal/scenarios?page_size=0", headers=auth_header)
        assert resp.status_code == 200, resp.json()
        ids = [s["id"] for s in resp.json()["items"]]
        assert "admin_only" not in ids
        assert "code_review" in ids  # no field -> defaults to visible


def _now_iso(days_ago: int) -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) - timedelta(days=days_ago)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"
