"""UI interaction and real pseudo-terminal cleanup without GPU hardware."""

import curses
import os
import select
import struct
import subprocess
import sys
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from musatop.models import Device, Host, Process, Snapshot
from musatop.tui import TerminalUI, run_tui
from musatop.view import Options


def sample(device_count=8, process_count=12):
    return Snapshot(
        driver_version="test-driver",
        gmi_version="test-gmi",
        musa_version="test-musa",
        host=Host(hostname="test-host", cpu_percent=30, memory_used_bytes=1024**3, memory_total_bytes=8 * 1024**3),
        devices=[
            Device(index=i, name=f"MTT-S5000-{i}", gpu_utilization_percent=25,
                   memory_used_bytes=2 * 1024**3, memory_total_bytes=80 * 1024**3,
                   temperature_c=40, power_draw_w=120, power_limit_w=300)
            for i in range(device_count)
        ],
        processes=[
            Process(device_index=i % device_count, pid=1000 + i, username="test-user",
                    command=f"python workload_{i}.py --long-argument=" + "x" * 150,
                    gpu_memory_bytes=(process_count - i) * 1024**2,
                    cpu_percent=i, rss_bytes=1024**2, create_time=100 + i,
                    running_seconds=120, status="ok")
            for i in range(process_count)
        ],
    )


class FakeMonitor:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.refreshes = 0
        self.started = self.stopped = False

    def latest(self):
        return self.snapshot

    def start(self):
        self.started = True

    def refresh(self):
        self.refreshes += 1

    def stop(self):
        self.stopped = True


class FakeScreen:
    def __init__(self, height=24, width=80):
        self.height, self.width = height, width
        self.lines = {}

    def getmaxyx(self):
        return self.height, self.width

    def addnstr(self, row, col, text, limit, attr=0):
        self.lines[row] = text[:limit]

    def erase(self):
        self.lines.clear()

    def refresh(self):
        pass

    def content(self):
        return "\n".join(self.lines[row] for row in sorted(self.lines))


class TerminalUITests(unittest.TestCase):
    def setUp(self):
        self.monitor = FakeMonitor(sample())
        self.screen = FakeScreen()
        self.options = Options()
        self.ui = TerminalUI(self.screen, self.monitor, self.options)
        self.ui.update()

    def test_eight_gpu_layout_and_process_scrolling(self):
        self.ui.render()
        output = self.screen.content()
        for i in range(8):
            self.assertIn(f"MTT-S5000-{i}", output)
        self.assertIn("1000", output)
        self.ui.handle_key(curses.KEY_END)
        self.ui.render()
        self.assertIn("1011", self.screen.content())
        self.assertGreater(self.ui.process_offset, 0)

    def test_gpu_paging_and_resize(self):
        self.monitor.snapshot = sample(device_count=30)
        self.ui.update()
        self.ui.render()
        self.ui.handle_key(curses.KEY_NPAGE)
        self.ui.render()
        self.assertIn("MTT-S5000-12", self.screen.content())
        self.assertNotIn("MTT-S5000-0 ", self.screen.content())
        self.screen.height, self.screen.width = 10, 40
        self.ui.handle_key(curses.KEY_RESIZE)
        self.ui.render()
        self.assertIn("Terminal too small", self.screen.content())
        self.screen.height, self.screen.width = 24, 80
        self.ui.render()
        self.assertIn("musatop", self.screen.content())

    def test_sort_search_user_compact_and_refresh(self):
        self.ui.handle_key("s")
        self.assertEqual(self.options.sort, "cpu")
        self.ui.handle_key("S")
        self.assertTrue(self.options.reverse)
        self.ui.handle_key("/")
        for char in "workload_9.py":
            self.ui.handle_key(char)
        self.ui.handle_key("\n")
        self.ui.update()
        self.assertEqual([p.pid for p in self.ui.processes], [1009])
        self.ui.handle_key("/")
        self.ui.handle_key("X")
        self.ui.handle_key("\x1b")
        self.assertEqual(self.options.search, "workload_9.py")
        self.ui.handle_key("u")
        self.ui.handle_key("c")
        self.ui.handle_key("r")
        self.assertTrue(self.options.current_user)
        self.assertTrue(self.options.compact)
        self.assertEqual(self.monitor.refreshes, 1)

    def test_details_preserve_full_command_and_scroll(self):
        self.ui.handle_key("\n")
        _, lines, _ = self.ui.modal_lines()
        self.assertEqual(lines[-1], self.monitor.snapshot.processes[0].command)
        self.ui.render()
        self.assertIn("PID: 1000", self.screen.content())
        self.ui.handle_key(curses.KEY_DOWN)
        self.ui.handle_key("q")
        self.assertIsNone(self.ui.modal)
        self.assertTrue(self.ui.running)

    @patch("musatop.tui.terminate_process")
    def test_confirmation_defaults_to_no(self, terminate):
        for key in ("\n", "n", "\x1b", "q"):
            self.ui.handle_key("k")
            self.assertEqual(self.ui.modal, "confirm")
            self.ui.handle_key(key)
            self.assertIsNone(self.ui.modal)
        terminate.assert_not_called()

    @patch("musatop.tui.terminate_process", return_value="SIGTERM sent")
    def test_confirmation_keeps_original_target_after_selection_and_refresh(self, terminate):
        self.ui.handle_key("k")
        target = self.ui.modal_process
        self.assertIsNot(target, self.monitor.snapshot.processes[0])
        self.ui.selected = 5
        self.monitor.snapshot = replace(self.monitor.snapshot, processes=list(reversed(self.monitor.snapshot.processes)))
        self.ui.handle_key("y")
        terminate.assert_called_once_with(target)
        self.assertEqual(target.pid, 1000)
        self.assertEqual(self.monitor.refreshes, 1)

    @patch("musatop.tui.terminate_process")
    def test_stale_exited_and_reused_identity_cancel_confirmation(self, terminate):
        for mutation in ("stale", "gone", "reused", "denied", "uuid_changed"):
            with self.subTest(mutation=mutation):
                self.monitor.snapshot = sample()
                self.ui.update()
                self.ui.selected = 0
                self.ui.handle_key("k")
                original = self.monitor.snapshot.processes[0]
                if mutation == "stale":
                    self.monitor.snapshot = replace(self.monitor.snapshot, processes_stale=True)
                elif mutation == "gone":
                    self.monitor.snapshot = replace(self.monitor.snapshot, processes=self.monitor.snapshot.processes[1:])
                else:
                    kwargs = {"create_time": 9999} if mutation == "reused" else {"status": "access_denied"} if mutation == "denied" else {"device_uuid": "new-device"}
                    self.monitor.snapshot = replace(self.monitor.snapshot, processes=[replace(original, **kwargs)] + self.monitor.snapshot.processes[1:])
                self.ui.handle_key("y")
                self.assertIsNone(self.ui.modal)
                self.assertIn("cancelled", self.ui.message)
        terminate.assert_not_called()

    @patch("musatop.tui.terminate_process")
    def test_unverified_process_cannot_open_confirmation(self, terminate):
        self.monitor.snapshot = replace(self.monitor.snapshot, processes=[replace(self.monitor.snapshot.processes[0], status="access_denied")])
        self.ui.update()
        self.ui.handle_key("k")
        self.assertIsNone(self.ui.modal)
        terminate.assert_not_called()

    @patch("musatop.tui.terminate_process")
    def test_ctrl_c_always_exits_without_signaling(self, terminate):
        self.ui.handle_key("k")
        self.ui.handle_key("\x03")
        self.assertFalse(self.ui.running)
        terminate.assert_not_called()

    def test_monitor_stops_even_when_curses_fails(self):
        with patch("musatop.tui.curses.wrapper", side_effect=RuntimeError("render failed")):
            with self.assertRaisesRegex(RuntimeError, "render failed"):
                run_tui(self.monitor, self.options)
        self.assertTrue(self.monitor.started)
        self.assertTrue(self.monitor.stopped)

    def test_empty_unknown_and_filtered_lists_have_distinct_messages(self):
        self.monitor.snapshot = Snapshot(devices_stale=True, processes_stale=True)
        self.ui.update()
        self.ui.render()
        self.assertIn("GPU data unavailable", self.screen.content())
        self.assertIn("Process data unavailable", self.screen.content())
        self.assertNotIn("No GPUs", self.screen.content())
        self.monitor.snapshot = Snapshot()
        self.ui.update()
        self.ui.render()
        self.assertIn("No GPUs found", self.screen.content())
        self.assertIn("No GPU processes running", self.screen.content())
        self.monitor.snapshot = sample()
        self.options.gpu = {999}
        self.ui.update()
        self.ui.render()
        self.assertIn("No GPUs match the selected filters", self.screen.content())
        self.assertIn("No GPU processes match the selected filters", self.screen.content())

    def test_collection_errors_override_temporary_messages_and_messages_expire(self):
        with patch("musatop.tui.time.monotonic", return_value=10):
            self.ui.handle_key("r")
            self.ui.render()
            self.assertIn("Refresh requested", self.screen.content())
            self.monitor.snapshot = replace(self.monitor.snapshot, errors=["devices: driver failed"])
            self.ui.update()
            self.ui.render()
            self.assertIn("devices: driver failed", self.screen.content())
            self.assertNotIn("Refresh requested", self.screen.content())
        self.monitor.snapshot = replace(self.monitor.snapshot, errors=[])
        with patch("musatop.tui.time.monotonic", return_value=14):
            self.ui.update()
            self.ui.render()
            self.assertNotIn("Refresh requested", self.screen.content())

    def test_missing_power_limit_note_yields_to_errors_and_is_explained_in_help(self):
        self.monitor.snapshot.devices[0].power_limit_w = None
        self.monitor.snapshot.devices[0].power_limit_reason = "Current power limit is not reported by GMI"
        self.ui.update()
        self.ui.render()
        self.assertIn("Power limit N/A:", self.screen.content())
        self.monitor.snapshot.errors = ["devices: driver failed"]
        self.ui.update()
        self.ui.render()
        self.assertIn("devices: driver failed", self.screen.content())
        self.assertNotIn("Power limit N/A:", self.screen.content())
        self.ui.handle_key("?")
        _, lines, _ = self.ui.modal_lines()
        self.assertIn("power_limit_reason", "\n".join(lines))


@unittest.skipUnless(sys.platform.startswith("linux"), "PTY smoke test requires Linux")
class PseudoTerminalTests(unittest.TestCase):
    def test_exception_restores_real_terminal_and_stops_monitor(self):
        import fcntl
        import pty
        import termios

        master, slave = pty.openpty()
        child = None
        output = bytearray()
        try:
            initial = termios.tcgetattr(slave)
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
            code = """
from musatop.tui import run_tui
from musatop.view import Options
class Monitor:
    stopped = False
    def start(self): pass
    def stop(self): self.stopped = True
    def refresh(self): pass
    def latest(self): raise RuntimeError('injected render-side failure')
monitor = Monitor()
try:
    run_tui(monitor, Options())
except RuntimeError:
    assert monitor.stopped
    print('EXCEPTION_CLEANED')
else:
    raise AssertionError('injected failure was not reached')
"""
            child = subprocess.Popen(
                [sys.executable, "-c", code], stdin=slave, stdout=slave, stderr=slave,
                cwd=Path(__file__).resolve().parents[1], env={**os.environ, "TERM": "xterm-256color"},
            )
            deadline = time.monotonic() + 5
            while b"EXCEPTION_CLEANED" not in output and time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.1)
                if ready:
                    output.extend(os.read(master, 65536))
            self.assertIn(b"EXCEPTION_CLEANED", output)
            self.assertEqual(child.wait(timeout=5), 0)
            self.assertEqual(termios.tcgetattr(slave), initial)
        finally:
            if child is not None and child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
            os.close(master)
            os.close(slave)

    def test_real_keys_resize_and_terminal_restoration(self):
        import fcntl
        import pty
        import termios

        master, slave = pty.openpty()
        child = None
        output = bytearray()
        try:
            initial = termios.tcgetattr(slave)
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
            code = """
from musatop.tui import run_tui
from musatop.view import Options
from musatop.models import Snapshot, Device, Process
class Monitor:
    def start(self): pass
    def stop(self): pass
    def refresh(self): pass
    def latest(self):
        return Snapshot(devices=[Device(index=i, name='MTT-PTY-'+str(i)) for i in range(8)],
            processes=[Process(device_index=0,pid=876543,username='pty-user',
                command='python pty-smoke.py',create_time=123,status='ok')])
run_tui(Monitor(), Options())
print('UI_DONE')
"""
            child = subprocess.Popen(
                [sys.executable, "-c", code], stdin=slave, stdout=slave, stderr=slave,
                cwd=Path(__file__).resolve().parents[1], env={**os.environ, "TERM": "xterm-256color"},
            )

            def read_until(marker, timeout=5):
                deadline = time.monotonic() + timeout
                while marker not in output and time.monotonic() < deadline:
                    ready, _, _ = select.select([master], [], [], 0.1)
                    if ready:
                        output.extend(os.read(master, 65536))
                    if child.poll() is not None:
                        break
                self.assertIn(marker, output, output[-2000:].decode(errors="replace"))

            read_until(b"MTT-PTY-7")
            os.write(master, b"?")
            read_until(b"next process sort field")
            os.write(master, b"\nsc/pty-smoke\n\n")
            read_until(b"Created:")
            os.write(master, b"\nk\n")
            read_until(b"Termination cancelled")
            # Exercise a resize while the event loop is active.
            # Resize below minimum and back while the event loop is active.
            import signal
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 10, 40, 0, 0))
            child.send_signal(signal.SIGWINCH)
            read_until(b"Terminal too small")
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
            child.send_signal(signal.SIGWINCH)
            os.write(master, b"q")
            read_until(b"UI_DONE")
            self.assertEqual(child.wait(timeout=5), 0)
            self.assertEqual(termios.tcgetattr(slave), initial)
        finally:
            if child is not None and child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
            os.close(master)
            os.close(slave)


if __name__ == "__main__":
    unittest.main()
