"""API-level tests for the attachment upload/download endpoints.

Uses an in-memory AttachmentStore so the endpoint contract (upload,
download, ownership, limits) is exercised without a database.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Generator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mh_gateway.adapters import AttachmentRecord, AttachmentStore, UserIdentity
from mh_gateway.app import GatewayAdapters, create_app
from mh_gateway.config import ConfigSchema

from .conftest import ALL_PERMS, _MockLLM, _MockMetadata, _MockSessionRepo


class _HeaderAuth:
    """Reads the user id from the ``X-User-Id`` header (test double)."""

    def __init__(self) -> None:
        self.get_permissions = AsyncMock(return_value=ALL_PERMS)
        self.check = AsyncMock(side_effect=lambda uid, perm: True)
        self.authenticate = AsyncMock(return_value="default-app")
        self.get_identity_headers = AsyncMock(return_value={})
        self.get_headers = AsyncMock(return_value={})
        self.logout = AsyncMock()
        self.close = AsyncMock()

    async def verify(self, request: Any) -> UserIdentity | None:
        uid = request.headers.get("X-User-Id", "")
        if not uid:
            return None
        return UserIdentity(user_id=uid, username=uid)


class InMemoryAttachmentStore(AttachmentStore):
    def __init__(self) -> None:
        self._records: dict[str, AttachmentRecord] = {}
        self._blobs: dict[str, bytes] = {}

    async def save(self, record: AttachmentRecord, data: bytes) -> AttachmentRecord:
        self._records[record.file_id] = record
        self._blobs[record.file_id] = data
        return record

    async def get(self, file_id: str) -> AttachmentRecord | None:
        return self._records.get(file_id)

    async def open(self, file_id: str) -> bytes | None:
        return self._blobs.get(file_id)

    async def bind(self, file_id: str, session_id: str) -> bool:
        rec = self._records.get(file_id)
        if rec is None:
            return False
        rec.session_id = session_id
        return True

    async def list_for_session(self, session_id: str) -> list[AttachmentRecord]:
        return [r for r in self._records.values() if r.session_id == session_id]

    async def delete(self, file_id: str) -> bool:
        return self._records.pop(file_id, None) is not None

    async def close(self) -> None:
        return None


@pytest.fixture
def store() -> InMemoryAttachmentStore:
    return InMemoryAttachmentStore()


@pytest.fixture
def client(store: InMemoryAttachmentStore) -> Generator[TestClient, None, None]:
    settings = ConfigSchema(
        db_path="./unused.db",
        cors_origins=[],
        metrics_enabled=False,
        enable_eval=False,
    )
    mock_metadata = _MockMetadata()
    mock_provider = _HeaderAuth()

    @asynccontextmanager
    async def adapter_lifespan(app: FastAPI) -> AsyncIterator[GatewayAdapters]:
        yield GatewayAdapters(
            settings=settings,
            user_auth=mock_provider,
            authorization=mock_provider,
            m2m_auth=mock_provider,
            outbound_auth=mock_provider,
            metadata=mock_metadata,
            llm=_MockLLM(),
            sessions=_MockSessionRepo(),  # type: ignore[arg-type]
            eval_results=None,
            attachments=store,
        )

    app = create_app(settings=settings, adapters=adapter_lifespan)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_upload_returns_metadata(client: TestClient) -> None:
    r = client.post(
        "/api/v1/attachments",
        files={"file": ("report.md", b"# hello", "text/markdown")},
        headers={"X-User-Id": "1"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["file_name"] == "report.md"
    assert body["file_size"] == 7
    assert body["backend_type"] == "attachment"
    assert body["file_id"]


def test_upload_unsupported_extension_rejected(client: TestClient) -> None:
    r = client.post(
        "/api/v1/attachments",
        files={"file": ("evil.exe", b"MZ", "application/octet-stream")},
        headers={"X-User-Id": "1"},
    )
    assert r.status_code == 400, r.text
    assert "Unsupported file type" in r.json()["detail"]


def test_upload_too_large_rejected(client: TestClient) -> None:
    big = b"x" * (21 * 1024 * 1024)
    r = client.post(
        "/api/v1/attachments",
        files={"file": ("big.txt", big, "text/plain")},
        headers={"X-User-Id": "1"},
    )
    assert r.status_code == 413, r.text
    assert "too large" in r.json()["detail"]


def test_download_roundtrip(client: TestClient) -> None:
    up = client.post(
        "/api/v1/attachments",
        files={"file": ("你好.md", "内容".encode("utf-8"), "text/markdown")},
        headers={"X-User-Id": "1"},
    )
    file_id = up.json()["file_id"]
    r = client.get(f"/api/v1/attachments/{file_id}", headers={"X-User-Id": "1"})
    assert r.status_code == 200, r.text
    assert r.content == "内容".encode("utf-8")
    assert "filename*=UTF-8''" in r.headers["content-disposition"]


def test_download_other_user_forbidden(client: TestClient) -> None:
    up = client.post(
        "/api/v1/attachments",
        files={"file": ("a.md", b"x", "text/markdown")},
        headers={"X-User-Id": "1"},
    )
    file_id = up.json()["file_id"]
    r = client.get(f"/api/v1/attachments/{file_id}", headers={"X-User-Id": "2"})
    assert r.status_code == 403, r.text


def test_download_unknown_404(client: TestClient) -> None:
    r = client.get("/api/v1/attachments/nope", headers={"X-User-Id": "1"})
    assert r.status_code == 404, r.text


def test_delete_removes_attachment(client: TestClient) -> None:
    up = client.post(
        "/api/v1/attachments",
        files={"file": ("a.md", b"x", "text/markdown")},
        headers={"X-User-Id": "1"},
    )
    file_id = up.json()["file_id"]

    r = client.delete(f"/api/v1/attachments/{file_id}", headers={"X-User-Id": "1"})
    assert r.status_code == 204, r.text

    # 删除后下载应 404
    r = client.get(f"/api/v1/attachments/{file_id}", headers={"X-User-Id": "1"})
    assert r.status_code == 404, r.text


def test_delete_other_user_forbidden(client: TestClient) -> None:
    up = client.post(
        "/api/v1/attachments",
        files={"file": ("a.md", b"x", "text/markdown")},
        headers={"X-User-Id": "1"},
    )
    file_id = up.json()["file_id"]

    r = client.delete(f"/api/v1/attachments/{file_id}", headers={"X-User-Id": "2"})
    assert r.status_code == 403, r.text

    # 非 owner 删除失败后文件仍可下载
    r = client.get(f"/api/v1/attachments/{file_id}", headers={"X-User-Id": "1"})
    assert r.status_code == 200, r.text


def test_delete_unknown_404(client: TestClient) -> None:
    r = client.delete("/api/v1/attachments/nope", headers={"X-User-Id": "1"})
    assert r.status_code == 404, r.text
