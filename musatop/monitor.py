"""One serial sampler shared by terminal and one-shot output."""

import copy
import math
import threading
import time

from .backend import GmiBackend
from .history import HistoryBuffer, HistoryPoint
from .models import Snapshot, utc_now
from .processes import ProcessEnricher


class Monitor:
    def __init__(self, interval: float = 1.0, backend=None):
        if not math.isfinite(interval) or not 0 < interval <= 3600:
            raise ValueError("sampling interval must be positive and at most 3600 seconds")
        self.interval = interval
        self.backend = backend if backend is not None else GmiBackend()
        self.enricher = ProcessEnricher()
        self._snapshot: Snapshot | None = None
        self._history = HistoryBuffer()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def sample(self) -> Snapshot:
        snapshot = self.backend.sample()
        snapshot.processes = self.enricher.enrich(snapshot.processes)
        try:
            snapshot.host = self.enricher.host()
        except (OSError, RuntimeError) as exc:
            snapshot.errors.append(f"Host sampling failed: {exc}")
        return snapshot

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="musatop-sampler", daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            self._wake.clear()
            started = time.monotonic()
            try:
                snapshot = self.sample()
            except Exception as exc:
                snapshot = self.latest() or Snapshot()
                snapshot.sampled_at = utc_now()
                snapshot.devices_stale = snapshot.processes_stale = True
                snapshot.errors = [f"Sampling failed: {type(exc).__name__}: {exc}"]
            with self._lock:
                self._history.record(snapshot, time.monotonic())
                self._snapshot = snapshot
            self._wake.wait(max(0, self.interval - (time.monotonic() - started)))

    def latest(self) -> Snapshot | None:
        with self._lock:
            return copy.deepcopy(self._snapshot)

    def latest_with_history(self) -> tuple[Snapshot | None, dict[str, list[HistoryPoint]]]:
        """Return consistent, independently owned data for one UI redraw."""
        with self._lock:
            return copy.deepcopy(self._snapshot), self._history.snapshot(time.monotonic())

    def refresh(self):
        self._wake.set()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=7)
