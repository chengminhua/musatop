import errno
import os
import signal
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import psutil

from musatop.models import Process
from musatop.processes import ProcessEnricher, terminate_process


def fake_process(created=100.0, username="tester", command=None, cpu=12.5):
    process = MagicMock()
    process.create_time.return_value = created
    process.is_running.return_value = True
    process.username.return_value = username
    process.cmdline.return_value = command if command is not None else ["worker", "--test"]
    process.name.return_value = "worker"
    process.memory_info.return_value = SimpleNamespace(rss=4096)
    process.cpu_percent.return_value = cpu
    return process


class EnrichmentTests(unittest.TestCase):
    def test_same_pid_on_two_devices_is_sampled_once(self):
        raw = [Process(0, 42, gpu_memory_bytes=1024), Process(1, 42, gpu_memory_bytes=2048)]
        process = fake_process()
        with patch("musatop.processes.psutil.Process", return_value=process) as constructor, \
                patch("musatop.processes.time.time", return_value=125):
            rows = ProcessEnricher().enrich(raw)
        constructor.assert_called_once_with(42)
        process.cpu_percent.assert_called_once_with(interval=None)
        self.assertEqual([row.gpu_memory_bytes for row in rows], [1024, 2048])
        self.assertEqual([row.device_index for row in rows], [0, 1])
        self.assertTrue(all(row.cpu_percent is None and row.status == "ok" for row in rows))
        self.assertTrue(all(row.running_seconds == 25 and row.command == "worker --test" for row in rows))
        self.assertIsNone(raw[0].username)

    def test_cpu_unknown_until_same_process_has_two_samples(self):
        enricher = ProcessEnricher()
        process, fresh_probe = fake_process(cpu=0.0), fake_process()
        with patch("musatop.processes.psutil.Process", side_effect=[process, fresh_probe]):
            first = enricher.enrich([Process(0, 42)])[0]
            second = enricher.enrich([Process(0, 42)])[0]
        self.assertIsNone(first.cpu_percent)
        self.assertEqual(second.cpu_percent, 0.0)
        self.assertEqual(process.cpu_percent.call_count, 2)
        fresh_probe.cpu_percent.assert_not_called()

    def test_reused_pid_restarts_cpu_sampling_and_discards_old_details(self):
        enricher = ProcessEnricher()
        old = fake_process(created=100, username="old", command=["old"])
        new = fake_process(created=200, username="new", command=["new"])
        with patch("musatop.processes.psutil.Process", side_effect=[old, new]):
            enricher.enrich([Process(0, 42)])
            result = enricher.enrich([Process(0, 42)])[0]
        self.assertEqual(result.create_time, 200)
        self.assertEqual(result.username, "new")
        self.assertEqual(result.command, "new")
        self.assertIsNone(result.cpu_percent)
        self.assertEqual(old.cpu_percent.call_count, 1)

    def test_removed_pid_is_not_retained_for_later_sampling(self):
        enricher = ProcessEnricher()
        old, new = fake_process(), fake_process()
        with patch("musatop.processes.psutil.Process", side_effect=[old, new]):
            enricher.enrich([Process(0, 42)])
            self.assertEqual(enricher.enrich([]), [])
            row = enricher.enrich([Process(0, 42)])[0]
        self.assertIsNone(row.cpu_percent)
        new.cpu_percent.assert_called_once()

    def test_process_exit_and_zombie_are_nonfatal(self):
        for error in (psutil.NoSuchProcess(42), psutil.ZombieProcess(42)):
            with self.subTest(error=type(error).__name__):
                with patch("musatop.processes.psutil.Process", side_effect=error):
                    row = ProcessEnricher().enrich([Process(0, 42)])[0]
                self.assertEqual(row.status, "exited")
                self.assertIsNone(row.create_time)

    def test_exit_during_detail_sampling_is_reported(self):
        process = fake_process()
        process.cmdline.side_effect = psutil.NoSuchProcess(42)
        with patch("musatop.processes.psutil.Process", return_value=process):
            row = ProcessEnricher().enrich([Process(0, 42)])[0]
        self.assertEqual(row.status, "exited")
        self.assertIsNone(row.cpu_percent)

    def test_access_denied_does_not_fabricate_zero_metrics(self):
        process = fake_process()
        process.username.side_effect = psutil.AccessDenied(42)
        with patch("musatop.processes.psutil.Process", return_value=process):
            row = ProcessEnricher().enrich([Process(0, 42)])[0]
        self.assertEqual(row.status, "access_denied")
        self.assertIsNone(row.username)
        self.assertIsNone(row.cpu_percent)
        self.assertIsNone(row.rss_bytes)

    def test_not_running_and_empty_cmdline(self):
        process = fake_process(command=[])
        with patch("musatop.processes.psutil.Process", return_value=process):
            self.assertEqual(ProcessEnricher().enrich([Process(0, 42)])[0].command, "worker")
            process.is_running.return_value = False
            self.assertEqual(ProcessEnricher().enrich([Process(0, 42)])[0].status, "exited")

    def test_host_cpu_is_unknown_only_on_first_sample(self):
        memory = SimpleNamespace(used=1024, total=8192)
        with patch("musatop.processes.psutil.virtual_memory", return_value=memory), \
                patch("musatop.processes.psutil.cpu_percent", side_effect=[0.0, 23.0]), \
                patch("musatop.processes.socket.gethostname", return_value="fixture-host"):
            enricher = ProcessEnricher()
            self.assertIsNone(enricher.host().cpu_percent)
            host = enricher.host()
        self.assertEqual(host.cpu_percent, 23.0)
        self.assertEqual((host.hostname, host.memory_used_bytes, host.memory_total_bytes),
                         ("fixture-host", 1024, 8192))


class TerminationTests(unittest.TestCase):
    def row(self, **kwargs):
        return Process(0, 424242, create_time=100.0, status="ok", **kwargs)

    def test_protected_pids_are_refused_without_os_access(self):
        with patch("musatop.processes.psutil.Process") as constructor:
            for pid in (-1, 0, 1, os.getpid()):
                with self.subTest(pid=pid):
                    result = terminate_process(Process(0, pid, create_time=100, status="ok"))
                    self.assertIn("protected", result)
            constructor.assert_not_called()

    def test_unknown_or_inaccessible_identity_is_refused(self):
        for row in (Process(0, 424242), Process(0, 424242, status="ok"),
                    Process(0, 424242, create_time=100, status="access_denied"),
                    Process(0, 424242, create_time=100, status="exited")):
            with self.subTest(row=row), patch("musatop.processes.psutil.Process") as constructor:
                self.assertIn("not verified", terminate_process(row))
                constructor.assert_not_called()

    def test_pid_reuse_or_exit_refuses_signal_and_closes_pidfd(self):
        for created, running in ((200.0, True), (100.0, False)):
            target = fake_process(created=created)
            target.is_running.return_value = running
            with self.subTest(created=created, running=running), \
                    patch("musatop.processes.os.pidfd_open", return_value=19, create=True), \
                    patch("musatop.processes.signal.pidfd_send_signal", create=True) as send, \
                    patch("musatop.processes.os.close") as close, \
                    patch("musatop.processes.psutil.Process", return_value=target):
                self.assertIn("reused or", terminate_process(self.row()))
                send.assert_not_called()
                target.terminate.assert_not_called()
                close.assert_called_once_with(19)

    def test_verified_pidfd_sends_sigterm_and_closes(self):
        target = fake_process()
        with patch("musatop.processes.os.pidfd_open", return_value=19, create=True), \
                patch("musatop.processes.signal.pidfd_send_signal", create=True) as send, \
                patch("musatop.processes.os.close") as close, \
                patch("musatop.processes.psutil.Process", return_value=target):
            self.assertEqual(terminate_process(self.row()), "SIGTERM sent to PID 424242.")
            send.assert_called_once_with(19, signal.SIGTERM)
            close.assert_called_once_with(19)
            target.terminate.assert_not_called()

    def test_pidfd_open_permission_failure_does_not_fall_back(self):
        with patch("musatop.processes.os.pidfd_open", side_effect=PermissionError(errno.EPERM, "denied"), create=True), \
                patch("musatop.processes.signal.pidfd_send_signal", create=True), \
                patch("musatop.processes.psutil.Process") as constructor:
            self.assertIn("Permission denied", terminate_process(self.row()))
            constructor.assert_not_called()

    def test_pidfd_signal_permission_failure_closes_descriptor(self):
        with patch("musatop.processes.os.pidfd_open", return_value=19, create=True), \
                patch("musatop.processes.signal.pidfd_send_signal", side_effect=PermissionError(), create=True), \
                patch("musatop.processes.os.close") as close, \
                patch("musatop.processes.psutil.Process", return_value=fake_process()):
            self.assertIn("Permission denied", terminate_process(self.row()))
            close.assert_called_once_with(19)

    def test_unsupported_pidfd_kernel_uses_psutil(self):
        for code in (errno.ENOSYS, errno.EINVAL):
            target = fake_process()
            with self.subTest(code=code), \
                    patch("musatop.processes.os.pidfd_open", side_effect=OSError(code, "unsupported"), create=True), \
                    patch("musatop.processes.signal.pidfd_send_signal", create=True) as send, \
                    patch("musatop.processes.psutil.Process", return_value=target):
                self.assertIn("SIGTERM sent", terminate_process(self.row()))
                target.terminate.assert_called_once_with()
                send.assert_not_called()

    def test_vanished_process_is_reported(self):
        with patch("musatop.processes.os.pidfd_open", side_effect=ProcessLookupError(), create=True), \
                patch("musatop.processes.signal.pidfd_send_signal", create=True):
            self.assertIn("already exited", terminate_process(self.row()))

    def test_can_terminate_only_the_real_child_started_by_this_test(self):
        child = subprocess.Popen(["sleep", "30"])
        try:
            row = Process(0, child.pid, create_time=psutil.Process(child.pid).create_time(), status="ok")
            self.assertIn("SIGTERM sent", terminate_process(row))
            self.assertEqual(child.wait(timeout=5), -signal.SIGTERM)
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
