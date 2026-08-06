"""User feedback submission endpoint.

``POST /api/v1/feedback`` — submit feedback on a message or tool call.
``PUT /api/v1/feedback/{id}`` — backfill comment/category on an entry.
``DELETE /api/v1/feedback/{id}`` — cancel a like/dislike.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from mh_gateway.adapters import Feedback
from mh_gateway.api.dependencies import resolve_request_identity

logger = logging.getLogger("orchestration.feedback")

router = APIRouter(prefix="/api/v1/feedback", tags=["feedback"])


class FeedbackCreateRequest(BaseModel):
    session_id: str
    target_type: str = "message"  # "message" | "tool_call"
    target_id: str = ""
    feedback_type: str  # "thumbs_up" | "thumbs_down"
    comment: str | None = None
    category: str | None = None


class FeedbackUpdateRequest(BaseModel):
    """Backfill comment/category after a bare like/dislike."""

    comment: str | None = None
    category: str | None = None


async def _owned_feedback(adapters: Any, feedback_id: str, user_id: str) -> Feedback:
    """Fetch a feedback entry, raising 404/403 unless the caller owns it."""
    fb = await adapters.feedback.get(feedback_id)
    if fb is None:
        raise HTTPException(404, "Feedback not found")
    if fb.user_id != user_id:
        raise HTTPException(403, "Access denied")
    return fb


@router.get("")
async def list_session_feedback(
    request: Request,
    session_id: str | None = Query(None, description="Filter by session_id"),
    user_id: str = Depends(resolve_request_identity),
) -> list[dict[str, Any]]:
    """Return feedback entries, optionally filtered by session."""
    adapters = request.app.state.adapters
    if adapters.feedback is None:
        return []
    items, _ = await adapters.feedback.list(
        page=1,
        # The session list must return EVERY row for the session — the
        # endpoint filters by session_id itself after fetching. A large
        # page_size mirrors the CSV export (page_size=999999); stores
        # additionally treat page_size <= 0 as "all".
        page_size=100000,
        q=None,
    )
    if session_id:
        items = [fb for fb in items if fb.session_id == session_id]
    # Return only items belonging to the current user?
    # For mh-local there's only one user, so skip ownership check.
    return [
        {
            "feedback_id": fb.feedback_id,
            "target_type": fb.target_type,
            "target_id": fb.target_id,
            "feedback_type": fb.feedback_type,
            "comment": fb.comment,
            "created_at": fb.created_at,
        }
        for fb in items
    ]


@router.post("")
async def submit_feedback(
    request: Request,
    body: FeedbackCreateRequest,
    user_id: str = Depends(resolve_request_identity),
) -> dict[str, Any]:
    adapters = request.app.state.adapters
    if adapters.feedback is None:
        raise HTTPException(501, "Feedback storage not configured")

    # verify session exists and belongs to user
    session = await adapters.sessions.get_session(body.session_id)
    if session is None:
        raise HTTPException(404, "Session not found")
    if getattr(session, "user_id", None) != user_id:
        raise HTTPException(403, "Access denied")
    agent_name = getattr(session, "agent_name", "") or ""

    feedback = Feedback(
        feedback_id=f"fb_{uuid4().hex[:12]}",
        session_id=body.session_id,
        target_type=body.target_type,
        target_id=body.target_id,
        user_id=user_id,
        feedback_type=body.feedback_type,
        comment=body.comment,
        category=body.category,
        source="ui_button",
        agent_name=agent_name,
        metadata={},
        created_at="",
    )
    saved = await adapters.feedback.save(feedback)
    logger.info(
        "Feedback saved id=%s type=%s user=%s session=%s",
        saved.feedback_id,
        saved.feedback_type,
        user_id,
        body.session_id,
    )
    return {"feedback_id": saved.feedback_id, "ok": True}


@router.put("/{feedback_id}")
async def update_feedback(
    request: Request,
    feedback_id: str,
    body: FeedbackUpdateRequest,
    user_id: str = Depends(resolve_request_identity),
) -> dict[str, Any]:
    """Backfill comment/category on an existing feedback entry."""
    adapters = request.app.state.adapters
    if adapters.feedback is None:
        raise HTTPException(501, "Feedback storage not configured")
    fb = await _owned_feedback(adapters, feedback_id, user_id)
    comment = body.comment if body.comment is not None else fb.comment
    category = body.category if body.category is not None else fb.category
    await adapters.feedback.update_content(
        feedback_id, comment=comment, category=category
    )
    logger.info("Feedback updated id=%s user=%s", feedback_id, user_id)
    return {"feedback_id": feedback_id, "ok": True}


@router.delete("/{feedback_id}")
async def delete_feedback(
    request: Request,
    feedback_id: str,
    user_id: str = Depends(resolve_request_identity),
) -> dict[str, Any]:
    """Cancel a like/dislike: delete the feedback entry."""
    adapters = request.app.state.adapters
    if adapters.feedback is None:
        raise HTTPException(501, "Feedback storage not configured")
    await _owned_feedback(adapters, feedback_id, user_id)
    await adapters.feedback.delete(feedback_id)
    logger.info("Feedback deleted id=%s user=%s", feedback_id, user_id)
    return {"feedback_id": feedback_id, "ok": True}
