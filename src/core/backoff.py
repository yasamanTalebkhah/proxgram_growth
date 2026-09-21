import random


class ExponentialBackoff:
    def __init__(self, base_delay: float = 2.0, max_delay: float = 60.0, factor: float = 2.0, jitter: bool = True):
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.factor = factor
        self.jitter = jitter

    def compute_delay(self, attempt: int) -> float:
        delay = min(self.base_delay * (self.factor ** attempt), self.max_delay)
        if self.jitter:
            delay = delay * (0.5 + random.random() * 0.5)
        return round(delay, 2)
