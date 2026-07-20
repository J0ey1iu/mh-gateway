from __future__ import annotations

import json
from typing import Any, AsyncIterator

from minimal_harness.llm.llm import LLMProvider
from minimal_harness.memory import Message

SUMMARIZER_SYSTEM_PROMPT = (
    "You are a conversation compressor. You receive the recent "
    "conversation transcript and a prior summary (if any). Your job is "
    "to produce a single, dense summary that preserves:\n"
    "  1. The user's original goal and any updated goals.\n"
    "  2. Concrete facts, decisions, and outcomes.\n"
    "  3. The current state of any in-progress work.\n"
    "Be terse. Use bullet points. Do not include pleasantries."
)


def _format_messages_for_summary(
    messages: list[Message],
    existing_summary: str | None = None,
) -> list[dict[str, Any]]:
    transcript = json.dumps(
        [
            {"role": m.get("role"), "content": str(m.get("content", ""))[:2000]}
            for m in messages
        ],
        ensure_ascii=False,
        indent=1,
    )
    parts = ["Conversation transcript to fold:\n" + transcript]
    if existing_summary:
        parts.append(f"Prior summary (fold into the new one):\n{existing_summary}")
    parts.append(
        "Fold the above into an updated summary. "
        "Return only the new summary text -- no preamble, no labels."
    )
    return [
        {"role": "system", "content": SUMMARIZER_SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def make_llm_summarizer(llm_provider: LLMProvider):
    """Build a ``CompactionSummarizer`` from an LLM provider."""

    async def _summarize(
        messages: list[Message],
        existing_summary: str | None,
    ) -> AsyncIterator[str]:
        payload = _format_messages_for_summary(messages, existing_summary)
        response = await llm_provider.chat(messages=payload, tools=[])  # type: ignore[arg-type]
        async for chunk in response:
            if chunk.content:
                yield chunk.content
        final = response.response
        if final.content:
            yield final.content

    return _summarize
