from __future__ import annotations

import statistics
import time
from collections import defaultdict, deque
from typing import Any


class SessionMetrics:
    def __init__(self):
        self.started = time.monotonic()
        self.counters: dict[str, int] = defaultdict(int)
        self.find_times: deque[float] = deque(maxlen=60)
        self.last_find_seconds = 0.0
        self.current_search_started: float | None = None
        self.last_progress = time.monotonic()
        self.latencies = defaultdict(lambda: deque(maxlen=120))

    def timing(self, name, seconds):
        self.latencies[name].append(max(0.0, float(seconds)))

    def inc(self, name: str, amount: int = 1) -> None:
        self.counters[name] += int(amount)

    def progress(self) -> None:
        self.last_progress = time.monotonic()

    def begin_search(self) -> None:
        self.current_search_started = time.monotonic()
        self.inc("searches_started")

    def gem_found(self) -> float:
        now = time.monotonic()
        elapsed = 0.0
        if self.current_search_started is not None:
            elapsed = max(0.0, now - self.current_search_started)
            self.find_times.append(elapsed)
            self.last_find_seconds = elapsed
        self.current_search_started = None
        self.inc("free_gems_found")
        self.progress()
        return elapsed

    def search_timeout(self) -> None:
        self.current_search_started = None
        self.inc("search_timeouts")

    def snapshot(self) -> dict[str, Any]:
        avg = statistics.fmean(self.find_times) if self.find_times else 0.0
        data = {
            **dict(self.counters),
            "avg_find_seconds": round(avg, 1),
            "last_find_seconds": round(self.last_find_seconds, 1),
            "stuck_seconds": round(max(0.0, time.monotonic() - self.last_progress), 1),
            "session_seconds": round(max(0.0, time.monotonic() - self.started), 1),
        }
        checked = max(1, int(data.get("candidate_checks", 0)))
        accepted = int(data.get("confirmed_free_gems", 0))
        prevented = int(data.get("false_positive_prevented", 0))
        data["candidate_success_rate"] = round(accepted / checked * 100.0, 1)
        data["prevention_rate"] = round(
            prevented / max(1, prevented + accepted) * 100.0,
            1,
        )
        dispatch_count = max(1, int(data.get("dispatch_count", 0)))
        data["avg_dispatch_seconds"] = round(
            float(data.get("dispatch_ms_total", 0)) / 1000.0 / dispatch_count,
            2,
        ) if int(data.get("dispatch_count", 0)) else 0.0
        for name, values in self.latencies.items():
            samples = sorted(values)
            if samples:
                data[name + "_p50_seconds"] = round(statistics.median(samples), 3)
                data[name + "_p95_seconds"] = round(samples[min(len(samples)-1, int((len(samples)-1)*0.95))], 3)
        data["dispatch_confirmation_rate"] = round(100 * int(data.get("dispatch_confirmations", 0)) / max(1, int(data.get("dispatch_confirmations", 0)) + int(data.get("dispatch_unconfirmed", 0))), 1)
        return data
