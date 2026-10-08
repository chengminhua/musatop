"""Host process enrichment and identity-checked termination."""

import os
import signal
import socket
import time
from dataclasses import replace

import psutil

from .models import Host, Process


class ProcessEnricher:
    def __init__(self):
        self._processes: dict[int, tuple[float, psutil.Process]] = {}
        self._host_primed = False

    def host(self) -> Host:
        memory = psutil.virtual_memory()
        cpu = psutil.cpu_percent(interval=None)
        result = Host(socket.gethostname(), cpu if self._host_primed else None,
                      memory.used, memory.total)
        self._host_primed = True
        return result

    def enrich(self, processes: list[Process]) -> list[Process]:
        details: dict[int, dict] = {}
        live: dict[int, tuple[float, psutil.Process]] = {}
        now = time.time()
        for row in processes:
            if row.pid in details:
                continue
            info = dict(status="unverified")
            try:
                fresh = psutil.Process(row.pid)
                created = fresh.create_time()
                cached = self._processes.get(row.pid)
                primed = cached is not None and cached[0] == created
                process = cached[1] if primed else fresh
                if not process.is_running():
                    raise psutil.NoSuchProcess(row.pid)
                info.update(create_time=created, running_seconds=max(0, now - created))
                with process.oneshot():
                    info["username"] = process.username()
                    info["command"] = " ".join(process.cmdline()) or process.name()
                    info["rss_bytes"] = process.memory_info().rss
                    cpu = process.cpu_percent(interval=None)
                    info["cpu_percent"] = cpu if primed else None
                    info["status"] = "ok"
                live[row.pid] = (created, process)
            except psutil.AccessDenied:
                info["status"] = "access_denied"
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                info["status"] = "exited"
            details[row.pid] = info
        self._processes = live
        return [replace(row, **details[row.pid]) for row in processes]


def terminate_process(process: Process) -> str:
    """Send only SIGTERM; caller must obtain explicit interactive confirmation.

    Linux pidfds bind the signal to one process instance. psutil's own reuse
    check remains the fallback on kernels without pidfd support.
    """
    if process.pid <= 1 or process.pid == os.getpid():
        return "Refused: protected PID."
    if process.create_time is None or process.status != "ok":
        return "Refused: process identity is not verified."
    fd = None
    try:
        if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
            try:
                fd = os.pidfd_open(process.pid)
            except OSError as exc:
                # Older kernels may lack pidfd_open even on recent Python.
                import errno
                if exc.errno not in (errno.ENOSYS, errno.EINVAL):
                    raise
        target = psutil.Process(process.pid)
        if target.create_time() != process.create_time or not target.is_running():
            return "Refused: PID was reused or the process exited."
        if fd is not None:
            signal.pidfd_send_signal(fd, signal.SIGTERM)
        else:
            target.terminate()
        return f"SIGTERM sent to PID {process.pid}."
    except (psutil.NoSuchProcess, psutil.ZombieProcess, ProcessLookupError):
        return "Process has already exited."
    except (psutil.AccessDenied, PermissionError):
        return "Permission denied: cannot terminate this process."
    except OSError as exc:
        return f"Could not terminate process: {exc.strerror or exc}."
    finally:
        if fd is not None:
            os.close(fd)
