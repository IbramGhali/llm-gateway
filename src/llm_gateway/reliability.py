"""Per-model circuit breaker: after repeated retryable failures a model is skipped for a
cool-down, so the gateway moves straight to the fallback instead of waiting on a provider
that is down."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class Breaker:
    threshold: int = 3
    cooldown: float = 10.0
    failures: int = 0
    opened_at: float | None = None
    trips: int = 0


@dataclass
class Breakers:
    threshold: int = 3
    cooldown: float = 10.0
    state: dict[str, Breaker] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def _get(self, key: str) -> Breaker:
        return self.state.setdefault(key, Breaker(self.threshold, self.cooldown))

    def allow(self, key: str) -> bool:
        with self.lock:
            b = self._get(key)
            if b.opened_at is None:
                return True
            if time.monotonic() - b.opened_at >= b.cooldown:
                b.opened_at = None  # half-open: one trial request
                b.failures = b.threshold - 1
                return True
            return False

    def record(self, key: str, ok: bool) -> None:
        with self.lock:
            b = self._get(key)
            if ok:
                b.failures = 0
                return
            b.failures += 1
            if b.failures >= b.threshold and b.opened_at is None:
                b.opened_at = time.monotonic()
                b.trips += 1

    def trips(self) -> dict[str, int]:
        with self.lock:
            return {k: b.trips for k, b in self.state.items() if b.trips}
