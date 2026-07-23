"""Lock the wire schema produced by ``serialize_harness_event``.

The chat SSE stream's frontend depends on a stable dict shape per
event type.  Any change that wraps or renames fields (e.g. putting
``LLMChunk`` content under a ``"chunk"`` key, or adding a
``"type"`` discriminator) will silently break the assistant
message render.  These tests pin the contract.
"""

from __future__ import annotations

from minimal_harness.types import (
    AgentEnd,
    AgentStart,
    CompactionChunk,
    CompactionEnd,
    CompactionStart,
    ExecutionEnd,
    ExecutionStart,
    LLMChunk,
    LLMEnd,
    LLMStart,
    MemoryUpdate,
    MessageEvent,
    ToolEnd,
    ToolProgress,
    ToolResult,
    ToolStart,
)

from mh_gateway.services.runtime_service import serialize_harness_event


class _FakeChunk:
    content = "Hello "
    reasoning = "thinking..."
    tool_calls = None


class _FakeTool:
    function = {"name": "foo"}


def test_llm_chunk_fields_at_top_level() -> None:
    out = serialize_harness_event(LLMChunk(chunk=_FakeChunk()))
    assert set(out.keys()) == {"content", "reasoning", "tool_calls"}
    assert out["content"] == "Hello "
    assert out["reasoning"] == "thinking..."


def test_llm_chunk_without_chunk_yields_empty_dict() -> None:
    out = serialize_harness_event(LLMChunk(chunk=None))
    assert out == {}


def test_llm_end_fields_at_top_level() -> None:
    out = serialize_harness_event(
        LLMEnd(
            content="c",
            reasoning_content="r",
            tool_calls=None,
            usage={"total_tokens": 1},
            error=None,
        )
    )
    assert set(out.keys()) == {
        "content",
        "reasoning_content",
        "tool_calls",
        "usage",
        "error",
    }
    assert out["content"] == "c"
    assert out["reasoning_content"] == "r"


def test_llm_start_uses_compute_helper() -> None:
    out = serialize_harness_event(
        LLMStart(messages=[{"role": "user", "content": "hi"}], tools=[_FakeTool()])
    )
    assert out["message_count"] == 1
    assert "tool_names" in out
    assert "total_chars" in out


def test_agent_end_fields_at_top_level() -> None:
    out = serialize_harness_event(
        AgentEnd(
            response="r",
            time_taken=1.0,
            exceeded=False,
            interrupted=False,
            error=None,
        )
    )
    assert set(out.keys()) == {
        "response",
        "time_taken",
        "exceeded",
        "interrupted",
        "error",
    }


def test_agent_start_is_empty() -> None:
    out = serialize_harness_event(AgentStart(user_input="hi"))
    assert out == {}


def test_tool_start_includes_display_name() -> None:
    out = serialize_harness_event(
        ToolStart(tool_call={"id": "1", "function": {"name": "foo"}})
    )
    assert out["tool_call"] == {"id": "1", "function": {"name": "foo"}}
    assert out["display_name"] == "foo"


def test_tool_progress_includes_chunk() -> None:
    out = serialize_harness_event(
        ToolProgress(tool_call={"id": "1"}, chunk={"progress": "p"})
    )
    assert out["tool_call"] == {"id": "1"}
    assert out["chunk"] == {"progress": "p"}


def test_tool_end_with_tool_result_dataclass() -> None:
    out = serialize_harness_event(
        ToolEnd(
            tool_call={"id": "1"},
            result=ToolResult(content="done", meta={"k": "v"}, stop=True),
        )
    )
    assert out["tool_call"] == {"id": "1"}
    assert out["result"] == "done"
    assert out["meta"] == {"k": "v"}
    assert out["stop"] is True


def test_tool_end_with_raw_result() -> None:
    out = serialize_harness_event(ToolEnd(tool_call={"id": "1"}, result="ok"))
    assert out == {"tool_call": {"id": "1"}, "result": "ok"}


def test_memory_update_only_usage() -> None:
    out = serialize_harness_event(MemoryUpdate(usage={"total_tokens": 100}))
    assert out == {"usage": {"total_tokens": 100}}


def test_message_event_passes_through() -> None:
    out = serialize_harness_event(MessageEvent(message={"role": "assistant"}))
    assert out == {"message": {"role": "assistant"}}


def test_execution_events_flat() -> None:
    assert serialize_harness_event(ExecutionStart(tool_calls=[])) == {"tool_calls": []}
    out = serialize_harness_event(
        ExecutionEnd(results=[], error=None, should_stop=False, response_text="")
    )
    assert set(out.keys()) == {
        "results",
        "error",
        "should_stop",
        "response_text",
    }


def test_compaction_events_flat() -> None:
    start = serialize_harness_event(
        CompactionStart(
            dropped_message_count=1,
            existing_summary=None,
            keep_recent=5,
            total_tokens=10,
        )
    )
    assert set(start.keys()) == {
        "dropped_message_count",
        "existing_summary",
        "keep_recent",
        "total_tokens",
    }
    chunk = serialize_harness_event(CompactionChunk(delta="d", accumulated="a"))
    assert set(chunk.keys()) == {"delta", "accumulated"}
    end = serialize_harness_event(
        CompactionEnd(
            summary="s",
            dropped_message_count=1,
            new_offset=2,
            duration=1.0,
            error=None,
        )
    )
    assert set(end.keys()) == {
        "summary",
        "dropped_message_count",
        "new_offset",
        "duration",
        "error",
    }


def test_no_type_discriminator_in_payload() -> None:
    """The SSE 'event:' line carries the type; the payload must not.

    A previous version of this function wrapped every event in
    ``{"type": ..., ...}`` which made the frontend read
    ``data.content`` as ``undefined`` and broke the assistant
    message render.
    """
    for event, factory in [
        (
            AgentEnd(
                response="r",
                time_taken=1.0,
                exceeded=False,
                interrupted=False,
                error=None,
            ),
            None,
        ),
        (LLMChunk(chunk=_FakeChunk()), None),
    ]:
        assert "type" not in serialize_harness_event(event)
