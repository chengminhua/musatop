"""Read-only, opt-in five-minute history acceptance for eight 80 GiB GPUs.

Only Monitor's background sampler queries GMI. No GPU workload is launched.
Output is JSON Lines containing counts and ranges, never device identities,
hostnames, user names, process IDs, commands, or raw collection errors.
"""

import argparse
from collections import Counter
from dataclasses import dataclass, field
import json
import math
import time

import psutil

from musatop.history import (AGGREGATE_KEY, HOST_KEY, WINDOW_SECONDS, aggregate_values,
                             device_key, host_memory_percent, memory_percent)
from musatop.monitor import Monitor


EXPECTED_DEVICES = 8
EXPECTED_MEMORY_BYTES = 80 * 1024**3
EXPECTED_WINDOW_SECONDS = 300
RSS_GROWTH_LIMIT_BYTES = 16 * 1024**2


def valid_percent(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 0 <= value <= 100)


@dataclass
class DeviceStats:
    """Numeric history statistics, shared by devices and named summaries."""

    first_second: int | None = None
    first_bucket_evicted: bool = False
    max_buckets: int = 0
    max_valid_util_buckets: int = 0
    max_valid_memory_buckets: int = 0
    minimum_full_window_buckets: int | None = None
    util_min: float | None = None
    util_max: float | None = None
    memory_min: float | None = None
    memory_max: float | None = None

    def add_value(self, name, value):
        if not valid_percent(value):
            return
        low, high = getattr(self, name + "_min"), getattr(self, name + "_max")
        setattr(self, name + "_min", value if low is None else min(low, value))
        setattr(self, name + "_max", value if high is None else max(high, value))

    def report(self, index):
        return {
            "gpu_index": index,
            "first_bucket_evicted": self.first_bucket_evicted,
            "max_buckets": self.max_buckets,
            "max_valid_util_buckets": self.max_valid_util_buckets,
            "max_valid_memory_buckets": self.max_valid_memory_buckets,
            "minimum_full_window_buckets": self.minimum_full_window_buckets,
            "util_percent_range": [self.util_min, self.util_max],
            "memory_occupied_percent_range": [self.memory_min, self.memory_max],
        }

    def summary_report(self, name):
        report = self.report(None)
        del report["gpu_index"]
        return {"name": name, **report}


@dataclass
class Acceptance:
    """Pure aggregation, with injected monotonic times for deterministic checks."""

    started: float
    duration: float
    samples: int = 0
    max_gap: float = 0
    max_history_age: int = 0
    failures: Counter = field(default_factory=Counter)
    devices: dict[int, DeviceStats] = field(default_factory=dict)
    summaries: dict[str, DeviceStats] = field(default_factory=dict)
    _keys: dict[int, str] = field(default_factory=dict)
    _previous_sample: str | None = None
    _last_observed: float | None = None
    _rss: list[tuple[float, int]] = field(default_factory=list)

    def __post_init__(self):
        if WINDOW_SECONDS != EXPECTED_WINDOW_SECONDS:
            self.failures["history_window_configuration_not_300_seconds"] += 1

    def observe(self, snapshot, history, now, rss):
        if snapshot is None or snapshot.sampled_at == self._previous_sample:
            return
        self._previous_sample = snapshot.sampled_at
        previous = self.started if self._last_observed is None else self._last_observed
        self.max_gap = max(self.max_gap, now - previous)
        self._last_observed = now
        self.samples += 1
        self._rss.append((now - self.started, rss))
        if snapshot.errors:
            self.failures["snapshot_has_collection_errors"] += 1
        if snapshot.devices_stale or snapshot.processes_stale:
            self.failures["stale_snapshot"] += 1
        if len(snapshot.devices) != EXPECTED_DEVICES:
            self.failures["unexpected_device_count"] += 1
        if any(device.memory_total_bytes != EXPECTED_MEMORY_BYTES for device in snapshot.devices):
            self.failures["unexpected_device_memory_capacity"] += 1
        if len({device.index for device in snapshot.devices}) != len(snapshot.devices):
            self.failures["duplicate_device_index"] += 1

        current_keys = [device_key(device) for device in snapshot.devices]
        if None in current_keys or len(set(current_keys)) != len(current_keys):
            self.failures["missing_or_duplicate_device_identity"] += 1
        if set(history) - {HOST_KEY, AGGREGATE_KEY} != {key for key in current_keys if key is not None}:
            self.failures["history_device_mismatch"] += 1
        for device, key in zip(snapshot.devices, current_keys):
            if key is None:
                continue
            if device.index in self._keys and self._keys[device.index] != key:
                self.failures["device_identity_changed"] += 1
            self._keys[device.index] = key
            stats = self.devices.setdefault(device.index, DeviceStats())
            stats.add_value("util", device.gpu_utilization_percent)
            stats.add_value("memory", memory_percent(device))
            self._observe_history(stats, history.get(key, []), now)
        summary_values = {
            HOST_KEY: (snapshot.host.cpu_percent, host_memory_percent(snapshot.host)),
            AGGREGATE_KEY: aggregate_values(snapshot.devices),
        }
        for name, (util, memory) in summary_values.items():
            stats = self.summaries.setdefault(name, DeviceStats())
            stats.add_value("util", util)
            stats.add_value("memory", memory)
            if any(value is not None and not valid_percent(value) for value in (util, memory)):
                self.failures["invalid_summary_metric_value"] += 1
            self._observe_history(stats, history.get(name, []), now, summary=True)

    def _observe_history(self, stats, points, now, *, summary=False):
        """Apply identical five-minute coverage and eviction rules to every plot."""
        seconds = [point.second for point in points]
        valid_util = sum(valid_percent(point.util_percent) for point in points)
        valid_memory = sum(valid_percent(point.memory_percent) for point in points)
        if any(value is not None and not valid_percent(value)
               for point in points for value in (point.util_percent, point.memory_percent)):
            self.failures["invalid_history_metric_value"] += 1
        stats.max_buckets = max(stats.max_buckets, len(points))
        stats.max_valid_util_buckets = max(stats.max_valid_util_buckets, valid_util)
        stats.max_valid_memory_buckets = max(stats.max_valid_memory_buckets, valid_memory)
        if len(points) > EXPECTED_WINDOW_SECONDS:
            self.failures["history_exceeds_bucket_bound"] += 1
        if seconds != sorted(set(seconds)):
            self.failures["history_seconds_not_unique_and_ordered"] += 1
        if not seconds:
            self.failures["history_empty_for_required_summary" if summary else "history_empty_for_present_device"] += 1
            return
        # The read and this observation can straddle a second boundary;
        # allow that one second, but never allow 301 stored buckets.
        age = math.floor(now) - seconds[0]
        self.max_history_age = max(self.max_history_age, age)
        if age > EXPECTED_WINDOW_SECONDS or seconds[-1] > math.floor(now):
            self.failures["history_outside_time_window"] += 1
        if stats.first_second is None:
            stats.first_second = seconds[0]
        if math.floor(now) > stats.first_second + EXPECTED_WINDOW_SECONDS:
            if stats.first_second in seconds:
                self.failures["initial_bucket_not_evicted"] += 1
            else:
                stats.first_bucket_evicted = True
        if now - self.started >= EXPECTED_WINDOW_SECONDS + 5:
            previous_count = stats.minimum_full_window_buckets
            stats.minimum_full_window_buckets = len(points) if previous_count is None else min(previous_count, len(points))
            if len(points) < EXPECTED_WINDOW_SECONDS * 0.9:
                self.failures["full_history_window_under_90_percent"] += 1
            if min(valid_util, valid_memory) < EXPECTED_WINDOW_SECONDS * 0.9:
                self.failures["full_history_metric_coverage_under_90_percent"] += 1

    def progress(self, now):
        return {"check": "history", "state": "running",
                "elapsed_seconds": round(now - self.started, 1), "samples": self.samples,
                "max_buckets_per_gpu": max((d.max_buckets for d in self.devices.values()), default=0),
                "max_buckets_per_summary": max((d.max_buckets for d in self.summaries.values()), default=0),
                "failed_observations": sum(self.failures.values())}

    def finish(self, now, cpu_seconds):
        elapsed = now - self.started
        tail_gap = now - (self.started if self._last_observed is None else self._last_observed)
        self.max_gap = max(self.max_gap, tail_gap)
        if elapsed < self.duration:
            self.failures["monitoring_ended_early"] += 1
        if self.samples < self.duration * 0.9:
            self.failures["sample_count_under_90_percent"] += 1
        if self.max_gap >= 5:
            self.failures["sample_gap_at_least_five_seconds"] += 1
        if len(self.devices) != EXPECTED_DEVICES:
            self.failures["expected_eight_device_histories"] += 1
        if set(self.summaries) != {HOST_KEY, AGGREGATE_KEY}:
            self.failures["expected_host_and_aggregate_histories"] += 1
        all_stats = list(self.devices.values()) + list(self.summaries.values())
        if any(not device.first_bucket_evicted for device in all_stats):
            self.failures["initial_bucket_eviction_not_observed"] += 1
        if any(device.max_valid_util_buckets < EXPECTED_WINDOW_SECONDS * 0.9 or
               device.max_valid_memory_buckets < EXPECTED_WINDOW_SECONDS * 0.9 for device in all_stats):
            self.failures["valid_history_window_under_90_percent"] += 1
        second_half = [rss for elapsed_at_sample, rss in self._rss if elapsed_at_sample >= self.duration / 2]
        growth = max(0, max(second_half) - second_half[0]) if second_half else None
        if growth is None or growth >= RSS_GROWTH_LIMIT_BYTES:
            self.failures["second_half_rss_growth_at_least_16_mib_or_missing"] += 1
        rss_values = [rss for _, rss in self._rss]
        return {
            "check": "history", "state": "completed", "passed": not self.failures,
            "requested_seconds": self.duration, "elapsed_seconds": round(elapsed, 3),
            "history_window_seconds": WINDOW_SECONDS, "sampling_interval_seconds": 1,
            "expected_history_window_seconds": EXPECTED_WINDOW_SECONDS,
            "samples": self.samples, "max_observed_sample_gap_seconds": round(self.max_gap, 3),
            "max_history_age_seconds": self.max_history_age,
            "rss_min_bytes": min(rss_values, default=None), "rss_max_bytes": max(rss_values, default=None),
            "second_half_rss_growth_bytes": growth,
            "cpu_percent_one_core": round(100 * cpu_seconds / elapsed, 3) if elapsed > 0 else None,
            "gpus": [self.devices[index].report(index) for index in sorted(self.devices)],
            "summaries": [self.summaries[name].summary_report(name) for name in sorted(self.summaries)],
            "failures": dict(sorted(self.failures.items())),
        }


def validate(seconds=600, *, monitor=None, clock=time.monotonic, sleep=time.sleep, process=None, emit=print):
    """Run one sampler; injectable collaborators allow an instant fake-clock test."""
    monitor = Monitor(interval=1) if monitor is None else monitor
    process = psutil.Process() if process is None else process
    started = clock()
    acceptance = Acceptance(started, seconds)
    cpu_start = sum(process.cpu_times()[:2])
    next_progress = started + 60
    emit(json.dumps({"check": "history", "state": "started", "seconds": seconds}), flush=True)
    try:
        monitor.start()
        while clock() - started < seconds:
            snapshot, history = monitor.latest_with_history()
            now = clock()
            acceptance.observe(snapshot, history, now, process.memory_info().rss)
            if now >= next_progress:
                emit(json.dumps(acceptance.progress(now)), flush=True)
                next_progress = now + 60
            sleep(min(0.2, max(0, seconds - (clock() - started))))
    except KeyboardInterrupt:
        acceptance.failures["interrupted"] += 1
    except Exception:
        # Do not propagate diagnostics that might contain a device identity,
        # hostname, command line, or other private details into the report.
        acceptance.failures["validation_runtime_error"] += 1
    finally:
        finished = clock()
        try:
            monitor.stop()
        except Exception:
            acceptance.failures["monitor_stop_error"] += 1
    report = acceptance.finish(finished, sum(process.cpu_times()[:2]) - cpu_start)
    emit(json.dumps(report), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=600,
                        help="continuous observation duration; at least 600 seconds (default: 600)")
    args = parser.parse_args()
    if args.seconds < 600:
        parser.error("history acceptance requires at least 600 seconds")
    return 0 if validate(args.seconds)["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
