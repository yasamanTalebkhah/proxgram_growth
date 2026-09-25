"""Tests for bulk channel import (dedupe + summary counts) and the
dashboard retry endpoint (DB mocked)."""

import sys
import os
from contextlib import contextmanager
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


# ------------------------------------------------------------ bulk import --

def test_bulk_import_service_dedupes_batch_and_table():
    conn, cur, fake = _db(fetchall=[("@dup",)])
    with patch("src.dashboard.services.get_db_connection", fake):
        summary = services.bulk_import_targets(
            "@new1\nhttps://t.me/new2\n@dup\n@dup\n@new1", tag="batch-x"
        )
    assert summary == {"added": 2, "skipped": 1, "duplicates": ["@dup"]}
    inserts = [c.args[0] for c in cur.execute.call_args_list
               if "INSERT INTO target_channels" in c.args[0]]
    assert len(inserts) == 2
    # Existing-target lookup used the deduped batch
    lookup = [c for c in cur.execute.call_args_list if "ANY(%s)" in c.args[0]][0]
    assert sorted(lookup.args[1][0]) == ["@dup", "@new1", "https://t.me/new2"]
    # Common tag applied to inserts
    insert_params = [c.args[1] for c in cur.execute.call_args_list
                     if "INSERT INTO target_channels" in c.args[0]]
    assert all(p[1] == "batch-x" for p in insert_params)


def test_bulk_import_endpoint_rejects_empty_and_counts():
    conn, cur, fake = _db(fetchall=[])
    with patch("src.dashboard.services.get_db_connection", fake):
        resp = client.post("/api/channels/bulk-import", json={
            "targets": "https://t.me/a\n@b", "tag": "promo"})
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {"added", "skipped", "duplicates"}

        assert client.post("/api/channels/bulk-import", json={"targets": "   "}).status_code == 400
        assert client.post("/api/channels/bulk-import", json={}).status_code == 400


# ----------------------------------------------------------- retry route ---

def test_retry_endpoint_requeues_or_conflicts():
    with patch("src.dashboard.app.dispatcher") as mock_disp:
        mock_disp.retry_task.return_value = True
        assert client.post("/api/tasks/9/retry").status_code == 200

        mock_disp.retry_task.return_value = False
        resp = client.post("/api/tasks/9/retry")
        assert resp.status_code == 409
        assert "retryable" in resp.json()["detail"]


# ------------------------------------------------------------ overview -----

def test_kpis_include_active_accounts():
    conn, cur, fake = _db(fetchall=[("COMPLETED", 3)], fetchone=(5, 12.3))
    with patch("src.dashboard.services.get_db_connection", fake):
        kpis = services.dashboard_kpis()
    assert "active_accounts" in kpis


def test_settings_table_untouched_by_import_changes():
    """Guard: message_templates/target_channels routes still registered."""
    routes = {r.path for r in app.routes}
    assert "/api/channels/bulk-import" in routes
    assert "/api/tasks/{task_id}/retry" in routes
    assert "/api/studio/templates" in routes
