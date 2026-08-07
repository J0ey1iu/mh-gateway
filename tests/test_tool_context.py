"""Tests for build_tool_context — the structured context forwarded to tool services."""

from types import SimpleNamespace

from mh_gateway.adapters import UserIdentity
from mh_gateway.context import (
    build_tool_context,
    set_current_identity,
    set_current_request,
    set_current_trace_id,
)


def test_empty_without_identity_or_trace():
    ctx = build_tool_context()
    assert ctx.get("user_id", "") == ""
    assert "trace_id" not in ctx
    assert "username" not in ctx


def test_identity_fields_propagate():
    set_current_identity(
        UserIdentity(
            user_id="u-1",
            username="alice",
            roles=["admin", "member"],
            extra_data={"tenant": "t-1"},
        )
    )
    ctx = build_tool_context(user_id="u-1")
    assert ctx["user_id"] == "u-1"
    assert ctx["username"] == "alice"
    assert ctx["roles"] == ["admin", "member"]
    assert ctx["extra_data"] == {"tenant": "t-1"}
    set_current_identity(None)


def test_trace_and_scenario_and_agent():
    set_current_trace_id("trace-123")
    ctx = build_tool_context(user_id="u-1", scenario_id="s-1", agent_name="triage")
    assert ctx["trace_id"] == "trace-123"
    assert ctx["scenario_id"] == "s-1"
    assert ctx["agent_name"] == "triage"
    set_current_trace_id("")


def test_locale_from_request_accept_language():
    req = SimpleNamespace(headers={"accept-language": "en"})
    token = set_current_request(req)  # type: ignore[arg-type]
    try:
        ctx = build_tool_context(user_id="u-1")
        assert ctx["locale"] == "en"
    finally:
        from mh_gateway.context import reset_current_request

        reset_current_request(token)


def test_only_nonempty_fields_included():
    ctx = build_tool_context()
    # locale defaults to "zh" in the gateway; everything else empty stays out
    assert set(ctx.keys()) <= {"locale"}
