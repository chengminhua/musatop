"""Read-only collection from the vendor's installed ``mthreads-gmi`` tool.

The device query is JSON; the process table is a separate command because GMI
does not document a JSON process interface. Each source has its own freshness.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import subprocess
from typing import Any

from .models import Device, Process, Snapshot, utc_now
from .toolkit import detect_toolkit


class CollectionError(RuntimeError):
    """An unavailable command or an output shape we cannot safely interpret."""


_MISSING = {"", "n/a", "na", "none", "null", "unknown", "-", "--", "not supported"}
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)"
_MEMORY = re.compile(rf"^({_NUMBER})\s*([KMGTPE]?i?B)?$", re.IGNORECASE)
_PROCESS_ROW = re.compile(
    rf"^(\d+)\s+(\d+)\s+(.+?)\s+({_NUMBER}\s*[KMGTPE]?i?B|N/A|--|Not Supported)\s*$",
    re.IGNORECASE,
)


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _get(mapping: dict[str, Any], *names: str) -> Any:
    """Handle harmless GMI whitespace/case differences (including Power Draw )."""
    wanted = {_key(name) for name in names}
    return next((value for key, value in mapping.items() if _key(str(key)) in wanted), None)


def _section(mapping: dict[str, Any], name: str) -> dict[str, Any]:
    value = _get(mapping, name)
    return value if isinstance(value, dict) else {}


def _string(value: Any) -> str | None:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    text = str(value).strip()
    return None if text.casefold().strip("[]") in _MISSING else text


def _number(value: Any, unit: str, *, negative: bool = False) -> float | None:
    text = _string(value)
    if text is None:
        return None
    match = re.fullmatch(rf"({_NUMBER})\s*(?:{re.escape(unit)})?", text, re.IGNORECASE)
    if not match:
        return None
    result = float(match[1])
    if not math.isfinite(result) or (not negative and result < 0):
        return None
    return result


def _percent(value: Any) -> float | None:
    result = _number(value, "%")
    return result if result is None or result <= 100 else None


def _power_limit(power: dict[str, Any]) -> tuple[float | None, str | None]:
    # A default/maximum cap is not necessarily the cap currently in force.
    current_key = next((key for key in power if _key(str(key)) == "currentpowerlimit"), None)
    raw = power[current_key] if current_key is not None else _get(power, "Power Limit")
    value = _number(raw, "W")
    if value is not None and value > 0:
        return value, None
    if raw is None:
        return None, "Current power limit field is missing from GMI"
    if _string(raw) is None:
        return None, "Current power limit is not reported by GMI"
    return None, "GMI returned an invalid current power limit"


def memory_bytes(value: Any) -> int | None:
    """Convert GMI quantities to bytes; unitless GMI memory numbers are MiB."""
    text = _string(value)
    if text is None:
        return None
    match = _MEMORY.fullmatch(text)
    if not match:
        return None
    amount = float(match[1])
    if not math.isfinite(amount) or amount < 0:
        return None
    unit = (match[2] or "MiB").upper()
    prefix = unit[0] if unit != "B" else ""
    exponent = "KMGTPE".index(prefix) + 1 if prefix else 0
    base = 1024 if "I" in unit else 1000
    try:
        converted = amount * base**exponent
        return int(converted) if math.isfinite(converted) else None
    except (OverflowError, ValueError):
        return None


def parse_devices(output: str) -> tuple[list[Device], str | None]:
    """Parse a GMI query; missing optional metrics remain unknown, never zero."""
    try:
        data = json.loads(output)
    except (json.JSONDecodeError, TypeError) as error:
        raise CollectionError(f"invalid GMI JSON: {error}") from error
    if not isinstance(data, dict):
        raise CollectionError("invalid GMI JSON: expected an object")
    entries = _get(data, "GPU", "GPUs")
    if not isinstance(entries, list):
        raise CollectionError("invalid GMI JSON: GPU must be an explicit list")
    attached = _get(data, "Attached GPUs")
    if attached is not None:
        if not re.fullmatch(r"\d+", str(attached).strip()) or int(attached) != len(entries):
            raise CollectionError("invalid GMI JSON: Attached GPUs does not match the GPU list")
    devices: list[Device] = []
    indices: set[int] = set()
    uuids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise CollectionError("invalid GMI JSON: GPU entry must be an object")
        raw_index = _get(entry, "Index")
        if isinstance(raw_index, bool) or not re.fullmatch(r"\d+", str(raw_index).strip()):
            raise CollectionError("invalid GMI JSON: GPU Index must be a nonnegative integer")
        index = int(raw_index)
        if index in indices:
            raise CollectionError(f"invalid GMI JSON: duplicate GPU Index {index}")
        indices.add(index)
        uuid = _string(_get(entry, "GPU UUID", "UUID"))
        if uuid is not None:
            if uuid.casefold() in uuids:
                raise CollectionError("invalid GMI JSON: duplicate GPU UUID")
            uuids.add(uuid.casefold())
        memory = _section(entry, "FB Memory Usage")
        utilization = _section(entry, "Utilization")
        temperature = _section(entry, "Temperature")
        power = _section(entry, "Power Readings")
        power_limit, power_limit_reason = _power_limit(power)
        clocks = _section(entry, "Clocks")
        devices.append(
            Device(
                index=index,
                uuid=uuid,
                name=_string(_get(entry, "Product Name", "Name")),
                bus_id=_string(_get(_section(entry, "PCI"), "Bus ID")),
                gpu_utilization_percent=_percent(_get(utilization, "Gpu")),
                memory_utilization_percent=_percent(_get(utilization, "Memory")),
                memory_used_bytes=memory_bytes(_get(memory, "Used")),
                memory_total_bytes=memory_bytes(_get(memory, "Total")),
                memory_free_bytes=memory_bytes(_get(memory, "Free")),
                temperature_c=_number(_get(temperature, "GPU Current Temp"), "C", negative=True),
                power_draw_w=_number(_get(power, "Power Draw"), "W"),
                power_limit_w=power_limit,
                power_limit_reason=power_limit_reason,
                graphics_clock_mhz=_number(_get(clocks, "Graphics"), "MHz"),
                memory_clock_mhz=_number(_get(clocks, "Memory"), "MHz"),
            )
        )
    return devices, _string(_get(data, "Driver Version"))


def parse_processes(output: str, devices: list[Device] | None = None) -> list[Process]:
    """Read the documented ID/PID/name/memory table, allowing wrapped headers.

    A missing table or a malformed data row is an error, not an empty process
    list. The explicit vendor "No running processes found" message is required
    for an empty table.
    """
    text = _ANSI.sub("", output)
    marker = re.search(r"\bProcesses\s*:", text, re.IGNORECASE)
    if marker is None:
        raise CollectionError("GMI process table is missing")
    lines = text[marker.end():].splitlines()
    header = ""
    has_header = False
    explicit_empty = False
    processes: list[Process] = []
    identities: set[tuple[int, int]] = set()
    uuids = {device.index: device.uuid for device in devices or []}
    for raw in lines:
        line = raw.strip().strip("|").strip()
        if not line or re.fullmatch(r"[+|=\-\s]+", line):
            continue
        if re.fullmatch(r"No running processes found\s*[.!]?", line, re.IGNORECASE):
            explicit_empty = True
            continue
        if not has_header:
            header += " " + line
            has_header = all(re.search(rf"\b{word}\b", header, re.IGNORECASE)
                             for word in ("ID", "PID", "Process", "Memory"))
            if re.match(r"\d+\s+\d+\b", line):
                raise CollectionError("GMI process table has no recognized column header")
            continue
        if re.fullmatch(r"Usage", line, re.IGNORECASE):
            continue
        match = _PROCESS_ROW.fullmatch(line)
        if match is None:
            raise CollectionError("GMI process table contains an unrecognized row")
        index, pid = int(match[1]), int(match[2])
        if pid <= 0 or (index, pid) in identities:
            raise CollectionError("GMI process table contains an invalid or duplicate process")
        identities.add((index, pid))
        processes.append(Process(
            device_index=index,
            pid=pid,
            device_uuid=uuids.get(index),
            command=_string(match[3]),
            gpu_memory_bytes=memory_bytes(match[4]),
        ))
    if not has_header:
        raise CollectionError("GMI process table has no recognized column header")
    if explicit_empty and processes:
        raise CollectionError("GMI process table contradicts its empty-process message")
    if not processes and not explicit_empty:
        raise CollectionError("GMI process table ended without rows or an empty-process message")
    return processes


class GmiBackend:
    """Collect every GPU in two commands and retain source-specific last success."""

    def __init__(self, binary: str = "mthreads-gmi", timeout: float = 3.0):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number")
        self.binary = binary
        self.timeout = timeout
        self._devices: list[Device] = []
        self._processes: list[Process] = []
        self._devices_sampled_at: str | None = None
        self._processes_sampled_at: str | None = None
        self._driver_version: str | None = None
        self._gmi_version: str | None = None
        self._musa_version: str | None = None
        self._musa_version_source: str | None = None
        self._musa_version_reason: str | None = None
        self._versions_checked = False

    def _run(self, *args: str, timeout: float | None = None) -> str:
        argv = [self.binary, *args]
        environment = os.environ.copy()
        environment["LC_ALL"] = "C"
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=self.timeout if timeout is None else timeout, env=environment,
                check=False,
            )
        except FileNotFoundError as error:
            raise CollectionError(f"GMI executable not found: {self.binary}") from error
        except subprocess.TimeoutExpired as error:
            raise CollectionError(f"GMI command timed out: {' '.join(argv)}") from error
        except OSError as error:
            raise CollectionError(f"GMI command could not start: {error}") from error
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().replace("\n", " ")[:300]
            suffix = f": {detail}" if detail else ""
            raise CollectionError(f"GMI exited with code {result.returncode}{suffix}")
        return result.stdout

    def _detect_versions(self) -> None:
        if self._versions_checked:
            return
        self._versions_checked = True
        try:
            output = self._run("-v", timeout=min(self.timeout, 1.0))
            match = re.search(r"mthreads-gmi\s+(?:version\s*)?:?\s*(\d[\w.+-]*)", output, re.IGNORECASE)
            if match:
                self._gmi_version = match[1]
        except CollectionError:
            pass
        self._musa_version, self._musa_version_source, self._musa_version_reason = detect_toolkit()

    def sample(self) -> Snapshot:
        self._detect_versions()
        snapshot = Snapshot(gmi_version=self._gmi_version, musa_version=self._musa_version,
                            musa_version_source=self._musa_version_source,
                            musa_version_reason=self._musa_version_reason)
        try:
            devices, driver = parse_devices(self._run("-q", "--json"))
            self._devices = devices
            self._devices_sampled_at = utc_now()
            if driver is not None:
                self._driver_version = driver
        except CollectionError as error:
            snapshot.devices_stale = True
            snapshot.errors.append(f"devices: {error}")
        try:
            # Only the current successful device query can establish a GPU
            # identity. An old ordinal-to-UUID mapping may name a different GPU.
            current_devices = None if snapshot.devices_stale else self._devices
            processes = parse_processes(self._run(), current_devices)
            if current_devices is not None:
                known_indices = {device.index for device in current_devices}
                if any(process.device_index not in known_indices for process in processes):
                    raise CollectionError("GMI process table refers to a GPU absent from the current device query")
            self._processes = processes
            self._processes_sampled_at = utc_now()
        except CollectionError as error:
            snapshot.processes_stale = True
            snapshot.errors.append(f"processes: {error}")
        snapshot.devices = copy.deepcopy(self._devices)
        snapshot.processes = copy.deepcopy(self._processes)
        snapshot.devices_sampled_at = self._devices_sampled_at
        snapshot.processes_sampled_at = self._processes_sampled_at
        snapshot.driver_version = self._driver_version
        return snapshot
