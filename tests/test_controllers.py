from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Sequence
from unittest import mock

import pytest

from mh_gateway.services.controllers import (
    GoalController,
    TimerController,
    _format_duration,
    _parse_duration,
)
from minimal_harness.llm.llm import LLMResponse, Stream
from minimal_harness.memory import (
    ConversationMemory,
    Message,
    TextContentPart,
    user_message,
)
from minimal_harness.types import (
    AgentEnd,
    AgentEvent,
    ControllerContinue,
    ControllerEnd,
    ControllerEvent,
    ControllerStart,
    LLMChunkDelta,
)

# ── Fakes ─────────────────────────────────────────────────────────────────


async def _stream_of(content: str | None) -> AsyncIterator[LLMChunkDelta | LLMResponse]:
    if content:
        yield LLMChunkDelta(content=content)
    yield LLMResponse(
        content=content,
        reasoning_content=None,
        tool_calls=[],
        finish_reason=None,
    )


class FakeLLMProvider:
    """可编程模拟 LLM：chat() 依次返回预设内容列表。"""

    def __init__(self, responses: list[str | None]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: Sequence[Message],
        tools: Any = None,
        stop_event: asyncio.Event | None = None,
        **kwargs: Any,
    ) -> Stream[LLMChunkDelta]:
        self.calls.append(
            {"messages": messages, "tools": tools, "stop_event": stop_event}
        )
        content = self.responses.pop(0) if self.responses else "DONE"
        return Stream[LLMChunkDelta](_stream_of(content))


class RaisingLLMProvider:
    """chat() 永远抛异常——验证 judge 失败的安全默认行为。"""

    async def chat(
        self,
        messages: Sequence[Message],
        tools: Any = None,
        stop_event: asyncio.Event | None = None,
        **kwargs: Any,
    ) -> Stream[LLMChunkDelta]:
        raise RuntimeError("provider down")


class _FakeAgent:
    """记录 run 参数并按预设事件序列执行的假 Agent。"""

    def __init__(self, events: list[Any] | None = None) -> None:
        self.events: list[Any] = events or []
        self.run_inputs: list[Any] = []
        self.run_kwargs: list[dict[str, Any]] = []

    async def run(
        self,
        user_input: Any,
        stop_event: asyncio.Event | None = None,
        memory: Any = None,
        tools: Any = None,
        system_prompt: str = "",
        context: Any = None,
        llm_kwargs: dict[str, Any] | None = None,
        **kwargs: Any,
    ):
        self.run_inputs.append(user_input)
        self.run_kwargs.append(kwargs)
        for event in self.events:
            yield event


class _FakeClock:
    """可控时钟，替换 ``controllers.time.time`` 以精确控制 elapsed。

    ``advance`` 是每次调用递增的秒数——execute 里第一调用记 start_time，
    后续调用读取 elapsed，模拟真实流逝。
    """

    def __init__(self, start: float = 1000.0, advance: float = 0.0) -> None:
        self.now = start
        self.advance = advance

    def __call__(self) -> float:
        now = self.now
        self.now += self.advance
        return now


def _memory() -> ConversationMemory:
    return ConversationMemory()


def _input(text: str = "hi") -> list[TextContentPart]:
    return [TextContentPart(type="text", text=text)]


def _agent_end(
    response: str = "done",
    *,
    interrupted: bool = False,
    error: str | None = None,
) -> AgentEnd:
    return AgentEnd(
        response=response,
        time_taken=0.5,
        interrupted=interrupted,
        error=error,
    )


async def _collect(
    controller: Any,
    agent: _FakeAgent,
    controller_config: dict | None = None,
):
    mem = _memory()
    events: list[AgentEvent | ControllerEvent] = []
    async for event in controller.execute(
        agent=agent,
        user_input=_input(),
        stop_event=None,
        memory=mem,
        tools=[],
        controller_config=controller_config,
    ):
        events.append(event)
    return events


# ── GoalController ────────────────────────────────────────────────────────


class TestGoalController:
    async def test_single_round_judge_says_done(self):
        agent = _FakeAgent([_agent_end(response="answer")])
        controller = GoalController(FakeLLMProvider(["DONE"]), max_goal_rounds=5)
        events = await _collect(controller, agent)

        assert isinstance(events[0], ControllerStart)
        assert events[0].controller_type == "goal"
        assert (
            len(events) == 3
        )  # ControllerStart + AgentEnd + ControllerEnd，无 Continue
        end = events[-1]
        assert isinstance(end, ControllerEnd)
        assert end.response == "answer"
        assert end.exceeded is False
        assert end.error is None

    async def test_two_rounds_judge_next_then_done(self):
        agent = _FakeAgent([_agent_end(response="part1"), _agent_end(response="part2")])
        controller = GoalController(
            FakeLLMProvider(["NEXT: do more", "DONE"]), max_goal_rounds=5
        )
        events = await _collect(controller, agent)

        continues = [e for e in events if isinstance(e, ControllerContinue)]
        assert len(continues) == 1
        assert continues[0].next_prompt == "do more"

        # 第二轮 agent 收到的输入是 judge 的 next_prompt，且带上 auto 标记
        assert agent.run_inputs[1] == [{"type": "text", "text": "do more"}]
        assert agent.run_kwargs[0].get("user_message_meta") is None
        assert agent.run_kwargs[1].get("user_message_meta") == {"source": "auto"}

        end = events[-1]
        assert isinstance(end, ControllerEnd)
        assert end.response == "part2"

    async def test_max_rounds_exceeded(self):
        agent = _FakeAgent([_agent_end(response="r1"), _agent_end(response="r2")])
        controller = GoalController(
            FakeLLMProvider(["NEXT: a", "NEXT: b"]), max_goal_rounds=5
        )
        events = await _collect(
            controller, agent, controller_config={"max_goal_rounds": 2}
        )

        continues = [e for e in events if isinstance(e, ControllerContinue)]
        assert len(continues) == 2
        end = events[-1]
        assert isinstance(end, ControllerEnd)
        assert end.exceeded is True
        assert end.response == "r2"

    async def test_agent_interrupted_stops_immediately(self):
        agent = _FakeAgent([_agent_end(response="partial", interrupted=True)])
        controller = GoalController(
            FakeLLMProvider(["NEXT: continue"]), max_goal_rounds=5
        )
        events = await _collect(controller, agent)

        end = events[-1]
        assert isinstance(end, ControllerEnd)
        assert end.interrupted is True
        # judge 不该被调用
        assert controller._llm_provider.calls == []  # type: ignore[attr-defined]

    async def test_agent_error_stops_immediately(self):
        agent = _FakeAgent([_agent_end(response="", error="tool failed")])
        controller = GoalController(
            FakeLLMProvider(["NEXT: continue"]), max_goal_rounds=5
        )
        events = await _collect(controller, agent)

        end = events[-1]
        assert isinstance(end, ControllerEnd)
        assert end.error == "tool failed"

    async def test_judge_error_defaults_to_stop(self):
        agent = _FakeAgent([_agent_end(response="answer")])
        controller = GoalController(RaisingLLMProvider(), max_goal_rounds=5)
        events = await _collect(controller, agent)

        # 安全默认：judge 异常 → DONE（不继续，不报错）
        assert len(events) == 3
        end = events[-1]
        assert isinstance(end, ControllerEnd)
        assert end.error is None
        assert end.response == "answer"

    async def test_judge_call_receives_stop_event(self):
        """验证 _call_judge 把 stop_event 传给了 llm_provider.chat()。"""
        agent = _FakeAgent([_agent_end()])
        provider = FakeLLMProvider(["DONE"])
        controller = GoalController(provider, max_goal_rounds=5)
        stop_event = asyncio.Event()

        mem = _memory()
        async for _ in controller.execute(
            agent=agent,
            user_input=_input(),
            stop_event=stop_event,
            memory=mem,
            tools=[],
        ):
            pass

        assert len(provider.calls) == 1
        assert provider.calls[0]["stop_event"] is stop_event

    async def test_judge_parse_done_case_variants(self):
        c = GoalController(FakeLLMProvider([]), max_goal_rounds=5)
        for content in ["DONE", "done", "Done", "DONE.", "DONE 全部完成", "done "]:
            assert c._parse_judge_response(content) is None, content

    async def test_judge_parse_next_format_variants(self):
        c = GoalController(FakeLLMProvider([]), max_goal_rounds=5)
        assert c._parse_judge_response("NEXT: do it") == "do it"
        assert c._parse_judge_response("Next: do it") == "do it"
        assert c._parse_judge_response("NEXT：做吧") == "做吧"
        assert c._parse_judge_response("NEXT - do it") == "do it"
        assert c._parse_judge_response("NEXT") is None
        assert c._parse_judge_response("") is None
        assert c._parse_judge_response("whatever") is None

    async def test_judge_receives_conversation_messages(self):
        agent = _FakeAgent([_agent_end()])
        provider = FakeLLMProvider(["DONE"])
        controller = GoalController(provider, max_goal_rounds=5)

        mem = _memory()
        await mem.add_message(user_message([{"type": "text", "text": "original goal"}]))
        async for _ in controller.execute(
            agent=agent,
            user_input=_input(),
            stop_event=None,
            memory=mem,
            tools=[],
        ):
            pass

        msgs = provider.calls[0]["messages"]
        roles = [m["role"] for m in msgs]
        assert roles == ["system", "user", "user"]
        assert msgs[1]["content"][0]["text"] == "original goal"
        assert "Reply DONE or NEXT" in msgs[-1]["content"]


# ── TimerController ───────────────────────────────────────────────────────


class TestTimerController:
    async def _collect_with_clock(
        self, controller, agent, clock, controller_config=None
    ):
        with mock.patch("mh_gateway.services.controllers.time.time", clock):
            return await _collect(
                controller, agent, controller_config=controller_config
            )

    async def test_elapsed_under_duration_continues(self):
        clock = _FakeClock(1000.0, advance=7.0)  # 每轮 +7s；start_time 取 1000
        agent = _FakeAgent([_agent_end(response=f"r{i}") for i in range(1, 4)])
        controller = TimerController(
            FakeLLMProvider(["NEXT: keep going"] * 2), default_duration="20s"
        )
        events = await self._collect_with_clock(controller, agent, clock)

        continues = [e for e in events if isinstance(e, ControllerContinue)]
        assert len(continues) == 2  # 前 2 轮 elapsed(7/14) < 20s → 继续
        assert continues[0].next_prompt == "keep going"
        end = events[-1]
        assert isinstance(end, ControllerEnd)
        assert end.exceeded is False  # 第 3 轮 elapsed 21s ≥ 20s → 时间到停
        assert end.response == "r3"

    async def test_elapsed_exceeds_duration_stops(self):
        clock = _FakeClock(1000.0, advance=6.0)  # 每次调用 +6s
        agent = _FakeAgent([_agent_end(response="r1")])
        controller = TimerController(
            FakeLLMProvider(["NEXT: x"]), default_duration="5s"
        )
        events = await self._collect_with_clock(controller, agent, clock)

        assert (
            len(events) == 3
        )  # ControllerStart + AgentEnd + ControllerEnd，judge 不调用
        end = events[-1]
        assert isinstance(end, ControllerEnd)
        assert end.exceeded is False
        assert end.response == "r1"
        assert controller._llm_provider.calls == []  # type: ignore[attr-defined]

    async def test_judge_returns_done_but_time_not_up_uses_forced_prompt(self):
        clock = _FakeClock(1000.0, advance=11.0)  # 第 2 轮 elapsed 22s ≥ 20s 停
        agent = _FakeAgent([_agent_end(response="r1"), _agent_end(response="r2")])
        controller = TimerController(FakeLLMProvider(["DONE"]), default_duration="20s")
        events = await self._collect_with_clock(controller, agent, clock)

        continues = [e for e in events if isinstance(e, ControllerContinue)]
        assert len(continues) == 1
        assert "Continue working on the original task" in continues[0].next_prompt
        assert "20s" in continues[0].next_prompt

        # 第二轮（系统强制继续）的 user 消息同样带 auto 标记
        assert agent.run_kwargs[0].get("user_message_meta") is None
        assert agent.run_kwargs[1].get("user_message_meta") == {"source": "auto"}

    async def test_judge_returns_next_with_time_context(self):
        clock = _FakeClock(1000.0, advance=11.0)  # 第 2 轮 elapsed 22s ≥ 20s 停
        agent = _FakeAgent([_agent_end(response="r1"), _agent_end(response="r2")])
        provider = FakeLLMProvider(["NEXT: finish the rest"])
        controller = TimerController(provider, default_duration="20s")
        events = await self._collect_with_clock(controller, agent, clock)

        continues = [e for e in events if isinstance(e, ControllerContinue)]
        assert continues[0].next_prompt == "finish the rest"

        # judge 的系统 prompt 带时间上下文
        system_msg = provider.calls[0]["messages"][0]["content"]
        assert "Time context" in system_msg
        assert "20s" in system_msg

    async def test_duration_parsing(self):
        assert _parse_duration("30m") == 1800
        assert _parse_duration("1h") == 3600
        assert _parse_duration("300s") == 300
        assert _parse_duration("1.5h") == 5400
        assert _parse_duration("90") == 90
        assert _parse_duration(90) == 90
        assert _parse_duration("garbage") == 300  # 默认 5 分钟
        assert _parse_duration("") == 300
        assert _format_duration(180) == "3m 0s"
        assert _format_duration(3661) == "1h 1m"
        assert _format_duration(45) == "45s"

    async def test_agent_error_stops_immediately(self):
        clock = _FakeClock(1000.0)
        agent = _FakeAgent([_agent_end(response="", error="boom")])
        controller = TimerController(
            FakeLLMProvider(["NEXT: x"]), default_duration="30m"
        )
        events = await self._collect_with_clock(controller, agent, clock)

        end = events[-1]
        assert isinstance(end, ControllerEnd)
        assert end.error == "boom"

    async def test_config_duration_used(self):
        clock = _FakeClock(1000.0, advance=5.0)  # 第 2 轮 elapsed 10s ≥ 10s 停
        agent = _FakeAgent([_agent_end(response="r1"), _agent_end(response="r2")])
        controller = TimerController(FakeLLMProvider([]), default_duration="30m")
        events = await self._collect_with_clock(
            controller,
            agent,
            clock,
            controller_config={"duration": "10s"},
        )
        end = events[-1]
        assert isinstance(end, ControllerEnd)
        # 5 < 10s → 继续，但 judge 返回 None（空列表默认 DONE）→ forced prompt
        assert any(isinstance(e, ControllerContinue) for e in events)


# ── 通过 ControllerRegistry 插入（外部扩展点验证） ───────────────────────


class TestExternalRegistration:
    async def test_gateway_controllers_plug_into_sdk_registry(self):
        """goal/timer 作为"外部 controller"通过 SDK 的 registry 插入并可用。"""
        from minimal_harness.agent.runtime import ControllerRegistry
        from minimal_harness.agent.controller import DefaultController

        reg = ControllerRegistry()
        reg.register(
            "goal", lambda llm_provider: GoalController(llm_provider, max_goal_rounds=5)
        )
        reg.register(
            "timer",
            lambda llm_provider: TimerController(llm_provider, default_duration="30m"),
        )

        goal = reg.create("goal", llm_provider=FakeLLMProvider(["DONE"]))
        timer = reg.create("timer", llm_provider=FakeLLMProvider(["DONE"]))
        fallback = reg.create("unknown", llm_provider=None)

        assert isinstance(goal, GoalController)
        assert isinstance(timer, TimerController)
        assert isinstance(fallback, DefaultController)

        # catalog 顺序 = 注册顺序
        assert [c["value"] for c in reg.catalog()] == ["goal", "timer"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
