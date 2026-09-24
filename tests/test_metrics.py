"""Tests for lifecycle metrics collection and formatting (DB mocked)."""

import os
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from src.core.metrics import collect_lifecycle_metrics, format_lifecycle_summary


def _mock_db():
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    state = {"sql": "", "params": None}

    def execute(sql, params=None):
        state["sql"], state["params"] = sql, params

    def fetchall():
        if "FROM tasks" in state["sql"] and "GROUP BY status" in state["sql"]:
            return [("COMPLETED", 8), ("FAILED", 1), ("PENDING", 0)]
        return []

    def fetchone():
        sql = state["sql"]
        if "AVG(retry_count)" in sql:
            return (2, 1.5)
        if "executed_at - created_at" in sql:
            return (8, 95.0)
        if "system_logs" in sql:
            return (3,)
        if "retry_count >= %s" in sql:
            return (1,)
        return (0,)

    mock_cur.execute.side_effect = execute
    mock_cur.fetchall.side_effect = fetchall
    mock_cur.fetchone.side_effect = fetchone
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur

    @contextmanager
    def fake_conn():
        yield mock_conn

    return mock_conn, mock_cur, fake_conn


def test_collect_success_rate_pairs_completed_against_currently_failed():
    mock_conn, mock_cur, fake_conn = _mock_db()
    with patch("src.core.metrics.get_db_connection", fake_conn):
        m = collect_lifecycle_metrics()
    assert m["task_counts"] == {"COMPLETED": 8, "FAILED": 1, "PENDING": 0}
    assert m["success_rate"] == 88.9  # 8 / (8 + 1)


def test_collect_delivery_and_retry_efficiency_stats():
    mock_conn, mock_cur, fake_conn = _mock_db()
    with patch("src.core.metrics.get_db_connection", fake_conn):
        m = collect_lifecycle_metrics()
    assert m["deliveries_measured"] == 8
    assert m["avg_time_to_delivery_seconds"] == 95.0
    assert m["retry_graduates"] == 2
    assert m["avg_retries_of_graduates"] == 1.5
    assert m["terminal_failures"] == 1


def test_collect_terminal_query_uses_max_retries_from_env():
    mock_conn, mock_cur, fake_conn = _mock_db()
    with patch.dict(os.environ, {"GROWTH_MAX_RETRIES": "5"}), \
         patch("src.core.metrics.get_db_connection", fake_conn):
        collect_lifecycle_metrics()
    assert mock_cur.execute.call_args_list[-1].args[1] == (5,)


def test_collect_counts_all_lifecycle_events():
    mock_conn, mock_cur, fake_conn = _mock_db()
    with patch("src.core.metrics.get_db_connection", fake_conn):
        m = collect_lifecycle_metrics()
    for event in ("TASK_REQUEUED", "STALE_TASK_RECOVERED",
                  "DELIVERY_VERIFIED", "CIRCUIT_BREAKER_TRIPPED"):
        assert m[event] == 3


def test_format_summary_is_concise_and_complete():
    mock_conn, mock_cur, fake_conn = _mock_db()
    with patch("src.core.metrics.get_db_connection", fake_conn):
        m = collect_lifecycle_metrics()
    summary = format_lifecycle_summary(m)
    assert "Success rate: 88.9%" in summary
    assert "1.6 min" in summary          # 95s formatted as minutes
    assert "8 measured send(s)" in summary
    assert "Requeue events: 3" in summary
    assert "orphan recoveries: 3" in summary
    assert "2 retried task(s) eventually completed" in summary
    assert "3/8" in summary              # delivery verification coverage
    assert summary.count("\n") == 6      # concise: 7 lines total


def test_format_handles_empty_metrics_without_crashing():
    summary = format_lifecycle_summary({})
    assert "n/a" in summary
    assert "Success rate" in summary


def test_format_short_durations_in_seconds():
    m = {
        "task_counts": {"COMPLETED": 1, "FAILED": 0},
        "success_rate": 100.0,
        "deliveries_measured": 1,
        "avg_time_to_delivery_seconds": 42.0,
        "retry_graduates": 0,
        "avg_retries_of_graduates": 0,
        "terminal_failures": 0,
        "TASK_REQUEUED": 0,
        "STALE_TASK_RECOVERED": 0,
        "DELIVERY_VERIFIED": 1,
        "CIRCUIT_BREAKER_TRIPPED": 0,
    }
    assert "42 s" in format_lifecycle_summary(m)
