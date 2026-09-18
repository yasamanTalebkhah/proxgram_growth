"""State persistence so cooldowns survive worker restarts.

The file stores only timestamps and message ids — never credentials.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Any


class StateStore:
    """Tiny JSON-file backed store for per-target last-comment timestamps."""

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

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def save(self) -> None:
        if not self.path:
            return
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".growth_state_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self.path)
        except OSError:
            # Best-effort persistence; losing state only risks one extra comment.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # Convenience helpers used by the worker -------------------------------

    def last_post_at(self, channel: str) -> float | None:
        value = self.get(f"last_post:{_norm(channel)}")
        if value is None:
            return None
        try:
            return float(value)
            # Note: wall-clock (time.time) is persisted, not monotonic.
        except (TypeError, ValueError):
            return None

    def remember_post(self, channel: str, *, at: float | None = None) -> None:
        self.set(f"last_post:{_norm(channel)}", at if at is not None else time.time())
        self.save()

    def last_template(self, channel: str) -> str | None:
        value = self.get(f"last_template:{_norm(channel)}")
        return str(value) if value is not None else None

    def remember_template(self, channel: str, template: str) -> None:
        self.set(f"last_template:{_norm(channel)}", template)
        self.save()


def _norm(channel: str) -> str:
    text = channel.strip()
    if text.startswith("@"):
        text = text[1:]
    return text.lower()
