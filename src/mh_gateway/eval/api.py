from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from mh_gateway.api.dependencies import verify_m2m_request
from mh_gateway.eval.runner import run_batch_eval
from mh_gateway.adapters import EvalResultStorage
from mh_gateway.eval.types import (
    BatchEvalRequest,
    EvalQuestion,
)

logger = logging.getLogger("orchestration.eval.api")

router = APIRouter(prefix="/api/v1/eval", tags=["eval"])

_running_tasks: dict[str, asyncio.Task] = {}


class EvalQuestionSchema(BaseModel):
    question_id: str
    input_text: str
    scenario_id: str
    agent_name: str


class BatchEvalRequestSchema(BaseModel):
    questions: list[EvalQuestionSchema] = Field(min_length=1)
    llm_provider: str
    llm_model: str
    max_concurrency: int = 4


def _get_storage(request: Request) -> EvalResultStorage:
    storage = getattr(request.app.state.adapters, "eval_result_storage", None)
    if storage is None:
        raise HTTPException(
            status_code=501,
            detail="No EvalResultStorage adapter configured",
        )
    if not isinstance(storage, EvalResultStorage):
        raise HTTPException(
            status_code=500,
            detail="Configured eval_result_storage does not implement EvalResultStorage",
        )
    return storage


@router.post("/batches", status_code=201)
async def create_batch_eval(
    request: Request,
    body: BatchEvalRequestSchema,
    app_id: str = Depends(verify_m2m_request),
):
    storage = _get_storage(request)

    batch_request = BatchEvalRequest(
        questions=[
            EvalQuestion(
                question_id=q.question_id,
                input_text=q.input_text,
                scenario_id=q.scenario_id,
                agent_name=q.agent_name,
            )
            for q in body.questions
        ],
        llm_provider=body.llm_provider,
        llm_model=body.llm_model,
        max_concurrency=body.max_concurrency,
    )

    batch_id = str(uuid.uuid4())[:12]

    async def _run_and_cleanup():
        try:
            await run_batch_eval(
                request=request,
                user_id=app_id,
                batch_request=batch_request,
                storage=storage,
                batch_id=batch_id,
            )
        finally:
            _running_tasks.pop(batch_id, None)

    task = asyncio.create_task(_run_and_cleanup())
    _running_tasks[batch_id] = task

    return {"batch_id": batch_id, "status": "running"}


@router.get("/batches")
async def list_batches(
    request: Request,
    app_id: str = Depends(verify_m2m_request),
):
    storage = _get_storage(request)
    batches = await storage.list_batches()
    return [
        {
            "batch_id": b.batch_id,
            "status": b.status,
            "total_questions": b.total_questions,
            "completed": b.completed,
            "failed": b.failed,
            "interrupted": b.interrupted,
            "created_at": b.created_at,
            "finished_at": b.finished_at,
            "error": b.error,
        }
        for b in batches
    ]


@router.get("/batches/{batch_id}")
async def get_batch(
    batch_id: str,
    request: Request,
    app_id: str = Depends(verify_m2m_request),
):
    storage = _get_storage(request)
    summary = await storage.get_batch(batch_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="Batch not found")
    return {
        "batch_id": summary.batch_id,
        "status": summary.status,
        "total_questions": summary.total_questions,
        "completed": summary.completed,
        "failed": summary.failed,
        "interrupted": summary.interrupted,
        "created_at": summary.created_at,
        "finished_at": summary.finished_at,
        "error": summary.error,
    }


@router.post("/batches/{batch_id}/cancel")
async def cancel_batch(
    batch_id: str,
    request: Request,
    app_id: str = Depends(verify_m2m_request),
):
    task = _running_tasks.get(batch_id)
    if task is None or task.done():
        summary = await _get_storage(request).get_batch(batch_id)
        if summary is None:
            raise HTTPException(status_code=404, detail="Batch not found")
        if summary.status in ("completed", "failed"):
            raise HTTPException(
                status_code=400, detail=f"Batch already {summary.status}"
            )
        raise HTTPException(status_code=400, detail="Batch is not running")

    task.cancel()
    logger.info("Batch %s cancelled by app_id=%s", batch_id, app_id)
    return {"batch_id": batch_id, "status": "cancelling"}
