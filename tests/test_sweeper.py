"""Unit tests for the claim-timeout sweeper (DB mocked per repo convention)."""

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


def _log_inserts(mock_cur):
    return [
        call for call in mock_cur.execute.call_args_list
        if "system_logs" in call.args[0]
    ]


def test_sweeper_requeues_stale_running_task_with_audit_log():
    dispatcher = TaskDispatcher()
    mock_conn, mock_cur, fake_conn = _mock_db([(42, "PENDING", 1)])

    with patch("src.core.dispatcher.get_db_connection", fake_conn):
        counts = dispatcher.sweep_stale_tasks()

    assert counts == {"recovered": 1, "failed": 0}
    mock_conn.commit.assert_called_once()

    sweep_sql = mock_cur.execute.call_args_list[0].args[0]
    assert "FOR UPDATE SKIP LOCKED" in sweep_sql
    assert "status = 'RUNNING'" in sweep_sql

    logs = _log_inserts(mock_cur)
    assert len(logs) == 1
    assert "STALE_TASK_RECOVERED" == logs[0].args[1][1]
    assert "requeued as PENDING" in logs[0].args[1][2]


def test_sweeper_marks_failed_when_max_retries_exhausted():
    dispatcher = TaskDispatcher()
    mock_conn, mock_cur, fake_conn = _mock_db([(7, "FAILED", 4)])

    with patch("src.core.dispatcher.get_db_connection", fake_conn):
        counts = dispatcher.sweep_stale_tasks()

    assert counts == {"recovered": 0, "failed": 1}
    logs = _log_inserts(mock_cur)
    assert len(logs) == 1
    assert "max retries exhausted" in logs[0].args[1][2]
    assert "FAILED" in logs[0].args[1][2]


def test_sweeper_handles_mixed_outcomes():
    dispatcher = TaskDispatcher()
    mock_conn, mock_cur, fake_conn = _mock_db(
        [(1, "PENDING", 1), (2, "FAILED", 3)]
    )

    with patch("src.core.dispatcher.get_db_connection", fake_conn):
        counts = dispatcher.sweep_stale_tasks()

    assert counts == {"recovered": 1, "failed": 1}
    assert len(_log_inserts(mock_cur)) == 2


def test_sweeper_passes_timeout_and_max_retries_to_query():
    dispatcher = TaskDispatcher()
    mock_conn, mock_cur, fake_conn = _mock_db([])

    with patch("src.core.dispatcher.get_db_connection", fake_conn):
        dispatcher.sweep_stale_tasks(timeout_minutes=7, max_retries=5)

    sweep_params = mock_cur.execute.call_args_list[0].args[1]
    assert sweep_params == (7, 5, 5)


def test_sweeper_is_silent_when_nothing_is_stale():
    dispatcher = TaskDispatcher()
    mock_conn, mock_cur, fake_conn = _mock_db([])

    with patch("src.core.dispatcher.get_db_connection", fake_conn):
        counts = dispatcher.sweep_stale_tasks()

    assert counts == {"recovered": 0, "failed": 0}
    assert _log_inserts(mock_cur) == []


def test_worker_sweeper_swallows_errors_without_raising():
    worker = GrowthWorker()
    with patch.object(
        worker.dispatcher, "sweep_stale_tasks", side_effect=Exception("db down")
    ):
        worker._run_sweeper()  # must not raise
    assert worker.is_running is True
