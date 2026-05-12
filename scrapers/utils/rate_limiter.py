"""Limite globale du débit des requêtes HTTP (transverse, thread-safe)."""

from __future__ import annotations

import threading
import time

# Plage recommandée 30–60 req/min — ajustable ici sans toucher au reste du code.
DEFAULT_MAX_CALLS_PER_MINUTE = 45
DEFAULT_PERIOD_SECONDS = 60.0


class GlobalRateLimiter:
    """Compteur glissant : au plus ``max_calls`` événements par ``period`` secondes."""

    def __init__(self, max_calls: int = DEFAULT_MAX_CALLS_PER_MINUTE, period: float = DEFAULT_PERIOD_SECONDS) -> None:
        self.max_calls = int(max_calls)
        self.period = float(period)
        self.lock = threading.Lock()
        self.calls: list[float] = []

    def wait(self) -> None:
        """Attend si nécessaire pour respecter la limite ; ne lève pas d’exception."""
        try:
            while True:
                sleep_for: float = 0.0
                with self.lock:
                    now = time.time()
                    self.calls = [t for t in self.calls if now - t < self.period]
                    if len(self.calls) < self.max_calls:
                        self.calls.append(time.time())
                        return
                    sleep_for = max(0.0, self.period - (now - self.calls[0]))
                if sleep_for > 0:
                    time.sleep(sleep_for)
                else:
                    time.sleep(0.001)
        except Exception:
            pass


global_rate_limiter = GlobalRateLimiter(
    max_calls=DEFAULT_MAX_CALLS_PER_MINUTE,
    period=DEFAULT_PERIOD_SECONDS,
)
