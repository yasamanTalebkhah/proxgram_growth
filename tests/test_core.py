import pytest
from datetime import datetime
from unittest.mock import patch
from src.core.templates import SpintaxEngine
from src.core.rate_limiter import AntiSpamLimiter

def test_spintax_engine_basic_and_variables():
    template = "{سلام|درود} دوستان! {کانال ما|منبع}: {channel_link}"
    rendered = SpintaxEngine.render_promo(template, "@proxgram")
    assert "@proxgram" in rendered
    assert ("سلام" in rendered or "درود" in rendered)
    assert ("کانال ما" in rendered or "منبع" in rendered)
    assert "{" not in rendered and "}" not in rendered

def test_rate_limiter_quiet_hours_standard():
    limiter = AntiSpamLimiter(quiet_start=1, quiet_end=6)
    with patch("src.core.rate_limiter.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 1, 1, 3, 0)
        assert limiter.is_quiet_hours() is True
        mock_dt.now.return_value = datetime(2026, 1, 1, 12, 0)
        assert limiter.is_quiet_hours() is False

def test_rate_limiter_quiet_hours_wrap_around():
    limiter = AntiSpamLimiter(quiet_start=23, quiet_end=6)
    with patch("src.core.rate_limiter.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 1, 1, 23, 30)
        assert limiter.is_quiet_hours() is True
        mock_dt.now.return_value = datetime(2026, 1, 1, 2, 0)
        assert limiter.is_quiet_hours() is True
        mock_dt.now.return_value = datetime(2026, 1, 1, 15, 0)
        assert limiter.is_quiet_hours() is False
