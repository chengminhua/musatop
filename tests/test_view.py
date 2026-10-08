import unittest
from dataclasses import replace
from unittest.mock import patch

from musatop.models import Device, Host, Process, Snapshot
from musatop.view import (Options, filter_snapshot, fmt_bytes, fmt_duration, fmt_number,
                          render_text, safe_text, sorted_processes)


class SortingTests(unittest.TestCase):
    def test_missing_numeric_values_stay_last_in_both_directions(self):
        for key, attribute in (("gpu_memory", "gpu_memory_bytes"), ("cpu", "cpu_percent"), ("rss", "rss_bytes")):
            rows = [Process(0, 1), Process(0, 2, **{attribute: 0}), Process(0, 3, **{attribute: 20})]
            for reverse, expected in ((False, [3, 2, 1]), (True, [2, 3, 1])):
                with self.subTest(key=key, reverse=reverse):
                    result = sorted_processes(rows, Options(sort=key, reverse=reverse))
                    self.assertEqual([p.pid for p in result], expected)
            self.assertEqual([p.pid for p in rows], [1, 2, 3])

    def test_user_sort_places_unknown_last_and_honors_reverse(self):
        rows = [Process(0, 1), Process(0, 2, username="alice"), Process(0, 3, username="zoe")]
        self.assertEqual([p.pid for p in sorted_processes(rows, Options(sort="user"))], [2, 3, 1])
        self.assertEqual([p.pid for p in sorted_processes(rows, Options(sort="user", reverse=True))], [3, 2, 1])

    def test_numeric_identity_sort_and_ties_are_deterministic(self):
        rows = [Process(1, 12, gpu_memory_bytes=5), Process(1, 2, gpu_memory_bytes=5),
                Process(0, 2, gpu_memory_bytes=5)]
        for key in ("pid", "gpu", "gpu_memory"):
            with self.subTest(key=key):
                result = sorted_processes(rows, Options(sort=key))
                self.assertEqual([(p.pid, p.device_index) for p in result], [(2, 0), (2, 1), (12, 1)])


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = Snapshot(
            devices=[Device(0), Device(1)],
            processes=[Process(0, 10, username="alice", command="Python Training"),
                       Process(1, 10, username="alice", command="Python Training"),
                       Process(1, 20, username="bob", command="worker"),
                       Process(0, 30, username=None, command=None)],
            errors=["retained diagnostic"])

    def test_gpu_filter_selects_devices_and_processes(self):
        result = filter_snapshot(self.snapshot, Options(gpu={1}))
        self.assertEqual([d.index for d in result.devices], [1])
        self.assertEqual([(p.device_index, p.pid) for p in result.processes], [(1, 10), (1, 20)])

    def test_pid_filter_keeps_all_devices_and_multigpu_rows(self):
        result = filter_snapshot(self.snapshot, Options(pid={10}))
        self.assertEqual(len(result.devices), 2)
        self.assertEqual([p.device_index for p in result.processes], [0, 1])

    def test_user_and_text_filter_are_intersected(self):
        result = filter_snapshot(self.snapshot, Options(user="alice", gpu={1}, search="pYtHoN"))
        self.assertEqual([(p.device_index, p.pid) for p in result.processes], [(1, 10)])
        self.assertEqual(filter_snapshot(self.snapshot, Options(user="Alice")).processes, [])

    def test_current_user_filter_and_explicit_user_can_yield_empty(self):
        with patch("musatop.view.getpass.getuser", return_value="alice"):
            self.assertEqual(len(filter_snapshot(self.snapshot, Options(current_user=True)).processes), 2)
            self.assertEqual(filter_snapshot(self.snapshot, Options(current_user=True, user="bob")).processes, [])

    def test_search_can_match_pid_user_and_missing_values(self):
        for search, expected in (("20", [20]), ("BOB", [20]), ("30", [30]), ("absent", [])):
            with self.subTest(search=search):
                result = filter_snapshot(self.snapshot, Options(search=search))
                self.assertEqual([p.pid for p in result.processes], expected)

    def test_filter_preserves_original_snapshot_and_diagnostics(self):
        result = filter_snapshot(self.snapshot, Options(gpu={99}))
        self.assertEqual(result.devices, [])
        self.assertEqual(result.processes, [])
        self.assertEqual(result.errors, ["retained diagnostic"])
        self.assertEqual(len(self.snapshot.devices), 2)
        self.assertEqual(len(self.snapshot.processes), 4)


class FormattingTests(unittest.TestCase):
    def test_bytes_zero_and_unknown_are_distinct(self):
        self.assertEqual(fmt_bytes(None), "N/A")
        self.assertEqual(fmt_bytes(0), "0MiB")
        self.assertEqual(fmt_bytes(1024**2), "1MiB")
        self.assertEqual(fmt_bytes(3 * 1024**3 // 2), "1.5GiB")
        self.assertEqual(fmt_number(None, "%"), "N/A")
        self.assertEqual(fmt_number(0.0, "%"), "0%")

    def test_duration_days_and_negative_clock_delta(self):
        self.assertEqual(fmt_duration(None), "N/A")
        self.assertEqual(fmt_duration(-1), "00:00:00")
        self.assertEqual(fmt_duration(3661), "01:01:01")
        self.assertEqual(fmt_duration(90061), "1d01:01:01")

    def test_untrusted_control_sequences_are_neutralized(self):
        attack = "\x1b]52;c;secret\x07\n\r\t\x00\x7f\x9b\u202e\ud800safe"
        clean = safe_text(attack)
        self.assertEqual(clean, " ]52;c;secret" + " " * 9 + "safe")
        self.assertEqual(safe_text("中文 --safe"), "中文 --safe")
        self.assertEqual(safe_text(None), "N/A")

    def test_text_output_sanitizes_all_external_string_fields(self):
        attack = "value\x1b[2J\nINJECTED"
        snapshot = Snapshot(
            driver_version=attack, gmi_version=attack, musa_version=attack,
            host=Host(hostname=attack), devices=[Device(0, name=attack)],
            processes=[Process(0, 42, username=attack, command=attack)], errors=[attack])
        text = render_text(snapshot)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\nINJECTED", text)
        self.assertIn("N/A", text)
        self.assertIn("ERROR:", text)

    def test_empty_and_stale_are_distinguished(self):
        snapshot = Snapshot()
        self.assertIn("No GPUs found.", render_text(snapshot))
        self.assertIn("No matching GPU processes.", render_text(snapshot))
        stale = replace(snapshot, devices_stale=True, processes_stale=True,
                        devices_sampled_at="old-devices", processes_sampled_at="old-processes")
        text = render_text(stale)
        self.assertIn("Device data unavailable.", text)
        self.assertIn("Process data unavailable.", text)
        self.assertIn("STALE:", text)
        self.assertIn("old-devices, old-processes", text)

    def test_optional_metric_diagnostics_preserve_null_and_are_not_collection_errors(self):
        snapshot = Snapshot(musa_version_reason="Missing metadata\nINJECTED", devices=[
            Device(0, power_draw_w=410, power_limit_reason="Current power limit is not reported by GMI")])
        text = render_text(snapshot)
        self.assertIn("power limit unavailable from GMI", text)
        self.assertIn("MUSA Toolkit: Missing metadata INJECTED", text)
        self.assertNotIn("ERROR:", text)
        data = snapshot.to_dict()
        self.assertIsNone(data["devices"][0]["power_limit_w"])
        self.assertEqual(data["devices"][0]["power_draw_w"], 410)
        self.assertTrue(data["devices"][0]["power_limit_reason"])
        self.assertEqual(data["errors"], [])


if __name__ == "__main__":
    unittest.main()
