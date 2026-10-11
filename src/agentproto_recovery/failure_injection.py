"""Deterministic failure injection and a manual clock.

Failures are armed explicitly at named points; nothing is random unless a
caller derives a schedule from a seeded RNG (see evaluation.py).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .errors import InjectedCrash, InjectedDrop


class ManualClock:
    """Time source for authorization expiry and recovery leases."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self._now = start
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds


@dataclass
class _Arm:
    action: str  # "crash" | "drop" | "active"
    remaining: int | None  # None = until disarmed


@dataclass
class FailureInjector:
    _armed: dict[str, _Arm] = field(default_factory=dict)
    fired: list[tuple[str, str]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def arm(self, point: str, action: str = "crash", times: int | None = 1) -> None:
        if action not in ("crash", "drop", "active"):
            raise ValueError(action)
        with self._lock:
            self._armed[point] = _Arm(action, times)

    def disarm(self, point: str) -> None:
        with self._lock:
            self._armed.pop(point, None)

    def _take(self, point: str) -> str | None:
        with self._lock:
            arm = self._armed.get(point)
            if arm is None:
                return None
            if arm.remaining is not None:
                arm.remaining -= 1
                if arm.remaining <= 0:
                    del self._armed[point]
            self.fired.append((point, arm.action))
            return arm.action

    def check(self, point: str) -> None:
        """Raise InjectedCrash / InjectedDrop if a failure is armed here."""
        action = self._take(point)
        if action == "crash":
            raise InjectedCrash(point)
        if action == "drop":
            raise InjectedDrop(point)

    def active(self, point: str) -> bool:
        """For persistent conditions such as 'store unavailable'."""
        with self._lock:
            arm = self._armed.get(point)
            return arm is not None and arm.action == "active"
