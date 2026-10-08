import threading
import time
import unittest
from unittest.mock import MagicMock

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

    def test_host_failure_retains_device_data_and_reports_error(self):
        backend = MagicMock()
        backend.sample.return_value = Snapshot(devices=[Device(0)])
        monitor = self.make_monitor(backend)
        monitor.enricher.host.side_effect = OSError("host metrics unavailable")
        snapshot = monitor.sample()
        self.assertEqual([d.index for d in snapshot.devices], [0])
        self.assertIn("Host sampling failed", snapshot.errors[0])

    def test_start_is_idempotent_and_latest_returns_an_independent_copy(self):
        backend = MagicMock()
        backend.sample.return_value = Snapshot(devices=[Device(0)])
        monitor = self.make_monitor(backend)
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


if __name__ == "__main__":
    unittest.main()
