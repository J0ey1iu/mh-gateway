"""Tests for JSON metadata export and the export→import roundtrip."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from mh_gateway.api.imports import build_export_payload, validate_import_content
from mh_gateway.app import GatewayAdapters
from mh_gateway.config import ConfigSchema

# ── payloads to create entities via the CRUD API ──


def _scene_payload(key: str) -> dict:
    return {
        "id": key,
        "name": f"Scene {key}",
        "name_locale": json.dumps(
            {"zh": f"场景 {key}", "en": f"Scene {key}"}, ensure_ascii=False
        ),
        "icon": "🧪",
        "description": "created for export test",
        "show_on_homepage": True,
    }


def _agent_payload(key: str) -> dict:
    return {
        "name": key,
        "display_name": f"Agent {key}",
        "display_name_locale": json.dumps(
            {"zh": f"代理 {key}", "en": f"Agent {key}"}, ensure_ascii=False
        ),
        "description": "created for export test",
        "system_prompt": "You are a test agent.",
        "provider": "openai",
        "model": "gpt-4o-mini",
        "llm_config": {"temperature": 0.5},
        "agent_type": "simple",
    }


def _tool_payload(key: str) -> dict:
    return {
        "name": key,
        "display_name": f"Tool {key}",
        "display_name_locale": json.dumps(
            {"zh": f"工具 {key}", "en": f"Tool {key}"}, ensure_ascii=False
        ),
        "description": "created for export test",
        "parameters": {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
        "endpoint_url": "",
    }


_CREATE_URLS = {
    "scene": "/api/v1/management/scenarios",
    "agent": "/api/v1/management/agents",
    "tool": "/api/v1/management/tools",
}

_DELETE_URLS = {
    "scene": "/api/v1/management/scenarios/{key}",
    "agent": "/api/v1/management/agents/{key}",
    "tool": "/api/v1/management/tools/{key}",
}

_KEY_FIELD = {"scene": "id", "agent": "name", "tool": "name"}


def _create(client, headers, etype: str, key: str) -> None:
    payload = {
        "scene": _scene_payload,
        "agent": _agent_payload,
        "tool": _tool_payload,
    }[etype](key)
    resp = client.post(_CREATE_URLS[etype], json=payload, headers=headers)
    assert resp.status_code in (200, 201), resp.text


def _delete(client, headers, etype: str, key: str) -> None:
    resp = client.delete(_DELETE_URLS[etype].format(key=key), headers=headers)
    assert resp.status_code == 200, resp.text


def _upload(client, headers, files: list[tuple[str, str]]):
    return client.post(
        "/api/v1/management/import",
        files=[
            ("files", (name, content.encode("utf-8"), "application/json"))
            for name, content in files
        ],
        headers=headers,
    )


# ── unit: build_export_payload ──


def test_build_export_payload_strips_audit_and_relationships():
    stored = {
        "id": "s1",
        "name": "S1",
        "name_locale": json.dumps(
            {"zh": "场景一", "en": "Scene One"}, ensure_ascii=False
        ),
        "agents": [{"name": "a", "tool_names": []}],
        "created_at": "2024-01-01T00:00:00+00:00",
        "updated_by": "admin",
        "_fn": object(),
    }
    out = build_export_payload("scene", stored)
    assert out["entity_type"] == "scene"
    assert out["id"] == "s1"
    assert out["name_locale"] == {"zh": "场景一", "en": "Scene One"}
    assert "agents" not in out
    assert "created_at" not in out
    assert "updated_by" not in out
    assert "_fn" not in out


def test_build_export_payload_unknown_type():
    with pytest.raises(ValueError):
        build_export_payload("widget", {})


def test_build_export_payload_agent_excludes_provider_model():
    stored = {
        "name": "a1",
        "provider": "openai",
        "provider_name": "my-cfg",
        "model": "gpt-4",
        "system_prompt": "x",
    }
    out = build_export_payload("agent", stored)
    for bad in ("provider", "provider_name", "model"):
        assert bad not in out, bad
    assert out["system_prompt"] == "x"


# ── unit: bundle validation ──


def test_validate_import_content_bundle_splits_elements():
    bundle = [
        {"entity_type": "agent", "name": "a1"},
        {"entity_type": "tool", "name": "t1"},
    ]
    results = validate_import_content("bundle.json", json.dumps(bundle).encode())
    assert len(results) == 2
    assert results[0].filename == "bundle.json[0]"
    assert results[1].filename == "bundle.json[1]"
    assert all(r.ok for r in results)


def test_validate_import_content_bundle_bad_element_reported():
    bundle = [
        {"entity_type": "agent", "name": "a1"},
        {"entity_type": "tool"},  # missing required name
        {"entity_type": "scene", "id": "s1", "name": "S1"},
    ]
    results = validate_import_content("bundle.json", json.dumps(bundle).encode())
    assert len(results) == 3
    assert results[0].ok and results[2].ok
    assert not results[1].ok
    assert any(i.path == "$.name" for i in results[1].issues)
    assert results[1].filename == "bundle.json[1]"


def test_validate_import_content_empty_bundle():
    results = validate_import_content("bundle.json", b"[]")
    assert len(results) == 1
    assert not results[0].ok
    assert results[0].issues[0].code == "empty_bundle"


def test_validate_import_content_single_object_still_works():
    results = validate_import_content(
        "a.json", json.dumps({"entity_type": "agent", "name": "x"}).encode()
    )
    assert len(results) == 1
    assert results[0].ok


# ── API: export ──


@pytest.mark.parametrize(
    "etype,key", [("scene", "exp_s1"), ("agent", "exp_a1"), ("tool", "exp_t1")]
)
def test_export_single_roundtrip(client, auth_header, etype, key):
    _create(client, auth_header, etype, key)

    resp = client.get(f"/api/v1/management/export/{etype}/{key}", headers=auth_header)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-disposition"].startswith("attachment")
    data = resp.json()
    assert data["entity_type"] == etype
    # locale values are expanded to objects and stay importable
    locale_field = {
        "scene": "name_locale",
        "agent": "display_name_locale",
        "tool": "display_name_locale",
    }[etype]
    assert isinstance(data.get(locale_field), dict)
    # server-only / relationship fields must not leak into the export
    for bad in (
        "created_at",
        "updated_at",
        "created_by",
        "updated_by",
        "agents",
        "tool_names",
    ):
        assert bad not in data
    # provider / model are machine-specific and must not travel with the file
    if etype == "agent":
        for bad in ("provider", "provider_name", "model"):
            assert bad not in data, bad

    # roundtrip: delete the original, then import the exported file
    _delete(client, auth_header, etype, key)
    imp = _upload(client, auth_header, [(f"{key}.json", json.dumps(data))])
    assert imp.status_code == 201, imp.text
    assert imp.json()["file_count"] == 1

    # the re-imported entity is persisted and identical where it matters
    list_resp = client.get(_CREATE_URLS[etype], headers=auth_header).json()
    assert any(item[_KEY_FIELD[etype]] == key for item in list_resp["items"])


def test_export_batch_roundtrip(client, auth_header):
    for etype in ("scene", "agent", "tool"):
        _create(client, auth_header, etype, f"b_{etype}_1")
        _create(client, auth_header, etype, f"b_{etype}_2")

    # selected batch export
    resp = client.get(
        "/api/v1/management/export/scene?ids=b_scene_1,b_scene_2", headers=auth_header
    )
    assert resp.status_code == 200
    scenes = resp.json()
    assert isinstance(scenes, list) and len(scenes) == 2
    assert {s["id"] for s in scenes} == {"b_scene_1", "b_scene_2"}

    # export-all (no ids)
    agents = client.get("/api/v1/management/export/agent", headers=auth_header).json()
    assert len(agents) == 2
    tools = client.get("/api/v1/management/export/tool", headers=auth_header).json()
    assert len(tools) == 2

    # roundtrip: delete everything, re-import the batch files
    for etype in ("scene", "agent", "tool"):
        for n in (1, 2):
            _delete(client, auth_header, etype, f"b_{etype}_{n}")

    for etype, content in (
        ("scene", json.dumps(scenes)),
        ("agent", json.dumps(agents)),
        ("tool", json.dumps(tools)),
    ):
        imp = _upload(client, auth_header, [(f"{etype}-batch.json", content)])
        assert imp.status_code == 201, imp.text
        assert imp.json()["file_count"] == 2

    scenes_after = client.get(
        "/api/v1/management/scenarios", headers=auth_header
    ).json()
    assert {s["id"] for s in scenes_after["items"]} >= {"b_scene_1", "b_scene_2"}
    agents_after = client.get("/api/v1/management/agents", headers=auth_header).json()
    assert {a["name"] for a in agents_after["items"]} >= {"b_agent_1", "b_agent_2"}


def test_export_batch_ignores_unknown_ids(client, auth_header):
    _create(client, auth_header, "agent", "known_agent")
    resp = client.get(
        "/api/v1/management/export/agent?ids=known_agent,missing_agent",
        headers=auth_header,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert [a["name"] for a in data] == ["known_agent"]


def test_export_unknown_type_404(client, auth_header):
    resp = client.get("/api/v1/management/export/widget", headers=auth_header)
    assert resp.status_code == 404
    resp = client.get("/api/v1/management/export/widget/x", headers=auth_header)
    assert resp.status_code == 404


def test_export_missing_entity_404(client, auth_header):
    resp = client.get("/api/v1/management/export/scene/nope", headers=auth_header)
    assert resp.status_code == 404


def test_export_requires_permission(client, auth_header, mock_provider):
    mock_provider.check.side_effect = lambda uid, perm: perm != "manage:tool:*"
    assert (
        client.get("/api/v1/management/export/tool/x", headers=auth_header).status_code
        == 403
    )
    assert (
        client.get("/api/v1/management/export/tool", headers=auth_header).status_code
        == 403
    )
    assert (
        client.get("/api/v1/management/export/scene/x", headers=auth_header).status_code
        == 404
    )
    _create(client, auth_header, "scene", "perm_scene")
    assert (
        client.get(
            "/api/v1/management/export/scene/perm_scene", headers=auth_header
        ).status_code
        == 200
    )


# ── API: bundle import atomicity ──


def test_import_bundle_partial_failure_is_atomic(client, auth_header):
    bundle = [
        {"entity_type": "agent", "name": "bundle_ok_agent"},
        {"entity_type": "tool"},  # missing name → invalid
        {"entity_type": "scene", "id": "bundle_ok_scene", "name": "S"},
    ]
    resp = _upload(client, auth_header, [("bundle.json", json.dumps(bundle))])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    by_file = {f["filename"]: f for f in detail["files"]}
    assert by_file["bundle.json[0]"]["ok"] is True
    assert by_file["bundle.json[1]"]["ok"] is False
    assert by_file["bundle.json[2]"]["ok"] is True
    # nothing was created
    agents = client.get("/api/v1/management/agents", headers=auth_header).json()[
        "items"
    ]
    assert not any(a["name"] == "bundle_ok_agent" for a in agents)
    scenes = client.get("/api/v1/management/scenarios", headers=auth_header).json()[
        "items"
    ]
    assert not any(s["id"] == "bundle_ok_scene" for s in scenes)


def test_import_bundle_creates_all(client, auth_header):
    bundle = [
        {"entity_type": "agent", "name": "bundle_agent"},
        {"entity_type": "tool", "name": "bundle_tool"},
    ]
    resp = _upload(client, auth_header, [("bundle.json", json.dumps(bundle))])
    assert resp.status_code == 201, resp.text
    assert resp.json()["file_count"] == 2
    agents = client.get("/api/v1/management/agents", headers=auth_header).json()[
        "items"
    ]
    assert any(a["name"] == "bundle_agent" for a in agents)


# ── runtime robustness: tool without a local implementation ──


class _FakeRequest:
    def __init__(self, adapters):
        self.app = SimpleNamespace(state=SimpleNamespace(adapters=adapters))
        self.base_url = "http://testserver/"


@pytest.fixture
def runtime_adapters(tmp_path, mock_metadata, mock_provider):
    from tests.conftest import _MockLLM, _MockSessionRepo  # type: ignore[import-not-found]

    settings = ConfigSchema(
        db_path=str(tmp_path / "test.db"),
        cors_origins=[],
        metrics_enabled=False,
        enable_eval=False,
    )
    return GatewayAdapters(
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


async def test_runtime_survives_tool_without_binding(runtime_adapters, mock_metadata):
    from minimal_harness.tool.factory import DefaultToolFactory
    from minimal_harness.types import ToolEnd

    from mh_gateway.services.runtime_service import create_runtime

    # a metadata-only tool (no endpoint_url / script_path / _fn) — exactly
    # what an imported tool JSON without a local implementation looks like
    await mock_metadata.create_tool(
        {"name": "ghost_tool", "display_name": "Ghost", "parameters": {}}
    )
    await mock_metadata.create_agent(
        {"name": "ghost_agent", "provider": "openai", "model": "gpt-4"}
    )

    runtime, agent_registry, tool_registry, _ = await create_runtime(
        _FakeRequest(runtime_adapters),  # type: ignore[arg-type]
        user_id="1",
        agent_name="ghost_agent",
        tool_names=["ghost_tool"],
        session_store=runtime_adapters.sessions,
    )
    # the tool stays registered (visible to the agent) instead of crashing setup
    tool_meta = await tool_registry.get("ghost_tool")
    assert tool_meta is not None

    from minimal_harness.types import ToolCall

    tool_call = ToolCall(
        id="1",
        type="function",
        function={"name": "ghost_tool", "arguments": "{}"},
    )
    tool = DefaultToolFactory().create(tool_meta)
    events = []
    async for ev in tool.execute({}, tool_call, None):
        events.append(ev)
    ends = [e for e in events if isinstance(e, ToolEnd)]
    assert ends, "expected a ToolEnd event"
    assert "no implementation" in str(ends[-1].result)


async def test_runtime_survives_scene_agent_missing_implementation(
    runtime_adapters, mock_metadata
):
    """An agent whose provider is not configured locally must not crash
    runtime construction for other agents."""
    from minimal_harness.types import AgentMetadata

    from mh_gateway.services.runtime_service import create_runtime

    await mock_metadata.create_agent(
        {"name": "good_agent", "provider": "openai", "model": "gpt-4"}
    )
    # unknown driver — the resolver only fails when THIS agent is invoked
    await mock_metadata.create_agent(
        {"name": "broken_agent", "provider": "not-a-real-provider", "model": "m"}
    )

    runtime, agent_registry, tool_registry, _ = await create_runtime(
        _FakeRequest(runtime_adapters),  # type: ignore[arg-type]
        user_id="1",
        agent_name="good_agent",
        tool_names=[],
        session_store=runtime_adapters.sessions,
    )
    meta = await agent_registry.get("broken_agent")
    assert isinstance(meta, AgentMetadata)
    assert meta.provider == "not-a-real-provider"
    # the runnable agent is available too
    good = await agent_registry.get("good_agent")
    assert good is not None
