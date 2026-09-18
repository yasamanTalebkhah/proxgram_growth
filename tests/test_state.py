"""Unit tests for the state store persistence."""

from __future__ import annotations

from proxgram_growth.state import StateStore


def test_state_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    store = StateStore(str(path))
    store.remember_post("@News", at=1000.0)
    store.remember_template("@News", "Hello {channel}")

    store2 = StateStore(str(path))
    assert store2.last_post_at("@news") == 1000.0
    assert store2.last_template("@News") == "Hello {channel}"


def test_corrupt_state_starts_clean(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    store = StateStore(str(path))
    assert store.last_post_at("@news") is None


def test_missing_state_file_is_fine(tmp_path):
    store = StateStore(str(tmp_path / "missing.json"))
    assert store.last_post_at("@news") is None


def test_in_memory_store_never_touches_disk(tmp_path):
    store = StateStore(None)
    store.remember_post("@news", at=1.0)
    assert store.last_post_at("@news") == 1.0
    assert not list(tmp_path.iterdir())


def test_state_file_contains_no_credentials(tmp_path):
    import json

    path = tmp_path / "state.json"
    store = StateStore(str(path))
    store.remember_post("@news", at=1.0)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert all("session" not in k.lower() and "api" not in k.lower() for k in data)
