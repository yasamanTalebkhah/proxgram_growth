"""Tests for the dashboard API and the runtime settings engine (DB mocked)."""

import os
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import src.dashboard.services as services
from src.core.settings import SETTING_DEFS, get_setting, set_setting
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


# ------------------------------------------------------------ settings ------

def test_get_setting_prefers_db_then_env_then_default():
    with patch("src.core.settings._load_cache", return_value={"SCHEDULER_INTERVAL": "900"}):
        assert get_setting("SCHEDULER_INTERVAL") == "900"
    with patch("src.core.settings._load_cache", return_value={}):
        with patch.dict(os.environ, {"SCHEDULER_INTERVAL": "1800"}):
            assert get_setting("SCHEDULER_INTERVAL") == "1800"
        with patch.dict(os.environ, {}, clear=True):
            assert get_setting("SCHEDULER_INTERVAL") == "3600"


def test_set_setting_rejects_unknown_keys():
    with pytest.raises(KeyError):
        set_setting("NOT_A_SETTING", "1")


def test_setting_defs_cover_documented_dashboard_keys():
    keys = {d["key"] for d in SETTING_DEFS}
    assert {"SCHEDULER_INTERVAL", "GROWTH_CLAIM_TIMEOUT_MINUTES",
            "GROWTH_SWEEP_INTERVAL_SECONDS", "GROWTH_MAX_RETRIES",
            "GROWTH_REQUEUE_BASE_DELAY_SECONDS",
            "GROWTH_CONNECT_TIMEOUT_SECONDS", "GROWTH_SEED_DEDUPE_HOURS"} <= keys


# ------------------------------------------------------------ pages ---------

def test_index_and_page_routes_render_shell():
    assert client.get("/").status_code == 200
    assert "ProxGram Growth" in client.get("/").text
    assert client.get("/tasks").status_code == 200
    assert client.get("/nope").status_code == 404


def test_health_reports_components():
    with patch("src.dashboard.app.postgres_ok", return_value=True), \
         patch("src.dashboard.app.redis_ok", return_value=False), \
         patch("src.dashboard.app.get_worker_heartbeat",
               return_value={"running": True, "last_event": "WORKER_STARTED", "seconds_ago": 5}):
        data = client.get("/health").json()
    assert data == {"dashboard": "ok", "postgres": True, "redis": False,
                    "worker": {"running": True, "last_event": "WORKER_STARTED", "seconds_ago": 5}}


# ------------------------------------------------------------ overview ------

def test_overview_api_shape():
    kpi = MagicMock()
    kpi.__getitem__.side_effect = None
    with patch("src.dashboard.app.dashboard_kpis",
               return_value={"counts": {"COMPLETED": 2}, "success_rate": 100.0}), \
         patch("src.dashboard.app.postgres_ok", return_value=True), \
         patch("src.dashboard.app.redis_ok", return_value=True), \
         patch("src.dashboard.app.get_worker_heartbeat",
               return_value={"running": False, "last_event": None, "seconds_ago": None}), \
         patch("src.dashboard.app.socks_proxy_ok", return_value=False):
        data = client.get("/api/overview").json()
    assert data["kpis"]["success_rate"] == 100.0
    assert set(data["health"].keys()) == {"postgres", "redis", "worker", "socks"}


# ------------------------------------------------------------ channels ------

def test_channel_add_toggle_delete_flow():
    conn, cur, fake = _db()
    with patch("src.dashboard.services.get_db_connection", fake):
        resp = client.post("/api/channels", data={"target": "-100999", "tag": "x"})
        assert resp.status_code == 200
        client.post("/api/channels/1/toggle")
        client.delete("/api/channels/1")
    sqls = [c.args[0] for c in cur.execute.call_args_list]
    assert any("INSERT INTO target_channels" in s for s in sqls)
    assert any("enabled = NOT enabled" in s for s in sqls)
    assert any("DELETE FROM target_channels" in s for s in sqls)

    assert client.post("/api/channels", data={"target": " "}).status_code == 400


# ------------------------------------------------------------ studio --------

def test_template_crud_and_exclusive_activation():
    conn, cur, fake = _db(fetchall=[(False,)])
    with patch("src.dashboard.services.get_db_connection", fake):
        client.post("/api/studio/templates", data={"name": "T", "template": "{a|b}"})
        client.post("/api/studio/templates/2", data={"template": "{a|c}"})
        client.post("/api/studio/templates/2/toggle")
        client.delete("/api/studio/templates/2")
    sqls = [c.args[0] for c in cur.execute.call_args_list]
    assert any("INSERT INTO message_templates" in s for s in sqls)
    assert any("UPDATE message_templates SET is_active = FALSE" in s for s in sqls)
    assert any("DELETE FROM message_templates" in s for s in sqls)
    assert client.post("/api/studio/templates", data={"name": " ", "template": "x"}).status_code == 400


def test_spin_preview_returns_variations():
    with patch("src.core.spintax_service.SpintaxEngine.render_promo",
               side_effect=["one", "two", "one"]):
        from src.core.spintax_service import spin_preview

        variants = spin_preview("{a|b}", count=3)
    assert variants == ["one", "two"]


def test_test_send_endpoint_reports_failure_without_active_account():
    conn, cur, fake = _db(fetchone=None)
    with patch("src.dashboard.services.get_db_connection", fake):
        resp = client.post("/api/studio/test-send", data={"target": "-1003481813519", "template": "t"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


# ------------------------------------------------------------ tasks ---------

def test_task_filters_and_detail():
    rows = [(1, "-100", "SEND_MESSAGE", "COMPLETED", 0, 0, None,
             None, None, None, None)]
    conn, cur, fake = _db(fetchall=rows)
    with patch("src.dashboard.services.get_db_connection", fake):
        data = client.get("/api/tasks", params={"status": "completed", "target": "100"}).json()
        assert data["tasks"][0]["status"] == "COMPLETED"
        params = cur.execute.call_args_list[0].args[1]
        assert params[0] == "COMPLETED" and params[1] == "%100%"
        assert "LIMIT %s" in cur.execute.call_args_list[0].args[0]

    conn, cur, fake = _db(fetchone=None)
    with patch("src.dashboard.services.get_db_connection", fake):
        assert client.get("/api/tasks/999").status_code == 404


def test_cancel_only_pending_and_purge_only_finished():
    conn, cur, fake = _db(rowcount=0)
    with patch("src.dashboard.services.get_db_connection", fake):
        assert client.post("/api/tasks/5/cancel").status_code == 409
        assert client.delete("/api/tasks/5").status_code == 409


def test_action_endpoints_delegate_to_engines():
    with patch("src.dashboard.app.dispatcher") as mock_disp, \
         patch("src.dashboard.app.seed_tasks",
               return_value={"seeded": [("-1009", 12)], "skipped": {}}):
        seed = client.post("/api/actions/seed?force=true").json()
        assert seed["seeded"] == [{"task_id": 12, "target": "-1009"}]
        mock_disp.seed_tasks.assert_not_called()

        mock_disp.sweep_stale_tasks.return_value = {"recovered": 1, "failed": 0}
        assert client.post("/api/actions/sweep").json()["recovered"] == 1

        mock_disp.requeue_failed_tasks.return_value = 4
        assert client.post("/api/actions/requeue").json()["requeued"] == 4


def test_worker_reload_sets_flag():
    with patch("src.dashboard.app.set_worker_restart_flag") as mock_flag:
        resp = client.post("/api/worker/reload").json()
    mock_flag.assert_called_once_with(True)
    assert resp["ok"] is True


def test_settings_post_validates_and_saves():
    conn, cur, fake = _db()
    defs_payload = {"SCHEDULER_INTERVAL": "1800", "GROWTH_MAX_RETRIES": "99",
                    "GROWTH_SWEEP_INTERVAL_SECONDS": "abc"}
    with patch("src.dashboard.app.set_setting") as mock_set:
        resp = client.post("/api/settings", data=defs_payload)
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "GROWTH_MAX_RETRIES" in detail and "GROWTH_SWEEP_INTERVAL_SECONDS" in detail
    mock_set.assert_not_called()

    with patch("src.dashboard.app.set_setting") as mock_set:
        resp = client.post("/api/settings", data={"SCHEDULER_INTERVAL": "1800"}).json()
    mock_set.assert_called_once_with("SCHEDULER_INTERVAL", "1800")
    assert resp["saved"] == ["SCHEDULER_INTERVAL=1800"]


def test_logs_endpoint_caps_limit():
    conn, cur, fake = _db(fetchall=[])
    with patch("src.dashboard.services.get_db_connection", fake):
        client.get("/api/logs", params={"limit": 100000})
    assert cur.execute.call_args_list[0].args[1][0] == 500
