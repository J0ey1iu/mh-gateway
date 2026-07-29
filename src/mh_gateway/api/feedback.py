"""User feedback submission endpoint.

``POST /api/v1/feedback`` — submit feedback on a message or tool call.
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
    rating: int | None = None
    comment: str | None = None
    category: str | None = None


@router.get("")
async def list_session_feedback(
    request: Request,
    session_id: str | None = Query(
        None, description="Filter by session_id"
    ),
    user_id: str = Depends(resolve_request_identity),
) -> list[dict[str, Any]]:
    """Return feedback entries, optionally filtered by session."""
    adapters = request.app.state.adapters
    if adapters.feedback is None:
        return []
    items, _ = await adapters.feedback.list(
        page=1, page_size=0, q=None
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
            "rating": fb.rating,
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

    feedback = Feedback(
        feedback_id=f"fb_{uuid4().hex[:12]}",
        session_id=body.session_id,
        target_type=body.target_type,
        target_id=body.target_id,
        user_id=user_id,
        feedback_type=body.feedback_type,
        rating=body.rating,
        comment=body.comment,
        category=body.category,
        source="ui_button",
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
