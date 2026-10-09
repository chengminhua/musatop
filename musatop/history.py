"""Bounded host and GPU history on the sampler's monotonic clock."""

from collections import Counter, deque
from dataclasses import dataclass
import math

from .models import Device, Host, Snapshot


WINDOW_SECONDS = 300
HOST_KEY = "host"
AGGREGATE_KEY = "aggregate"
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


def _capacity_percent(used: int | None, total: int | None) -> float | None:
    if used is None or total is None or isinstance(used, bool) or isinstance(total, bool):
        return None
    if not math.isfinite(used) or not math.isfinite(total) or total <= 0 or not 0 <= used <= total:
        return None
    return _percent(used / total * 100)


def memory_percent(device: Device) -> float | None:
    """Return occupied VRAM, not memory-controller utilization."""
    return _capacity_percent(device.memory_used_bytes, device.memory_total_bytes)


def host_memory_percent(host: Host) -> float | None:
    return _capacity_percent(host.memory_used_bytes, host.memory_total_bytes)


def aggregate_values(devices: list[Device]) -> tuple[float | None, float | None]:
    """Aggregate one simultaneous sample without silently omitting a GPU.

    UTIL is the arithmetic mean; VRAM is the total used capacity divided by
    total capacity. Completeness is checked independently for the two metrics.
    """
    if not devices:
        return None, None
    utils = [_percent(device.gpu_utilization_percent) for device in devices]
    util = sum(utils) / len(utils) if all(value is not None for value in utils) else None
    memory = None
    if all(memory_percent(device) is not None for device in devices):
        memory = _capacity_percent(sum(device.memory_used_bytes for device in devices),
                                   sum(device.memory_total_bytes for device in devices))
    return util, memory


def _peak(first: float | None, second: float | None) -> float | None:
    if first is None:
        return second
    if second is None:
        return first
    return max(first, second)


class HistoryBuffer:
    """One point per second and series; access is serialized by Monitor's lock.

    The window includes the current monotonic second and the preceding 299.
    Absent seconds represent gaps. Unknown metrics in an observed second are
    None, while repeated observations of that second retain each metric's peak.
    """

    def __init__(self, gpu_indices: set[int] | None = None):
        self._points: dict[str, deque[HistoryPoint]] = {}
        self._last_second: int | None = None
        self._gpu_indices = None if gpu_indices is None else frozenset(gpu_indices)
        self._aggregate_members: frozenset[str] | None = None

    def _prune(self, second: int):
        if self._last_second is not None and second < self._last_second:
            # Defensive reset if a substituted clock changes its origin.
            self._points.clear()
            self._aggregate_members = None
        self._last_second = second
        cutoff = second - WINDOW_SECONDS + 1
        for key, points in list(self._points.items()):
            while points and points[0].second < cutoff:
                points.popleft()
            if not points:
                del self._points[key]

    def _append(self, key: str, second: int, util: float | None, memory: float | None):
        point = HistoryPoint(second, util, memory)
        points = self._points.setdefault(key, deque(maxlen=WINDOW_SECONDS))
        if points and points[-1].second == second:
            previous = points.pop()
            point = HistoryPoint(second, _peak(previous.util_percent, point.util_percent),
                                 _peak(previous.memory_percent, point.memory_percent))
        points.append(point)

    def record(self, snapshot: Snapshot, now: float):
        second = math.floor(now)
        self._prune(second)
        host_util = _percent(snapshot.host.cpu_percent)
        host_memory = host_memory_percent(snapshot.host)
        if host_util is not None or host_memory is not None:
            self._append(HOST_KEY, second, host_util, host_memory)
        if snapshot.devices_stale:
            # Host sampling remains independent of failed GPU queries. Cached
            # GPU values are for display only and never become new history.
            return

        keys = [device_key(device) for device in snapshot.devices]
        counts = Counter(keys)
        active = {key for key in keys if key is not None and counts[key] == 1}
        for key in self._points.keys() - active - {HOST_KEY, AGGREGATE_KEY}:
            del self._points[key]

        for device, key in zip(snapshot.devices, keys):
            if key not in active:
                continue
            self._append(key, second, _percent(device.gpu_utilization_percent), memory_percent(device))

        selected = [device for device in snapshot.devices
                    if self._gpu_indices is None or device.index in self._gpu_indices]
        selected_keys = [device_key(device) for device in selected]
        if not selected or any(key not in active for key in selected_keys):
            # An unknown or ambiguous identity cannot safely join a historical
            # aggregate even when its current metric values are available.
            self._aggregate_members = None
            self._points.pop(AGGREGATE_KEY, None)
            return
        members = frozenset(selected_keys)
        if members != self._aggregate_members:
            self._points.pop(AGGREGATE_KEY, None)
            self._aggregate_members = members
        # Aggregate this sample before taking the per-second peak. Averaging
        # each card's independent peaks would invent a simultaneous load.
        self._append(AGGREGATE_KEY, second, *aggregate_values(selected))

    def snapshot(self, now: float) -> dict[str, list[HistoryPoint]]:
        self._prune(math.floor(now))
        # Points are frozen; new dictionaries/lists isolate callers completely.
        return {key: list(points) for key, points in self._points.items()}
