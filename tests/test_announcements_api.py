"""API-level tests for the announcement endpoints.

Uses an in-memory AnnouncementStore so the endpoint contract (admin CRUD,
permission gate, re-push, stats, user visible/read/consent) is exercised
without a database.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Generator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mh_gateway.adapters import (
    AnnouncementRecord,
    AnnouncementStats,
    AnnouncementStore,
    UserIdentity,
)
from mh_gateway.app import GatewayAdapters, create_app
from mh_gateway.config import ConfigSchema

from .conftest import ALL_PERMS, _MockLLM, _MockMetadata, _MockSessionRepo


class _HeaderAuth:
    """Reads user id from ``X-User-Id``; permission check is injectable."""

    def __init__(self, admin: bool = True) -> None:
        self.admin = admin
        self.get_permissions = AsyncMock(return_value=ALL_PERMS)
        self.check = AsyncMock(side_effect=lambda uid, perm: self.admin)
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


class InMemoryAnnouncementStore(AnnouncementStore):
    def __init__(self) -> None:
        self._records: dict[str, AnnouncementRecord] = {}
        self._reads: dict[str, set[str]] = {}
        self._consents: dict[str, dict[str, str]] = {}

    async def list_announcements(
        self, page: int = 1, page_size: int = 20
    ) -> tuple[list[AnnouncementRecord], int]:
        records = sorted(
            self._records.values(),
            key=lambda r: (r.pushed_at, r.created_at),
            reverse=True,
        )
        total = len(records)
        if page_size <= 0:
            return records, total
        start = (page - 1) * page_size
        return records[start : start + page_size], total

    async def get_announcement(self, announcement_id: str) -> AnnouncementRecord | None:
        return self._records.get(announcement_id)

    async def create_announcement(
        self, announcement: AnnouncementRecord
    ) -> AnnouncementRecord:
        announcement.created_at = announcement.created_at or "t0"
        announcement.pushed_at = announcement.pushed_at or "t0"
        self._records[announcement.announcement_id] = announcement
        return announcement

    async def update_announcement(
        self, announcement: AnnouncementRecord
    ) -> AnnouncementRecord:
        self._records[announcement.announcement_id] = announcement
        return announcement

    async def delete_announcement(self, announcement_id: str) -> bool:
        if announcement_id not in self._records:
            return False
        del self._records[announcement_id]
        self._reads.pop(announcement_id, None)
        self._consents.pop(announcement_id, None)
        return True

    async def repush_announcement(self, announcement_id: str, pushed_by: str) -> bool:
        rec = self._records.get(announcement_id)
        if rec is None:
            return False
        rec.pushed_at = "t9"
        rec.pushed_by = pushed_by
        self._reads.pop(announcement_id, None)
        self._consents.pop(announcement_id, None)
        return True

    async def announcement_stats(self, announcement_id: str) -> AnnouncementStats:
        return AnnouncementStats(
            read_count=len(self._reads.get(announcement_id, set())),
            agree_count=sum(
                1
                for d in self._consents.get(announcement_id, {}).values()
                if d == "agree"
            ),
            decline_count=sum(
                1
                for d in self._consents.get(announcement_id, {}).values()
                if d == "decline"
            ),
            total_users=3,
        )

    async def visible_announcements(self, user_id: str) -> list[AnnouncementRecord]:
        return [
            r
            for r in self._records.values()
            if r.active
            and user_id not in self._reads.get(r.announcement_id, set())
            and self._consents.get(r.announcement_id, {}).get(user_id) != "agree"
        ]

    async def mark_read(self, announcement_id: str, user_id: str) -> bool:
        if announcement_id not in self._records:
            return False
        self._reads.setdefault(announcement_id, set()).add(user_id)
        return True

    async def record_consent(
        self, announcement_id: str, user_id: str, decision: str
    ) -> bool:
        if announcement_id not in self._records:
            return False
        self._consents.setdefault(announcement_id, {})[user_id] = decision
        return True

    async def close(self) -> None:
        return None


@pytest.fixture
def store() -> InMemoryAnnouncementStore:
    return InMemoryAnnouncementStore()


def _make_app(store: AnnouncementStore, auth: _HeaderAuth) -> FastAPI:
    settings = ConfigSchema(
        db_path="./unused.db",
        cors_origins=[],
        metrics_enabled=False,
        enable_eval=False,
    )

    @asynccontextmanager
    async def adapter_lifespan(
        app: FastAPI,
    ) -> AsyncIterator[GatewayAdapters]:
        yield GatewayAdapters(
            settings=settings,
            user_auth=auth,
            authorization=auth,
            m2m_auth=auth,
            outbound_auth=auth,
            metadata=_MockMetadata(),
            llm=_MockLLM(),
            sessions=_MockSessionRepo(),  # type: ignore[arg-type]
            eval_results=None,
            announcements=store,
        )

    return create_app(settings=settings, adapters=adapter_lifespan)


@pytest.fixture
def client(store: InMemoryAnnouncementStore) -> Generator[TestClient, None, None]:
    with TestClient(
        _make_app(store, _HeaderAuth(admin=True)),
        raise_server_exceptions=False,
    ) as c:
        yield c


@pytest.fixture
def non_admin_client(
    store: InMemoryAnnouncementStore,
) -> Generator[TestClient, None, None]:
    with TestClient(
        _make_app(store, _HeaderAuth(admin=False)),
        raise_server_exceptions=False,
    ) as c:
        yield c


def _create(client: TestClient, title: str = "Hello", **kw: Any) -> dict[str, Any]:
    body = {"title": title, "body": "## Content", **kw}
    r = client.post(
        "/api/v1/announcements",
        json=body,
        headers={"X-User-Id": "1"},
    )
    assert r.status_code == 200, r.text
    return r.json()


# ── Admin surface ──────────────────────────────────────────────────────────────


def test_create_list_update_delete(client: TestClient) -> None:
    created = _create(client, title="v2 release notes")
    aid = created["announcement_id"]
    assert created["pushed_by"] == "1"
    assert created["active"] is True

    listed = client.get("/api/v1/announcements", headers={"X-User-Id": "1"}).json()
    assert listed["total"] == 1
    assert [a["announcement_id"] for a in listed["items"]] == [aid]

    updated = client.patch(
        f"/api/v1/announcements/{aid}",
        json={"title": "renamed", "body": "new body", "consent_required": True},
        headers={"X-User-Id": "1"},
    ).json()
    assert updated["title"] == "renamed"
    assert updated["consent_required"] is True

    r = client.delete(f"/api/v1/announcements/{aid}", headers={"X-User-Id": "1"})
    assert r.status_code == 200
    assert (
        client.get("/api/v1/announcements", headers={"X-User-Id": "1"}).json()["total"]
        == 0
    )


def test_list_paginated(client: TestClient) -> None:
    """管理端列表分页：page/page_size 生效，total 反映总数。"""
    for i in range(5):
        _create(client, title=f"ann-{i}")

    r = client.get(
        "/api/v1/announcements?page=1&page_size=3",
        headers={"X-User-Id": "1"},
    )
    body = r.json()
    assert len(body["items"]) == 3
    assert body["total"] == 5
    assert body["page"] == 1
    assert body["page_size"] == 3

    r = client.get(
        "/api/v1/announcements?page=2&page_size=3",
        headers={"X-User-Id": "1"},
    )
    assert len(r.json()["items"]) == 2
    assert r.json()["total"] == 5

    r = client.get(
        "/api/v1/announcements?page=99&page_size=3",
        headers={"X-User-Id": "1"},
    )
    assert r.json()["items"] == []

    r = client.get(
        "/api/v1/announcements?page_size=0",
        headers={"X-User-Id": "1"},
    )
    assert len(r.json()["items"]) == 5


def test_admin_endpoints_require_permission(non_admin_client: TestClient) -> None:
    r = non_admin_client.get("/api/v1/announcements", headers={"X-User-Id": "1"})
    assert r.status_code == 403
    r = non_admin_client.post(
        "/api/v1/announcements",
        json={"title": "x", "body": "y"},
        headers={"X-User-Id": "1"},
    )
    assert r.status_code == 403


def test_validation_title_and_body_required(client: TestClient) -> None:
    r = client.post(
        "/api/v1/announcements",
        json={"title": "", "body": ""},
        headers={"X-User-Id": "1"},
    )
    assert r.status_code == 422


def test_update_delete_missing_returns_404(client: TestClient) -> None:
    r = client.patch(
        "/api/v1/announcements/nope",
        json={"title": "x", "body": "y"},
        headers={"X-User-Id": "1"},
    )
    assert r.status_code == 404
    assert (
        client.delete(
            "/api/v1/announcements/nope", headers={"X-User-Id": "1"}
        ).status_code
        == 404
    )


# ── User surface ───────────────────────────────────────────────────────────────


def test_visible_read_and_read_counts(client: TestClient) -> None:
    aid = _create(client)["announcement_id"]

    visible = client.get(
        "/api/v1/announcements/visible", headers={"X-User-Id": "u1"}
    ).json()
    assert [a["announcement_id"] for a in visible] == [aid]

    r = client.post(f"/api/v1/announcements/{aid}/read", headers={"X-User-Id": "u1"})
    assert r.status_code == 200

    assert (
        client.get("/api/v1/announcements/visible", headers={"X-User-Id": "u1"}).json()
        == []
    )
    # 其他用户仍可见
    assert (
        client.get("/api/v1/announcements/visible", headers={"X-User-Id": "u2"}).json()
        != []
    )

    stats = client.get(
        f"/api/v1/announcements/{aid}/stats", headers={"X-User-Id": "1"}
    ).json()
    assert stats["read_count"] == 1
    assert stats["total_users"] == 3


def test_consent_flow(client: TestClient) -> None:
    aid = _create(client, consent_required=True)["announcement_id"]

    r = client.post(
        f"/api/v1/announcements/{aid}/consent",
        json={"decision": "agree"},
        headers={"X-User-Id": "u1"},
    )
    assert r.status_code == 200

    # 决策后不再可见
    assert (
        client.get("/api/v1/announcements/visible", headers={"X-User-Id": "u1"}).json()
        == []
    )

    stats = client.get(
        f"/api/v1/announcements/{aid}/stats", headers={"X-User-Id": "1"}
    ).json()
    assert stats["agree_count"] == 1
    assert stats["decline_count"] == 0

    # 非法决策
    r = client.post(
        f"/api/v1/announcements/{aid}/consent",
        json={"decision": "maybe"},
        headers={"X-User-Id": "u2"},
    )
    assert r.status_code == 422


def test_declined_consent_stays_visible(client: TestClient) -> None:
    """拒绝 consent 公告后：公告对用户仍然可见（下次进入再次选择），
    同意后才从 visible 消失。"""
    aid = _create(client, consent_required=True)["announcement_id"]

    # 拒绝 → visible 仍在（拒绝不是终态）
    r = client.post(
        f"/api/v1/announcements/{aid}/consent",
        json={"decision": "decline"},
        headers={"X-User-Id": "u1"},
    )
    assert r.status_code == 200
    visible = client.get(
        "/api/v1/announcements/visible", headers={"X-User-Id": "u1"}
    ).json()
    assert [a["announcement_id"] for a in visible] == [aid]

    # 同意 → visible 消失
    client.post(
        f"/api/v1/announcements/{aid}/consent",
        json={"decision": "agree"},
        headers={"X-User-Id": "u1"},
    )
    assert (
        client.get("/api/v1/announcements/visible", headers={"X-User-Id": "u1"}).json()
        == []
    )

    # 统计保留 decline 记录（admin 可见拒绝人数）
    client.post(f"/api/v1/announcements/{aid}/repush", headers={"X-User-Id": "1"})
    client.post(
        f"/api/v1/announcements/{aid}/consent",
        json={"decision": "decline"},
        headers={"X-User-Id": "u2"},
    )
    stats = client.get(
        f"/api/v1/announcements/{aid}/stats", headers={"X-User-Id": "1"}
    ).json()
    assert stats["decline_count"] == 1


def test_repush_clears_read_state(client: TestClient) -> None:
    aid = _create(client)["announcement_id"]
    client.post(f"/api/v1/announcements/{aid}/read", headers={"X-User-Id": "u1"})
    assert (
        client.get("/api/v1/announcements/visible", headers={"X-User-Id": "u1"}).json()
        == []
    )

    r = client.post(f"/api/v1/announcements/{aid}/repush", headers={"X-User-Id": "1"})
    assert r.status_code == 200

    visible = client.get(
        "/api/v1/announcements/visible", headers={"X-User-Id": "u1"}
    ).json()
    assert [a["announcement_id"] for a in visible] == [aid]
    # repush 后 stats 归零
    stats = client.get(
        f"/api/v1/announcements/{aid}/stats", headers={"X-User-Id": "1"}
    ).json()
    assert stats["read_count"] == 0


def test_admin_sees_own_announcement(client: TestClient) -> None:
    """管理员也是用户：自己推的公告自己先看到，已读后消失。"""
    aid = _create(client)["announcement_id"]

    visible = client.get(
        "/api/v1/announcements/visible", headers={"X-User-Id": "1"}
    ).json()
    assert [a["announcement_id"] for a in visible] == [aid]

    client.post(f"/api/v1/announcements/{aid}/read", headers={"X-User-Id": "1"})
    assert (
        client.get("/api/v1/announcements/visible", headers={"X-User-Id": "1"}).json()
        == []
    )


def test_inactive_announcement_not_visible(client: TestClient) -> None:
    aid = _create(client, active=False)["announcement_id"]
    visible = client.get(
        "/api/v1/announcements/visible", headers={"X-User-Id": "u1"}
    ).json()
    assert all(a["announcement_id"] != aid for a in visible)


def test_store_not_configured_returns_503() -> None:
    """部署未启用公告存储时返回 503（非 500）。"""
    settings = ConfigSchema(
        db_path="./unused.db",
        cors_origins=[],
        metrics_enabled=False,
        enable_eval=False,
    )

    @asynccontextmanager
    async def adapter_lifespan(
        app: FastAPI,
    ) -> AsyncIterator[GatewayAdapters]:
        yield GatewayAdapters(
            settings=settings,
            user_auth=_HeaderAuth(),
            authorization=_HeaderAuth(),
            m2m_auth=_HeaderAuth(),
            outbound_auth=_HeaderAuth(),
            metadata=_MockMetadata(),
            llm=_MockLLM(),
            sessions=_MockSessionRepo(),  # type: ignore[arg-type]
            eval_results=None,
        )

    app = create_app(settings=settings, adapters=adapter_lifespan)
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/api/v1/announcements/visible", headers={"X-User-Id": "u1"})
        assert r.status_code == 503
