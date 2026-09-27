from __future__ import annotations

import math
import random
from dataclasses import dataclass


@dataclass
class SearchPoint:
    x: float = 0.0
    y: float = 0.0


def radius_units(web_radius: int | float) -> float:
    """
    Map the web radius to approximate viewport-distance units.

    100 ~= one screen radius from the search origin.
    200 ~= two screens.
    1000 ~= ten screens.

    This is deliberately an approximate camera/search radius because RoK does
    not expose a stable meter-per-pixel mapping to this visual bot.
    """
    try:
        value = float(web_radius)
    except Exception:
        value = 100.0

    value = max(1.0, min(1000.0, value))
    return max(0.15, value / 100.0)


def choose_bounded_step(
    point: SearchPoint,
    web_radius: int | float,
    *,
    rng: random.Random | None = None,
) -> tuple[SearchPoint, float, float]:
    """
    Choose one random viewport movement while staying inside the configured
    circular search radius.

    Returns:
      new_point, dx, dy

    dx/dy are virtual camera movements in viewport units.
    """
    rng = rng or random
    radius = radius_units(web_radius)

    # Smaller radii use smaller steps; large radii can move farther per swipe.
    max_step = min(0.72, max(0.16, radius * 0.45))
    min_step = min(max_step, max(0.08, max_step * 0.38))

    # Try genuinely random directions first.
    for _ in range(18):
        angle = rng.uniform(0.0, math.tau)
        step = rng.uniform(min_step, max_step)

        nx = point.x + math.cos(angle) * step
        ny = point.y + math.sin(angle) * step

        if math.hypot(nx, ny) <= radius:
            return SearchPoint(nx, ny), nx - point.x, ny - point.y

    # Near the boundary: deliberately bias back toward the origin, but add
    # enough angular jitter that the path does not become deterministic.
    distance = math.hypot(point.x, point.y)
    if distance <= 1e-6:
        angle = rng.uniform(0.0, math.tau)
    else:
        toward_origin = math.atan2(-point.y, -point.x)
        angle = toward_origin + rng.uniform(-0.70, 0.70)

    step = min(max_step, max(min_step, distance * rng.uniform(0.35, 0.75)))

    nx = point.x + math.cos(angle) * step
    ny = point.y + math.sin(angle) * step

    # Numerical safety: project back inside the circle if required.
    d = math.hypot(nx, ny)
    if d > radius and d > 0:
        scale = (radius * 0.97) / d
        nx *= scale
        ny *= scale

    return SearchPoint(nx, ny), nx - point.x, ny - point.y


def swipe_from_delta(
    dx: float,
    dy: float,
    *,
    jitter_x: float = 0.0,
    jitter_y: float = 0.0,
) -> tuple[float, float, float, float]:
    """
    Convert virtual camera movement to a normalized finger swipe.

    Map motion is opposite finger motion, so the swipe vector is inverted.
    """
    start_x = max(0.27, min(0.73, 0.50 + jitter_x))
    start_y = max(0.27, min(0.73, 0.50 + jitter_y))

    # One virtual viewport unit maps to roughly 55% of the screen.
    end_x = start_x - dx * 0.55
    end_y = start_y - dy * 0.55

    end_x = max(0.14, min(0.86, end_x))
    end_y = max(0.14, min(0.86, end_y))

    return start_x, start_y, end_x, end_y
