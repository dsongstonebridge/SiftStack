"""
throttle.py — request-pacing utility for reisift clients.

Enforces a minimum gap between calls to look like a human user, not a bot.
Default is 400-800ms randomized — close to a fast-clicking user. Set
min_ms=0 / max_ms=0 for no throttling (batch scripts running off-hours).

Usage:
    from throttle import Throttle

    throttle = Throttle()                    # default 400-800ms
    throttle = Throttle(min_ms=0)            # off
    throttle = Throttle(min_ms=800, max_ms=1500)  # slower pacing

    # In a client's _request():
    throttle.wait()
    # ...make HTTP request...

One Throttle instance per process is the normal pattern — the clients share
a single instance so the gap is enforced across both CRM and SiftMap calls.
"""

from __future__ import annotations
import random
import time
import threading
from dataclasses import dataclass, field


@dataclass
class Throttle:
    """Enforces a minimum gap between calls, randomized within [min_ms, max_ms]."""

    min_ms: int = 400
    max_ms: int = 800
    _last: float = field(default=0.0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def wait(self) -> None:
        """Block until enough time has passed since the previous call."""
        if self.max_ms <= 0:
            return
        with self._lock:
            now = time.time()
            # Pick a random gap for THIS call
            gap_ms = random.randint(self.min_ms, self.max_ms) if self.max_ms > self.min_ms else self.min_ms
            elapsed_ms = (now - self._last) * 1000
            if elapsed_ms < gap_ms:
                time.sleep((gap_ms - elapsed_ms) / 1000)
            self._last = time.time()


# Module-level default throttle. Both clients grab this by default so pacing
# is enforced across all reisift calls in a single process.
DEFAULT_THROTTLE = Throttle()
