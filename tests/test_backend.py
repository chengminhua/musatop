"""Collector boundary tests; fixtures contain synthetic identifiers and PIDs."""

import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from musatop.backend import CollectionError, GmiBackend, memory_bytes, parse_devices, parse_processes


FIXTURES = Path(__file__).parent / "fixtures"
QUERY = (FIXTURES / "gmi_query.json").read_text()
PROCESSES = (FIXTURES / "gmi_processes.txt").read_text()
EMPTY = (FIXTURES / "gmi_empty.txt").read_text()


def completed(text, returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, text, stderr)


class DeviceParsingTests(unittest.TestCase):
    def test_vendor_units_trailing_key_space_and_sparse_indices(self):
        devices, driver = parse_devices(QUERY)
        self.assertEqual(driver, "3.3.8-server")
        self.assertEqual([device.index for device in devices], [0, 3])
        device = devices[0]
        self.assertEqual(device.uuid, "fixture-gpu-0")
        self.assertEqual(device.name, "X10000")
        self.assertEqual(device.bus_id, "00000000:03:00.0")
        self.assertEqual(device.memory_total_bytes, 81920 * 1024**2)
        self.assertEqual(device.memory_used_bytes, 1024**3)
        self.assertEqual(device.memory_free_bytes, 80896 * 1024**2)
        self.assertEqual(device.gpu_utilization_percent, 42)
        self.assertEqual(device.memory_utilization_percent, 19)
        self.assertEqual(device.temperature_c, 34)
        self.assertEqual(device.power_draw_w, 367.49)
        self.assertEqual(device.power_limit_w, 950)
        self.assertEqual(device.graphics_clock_mhz, 1750)
        self.assertEqual(device.memory_clock_mhz, 2500)

    def test_unavailable_and_absent_metrics_are_not_zero(self):
        devices, _ = parse_devices(QUERY)
        self.assertEqual(devices[1].gpu_utilization_percent, 0)
        self.assertIsNone(devices[1].memory_used_bytes)
        self.assertIsNone(devices[1].memory_utilization_percent)
        self.assertIsNone(devices[1].temperature_c)
        self.assertIsNone(devices[1].power_draw_w)
        sparse, _ = parse_devices('{"GPU": [{"Index": 0}]}')
        self.assertIsNone(sparse[0].memory_total_bytes)
        self.assertIsNone(sparse[0].uuid)

    def test_explicit_zero_devices(self):
        devices, _ = parse_devices('{"Attached GPUs":"0", "GPU":[]}')
        self.assertEqual(devices, [])

    def test_unknown_current_power_limit_never_uses_another_cap(self):
        for raw in ("N/A", "[Not Supported]", None, "0W", 0, "-1W", "NaNW", "bad"):
            with self.subTest(raw=raw):
                data = {"GPU": [{"Index": 0, "Power Readings": {
                    "Power Draw ": "410W", "Power Limit": "950W",
                    "Current Power Limit": raw, "Default Power Limit": "950W",
                    "Max Power Limit": "1000W"}}]}
                device = parse_devices(json.dumps(data))[0][0]
                self.assertEqual(device.power_draw_w, 410)
                self.assertIsNone(device.power_limit_w)
                self.assertTrue(device.power_limit_reason)

    def test_missing_limit_and_recovered_numeric_limit_have_correct_diagnostics(self):
        cases = [({}, None), ({"Default Power Limit": "950W"}, None),
                 ({"Power Limit": "950W"}, 950),
                 ({"Power Limit": "950W", " current power limit ": "800 W"}, 800)]
        for readings, expected in cases:
            with self.subTest(readings=readings):
                device = parse_devices(json.dumps({"GPU": [{"Index": 0, "Power Readings": readings}]}))[0][0]
                self.assertEqual(device.power_limit_w, expected)
                self.assertEqual(device.power_limit_reason is None, expected is not None)

    def test_corrupt_shapes_cannot_be_interpreted_as_empty(self):
        for value in ("not JSON", "[]", "{}", '{"GPU":{}}', '{"GPU":[null]}',
                      '{"GPU":[{}]}', '{"GPU":[{"Index":true}]}',
                      '{"GPU":[{"Index":-1}]}', '{"GPU":[{"Index":1.5}]}',
                      '{"Attached GPUs":8,"GPU":[]}',
                      '{"GPU":[{"Index":0},{"Index":"0"}]}'):
            with self.subTest(value=value), self.assertRaises(CollectionError):
                parse_devices(value)

    def test_invalid_optional_numbers_are_unknown(self):
        data = json.loads(QUERY)
        gpu = data["GPU"][0]
        gpu["Utilization"]["Gpu"] = "101%"
        gpu["Power Readings"]["Power Draw "] = "NaNW"
        gpu["Temperature"]["GPU Current Temp"] = "-4C"
        devices, _ = parse_devices(json.dumps(data))
        self.assertIsNone(devices[0].gpu_utilization_percent)
        self.assertIsNone(devices[0].power_draw_w)
        self.assertEqual(devices[0].temperature_c, -4)

    def test_duplicate_uuid_rejected_but_multiple_unknown_uuids_allowed(self):
        data = json.loads(QUERY)
        data["GPU"][1]["GPU UUID"] = "FIXTURE-GPU-0"
        with self.assertRaisesRegex(CollectionError, "duplicate GPU UUID"):
            parse_devices(json.dumps(data))
        for entry in data["GPU"]:
            entry["GPU UUID"] = "N/A"
        devices, _ = parse_devices(json.dumps(data))
        self.assertEqual([device.uuid for device in devices], [None, None])

    def test_memory_unit_conversion(self):
        for value, expected in (("1GiB", 1024**3), ("1.5 MiB", 1572864),
                                ("2MB", 2000000), ("512B", 512), (0, 0),
                                ("N/A", None), (None, None), ("-1MiB", None),
                                ("NaN", None), (True, None), ("bad", None)):
            with self.subTest(value=value):
                self.assertEqual(memory_bytes(value), expected)

    def test_memory_unit_multiplication_overflow_is_unknown(self):
        self.assertIsNone(memory_bytes("1" + "0" * 300 + "EiB"))


class ProcessParsingTests(unittest.TestCase):
    def test_sanitized_live_x10000_process_table(self):
        output = (Path(__file__).parent / "fixtures" / "gmi_x10000_process.txt").read_text()
        rows = parse_processes(output)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].device_index, rows[0].pid), (0, 12345))
        self.assertEqual(rows[0].command, "/path/gpu_smoke")
        self.assertEqual(rows[0].gpu_memory_bytes, 111 * 1024**2)

    def test_multiple_cards_same_pid_spaces_in_command_and_unknown_memory(self):
        devices, _ = parse_devices(QUERY)
        rows = parse_processes(PROCESSES, devices)
        self.assertEqual([(row.device_index, row.pid) for row in rows],
                         [(0, 12345), (3, 12345), (3, 23456)])
        self.assertEqual(rows[0].command, "python3 train.py --name example")
        self.assertEqual(rows[0].gpu_memory_bytes, 1024**3)
        self.assertEqual(rows[1].gpu_memory_bytes, 512 * 1024**2)
        self.assertEqual(rows[1].device_uuid, "fixture-gpu-3")
        self.assertIsNone(rows[2].gpu_memory_bytes)

    def test_explicit_empty(self):
        self.assertEqual(parse_processes(EMPTY), [])
        self.assertEqual(parse_processes(EMPTY.replace("No running processes found", "No running processes found.")), [])

    def test_empty_marker_inside_a_command_does_not_hide_the_process(self):
        output = "Processes:\nID PID Process name GPU Memory Usage\n0 123 python No running processes found 12MiB"
        processes = parse_processes(output)
        self.assertEqual(len(processes), 1)
        self.assertEqual(processes[0].command, "python No running processes found")
        self.assertEqual(processes[0].pid, 123)

    def test_missing_or_damaged_table_is_an_error(self):
        for value in ("", "Failed to initialize driver", "Processes:",
                      "Processes:\nNo running processes found",
                      "Processes:\nID PID Process name GPU Memory Usage\n",
                      "Processes:\nID PID Process name GPU Memory Usage\nbad row",
                      "Processes:\n0 123 python 12MiB"):
            with self.subTest(value=value), self.assertRaises(CollectionError):
                parse_processes(value)

    def test_invalid_pid_duplicate_and_contradictory_empty(self):
        header = "Processes:\nID PID Process name GPU Memory Usage\n"
        for value in ("0 0 python 12MiB", "0 42 python 12MiB\n0 42 python 12MiB",
                      "0 42 python 12MiB\nNo running processes found"):
            with self.subTest(value=value), self.assertRaises(CollectionError):
                parse_processes(header + value)

    def test_ansi_and_compact_memory_units(self):
        output = "\x1b[32mProcesses:\x1b[0m\nID PID Process name GPU Memory Usage\n0 42 python 1.5GiB"
        self.assertEqual(parse_processes(output)[0].gpu_memory_bytes, 1610612736)


class BackendCollectionTests(unittest.TestCase):
    def backend(self):
        backend = GmiBackend()
        backend._versions_checked = True
        return backend

    def test_fixed_arguments_locale_and_two_full_card_queries(self):
        with patch("musatop.backend.subprocess.run", side_effect=[completed(QUERY), completed(PROCESSES)]) as run:
            result = self.backend().sample()
        self.assertFalse(result.devices_stale)
        self.assertFalse(result.processes_stale)
        self.assertIsNotNone(result.devices_sampled_at)
        self.assertIsNotNone(result.processes_sampled_at)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0], ["mthreads-gmi", "-q", "--json"])
        self.assertEqual(run.call_args_list[1].args[0], ["mthreads-gmi"])
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["env"]["LC_ALL"], "C")
            self.assertEqual(call.kwargs["timeout"], 3.0)
            self.assertNotIn("shell", call.kwargs)

    def test_independent_staleness_retains_data_and_original_source_time(self):
        backend = self.backend()
        results = [completed(QUERY), completed(PROCESSES),
                   completed("broken JSON"), completed(EMPTY),
                   completed(QUERY), completed("broken process output")]
        with patch("musatop.backend.subprocess.run", side_effect=results):
            first = backend.sample()
            second = backend.sample()
            third = backend.sample()
        self.assertEqual(second.devices, first.devices)
        self.assertEqual(second.devices_sampled_at, first.devices_sampled_at)
        self.assertTrue(second.devices_stale)
        self.assertFalse(second.processes_stale)
        self.assertEqual(second.processes, [])
        self.assertNotEqual(second.processes_sampled_at, first.processes_sampled_at)
        self.assertFalse(third.devices_stale)
        self.assertTrue(third.processes_stale)
        self.assertEqual(third.processes, second.processes)
        self.assertEqual(third.processes_sampled_at, second.processes_sampled_at)
        self.assertEqual(len(third.errors), 1)

    def test_nonempty_cached_processes_survive_command_failure(self):
        backend = self.backend()
        results = [completed(QUERY), completed(PROCESSES), completed(QUERY), completed("", 4, "driver failure")]
        with patch("musatop.backend.subprocess.run", side_effect=results):
            first = backend.sample()
            second = backend.sample()
        self.assertEqual(second.processes, first.processes)
        self.assertEqual(second.processes_sampled_at, first.processes_sampled_at)
        self.assertTrue(second.processes_stale)
        self.assertIn("code 4", second.errors[0])
        self.assertIn("driver failure", second.errors[0])

    def test_fresh_process_query_with_unknown_gpu_preserves_previous_processes(self):
        backend = self.backend()
        changed = json.loads(QUERY)
        changed["Attached GPUs"] = "1"
        changed["GPU"] = changed["GPU"][:1]
        results = [completed(QUERY), completed(PROCESSES),
                   completed(json.dumps(changed)), completed(PROCESSES)]
        with patch("musatop.backend.subprocess.run", side_effect=results):
            first = backend.sample()
            second = backend.sample()
        self.assertFalse(second.devices_stale)
        self.assertTrue(second.processes_stale)
        self.assertEqual(second.processes, first.processes)
        self.assertEqual(second.processes_sampled_at, first.processes_sampled_at)
        self.assertIn("absent from the current device query", second.errors[0])

    def test_zero_fresh_devices_with_process_rows_is_not_a_success(self):
        with patch("musatop.backend.subprocess.run", side_effect=[completed('{"GPU": []}'), completed(PROCESSES)]):
            snapshot = self.backend().sample()
        self.assertTrue(snapshot.processes_stale)
        self.assertIsNone(snapshot.processes_sampled_at)
        self.assertEqual(snapshot.processes, [])

    def test_fresh_process_rows_do_not_use_stale_device_uuid_mapping(self):
        backend = self.backend()
        results = [completed(QUERY), completed(PROCESSES), completed("bad JSON"), completed(PROCESSES)]
        with patch("musatop.backend.subprocess.run", side_effect=results):
            first = backend.sample()
            second = backend.sample()
        self.assertEqual(first.processes[0].device_uuid, "fixture-gpu-0")
        self.assertTrue(second.devices_stale)
        self.assertFalse(second.processes_stale)
        self.assertTrue(all(process.device_uuid is None for process in second.processes))
        self.assertEqual(second.devices_sampled_at, first.devices_sampled_at)

    def test_first_device_failure_still_allows_unmapped_process_rows(self):
        with patch("musatop.backend.subprocess.run", side_effect=[completed("bad JSON"), completed(PROCESSES)]):
            snapshot = self.backend().sample()
        self.assertTrue(snapshot.devices_stale)
        self.assertFalse(snapshot.processes_stale)
        self.assertEqual(len(snapshot.processes), 3)
        self.assertTrue(all(process.device_uuid is None for process in snapshot.processes))

    def test_first_failure_remains_unknown_not_a_fresh_empty_snapshot(self):
        cases = [(FileNotFoundError("missing"), "not found"),
                 (subprocess.TimeoutExpired(["mthreads-gmi"], 3), "timed out"),
                 (PermissionError("denied"), "could not start")]
        for error, expected in cases:
            with self.subTest(error=error), patch("musatop.backend.subprocess.run", side_effect=error):
                snapshot = self.backend().sample()
            self.assertTrue(snapshot.devices_stale)
            self.assertTrue(snapshot.processes_stale)
            self.assertIsNone(snapshot.devices_sampled_at)
            self.assertIsNone(snapshot.processes_sampled_at)
            self.assertEqual(len(snapshot.errors), 2)
            self.assertIn(expected, snapshot.errors[0])

    def test_versions_are_cached_including_failed_optional_detection(self):
        backend = GmiBackend()
        results = [completed("mthreads-gmi version : 2.3.3\n"), completed(QUERY), completed(EMPTY),
                   completed(QUERY), completed(EMPTY)]
        with patch("musatop.backend.subprocess.run", side_effect=results) as run, \
             patch("musatop.backend.detect_toolkit", return_value=("5.1.0", "/example/version.json", None)) as read:
            first = backend.sample()
            second = backend.sample()
        self.assertEqual(first.gmi_version, "2.3.3")
        self.assertEqual(second.musa_version, "5.1.0")
        self.assertEqual(second.musa_version_source, "/example/version.json")
        self.assertIsNone(second.musa_version_reason)
        self.assertEqual(run.call_count, 5)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(run.call_args_list[0].kwargs["timeout"], 1)
        backend = GmiBackend()
        results = [FileNotFoundError(), completed(QUERY), completed(EMPTY), completed(QUERY), completed(EMPTY)]
        with patch("musatop.backend.subprocess.run", side_effect=results) as run, \
             patch("musatop.backend.detect_toolkit", return_value=(None, None, "No MUSA Toolkit installation found")) as read:
            backend.sample()
            result = backend.sample()
        self.assertEqual(run.call_count, 5)
        self.assertEqual(read.call_count, 1)
        self.assertIsNone(result.gmi_version)
        self.assertIsNone(result.musa_version)
        self.assertEqual(result.musa_version_reason, "No MUSA Toolkit installation found")
        self.assertEqual(result.errors, [])

    def test_returned_snapshot_mutation_does_not_corrupt_cached_sample(self):
        backend = self.backend()
        results = [completed(QUERY), completed(PROCESSES), completed("bad"), completed("bad")]
        with patch("musatop.backend.subprocess.run", side_effect=results):
            first = backend.sample()
            first.devices[0].name = "changed by caller"
            first.processes[0].command = "changed by caller"
            second = backend.sample()
        self.assertEqual(second.devices[0].name, "X10000")
        self.assertEqual(second.processes[0].command, "python3 train.py --name example")

    def test_invalid_timeout(self):
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                GmiBackend(timeout=timeout)


if __name__ == "__main__":
    unittest.main()
