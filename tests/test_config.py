"""Unit tests for configuration loading and validation."""

from __future__ import annotations

import pytest

from config import (
    ConfigError,
    TargetConfig,
    config_from_env,
    load_config,
)
from templates import DEFAULT_TEMPLATES

BASE_ENV = {
    "TELEGRAM_API_ID": "123456",
    "TELEGRAM_API_HASH": "b" * 32,
    "SESSION_STRING": "s" * 40,
    "GROWTH_DESTINATION_CHANNEL": "@proxgram",
    "GROWTH_TARGET_CHANNELS": "@news, https://t.me/markets_daily",
}


def test_env_config_builds_targets_and_defaults():
    config = config_from_env(BASE_ENV, templates=DEFAULT_TEMPLATES)
    config.validate()
    assert [t.channel for t in config.targets] == ["@news", "https://t.me/markets_daily"]
    assert config.destination_channel == "@proxgram"
    assert config.targets[0].cooldown == 600
    assert config.global_max_comments == 12


def test_missing_required_env_raises():
    for required in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "SESSION_STRING",
                     "GROWTH_DESTINATION_CHANNEL", "GROWTH_TARGET_CHANNELS"):
        env = {k: v for k, v in BASE_ENV.items() if k != required}
        with pytest.raises(ConfigError):
            config_from_env(env, templates=DEFAULT_TEMPLATES).validate()


def test_invalid_target_format_rejected():
    with pytest.raises(ConfigError):
        TargetConfig(channel="not a channel!").validate()


def test_minimum_length_targets_accepted():
    TargetConfig(channel="@abcd").validate()
    TargetConfig(channel="https://t.me/abcd").validate()


def test_numeric_channel_ids_accepted():
    TargetConfig(channel="-1001234567890").validate()


def test_delay_range_must_be_ordered():
    with pytest.raises(ConfigError):
        TargetConfig(channel="@news", delay_min=20, delay_max=5).validate()


def test_duplicate_targets_rejected():
    env = dict(BASE_ENV, GROWTH_TARGET_CHANNELS="@news, @news")
    config = config_from_env(env, templates=DEFAULT_TEMPLATES)
    with pytest.raises(ConfigError):
        config.validate()


def test_empty_templates_rejected():
    config = config_from_env(BASE_ENV, templates=())
    with pytest.raises(ConfigError):
        config.validate()


def test_template_placeholder_validation():
    config = config_from_env(BASE_ENV, templates=("Hi {unknown_var}",))
    with pytest.raises(ConfigError):
        config.validate()


def test_overlong_template_rejected():
    config = config_from_env(BASE_ENV, templates=("x" * 901,))
    with pytest.raises(ConfigError):
        config.validate()


def test_default_templates_pass_validation():
    config = config_from_env(BASE_ENV, templates=DEFAULT_TEMPLATES)
    config.validate()  # must not raise


# --------------------------------------------------------------------- #
# Isolation guard: bot tokens must never be accepted
# --------------------------------------------------------------------- #


def test_bot_token_in_session_string_rejected():
    token = "1234567890:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
    env = dict(BASE_ENV, SESSION_STRING=token)
    with pytest.raises(ConfigError):
        config_from_env(env, templates=DEFAULT_TEMPLATES).validate()


def test_bot_token_in_api_hash_rejected():
    env = dict(BASE_ENV, TELEGRAM_API_HASH="1234567890:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw")
    with pytest.raises(ConfigError):
        config_from_env(env, templates=DEFAULT_TEMPLATES).validate()


# --------------------------------------------------------------------- #
# File loading
# --------------------------------------------------------------------- #


def test_load_config_from_env_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "TELEGRAM_API_ID=123456",
                "TELEGRAM_API_HASH=" + "b" * 32,
                "SESSION_STRING=" + "s" * 40,
                "GROWTH_DESTINATION_CHANNEL=@proxgram",
                "GROWTH_TARGET_CHANNELS=@news",
            ]
        ),
        encoding="utf-8",
    )
    config = load_config(env_file=str(env_file), environ={})
    assert config.destination_channel == "@proxgram"
    assert config.targets[0].channel == "@news"


def test_process_env_takes_precedence_over_env_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "TELEGRAM_API_ID=123456",
                "TELEGRAM_API_HASH=" + "b" * 32,
                "SESSION_STRING=" + "s" * 40,
                "GROWTH_DESTINATION_CHANNEL=@fromfile",
                "GROWTH_TARGET_CHANNELS=@news",
            ]
        ),
        encoding="utf-8",
    )
    config = load_config(
        env_file=str(env_file), environ=dict(BASE_ENV, GROWTH_DESTINATION_CHANNEL="@fromenv")
    )
    assert config.destination_channel == "@fromenv"


def test_load_config_json_file_overrides(tmp_path):
    import json

    cfg_file = tmp_path / "growth_worker.json"
    cfg_file.write_text(
        json.dumps({"GROWTH_TARGET_CHANNELS": "@news", "GROWTH_DESTINATION_CHANNEL": "@proxgram"}),
        encoding="utf-8",
    )
    config = load_config(str(cfg_file), env_file=str(tmp_path / "missing.env"), environ=BASE_ENV)
    assert config.destination_channel == "@proxgram"
    assert config.targets[0].channel == "@news"


def test_load_config_env_overrides_file(tmp_path):
    import json

    cfg_file = tmp_path / "growth_worker.json"
    cfg_file.write_text(
        json.dumps({"GROWTH_TARGET_CHANNELS": "@fromfile", "GROWTH_DESTINATION_CHANNEL": "@x"}),
        encoding="utf-8",
    )
    config = load_config(str(cfg_file), env_file=str(tmp_path / "missing.env"), environ=BASE_ENV)
    # Env takes precedence over the JSON file.
    assert config.destination_channel == "@proxgram"


def test_templates_file_loading(tmp_path):
    import json

    tpl_file = tmp_path / "templates.json"
    tpl_file.write_text(
        json.dumps({"templates": ["Visit {channel} for {proxy_count} proxies"]}),
        encoding="utf-8",
    )
    config = load_config(
        templates_path=str(tpl_file), env_file=str(tmp_path / "missing.env"), environ=BASE_ENV
    )
    assert config.templates == ("Visit {channel} for {proxy_count} proxies",)
