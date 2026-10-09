from dataclasses import FrozenInstanceError, replace
import unittest

from musatop.history import (AGGREGATE_KEY, HOST_KEY, HistoryBuffer, HistoryPoint, WINDOW_SECONDS,
                             aggregate_values, device_key, host_memory_percent, memory_percent)
from musatop.models import Device, Host, Snapshot


def gpu(index=0, uuid="fixture-gpu-a", util=20, used=25, total=100, bus_id=None):
    return Device(index=index, uuid=uuid, bus_id=bus_id, gpu_utilization_percent=util,
                  memory_used_bytes=used, memory_total_bytes=total)


class IdentityAndMetricsTests(unittest.TestCase):
    def test_uuid_precedes_pci_and_normalizes_case(self):
        self.assertEqual(device_key(gpu(uuid=" GPU-AbC ", bus_id="0000:03:00.0")), "uuid:gpu-abc")
        self.assertEqual(device_key(gpu(uuid=None, bus_id=" 0000:AB:00.0 ")), "pci:0000:ab:00.0")

    def test_unknown_identity_never_uses_device_index(self):
        for uuid in (None, "", "N/A", "unknown", " None ", "--", "[N/A]"):
            with self.subTest(uuid=uuid):
                self.assertIsNone(device_key(gpu(uuid=uuid)))
                self.assertEqual(device_key(gpu(uuid=uuid, bus_id="0000:03:00.0")), "pci:0000:03:00.0")

    def test_memory_ratio_uses_capacity_not_controller_utilization(self):
        device = gpu(used=25, total=80)
        device.memory_utilization_percent = 99
        self.assertEqual(memory_percent(device), 31.25)
        self.assertEqual(memory_percent(gpu(used=0)), 0)
        self.assertEqual(memory_percent(gpu(used=100)), 100)

    def test_unavailable_or_inconsistent_capacity_is_unknown(self):
        for used, total in ((None, 100), (10, None), (0, 0), (0, -1), (-1, 100),
                            (101, 100), (float("nan"), 100), (1, float("inf")),
                            (True, 100), (0, False)):
            with self.subTest(used=used, total=total):
                self.assertIsNone(memory_percent(gpu(used=used, total=total)))

    def test_host_memory_uses_same_capacity_rules(self):
        self.assertEqual(host_memory_percent(Host(memory_used_bytes=25, memory_total_bytes=100)), 25)
        for used, total in ((None, 100), (0, 0), (-1, 100), (101, 100)):
            with self.subTest(used=used, total=total):
                self.assertIsNone(host_memory_percent(Host(memory_used_bytes=used, memory_total_bytes=total)))

    def test_aggregate_is_mean_util_and_capacity_weighted_memory(self):
        devices = [gpu(util=10, used=20, total=100), gpu(index=1, util=90, used=180, total=300)]
        self.assertEqual(aggregate_values(devices), (50, 50))
        self.assertEqual(aggregate_values([]), (None, None))

    def test_aggregate_metrics_require_every_device_independently(self):
        self.assertEqual(aggregate_values([gpu(util=None), gpu(index=1)]), (None, 25))
        self.assertEqual(aggregate_values([gpu(used=None), gpu(index=1)]), (20, None))
        for util in (-1, 101, float("inf"), True):
            with self.subTest(util=util):
                self.assertEqual(aggregate_values([gpu(util=util), gpu(index=1)]), (None, 25))


class HistoryBufferTests(unittest.TestCase):
    def setUp(self):
        self.history = HistoryBuffer()
        self.device = gpu()
        self.key = device_key(self.device)

    def record(self, now, device=None, **kwargs):
        self.history.record(Snapshot(devices=[device or self.device], **kwargs), now)

    def points(self, now):
        return self.history.snapshot(now).get(self.key, [])

    def test_points_are_monotonic_seconds_and_metrics_are_independent(self):
        self.record(42.99)
        self.assertEqual(self.points(42.99), [HistoryPoint(42, 20, 25)])

    def test_multiple_samples_in_one_second_use_per_metric_peaks(self):
        self.record(10.01, gpu(util=70, used=10))
        self.record(10.2, gpu(util=30, used=90))
        self.record(10.8, gpu(util=None, used=None))
        self.assertEqual(self.points(10.99), [HistoryPoint(10, 70, 90)])

    def test_unknown_then_valid_within_bucket_and_zero_is_not_missing(self):
        self.record(8.01, gpu(util=None, used=None))
        self.record(8.9, gpu(util=0, used=0))
        self.assertEqual(self.points(8.99), [HistoryPoint(8, 0, 0)])

    def test_unknown_metrics_remain_none(self):
        self.record(5, gpu(util=None, total=None))
        self.assertEqual(self.points(5), [HistoryPoint(5, None, None)])

    def test_invalid_utilization_is_not_drawn_as_a_real_sample(self):
        for util in (-1, 101, float("inf"), float("nan"), True):
            with self.subTest(util=util):
                history = HistoryBuffer()
                history.record(Snapshot(devices=[gpu(util=util)]), 5)
                self.assertIsNone(history.snapshot(5)[self.key][0].util_percent)

    def test_exact_window_boundary_keeps_current_and_previous_299_seconds(self):
        self.record(100.25)
        self.assertEqual(self.points(399.999), [HistoryPoint(100, 20, 25)])
        self.assertEqual(self.points(400.0), [])

    def test_dense_long_running_sampling_stays_bounded(self):
        for tick in range(12001):
            self.record(tick / 10)
        points = self.points(1200)
        self.assertEqual(len(points), WINDOW_SECONDS)
        self.assertEqual([point.second for point in points], list(range(901, 1201)))

    def test_sparse_or_changed_intervals_preserve_gaps_without_interpolation(self):
        for second in (50.1, 50.9, 51, 61, 80, 300):
            self.record(second)
        self.assertEqual([point.second for point in self.points(300)], [50, 51, 61, 80, 300])
        self.record(700)
        self.assertEqual(self.points(700), [HistoryPoint(700, 20, 25)])

    def test_stale_device_data_does_not_repeat_last_known_value(self):
        self.record(1)
        self.record(2, devices_stale=True)
        self.record(3, devices_stale=True)
        self.record(4, gpu(util=55, used=40))
        self.assertEqual(self.points(4), [HistoryPoint(1, 20, 25), HistoryPoint(4, 55, 40)])

    def test_stale_data_still_expires_after_window(self):
        self.record(1)
        self.record(301, devices_stale=True)
        self.assertEqual(self.history.snapshot(301), {})

    def test_process_or_host_failure_does_not_discard_fresh_device_metrics(self):
        self.record(1, processes_stale=True, errors=["process query failed", "host failed"])
        self.assertEqual(self.points(1), [HistoryPoint(1, 20, 25)])

    def test_disappearance_and_return_start_a_new_history(self):
        self.record(1)
        self.history.record(Snapshot(), 2)
        self.assertEqual(self.history.snapshot(2), {})
        self.record(3)
        self.assertEqual(self.points(3), [HistoryPoint(3, 20, 25)])

    def test_device_replacement_at_same_index_does_not_inherit_history(self):
        self.record(1)
        replacement = gpu(uuid="fixture-gpu-b")
        self.record(2, replacement)
        self.assertEqual(self.history.snapshot(2), {
            device_key(replacement): [HistoryPoint(2, 20, 25)],
            AGGREGATE_KEY: [HistoryPoint(2, 20, 25)],
        })

    def test_device_reindexing_preserves_correct_uuid_history(self):
        other = gpu(index=1, uuid="fixture-gpu-b", util=80)
        self.history.record(Snapshot(devices=[self.device, other]), 1)
        moved_a = replace(self.device, index=1, gpu_utilization_percent=30)
        moved_b = replace(other, index=0, gpu_utilization_percent=90)
        self.history.record(Snapshot(devices=[moved_b, moved_a]), 2)
        result = self.history.snapshot(2)
        self.assertEqual([point.util_percent for point in result[self.key]], [20, 30])
        self.assertEqual([point.util_percent for point in result[device_key(other)]], [80, 90])

    def test_pci_identity_fallback_does_not_retain_history_if_uuid_becomes_unknown(self):
        device = gpu(bus_id="0000:03:00.0")
        self.record(1, device)
        device.uuid = None
        self.record(2, device)
        self.assertEqual(self.history.snapshot(2), {
            "pci:0000:03:00.0": [HistoryPoint(2, 20, 25)],
            AGGREGATE_KEY: [HistoryPoint(2, 20, 25)],
        })

    def test_devices_without_stable_identity_never_generate_history(self):
        for second in (1, 2, 3):
            self.record(second, gpu(uuid=None))
        self.assertEqual(self.history.snapshot(3), {})

    def test_ambiguous_duplicate_identity_is_discarded(self):
        self.record(1)
        self.history.record(Snapshot(devices=[self.device, replace(self.device, index=1)]), 2)
        self.assertEqual(self.history.snapshot(2), {})

    def test_snapshots_have_independent_containers_and_immutable_points(self):
        self.record(1)
        first = self.history.snapshot(1)
        point = first[self.key][0]
        with self.assertRaises(FrozenInstanceError):
            point.util_percent = 99
        first[self.key].append(HistoryPoint(2, 99, 99))
        first["unrelated"] = []
        self.assertEqual(self.history.snapshot(1), {
            self.key: [HistoryPoint(1, 20, 25)], AGGREGATE_KEY: [HistoryPoint(1, 20, 25)],
        })

    def test_changed_clock_origin_resets_instead_of_showing_future_data(self):
        self.record(100)
        self.record(90)
        self.assertEqual(self.points(90), [HistoryPoint(90, 20, 25)])


class HostAndAggregateHistoryTests(unittest.TestCase):
    def setUp(self):
        self.history = HistoryBuffer()
        self.a = gpu()
        self.b = gpu(index=1, uuid="fixture-gpu-b", util=80, used=75)
        self.host = Host(cpu_percent=12, memory_used_bytes=30, memory_total_bytes=100)

    def test_host_is_recorded_without_devices(self):
        self.history.record(Snapshot(host=self.host), 10)
        self.assertEqual(self.history.snapshot(10), {HOST_KEY: [HistoryPoint(10, 12, 30)]})

    def test_host_continues_during_gpu_failure_and_missing_host_leaves_gap(self):
        self.history.record(Snapshot(host=self.host, devices=[self.a]), 1)
        self.history.record(Snapshot(host=replace(self.host, cpu_percent=80),
                                     devices=[self.a], devices_stale=True), 2)
        self.history.record(Snapshot(host=Host(), devices=[self.a]), 3)
        self.history.record(Snapshot(host=self.host, devices=[self.a]), 4)
        result = self.history.snapshot(4)
        self.assertEqual(result[HOST_KEY], [HistoryPoint(1, 12, 30), HistoryPoint(2, 80, 30),
                                           HistoryPoint(4, 12, 30)])
        self.assertEqual([p.second for p in result[AGGREGATE_KEY]], [1, 3, 4])

    def test_host_missing_metric_does_not_discard_other_metric(self):
        self.history.record(Snapshot(host=Host(cpu_percent=0)), 1)
        self.history.record(Snapshot(host=Host(memory_used_bytes=0, memory_total_bytes=100)), 2)
        self.assertEqual(self.history.snapshot(2)[HOST_KEY],
                         [HistoryPoint(1, 0, None), HistoryPoint(2, None, 0)])

    def test_host_and_aggregate_have_same_peak_and_expiry_policy_as_devices(self):
        self.history.record(Snapshot(host=self.host, devices=[self.a]), 10.1)
        self.history.record(Snapshot(host=replace(self.host, cpu_percent=80, memory_used_bytes=20),
                                     devices=[self.b]), 10.9)
        self.assertEqual(self.history.snapshot(309)[HOST_KEY], [HistoryPoint(10, 80, 30)])
        self.assertEqual(self.history.snapshot(310), {})

    def test_aggregate_precedes_per_second_peak_for_non_simultaneous_card_peaks(self):
        self.history.record(Snapshot(devices=[replace(self.a, gpu_utilization_percent=100, memory_used_bytes=100),
                                             replace(self.b, gpu_utilization_percent=0, memory_used_bytes=0)]), 1.1)
        self.history.record(Snapshot(devices=[replace(self.a, gpu_utilization_percent=0, memory_used_bytes=0),
                                             replace(self.b, gpu_utilization_percent=100, memory_used_bytes=100)]), 1.9)
        result = self.history.snapshot(1.9)
        self.assertEqual(result[device_key(self.a)], [HistoryPoint(1, 100, 100)])
        self.assertEqual(result[device_key(self.b)], [HistoryPoint(1, 100, 100)])
        self.assertEqual(result[AGGREGATE_KEY], [HistoryPoint(1, 50, 50)])

    def test_filtered_aggregate_does_not_filter_single_card_history(self):
        indices = {1}
        history = HistoryBuffer(gpu_indices=indices)
        indices.add(0)
        history.record(Snapshot(devices=[self.a, self.b]), 1)
        result = history.snapshot(1)
        self.assertEqual(result[AGGREGATE_KEY], [HistoryPoint(1, 80, 75)])
        self.assertIn(device_key(self.a), result)
        self.assertIn(device_key(self.b), result)

    def test_membership_changes_reset_aggregate_but_preserve_surviving_card(self):
        self.history.record(Snapshot(devices=[self.a, self.b]), 1)
        self.history.record(Snapshot(devices=[self.a]), 2)
        result = self.history.snapshot(2)
        self.assertEqual(result[AGGREGATE_KEY], [HistoryPoint(2, 20, 25)])
        self.assertEqual([p.second for p in result[device_key(self.a)]], [1, 2])
        self.history.record(Snapshot(devices=[self.a, self.b]), 3)
        self.assertEqual(self.history.snapshot(3)[AGGREGATE_KEY], [HistoryPoint(3, 50, 50)])

    def test_same_set_reordered_and_reindexed_retains_aggregate(self):
        self.history.record(Snapshot(devices=[self.a, self.b]), 1)
        self.history.record(Snapshot(devices=[replace(self.b, index=0), replace(self.a, index=1)]), 2)
        self.assertEqual([p.second for p in self.history.snapshot(2)[AGGREGATE_KEY]], [1, 2])

    def test_filtered_identity_change_resets_even_when_indices_match(self):
        history = HistoryBuffer(gpu_indices={0})
        history.record(Snapshot(devices=[self.a, self.b]), 1)
        history.record(Snapshot(devices=[replace(self.b, index=0), replace(self.a, index=1)]), 2)
        self.assertEqual(history.snapshot(2)[AGGREGATE_KEY], [HistoryPoint(2, 80, 75)])

    def test_unfiltered_device_change_does_not_reset_filtered_aggregate(self):
        history = HistoryBuffer(gpu_indices={0})
        history.record(Snapshot(devices=[self.a, self.b]), 1)
        history.record(Snapshot(devices=[self.a, replace(self.b, uuid="fixture-gpu-c")]), 2)
        self.assertEqual([p.second for p in history.snapshot(2)[AGGREGATE_KEY]], [1, 2])

    def test_unreliable_or_duplicate_identity_resets_aggregate(self):
        for invalid in (replace(self.b, uuid=None), replace(self.b, uuid=self.a.uuid)):
            with self.subTest(uuid=invalid.uuid):
                history = HistoryBuffer()
                history.record(Snapshot(devices=[self.a, self.b]), 1)
                history.record(Snapshot(devices=[self.a, invalid]), 2)
                self.assertNotIn(AGGREGATE_KEY, history.snapshot(2))
                history.record(Snapshot(devices=[self.a, self.b]), 3)
                self.assertEqual(history.snapshot(3)[AGGREGATE_KEY], [HistoryPoint(3, 50, 50)])

    def test_no_selected_devices_has_no_aggregate(self):
        history = HistoryBuffer(gpu_indices={3})
        history.record(Snapshot(devices=[self.a, self.b]), 1)
        self.assertNotIn(AGGREGATE_KEY, history.snapshot(1))

    def test_missing_metric_and_failure_recovery_keep_holes(self):
        self.history.record(Snapshot(devices=[self.a, self.b]), 1)
        self.history.record(Snapshot(devices=[replace(self.a, gpu_utilization_percent=None), self.b]), 2)
        self.history.record(Snapshot(devices=[self.a, self.b], devices_stale=True), 3)
        self.history.record(Snapshot(devices=[self.a, self.b]), 4)
        self.assertEqual(self.history.snapshot(4)[AGGREGATE_KEY],
                         [HistoryPoint(1, 50, 50), HistoryPoint(2, None, 50), HistoryPoint(4, 50, 50)])


if __name__ == "__main__":
    unittest.main()
