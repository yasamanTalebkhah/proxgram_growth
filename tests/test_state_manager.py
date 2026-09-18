"""Unit tests for the StateManager persistence layer."""

from __future__ import annotations

import json

from state_manager import StateManager


def test_state_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    store = StateManager(str(path))
    store.remember_post("@News", at=1000.0)
    store.remember_template("@News", "Hello {channel}")

    store2 = StateManager(str(path))
    assert store2.last_post_at("@news") == 1000.0
    assert store2.last_template("@News") == "Hello {channel}"


def test_corrupt_state_starts_clean(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    store = StateManager(str(path))
    assert store.last_post_at("@news") is None


def test_missing_state_file_is_fine(tmp_path):
    store = StateManager(str(tmp_path / "missing.json"))
    assert store.last_post_at("@news") is None


def test_in_memory_store_never_touches_disk(tmp_path):
    store = StateManager(None)
    store.remember_post("@news", at=1.0)
    assert store.last_post_at("@news") == 1.0
    assert not list(tmp_path.iterdir())


def test_state_file_contains_no_credentials(tmp_path):
    path = tmp_path / "state.json"
    store = StateManager(str(path))
    store.remember_post("@news", at=1.0)
    data = json.loads(path.read_text(encoding="utf-8"))
    keys = json.dumps(data).lower()
    assert "session" not in keys and "api_hash" not in keys


# --------------------------------------------------------------------- #
# Processed channel/post ids
# --------------------------------------------------------------------- #


def test_processed_post_roundtrip(tmp_path):
    store = StateManager(str(tmp_path / "state.json"))
    assert not store.is_post_processed("@news", 42)
    store.mark_post_processed("@news", 42)
    store.save()

    store2 = StateManager(str(tmp_path / "state.json"))
    assert store2.is_post_processed("@news", 42)
    assert not store2.is_post_processed("@news", 43)


def test_processed_ids_are_per_channel(tmp_path):
    store = StateManager(str(tmp_path / "state.json"))
    store.mark_post_processed("@news", 42)
    assert not store.is_post_processed("@other", 42)


def test_processed_ids_prune_to_bound(tmp_path):
    from state_manager import MAX_TRACKED_POST_IDS

    store = StateManager(str(tmp_path / "state.json"))
    for i in range(MAX_TRACKED_POST_IDS + 400):
        store.mark_post_processed("@news", i)
    assert len(store._data["processed_posts"]["news"]) <= MAX_TRACKED_POST_IDS
    # Oldest half pruned, recent ones retained.
    assert store.is_post_processed("@news", MAX_TRACKED_POST_IDS + 399)


def test_forget_channel_clears_all_state(tmp_path):
    store = StateManager(str(tmp_path / "state.json"))
    store.remember_post("@news", at=1.0)
    store.mark_post_processed("@news", 1)
    store.forget_channel("@news")
    assert store.last_post_at("@news") is None
    assert not store.is_post_processed("@news", 1)
