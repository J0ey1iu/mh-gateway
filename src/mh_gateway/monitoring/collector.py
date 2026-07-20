from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger("orchestration.metrics")


def _labels_key(labels: dict[str, str]) -> str:
    return json.dumps(labels, sort_keys=True, ensure_ascii=False)


_COLLECTOR: MetricsCollector | None = None
_COLLECTOR_LOCK = threading.Lock()


def get_collector() -> MetricsCollector | None:
    return _COLLECTOR


def set_collector(collector: MetricsCollector | None) -> None:
    global _COLLECTOR
    with _COLLECTOR_LOCK:
        _COLLECTOR = collector


class _Counter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value = 0

    def inc(self, delta: int = 1) -> None:
        with self._lock:
            self._value += delta

    def get(self) -> int:
        with self._lock:
            return self._value


class _LabeledCounter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, _Counter] = {}

    def inc(self, labels: dict[str, str], delta: int = 1) -> None:
        key = _labels_key(labels)
        with self._lock:
            if key not in self._counters:
                self._counters[key] = _Counter()
            self._counters[key].inc(delta)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"labels": json.loads(k), "value": c.get()}
                for k, c in self._counters.items()
            ]


class _LabeledHistogram:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._samples: dict[str, list[float]] = {}

    def observe(self, labels: dict[str, str], value: float) -> None:
        key = _labels_key(labels)
        with self._lock:
            self._samples.setdefault(key, []).append(value)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            result = []
            for k, values in self._samples.items():
                if not values:
                    continue
                sorted_vals = sorted(values)
                n = len(sorted_vals)
                total = sum(sorted_vals)
                result.append(
                    {
                        "labels": json.loads(k),
                        "count": n,
                        "sum": total,
                        "avg": total / n,
                        "p50": sorted_vals[int(n * 0.5)] if n > 0 else 0,
                        "p90": sorted_vals[min(int(n * 0.9), n - 1)] if n > 0 else 0,
                        "p99": sorted_vals[min(int(n * 0.99), n - 1)] if n > 0 else 0,
                        "min": sorted_vals[0] if n > 0 else 0,
                        "max": sorted_vals[-1] if n > 0 else 0,
                    }
                )
            self._samples.clear()
            return result


class MetricsCollector:
    def __init__(self, instance_id: str = "") -> None:
        self._instance_id = instance_id or os.environ.get(
            "HOSTNAME", os.environ.get("POD_NAME", "unknown")
        )
        self._start_time = time.time()
        self._push_task: asyncio.Task[Any] | None = None
        self._push_interval: int = 60

        self.http_requests_total = _LabeledCounter()
        self.http_request_duration_ms = _LabeledHistogram()
        self.llm_requests_total = _LabeledCounter()
        self.llm_tokens_total = _LabeledCounter()
        self.llm_request_duration_ms = _LabeledHistogram()
        self.agent_runs_total = _LabeledCounter()
        self.tool_calls_total = _LabeledCounter()

    @property
    def instance_id(self) -> str:
        return self._instance_id

    def start_push(self, interval: int = 60) -> None:
        self._push_interval = interval
        loop = asyncio.get_running_loop()
        self._push_task = loop.create_task(self._push_loop())

    async def stop_push(self) -> None:
        if self._push_task:
            self._push_task.cancel()
            try:
                await self._push_task
            except asyncio.CancelledError:
                pass
            self._push_task = None

    async def _push_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._push_interval)
                snapshot = self.snapshot()
                if snapshot:
                    logger.info(json.dumps(snapshot, ensure_ascii=False, default=str))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Metrics push failed, retrying on next interval")

    def snapshot(self) -> dict[str, Any]:
        return {
            "instance_id": self._instance_id,
            "uptime_seconds": round(time.time() - self._start_time, 2),
            "http_requests_total": self.http_requests_total.snapshot(),
            "http_request_duration_ms": self.http_request_duration_ms.snapshot(),
            "llm_requests_total": self.llm_requests_total.snapshot(),
            "llm_tokens_total": self.llm_tokens_total.snapshot(),
            "llm_request_duration_ms": self.llm_request_duration_ms.snapshot(),
            "agent_runs_total": self.agent_runs_total.snapshot(),
            "tool_calls_total": self.tool_calls_total.snapshot(),
        }

    def live_snapshot(self) -> dict[str, Any]:
        return {
            "instance_id": self._instance_id,
            "uptime_seconds": round(time.time() - self._start_time, 2),
            "http_requests_total": self.http_requests_total.snapshot(),
            "http_request_duration_ms": self.http_request_duration_ms.snapshot(),
            "llm_requests_total": self.llm_requests_total.snapshot(),
            "llm_tokens_total": self.llm_tokens_total.snapshot(),
            "llm_request_duration_ms": self.llm_request_duration_ms.snapshot(),
            "agent_runs_total": self.agent_runs_total.snapshot(),
            "tool_calls_total": self.tool_calls_total.snapshot(),
        }
