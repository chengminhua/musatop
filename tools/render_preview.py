"""Render the actual TUI with deterministic synthetic data to a standalone SVG.

Run with the installed project Python: python tools/render_preview.py output.svg
No hardware is queried and no machine identity or real process enters the image.
"""

import argparse
import curses
from html import escape
from pathlib import Path
from unittest.mock import patch

from musatop.history import AGGREGATE_KEY, HOST_KEY, HistoryPoint, device_key
from musatop.models import Device, Host, Process, Snapshot
from musatop.tui import TerminalUI
from musatop.view import Options


NOW = 3600
COLORS = ["#46c46b", "#67ca65", "#86ce61", "#a6d15b", "#c8d457",
          "#e9c74f", "#efae49", "#f28e49", "#f56d53", "#f25460",
          "#87929e", "#52d6df", "#d68be0", "#ebc957", "#62d995"]


class Screen:
    encoding = "utf-8"

    def __init__(self, height, width):
        self.height, self.width = height, width
        self.cells = {}

    def getmaxyx(self):
        return self.height, self.width

    def addnstr(self, row, col, text, limit, attr=0):
        for offset, char in enumerate(text[:limit]):
            self.cells[row, col + offset] = char, attr

    def erase(self):
        self.cells.clear()

    def refresh(self):
        pass


class PreviewMonitor:
    def __init__(self):
        gib = 1024**3
        self.snapshot = Snapshot(
            sampled_at="2026-01-01T12:00:00+00:00",
            driver_version="3.3.8-server", gmi_version="2.3.3", musa_version="4.3.2",
            host=Host("demo-host", 18, 128 * gib, 512 * gib),
            devices=[Device(i, uuid=f"demo-{i}", name="X10000", gpu_utilization_percent=99,
                            memory_used_bytes=int(80 * gib * .69), memory_total_bytes=80 * gib,
                            temperature_c=61 + i % 3, power_draw_w=420 + i * 3,
                            power_limit_w=950) for i in range(8)],
            processes=[Process(i % 8, 12000 + i, username="demo", gpu_memory_bytes=18 * gib,
                               cpu_percent=42 + i, rss_bytes=4 * gib, running_seconds=7200,
                               command=f"python demo_job.py --worker {i}", status="ok")
                       for i in range(8)],
        )
        host, gpu = [], []
        for age in range(300):
            cpu = 18 + (65 if 60 <= age < 72 or 180 <= age < 188 else age % 13)
            util = 4 if age < 65 else 42 if age < 125 else 75 if age < 210 else 99
            memory = 3 if age < 65 else 26 if age < 125 else 52 if age < 210 else 69
            host.append(HistoryPoint(NOW - 299 + age, cpu if age < 299 else 18, 25))
            gpu.append(HistoryPoint(NOW - 299 + age, util, memory))
        self.history = {HOST_KEY: host, AGGREGATE_KEY: gpu}
        self.history.update({device_key(d): gpu for d in self.snapshot.devices})

    def latest_with_history(self):
        return self.snapshot, self.history


def preview(width=120, height=34):
    screen = Screen(height, width)
    ui = TerminalUI(screen, PreviewMonitor(), Options())
    # Encode color pairs in the same attribute bits used by ncurses. No real
    # terminal is initialized; Screen retains the rendered characters/attributes.
    ui.colors = [index << 8 for index in range(1, 11)]
    ui.unknown_color = 11 << 8
    ui.metric_colors = dict(cpu=12 << 8, ram=13 << 8, vram=14 << 8, util=15 << 8)
    with patch("musatop.tui.time.monotonic", return_value=NOW):
        ui.update()
        ui.render()
    return screen


def svg(screen):
    cell_width, cell_height, padding = 9, 19, 16
    width, height = screen.width * cell_width + padding * 2, screen.height * cell_height + padding * 2
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
             f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="title description">',
             '<title id="title">musatop four-panel trend preview</title>',
             '<desc id="description">Actual TUI rendered with synthetic GPU and host data. '
             'GPU utilization is 99 percent, GPU memory occupation is 69 percent.</desc>',
             f'<rect width="{width}" height="{height}" rx="8" fill="#111820"/>',
             '<g font-family="DejaVu Sans Mono,DejaVu Sans,monospace" font-size="15" xml:space="preserve">']
    for row in range(screen.height):
        col = 0
        while col < screen.width:
            char, attr = screen.cells.get((row, col), (" ", 0))
            end, chars = col + 1, [char]
            while end < screen.width:
                next_char, next_attr = screen.cells.get((row, end), (" ", 0))
                if next_attr != attr:
                    break
                chars.append(next_char)
                end += 1
            text = "".join(chars)
            pair = (attr & curses.A_COLOR) >> 8
            color = COLORS[pair - 1] if 0 < pair <= len(COLORS) else "#dce4ed"
            x, y = padding + col * cell_width, padding + row * cell_height
            if attr & curses.A_REVERSE:
                parts.append(f'<rect x="{x}" y="{y}" width="{len(text)*cell_width}" '
                             f'height="{cell_height}" fill="#dce4ed"/>')
                color = "#111820"
            if text.strip():
                style = ' font-weight="bold"' if attr & curses.A_BOLD else ''
                if attr & curses.A_DIM:
                    style += ' opacity="0.65"'
                parts.append(f'<text x="{x}" y="{y+15}" fill="{color}"{style} '
                             f'textLength="{len(text)*cell_width}" lengthAdjust="spacingAndGlyphs">'
                             f'{escape(text)}</text>')
            col = end
    parts.append('</g></svg>')
    return '\n'.join(parts) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--width", type=int, default=120)
    parser.add_argument("--height", type=int, default=34)
    args = parser.parse_args()
    if not 60 <= args.width <= 240 or not 18 <= args.height <= 80:
        parser.error("preview dimensions must be 60-240 columns by 18-80 rows")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(svg(preview(args.width, args.height)), encoding="utf-8")


if __name__ == "__main__":
    main()
