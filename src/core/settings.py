"""Runtime settings engine for the dashboard (DB-backed, env fallback).

Growth behaviour knobs live in the `system_settings` table so the
dashboard can edit them without touching files or restarting services.
`get_setting()` reads DB first and falls back to environment (so a
fresh deployment with no dashboard ever behaves exactly as before),
and `set_setting()` upserts + invalidates the cache.
"""

import os
from typing import Dict, Optional

from src.database.connection import get_db_connection

# Editable keys the dashboard exposes, with metadata for the form UI.
SETTING_DEFS = [
    {"key": "SCHEDULER_INTERVAL", "label": "Scheduler interval (s)",
     "type": "int", "min": 60, "max": 86400, "default": 3600,
     "help": "How often the worker refreshes the task queue. Takes effect on worker reload."},
    {"key": "GROWTH_CLAIM_TIMEOUT_MINUTES", "label": "Claim timeout (min)",
     "type": "int", "min": 1, "max": 120, "default": 10,
     "help": "RUNNING tasks older than this are treated as orphaned and recovered."},
    {"key": "GROWTH_SWEEP_INTERVAL_SECONDS", "label": "Sweep interval (s)",
     "type": "int", "min": 30, "max": 3600, "default": 300,
     "help": "How often orphan recovery + failed-task requeue runs."},
    {"key": "GROWTH_MAX_RETRIES", "label": "Max retries",
     "type": "int", "min": 1, "max": 10, "default": 3,
     "help": "Attempts before a task is terminally FAILED (sweeper + requeue)."},
    {"key": "GROWTH_REQUEUE_BASE_DELAY_SECONDS", "label": "Requeue base delay (s)",
     "type": "int", "min": 5, "max": 3600, "default": 60,
     "help": "Exponential backoff base for failed tasks: base * 2^retry_count."},
    {"key": "GROWTH_CONNECT_TIMEOUT_SECONDS", "label": "Connect timeout (s)",
     "type": "int", "min": 5, "max": 120, "default": 20,
     "help": "Hard cap per Telethon connection attempt (all transports)."},
    {"key": "GROWTH_SEED_DEDUPE_HOURS", "label": "Seed dedupe window (h)",
     "type": "int", "min": 1, "max": 168, "default": 24,
     "help": "Skip seeding targets with a PENDING/COMPLETED task newer than this."},
]

_CACHE: Optional[Dict[str, str]] = None


def _load_cache() -> Dict[str, str]:
    global _CACHE
    if _CACHE is None:
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT key, value FROM system_settings;")
                    _CACHE = dict(cur.fetchall())
        except Exception:
            _CACHE = {}
    return _CACHE


def invalidate_cache() -> None:
    global _CACHE
    _CACHE = None


def get_setting(key: str) -> str:
    """DB value -> env fallback -> SETTING_DEFS default. Never raises."""
    cache = _load_cache()
    if key in cache:
        return cache[key]
    env = os.getenv(key)
    if env not in (None, ""):
        return env
    for definition in SETTING_DEFS:
        if definition["key"] == key:
            return str(definition["default"])
    raise KeyError(f"Unknown setting: {key}")


def get_setting_int(key: str) -> int:
    return int(get_setting(key))


def set_setting(key: str, value: str) -> None:
    if key not in {d["key"] for d in SETTING_DEFS}:
        raise KeyError(f"Setting not editable: {key}")
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO system_settings (key, value, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (key) DO UPDATE
                SET value = EXCLUDED.value, updated_at = NOW();
                """,
                (key, value),
            )
        conn.commit()
    invalidate_cache()


def apply_db_settings_to_env() -> None:
    """Publish DB settings into the process env so every module that
    reads `os.getenv(...)` sees dashboard overrides without restarts."""
    cache = _load_cache()
    for key in cache:
        try:
            get_setting(key)
        except KeyError:
            continue
        value = get_setting(key)
        if value not in (None, ""):
            os.environ[key] = value


def get_worker_restart_flag() -> bool:
    cache = _load_cache()
    return cache.get("WORKER_RESTART_REQUESTED") == "true"


def set_worker_restart_flag(value: bool) -> None:
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO system_settings (key, value, updated_at)
                    VALUES ('WORKER_RESTART_REQUESTED', %s, NOW())
                    ON CONFLICT (key) DO UPDATE
                    SET value = EXCLUDED.value, updated_at = NOW();
                    """,
                    ("true" if value else "false",),
                )
            conn.commit()
    except Exception:
        pass
    invalidate_cache()
