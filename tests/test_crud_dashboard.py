"""Tests for dashboard CRUD controls: task purge, canonical templates API,
channel purge/toggle (DB + Redis mocked)."""

import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

import src.dashboard.services as services
from src.dashboard.app import app

client = TestClient(app)


def _db(fetchall=None, fetchone=(0,), rowcount=1):
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_cur.fetchall.return_value = fetchall or []
    mock_cur.fetchone.return_value = fetchone
    mock_cur.rowcount = rowcount
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur

    @contextmanager
    def fake_conn():
        yield mock_conn

    return mock_conn, mock_cur, fake_conn


# ------------------------------------------------------------ task purge ---

def test_purge_all_tasks_truncates_resets_and_flushes_redis():
    conn, cur, fake = _db(fetchone=(42,))
    fake_redis = MagicMock()
    fake_redis.scan_iter.return_value = iter(["task:1", "task:2", "task:3"])
    fake_redis.delete.return_value = 3

    with patch("src.dashboard.services.get_db_connection", fake), \
         patch("src.core.redis_client.get_redis_client", return_value=fake_redis):
        result = services.purge_all_tasks()

    assert result == {"cleared": 42, "redis_keys": 3}
    sqls = [c.args[0] for c in cur.execute.call_args_list]
    assert any("TRUNCATE tasks RESTART IDENTITY" in s for s in sqls)
    assert any("TASKS_PURGED" in s for s in sqls)
    fake_redis.delete.assert_called_once_with("task:1", "task:2", "task:3")


def test_purge_all_tasks_survives_redis_outage():
    conn, cur, fake = _db(fetchone=(7,))

    with patch("src.dashboard.services.get_db_connection", fake), \
         patch("src.core.redis_client.get_redis_client",
               side_effect=ConnectionError("redis down")):
        result = services.purge_all_tasks()

    assert result == {"cleared": 7, "redis_keys": 0}
    sqls = [c.args[0] for c in cur.execute.call_args_list]
    assert any("TRUNCATE tasks RESTART IDENTITY" in s for s in sqls)


def test_purge_tasks_endpoint_returns_counts():
    with patch("src.dashboard.app.purge_all_tasks",
               return_value={"cleared": 9, "redis_keys": 2}):
        resp = client.post("/api/tasks/purge")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "cleared": 9, "redis_keys": 2}


def test_seed_endpoint_delegates_to_seeder():
    with patch("src.dashboard.app.seed_tasks",
               return_value={"seeded": [("-1001", 5)], "skipped": {"-1002": "dupe"}}):
        data = client.post("/api/tasks/seed?force=true").json()
    assert data["seeded"] == [{"task_id": 5, "target": "-1001"}]
    assert data["skipped"] == {"-1002": "dupe"}


# ------------------------------------------------- canonical templates API --

_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_templates_crud_via_canonical_routes():
    conn, cur, fake = _db(fetchall=[(1, "T", "{a|b}", True, _TS, _TS)])
    with patch("src.dashboard.services.get_db_connection", fake):
        listing = client.get("/api/templates").json()
        assert listing["templates"][0]["created"] is not None  # created date exposed

        client.post("/api/templates", data={"name": "N", "template": "{x|y}",
                                            "is_active": "true"})
        client.put("/api/templates/1", data={"name": "N2", "template": "{x|z}",
                                             "is_active": "false"})
        client.patch("/api/templates/1/toggle")
        client.delete("/api/templates/1")

    sqls = [c.args[0] for c in cur.execute.call_args_list]
    assert any("INSERT INTO message_templates" in s for s in sqls)
    assert any("SET name = %s" in s for s in sqls)
    assert any("SET template = %s" in s for s in sqls)
    # Exclusive activation on create-with-active and on activate
    assert sqls.count("UPDATE message_templates SET is_active = FALSE;") >= 2
    assert any("DELETE FROM message_templates" in s for s in sqls)


def test_template_put_updates_name_body_and_active():
    conn, cur, fake = _db(fetchone=(1,))
    with patch("src.dashboard.services.get_db_connection", fake):
        assert client.put("/api/templates/3", data={
            "name": "renamed", "template": "{a}", "is_active": "true"
        }).status_code == 200
    calls = cur.execute.call_args_list
    assert any("SET name = %s" in c.args[0] for c in calls)
    assert any("SET template = %s" in c.args[0] for c in calls)
    activate = [c for c in calls if "is_active = %s, updated_at" in c.args[0]]
    assert activate and activate[0].args[1] == (True, 3)


def test_template_put_missing_template_returns_404():
    conn, cur, fake = _db(fetchone=None)
    with patch("src.dashboard.services.get_db_connection", fake):
        resp = client.put("/api/templates/999", data={"name": "x"})
    assert resp.status_code == 404


def test_template_put_with_no_fields_rejected():
    resp = client.put("/api/templates/1", data={})
    assert resp.status_code == 400


def test_template_toggle_patch_route_exists():
    conn, cur, fake = _db(fetchall=[(False,)])
    with patch("src.dashboard.services.get_db_connection", fake):
        resp = client.patch("/api/templates/2/toggle")
    assert resp.status_code == 200


def test_studio_legacy_template_routes_still_work():
    conn, cur, fake = _db(fetchall=[(1, "T", "{a|b}", False, _TS, _TS)])
    with patch("src.dashboard.services.get_db_connection", fake):
        assert client.get("/api/studio/templates").status_code == 200
        assert client.post("/api/studio/templates",
                           data={"name": "A", "template": "b"}).status_code == 200
        assert client.post("/api/studio/templates/1",
                           data={"template": "c"}).status_code == 200
        assert client.post("/api/studio/templates/1/toggle").status_code == 200
        assert client.delete("/api/studio/templates/1").status_code == 200


# ------------------------------------------------------------- channels ----

def test_channel_purge_clears_all_targets():
    conn, cur, fake = _db(fetchone=(12,))
    with patch("src.dashboard.services.get_db_connection", fake):
        resp = client.post("/api/channels/purge")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "cleared": 12}
    sqls = [c.args[0] for c in cur.execute.call_args_list]
    assert any(s.strip() == "DELETE FROM target_channels;" for s in sqls)
    assert any("TARGETS_PURGED" in s for s in sqls)


def test_channel_toggle_patch_route():
    conn, cur, fake = _db()
    with patch("src.dashboard.services.get_db_connection", fake):
        assert client.patch("/api/channels/4/toggle").status_code == 200
    assert any("enabled = NOT enabled" in c.args[0]
               for c in cur.execute.call_args_list)


def test_channel_toggle_post_still_works():
    conn, cur, fake = _db()
    with patch("src.dashboard.services.get_db_connection", fake):
        assert client.post("/api/channels/4/toggle").status_code == 200


# ------------------------------------------------------------- routing -----

def test_all_spec_routes_registered():
    routes = {r.path for r in app.routes}
    for expected in ("/api/tasks/purge", "/api/tasks/seed", "/api/tasks/{task_id}",
                     "/api/templates", "/api/templates/{template_id}",
                     "/api/templates/{template_id}/toggle",
                     "/api/channels/{channel_id}/toggle".replace("channel_id", "target_id"),
                     "/api/channels/purge"):
        assert expected in routes, f"missing {expected}"


def test_page_shell_includes_templates_tab():
    assert client.get("/templates").status_code == 200
