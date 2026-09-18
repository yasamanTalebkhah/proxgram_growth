"""Configuration schema, loader and validation for the growth worker."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

# Default cooldown between comments in the SAME target channel (seconds).
DEFAULT_PER_CHANNEL_COOLDOWN = 600
# Default jitter window (seconds) applied on top of the base cooldown.
DEFAULT_JITTER = 120
# Default human-like delay range (seconds) before commenting on a fresh post.
DEFAULT_DELAY_MIN = 5
DEFAULT_DELAY_MAX = 20
# FloodWait backoff defaults.
DEFAULT_BACKOFF_BASE = 30
DEFAULT_BACKOFF_FACTOR = 2.0
DEFAULT_BACKOFF_MAX = 3600
# Global cap on comments per rolling window across all targets.
DEFAULT_GLOBAL_WINDOW = 3600
DEFAULT_GLOBAL_MAX = 12


class ConfigError(ValueError):
    """Raised when the growth worker configuration is invalid."""


@dataclass(frozen=True)
class TargetConfig:
    """A single monitored channel (and its linked discussion group)."""

    channel: str
    cooldown: int = DEFAULT_PER_CHANNEL_COOLDOWN
    jitter: int = DEFAULT_JITTER
    delay_min: int = DEFAULT_DELAY_MIN
    delay_max: int = DEFAULT_DELAY_MAX

    def validate(self) -> None:
        if not self.channel or not isinstance(self.channel, str):
            raise ConfigError("target.channel must be a non-empty string")
        if not re.fullmatch(r"@\w{4,64}|https?://t\.me/\w{4,64}|-?\d+", self.channel):
            raise ConfigError(
                f"target.channel {self.channel!r} must be @username, t.me link or numeric id"
            )
        for name in ("cooldown", "jitter", "delay_min", "delay_max"):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 0:
                raise ConfigError(f"target.{name} must be a non-negative integer")
        if self.delay_min > self.delay_max:
            raise ConfigError("target.delay_min must be <= target.delay_max")


@dataclass(frozen=True)
class Config:
    """Growth worker configuration."""

    api_id: int
    api_hash: str
    session_string: str
    destination_channel: str
    targets: tuple[TargetConfig, ...]
    templates: tuple[str, ...]
    log_level: str = "INFO"
    state_file: str | None = None
    dry_run: bool = False
    backoff_base: float = DEFAULT_BACKOFF_BASE
    backoff_factor: float = DEFAULT_BACKOFF_FACTOR
    backoff_max: float = DEFAULT_BACKOFF_MAX
    global_window: int = DEFAULT_GLOBAL_WINDOW
    global_max_comments: int = DEFAULT_GLOBAL_MAX
    redact_logs: bool = True

    def validate(self) -> None:
        if not isinstance(self.api_id, int) or self.api_id <= 0:
            raise ConfigError("api_id must be a positive integer")
        if not self.api_hash or not isinstance(self.api_hash, str):
            raise ConfigError("api_hash must be a non-empty string")
        if not self.session_string or not isinstance(self.session_string, str):
            raise ConfigError("session_string must be a non-empty string")
        if not self.destination_channel:
            raise ConfigError("destination_channel must be a non-empty string")
        if not self.targets:
            raise ConfigError("at least one target channel is required")
        for target in self.targets:
            target.validate()
        if len({t.channel.lstrip("@").split("/")[-1] for t in self.targets}) != len(
            self.targets
        ):
            raise ConfigError("duplicate target channels are not allowed")
        if not self.templates:
            raise ConfigError("at least one comment template is required")
        from .templates import TemplateError, render_template

        sample_context = {
            "channel": "@proxgram",
            "proxy_count": 42,
            "speed_note": "low latency",
        }
        for template in self.templates:
            if not isinstance(template, str) or not template.strip():
                raise ConfigError("templates must be non-empty strings")
            if len(template) > 900:
                raise ConfigError("comment templates must be under 900 characters")
            try:
                render_template(template, sample_context)
            except TemplateError as exc:
                raise ConfigError(f"invalid template {template!r}: {exc}") from None
        if self.backoff_base <= 0 or self.backoff_factor <= 1 or self.backoff_max <= 0:
            raise ConfigError("invalid exponential backoff configuration")
        if self.global_window <= 0 or self.global_max_comments <= 0:
            raise ConfigError("invalid global rate-limit configuration")

    def target_for(self, channel: str) -> TargetConfig | None:
        """Find the target config matching a raw channel identifier."""
        needle = _normalize_channel(channel)
        for target in self.targets:
            if _normalize_channel(target.channel) == needle:
                return target
        return None


def _normalize_channel(channel: str) -> str:
    text = channel.strip()
    if text.startswith("https://t.me/") or text.startswith("http://t.me/"):
        text = "@" + text.split("t.me/", 1)[1].split("?", 1)[0].strip("/")
    if text.startswith("@"):
        text = text[1:].lower()
    return text.lower()


def _require_env(name: str, env: dict[str, str]) -> str:
    value = env.get(name)
    if not value:
        raise ConfigError(
            f"missing required environment variable {name}"
        )
    return value


def _coerce_int(value: Any, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{name} must be an integer") from None


def _coerce_float(value: Any, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{name} must be a number") from None


def config_from_env(env: dict[str, str], *, templates: tuple[str, ...]) -> Config:
    """Build a Config from environment variables plus a template list."""
    targets_raw = _require_env("GROWTH_TARGET_CHANNELS", env)
    targets = tuple(
        TargetConfig(
            channel=part.strip(),
            cooldown=_coerce_int(
                env.get("GROWTH_PER_CHANNEL_COOLDOWN", DEFAULT_PER_CHANNEL_COOLDOWN),
                "GROWTH_PER_CHANNEL_COOLDOWN",
            ),
            jitter=_coerce_int(
                env.get("GROWTH_PER_CHANNEL_JITTER", DEFAULT_JITTER),
                "GROWTH_PER_CHANNEL_JITTER",
            ),
            delay_min=_coerce_int(
                env.get("GROWTH_DELAY_MIN", DEFAULT_DELAY_MIN), "GROWTH_DELAY_MIN"
            ),
            delay_max=_coerce_int(
                env.get("GROWTH_DELAY_MAX", DEFAULT_DELAY_MAX), "GROWTH_DELAY_MAX"
            ),
        )
        for part in targets_raw.split(",")
        if part.strip()
    )
    return Config(
        api_id=_coerce_int(_require_env("API_ID", env), "API_ID"),
        api_hash=_require_env("API_HASH", env),
        session_string=_require_env("SESSION_STRING", env),
        destination_channel=_require_env("GROWTH_DESTINATION_CHANNEL", env),
        targets=targets,
        templates=templates,
        log_level=env.get("GROWTH_LOG_LEVEL", "INFO").upper(),
        state_file=env.get("GROWTH_STATE_FILE") or None,
        dry_run=env.get("GROWTH_DRY_RUN", "").strip().lower() in {"1", "true", "yes"},
        backoff_base=_coerce_float(env.get("GROWTH_BACKOFF_BASE", DEFAULT_BACKOFF_BASE), "GROWTH_BACKOFF_BASE"),
        backoff_factor=_coerce_float(env.get("GROWTH_BACKOFF_FACTOR", DEFAULT_BACKOFF_FACTOR), "GROWTH_BACKOFF_FACTOR"),
        backoff_max=_coerce_float(env.get("GROWTH_BACKOFF_MAX", DEFAULT_BACKOFF_MAX), "GROWTH_BACKOFF_MAX"),
        global_window=_coerce_int(env.get("GROWTH_GLOBAL_WINDOW", DEFAULT_GLOBAL_WINDOW), "GROWTH_GLOBAL_WINDOW"),
        global_max_comments=_coerce_int(env.get("GROWTH_GLOBAL_MAX", DEFAULT_GLOBAL_MAX), "GROWTH_GLOBAL_MAX"),
        redact_logs=env.get("GROWTH_REDACT_LOGS", "1").strip().lower() not in {"0", "false", "no"},
    )


def load_config(
    config_path: str | None = None,
    *,
    env: dict[str, str] | None = None,
    templates_path: str | None = None,
) -> Config:
    """Load configuration from optional JSON file and environment variables.

    Precedence: environment variables override JSON file values for secrets
    (API_ID, API_HASH, SESSION_STRING). Non-secret keys in the JSON file
    (targets, destination, timings) take precedence over env defaults.
    """
    env = dict(os.environ if env is None else env)

    file_values: dict[str, Any] = {}
    if config_path:
        with open(config_path, "r", encoding="utf-8") as fh:
            file_values = json.load(fh)
        if not isinstance(file_values, dict):
            raise ConfigError("config file must contain a JSON object")
        # Allow JSON file to inject env-like values as a lower-precedence layer.
        for key in (
            "API_ID",
            "API_HASH",
            "SESSION_STRING",
            "GROWTH_DESTINATION_CHANNEL",
            "GROWTH_TARGET_CHANNELS",
        ):
            if key in file_values and key not in env:
                env[key] = str(file_values[key])

    template_list: tuple[str, ...] = tuple(file_values.get("templates") or ())
    if templates_path:
        with open(templates_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        items = data.get("templates") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise ConfigError("templates file must be a JSON list or {templates: [...]}")
        template_list = tuple(str(item) for item in items)
    if not template_list:
        # Fall back to bundled defaults so the worker can always start safely.
        from .templates import DEFAULT_TEMPLATES

        template_list = DEFAULT_TEMPLATES

    config = config_from_env(env, templates=template_list)
    config.validate()
    return config
