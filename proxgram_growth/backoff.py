"""Exponential backoff with jitter for FloodWait and transient errors."""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from .config import DEFAULT_BACKOFF_BASE, DEFAULT_BACKOFF_FACTOR, DEFAULT_BACKOFF_MAX


@dataclass
class BackoffPolicy:
    """Exponential backoff: base * factor**attempt, capped, with full jitter."""

    base: float = DEFAULT_BACKOFF_BASE
    factor: float = DEFAULT_BACKOFF_FACTOR
    max_seconds: float = DEFAULT_BACKOFF_MAX
    rng: random.Random = field(default_factory=random.Random)

    def __post_init__(self) -> None:
        if self.base <= 0 or self.factor <= 1 or self.max_seconds <= 0:
            raise ValueError("backoff requires base > 0, factor > 1, max_seconds > 0")

    def delay_for(self, attempt: int) -> float:
        """Compute the delay (seconds) for the given zero-based attempt count.

        A full-jitter window is used: uniform in [0, cap] where cap is the
        exponential value. Full jitter spreads retries across many monitored
        channels and avoids thundering-herd reconnects after a FloodWait.
        """
        if attempt < 0:
            attempt = 0
        cap = min(self.base * (self.factor ** attempt), self.max_seconds)
        return self.rng.uniform(0, cap)

    def flood_wait_delay(self, wait_seconds: int, attempt: int) -> float:
        """Delay to honor for a FloodWaitError requiring `wait_seconds`.

        We obey Telegram's requested wait, plus a small randomized margin so
        we do not resume at the exact unfreeze instant.
        """
        margin = min(5.0, max(1.0, wait_seconds * 0.1))
        return max(float(wait_seconds) + self.rng.uniform(0, margin), self.delay_for(attempt))
