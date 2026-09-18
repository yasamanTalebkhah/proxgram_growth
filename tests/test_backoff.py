"""Unit tests for exponential backoff and FloodWait handling."""

from __future__ import annotations

import random

import pytest

from proxgram_growth.backoff import BackoffPolicy


def test_delay_grows_exponentially():
    policy = BackoffPolicy(base=30, factor=2.0, max_seconds=3600, rng=random.Random(5))
    caps = [min(30 * 2**a, 3600) for a in range(6)]
    for attempt, cap in enumerate(caps):
        delay = policy.delay_for(attempt)
        assert 0 <= delay <= cap


def test_delay_is_capped():
    policy = BackoffPolicy(base=30, factor=2.0, max_seconds=60, rng=random.Random(5))
    for attempt in range(10):
        assert policy.delay_for(attempt) <= 60


def test_flood_wait_honors_telegram_wait():
    policy = BackoffPolicy(base=30, factor=2.0, max_seconds=3600, rng=random.Random(5))
    delay = policy.flood_wait_delay(wait_seconds=120, attempt=0)
    assert delay >= 120.0


def test_flood_wait_adds_margin_but_stays_bounded():
    policy = BackoffPolicy(base=30, factor=2.0, max_seconds=3600, rng=random.Random(5))
    for _ in range(20):
        delay = policy.flood_wait_delay(wait_seconds=60, attempt=0)
        assert 60.0 <= delay <= 60.0 + 6.0 + 30.0  # wait + max margin + exp cap


def test_invalid_policy_rejected():
    with pytest.raises(ValueError):
        BackoffPolicy(base=0, factor=2.0)
    with pytest.raises(ValueError):
        BackoffPolicy(base=30, factor=0.5)
