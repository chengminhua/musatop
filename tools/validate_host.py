"""Opt-in host acceptance checks; never run by unit tests or installation."""

import argparse
import json
import os
import subprocess
import time

import psutil

from musatop.backend import GmiBackend, parse_devices, parse_processes
from musatop.monitor import Monitor
from musatop.processes import terminate_process


def workload(binary, devices, terminate):
    backend = GmiBackend()
    before = backend.sample()
    if before.errors or before.devices_stale or before.processes_stale:
        raise RuntimeError(f"Cannot establish GPU status: {before.errors}")
    if before.processes:
        raise RuntimeError("Other GPU processes are present; not starting a workload.")
    if not before.devices or not all(d.gpu_utilization_percent == 0 and d.memory_used_bytes == 0 for d in before.devices):
        raise RuntimeError("GPUs are not idle; not starting a workload.")
    command = ["timeout", "--signal=TERM", "--kill-after=5s", "25s", os.path.abspath(binary),
               "--devices", devices, "--seconds", "20", "--mib", "64"]
    monitor = Monitor(backend=backend)
    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    target = None
    try:
        started = json.loads(child.stdout.readline())
        assert started["event"] == "started", started
        pid = started["pid"]
        expected = {int(i) for i in devices.split(",")}
        observed_memory = []
        peak_util = 0
        for _ in range(4):
            sample = monitor.sample()
            assert not sample.errors, sample.errors
            rows = [p for p in sample.processes if p.pid == pid]
            assert {p.device_index for p in rows} == expected, rows
            assert all(p.status == "ok" and p.create_time is not None for p in rows), rows
            assert all(p.gpu_memory_bytes is not None and 0 < p.gpu_memory_bytes <= 1024**3 for p in rows), rows
            assert all(d.memory_used_bytes is not None and d.memory_used_bytes <= 1024**3
                       for d in sample.devices if d.index in expected), sample.devices
            assert all(p.cpu_percent == rows[0].cpu_percent and p.rss_bytes == rows[0].rss_bytes for p in rows)
            raw = subprocess.check_output(["mthreads-gmi"], text=True, timeout=3)
            reference = parse_processes(raw)
            assert {(p.device_index, p.pid, p.gpu_memory_bytes) for p in rows} == \
                   {(p.device_index, p.pid, p.gpu_memory_bytes) for p in reference if p.pid == pid}
            raw_devices = subprocess.check_output(["mthreads-gmi", "-q", "--json"], text=True, timeout=3)
            reference_devices, _ = parse_devices(raw_devices)
            assert [(d.index, d.memory_total_bytes) for d in sample.devices] == \
                   [(d.index, d.memory_total_bytes) for d in reference_devices]
            observed_memory.extend(p.gpu_memory_bytes for p in rows)
            peak_util = max(peak_util, *(d.gpu_utilization_percent or 0 for d in sample.devices))
            target = rows[0]
            time.sleep(1)
        if terminate:
            assert "gpu_smoke" in target.command
            message = terminate_process(target)
            assert message.startswith("SIGTERM sent"), message
        stdout, stderr = child.communicate(timeout=25)
        assert child.returncode == (143 if terminate else 0), (child.returncode, stdout, stderr)
        assert '"event":"completed"' in stdout, stdout
        for _ in range(10):
            after = backend.sample()
            assert not after.errors, after.errors
            if not any(p.pid == pid for p in after.processes) and all(d.memory_used_bytes == 0 for d in after.devices):
                break
            time.sleep(0.5)
        else:
            raise AssertionError("Workload process or GPU memory did not disappear")
        print(json.dumps({"check": "workload", "devices": sorted(expected), "passed": True,
                          "terminated": terminate, "max_process_memory_bytes": max(observed_memory),
                          "peak_gpu_utilization_percent": peak_util, "memory_released": True}), flush=True)
    finally:
        if child.poll() is None:
            # timeout relays SIGTERM to its own command; never touch another task.
            child.terminate()
            try:
                child.communicate(timeout=7)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate(timeout=3)


def soak(seconds):
    monitor = Monitor(interval=1)
    process = psutil.Process()
    monitor.start()
    started = time.monotonic()
    cpu_start = sum(process.cpu_times()[:2])
    count = 0
    previous = None
    last_time = started
    max_gap = 0
    rss = []
    failures = []
    print(json.dumps({"check": "soak", "state": "started", "seconds": seconds}), flush=True)
    try:
        while time.monotonic() - started < seconds:
            snapshot = monitor.latest()
            if snapshot is not None and snapshot.sampled_at != previous:
                now = time.monotonic()
                max_gap = max(max_gap, now - last_time)
                last_time = now
                previous = snapshot.sampled_at
                count += 1
                rss.append(process.memory_info().rss)
                if snapshot.errors or snapshot.devices_stale or snapshot.processes_stale:
                    failures.append(snapshot.errors)
                if len(snapshot.devices) != 8 or any(d.memory_total_bytes != 80 * 1024**3 for d in snapshot.devices):
                    failures.append(["Unexpected device count or memory capacity"])
            time.sleep(0.2)
    finally:
        monitor.stop()
    elapsed = time.monotonic() - started
    passed = not failures and count >= seconds * 0.9 and max_gap < 5
    print(json.dumps({"check": "soak", "passed": passed, "seconds": round(elapsed, 2),
                      "samples": count, "max_gap_seconds": round(max_gap, 3),
                      "rss_min_bytes": min(rss, default=0), "rss_max_bytes": max(rss, default=0),
                      "cpu_percent_one_core": round(100 * (sum(process.cpu_times()[:2]) - cpu_start) / elapsed, 2),
                      "failures": failures}), flush=True)
    assert passed, "Soak acceptance failed"


if __name__ == "__main__":
    if not __debug__:
        raise SystemExit("Validation requires assertions; do not use python -O or PYTHONOPTIMIZE.")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="check", required=True)
    load = sub.add_parser("workload")
    load.add_argument("binary")
    load.add_argument("--devices", default="0")
    load.add_argument("--terminate", action="store_true")
    sustained = sub.add_parser("soak")
    sustained.add_argument("--seconds", type=int, default=600)
    args = parser.parse_args()
    if args.check == "workload":
        workload(args.binary, args.devices, args.terminate)
    else:
        if args.seconds < 10:
            parser.error("soak requires at least 10 seconds")
        soak(args.seconds)
