"""Persistent JSON-based state tracker.

Maintains per-target last-comment timestamps, last-used templates and the
set of already-processed channel/post IDs across restarts, so the worker
never double-comments after a restart. The file stores only operational
metadata — never credentials.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Any

STATE_SCHEMA_VERSION = 1
MAX_TRACKED_POST_IDS = 5000  # prune bound per channel


def _norm(channel: str) -> str:
    text = channel.strip()
    if text.startswith("@"):
        text = text[1:]
    return text.lower()


class StateManager:
    """JSON-file backed store: cooldowns, last templates, processed posts."""

    def __init__(self, path: str | None) -> None:
        self.path = path
        self._data: dict[str, Any] = {}
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    self._data = json.load(fh)
            except (json.JSONDecodeError, OSError):
                # Corrupt state must never wedge the worker; start clean.
                self._data = {}
        if "version" not in self._data:
            self._data["version"] = STATE_SCHEMA_VERSION
        self._data.setdefault("last_post", {})
        self._data.setdefault("last_template", {})
        self._data.setdefault("processed_posts", {})

    # -- generic --------------------------------------------------------- #

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def save(self) -> None:
        if not self.path:
            return
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".state_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self.path)
        except OSError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # -- cooldowns --------------------------------------------------------- #

    def last_post_at(self, channel: str) -> float | None:
        value = self._data["last_post"].get(_norm(channel))
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def remember_post(self, channel: str, *, at: float | None = None) -> None:
        self._data["last_post"][_norm(channel)] = at if at is not None else time.time()
        self.save()

    # -- template rotation --------------------------------------------------- #

    def last_template(self, channel: str) -> str | None:
        value = self._data["last_template"].get(_norm(channel))
        return str(value) if value is not None else None

    def remember_template(self, channel: str, template: str) -> None:
        self._data["last_template"][_norm(channel)] = template
        self.save()

    # -- processed channel/post ids ----------------------------------------- #

    def is_post_processed(self, channel: str, post_id: int) -> bool:
        """True when this channel/post id was already handled."""
        key = str(_norm(channel))
        ids = self._data["processed_posts"].get(key)
        if not ids:
            return False
        return str(post_id) in ids

    def mark_post_processed(self, channel: str, post_id: int) -> None:
        """Record a channel/post id as handled and prune old entries."""
        key = str(_norm(channel))
        ids: dict = self._data["processed_posts"].setdefault(key, {})
        ids[str(post_id)] = time.time()
        if len(ids) > MAX_TRACKED_POST_IDS:
            for stale in sorted(ids, key=ids.get)[: len(ids) // 2]:
                del ids[stale]
        self.save()

    def forget_channel(self, channel: str) -> None:
        """Drop all state for one channel (e.g. after target removal)."""
        key = _norm(channel)
        self._data["last_post"].pop(key, None)
        self._data["last_template"].pop(key, None)
        self._data["processed_posts"].pop(key, None)
        self.save()
