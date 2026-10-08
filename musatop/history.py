"""Bounded in-memory device history on the sampler's monotonic clock."""

from collections import Counter, deque
from dataclasses import dataclass
import math

from .models import Device, Snapshot


WINDOW_SECONDS = 300
_UNKNOWN_IDENTITIES = {"", "n/a", "na", "none", "null", "unknown", "-", "--", "not supported"}


@dataclass(frozen=True)
class HistoryPoint:
    second: int
    util_percent: float | None
    memory_percent: float | None


def device_key(device: Device) -> str | None:
    """Use hardware identity, never an index that a different GPU can inherit."""
    for prefix, value in (("uuid", device.uuid), ("pci", device.bus_id)):
        if isinstance(value, str):
            normalized = value.strip().casefold()
            if normalized.strip("[]") not in _UNKNOWN_IDENTITIES:
                return f"{prefix}:{normalized}"
    return None


def _percent(value: float | None) -> float | None:
    if value is None or isinstance(value, bool) or not math.isfinite(value):
        return None
    return float(value) if 0 <= value <= 100 else None


def memory_percent(device: Device) -> float | None:
    """Return occupied VRAM, not memory-controller utilization."""
    used, total = device.memory_used_bytes, device.memory_total_bytes
    if used is None or total is None or isinstance(used, bool) or isinstance(total, bool):
        return None
    if not math.isfinite(used) or not math.isfinite(total) or total <= 0 or not 0 <= used <= total:
        return None
    return _percent(used / total * 100)


def _peak(first: float | None, second: float | None) -> float | None:
    if first is None:
        return second
    if second is None:
        return first
    return max(first, second)


class HistoryBuffer:
    """One point per second and GPU; access is serialized by Monitor's lock.

    The window includes the current monotonic second and the preceding 299.
    Absent seconds represent gaps. Unknown metrics in an observed second are
    None, while repeated observations of that second retain each metric's peak.
    """

    def __init__(self):
        self._points: dict[str, deque[HistoryPoint]] = {}
        self._last_second: int | None = None

    def _prune(self, second: int):
        if self._last_second is not None and second < self._last_second:
            # Defensive reset if a substituted clock changes its origin.
            self._points.clear()
        self._last_second = second
        cutoff = second - WINDOW_SECONDS + 1
        for key, points in list(self._points.items()):
            while points and points[0].second < cutoff:
                points.popleft()
            if not points:
                del self._points[key]

    def record(self, snapshot: Snapshot, now: float):
        second = math.floor(now)
        self._prune(second)
        if snapshot.devices_stale:
            # Backend failures retain the previous devices for display only.
            return

        keys = [device_key(device) for device in snapshot.devices]
        counts = Counter(keys)
        active = {key for key in keys if key is not None and counts[key] == 1}
        for key in self._points.keys() - active:
            del self._points[key]

        for device, key in zip(snapshot.devices, keys):
            if key not in active:
                continue
            point = HistoryPoint(second, _percent(device.gpu_utilization_percent), memory_percent(device))
            points = self._points.setdefault(key, deque(maxlen=WINDOW_SECONDS))
            if points and points[-1].second == second:
                previous = points.pop()
                point = HistoryPoint(second, _peak(previous.util_percent, point.util_percent),
                                     _peak(previous.memory_percent, point.memory_percent))
            points.append(point)

    def snapshot(self, now: float) -> dict[str, list[HistoryPoint]]:
        self._prune(math.floor(now))
        # Points are frozen; new dictionaries/lists isolate callers completely.
        return {key: list(points) for key, points in self._points.items()}
