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
from musatop.history import HistoryPoint, device_key
from musatop.tui import TerminalUI, run_tui
from musatop.view import Options


def sample(device_count=8, process_count=12):
    return Snapshot(
        driver_version="test-driver",
        gmi_version="test-gmi",
        musa_version="test-musa",
        host=Host(hostname="test-host", cpu_percent=30, memory_used_bytes=1024**3, memory_total_bytes=8 * 1024**3),
        devices=[
            Device(index=i, uuid=f"test-gpu-{i}", name=f"MTT-S5000-{i}", gpu_utilization_percent=25,
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
        self.history = {}

    def latest(self):
        return self.snapshot

    def latest_with_history(self):
        return self.snapshot, self.history

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
        self.attributes = {}

    def getmaxyx(self):
        return self.height, self.width

    def addnstr(self, row, col, text, limit, attr=0):
        previous = self.lines.get(row, "").ljust(col)
        value = text[:limit]
        self.lines[row] = previous[:col] + value + previous[col + len(value):]
        for index in range(col, col + len(value)):
            self.attributes[(row, index)] = attr

    def erase(self):
        self.lines.clear()
        self.attributes.clear()

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
        self.assertEqual(self.ui.process_page_size, 5)
        for pid in range(1000, 1005):
            self.assertIn(str(pid), output)
        self.assertNotIn("1005", output)
        self.assertIn("Trend GPU 0", self.screen.lines[19])
        self.assertIn("5 min", output)
        self.assertIn("UTIL |", self.screen.lines[20])
        self.assertIn("VRAM |", self.screen.lines[21])
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
        self.assertIn("MTT-S5000-9", self.screen.content())
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

    def test_trend_gpu_selection_is_independent_and_survives_device_reordering(self):
        self.ui.handle_key(curses.KEY_DOWN)
        self.ui.handle_key("g")
        self.assertEqual(self.ui.selected, 1)
        self.assertEqual(self.ui.current_trend_device().index, 1)
        self.monitor.snapshot.devices.reverse()
        self.ui.update()
        self.assertEqual(self.ui.current_trend_device().index, 1)
        self.ui.handle_key("G")
        self.assertEqual(self.ui.current_trend_device().index, 2)
        self.options.gpu = {5, 6}
        self.ui.update()
        self.assertEqual(self.ui.current_trend_device().index, 6)
        self.options.gpu = {999}
        self.ui.update()
        self.ui.render()
        self.assertIsNone(self.ui.current_trend_device())
        self.assertIn("Trend no visible GPU", self.screen.content())

    def test_trend_switch_wraps_and_process_filter_does_not_change_gpu(self):
        self.ui.handle_key("G")
        self.assertEqual(self.ui.current_trend_device().index, 7)
        self.ui.handle_key("g")
        self.assertEqual(self.ui.current_trend_device().index, 0)
        self.options.search = "workload_9.py"
        self.ui.update()
        self.assertEqual(self.ui.current_trend_device().index, 0)
        self.assertEqual(self.ui.current_process().pid, 1009)

    def test_trend_toggle_reclaims_rows_and_resize_preserves_preference(self):
        self.ui.render()
        self.ui.handle_key("h")
        self.ui.render()
        self.assertNotIn("Trend GPU", self.screen.content())
        self.assertEqual(self.ui.process_page_size, 8)
        self.screen.height = 15
        self.ui.render()
        self.screen.height = 24
        self.ui.render()
        self.assertFalse(self.ui.show_trend)
        self.assertNotIn("Trend GPU", self.screen.content())
        self.ui.handle_key("h")
        self.screen.height = 15
        self.ui.render()
        self.assertTrue(self.ui.show_trend)
        self.assertNotIn("Trend GPU", self.screen.content())
        self.screen.height = 24
        self.ui.render()
        self.assertIn("Trend GPU", self.screen.content())

    def test_narrow_layout_keeps_both_metrics_temperature_power_and_details(self):
        for width in (60, 79):
            with self.subTest(width=width):
                self.screen.width = width
                self.ui.render()
                output = self.screen.content()
                self.assertNotIn("MTT-S5000", output)
                self.assertIn("25%", self.screen.lines[4])
                self.assertIn("2.0/80.0GiB", self.screen.lines[4])
                self.assertIn("40C", self.screen.lines[4])
                self.assertIn("120/300", self.screen.lines[4])
                self.assertIn("workload_0", self.screen.lines[14])
                self.assertIn("k term Enter info", self.screen.lines[23])
                self.assertEqual(self.ui.process_page_size, 5)
        self.assertFalse(self.options.compact)

    def test_unknown_values_differ_from_zero_and_stale_rows_are_dim(self):
        self.monitor.snapshot.devices[0].gpu_utilization_percent = None
        self.monitor.snapshot.devices[1].gpu_utilization_percent = 0
        self.monitor.snapshot.devices[0].memory_total_bytes = 0
        self.ui.ascii = False
        self.ui.update()
        self.ui.render()
        self.assertIn("░", self.screen.lines[4])
        self.assertIn("N/A", self.screen.lines[4])
        self.assertIn("  0%", self.screen.lines[5])
        self.assertNotIn("░", self.screen.lines[5])
        self.monitor.snapshot.devices_stale = True
        self.ui.update()
        self.ui.render()
        self.assertIn("STALE", self.screen.lines[2])
        self.assertIn("STALE", self.screen.lines[19])
        for (row, _), attr in self.screen.attributes.items():
            if 4 <= row <= 11 or row in (20, 21):
                self.assertTrue(attr & curses.A_DIM)

    def test_mixed_memory_units_never_truncate_temperature_or_power_limit(self):
        for width in (60, 79, 80, 140):
            percent_columns = None
            for mib in (0, 1, 900, 1023, 1024, 81920):
                with self.subTest(width=width, used_mib=mib):
                    self.screen.width = width
                    device = self.monitor.snapshot.devices[0]
                    device.memory_used_bytes = mib * 1024**2
                    device.power_draw_w = device.power_limit_w = 950
                    self.ui.update()
                    self.ui.render()
                    line = self.screen.lines[4]
                    self.assertIn(self.ui.device_memory(device), line)
                    self.assertIn("40C", line)
                    self.assertTrue(line.endswith("950/950"), line)
                    self.assertLessEqual(len(line), width - 1)
                    columns = [i for i, value in enumerate(line) if value == "%"]
                    if percent_columns is None:
                        percent_columns = columns
                    self.assertEqual(columns, percent_columns)

    def test_history_uses_uuid_and_retains_gaps_on_fixed_five_minute_scale(self):
        key = device_key(self.monitor.snapshot.devices[0])
        self.monitor.history[key] = [HistoryPoint(701, 0, 10), HistoryPoint(1000, 100, 50)]
        self.ui.ascii = False
        with patch("musatop.tui.time.monotonic", return_value=1000):
            self.ui.update()
            self.ui.render()
        self.assertTrue(self.screen.lines[20].startswith("UTIL |▁"))
        self.assertIn(" " * 64 + "█|", self.screen.lines[20])
        self.assertIn(" 25%", self.screen.lines[20])  # current reading, not the bucket peak
        self.assertIn("fixed 0-100%", self.screen.lines[19])
        self.ui.handle_key("g")
        self.ui.render()
        self.assertIn("UTIL |" + " " * 66 + "|", self.screen.lines[20])

    def test_ascii_can_be_forced_and_non_utf8_terminal_falls_back(self):
        with patch("musatop.tui.locale.getpreferredencoding", return_value="ASCII"):
            ui = TerminalUI(self.screen, self.monitor, Options())
            self.assertTrue(ui.ascii)
        with patch("musatop.tui.locale.getpreferredencoding", return_value="UTF-8"):
            ui = TerminalUI(self.screen, self.monitor, Options(ascii=True))
            self.assertTrue(ui.ascii)
            ui.update()
            ui.render()
            self.assertIn("#", self.screen.lines[4])
            self.assertTrue(self.screen.content().isascii())
            ui = TerminalUI(self.screen, self.monitor, Options())
            self.assertFalse(ui.ascii)

    def test_color_disabled_and_terminal_palette_fallback(self):
        ui = TerminalUI(self.screen, self.monitor, Options(no_color=True))
        with patch("musatop.tui.curses.has_colors") as has_colors:
            ui.init_colors()
        has_colors.assert_not_called()
        self.assertEqual(ui.load_color(100), 0)
        for colors, pairs, expected in ((256, 256, 10), (8, 64, 3), (0, 0, 0)):
            with self.subTest(colors=colors), patch("musatop.tui.curses.has_colors", return_value=True), \
                patch("musatop.tui.curses.start_color"), patch("musatop.tui.curses.use_default_colors"), \
                patch("musatop.tui.curses.init_pair") as init_pair, \
                patch("musatop.tui.curses.color_pair", side_effect=lambda i: i << 8), \
                patch("musatop.tui.curses.COLORS", colors, create=True), \
                patch("musatop.tui.curses.COLOR_PAIRS", pairs, create=True):
                self.ui.init_colors()
                self.assertEqual(len(self.ui.colors), expected)
                self.assertEqual(init_pair.call_count, expected + 1 if expected else 0)
                if expected:
                    self.assertNotEqual(self.ui.load_color(0), self.ui.load_color(100))

    def test_gradient_bar_and_fixed_percent_column(self):
        self.ui.colors = [256, 512, 768]
        self.ui.ascii = False
        self.ui.render_bar(4, 0, 100, 9, False)
        self.assertEqual(self.screen.lines[4], "█████████ 100%")
        self.assertNotEqual(self.screen.attributes[(4, 0)], self.screen.attributes[(4, 8)])
        self.screen.erase()
        self.ui.render_bar(4, 0, 0, 9, False)
        self.assertEqual(self.screen.lines[4], "            0%")

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
    def latest_with_history(self): raise RuntimeError('injected render-side failure')
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
import musatop.tui as tui
from musatop.view import Options
from musatop.models import Snapshot, Device, Process
controls = []
class RecordingUI(tui.TerminalUI):
    def handle_key(self, key):
        super().handle_key(key)
        if key in ('g', 'G', 'h'):
            controls.append((key, self.current_trend_device().index, self.show_trend))
tui.TerminalUI = RecordingUI
class Monitor:
    def start(self): pass
    def stop(self): pass
    def refresh(self): pass
    def latest_with_history(self):
        return Snapshot(devices=[Device(index=i, uuid='pty-'+str(i), name='MTT-PTY-'+str(i)) for i in range(8)],
            processes=[Process(device_index=0,pid=876543,username='pty-user',
                command='python pty-smoke.py',create_time=123,status='ok')]), {}
tui.run_tui(Monitor(), Options())
assert controls == [('g', 1, True), ('G', 0, True), ('h', 0, False), ('h', 0, True)], controls
print('CONTROLS_OK')
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
            read_until(b"5 min")
            os.write(master, b"gGhh")
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
            self.assertIn(b"CONTROLS_OK", output)
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
