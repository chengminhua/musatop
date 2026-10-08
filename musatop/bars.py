"""Terminal-width-independent primitives for load bars and five-minute sparklines."""

from __future__ import annotations

import math
from collections.abc import Iterable

from musatop.history import HistoryPoint


def valid_percent(value: float | None) -> float | None:
    """Reject unknown or impossible percentages instead of drawing them as zero."""
    if value is None or not math.isfinite(value) or not 0 <= value <= 100:
        return None
    return value


def percent_label(value: float | None) -> str:
    value = valid_percent(value)
    return " N/A" if value is None else f"{value:3.0f}%"


def bar_cells(value: float | None, width: int, *, ascii: bool = False) -> str:
    """Return exactly ``width`` cells, with eighth-cell Unicode precision."""
    width = max(0, width)
    value = valid_percent(value)
    if value is None:
        return ("?" if ascii else "░") * width
    if ascii:
        full = int(value * width / 100)
        return "#" * full + " " * (width - full)
    eighths = int(value * width * 8 / 100)
    full, fraction = divmod(eighths, 8)
    partial = "▏▎▍▌▋▊▉"[fraction - 1] if fraction else ""
    return ("█" * full + partial).ljust(width)


def trend_values(
    points: Iterable[HistoryPoint], metric: str, width: int, now: int,
) -> list[float | None]:
    """Project seconds [now-299, now] onto columns, preserving observed peaks.

    A column with no valid observation stays unknown. Larger displays retain
    gaps rather than expanding a one-second sample into invented observations.
    """
    if metric not in ("util_percent", "memory_percent"):
        raise ValueError(f"Unknown history metric: {metric}")
    if width <= 0:
        return []
    values: list[float | None] = [None] * width
    first = now - 299
    for point in points:
        age = point.second - first
        if not 0 <= age < 300:
            continue
        value = valid_percent(getattr(point, metric))
        if value is None:
            continue
        column = min(width - 1, ((age + 1) * width - 1) // 300)
        previous = values[column]
        values[column] = value if previous is None else max(previous, value)
    return values


def spark_cells(values: Iterable[float | None], *, ascii: bool = False) -> str:
    """Fixed 0–100% scale; observed zero has a baseline, unknown is a gap."""
    levels = "._:-=+*#@" if ascii else "▁▂▃▄▅▆▇█"
    result = []
    for value in values:
        value = valid_percent(value)
        if value is None:
            result.append(" ")
        else:
            result.append(levels[min(len(levels) - 1, int(value * len(levels) / 100))])
    return "".join(result)
