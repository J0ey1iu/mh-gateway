"""OpenAPI route surface baseline.

This test pins the public REST route surface (paths + methods) for the
default ``mh_gateway.main:app`` instance. The refactor must keep the
external HTTP contract identical; the snapshot below will fail if a
route is added, removed, or renamed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

BASELINE_FILE = Path(__file__).parent / "baseline_openapi.json"


def _collect_routes(app) -> set[tuple[str, str]]:
    spec = app.openapi()
    routes: set[tuple[str, str]] = set()
    for path, methods in spec.get("paths", {}).items():
        for method in methods:
            if method.upper() in {"GET", "POST", "PUT", "DELETE", "PATCH"}:
                routes.add((method.upper(), path))
    return routes


@pytest.fixture(scope="module")
def baseline_routes() -> set[tuple[str, str]]:
    data = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
    routes: set[tuple[str, str]] = set()
    for path, methods in data.get("paths", {}).items():
        for method in methods:
            if method.upper() in {"GET", "POST", "PUT", "DELETE", "PATCH"}:
                routes.add((method.upper(), path))
    return routes


def test_baseline_snapshot_exists() -> None:
    assert BASELINE_FILE.is_file(), (
        f"Missing baseline file {BASELINE_FILE}. Run "
        "uv run python scripts/capture_openapi.py to regenerate."
    )


def test_current_routes_match_baseline() -> None:
    from mh_gateway.main import app

    current = _collect_routes(app)
    data = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
    expected: set[tuple[str, str]] = set()
    for path, methods in data.get("paths", {}).items():
        for method in methods:
            if method.upper() in {"GET", "POST", "PUT", "DELETE", "PATCH"}:
                expected.add((method.upper(), path))
    missing = expected - current
    added = current - expected
    assert not missing, f"Routes removed compared to baseline: {sorted(missing)}"
    assert not added, f"Routes added compared to baseline: {sorted(added)}"
