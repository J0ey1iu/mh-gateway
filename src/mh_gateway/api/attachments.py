"""Attachment upload / download endpoints.

Upload is session-free (the frontend uploads as soon as the user picks a
file — the session may not exist yet); ownership is enforced by user.  The
file gets bound to a session when it is included in a chat request
(``POST /api/v1/chat/{id}`` → ``attachments``), after which attachment tools
and this download endpoint can enforce session-level access.
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response

from mh_gateway.adapters import AttachmentRecord, AttachmentStore
from mh_gateway.api.dependencies import resolve_request_identity
from mh_gateway.services.database import get_adapters

logger = logging.getLogger("orchestration.attachments")

router = APIRouter(prefix="/api/v1/attachments", tags=["attachments"])


def _get_store(request: Request) -> AttachmentStore:
    store = getattr(get_adapters(request), "attachments", None)
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="Attachments are not enabled in this deployment",
        )
    return store


@router.post("")
async def upload_attachment(
    request: Request,
    file: UploadFile = File(...),
    user_id: str = Depends(resolve_request_identity),
):
    store = _get_store(request)
    file_name = (file.filename or "file").strip() or "file"
    ext = Path(file_name).suffix.lower().lstrip(".")
    settings = get_adapters(request).settings

    allowed = settings.attachment_allowed_extensions
    if allowed and ext not in allowed:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported file type '.{ext}'. Allowed: "
                + ", ".join(f".{e}" for e in allowed)
            ),
        )

    data = await file.read()
    if len(data) > settings.attachment_max_size_mb * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail=(
                f"File too large: {len(data)} bytes exceeds the "
                f"{settings.attachment_max_size_mb} MB limit"
            ),
        )

    record = AttachmentRecord(
        file_id=uuid4().hex,
        file_name=file_name,
        file_size=len(data),
        content_type=file.content_type or "application/octet-stream",
        user_id=user_id,
    )
    await store.save(record, data)
    logger.info(
        "attachment.uploaded file_id=%s name=%s size=%d user=%s",
        record.file_id,
        file_name,
        len(data),
        user_id,
    )
    return record.as_metadata()


@router.get("/{file_id}")
async def download_attachment(
    request: Request,
    file_id: str,
    user_id: str = Depends(resolve_request_identity),
):
    store = _get_store(request)
    record = await store.get(file_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    if record.user_id != user_id:
        raise HTTPException(status_code=403, detail="Access denied")
    data = await store.open(file_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Attachment data not found")
    # filename*=UTF-8''… keeps non-ASCII names intact on download.
    disposition = f"attachment; filename*=UTF-8''{quote(record.file_name)}"
    return Response(
        content=data,
        media_type=record.content_type or "application/octet-stream",
        headers={"Content-Disposition": disposition},
    )
