"""Unit-explicit snapshots shared by collectors and all output formats."""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Device:
    index: int
    uuid: str | None = None
    name: str | None = None
    bus_id: str | None = None
    gpu_utilization_percent: float | None = None
    memory_utilization_percent: float | None = None
    memory_used_bytes: int | None = None
    memory_total_bytes: int | None = None
    memory_free_bytes: int | None = None
    temperature_c: float | None = None
    power_draw_w: float | None = None
    power_limit_w: float | None = None
    graphics_clock_mhz: float | None = None
    memory_clock_mhz: float | None = None


@dataclass
class Process:
    device_index: int
    pid: int
    device_uuid: str | None = None
    gpu_memory_bytes: int | None = None
    username: str | None = None
    command: str | None = None
    cpu_percent: float | None = None
    rss_bytes: int | None = None
    create_time: float | None = None
    running_seconds: float | None = None
    status: str = "unverified"


@dataclass
class Host:
    hostname: str | None = None
    cpu_percent: float | None = None
    memory_used_bytes: int | None = None
    memory_total_bytes: int | None = None


@dataclass
class Snapshot:
    schema_version: int = 1
    sampled_at: str = field(default_factory=utc_now)
    devices_sampled_at: str | None = None
    processes_sampled_at: str | None = None
    driver_version: str | None = None
    gmi_version: str | None = None
    musa_version: str | None = None
    host: Host = field(default_factory=Host)
    devices: list[Device] = field(default_factory=list)
    processes: list[Process] = field(default_factory=list)
    devices_stale: bool = False
    processes_stale: bool = False
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)
