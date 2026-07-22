from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from fastapi import Request
from minimal_harness.agent.middleware import Middleware
from minimal_harness.types import AgentEnd, AgentMetadata, LLMEnd, ToolCall

from mh_gateway.adapters import EvalResultRepository
from mh_gateway.eval.types import (
    BatchEvalRequest,
    BatchSummary,
    EvalQuestion,
    LLMCallRecord,
    QuestionResult,
)
from mh_gateway.services.database import get_session_store
from mh_gateway.services.runtime_service import create_runtime

logger = logging.getLogger("orchestration.eval.runner")


class LLMCaptureMiddleware(Middleware):
    def __init__(self, question: EvalQuestion, provider: str, model: str) -> None:
        super().__init__()
        self._question = question
        self._provider = provider
        self._model = model
        self._current_messages: list[dict] | None = None
        self._started_at: float = 0.0
        self.records: list[LLMCallRecord] = []
        self.tool_call_count: int = 0

    async def on_llm_start(self, messages: list[dict], tools: Any) -> None:
        self._current_messages = list(messages)
        self._started_at = time.time()

    async def on_llm_end(self, event: LLMEnd) -> None:
        if self._current_messages is None:
            return
        finished_at = time.time()
        usage = event.usage or {}
        record = LLMCallRecord(
            call_id=str(uuid.uuid4())[:8],
            question_id=self._question.question_id,
            scenario_id=self._question.scenario_id,
            agent_name=self._question.agent_name,
            provider=self._provider,
            model=self._model,
            messages=self._current_messages,
            response_content=event.content or "",
            tool_calls=list(event.tool_calls) if event.tool_calls else None,
            started_at=self._started_at,
            finished_at=finished_at,
            duration_ms=(finished_at - self._started_at) * 1000,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
            error=event.error,
        )
        self.records.append(record)
        self._current_messages = None
        self._started_at = 0.0

    async def on_tool_start(self, tool_call: ToolCall) -> None:
        self.tool_call_count += 1


async def _resolve_agent_tools(
    metadata: Any, scenario_id: str, agent_name: str
) -> list[str]:
    scenarios = await metadata.list_scenarios()
    scenario = next((s for s in scenarios if s["id"] == scenario_id), None)
    if scenario is None:
        raise ValueError(f"Scenario '{scenario_id}' not found")
    for a in scenario.get("agents", []):
        if a["name"] == agent_name:
            return a.get("tool_names", [])
    raise ValueError(f"Agent '{agent_name}' not found in scenario '{scenario_id}'")


async def run_batch_eval(
    request: Request,
    user_id: str,
    batch_request: BatchEvalRequest,
    storage: EvalResultRepository,
    batch_id: str,
) -> BatchSummary:
    adapters = request.app.state.adapters
    semaphore = asyncio.Semaphore(batch_request.max_concurrency)

    summary = BatchSummary(
        batch_id=batch_id,
        status="running",
        total_questions=len(batch_request.questions),
        created_at=time.time(),
    )
    await storage.on_batch_started(batch_id, batch_request)

    _cancelled = False

    async def run_question(
        question: EvalQuestion,
    ) -> QuestionResult:
        nonlocal _cancelled
        result = QuestionResult(
            question_id=question.question_id,
            scenario_id=question.scenario_id,
            agent_name=question.agent_name,
            input_text=question.input_text,
            status="running",
            started_at=time.time(),
        )

        capture_mw: LLMCaptureMiddleware | None = None
        llm_records: list[LLMCallRecord] = []

        try:
            await storage.on_question_started(batch_id, question)

            async with semaphore:
                if _cancelled:
                    result.status = "interrupted"
                else:
                    tool_names = await _resolve_agent_tools(
                        adapters.metadata,
                        question.scenario_id,
                        question.agent_name,
                    )

                    capture_mw = LLMCaptureMiddleware(
                        question, batch_request.llm_provider, batch_request.llm_model
                    )

                    session_id = f"eval_{batch_id}_{question.question_id}"
                    store = await get_session_store(request)
                    agent_meta = await adapters.metadata.get_agent(
                        question.agent_name
                    )
                    await store.create_session(
                        session_id=session_id,
                        agent_name=question.agent_name,
                        user_id=user_id,
                        scenario_id=question.scenario_id,
                        display_name_locale=agent_meta.get("display_name_locale")
                        if agent_meta
                        else None,
                    )

                    runtime, agent_registry, _, _ = await create_runtime(
                        request=request,
                        user_id=user_id,
                        agent_name=question.agent_name,
                        tool_names=tool_names,
                        session_store=store,
                        session_id=session_id,
                        scenario_id=question.scenario_id,
                        provider=batch_request.llm_provider,
                        model=batch_request.llm_model,
                        extra_middleware=[capture_mw],
                    )

                    existing = await agent_registry.get(question.agent_name)
                    if existing is not None:
                        overridden = AgentMetadata(
                            name=existing.name,
                            display_name=existing.display_name,
                            display_name_locale=existing.display_name_locale,
                            description=existing.description,
                            description_locale=existing.description_locale,
                            system_prompt=existing.system_prompt,
                            system_prompt_locale=existing.system_prompt_locale,
                            metadata_id=existing.metadata_id,
                            tool_names=existing.tool_names,
                            provider=batch_request.llm_provider,
                            model=batch_request.llm_model,
                            llm_config=existing.llm_config,
                        )
                        await agent_registry.register(overridden)
                    else:
                        logger.warning(
                            "Agent '%s' not found in registry — provider/model override skipped",
                            question.agent_name,
                        )

                    events = await runtime.run_batch(
                        user_input=[{"type": "text", "text": question.input_text}],
                        agent_metadata_id=question.agent_name,
                        memory_id=session_id,
                        tool_names=tool_names,
                    )

                    for event in events:
                        if isinstance(event, AgentEnd):
                            result.response = event.response or ""
                            if event.time_taken is not None:
                                result.duration_ms = event.time_taken * 1000
                            result.llm_call_count = len(capture_mw.records)
                            result.tool_call_count = capture_mw.tool_call_count
                            if event.interrupted:
                                result.status = "interrupted"
                            elif event.error:
                                result.status = "failed"
                                result.error = event.error
                            else:
                                result.status = "completed"
                            break

        except asyncio.CancelledError:
            result.status = "interrupted"
        except Exception as exc:
            logger.exception("Question %s failed", question.question_id)
            result.status = "failed"
            result.error = f"{type(exc).__name__}: {exc}"

        result.finished_at = time.time()
        if result.duration_ms <= 0.0 and result.started_at:
            result.duration_ms = (result.finished_at - result.started_at) * 1000

        if capture_mw is not None:
            llm_records = capture_mw.records

        for record in llm_records:
            try:
                await storage.on_llm_call_recorded(batch_id, record)
            except Exception:
                logger.exception(
                    "Failed to persist LLM call record %s",
                    record.call_id,
                )

        await storage.on_question_completed(batch_id, result)
        return result

    tasks = [asyncio.create_task(run_question(q)) for q in batch_request.questions]

    results: list[QuestionResult] = []
    try:
        for coro in asyncio.as_completed(tasks):
            result = await coro
            results.append(result)
    except asyncio.CancelledError:
        _cancelled = True
        for t in tasks:
            if not t.done():
                t.cancel()
        for t in tasks:
            try:
                results.append(await t)
            except (asyncio.CancelledError, Exception):
                pass

    results.sort(key=lambda r: r.started_at)
    result_map = {r.question_id: r for r in results}
    for q in batch_request.questions:
        if q.question_id not in result_map:
            orphan = QuestionResult(
                question_id=q.question_id,
                scenario_id=q.scenario_id,
                agent_name=q.agent_name,
                input_text=q.input_text,
                status="interrupted",
                started_at=0.0,
                finished_at=time.time(),
            )
            results.append(orphan)
            try:
                await storage.on_question_completed(batch_id, orphan)
            except Exception:
                logger.exception(
                    "Failed to persist interrupted question %s", q.question_id
                )

    results.sort(key=lambda r: r.started_at)
    completed = sum(1 for r in results if r.status == "completed")
    failed = sum(1 for r in results if r.status == "failed")
    interrupted = sum(1 for r in results if r.status == "interrupted")

    summary.completed = completed
    summary.failed = failed
    summary.interrupted = interrupted
    if _cancelled:
        summary.status = "failed"
        summary.error = "Batch was cancelled"
    elif completed > 0:
        summary.status = "completed"
    else:
        summary.status = "failed"
    summary.finished_at = time.time()

    await storage.on_batch_completed(batch_id, summary)
    return summary
