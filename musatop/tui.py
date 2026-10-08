"""Keyboard-driven curses UI; collection always runs outside the UI thread."""

from __future__ import annotations

import curses
import locale
import textwrap
import time
from dataclasses import replace

from musatop import __version__
from musatop.bars import bar_cells, percent_label, spark_cells, trend_values
from musatop.history import device_key, memory_percent
from musatop.models import Process, Snapshot
from musatop.processes import terminate_process
from musatop.view import (
    Options,
    filter_snapshot,
    fmt_bytes,
    fmt_duration,
    fmt_number,
    safe_text,
)


SORT_FIELDS = ("gpu_memory", "cpu", "rss", "pid", "user", "gpu")


def _identity(process: Process) -> tuple:
    return (
        process.device_index,
        process.device_uuid,
        process.pid,
        process.create_time,
    )


class TerminalUI:
    """Small UI state machine, also usable with a fake screen in tests."""

    def __init__(self, screen, monitor, options: Options):
        self.screen = screen
        self.monitor = monitor
        self.options = options
        self.snapshot: Snapshot | None = None
        self.visible: Snapshot | None = None
        self.selected = 0
        self.process_offset = 0
        self.device_offset = 0
        self.device_page_size = 1
        self.process_page_size = 1
        self.history = {}
        self.show_trend = True
        self.trend_identity = None
        encoding = getattr(screen, "encoding", None) or locale.getpreferredencoding(False)
        self.ascii = options.ascii or encoding.lower().replace("-", "") != "utf8"
        self.colors: list[int] = []
        self.unknown_color = 0
        self.modal: str | None = None
        self.modal_process: Process | None = None
        self.modal_offset = 0
        self.search_input: str | None = None
        self.message = ""
        self.running = True

    @property
    def message(self) -> str:
        return self._message if time.monotonic() < self._message_until else ""

    @message.setter
    def message(self, value: str) -> None:
        self._message = value
        self._message_until = time.monotonic() + 3.0 if value else 0.0

    @property
    def processes(self) -> list[Process]:
        return self.visible.processes if self.visible is not None else []

    def update(self) -> None:
        previous = self.current_process()
        selected_identity = _identity(previous) if previous is not None else None
        self.snapshot, self.history = self.monitor.latest_with_history()
        self.visible = (
            filter_snapshot(self.snapshot, self.options)
            if self.snapshot is not None
            else None
        )
        if selected_identity is not None:
            for index, process in enumerate(self.processes):
                if _identity(process) == selected_identity:
                    self.selected = index
                    break
        self.selected = min(self.selected, max(0, len(self.processes) - 1))
        self.current_trend_device()
        if self.modal == "confirm" and not self.confirmation_is_current():
            self.close_modal()
            self.message = "Termination cancelled: process changed or process data is stale."

    @staticmethod
    def trend_device_identity(device):
        # An index keeps selection usable without pretending it is a history key.
        return ("key", device_key(device)) if device_key(device) else ("index", device.index)

    def current_trend_device(self):
        devices = self.visible.devices if self.visible is not None else []
        for device in devices:
            if self.trend_device_identity(device) == self.trend_identity:
                return device
        device = devices[0] if devices else None
        self.trend_identity = self.trend_device_identity(device) if device is not None else None
        return device

    def cycle_trend_device(self, step: int) -> None:
        current = self.current_trend_device()
        if current is not None:
            devices = self.visible.devices
            index = next(i for i, device in enumerate(devices) if device is current)
            self.trend_identity = self.trend_device_identity(devices[(index + step) % len(devices)])

    def init_colors(self) -> None:
        """Color is optional; a plain terminal retains every metric and state."""
        self.colors = []
        self.unknown_color = 0
        if self.options.no_color:
            return
        try:
            if not curses.has_colors():
                return
            curses.start_color()
            background = curses.COLOR_BLACK
            try:
                curses.use_default_colors()
                background = -1
            except curses.error:
                pass
            if curses.COLORS >= 256 and curses.COLOR_PAIRS >= 12:
                foregrounds = (40, 76, 112, 148, 184, 220, 214, 208, 202, 196)
                unknown = 244
            elif curses.COLORS >= 8 and curses.COLOR_PAIRS >= 5:
                foregrounds = (curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_RED)
                unknown = curses.COLOR_WHITE
            else:
                return
            for pair, foreground in enumerate(foregrounds, 1):
                curses.init_pair(pair, foreground, background)
                self.colors.append(curses.color_pair(pair))
            pair = len(foregrounds) + 1
            curses.init_pair(pair, unknown, background)
            self.unknown_color = curses.color_pair(pair)
        except curses.error:
            self.colors = []
            self.unknown_color = 0

    def load_color(self, percentage: float) -> int:
        if not self.colors:
            return 0
        index = min(len(self.colors) - 1, max(0, int(percentage * len(self.colors) / 100)))
        return self.colors[index]

    def current_process(self) -> Process | None:
        return self.processes[self.selected] if self.processes else None

    def confirmation_is_current(self) -> bool:
        target = self.modal_process
        snapshot = self.snapshot
        if (
            target is None
            or snapshot is None
            or snapshot.processes_stale
            or target.create_time is None
            or target.status != "ok"
        ):
            return False
        return any(
            _identity(process) == _identity(target) and process.status == "ok"
            for process in snapshot.processes
        )

    def close_modal(self) -> None:
        self.modal = None
        self.modal_process = None
        self.modal_offset = 0

    def open_process_modal(self, kind: str) -> None:
        process = self.current_process()
        if process is None:
            self.message = "No process selected."
            return
        self.modal_process = replace(process)
        self.modal = kind
        self.modal_offset = 0
        if kind == "confirm" and not self.confirmation_is_current():
            self.close_modal()
            self.message = "Cannot terminate: stale or unverified process data."

    def handle_key(self, key) -> None:
        if key == "\x03":
            self.running = False
            return
        if key == curses.KEY_RESIZE:
            return
        if self.search_input is not None:
            if key in ("\n", "\r", curses.KEY_ENTER):
                self.options.search = self.search_input
                self.search_input = None
                self.selected = self.process_offset = 0
            elif key == "\x1b":
                self.search_input = None
            elif key in (curses.KEY_BACKSPACE, "\b", "\x7f"):
                self.search_input = self.search_input[:-1]
            elif isinstance(key, str) and key.isprintable():
                self.search_input += key
            return
        if self.modal is not None:
            if key in (curses.KEY_UP, curses.KEY_PPAGE):
                self.modal_offset = max(0, self.modal_offset - (1 if key == curses.KEY_UP else 5))
            elif key in (curses.KEY_DOWN, curses.KEY_NPAGE):
                self.modal_offset += 1 if key == curses.KEY_DOWN else 5
            elif self.modal == "confirm":
                if key in ("y", "Y"):
                    # Re-read the newest completed sample, not the selection after a refresh.
                    self.update()
                    if self.modal == "confirm" and self.confirmation_is_current():
                        target = self.modal_process
                        self.close_modal()
                        try:
                            self.message = terminate_process(target)
                        except (OSError, ValueError) as error:
                            self.message = f"Termination failed: {error}"
                        self.monitor.refresh()
                elif key in ("n", "N", "q", "Q", "\x1b", "\n", "\r", curses.KEY_ENTER):
                    self.close_modal()
                    self.message = "Termination cancelled."
            elif key in ("q", "Q", "\x1b", "\n", "\r", "?", curses.KEY_ENTER):
                self.close_modal()
            return
        if key in ("q", "Q"):
            self.running = False
        elif key in (curses.KEY_UP, curses.KEY_BTAB):
            self.selected = max(0, self.selected - 1)
        elif key in (curses.KEY_DOWN, "\t"):
            self.selected = min(max(0, len(self.processes) - 1), self.selected + 1)
        elif key == curses.KEY_HOME:
            self.selected = 0
        elif key == curses.KEY_END:
            self.selected = max(0, len(self.processes) - 1)
        elif key == curses.KEY_PPAGE:
            self.device_offset = max(0, self.device_offset - self.device_page_size)
        elif key == curses.KEY_NPAGE:
            self.device_offset += self.device_page_size
        elif key == "s":
            index = SORT_FIELDS.index(self.options.sort) if self.options.sort in SORT_FIELDS else -1
            self.options.sort = SORT_FIELDS[(index + 1) % len(SORT_FIELDS)]
        elif key == "S":
            self.options.reverse = not self.options.reverse
        elif key == "/":
            self.search_input = self.options.search
        elif key == "u":
            self.options.current_user = not self.options.current_user
        elif key == "c":
            self.options.compact = not self.options.compact
        elif key in ("g", "G"):
            self.cycle_trend_device(1 if key == "g" else -1)
        elif key == "h":
            self.show_trend = not self.show_trend
        elif key in ("r", "R", curses.KEY_F5):
            self.message = "Refresh requested."
            self.monitor.refresh()
        elif key == "?":
            self.modal = "help"
            self.modal_offset = 0
        elif key in ("\n", "\r", curses.KEY_ENTER):
            self.open_process_modal("details")
        elif key == "k":
            self.open_process_modal("confirm")

    def put(self, row: int, text: str, attr: int = 0, *, col: int = 0) -> None:
        height, width = self.screen.getmaxyx()
        if row < 0 or row >= height or col < 0 or col >= width - 1:
            return
        try:
            # Leaving the last column free avoids curses' bottom-right-cell error.
            self.screen.addnstr(row, col, safe_text(str(text)), width - col - 1, attr)
        except curses.error:
            pass  # A resize may occur between getmaxyx() and addnstr().

    def render(self) -> None:
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        if height < 12 or width < 60:
            self.put(0, "Terminal too small: resize to at least 60x12.")
            self.put(1, "Press q or Ctrl-C to quit.")
        elif self.modal is not None:
            self.render_modal(height, width)
        elif self.visible is None:
            self.put(0, f"musatop {__version__} | Waiting for the first sample...")
            self.put(height - 1, "q quit  r refresh  ? help")
        else:
            self.render_main(height, width)
        self.screen.refresh()

    def render_main(self, height: int, width: int) -> None:
        snapshot = self.visible
        host = snapshot.host
        self.put(
            0,
            f"musatop {__version__} | {host.hostname or 'N/A'} | "
            f"CPU {fmt_number(host.cpu_percent, '%')} | "
            f"RAM {fmt_bytes(host.memory_used_bytes)}/{fmt_bytes(host.memory_total_bytes)}",
            curses.A_BOLD,
        )
        self.put(
            1,
            f"Driver {snapshot.driver_version or 'N/A'}  "
            f"GMI {snapshot.gmi_version or 'N/A'}  MUSA {snapshot.musa_version or 'N/A'}",
        )
        stale = []
        if snapshot.devices_stale:
            stale.append("GPU DATA STALE")
        if snapshot.processes_stale:
            stale.append("PROCESS DATA STALE")
        devices = snapshot.devices
        trend_visible = self.show_trend and height >= 16
        # Eight GPUs + five processes + three trend rows fit at exactly 80x24.
        self.device_page_size = max(1, height - (15 if trend_visible else 12))
        self.device_offset = min(self.device_offset, max(0, len(devices) - self.device_page_size))
        stop = min(len(devices), self.device_offset + self.device_page_size)
        page = f" GPUs {self.device_offset + 1}-{stop}/{len(devices)}" if len(devices) > self.device_page_size else ""
        sample_state = " | ".join(stale) if stale else f"Sample {snapshot.sampled_at}"
        self.put(2, sample_state + page, curses.A_DIM if stale else 0)
        name_width = 12 if width >= 80 else 0
        # Reserve mixed units too ("1023MiB/80.0GiB"), so normal load changes
        # do not shift the fixed percentage columns as usage crosses one GiB.
        memory_width = max([15] + [len(self.device_memory(device)) for device in devices])
        bar_width = max(1, (width - 1 - 44 - (memory_width - 13)
                            - (name_width + 1 if name_width else 0)) // 2)
        if width < 80:
            bar_width = min(bar_width, 8)
        prefix = "GPU " + (f"{'NAME':<{name_width}} " if name_width else "")
        self.put(3, prefix + f"{'UTIL':<{bar_width + 5}} {'VRAM':<{bar_width + 5}} "
                 f"{'USED/TOTAL':>{memory_width}} {'TEMP':>4} {'POWER W':>9}", curses.A_BOLD)
        row = 4
        for device in devices[self.device_offset:stop]:
            self.render_device(row, device, name_width, bar_width, memory_width, snapshot.devices_stale)
            row += 1
        if not devices:
            if not self.snapshot.devices and snapshot.devices_stale:
                empty_devices = "GPU data unavailable."
            elif not self.snapshot.devices:
                empty_devices = "No GPUs found."
            else:
                empty_devices = "No GPUs match the selected filters."
            self.put(row, empty_devices)
            row += 1
        filters = []
        if self.options.current_user:
            filters.append("current user")
        if self.options.search:
            filters.append(f"search={self.options.search}")
        self.put(
            row,
            f"Processes {len(self.processes)} | sort={self.options.sort}"
            f"{' reversed' if self.options.reverse else ''}"
            f"{(' | ' + ', '.join(filters)) if filters else ''}",
            curses.A_BOLD,
        )
        row += 1
        compact = self.options.compact or width < 80
        if compact:
            header = " GPU     PID USER        GPU MEM COMMAND"
        else:
            header = " GPU     PID USER        GPU MEM   CPU%      RSS     TIME COMMAND"
        self.put(row, header, curses.A_UNDERLINE)
        row += 1
        self.process_page_size = max(1, height - row - 2 - (3 if trend_visible else 0))
        if self.selected < self.process_offset:
            self.process_offset = self.selected
        if self.selected >= self.process_offset + self.process_page_size:
            self.process_offset = self.selected - self.process_page_size + 1
        self.process_offset = min(self.process_offset, max(0, len(self.processes) - self.process_page_size))
        if not self.processes:
            if not self.snapshot.processes and snapshot.processes_stale:
                empty_processes = "Process data unavailable."
            elif not self.snapshot.processes:
                empty_processes = "No GPU processes running."
            else:
                empty_processes = "No GPU processes match the selected filters."
            self.put(row, empty_processes)
        for index in range(self.process_offset, min(len(self.processes), self.process_offset + self.process_page_size)):
            process = self.processes[index]
            line = (
                f" {process.device_index:>3} {process.pid:>7} "
                f"{safe_text(process.username or 'N/A')[:10]:10} "
                f"{fmt_bytes(process.gpu_memory_bytes):>9} "
            )
            if not compact:
                line += (
                    f"{fmt_number(process.cpu_percent):>6} "
                    f"{fmt_bytes(process.rss_bytes):>8} "
                    f"{fmt_duration(process.running_seconds):>8} "
                )
            line += process.command or "N/A"
            self.put(row + index - self.process_offset, line, curses.A_REVERSE if index == self.selected else 0)
        if trend_visible:
            self.render_trend(height - 5, width)
        note = ""
        if any(device.power_limit_reason for device in snapshot.devices):
            note = "Power limit N/A: GMI does not report a usable current limit (? for help)."
        elif snapshot.musa_version is None and snapshot.musa_version_reason:
            note = f"MUSA Toolkit: {snapshot.musa_version_reason}"
        status = "; ".join(snapshot.errors) if snapshot.errors else (self.message or note)
        if self.search_input is not None:
            status = f"/{self.search_input}  [Enter apply, Esc cancel]"
        self.put(height - 2, status)
        if width >= 80:
            footer = "q quit ? help g/G GPU h trend s/S sort / find r refresh k term Enter info"
        else:
            footer = "q quit ? help g/G GPU h trend / find k term Enter info"
        self.put(height - 1, footer)

    def render_bar(self, row: int, col: int, value: float | None, width: int, stale: bool) -> None:
        cells = bar_cells(value, width, ascii=self.ascii)
        dim = curses.A_DIM if stale else 0
        unknown = percent_label(value) == " N/A"
        for index, character in enumerate(cells):
            color = self.unknown_color if unknown else self.load_color((index + 1) * 100 / width)
            self.put(row, character, color | dim | (curses.A_DIM if unknown else 0), col=col + index)
        self.put(row, " " + percent_label(value), dim, col=col + width)

    @staticmethod
    def device_memory(device) -> str:
        used, total = fmt_bytes(device.memory_used_bytes), fmt_bytes(device.memory_total_bytes)
        for suffix in ("GiB", "MiB"):
            if used.endswith(suffix) and total.endswith(suffix):
                used = used[:-len(suffix)]
                break
        return f"{used}/{total}"

    def render_device(self, row, device, name_width: int, bar_width: int, memory_width: int, stale: bool) -> None:
        attr = curses.A_DIM if stale else 0
        prefix = f"{device.index:>3} "
        if name_width:
            name = safe_text(device.name or "N/A")
            prefix += f"{name[:name_width]:<{name_width}} "
        self.put(row, prefix, attr)
        col = len(prefix)
        self.render_bar(row, col, device.gpu_utilization_percent, bar_width, stale)
        col += bar_width + 6
        self.render_bar(row, col, memory_percent(device), bar_width, stale)
        col += bar_width + 6
        memory = self.device_memory(device)
        power = f"{fmt_number(device.power_draw_w)}/{fmt_number(device.power_limit_w)}"
        self.put(row, f"{memory:>{memory_width}} {fmt_number(device.temperature_c, 'C'):>4} {power:>9}", attr, col=col)

    def render_trend(self, row: int, width: int) -> None:
        device = self.current_trend_device()
        stale = self.visible.devices_stale
        state = " STALE" if stale else ""
        target = f"GPU {device.index}" if device is not None else "no visible GPU"
        self.put(row, f"Trend {target}{state} | 5 min (-5m -> now) | fixed 0-100%", curses.A_BOLD)
        key = device_key(device) if device is not None else None
        points = self.history.get(key, []) if key else []
        current = (device.gpu_utilization_percent, memory_percent(device)) if device is not None else (None, None)
        now = int(time.monotonic())
        curve_width = min(300, max(1, width - 14))
        for offset, (label, metric, value) in enumerate(zip(
            ("UTIL", "VRAM"), ("util_percent", "memory_percent"), current,
        ), 1):
            values = trend_values(points, metric, curve_width, now)
            curve = spark_cells(values, ascii=self.ascii)
            attr = curses.A_DIM if stale else 0
            self.put(row + offset, f"{label} |", attr)
            for index, (character, point_value) in enumerate(zip(curve, values)):
                color = self.load_color(point_value) if point_value is not None else 0
                self.put(row + offset, character, attr | color, col=6 + index)
            self.put(row + offset, "| " + percent_label(value), attr, col=6 + curve_width)

    def modal_lines(self) -> tuple[str, list[str], str]:
        if self.modal == "help":
            return "musatop help", [
                "Up / Down / Tab: select a process; Home / End: first / last process.",
                "PgUp / PgDn: scroll GPU pages when GPUs exceed the available rows.",
                "g / G: next / previous trend GPU; independent of process selection.",
                "h: show / hide the three-row trend panel (auto-hidden below 16 rows).",
                "UTIL and VRAM bars use a fixed 0-100% scale; VRAM is used / total bytes.",
                "Trends show 5 minutes (300 seconds), oldest left and newest right.",
                "Each second and each display column show the observed peak, not an average.",
                "Missing / failed samples leave gaps; zero has a baseline. STALE data is dim.",
                "--ascii uses ASCII bars / curves; --no-color disables colors (TUI only).",
                "s: next process sort field; S: reverse sort order.",
                "/: edit command/PID/user search; Enter applies; Esc cancels.",
                "u: toggle current-user filter; c: toggle compact process rows.",
                "Enter: process details, including its complete command.",
                "k: request SIGTERM for the selected verified process; y confirms.",
                "Process termination defaults to NO. Stale data disables termination.",
                "r / F5: request a fresh sample. Collection runs in the background.",
                "q: quit; Ctrl-C: quit from any screen, never signal a process.",
                "GPU and process values may be unavailable; N/A means unknown.",
                "Power limit N/A: GMI did not report a usable current limit; power draw is separate.",
                "musatop does not substitute a default cap, another GPU's cap, or zero.",
                "--json includes power_limit_reason and Toolkit version source/reason.",
            ], "Enter / Esc / q return | Up / Down scroll"
        process = self.modal_process
        lines = [
            f"PID: {process.pid}",
            f"User: {process.username or 'N/A'}",
            f"GPU: {process.device_index}  UUID: {process.device_uuid or 'N/A'}",
            f"Status: {process.status}  Created: {process.create_time}",
            f"GPU memory: {fmt_bytes(process.gpu_memory_bytes)}",
            f"CPU: {fmt_number(process.cpu_percent, '%')}  RSS: {fmt_bytes(process.rss_bytes)}",
            f"Running: {fmt_duration(process.running_seconds)}",
            "Full command:",
            process.command or "N/A",
        ]
        if self.modal == "confirm":
            return "Terminate this process with SIGTERM?", lines, "y confirm | n / Enter / Esc cancel (default: NO) | Up/Down scroll"
        return "Process details", lines, "Enter / Esc / q return | Up / Down scroll"

    def render_modal(self, height: int, width: int) -> None:
        title, source_lines, footer = self.modal_lines()
        lines = []
        for line in source_lines:
            lines.extend(textwrap.wrap(safe_text(line), width=max(1, width - 2), replace_whitespace=False) or [""])
        page_size = height - 3
        self.modal_offset = min(self.modal_offset, max(0, len(lines) - page_size))
        self.put(0, title, curses.A_BOLD)
        for row, line in enumerate(lines[self.modal_offset:self.modal_offset + page_size], 1):
            self.put(row, line)
        self.put(height - 2, f"Lines {self.modal_offset + 1}-{min(len(lines), self.modal_offset + page_size)}/{len(lines)}")
        self.put(height - 1, footer)

    def loop(self) -> int:
        self.init_colors()
        self.screen.keypad(True)
        self.screen.timeout(100)
        if hasattr(curses, "set_escdelay"):
            curses.set_escdelay(25)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        while self.running:
            self.update()
            self.render()
            try:
                key = self.screen.get_wch()
            except curses.error:
                continue
            self.handle_key(key)
        return 0


def run_tui(monitor, options: Options) -> int:
    """Run curses with bounded input waits and unconditional terminal restoration."""
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    monitor.start()
    try:
        return curses.wrapper(lambda screen: TerminalUI(screen, monitor, options).loop())
    except KeyboardInterrupt:
        return 0
    finally:
        monitor.stop()
