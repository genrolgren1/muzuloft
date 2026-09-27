from __future__ import annotations

import io
import math
import random
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

from PIL import Image

from random_map_search import SearchPoint, radius_units


PROFILES = {
    "smart": {
        "samples": 48,
        "min_step": 0.18,
        "max_step": 0.70,
        "settle_min": 0.18,
        "settle_max": 0.36,
        "frontier_weight": 4.2,
        "distance_weight": 1.25,
        "occupied_penalty": 2.8,
    },
    "fast": {
        "samples": 34,
        "min_step": 0.28,
        "max_step": 0.82,
        "settle_min": 0.12,
        "settle_max": 0.25,
        "frontier_weight": 3.5,
        "distance_weight": 1.55,
        "occupied_penalty": 2.2,
    },
    "wide": {
        "samples": 58,
        "min_step": 0.35,
        "max_step": 0.95,
        "settle_min": 0.20,
        "settle_max": 0.40,
        "frontier_weight": 4.8,
        "distance_weight": 2.0,
        "occupied_penalty": 2.4,
    },
    "eco": {
        "samples": 24,
        "min_step": 0.18,
        "max_step": 0.58,
        "settle_min": 0.34,
        "settle_max": 0.62,
        "frontier_weight": 4.0,
        "distance_weight": 1.0,
        "occupied_penalty": 3.0,
    },
}


@dataclass
class SearchDecision:
    point: SearchPoint
    dx: float
    dy: float
    effective_radius: float
    score: float
    reason: str


class SearchBrain:
    """Coverage-aware visual map exploration for gem searching."""

    def __init__(self, mode: str = "smart", seed: int | None = None):
        self.rng = random.Random(seed)
        self.mode = mode if mode in PROFILES else "smart"
        self.visits: dict[tuple[int, int], int] = defaultdict(int)
        self.occupied_heat: dict[tuple[int, int], float] = defaultdict(float)
        self.free_heat: dict[tuple[int, int], int] = defaultdict(int)
        self.recent_hashes: deque[int] = deque(maxlen=12)
        self.duplicate_streak = 0
        self.unique_views = 0
        self.total_views = 0
        self.search_number = 0
        self._last_heading: float | None = None

    @property
    def profile(self) -> dict[str, float]:
        return PROFILES[self.mode]

    def set_mode(self, mode: str) -> None:
        if mode in PROFILES:
            self.mode = mode

    def reset_search(self) -> None:
        self.search_number += 1
        self.recent_hashes.clear()
        self.duplicate_streak = 0
        self._last_heading = None

    @staticmethod
    def _cell(x: float, y: float, size: float = 0.34) -> tuple[int, int]:
        return (round(x / size), round(y / size))

    def record_position(self, point: SearchPoint) -> None:
        self.visits[self._cell(point.x, point.y)] += 1

    def record_occupied(self, x: float, y: float, weight: float = 1.0) -> None:
        cell = self._cell(x, y)
        self.occupied_heat[cell] = min(12.0, self.occupied_heat[cell] + max(0.1, weight))

    def record_free(self, x: float, y: float) -> None:
        self.free_heat[self._cell(x, y)] += 1

    def _nearby_heat(self, table: dict[tuple[int, int], Any], x: float, y: float) -> float:
        cx, cy = self._cell(x, y)
        total = 0.0
        for ox in (-1, 0, 1):
            for oy in (-1, 0, 1):
                value = table.get((cx + ox, cy + oy), 0)
                total += float(value) / (1.0 + abs(ox) + abs(oy))
        return total

    @staticmethod
    def dhash(png: bytes, hash_size: int = 8) -> int:
        with Image.open(io.BytesIO(png)) as im:
            gray = im.convert("L").resize((hash_size + 1, hash_size), Image.Resampling.BILINEAR)
            pixels = list(gray.getdata())
        value = 0
        bit = 0
        width = hash_size + 1
        for y in range(hash_size):
            row = y * width
            for x in range(hash_size):
                if pixels[row + x] > pixels[row + x + 1]:
                    value |= 1 << bit
                bit += 1
        return value

    @staticmethod
    def hamming(a: int, b: int) -> int:
        return (a ^ b).bit_count()

    def observe_view(self, png: bytes) -> tuple[bool, int]:
        self.total_views += 1
        h = self.dhash(png)
        duplicate = any(self.hamming(h, old) <= 4 for old in self.recent_hashes)
        if duplicate:
            self.duplicate_streak += 1
        else:
            self.duplicate_streak = 0
            self.unique_views += 1
        self.recent_hashes.append(h)
        return duplicate, self.duplicate_streak

    def effective_radius(self, configured_radius: int | float, elapsed: float, timeout: float) -> float:
        maximum = radius_units(configured_radius)
        if maximum <= 0.25:
            return maximum
        # Search close first, then expand smoothly toward the web-configured max.
        progress = max(0.0, min(1.0, elapsed / max(1.0, timeout)))
        start = min(maximum, max(0.45, maximum * 0.30))
        curved = progress ** 0.72
        return min(maximum, start + (maximum - start) * curved)

    def coverage_percent(self, configured_radius: int | float) -> float:
        radius = radius_units(configured_radius)
        cell_size = 0.34
        approx_cells = max(1.0, math.pi * (radius / cell_size) ** 2)
        inside = 0
        for cx, cy in self.visits:
            x, y = cx * cell_size, cy * cell_size
            if math.hypot(x, y) <= radius + cell_size:
                inside += 1
        return max(0.0, min(100.0, inside / approx_cells * 100.0))

    def choose_step(
        self,
        point: SearchPoint,
        configured_radius: int | float,
        *,
        elapsed: float,
        timeout: float,
        force_escape: bool = False,
    ) -> SearchDecision:
        profile = self.profile
        radius = self.effective_radius(configured_radius, elapsed, timeout)
        samples = int(profile["samples"])
        best: tuple[float, SearchPoint, float, float, str] | None = None

        if force_escape:
            samples = max(samples, 70)

        for _ in range(samples):
            if force_escape:
                step = self.rng.uniform(max(0.42, profile["min_step"]), max(0.72, profile["max_step"]))
            else:
                step = self.rng.uniform(profile["min_step"], profile["max_step"])

            angle = self.rng.uniform(0.0, math.tau)
            nx = point.x + math.cos(angle) * step
            ny = point.y + math.sin(angle) * step
            dist = math.hypot(nx, ny)
            if dist > radius:
                continue

            cell = self._cell(nx, ny)
            visits = self.visits.get(cell, 0)
            occupied = self._nearby_heat(self.occupied_heat, nx, ny)
            free = self._nearby_heat(self.free_heat, nx, ny)

            novelty = 1.0 / (1.0 + visits)
            frontier = novelty * profile["frontier_weight"]
            radial = (dist / max(0.1, radius)) * profile["distance_weight"]
            occupied_cost = occupied * profile["occupied_penalty"] * 0.12
            free_bonus = min(1.3, free * 0.18)

            heading_bonus = 0.0
            if self._last_heading is not None:
                delta = abs(math.atan2(math.sin(angle - self._last_heading), math.cos(angle - self._last_heading)))
                # Avoid tiny back-and-forth oscillations without forbidding direction changes.
                heading_bonus = min(0.35, delta / math.pi * 0.35)

            score = frontier + radial + free_bonus + heading_bonus - occupied_cost + self.rng.uniform(0.0, 0.30)
            if force_escape:
                score += dist * 1.8 + novelty * 2.0

            if best is None or score > best[0]:
                best = (score, SearchPoint(nx, ny), nx - point.x, ny - point.y, "escape" if force_escape else "frontier")
                self._last_heading = angle

        if best is None:
            # Boundary fallback: move toward the origin with randomized angle.
            angle = math.atan2(-point.y, -point.x) + self.rng.uniform(-0.55, 0.55)
            step = min(profile["max_step"], max(profile["min_step"], math.hypot(point.x, point.y) * 0.55))
            nx = point.x + math.cos(angle) * step
            ny = point.y + math.sin(angle) * step
            d = math.hypot(nx, ny)
            if d > radius and d > 0:
                scale = radius * 0.96 / d
                nx *= scale
                ny *= scale
            best = (0.0, SearchPoint(nx, ny), nx - point.x, ny - point.y, "boundary-return")

        score, new_point, dx, dy, reason = best
        return SearchDecision(new_point, dx, dy, radius, score, reason)

    def settle_delay(self) -> float:
        p = self.profile
        return self.rng.uniform(p["settle_min"], p["settle_max"])

    def snapshot(self, configured_radius: int | float) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "coverage_percent": round(self.coverage_percent(configured_radius), 1),
            "unique_views": self.unique_views,
            "total_views": self.total_views,
            "duplicate_streak": self.duplicate_streak,
            "visited_cells": len(self.visits),
            "occupied_cells": len(self.occupied_heat),
            "free_cells": len(self.free_heat),
        }
