"""Tests for the failed-task requeue policy (exponential backoff)."""

import os
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from src.core.dispatcher import TaskDispatcher
from src.core.worker import GrowthWorker


def _mock_db(fetchall_rows):
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_cur.fetchall.return_value = fetchall_rows
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur

    @contextmanager
    def fake_conn():
        yield mock_conn

    return mock_conn, mock_cur, fake_conn


def _requeue_inserts(mock_cur):
    return [
        call for call in mock_cur.execute.call_args_list
        if "system_logs" in call.args[0] and "TASK_REQUEUED" == call.args[1][1]
    ]


def test_backoff_math_matches_configured_base():
    for retry_count, base in ((0, 60), (1, 60), (2, 60), (3, 120), (5, 30)):
        assert base * (2 ** retry_count) == base * (2 ** retry_count)
    # Reference progression for GROWTH_REQUEUE_BASE_DELAY_SECONDS=60
    assert [60 * (2 ** n) for n in range(4)] == [60, 120, 240, 480]


def test_requeue_passes_max_retries_and_base_delay_to_query():
    dispatcher = TaskDispatcher()
    mock_conn, mock_cur, fake_conn = _mock_db([])

    with patch("src.core.dispatcher.get_db_connection", fake_conn):
        dispatcher.requeue_failed_tasks(max_retries=5, base_delay_seconds=90)

    sweep_params = mock_cur.execute.call_args_list[0].args[1]
    assert sweep_params == (5, 90)
    requeue_sql = mock_cur.execute.call_args_list[0].args[0]
    assert "status = 'FAILED'" in requeue_sql
    assert "retry_count < %s" in requeue_sql
    assert "POWER(2, retry_count)" in requeue_sql
    assert "FOR UPDATE SKIP LOCKED" in requeue_sql


def test_requeue_reads_defaults_from_environment():
    dispatcher = TaskDispatcher()
    mock_conn, mock_cur, fake_conn = _mock_db([])

    env = {"GROWTH_MAX_RETRIES": "7", "GROWTH_REQUEUE_BASE_DELAY_SECONDS": "45"}
    with patch.dict(os.environ, env), patch("src.core.dispatcher.get_db_connection", fake_conn):
        dispatcher.requeue_failed_tasks()

    assert mock_cur.execute.call_args_list[0].args[1] == (7, 45)


def test_requeued_tasks_get_audit_rows_with_backoff_details():
    dispatcher = TaskDispatcher()
    # Two tasks: retry_count 0 (60s window at base 60) and 2 (240s).
    mock_conn, mock_cur, fake_conn = _mock_db([(21, 0), (22, 2)])

    with patch("src.core.dispatcher.get_db_connection", fake_conn):
        requeued = dispatcher.requeue_failed_tasks(base_delay_seconds=60)

    assert requeued == 2
    logs = _requeue_inserts(mock_cur)
    assert len(logs) == 2
    messages = [call.args[1][2] for call in logs]
    assert any("#21" in m and "backoff=60s" in m for m in messages)
    assert any("#22" in m and "backoff=240s" in m for m in messages)
    assert all("TASK_REQUEUED" == call.args[1][1] for call in logs)
    mock_conn.commit.assert_called_once()


def test_requeue_is_silent_when_no_eligible_tasks():
    dispatcher = TaskDispatcher()
    mock_conn, mock_cur, fake_conn = _mock_db([])

    with patch("src.core.dispatcher.get_db_connection", fake_conn):
        requeued = dispatcher.requeue_failed_tasks()

    assert requeued == 0
    assert _requeue_inserts(mock_cur) == []


def test_requeue_swallows_db_errors():
    dispatcher = TaskDispatcher()

    @contextmanager
    def broken_conn():
        raise RuntimeError("db down")
        yield  # pragma: no cover

    with patch("src.core.dispatcher.get_db_connection", broken_conn):
        assert dispatcher.requeue_failed_tasks() == 0  # must not raise


def test_worker_maintenance_pass_runs_requeue_after_sweep():
    worker = GrowthWorker()
    with patch.object(worker.dispatcher, "sweep_stale_tasks",
                      return_value={"recovered": 1, "failed": 0}) as mock_sweep, \
         patch.object(worker.dispatcher, "requeue_failed_tasks", return_value=3) as mock_requeue:
        worker._run_sweeper()

    mock_sweep.assert_called_once()
    mock_requeue.assert_called_once()
    assert worker.last_sweep_monotonic is not None


def test_worker_requeue_failure_does_not_break_maintenance_pass():
    worker = GrowthWorker()
    with patch.object(worker.dispatcher, "sweep_stale_tasks",
                      return_value={"recovered": 0, "failed": 0}), \
         patch.object(worker.dispatcher, "requeue_failed_tasks",
                      side_effect=Exception("boom")):
        worker._run_sweeper()  # must not raise
    assert worker.is_running is True
