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
    ControllerContinue,
    ControllerEnd,
    ControllerStart,
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


def test_llm_chunk_tool_calls_are_plain_dicts() -> None:
    """Streaming tool-call deltas must serialize as JSON objects, not
    dataclass repr strings (the frontend accumulates them into the
    provisional tool-call card)."""
    from minimal_harness.types import LLMChunkDelta, ToolCallDelta

    chunk = LLMChunk(
        chunk=LLMChunkDelta(
            content=None,
            reasoning=None,
            tool_calls=[
                ToolCallDelta(index=0, id="call_1", name="bash", arguments='{"cmd": '),
                ToolCallDelta(index=0, arguments='"ls"}'),
            ],
        )
    )
    out = serialize_harness_event(chunk)
    assert out["tool_calls"] == [
        {"index": 0, "id": "call_1", "name": "bash", "arguments": '{"cmd": '},
        {"index": 0, "id": None, "name": None, "arguments": '"ls"}'},
    ]
    import json

    # Round-trip through the SSE serializer: must be real JSON, not a
    # repr string like "ToolCallDelta(index=0, ...)".
    data = json.loads(json.dumps(out, ensure_ascii=False, default=str))
    assert isinstance(data["tool_calls"], list)
    assert data["tool_calls"][0]["name"] == "bash"
    assert data["tool_calls"][0]["arguments"] == '{"cmd": '


def test_llm_chunk_tool_calls_none_when_absent() -> None:
    out = serialize_harness_event(LLMChunk(chunk=_FakeChunk()))
    assert out["tool_calls"] is None


def test_llm_end_fields_at_top_level() -> None:
    out = serialize_harness_event(
        LLMEnd(
            content="c",
            reasoning_content="r",
            tool_calls=None,
            usage={"total_tokens": 1},
            error=None,
            message_id="msg-3",
        )
    )
    assert set(out.keys()) == {
        "content",
        "reasoning_content",
        "tool_calls",
        "usage",
        "error",
        "message_id",
    }
    assert out["content"] == "c"
    assert out["reasoning_content"] == "r"
    assert out["message_id"] == "msg-3"


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
            message_id="msg-3",
        )
    )
    assert set(out.keys()) == {
        "response",
        "time_taken",
        "exceeded",
        "interrupted",
        "error",
        "message_id",
    }
    assert out["message_id"] == "msg-3"


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
    # the canonical id stamped by Memory.add_message rides along verbatim
    out2 = serialize_harness_event(
        MessageEvent(message={"role": "assistant", "content": "hi", "id": "msg-0"})
    )
    assert out2 == {"message": {"role": "assistant", "content": "hi", "id": "msg-0"}}


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
            message_id="msg-8",
        )
    )
    assert set(end.keys()) == {
        "summary",
        "dropped_message_count",
        "new_offset",
        "duration",
        "error",
        "message_id",
    }
    assert end["message_id"] == "msg-8"


def test_controller_events_flat() -> None:
    start = serialize_harness_event(
        ControllerStart(controller_type="goal", user_input="hi")
    )
    assert set(start.keys()) == {"controller_type", "user_input"}
    assert start["controller_type"] == "goal"

    cont = serialize_harness_event(
        ControllerContinue(
            controller_type="timer",
            next_prompt="keep going",
        )
    )
    assert set(cont.keys()) == {"controller_type", "next_prompt"}
    assert cont["next_prompt"] == "keep going"

    end = serialize_harness_event(
        ControllerEnd(
            controller_type="goal",
            response="r",
            time_taken=1.0,
            exceeded=True,
            interrupted=False,
            error=None,
        )
    )
    assert set(end.keys()) == {
        "controller_type",
        "response",
        "time_taken",
        "exceeded",
        "interrupted",
        "error",
    }
    assert end["exceeded"] is True


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
