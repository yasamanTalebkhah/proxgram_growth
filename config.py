"""Centralized environment loader and validator for the growth worker.

Secrets come exclusively from the environment (or a local .env file that is
never committed): TELEGRAM_API_ID, TELEGRAM_API_HASH, SESSION_STRING.
Main-bot credentials (e.g. TELEGRAM_BOT_TOKEN) are deliberately NOT part of
this repository — the growth userbot must stay fully isolated from it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

try:  # python-dotenv is optional; env vars still work without it
    from dotenv import dotenv_values
except ImportError:  # pragma: no cover

    def dotenv_values(_path: str) -> dict[str, str | None]:
        return {}


class ConfigError(ValueError):
    """Raised when the growth worker configuration is invalid."""


# ---- Timing defaults ---------------------------------------------------- #
DEFAULT_PER_CHANNEL_COOLDOWN = 600  # max 1 comment per target per 10 min
DEFAULT_JITTER = 120  # randomized extension of the cooldown window
DEFAULT_DELAY_MIN = 5  # human-like delay before engaging a fresh post
DEFAULT_DELAY_MAX = 20
DEFAULT_BACKOFF_BASE = 30
DEFAULT_BACKOFF_FACTOR = 2.0
DEFAULT_BACKOFF_MAX = 3600
DEFAULT_GLOBAL_WINDOW = 3600
DEFAULT_GLOBAL_MAX = 12  # account-level rolling cap


@dataclass(frozen=True)
class TargetConfig:
    """A single monitored channel."""

    channel: str
    cooldown: int = DEFAULT_PER_CHANNEL_COOLDOWN
    jitter: int = DEFAULT_JITTER
    delay_min: int = DEFAULT_DELAY_MIN
    delay_max: int = DEFAULT_DELAY_MAX

    def validate(self) -> None:
        if not self.channel or not isinstance(self.channel, str):
            raise ConfigError("target.channel must be a non-empty string")
        if not re.fullmatch(r"@\w{4,64}|https?://t\.me/\w{4,64}|(-100)?\d+", self.channel):
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
    """Growth worker configuration (secrets + behaviour)."""

    api_id: int
    api_hash: str
    session_string: str
    destination_channel: str
    targets: tuple[TargetConfig, ...]
    templates: tuple[str, ...]
    log_level: str = "INFO"
    state_file: str = "data/state.json"
    dry_run: bool = False
    backoff_base: float = DEFAULT_BACKOFF_BASE
    backoff_factor: float = DEFAULT_BACKOFF_FACTOR
    backoff_max: float = DEFAULT_BACKOFF_MAX
    global_window: int = DEFAULT_GLOBAL_WINDOW
    global_max_comments: int = DEFAULT_GLOBAL_MAX

    def validate(self) -> None:
        if not isinstance(self.api_id, int) or self.api_id <= 0:
            raise ConfigError("TELEGRAM_API_ID must be a positive integer")
        if not self.api_hash or not isinstance(self.api_hash, str):
            raise ConfigError("TELEGRAM_API_HASH must be a non-empty string")
        if not self.session_string or not isinstance(self.session_string, str):
            raise ConfigError("SESSION_STRING must be a non-empty string")
        if not self.destination_channel:
            raise ConfigError("GROWTH_DESTINATION_CHANNEL must be set")
        if not self.targets:
            raise ConfigError("at least one target channel is required")
        for target in self.targets:
            target.validate()
        normalized = [_normalize_channel(t.channel) for t in self.targets]
        if len(set(normalized)) != len(normalized):
            raise ConfigError("duplicate target channels are not allowed")
        if not self.templates:
            raise ConfigError("at least one comment template is required")
        from templates import TemplateError, render_template

        sample_context = {"channel": "@proxgram", "proxy_count": 42, "speed_note": "low latency"}
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

        # Hard isolation guard: a bot token must never appear in this config.
        if re.search(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}", f"{self.api_hash} {self.session_string}"):
            raise ConfigError(
                "bot-token-like secret detected in client credentials; "
                "this repository must never hold main-bot credentials"
            )

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


def _require(name: str, env: dict[str, str]) -> str:
    value = env.get(name)
    if not value or not value.strip():
        raise ConfigError(f"missing required environment variable {name}")
    return value.strip()


def _int(name: str, env: dict[str, str], default: int) -> int:
    raw = env.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        raise ConfigError(f"{name} must be an integer") from None


def _float(name: str, env: dict[str, str], default: float) -> float:
    raw = env.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(str(raw).strip())
    except ValueError:
        raise ConfigError(f"{name} must be a number") from None


def _bool(name: str, env: dict[str, str], default: bool = False) -> bool:
    raw = env.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def config_from_env(env: dict[str, str], *, templates: tuple[str, ...]) -> Config:
    """Build a Config from an environment mapping plus a template list."""
    targets_raw = _require("GROWTH_TARGET_CHANNELS", env)
    targets = tuple(
        TargetConfig(
            channel=part.strip(),
            cooldown=_int("GROWTH_PER_CHANNEL_COOLDOWN", env, DEFAULT_PER_CHANNEL_COOLDOWN),
            jitter=_int("GROWTH_PER_CHANNEL_JITTER", env, DEFAULT_JITTER),
            delay_min=_int("GROWTH_DELAY_MIN", env, DEFAULT_DELAY_MIN),
            delay_max=_int("GROWTH_DELAY_MAX", env, DEFAULT_DELAY_MAX),
        )
        for part in targets_raw.split(",")
        if part.strip()
    )
    return Config(
        api_id=_int("TELEGRAM_API_ID", env, 0),
        api_hash=_require("TELEGRAM_API_HASH", env),
        session_string=_require("SESSION_STRING", env),
        destination_channel=_require("GROWTH_DESTINATION_CHANNEL", env),
        targets=targets,
        templates=templates,
        log_level=env.get("GROWTH_LOG_LEVEL", "INFO").upper(),
        state_file=env.get("GROWTH_STATE_FILE") or "data/state.json",
        dry_run=_bool("GROWTH_DRY_RUN", env),
        backoff_base=_float("GROWTH_BACKOFF_BASE", env, DEFAULT_BACKOFF_BASE),
        backoff_factor=_float("GROWTH_BACKOFF_FACTOR", env, DEFAULT_BACKOFF_FACTOR),
        backoff_max=_float("GROWTH_BACKOFF_MAX", env, DEFAULT_BACKOFF_MAX),
        global_window=_int("GROWTH_GLOBAL_WINDOW", env, DEFAULT_GLOBAL_WINDOW),
        global_max_comments=_int("GROWTH_GLOBAL_MAX", env, DEFAULT_GLOBAL_MAX),
    )


def load_config(
    config_path: str | None = None,
    *,
    env_file: str = ".env",
    templates_path: str | None = None,
    environ: dict[str, str] | None = None,
) -> Config:
    """Load configuration: process env > .env file > JSON config > defaults.

    The .env file is read with dotenv_values (pure parse, no mutation of the
    real process environment), which keeps loading deterministic and safe.
    """
    env = dict(os.environ if environ is None else environ)
    if env_file:
        for key, value in dotenv_values(env_file).items():
            if value is not None and key not in env:
                env[key] = value

    file_values: dict = {}
    if config_path:
        import json

        with open(config_path, "r", encoding="utf-8") as fh:
            file_values = json.load(fh)
        if not isinstance(file_values, dict):
            raise ConfigError("config file must contain a JSON object")
        for key in ("GROWTH_TARGET_CHANNELS", "GROWTH_DESTINATION_CHANNEL"):
            if key in file_values and key not in env:
                env[key] = str(file_values[key])

    template_list: tuple[str, ...] = ()
    if templates_path:
        import json

        with open(templates_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        items = data.get("templates") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise ConfigError("templates file must be a JSON list or {templates: [...]}")
        template_list = tuple(str(item) for item in items)
    if not template_list:
        from templates import DEFAULT_TEMPLATES

        template_list = DEFAULT_TEMPLATES

    config = config_from_env(env, templates=template_list)
    config.validate()
    return config
