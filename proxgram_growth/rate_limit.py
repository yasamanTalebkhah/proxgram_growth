"""Rate limiting and pacing primitives for the growth worker.

Two layers of protection:

1. PerTargetPacer  — max one comment per target channel per cooldown window
                     (plus jitter so multiple targets don't synchronize).
2. GlobalPacer     — hard cap on comments per rolling window across ALL
                     targets, protecting the account from volume-based flags.
"""

from __future__ import annotations

import random
import time
from collections import deque
from dataclasses import dataclass, field

from .config import TargetConfig


class RateLimitExceeded(Exception):
    """Raised (internally) when a comment is not allowed right now."""


def _now() -> float:
    return time.monotonic()


class PerTargetPacer:
    """Enforces min-interval-per-target with randomized jitter."""

    def __init__(self, *, rng: random.Random | None = None) -> None:
        self._last_post: dict[str, float] = {}
        self._rng = rng or random.Random()

    def ready_at(self, target: TargetConfig, *, now: float | None = None) -> float:
        """Earliest monotonic time the next comment for `target` may be posted."""
        key = _normalize(target.channel)
        now = _now() if now is None else now
        last = self._last_post.get(key)
        if last is None:
            return now
        window = target.cooldown + self._rng.uniform(0, max(0, target.jitter))
        return last + window

    def acquire(
        self, target: TargetConfig, *, now: float | None = None
    ) -> float:
        """Reserve the next slot for this target.

        Returns the wait (seconds, >= 0) before the comment may be posted.
        """
        now = _now() if now is None else now
        key = _normalize(target.channel)
        ready = self.ready_at(target, now=now)
        wait = max(0.0, ready - now)
        if wait == 0.0:
            # Only stamp the window when we actually consume the slot.
            self._last_post[key] = now
        return wait

    def mark_posted(self, target: TargetConfig, *, now: float | None = None) -> None:
        """Record that a comment was actually posted for this target."""
        key = _normalize(target.channel)
        self._last_post[key] = _now() if now is None else now

    def time_until_ready(self, target: TargetConfig, *, now: float | None = None) -> float:
        return max(0.0, self.ready_at(target, now=now) - (_now() if now is None else now))


class GlobalPacer:
    """Rolling-window cap across all targets (account-level safety)."""

    def __init__(
        self,
        *,
        max_comments: int,
        window_seconds: int,
        rng: random.Random | None = None,
    ) -> None:
        if max_comments <= 0 or window_seconds <= 0:
            raise ValueError("max_comments and window_seconds must be positive")
        self._max = max_comments
        self._window = window_seconds
        self._events: deque[float] = deque()
        self._rng = rng or random.Random()

    def try_acquire(self, *, now: float | None = None) -> bool:
        """Consume one global slot if available."""
        now = _now() if now is None else now
        self._prune(now)
        if len(self._events) >= self._max:
            return False
        self._events.append(now)
        return True

    def seconds_until_slot(self, *, now: float | None = None) -> float:
        now = _now() if now is None else now
        self._prune(now)
        if len(self._events) < self._max:
            return 0.0
        return max(0.0, self._events[0] + self._window - now)

    def _prune(self, now: float) -> None:
        cutoff = now - self._window
        while self._events and self._events[0] <= cutoff:
            self._events.popleft()

    @property
    def used(self) -> int:
        self._prune(_now())
        return len(self._events)


def _normalize(channel: str) -> str:
    text = channel.strip()
    if text.startswith("@"):
        text = text[1:]
    return text.lower()


@dataclass
class HumanDelay:
    """Randomized human-like delay before acting on a fresh post."""

    min_seconds: float
    max_seconds: float
    rng: random.Random = field(default_factory=random.Random)

    def __post_init__(self) -> None:
        if self.min_seconds < 0 or self.max_seconds < self.min_seconds:
            raise ValueError("require 0 <= min_seconds <= max_seconds")

    def sample(self) -> float:
        return self.rng.uniform(self.min_seconds, self.max_seconds)
