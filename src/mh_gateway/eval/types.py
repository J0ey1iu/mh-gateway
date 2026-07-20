from __future__ import annotations

import uuid
from dataclasses import dataclass, field


@dataclass
class EvalQuestion:
    question_id: str
    input_text: str
    scenario_id: str
    agent_name: str


@dataclass
class BatchEvalRequest:
    questions: list[EvalQuestion]
    llm_provider: str
    llm_model: str
    max_concurrency: int = 4


@dataclass
class LLMCallRecord:
    call_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    question_id: str = ""
    scenario_id: str = ""
    agent_name: str = ""
    provider: str = ""
    model: str = ""

    messages: list[dict] = field(default_factory=list)
    response_content: str = ""
    tool_calls: list | None = None

    started_at: float = 0.0
    finished_at: float = 0.0
    duration_ms: float = 0.0

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    error: str | None = None


@dataclass
class QuestionResult:
    question_id: str
    scenario_id: str
    agent_name: str
    input_text: str
    status: str  # "running" | "completed" | "failed" | "interrupted"
    error: str | None = None
    response: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    duration_ms: float = 0.0
    llm_call_count: int = 0
    tool_call_count: int = 0


@dataclass
class BatchSummary:
    batch_id: str
    status: str  # "running" | "completed" | "failed"
    total_questions: int = 0
    completed: int = 0
    failed: int = 0
    interrupted: int = 0
    created_at: float = 0.0
    finished_at: float | None = None
    error: str | None = None
