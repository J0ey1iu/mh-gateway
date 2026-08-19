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
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from mh_gateway.adapters import AnnouncementRecord, AnnouncementStore
from mh_gateway.api.dependencies import (
    require_permission,
    resolve_request_identity,
)
from mh_gateway.metrics_repo import AnnouncementEventRecord, get_metrics_repo
from mh_gateway.services.database import get_adapters

logger = logging.getLogger("orchestration.announcements")

router = APIRouter(prefix="/api/v1/announcements", tags=["announcements"])

PERMISSION = "manage:announcement:*"

ALLOWED_MEDIA_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4"}

_MEDIA_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "mp4": "video/mp4",
}


class AnnouncementPayload(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1)
    # 标题/正文的多语言 JSON（{"zh": …,"en": …}，空 = 未提供）
    title_locale: str = ""
    body_locale: str = ""
    consent_required: bool = False
    active: bool = True
    scope: Literal["all", "scene"] = "all"
    scene_id: str = ""
    image: str = ""
    style: Literal["image_text", "text_only"] = "image_text"
    # 媒体 URL 列表（图片 + mp4 视频），image_text 大版本轮播
    media: list[str] = Field(default_factory=list)
    # "draft" = 草稿（用户端不可见）；"published" = 已发布
    status: Literal["draft", "published"] = "published"
    # 生效时间范围（ISO 8601，空 = 不限）
    start_time: str = ""
    end_time: str = ""


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
        title_locale=payload.title_locale,
        body_locale=payload.body_locale,
        consent_required=payload.consent_required,
        active=payload.active,
        pushed_by=user_id,
        scope=payload.scope,
        scene_id=payload.scene_id,
        image=payload.image,
        style=payload.style,
        media=payload.media,
        status=payload.status,
        start_time=payload.start_time,
        end_time=payload.end_time,
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
    existing.title_locale = payload.title_locale
    existing.body_locale = payload.body_locale
    existing.consent_required = payload.consent_required
    existing.active = payload.active
    existing.scope = payload.scope
    existing.scene_id = payload.scene_id
    existing.image = payload.image
    existing.style = payload.style
    existing.media = payload.media
    existing.status = payload.status
    existing.start_time = payload.start_time
    existing.end_time = payload.end_time
    # 草稿 → 发布：视为一次新推送，排在前面
    if existing.status == "draft" and payload.status == "published":
        existing.pushed_at = datetime.now(UTC).isoformat()
        existing.pushed_by = user_id
    updated = await store.update_announcement(existing)
    return asdict(updated)


@router.delete("/{announcement_id}")
async def delete_announcement(
    announcement_id: str,
    request: Request,
    user_id: str = Depends(require_permission(PERMISSION)),
):
    store = _get_store(request)
    existing = await store.get_announcement(announcement_id)
    if existing is None:
        raise HTTPException(404, "Announcement not found")
    if existing.status != "draft":
        raise HTTPException(409, "Only draft announcements can be deleted")
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
        "confirm_count": stats.confirm_count,
        "agree_count": stats.agree_count,
        "decline_count": stats.decline_count,
        "total_users": stats.total_users,
    }


# ── Announcement media (images + mp4 videos) ────────────────────────────────


@router.post("/image")
async def upload_announcement_image(
    request: Request,
    file: UploadFile = File(...),
    user_id: str = Depends(require_permission(PERMISSION)),
):
    store = _get_store(request)
    ext = Path(file.filename or "").suffix.lower().lstrip(".")
    if f".{ext}" not in ALLOWED_MEDIA_EXT:
        raise HTTPException(
            400,
            f"Unsupported media type '.{ext}'. Allowed: "
            + ", ".join(sorted(ALLOWED_MEDIA_EXT)),
        )
    data = await file.read()
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(413, "Media too large: 20 MB limit")
    image_id = await store.save_image(data, _MEDIA_TYPES[ext])
    url = f"/api/v1/announcements/images/{image_id}"
    logger.info("announcement.media.uploaded id=%s by=%s", image_id, user_id)
    return {"image_id": image_id, "url": url}


@router.get("/images/{image_id}")
async def get_announcement_image(request: Request, image_id: str):
    store = _get_store(request)
    opened = await store.open_image(image_id)
    if opened is None:
        raise HTTPException(404, "Image not found")
    data, content_type = opened
    return Response(content=data, media_type=content_type)


# ── User surface ───────────────────────────────────────────────────────────────


async def _record_announcement_event(
    kind: str, user_id: str, announcement_id: str
) -> None:
    """与其它运维指标一致的打点（metrics repo 未配置时静默跳过）。"""
    repo = get_metrics_repo()
    if repo is None:
        return
    await repo.record_announcement_event(
        AnnouncementEventRecord(
            ts=datetime.now(UTC).isoformat(),
            user_id=user_id,
            announcement_id=announcement_id,
            kind=kind,
        )
    )


@router.get("/visible")
async def visible_announcements(
    request: Request,
    scene_id: str | None = Query(
        None, description="进入场景时传场景 id；不传 = 场景主页"
    ),
    user_id: str = Depends(resolve_request_identity),
):
    store = _get_store(request)
    records = await store.visible_announcements(user_id, scene_id)
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
    await _record_announcement_event("exposure", user_id, announcement_id)
    return {"ok": True}


class ConsentPayload(BaseModel):
    decision: Literal["agree", "decline"]


@router.post("/{announcement_id}/confirm")
async def confirm_announcement(
    announcement_id: str,
    request: Request,
    user_id: str = Depends(resolve_request_identity),
):
    """用户点"我知道"按钮：同时计入曝光和确认。"""
    store = _get_store(request)
    if not await store.mark_confirmed(announcement_id, user_id):
        raise HTTPException(404, "Announcement not found")
    await _record_announcement_event("exposure", user_id, announcement_id)
    await _record_announcement_event("confirm", user_id, announcement_id)
    return {"ok": True}


@router.post("/{announcement_id}/consent")
async def record_consent(
    announcement_id: str,
    payload: ConsentPayload,
    request: Request,
    user_id: str = Depends(resolve_request_identity),
):
    """consent 型公告：同意计入确认，拒绝仅计曝光。"""
    store = _get_store(request)
    if not await store.record_consent(announcement_id, user_id, payload.decision):
        raise HTTPException(404, "Announcement not found")
    await _record_announcement_event("exposure", user_id, announcement_id)
    if payload.decision == "agree":
        await store.mark_confirmed(announcement_id, user_id)
        await _record_announcement_event("confirm", user_id, announcement_id)
    return {"ok": True}
