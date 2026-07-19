"""Best-effort brute-force protection for the authorization password form.

This gateway has GitHub write access, and its `/authorize` endpoint is a public
password form. A single static password with no attempt limiting is the weak
point, so we throttle failed attempts.

The gateway is single-user, so the throttle is global rather than per-IP: this
avoids trusting `X-Forwarded-For` behind Render's proxy and cannot be bypassed
by rotating source addresses. The trade-off is that a flood of failures briefly
locks out the legitimate user too, which is acceptable for a personal tool.

State is in-memory: on a single Starter instance that is sufficient; it resets
on deploy/restart, and it does not coordinate across multiple instances. Do not
treat this as a substitute for a strong, high-entropy `MCP_LOGIN_PASSWORD`.
"""

from __future__ import annotations

import threading
import time
from functools import lru_cache


class LoginThrottle:
    """Sliding-window counter of recent failed login attempts."""

    def __init__(self, max_attempts: int, window_seconds: int) -> None:
        self._max_attempts = max_attempts
        self._window_seconds = window_seconds
        self._failures: list[float] = []
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        cutoff = now - self._window_seconds
        self._failures = [stamp for stamp in self._failures if stamp > cutoff]

    def retry_after_seconds(self) -> int:
        """Return seconds to wait if locked out, or 0 if attempts are allowed."""
        now = time.time()
        with self._lock:
            self._prune(now)
            if len(self._failures) < self._max_attempts:
                return 0
            oldest = min(self._failures)
            return max(1, int(oldest + self._window_seconds - now))

    def record_failure(self) -> None:
        """Record one failed password attempt."""
        now = time.time()
        with self._lock:
            self._prune(now)
            self._failures.append(now)

    def reset(self) -> None:
        """Clear all recorded failures. Called after a successful login."""
        with self._lock:
            self._failures.clear()


@lru_cache
def _throttle(max_attempts: int, window_seconds: int) -> LoginThrottle:
    return LoginThrottle(max_attempts, window_seconds)


def get_login_throttle(max_attempts: int, window_seconds: int) -> LoginThrottle:
    """Return the process-wide throttle for the given limits.

    Cached so every request shares one counter. Keyed on the limit values so a
    settings change produces a fresh throttle rather than a stale one.
    """
    return _throttle(max_attempts, window_seconds)
