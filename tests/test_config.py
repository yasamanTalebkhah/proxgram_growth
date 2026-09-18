"""Unit tests for configuration loading and validation."""

from __future__ import annotations

import pytest

from proxgram_growth.config import (
    ConfigError,
    TargetConfig,
    config_from_env,
    load_config,
)
from proxgram_growth.templates import DEFAULT_TEMPLATES

BASE_ENV = {
    "API_ID": "123456",
    "API_HASH": "b" * 32,
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
    env = dict(BASE_ENV)
    del env["SESSION_STRING"]
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


def test_default_templates_pass_validation():
    config = config_from_env(BASE_ENV, templates=DEFAULT_TEMPLATES)
    config.validate()  # must not raise


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


def test_load_config_from_json_file(tmp_path):
    import json

    cfg_file = tmp_path / "growth_worker.json"
    cfg_file.write_text(
        json.dumps(
            {
                "GROWTH_TARGET_CHANNELS": "@news",
                "GROWTH_DESTINATION_CHANNEL": "@proxgram",
                "API_ID": 123456,
                "API_HASH": "b" * 32,
                "SESSION_STRING": "s" * 40,
            }
        ),
        encoding="utf-8",
    )
    config = load_config(str(cfg_file), env={}, templates_path=None)
    assert config.destination_channel == "@proxgram"
    assert config.targets[0].channel == "@news"


def test_load_config_env_overrides_file_secrets(tmp_path):
    import json

    cfg_file = tmp_path / "growth_worker.json"
    cfg_file.write_text(
        json.dumps({"API_ID": 111, "API_HASH": "file-hash", "SESSION_STRING": "file-session",
                    "GROWTH_TARGET_CHANNELS": "@news", "GROWTH_DESTINATION_CHANNEL": "@proxgram"}),
        encoding="utf-8",
    )
    env = {"API_ID": "222", "API_HASH": "env-hash", "SESSION_STRING": "env-session"}
    config = load_config(str(cfg_file), env=env)
    assert config.api_id == 222
    assert config.api_hash == "env-hash"
    assert config.session_string == "env-session"
