"""Target Studio: pool listing/stats service, reprobe + force-promote +
delete + purge, single-target validator re-probe, and the studio API
endpoints (/api/discovery/targets, stats flat keys, action routes).
"""

import asyncio
import sys
import os
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from src.dashboard.app import app
from src.services import discovery_studio

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


def _iso(**delta):
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone.utc) + timedelta(**delta)


# ------------------------------------------------------------ service: list --

def test_list_targets_returns_items_with_reasons_and_total():
    rows = [(3, "@chan3", "@seed1", "SIMILAR_CHANNELS",
             "DISQUALIFIED_NO_DISCUSSION", None, _iso(minutes=-5), _iso(hours=-2)),
            (2, "@chan2", "@seed1", "FORWARD_CHAIN",
             "VALIDATED_HAS_DISCUSSION", 555, _iso(minutes=-30), _iso(hours=-4))]
    rejections = [("Discovery validation disqualified '@chan3': no discussion group linked",)]

    # list_targets opens two connections: list+count, then rejection reasons.
    # _db returns (conn, cur, ctx_manager) — the ctx_manager is what gets
    # called by `with get_db_connection() as conn:`.
    conn_main, cur_main, cm_main = _db(fetchall=rows, fetchone=(7,))
    conn_logs, cur_logs, cm_logs = _db(fetchall=rejections)
    # side_effect values are returned per call, so hand over instantiated
    # context managers (calling the @contextmanager function yields the CM).
    with patch("src.services.discovery_studio.get_db_connection",
               side_effect=[cm_main(), cm_logs()]):
        out = discovery_studio.list_targets()
    assert out["total_count"] == 7
    assert out["items"][0]["username_or_link"] == "@chan3"
    assert out["items"][0]["disqualification_reason"] == "no discussion group linked"
    assert out["items"][1]["disqualification_reason"] is None
    assert out["items"][1]["linked_chat_id"] == 555
    assert isinstance(out["items"][0]["created_at"], str)


def test_list_targets_clamps_limit_and_offsets():
    captured = {}
    conn_main, cur_main, cm_main = _db(fetchone=(0,))
    conn_logs, cur_logs, cm_logs = _db(fetchall=[])

    original_execute = cur_main.execute

    def _capture(sql, params=None):
        captured.update(params or {})
        return original_execute(sql, params or ())

    cur_main.execute.side_effect = _capture
    with patch("src.services.discovery_studio.get_db_connection",
               side_effect=[cm_main(), cm_logs()]):
        discovery_studio.list_targets(status="PENDING_VALIDATION", search="v2ray",
                                      limit=9999, offset=-5)
    assert captured["limit"] == 200
    assert captured["offset"] == 0
    assert captured["status"] == "PENDING_VALIDATION"
    assert captured["like"] == "%v2ray%"


# ----------------------------------------------------------- service: stats --

def test_studio_stats_flat_keys_and_rejection_rate():
    conn, cur, fake = _db(fetchone=(40, 8, 10, 22))
    cur.fetchone.side_effect = [(40, 8, 10, 22), (6,)]
    with patch("src.services.discovery_studio.get_db_connection", fake):
        stats = discovery_studio.studio_stats()
    assert stats["total_discovered"] == 40
    assert stats["pending_count"] == 8
    assert stats["validated_count"] == 10
    assert stats["disqualified_count"] == 22
    assert stats["total_promoted"] == 6
    assert stats["rejection_rate_percentage"] == round(22 / 32 * 100, 1)


def test_studio_stats_zero_decided_rejection_rate():
    conn, cur, fake = _db(fetchone=(0, 0, 0, 0))
    cur.fetchone.side_effect = [(0, 0, 0, 0), (0,)]
    with patch("src.services.discovery_studio.get_db_connection", fake):
        stats = discovery_studio.studio_stats()
    assert stats["rejection_rate_percentage"] == 0.0


# -------------------------------------------- service: reprobe/promote/etc ---

def test_reprobe_target_resets_status_and_audits():
    conn, cur, fake = _db(rowcount=1)
    with patch("src.services.discovery_studio.get_db_connection", fake):
        assert discovery_studio.reprobe_target(9) is True
    assert "PENDING_VALIDATION" in cur.execute.call_args_list[0].args[0]


def test_reprobe_target_missing_returns_false():
    conn, cur, fake = _db(rowcount=0)
    with patch("src.services.discovery_studio.get_db_connection", fake):
        assert discovery_studio.reprobe_target(999) is False


def test_force_promote_inserts_with_manual_promoted_tag():
    conn, cur, fake = _db(rowcount=1)
    with patch("src.services.discovery_studio.get_db_connection", fake), \
         patch("src.services.discovery_studio.get_target_record",
               return_value={"id": 5, "username": "@cand", "status": "DISQUALIFIED_NO_DISCUSSION"}):
        assert discovery_studio.force_promote_target(5) is True
    sql, params = cur.execute.call_args_list[0].args
    assert "tag, enabled" in sql and "ON CONFLICT (target) DO NOTHING" in sql
    assert params == ("@cand", "manual_promoted")


def test_force_promote_missing_record_returns_none():
    with patch("src.services.discovery_studio.get_target_record", return_value=None):
        assert discovery_studio.force_promote_target(1234) is None


def test_delete_target_record():
    conn, cur, fake = _db(rowcount=1)
    with patch("src.services.discovery_studio.get_db_connection", fake):
        assert discovery_studio.delete_target_record(4) is True


def test_purge_disqualified_all_and_by_days():
    conn, cur, fake = _db(rowcount=11)
    with patch("src.services.discovery_studio.get_db_connection", fake):
        assert discovery_studio.purge_disqualified() == 11
    assert "DISQUALIFIED_NO_DISCUSSION" in cur.execute.call_args_list[0].args[0]
    conn, cur, fake = _db(rowcount=3)
    with patch("src.services.discovery_studio.get_db_connection", fake):
        assert discovery_studio.purge_disqualified(days=7) == 3
    assert "days" in cur.execute.call_args_list[0].args[0]


# ------------------------------------- validator: single-target re-probe -----

def _validator_manager(client_mock):
    manager = MagicMock()
    manager.get_active_accounts.return_value = [
        {"session_string": "sess", "proxy": None}]
    manager.create_client.return_value = client_mock
    manager.connect_with_fallback = AsyncMock(return_value=client_mock)
    return manager


def test_run_validation_single_id_probes_only_that_target():
    from src.services import discussion_validator as dv

    with patch("src.accounts.manager.AccountManager",
               return_value=_validator_manager(MagicMock())), \
         patch("src.services.discussion_validator.fetch_pending") as fetch, \
         patch("src.services.discussion_validator.validate_batch",
               new_callable=AsyncMock,
               return_value={"checked": 1, "validated": 1, "disqualified": 0}) as vb, \
         patch("src.services.discovery_studio.get_target_record",
               return_value={"id": 12, "username": "@recheck", "status": "PENDING_VALIDATION"}):
        snap = dv.run_validation(single_id=12)
    fetch.assert_not_called()
    vb.assert_called_once()
    assert vb.call_args.args[1] == [{"id": 12, "username": "@recheck", "attempts": 0}]
    assert snap["ok"] is True


def test_run_validation_single_id_rejects_non_pending_status():
    from src.services import discussion_validator as dv

    with patch("src.accounts.manager.AccountManager",
               return_value=_validator_manager(MagicMock())), \
         patch("src.services.discovery_studio.get_target_record",
               return_value={"id": 12, "username": "@x", "status": "VALIDATED_HAS_DISCUSSION"}):
        snap = dv.run_validation(single_id=12)
    assert snap["ok"] is False
    assert "not PENDING_VALIDATION" in snap["detail"]


# ------------------------------------------------------------------- API -----

def test_api_targets_list_endpoint():
    with patch("src.api.discovery_routes.list_targets",
               return_value={"items": [{"id": 1, "username_or_link": "@a"}],
                             "total_count": 1}) as lt:
        resp = client.get("/api/discovery/targets",
                          params={"status": "PENDING_VALIDATION",
                                  "search": "v2ray", "limit": 10, "offset": 5})
    assert resp.status_code == 200
    assert resp.json()["total_count"] == 1
    lt.assert_called_once_with(status="PENDING_VALIDATION", search="v2ray",
                               limit=10, offset=5)


def test_api_targets_validates_status_filter():
    resp = client.get("/api/discovery/targets", params={"status": "BOGUS"})
    assert resp.status_code == 400


def test_api_targets_defaults():
    with patch("src.api.discovery_routes.list_targets",
               return_value={"items": [], "total_count": 0}) as lt:
        assert client.get("/api/discovery/targets").status_code == 200
    assert lt.call_args.kwargs["limit"] == 50
    assert lt.call_args.kwargs["offset"] == 0
    assert lt.call_args.kwargs["status"] is None


def test_api_stats_includes_flat_studio_keys():
    with patch("src.api.discovery_routes.validator_status",
               return_value={"running": False, "pool": {"total": 1}}), \
         patch("src.api.discovery_routes.studio_stats",
               return_value={"total_discovered": 40, "pending_count": 8,
                             "validated_count": 10, "disqualified_count": 22,
                             "total_promoted": 6, "rejection_rate_percentage": 68.8}):
        data = client.get("/api/discovery/stats").json()
    assert data["total_discovered"] == 40
    assert data["rejection_rate_percentage"] == 68.8
    assert "crawler" in data and "pool" in data


def test_api_trigger_crawler_accepts_limit_seeds():
    with patch("src.api.discovery_routes.start_background_crawl",
               return_value={"ok": True, "detail": "crawler started in background"}) as bg, \
         patch("src.services.discovery_crawler.CRAWLER_STATE") as cs:
        cs.seeds = ["@s1", "@s2"]
        resp = client.post("/api/discovery/trigger-crawler",
                           json={"limit_seeds": 2})
    assert resp.status_code == 200
    bg.assert_called_once_with(max_seeds=2)
    assert resp.json()["limit_seeds"] == 2


def test_api_trigger_validator_accepts_batch_size():
    with patch("src.api.discovery_routes.start_background_validation",
               return_value={"ok": True, "detail": "validator started in background"}) as bg:
        resp = client.post("/api/discovery/trigger-validator",
                           json={"batch_size": 40})
    assert resp.status_code == 200
    bg.assert_called_once_with(limit=40)


def test_api_trigger_defaults_without_body():
    with patch("src.api.discovery_routes.start_background_crawl",
               return_value={"ok": True, "detail": "crawler started in background"}) as bgc, \
         patch("src.api.discovery_routes.start_background_validation",
               return_value={"ok": True, "detail": "validator started in background"}) as bgv:
        assert client.post("/api/discovery/trigger-crawler").status_code == 200
        assert client.post("/api/discovery/trigger-validator").status_code == 200
    bgc.assert_called_once_with(max_seeds=5)
    bgv.assert_called_once_with(limit=25)


def test_api_reprobe_resets_then_triggers_single_validation():
    with patch("src.api.discovery_routes.reprobe_target", return_value=True) as rp, \
         patch("src.api.discovery_routes.start_background_validation",
               return_value={"ok": True, "detail": "validator started in background"}) as bg:
        resp = client.post("/api/discovery/targets/7/reprobe")
    assert resp.status_code == 200
    rp.assert_called_once_with(7)
    bg.assert_called_once_with(single_id=7)


def test_api_reprobe_missing_target_404():
    with patch("src.api.discovery_routes.reprobe_target", return_value=False):
        assert client.post("/api/discovery/targets/999/reprobe").status_code == 404


def test_api_force_promote_reports_inserted_flag():
    with patch("src.api.discovery_routes.force_promote_target", return_value=True):
        resp = client.post("/api/discovery/targets/7/force-promote")
    assert resp.status_code == 200 and resp.json()["inserted"] is True


def test_api_force_promote_already_there_and_missing():
    with patch("src.api.discovery_routes.force_promote_target", return_value=False):
        resp = client.post("/api/discovery/targets/7/force-promote")
        assert resp.status_code == 200 and resp.json()["inserted"] is False
    with patch("src.api.discovery_routes.force_promote_target", return_value=None):
        assert client.post("/api/discovery/targets/999/force-promote").status_code == 404


def test_api_delete_target():
    with patch("src.api.discovery_routes.delete_target_record", return_value=True) as dt:
        resp = client.delete("/api/discovery/targets/3")
    assert resp.status_code == 200 and resp.json()["deleted"] == 3
    with patch("src.api.discovery_routes.delete_target_record", return_value=False):
        assert client.delete("/api/discovery/targets/3").status_code == 404


def test_api_purge_disqualified_with_and_without_days():
    with patch("src.api.discovery_routes.purge_disqualified", return_value=5) as p:
        resp = client.post("/api/discovery/purge-disqualified", json={"days": 14})
    assert resp.json()["deleted"] == 5
    p.assert_called_once_with(days=14)
    with patch("src.api.discovery_routes.purge_disqualified", return_value=22) as p2:
        resp = client.post("/api/discovery/purge-disqualified")
    assert resp.json()["deleted"] == 22
    p2.assert_called_once_with(days=None)


# ------------------------------------------------------------ autopilot API --

def test_api_autopilot_status_passthrough():
    snap = {"running": False, "current_phase": "idle", "progress_percent": 0.0,
            "target_quota": 100, "promoted_count": 0}
    with patch("src.api.discovery_routes.autopilot_status", return_value=snap):
        data = client.get("/api/discovery/autopilot/status").json()
    assert data == snap


def test_api_autopilot_start_with_quota():
    with patch("src.api.discovery_routes.start_background_autopilot",
               return_value={"ok": True, "detail": "started"}) as bg:
        resp = client.post("/api/discovery/autopilot/start", json={"quota": 42})
    assert resp.status_code == 200
    bg.assert_called_once_with(target_quota=42)


def test_api_autopilot_start_defaults_and_conflict():
    with patch("src.api.discovery_routes.start_background_autopilot",
               return_value={"ok": True, "detail": "started"}) as bg:
        assert client.post("/api/discovery/autopilot/start").status_code == 200
    bg.assert_called_once_with(target_quota=100)
    with patch("src.api.discovery_routes.start_background_autopilot",
               return_value={"ok": False, "detail": "Auto-Pilot loop is already running"}):
        assert client.post("/api/discovery/autopilot/start").status_code == 409


def test_api_autopilot_start_validates_quota_range():
    assert client.post("/api/discovery/autopilot/start", json={"quota": 4}).status_code == 422
    assert client.post("/api/discovery/autopilot/start", json={"quota": 1001}).status_code == 422


def test_api_autopilot_stop_and_conflict():
    with patch("src.api.discovery_routes.stop_background_autopilot",
               return_value={"ok": True, "detail": "Stop signal transmitted to Auto-Pilot"}) as sp:
        resp = client.post("/api/discovery/autopilot/stop")
    assert resp.status_code == 200
    sp.assert_called_once()
    with patch("src.api.discovery_routes.stop_background_autopilot",
               return_value={"ok": False, "detail": "Auto-Pilot is not currently running"}):
        assert client.post("/api/discovery/autopilot/stop").status_code == 409
