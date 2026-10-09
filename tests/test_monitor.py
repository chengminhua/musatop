import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from musatop.history import AGGREGATE_KEY, HOST_KEY, HistoryPoint, device_key
from musatop.models import Device, Host, Process, Snapshot
from musatop.monitor import Monitor


def eventually(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("background sampler did not reach expected state")


class MonitorTests(unittest.TestCase):
    def test_invalid_intervals_are_rejected_before_start(self):
        for value in (0, -1, float("nan"), float("inf"), 1e300):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Monitor(value)

    def make_monitor(self, backend=None, interval=60):
        monitor = Monitor(interval=interval, backend=backend or MagicMock())
        monitor.enricher = MagicMock()
        monitor.enricher.enrich.side_effect = lambda rows: rows
        monitor.enricher.host.return_value = Host(hostname="fixture-host", cpu_percent=12)
        self.addCleanup(monitor.stop)
        return monitor

    def test_one_shot_enriches_processes_and_host(self):
        backend = MagicMock()
        raw = [Process(0, 42)]
        enriched = [Process(0, 42, username="tester")]
        backend.sample.return_value = Snapshot(processes=raw)
        monitor = self.make_monitor(backend)
        monitor.enricher.enrich.side_effect = None
        monitor.enricher.enrich.return_value = enriched
        snapshot = monitor.sample()
        self.assertEqual(snapshot.processes, enriched)
        self.assertEqual(snapshot.host.hostname, "fixture-host")
        monitor.enricher.enrich.assert_called_once_with(raw)
        backend.sample.assert_called_once_with()
        self.assertEqual(monitor.latest_with_history(), (None, {}))

    def test_host_failure_retains_device_data_and_reports_error(self):
        backend = MagicMock()
        backend.sample.return_value = Snapshot(devices=[Device(0)])
        monitor = self.make_monitor(backend)
        monitor.enricher.host.side_effect = OSError("host metrics unavailable")
        snapshot = monitor.sample()
        self.assertEqual([d.index for d in snapshot.devices], [0])
        self.assertIn("Host sampling failed", snapshot.errors[0])

    def test_host_failure_clears_any_previous_host_values(self):
        backend = MagicMock()
        backend.sample.return_value = Snapshot(host=Host(cpu_percent=99))
        monitor = self.make_monitor(backend)
        monitor.enricher.host.side_effect = OSError("host metrics unavailable")
        self.assertEqual(monitor.sample().host, Host())

    def test_gpu_collection_failure_does_not_prevent_fresh_host_history(self):
        backend = MagicMock()
        backend.sample.return_value = Snapshot(devices_stale=True, errors=["devices: timed out"])
        monitor = self.make_monitor(backend)
        snapshot = monitor.sample()
        monitor._history.record(snapshot, 42)
        self.assertEqual(monitor._history.snapshot(42), {HOST_KEY: [HistoryPoint(42, 12, None)]})

    def test_start_is_idempotent_and_latest_returns_an_independent_copy(self):
        backend = MagicMock()
        backend.sample.return_value = Snapshot(devices=[Device(0)])
        monitor = self.make_monitor(backend)
        self.assertEqual(monitor.revision, 0)
        self.assertIsNone(monitor.latest())
        monitor.start()
        first_thread = monitor._thread
        monitor.start()
        self.assertIs(monitor._thread, first_thread)
        eventually(lambda: monitor.latest() is not None)
        snapshot = monitor.latest()
        snapshot.devices[0].name = "changed"
        snapshot.errors.append("changed")
        self.assertIsNone(monitor.latest().devices[0].name)
        self.assertEqual(monitor.latest().errors, [])
        self.assertEqual(backend.sample.call_count, 1)
        self.assertEqual(monitor.revision, 1)

    def test_background_failure_keeps_last_data_and_marks_both_sections_stale(self):
        backend = MagicMock()
        original = Snapshot(devices=[Device(0, name="fixture")], processes=[Process(0, 42)],
                            devices_sampled_at="device-success", processes_sampled_at="process-success")
        backend.sample.side_effect = [original, RuntimeError("driver unavailable")]
        monitor = self.make_monitor(backend)
        monitor.start()
        eventually(lambda: monitor.latest() is not None)
        monitor.refresh()
        eventually(lambda: monitor.latest().devices_stale)
        snapshot = monitor.latest()
        self.assertTrue(snapshot.processes_stale)
        self.assertEqual(snapshot.devices[0].name, "fixture")
        self.assertEqual(snapshot.processes[0].pid, 42)
        self.assertEqual(snapshot.devices_sampled_at, "device-success")
        self.assertEqual(snapshot.processes_sampled_at, "process-success")
        self.assertIn("RuntimeError: driver unavailable", snapshot.errors[0])
        self.assertFalse(original.devices_stale)
        self.assertEqual(snapshot.host, Host())
        self.assertEqual(monitor.revision, 2)

    def test_initial_background_failure_produces_an_error_snapshot(self):
        backend = MagicMock()
        backend.sample.side_effect = ValueError("bad fixture")
        monitor = self.make_monitor(backend)
        monitor.start()
        eventually(lambda: monitor.latest() is not None)
        snapshot = monitor.latest()
        self.assertEqual(snapshot.devices, [])
        self.assertTrue(snapshot.devices_stale)
        self.assertTrue(snapshot.processes_stale)
        self.assertIn("ValueError", snapshot.errors[0])

    def test_refresh_recovers_from_failure_without_waiting_full_interval(self):
        backend = MagicMock()
        backend.sample.side_effect = [RuntimeError("temporary"), Snapshot(devices=[Device(1)])]
        monitor = self.make_monitor(backend)
        monitor.start()
        eventually(lambda: monitor.latest() is not None)
        self.assertTrue(monitor.latest().devices_stale)
        monitor.refresh()
        eventually(lambda: bool(monitor.latest().devices))
        self.assertFalse(monitor.latest().devices_stale)
        self.assertEqual(monitor.latest().errors, [])

    def test_stop_wakes_sleeping_sampler_and_refresh_does_not_restart_it(self):
        backend = MagicMock()
        backend.sample.return_value = Snapshot()
        monitor = self.make_monitor(backend)
        monitor.start()
        eventually(lambda: monitor.latest() is not None)
        started = time.monotonic()
        monitor.stop()
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(monitor._thread.is_alive())
        calls = backend.sample.call_count
        monitor.refresh()
        time.sleep(0.03)
        self.assertEqual(backend.sample.call_count, calls)

    def test_in_flight_refresh_is_serial_and_runs_after_current_sample(self):
        entered, release = threading.Event(), threading.Event()
        active = 0
        maximum = 0

        def sample():
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            entered.set()
            release.wait(timeout=2)
            active -= 1
            return Snapshot()

        backend = MagicMock()
        backend.sample.side_effect = sample
        monitor = self.make_monitor(backend)
        monitor.start()
        self.assertTrue(entered.wait(timeout=2))
        monitor.refresh()
        release.set()
        eventually(lambda: backend.sample.call_count >= 2)
        monitor.stop()
        self.assertEqual(maximum, 1)
        self.assertEqual(backend.sample.call_count, 2)

    def test_background_history_uses_completion_time_and_skips_failed_samples(self):
        backend = MagicMock()
        first = Device(0, uuid="fixture-gpu", gpu_utilization_percent=10,
                       memory_used_bytes=25, memory_total_bytes=100)
        last = Device(0, uuid="fixture-gpu", gpu_utilization_percent=80,
                      memory_used_bytes=60, memory_total_bytes=100)
        events = iter([(11.5, Snapshot(devices=[first])),
                       (20.1, RuntimeError("driver unavailable")),
                       (33.8, Snapshot(devices=[last]))])
        now = 10.0

        def sample():
            nonlocal now
            now, result = next(events)
            if isinstance(result, Exception):
                raise result
            return result

        backend.sample.side_effect = sample
        monitor = self.make_monitor(backend)
        monitor._stop = MagicMock()
        monitor._stop.is_set.side_effect = [False, False, False, True]
        monitor._wake = MagicMock()
        observations = []
        monitor._wake.wait.side_effect = lambda timeout: observations.append(monitor.latest_with_history())
        with patch("musatop.monitor.time.monotonic", side_effect=lambda: now):
            monitor._run()
            result, history = monitor.latest_with_history()
        key = device_key(first)
        self.assertEqual(history[key], [HistoryPoint(11, 10, 25), HistoryPoint(33, 80, 60)])
        self.assertEqual(observations[0][1][key], [HistoryPoint(11, 10, 25)])
        self.assertTrue(observations[1][0].devices_stale)
        self.assertEqual(observations[1][1][key], [HistoryPoint(11, 10, 25)])
        self.assertFalse(result.devices_stale)
        self.assertEqual(backend.sample.call_count, 3)
        self.assertEqual(history[HOST_KEY], [HistoryPoint(11, 12, None), HistoryPoint(33, 12, None)])
        self.assertEqual(history[AGGREGATE_KEY], [HistoryPoint(11, 10, 25), HistoryPoint(33, 80, 60)])
        self.assertEqual(monitor.revision, 3)

    def test_monitor_applies_startup_gpu_filter_to_aggregate_only(self):
        monitor = Monitor(backend=MagicMock(), gpu_indices={1})
        self.addCleanup(monitor.stop)
        first = Device(0, uuid="fixture-a", gpu_utilization_percent=10)
        second = Device(1, uuid="fixture-b", gpu_utilization_percent=80)
        monitor._history.record(Snapshot(devices=[first, second]), 5)
        with patch("musatop.monitor.time.monotonic", return_value=5):
            _, history = monitor.latest_with_history()
        self.assertEqual(history[AGGREGATE_KEY], [HistoryPoint(5, 80, None)])
        self.assertIn(device_key(first), history)

    def test_latest_with_history_is_consistent_and_independent_and_expires_without_sampling(self):
        monitor = self.make_monitor()
        device = Device(0, uuid="fixture-gpu", gpu_utilization_percent=10)
        monitor._snapshot = Snapshot(devices=[device])
        monitor._history.record(monitor._snapshot, 10)
        key = device_key(device)
        with patch("musatop.monitor.time.monotonic", return_value=10):
            snapshot, history = monitor.latest_with_history()
            snapshot.devices[0].gpu_utilization_percent = 99
            history[key].clear()
            second_snapshot, second_history = monitor.latest_with_history()
        self.assertEqual(second_snapshot.devices[0].gpu_utilization_percent, 10)
        self.assertEqual(second_history[key], [HistoryPoint(10, 10, None)])
        with patch("musatop.monitor.time.monotonic", return_value=310):
            self.assertEqual(monitor.latest_with_history()[1], {})
        self.assertNotIn("history", monitor.latest().to_dict())


if __name__ == "__main__":
    unittest.main()
