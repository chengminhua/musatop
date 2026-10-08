"""Formatting, filtering and ordering independent of terminal state."""

import getpass
import unicodedata
from dataclasses import dataclass, replace

from .models import Process, Snapshot

SORT_KEYS = ("gpu_memory", "cpu", "rss", "pid", "user", "gpu")


@dataclass
class Options:
    gpu: set[int] | None = None
    pid: set[int] | None = None
    user: str | None = None
    sort: str = "gpu_memory"
    reverse: bool = False
    search: str = ""
    current_user: bool = False
    compact: bool = False
    ascii: bool = False
    no_color: bool = False


def safe_text(value: str | None) -> str:
    if value is None:
        return "N/A"
    # Process names and command lines are untrusted terminal input.
    return "".join(" " if unicodedata.category(c).startswith("C") else c for c in str(value))


def fmt_bytes(value: int | None) -> str:
    if value is None:
        return "N/A"
    if value >= 1024**3:
        return f"{value / 1024**3:.1f}GiB"
    return f"{value / 1024**2:.0f}MiB"


def fmt_number(value: float | None, suffix: str = "") -> str:
    return "N/A" if value is None else f"{value:.0f}{suffix}"


def fmt_duration(value: float | None) -> str:
    if value is None:
        return "N/A"
    seconds = max(0, int(value))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    result = f"{hours:02}:{minutes:02}:{seconds:02}"
    return f"{days}d{result}" if days else result


def sorted_processes(processes: list[Process], options: Options) -> list[Process]:
    attributes = {"gpu_memory": "gpu_memory_bytes", "cpu": "cpu_percent", "rss": "rss_bytes",
                  "pid": "pid", "user": "username", "gpu": "device_index"}
    attribute = attributes[options.sort]
    known = [p for p in processes if getattr(p, attribute) is not None]
    unknown = [p for p in processes if getattr(p, attribute) is None]
    # Stable PID/device order provides a deterministic tie-breaker.
    known.sort(key=lambda p: (p.pid, p.device_index))
    descending = options.sort in ("gpu_memory", "cpu", "rss")
    known.sort(key=lambda p: getattr(p, attribute), reverse=descending != options.reverse)
    return known + sorted(unknown, key=lambda p: (p.pid, p.device_index))


def filter_snapshot(snapshot: Snapshot, options: Options) -> Snapshot:
    devices = [d for d in snapshot.devices if options.gpu is None or d.index in options.gpu]
    rows = []
    for row in snapshot.processes:
        if options.gpu is not None and row.device_index not in options.gpu:
            continue
        if options.pid is not None and row.pid not in options.pid:
            continue
        if options.user is not None and row.username != options.user:
            continue
        if options.current_user and row.username != getpass.getuser():
            continue
        haystack = f"{row.pid} {row.device_index} {row.username or ''} {row.command or ''}".casefold()
        if options.search.casefold() not in haystack:
            continue
        rows.append(row)
    return replace(snapshot, devices=devices, processes=sorted_processes(rows, options))


def render_text(snapshot: Snapshot) -> str:
    lines = [f"musatop | {snapshot.sampled_at}",
             f"Driver: {safe_text(snapshot.driver_version)}  GMI: {safe_text(snapshot.gmi_version)}  "
             f"MUSA Toolkit: {safe_text(snapshot.musa_version)}",
             f"Host: {safe_text(snapshot.host.hostname)} CPU: {fmt_number(snapshot.host.cpu_percent, '%')} "
             f"RAM: {fmt_bytes(snapshot.host.memory_used_bytes)}/{fmt_bytes(snapshot.host.memory_total_bytes)}",
             "GPU  NAME                 UTIL   MEMORY (used/total)      TEMP   POWER/LIMIT"]
    for d in snapshot.devices:
        lines.append(f"{d.index:<4} {safe_text(d.name):<20} {fmt_number(d.gpu_utilization_percent, '%'):>5}  "
                     f"{fmt_bytes(d.memory_used_bytes):>8}/{fmt_bytes(d.memory_total_bytes):<8}  "
                     f"{fmt_number(d.temperature_c, 'C'):>5}  "
                     f"{fmt_number(d.power_draw_w, 'W')}/{fmt_number(d.power_limit_w, 'W')}")
    if not snapshot.devices:
        lines.append("Device data unavailable." if snapshot.devices_stale else "No GPUs found.")
    lines.append("GPU  PID      USER          GPU MEM    CPU%    RSS       TIME       COMMAND")
    for p in snapshot.processes:
        lines.append(f"{p.device_index:<4} {p.pid:<8} {safe_text(p.username):<13} "
                     f"{fmt_bytes(p.gpu_memory_bytes):>8} {fmt_number(p.cpu_percent):>6} "
                     f"{fmt_bytes(p.rss_bytes):>9} {fmt_duration(p.running_seconds):>10}  {safe_text(p.command)}")
    if not snapshot.processes:
        lines.append("Process data unavailable." if snapshot.processes_stale else "No matching GPU processes.")
    if snapshot.devices_stale or snapshot.processes_stale:
        lines.append(f"STALE: devices={snapshot.devices_stale}, processes={snapshot.processes_stale}; "
                     f"last successful samples: {snapshot.devices_sampled_at}, {snapshot.processes_sampled_at}")
    if snapshot.musa_version is None and snapshot.musa_version_reason:
        lines.append(f"NOTE: MUSA Toolkit: {safe_text(snapshot.musa_version_reason)}")
    unavailable = [str(d.index) for d in snapshot.devices if d.power_limit_reason]
    if unavailable:
        lines.append(f"NOTE: GPU {','.join(unavailable)} power limit unavailable from GMI; "
                     "power draw is a separate reading. See power_limit_reason in --json.")
    lines.extend(f"ERROR: {safe_text(error)}" for error in snapshot.errors)
    return "\n".join(lines)
