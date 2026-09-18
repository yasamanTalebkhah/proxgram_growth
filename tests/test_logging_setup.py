"""Unit tests for credential redaction in logging."""

from __future__ import annotations

import logging

from proxgram_growth.logging_setup import SecretFilter, configure_logging, redact

SESSION = "1ApWapzMBuVerySecretSessionString1234567890"
API_HASH = "0123456789abcdef0123456789abcdef"


def test_session_string_is_scrubbed_from_message():
    filt = SecretFilter([SESSION])
    record = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "Session failed: %s", (SESSION,), None
    )
    assert filt.filter(record)
    assert SESSION not in str(record.msg)
    assert SESSION not in str(record.args)


def test_partial_session_fragments_are_scrubbed():
    filt = SecretFilter([SESSION])
    leak = f"traceback referenced {SESSION[:24]} and more"
    record = logging.LogRecord("test", logging.ERROR, __file__, 1, leak, None, None)
    filt.filter(record)
    assert SESSION[:24] not in str(record.msg)


def test_short_values_are_not_matched():
    filt = SecretFilter(["abc"])
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "abc def", None, None)
    filt.filter(record)
    assert "abc def" in str(record.msg)


def test_redact_helper():
    text = f"token={SESSION} api={API_HASH}"
    scrubbed = redact(text, [SESSION, API_HASH])
    assert SESSION not in scrubbed
    assert API_HASH not in scrubbed


def test_configure_logging_attaches_redacting_handler(tmp_path):
    log_file = tmp_path / "growth.log"
    logger = configure_logging("DEBUG", secrets=[SESSION], log_file=str(log_file))
    logger.info("started with %s", SESSION)
    for handler in logger.handlers:
        handler.flush()
    content = log_file.read_text(encoding="utf-8")
    assert SESSION not in content
    assert "started with" in content
