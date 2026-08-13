"""Built-in attachment tools — the model-facing interface for uploaded files.

These are the *only* way a cloud agent can reach attachment content (cloud
deployments ship no bash / local file tools); the local app seeds them too so
both apps exercise the exact same engine path.  Implementations are
deliberately path-free: they address files by ``file_id`` and resolve the
store + session ownership from the per-request contextvars, so the gateway
never leaks filesystem paths to the model.

Tool metadata lives in ``attachment_tools.json`` (single source of truth, in
import-ready format with zh/en i18n) and is loaded here so both apps can seed
it from one place (``ATTACHMENT_TOOL_METADATA``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, AsyncIterator

from mh_gateway.attachments import (
    FileExtractor,
    UnsupportedFormatError,
    extract_attachment_text,
)
from mh_gateway.context import get_current_request, get_current_session_id
from mh_gateway.services.database import get_adapters

# Cap per read_attachment page — extraction can produce very long single
# lines (e.g. a pptx slide), and a page must stay cheap for the model.
PAGE_CHAR_CAP = 40_000


def _adapters() -> Any:
    """Resolve the active GatewayAdapters bundle for the current request."""
    request = get_current_request()
    if request is None:
        return None
    return get_adapters(request)


def _store() -> Any:
    """Resolve the active AttachmentStore for the current request."""
    adapters = _adapters()
    if adapters is None:
        return None
    return getattr(adapters, "attachments", None)


def _injected_extractors() -> list[FileExtractor]:
    """Deployment-injected extractors (``GatewayAdapters.attachment_extractors``).
    They take precedence over the gateway's built-in defaults inside
    ``get_extractor`` — apps can override a default format or add new ones
    without touching the gateway."""
    adapters = _adapters()
    if adapters is None:
        return []
    return list(getattr(adapters, "attachment_extractors", None) or [])


async def list_attachments_fn() -> AsyncIterator[Any]:
    store = _store()
    session_id = get_current_session_id()
    if store is None:
        yield {
            "status": "error",
            "message": "Attachments are not available in this deployment.",
        }
        return
    if not session_id:
        yield {"status": "ok", "attachments": []}
        return
    records = await store.list_for_session(session_id)
    yield {
        "status": "ok",
        "attachments": [
            {
                "file_id": r.file_id,
                "file_name": r.file_name,
                "file_size": r.file_size,
            }
            for r in records
        ],
    }


async def read_attachment_fn(
    file_id: str = "",
    offset: int = 1,
    limit: int = 2000,
) -> AsyncIterator[Any]:
    if not file_id:
        yield {
            "status": "error",
            "message": "file_id is required — call list_attachments to see available files",
        }
        return
    store = _store()
    session_id = get_current_session_id()
    if store is None:
        yield {
            "status": "error",
            "message": "Attachments are not available in this deployment.",
        }
        return
    record = await store.get(file_id)
    if record is None:
        yield {
            "status": "error",
            "message": f"Attachment not found: {file_id}. Call list_attachments to see available files.",
        }
        return
    if not session_id or record.session_id != session_id:
        yield {
            "status": "error",
            "message": "This attachment does not belong to the current conversation.",
        }
        return
    data = await store.open(file_id)
    if data is None:
        yield {
            "status": "error",
            "message": f"Attachment data missing: {file_id}",
        }
        return
    try:
        text = extract_attachment_text(
            record.file_name, data, extra=_injected_extractors()
        )
    except UnsupportedFormatError as exc:
        yield {
            "status": "error",
            "message": f"Cannot read '{record.file_name}' as text: {exc}",
            "file_name": record.file_name,
            "file_size": record.file_size,
        }
        return
    if not text:
        yield {
            "status": "error",
            "message": f"'{record.file_name}' contains no extractable text.",
            "file_name": record.file_name,
        }
        return

    lines = text.split("\n")
    start = max(int(offset), 1) - 1
    max_lines = max(int(limit), 1)
    selected = lines[start : start + max_lines]
    truncated = len(lines) > start + max_lines

    # Enforce a per-page character cap so a single huge line cannot blow up
    # the tool result (and thus the context).
    page = "\n".join(selected)
    if len(page) > PAGE_CHAR_CAP:
        page = page[:PAGE_CHAR_CAP]
        truncated = True

    yield {
        "status": "ok",
        "message": (
            f"Read lines {start + 1}-{start + len(selected)} of {len(lines)} "
            f"from '{record.file_name}'"
            + (" (truncated — use offset to read the next page)" if truncated else "")
        ),
        "content": page,
        "file_id": file_id,
        "file_name": record.file_name,
        "start_line": start + 1,
        "end_line": start + len(selected),
        "total_lines": len(lines),
        "truncated": truncated,
    }


ATTACHMENT_TOOL_FNS: dict[str, Any] = {
    "list_attachments": list_attachments_fn,
    "read_attachment": read_attachment_fn,
}


_META_FILE = Path(__file__).with_name("attachment_tools.json")


def _load_tool_metadata() -> list[dict[str, Any]]:
    """Load ``attachment_tools.json`` (import-ready, locale dicts) and project it
    to the gateway's tool-metadata shape: ``display_name``/``description`` hold
    the default (en) text, ``*_locale`` hold JSON-encoded zh/en maps, and each
    entry gets its ``_fn`` binding."""
    with _META_FILE.open(encoding="utf-8") as fh:
        raw = json.load(fh)
    metadata: list[dict[str, Any]] = []
    for item in raw:
        name = item["name"]
        if name not in ATTACHMENT_TOOL_FNS:
            raise RuntimeError(
                f"attachment_tools.json: '{name}' has no implementation "
                "in ATTACHMENT_TOOL_FNS"
            )
        meta: dict[str, Any] = {
            "name": name,
            "display_name": item.get("display_name", name),
            "description": item.get("description", ""),
            "parameters": item.get("parameters", {"type": "object", "properties": {}}),
            "_fn": ATTACHMENT_TOOL_FNS[name],
        }
        for field in ("display_name_locale", "description_locale"):
            locales = item.get(field)
            if isinstance(locales, dict):
                meta[field] = json.dumps(locales, ensure_ascii=False)
        metadata.append(meta)
    return metadata


ATTACHMENT_TOOL_METADATA: list[dict[str, Any]] = _load_tool_metadata()
ATTACHMENT_TOOL_NAMES: tuple[str, ...] = tuple(
    t["name"] for t in ATTACHMENT_TOOL_METADATA
)
