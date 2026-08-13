"""Tests for the JSON entity import API (management/import)."""

from __future__ import annotations

import json

import pytest

from mh_gateway.api.imports import validate_import_content

VALID_SCENE = {
    "entity_type": "scene",
    "id": "imported_scene",
    "name": "Imported Scene",
    "icon": "🚀",
    "description": "created by import",
    "show_on_homepage": False,
}

VALID_AGENT = {
    "entity_type": "agent",
    "name": "imported_agent",
    "display_name": "Imported Agent",
    "description": "created by import",
    "system_prompt": "You are imported.",
}

VALID_TOOL = {
    "entity_type": "tool",
    "name": "imported_tool",
    "display_name": "Imported Tool",
    "description": "created by import",
    "parameters": {"type": "object", "properties": {}},
}


def _upload(client, files: list[tuple[str, str]], headers=None):
    """POST /api/v1/management/import with (filename, json-content) pairs."""
    return client.post(
        "/api/v1/management/import",
        files=[
            ("files", (name, content.encode("utf-8"), "application/json"))
            for name, content in files
        ],
        headers=headers,
    )


# ── unit tests for validate_import_content ──


def test_validate_ok_sets_payload():
    r = validate_import_content("s.json", json.dumps(VALID_SCENE).encode())[0]
    assert r.ok
    assert r.payload is not None
    assert r.entity_type == "scene"
    assert r.entity_key == "imported_scene"
    assert r.payload["show_on_homepage"] is False  # overridden default
    assert "entity_type" not in r.payload


def test_validate_locale_object_normalized_to_string():
    agent = dict(
        VALID_AGENT,
        display_name_locale={"zh": "导入的 Agent", "en": "Imported Agent"},
    )
    r = validate_import_content("a.json", json.dumps(agent).encode())[0]
    assert r.ok
    assert r.payload is not None
    assert r.payload["display_name_locale"] == json.dumps(
        {"zh": "导入的 Agent", "en": "Imported Agent"}, ensure_ascii=False
    )


def test_validate_json_syntax_error_has_line_col():
    r = validate_import_content(
        "bad.json", b'{\n  "entity_type": "agent",\n  "name": \n}'
    )[0]
    assert not r.ok
    issue = r.issues[0]
    assert issue.code == "json_syntax"
    assert issue.line == 4
    assert issue.column == 1


def test_validate_missing_required_field():
    r = validate_import_content(
        "a.json", json.dumps({"entity_type": "agent"}).encode()
    )[0]
    assert not r.ok
    codes = {i.code for i in r.issues}
    assert "missing_field" in codes
    assert any(i.path == "$.name" for i in r.issues)


def test_validate_relationship_rejected():
    scene = dict(VALID_SCENE, agents=[{"name": "x"}])
    r = validate_import_content("s.json", json.dumps(scene).encode())[0]
    assert not r.ok
    issue = next(i for i in r.issues if i.code == "relationship_not_supported")
    assert issue.path == "$.agents"


def test_validate_unknown_field_rejected():
    agent = dict(VALID_AGENT, tool_names=["t1"])
    r = validate_import_content("a.json", json.dumps(agent).encode())[0]
    assert not r.ok
    assert any(i.code == "relationship_not_supported" for i in r.issues)
    assert any(i.code == "unknown_field" for i in r.issues) is False

    agent = dict(VALID_AGENT, bogus_field=1)
    r = validate_import_content("a.json", json.dumps(agent).encode())[0]
    assert not r.ok
    assert any(
        i.code == "unknown_field" and i.path == "$.bogus_field" for i in r.issues
    )


def test_validate_agent_provider_model_rejected():
    """provider / provider_name / model are machine-specific and must not
    travel with shared metadata (each instance picks its own).
    """
    for field in ("provider", "provider_name", "model"):
        agent = dict(VALID_AGENT, **{field: "x"})
        r = validate_import_content("a.json", json.dumps(agent).encode())[0]
        assert not r.ok, field
        assert any(
            i.path == f"$.{field}" and i.code == "unknown_field" for i in r.issues
        )


def test_validate_invalid_type():
    tool = dict(VALID_TOOL, parameters=["not", "a", "dict"])
    r = validate_import_content("t.json", json.dumps(tool).encode())[0]
    assert not r.ok
    assert any(i.code == "invalid_type" and i.path == "$.parameters" for i in r.issues)


def test_validate_invalid_entity_type():
    r = validate_import_content(
        "x.json", json.dumps({"entity_type": "widget"}).encode()
    )[0]
    assert not r.ok
    assert r.issues[0].code == "invalid_entity_type"


def test_array_elements_validated_individually():
    """An array file is split per element; non-object elements are rejected."""
    results = validate_import_content("x.json", b"[1,2,3]")
    assert len(results) == 3  # one result per array element
    assert all(not r.ok for r in results)
    assert "必须是 JSON 对象" in results[0].issues[0].message


# ── API tests ──


def test_import_mixed_types_creates_all(client, auth_header):
    resp = _upload(
        client,
        [
            ("scene.json", json.dumps(VALID_SCENE)),
            ("agent.json", json.dumps(VALID_AGENT)),
            ("tool.json", json.dumps(VALID_TOOL)),
        ],
        headers=auth_header,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["file_count"] == 3
    created = body["created"]
    assert any(c.get("id") == "imported_scene" for c in created)
    assert any(c.get("name") == "imported_agent" for c in created)
    assert any(c.get("name") == "imported_tool" for c in created)

    # entities really persisted
    list_resp = client.get("/api/v1/management/scenarios", headers=auth_header)
    ids = {s["id"] for s in list_resp.json()["items"]}
    assert "imported_scene" in ids
    agents = client.get("/api/v1/management/agents", headers=auth_header).json()[
        "items"
    ]
    assert any(a["name"] == "imported_agent" for a in agents)


def test_import_syntax_error_fails_atomically(client, auth_header):
    bad = '{\n  "entity_type": "agent",\n  "name": "broken"\n}\n}'
    resp = _upload(
        client,
        [("scene.json", json.dumps(VALID_SCENE)), ("bad.json", bad)],
        headers=auth_header,
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["message"].startswith("导入失败")
    files = {f["filename"]: f for f in detail["files"]}
    assert files["scene.json"]["ok"] is True
    assert files["bad.json"]["ok"] is False
    issue = files["bad.json"]["issues"][0]
    assert issue["code"] == "json_syntax"
    assert issue["line"] == 5

    # nothing was created
    agents = client.get("/api/v1/management/agents", headers=auth_header).json()[
        "items"
    ]
    assert not any(a["name"] == "imported_agent" for a in agents)
    scenes = client.get("/api/v1/management/scenarios", headers=auth_header).json()[
        "items"
    ]
    assert not any(s["id"] == "imported_scene" for s in scenes)


def test_import_relationship_rejected(client, auth_header):
    scene = dict(VALID_SCENE, agents=[{"name": "some_agent", "tool_names": []}])
    resp = _upload(client, [("scene.json", json.dumps(scene))], headers=auth_header)
    assert resp.status_code == 422
    issues = resp.json()["detail"]["files"][0]["issues"]
    assert any(i["code"] == "relationship_not_supported" for i in issues)
    assert "不支持导入" in next(
        i["message"] for i in issues if i["code"] == "relationship_not_supported"
    )


def test_import_conflict_with_existing(client, auth_header):
    # code_review exists in the seeded scenarios
    scene = dict(VALID_SCENE, id="code_review")
    resp = _upload(client, [("scene.json", json.dumps(scene))], headers=auth_header)
    assert resp.status_code == 422
    issue = resp.json()["detail"]["files"][0]["issues"][0]
    assert issue["code"] == "conflict"


def test_import_duplicate_within_batch(client, auth_header):
    resp = _upload(
        client,
        [
            ("a1.json", json.dumps(VALID_AGENT)),
            ("a2.json", json.dumps(VALID_AGENT)),
        ],
        headers=auth_header,
    )
    assert resp.status_code == 422
    by_file = {f["filename"]: f for f in resp.json()["detail"]["files"]}
    assert by_file["a1.json"]["ok"] is True
    assert any(i["code"] == "duplicate" for i in by_file["a2.json"]["issues"])
    agents = client.get("/api/v1/management/agents", headers=auth_header).json()[
        "items"
    ]
    assert not any(a["name"] == "imported_agent" for a in agents)


def test_import_requires_permission(client, auth_header, mock_provider):
    mock_provider.check.side_effect = lambda uid, perm: perm != "manage:agent:*"
    resp = _upload(
        client, [("agent.json", json.dumps(VALID_AGENT))], headers=auth_header
    )
    assert resp.status_code == 403
    assert "manage:agent:*" in resp.json()["detail"]


def test_import_requires_permission_only_for_present_types(
    client, auth_header, mock_provider
):
    # user lacks manage:agent:* but has manage:scene:* → scene-only batch is fine
    mock_provider.check.side_effect = lambda uid, perm: perm != "manage:agent:*"
    resp = _upload(
        client, [("scene.json", json.dumps(VALID_SCENE))], headers=auth_header
    )
    assert resp.status_code == 201, resp.text


def test_import_rejects_non_json_extension(client, auth_header):
    resp = client.post(
        "/api/v1/management/import",
        files=[("files", ("tool.py", b"print(1)", "text/plain"))],
        headers=auth_header,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["files"][0]["issues"][0]["code"] == "bad_extension"


def test_template_download(client, auth_header):
    for t in ("scene", "agent", "tool"):
        resp = client.get(
            f"/api/v1/management/import/templates/{t}", headers=auth_header
        )
        assert resp.status_code == 200
        assert resp.headers["content-disposition"].startswith("attachment")
        data = json.loads(resp.content)
        assert data["entity_type"] == t
        assert "agents" not in data and "tool_names" not in data


def test_template_download_unknown_type(client, auth_header):
    resp = client.get("/api/v1/management/import/templates/widget", headers=auth_header)
    assert resp.status_code == 404


def test_template_download_requires_permission(client, auth_header, mock_provider):
    mock_provider.check.side_effect = lambda uid, perm: perm != "manage:tool:*"
    resp = client.get("/api/v1/management/import/templates/tool", headers=auth_header)
    assert resp.status_code == 403
    resp = client.get("/api/v1/management/import/templates/scene", headers=auth_header)
    assert resp.status_code == 200


@pytest.mark.parametrize(
    "etype",
    ["scene", "agent", "tool"],
)
def test_import_defaults_applied(client, auth_header, etype):
    minimal = {"entity_type": etype}
    if etype == "scene":
        minimal["id"] = f"min_{etype}"
        minimal["name"] = f"min_{etype}"
    else:
        minimal["name"] = f"min_{etype}"
    resp = _upload(
        client, [(f"{etype}.json", json.dumps(minimal))], headers=auth_header
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()["created"][0]
    if etype == "scene":
        assert created["show_on_homepage"] is True
    elif etype == "agent":
        # provider is a machine-specific default applied on import
        assert created["provider"] == "openai"
