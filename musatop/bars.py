"""Pure geometry for device load bars and five-minute terminal area graphs."""

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
    """Project [now-299, now] onto fixed, monotonic-time-anchored peak buckets.

    ``width`` counts horizontal samples, i.e. twice the character width for a
    Braille graph. With at least two samples, bucket boundaries are multiples
    of 300 / (width - 1) seconds from the monotonic clock's origin. Integer
    arithmetic avoids rounding at these rational boundaries. Moving ``now``
    changes a single shared column offset, never the grouping of old points.

    Only the open bucket may acquire new samples; the oldest partial bucket
    may lose expired samples. Reprojection after a width change is deliberate.
    Empty buckets remain unknown, even at widths exceeding 300 samples.
    A one-sample graph necessarily uses the peak of the entire visible window.
    """
    if metric not in ("util_percent", "memory_percent"):
        raise ValueError(f"Unknown history metric: {metric}")
    if width <= 0:
        return []
    values: list[float | None] = [None] * width
    first = now - 299
    scale = width - 1
    current_bucket = now * scale // 300
    for point in points:
        if not first <= point.second <= now:
            continue
        value = valid_percent(getattr(point, metric))
        if value is None:
            continue
        bucket = point.second * scale // 300
        column = width - 1 + bucket - current_bucket
        previous = values[column]
        values[column] = value if previous is None else max(previous, value)
    return values


def area_rows(
    values: Iterable[float | None], height: int, *,
    upside_down: bool = False, ascii: bool = False,
) -> list[str]:
    """Draw a fixed 0–100% area graph, with its baseline toward the time axis.

    Braille characters contain two horizontal samples and four vertical dots.
    ASCII uses one sample per character and ``:`` / ``#`` for partial / full
    cells. Missing values are blank, while observed zero has a one-dot baseline.
    ``upside_down`` mirrors the vertical geometry so paired graphs can share
    an axis. An odd Braille sample count is padded with an unknown right half.
    """
    if height <= 0:
        return []
    samples = [valid_percent(value) for value in values]
    vertical_steps = height * (2 if ascii else 4)
    fills = [
        0 if value is None else max(1, int(value * vertical_steps / 100))
        for value in samples
    ]
    if ascii:
        rows = []
        for row in range(height):
            baseline_row = row if upside_down else height - row - 1
            counts = [max(0, min(2, fill - baseline_row * 2)) for fill in fills]
            rows.append("".join(" :#"[count] for count in counts))
        return rows

    # Braille's dot numbering is column-major with the fourth row at bits 6/7.
    dots = ((1, 8), (2, 16), (4, 32), (64, 128))
    if len(fills) % 2:
        fills.append(0)
    rows = []
    for row in range(height):
        cells = []
        for column in range(0, len(fills), 2):
            bits = 0
            for dy, pair in enumerate(dots):
                y = row * 4 + dy
                distance = y if upside_down else vertical_steps - y - 1
                for dx, bit in enumerate(pair):
                    if distance < fills[column + dx]:
                        bits |= bit
            cells.append(chr(0x2800 + bits) if bits else " ")
        rows.append("".join(cells))
    return rows


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
