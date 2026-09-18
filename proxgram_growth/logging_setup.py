"""Secure logging setup with automatic credential redaction."""

from __future__ import annotations

import logging
import re
from typing import Iterable

# We deliberately refuse to log these values. The session string is the
# equivalent of a full account takeover credential: if it ever reaches
# logs, crash reports or CI output, the account is compromised.
SENSITIVE_ENV_KEYS = (
    "SESSION_STRING",
    "API_HASH",
    "API_ID",
    "GROWTH_TG_PROXY",
)

_MARKER = "«redacted»"


def build_secret_patterns(values: Iterable[str]) -> list[re.Pattern[str]]:
    """Compile regex patterns that hide given secret strings in any log line."""
    patterns: list[re.Pattern[str]] = []
    for value in values:
        if not value or len(value) < 6:
            continue
        escaped = re.escape(value)
        patterns.append(re.compile(escaped))
        # Also redact base64url fragments of a session string, so partially
        # rotated or truncated credentials never leak either.
        if len(value) >= 24:
            chunk = escaped[:24]
            patterns.append(re.compile(chunk))
    return patterns


class SecretFilter(logging.Filter):
    """Logging filter that scrubs known secrets from every record."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self._patterns = build_secret_patterns(secrets)

    def filter(self, record: logging.LogRecord) -> bool:
        if self._patterns:
            record.msg = self._scrub(record.msg)
            if record.args:
                record.args = tuple(self._scrub(arg) if isinstance(arg, str) else arg for arg in record.args)
        return True

    def _scrub(self, value: object) -> str:
        text = value if isinstance(value, str) else str(value)
        for pattern in self._patterns:
            text = pattern.sub(_MARKER, text)
        return text


def configure_logging(
    level: str = "INFO",
    *,
    secrets: Iterable[str] = (),
    log_file: str | None = None,
) -> logging.Logger:
    """Configure the growth worker logger with redaction enabled."""
    logger = logging.getLogger("proxgram.growth")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(SecretFilter(secrets))
    logger.addHandler(console)

    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.addFilter(SecretFilter(secrets))
        logger.addHandler(file_handler)

    logger.propagate = False
    return logger


def redact(text: str, secrets: Iterable[str]) -> str:
    """Utility for scrubbing secrets from arbitrary strings (e.g. exceptions)."""
    filter_ = SecretFilter(secrets)
    return filter_._scrub(text)
