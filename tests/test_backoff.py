from src.core.backoff import ExponentialBackoff


def test_backoff_computation():
    bo = ExponentialBackoff(base_delay=1.0, max_delay=10.0, factor=2.0, jitter=False)
    assert bo.compute_delay(0) == 1.0
    assert bo.compute_delay(1) == 2.0
    assert bo.compute_delay(2) == 4.0
    assert bo.compute_delay(4) == 10.0


def test_backoff_jitter():
    bo = ExponentialBackoff(base_delay=2.0, max_delay=10.0, factor=2.0, jitter=True)
    delay = bo.compute_delay(1)
    assert 2.0 <= delay <= 4.0
