"""
core/circuit_breaker.py
Thread-safe Circuit Breaker implementation for external LLM APIs.
Protects upstream quotas and prevents cascade latency.
"""

import re
import threading
import time

from core.logger import app_logger


class CircuitBreaker:
    """
    Manages provider state:
      - CLOSED: Service healthy; calls proceed normally.
      - OPEN: Service degraded/rate-limited; calls fail over immediately without network I/O.
      - HALF_OPEN: Trial probe allowed to test service recovery.
    """

    def __init__(self, name: str, default_cooldown: float = 60.0):
        self.name = name
        self.default_cooldown = default_cooldown
        self._state = "CLOSED"
        self._trip_time = 0.0
        self._cooldown = default_cooldown
        self._half_open_probe_in_flight = False
        self._lock = threading.Lock()

    @property
    def is_available(self) -> bool:
        """Non-blocking check whether requests may proceed.

        Only one probe is admitted while HALF_OPEN so a recovering provider is
        not stampeded by all waiting callers.
        """
        with self._lock:
            if self._state == "CLOSED":
                return True

            elapsed = time.time() - self._trip_time
            if elapsed >= self._cooldown:
                if self._state != "HALF_OPEN":
                    self._state = "HALF_OPEN"
                    self._half_open_probe_in_flight = False
                    app_logger.info(
                        "[%s CircuitBreaker] Cooldown expired (%.1fs). State: HALF_OPEN",
                        self.name,
                        elapsed,
                    )
                if self._half_open_probe_in_flight:
                    return False
                self._half_open_probe_in_flight = True
                return True

            return False

    def record_success(self):
        """Resets the circuit upon confirmed successful interaction."""
        with self._lock:
            if self._state != "CLOSED":
                app_logger.info("[%s CircuitBreaker] Service recovered. State: CLOSED", self.name)
            self._state = "CLOSED"
            self._cooldown = self.default_cooldown
            self._half_open_probe_in_flight = False

    def record_failure(self, error: Exception):
        """Trips the circuit breaker with adaptive cooldown parsing."""
        with self._lock:
            self._half_open_probe_in_flight = False
            err_msg = str(error)
            retry_match = re.search(r"retry\s+(?:in|after)\s+([\d\.]+)\s*s", err_msg, re.IGNORECASE)
            if retry_match:
                parsed_delay = float(retry_match.group(1)) + 2.0
                self._cooldown = max(self.default_cooldown, parsed_delay)
            else:
                self._cooldown = self.default_cooldown

            self._trip_time = time.time()
            self._state = "OPEN"
            app_logger.warning(
                "[%s CircuitBreaker] Tripped to OPEN | Cooldown: %.1fs | Reason: %s",
                self.name,
                self._cooldown,
                err_msg[:120],
            )