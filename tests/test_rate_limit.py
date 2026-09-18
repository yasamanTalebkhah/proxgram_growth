"""Unit tests for per-target pacing and global rolling-window rate limiting."""

from __future__ import annotations

import random

from config import TargetConfig
from rate_limit import GlobalPacer, HumanDelay, PerTargetPacer

from conftest import make_config


def test_first_comment_is_immediately_allowed():
    pacer = PerTargetPacer(rng=random.Random(7))
    target = make_config().targets[0]
    assert pacer.acquire(target, now=1000.0) == 0.0


def test_second_comment_within_cooldown_must_wait():
    pacer = PerTargetPacer(rng=random.Random(7))
    target = make_config().targets[0]  # cooldown=600, jitter=0
    pacer.acquire(target, now=1000.0)
    pacer.mark_posted(target, now=1000.0)

    wait = pacer.acquire(target, now=1300.0)  # 5 minutes later
    assert wait == 300.0  # 600s cooldown minus 300s elapsed


def test_comment_allowed_after_cooldown_window():
    pacer = PerTargetPacer(rng=random.Random(7))
    target = make_config().targets[0]
    pacer.acquire(target, now=1000.0)
    pacer.mark_posted(target, now=1000.0)

    assert pacer.acquire(target, now=1000.0 + 600.0) == 0.0


def test_jitter_extends_the_cooldown_window():
    target = TargetConfig(channel="@news", cooldown=600, jitter=120, delay_min=0, delay_max=0)
    pacer = PerTargetPacer(rng=random.Random(7))
    pacer.acquire(target, now=1000.0)
    pacer.mark_posted(target, now=1000.0)

    # With seed 7 the uniform jitter lands in (0, 120]; either way the
    # required wait must exceed the bare cooldown.
    wait = pacer.acquire(target, now=1600.0)  # 100s past bare cooldown
    assert wait > 0.0


def test_targets_are_independent():
    pacer = PerTargetPacer(rng=random.Random(7))
    config = make_config(
        targets=(
            TargetConfig(channel="@alpha", cooldown=600, jitter=0, delay_min=0, delay_max=0),
            TargetConfig(channel="@beta", cooldown=600, jitter=0, delay_min=0, delay_max=0),
        )
    )
    a, b = config.targets
    pacer.acquire(a, now=1000.0)
    pacer.mark_posted(a, now=1000.0)

    # A different channel is not affected by @a's cooldown.
    assert pacer.acquire(b, now=1000.5) == 0.0


def test_same_channel_with_different_spellings_shares_cooldown():
    pacer = PerTargetPacer(rng=random.Random(7))
    a = TargetConfig(channel="@News", cooldown=600, jitter=0, delay_min=0, delay_max=0)
    b = TargetConfig(channel="@news", cooldown=600, jitter=0, delay_min=0, delay_max=0)
    pacer.acquire(a, now=1000.0)
    pacer.mark_posted(a, now=1000.0)

    assert pacer.acquire(b, now=1000.5) > 0.0


def test_global_pacer_blocks_after_max_comments():
    pacer = GlobalPacer(max_comments=3, window_seconds=3600, rng=random.Random(1))
    assert pacer.try_acquire(now=0.0)
    assert pacer.try_acquire(now=1.0)
    assert pacer.try_acquire(now=2.0)
    assert not pacer.try_acquire(now=3.0)


def test_global_pacer_slot_frees_after_window():
    pacer = GlobalPacer(max_comments=2, window_seconds=100, rng=random.Random(1))
    pacer.try_acquire(now=0.0)
    pacer.try_acquire(now=10.0)
    assert not pacer.try_acquire(now=50.0)
    assert pacer.seconds_until_slot(now=50.0) == 50.0  # first event expires at t=100
    assert pacer.try_acquire(now=100.5)


def test_human_delay_is_within_configured_range():
    delay = HumanDelay(min_seconds=5, max_seconds=20, rng=random.Random(42))
    samples = [delay.sample() for _ in range(200)]
    assert all(5.0 <= s <= 20.0 for s in samples)
    assert len(set(samples)) > 100  # genuinely randomized


def test_human_delay_rejects_invalid_range():
    import pytest

    with pytest.raises(ValueError):
        HumanDelay(min_seconds=20, max_seconds=5)
