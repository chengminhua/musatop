"""Exercise the opt-in acceptance checker without hardware or real waiting."""

import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from musatop.history import HistoryBuffer, HistoryPoint, device_key
from musatop.models import Device, Snapshot
from tools import validate_history as validation


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeProcess:
    def cpu_times(self):
        return (0.2, 0.1)

    def memory_info(self):
        return SimpleNamespace(rss=50 * 1024**2)


def make_snapshot(second=1599):
    return Snapshot(sampled_at=str(second), devices=[
        Device(index=index, uuid=f"private-identity-{index}",
               gpu_utilization_percent=second % 101,
               memory_used_bytes=(second % 10) * 1024**3,
               memory_total_bytes=80 * 1024**3)
        for index in range(8)
    ])


class FakeMonitor:
    def __init__(self, clock):
        self.clock = clock
        self.buffer = HistoryBuffer()
        self.second = None
        self.snapshot = None
        self.samples = 0
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def latest_with_history(self):
        now = self.clock()
        second = int(now)
        if second != self.second:
            self.second = second
            self.samples += 1
            self.snapshot = make_snapshot(second)
            self.buffer.record(self.snapshot, now)
        return self.snapshot, self.buffer.snapshot(now)


class HistoryAcceptanceTests(unittest.TestCase):
    def run_fake(self, monitor_type=FakeMonitor):
        clock = FakeClock()
        monitor = monitor_type(clock)
        lines = []
        report = validation.validate(
            monitor=monitor, clock=clock, sleep=clock.sleep, process=FakeProcess(),
            emit=lambda line, **kwargs: lines.append(line),
        )
        return report, monitor, lines

    def test_simulated_ten_minutes_full_window_eviction_and_privacy(self):
        report, monitor, lines = self.run_fake()
        self.assertTrue(report["passed"], report)
        self.assertTrue(monitor.started and monitor.stopped)
        self.assertEqual(report["samples"], monitor.samples)
        self.assertEqual(report["expected_history_window_seconds"], 300)
        self.assertEqual(len(report["gpus"]), 8)
        for gpu in report["gpus"]:
            self.assertEqual(gpu["max_buckets"], 300)
            self.assertGreaterEqual(gpu["minimum_full_window_buckets"], 299)
            self.assertTrue(gpu["first_bucket_evicted"])
            self.assertEqual(gpu["util_percent_range"], [0, 100])
        self.assertGreaterEqual(len(lines), 10)
        self.assertTrue(all(isinstance(json.loads(line), dict) for line in lines))
        self.assertNotIn("private-identity", "".join(lines))

    def test_repeated_snapshot_is_not_counted_twice(self):
        state = validation.Acceptance(1000, 600)
        snapshot = make_snapshot(1000)
        history = {device_key(device): [HistoryPoint(1000, 0, 0)] for device in snapshot.devices}
        state.observe(snapshot, history, 1000, 1024)
        state.observe(snapshot, history, 1000.2, 2048)
        self.assertEqual(state.samples, 1)
        self.assertEqual(len(state._rss), 1)

    def test_runtime_failure_stops_sampler_and_sanitizes_errors(self):
        class Broken(FakeMonitor):
            def latest_with_history(self):
                raise RuntimeError("private-hostname private-business-command")

        report, monitor, lines = self.run_fake(Broken)
        self.assertFalse(report["passed"])
        self.assertTrue(monitor.stopped)
        self.assertEqual(report["failures"]["validation_runtime_error"], 1)
        self.assertNotIn("private-", "".join(lines))

    def test_interrupt_stops_sampler_and_reports_failure(self):
        class Interrupted(FakeMonitor):
            def latest_with_history(self):
                raise KeyboardInterrupt

        report, monitor, _ = self.run_fake(Interrupted)
        self.assertFalse(report["passed"])
        self.assertTrue(monitor.stopped)
        self.assertEqual(report["failures"]["interrupted"], 1)

    def test_stop_failure_still_emits_final_report(self):
        class Broken(FakeMonitor):
            def latest_with_history(self):
                raise RuntimeError("private-error")

            def stop(self):
                self.stopped = True
                raise RuntimeError("private-shutdown-error")

        report, monitor, lines = self.run_fake(Broken)
        self.assertTrue(monitor.stopped)
        self.assertEqual(report["failures"]["monitor_stop_error"], 1)
        self.assertEqual(json.loads(lines[-1]), report)
        self.assertNotIn("private-", "".join(lines))

    def test_configuration_uses_independent_five_minute_expectation(self):
        with patch.object(validation, "WINDOW_SECONDS", 60):
            state = validation.Acceptance(1000, 600)
        self.assertEqual(state.failures["history_window_configuration_not_300_seconds"], 1)

    def inspect_history(self, points):
        state = validation.Acceptance(1000, 600)
        snapshot = make_snapshot()
        history = {device_key(device): [HistoryPoint(second, 0, 0) for second in range(1301, 1601)]
                   for device in snapshot.devices}
        history[device_key(snapshot.devices[0])] = points
        state.observe(snapshot, history, 1600, 50 * 1024**2)
        return state

    def test_history_bound_age_and_initial_eviction(self):
        state = self.inspect_history([HistoryPoint(second, 0, 0) for second in range(1000, 1601)])
        for failure in ("history_exceeds_bucket_bound", "history_outside_time_window",
                        "initial_bucket_not_evicted"):
            self.assertEqual(state.failures[failure], 1)

    def test_duplicate_seconds_and_sparse_window(self):
        state = self.inspect_history([HistoryPoint(1599, 0, 0)] * 2)
        self.assertEqual(state.failures["history_seconds_not_unique_and_ordered"], 1)
        self.assertEqual(state.failures["full_history_window_under_90_percent"], 1)

    def test_missing_metrics_reduce_coverage(self):
        state = self.inspect_history([HistoryPoint(second, None, None) for second in range(1301, 1601)])
        self.assertEqual(state.failures["full_history_metric_coverage_under_90_percent"], 1)
        self.assertNotIn("invalid_history_metric_value", state.failures)

    def test_nan_infinity_out_of_range_and_bool_are_invalid(self):
        for value in (float("nan"), float("inf"), -1, 101, True):
            with self.subTest(value=value):
                state = self.inspect_history([HistoryPoint(1599, value, value)])
                self.assertEqual(state.failures["invalid_history_metric_value"], 1)
                self.assertEqual(state.devices[0].max_valid_util_buckets, 0)
                self.assertEqual(state.devices[0].max_valid_memory_buckets, 0)

    def test_stale_and_collection_errors_are_fixed_categories(self):
        state = validation.Acceptance(1000, 600)
        snapshot = make_snapshot()
        snapshot.errors = ["private-hostname private-business-command"]
        snapshot.devices_stale = True
        state.observe(snapshot, {}, 1600, 50 * 1024**2)
        self.assertEqual(state.failures["snapshot_has_collection_errors"], 1)
        self.assertEqual(state.failures["stale_snapshot"], 1)
        self.assertNotIn("private-", json.dumps(state.finish(1600, 0)))

    def test_second_half_memory_growth_threshold(self):
        for growth_mib, passes in ((15, True), (16, False), (17, False)):
            with self.subTest(growth_mib=growth_mib):
                state = validation.Acceptance(1000, 600)
                state._rss = [(300, 50 * 1024**2), (599, (50 + growth_mib) * 1024**2)]
                report = state.finish(1600, 0)
                self.assertEqual(report["second_half_rss_growth_bytes"], growth_mib * 1024**2)
                self.assertEqual("second_half_rss_growth_at_least_16_mib_or_missing" not in report["failures"], passes)


if __name__ == "__main__":
    unittest.main()
