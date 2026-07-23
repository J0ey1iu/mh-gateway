"""Capture the OpenAPI schema for the default ``mh_gateway.main:app``.

Usage::

    uv run python scripts/capture_openapi.py [output_path]

Writes a JSON snapshot used by ``tests/test_openapi_baseline.py``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

DEFAULT_PATH = (
    Path(__file__).resolve().parent.parent / "tests" / "baseline_openapi.json"
)


def main() -> int:
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PATH
    from mh_gateway.main import app

    spec = app.openapi()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(spec, indent=2, default=str), encoding="utf-8")
    print(f"Wrote {output} with {len(spec.get('paths', {}))} paths.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
