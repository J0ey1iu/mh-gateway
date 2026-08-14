"""Announcement (bulletin) endpoints.

Split in two surfaces:

* **Admin** (``manage:announcement:*``) — CRUD, re-push (clear every user's
  read/consent state), coverage stats.  Admins are users too: their own
  pushes surface in ``visible`` until they read them.
* **User** — ``visible`` (active + unread + undecided), ``read``,
  ``consent``.  The frontend polls ``visible`` on every page load and pops
  a modal; consent-required announcements gate the UI until a decision.

All storage lives in the deployment's :class:`AnnouncementStore`
implementation (users table, read states, consents included).
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from mh_gateway.adapters import AnnouncementRecord, AnnouncementStore
from mh_gateway.api.dependencies import (
    require_permission,
    resolve_request_identity,
)
from mh_gateway.services.database import get_adapters

logger = logging.getLogger("orchestration.announcements")

router = APIRouter(prefix="/api/v1/announcements", tags=["announcements"])

PERMISSION = "manage:announcement:*"


class AnnouncementPayload(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1)
    consent_required: bool = False
    active: bool = True


def _get_store(request: Request) -> AnnouncementStore:
    store = getattr(get_adapters(request), "announcements", None)
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="Announcements are not enabled in this deployment",
        )
    return store


# ── Admin surface ──────────────────────────────────────────────────────────────


@router.get("")
async def list_announcements(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=0, le=1000),
    user_id: str = Depends(require_permission(PERMISSION)),
):
    store = _get_store(request)
    records, total = await store.list_announcements(page=page, page_size=page_size)
    return {
        "items": [asdict(r) for r in records],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post("")
async def create_announcement(
    payload: AnnouncementPayload,
    request: Request,
    user_id: str = Depends(require_permission(PERMISSION)),
):
    store = _get_store(request)
    record = AnnouncementRecord(
        announcement_id=uuid4().hex,
        title=payload.title,
        body=payload.body,
        consent_required=payload.consent_required,
        active=payload.active,
        pushed_by=user_id,
    )
    stored = await store.create_announcement(record)
    logger.info(
        "announcement.created id=%s title=%r pushed_by=%s",
        stored.announcement_id,
        stored.title,
        user_id,
    )
    return asdict(stored)


@router.patch("/{announcement_id}")
async def update_announcement(
    announcement_id: str,
    payload: AnnouncementPayload,
    request: Request,
    user_id: str = Depends(require_permission(PERMISSION)),
):
    store = _get_store(request)
    existing = await store.get_announcement(announcement_id)
    if existing is None:
        raise HTTPException(404, "Announcement not found")
    existing.title = payload.title
    existing.body = payload.body
    existing.consent_required = payload.consent_required
    existing.active = payload.active
    updated = await store.update_announcement(existing)
    return asdict(updated)


@router.delete("/{announcement_id}")
async def delete_announcement(
    announcement_id: str,
    request: Request,
    user_id: str = Depends(require_permission(PERMISSION)),
):
    store = _get_store(request)
    if not await store.delete_announcement(announcement_id):
        raise HTTPException(404, "Announcement not found")
    return {"ok": True}


@router.post("/{announcement_id}/repush")
async def repush_announcement(
    announcement_id: str,
    request: Request,
    user_id: str = Depends(require_permission(PERMISSION)),
):
    store = _get_store(request)
    if not await store.repush_announcement(announcement_id, pushed_by=user_id):
        raise HTTPException(404, "Announcement not found")
    logger.info("announcement.repushed id=%s by=%s", announcement_id, user_id)
    return {"ok": True}


@router.get("/{announcement_id}/stats")
async def announcement_stats(
    announcement_id: str,
    request: Request,
    user_id: str = Depends(require_permission(PERMISSION)),
):
    store = _get_store(request)
    if await store.get_announcement(announcement_id) is None:
        raise HTTPException(404, "Announcement not found")
    stats = await store.announcement_stats(announcement_id)
    return {
        "read_count": stats.read_count,
        "agree_count": stats.agree_count,
        "decline_count": stats.decline_count,
        "total_users": stats.total_users,
    }


# ── User surface ───────────────────────────────────────────────────────────────


@router.get("/visible")
async def visible_announcements(
    request: Request,
    user_id: str = Depends(resolve_request_identity),
):
    store = _get_store(request)
    records = await store.visible_announcements(user_id)
    return [asdict(r) for r in records]


@router.post("/{announcement_id}/read")
async def mark_read(
    announcement_id: str,
    request: Request,
    user_id: str = Depends(resolve_request_identity),
):
    store = _get_store(request)
    if not await store.mark_read(announcement_id, user_id):
        raise HTTPException(404, "Announcement not found")
    return {"ok": True}


class ConsentPayload(BaseModel):
    decision: Literal["agree", "decline"]


@router.post("/{announcement_id}/consent")
async def record_consent(
    announcement_id: str,
    payload: ConsentPayload,
    request: Request,
    user_id: str = Depends(resolve_request_identity),
):
    store = _get_store(request)
    if not await store.record_consent(announcement_id, user_id, payload.decision):
        raise HTTPException(404, "Announcement not found")
    return {"ok": True}
