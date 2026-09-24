"""Tests for the SEND_MESSAGE delivery read-back (all DB/Telegram mocked)."""

import sys
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.dispatcher import TaskDispatcher

ENV_NOTE = "DB layer is patched out; Telethon client is an AsyncMock."


def _mock_db():
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur

    @contextmanager
    def fake_conn():
        yield mock_conn

    return mock_conn, mock_cur, fake_conn


def _delivery_inserts(mock_cur):
    return [
        call for call in mock_cur.execute.call_args_list
        if "DELIVERY_VERIFIED" in call.args[0]
    ]


def _task(task_id=11):
    return {
        "id": task_id,
        "target": "-1003481813519",
        "action_type": "SEND_MESSAGE",
        "payload": {},
        "retry_count": 0,
    }


@pytest.mark.asyncio
async def test_verified_delivery_records_audit_row_with_message_id():
    dispatcher = TaskDispatcher()
    sent = MagicMock(); sent.id = 555
    client = AsyncMock()
    client.send_message.return_value = sent
    client.get_messages.return_value = [MagicMock(id=555), MagicMock(id=550)]
    mock_conn, mock_cur, fake_conn = _mock_db()

    with patch.object(dispatcher.limiter, "wait_jitter", new=AsyncMock()), \
         patch.object(dispatcher, "_resolve_entity", new=AsyncMock(return_value=object())), \
         patch("src.core.dispatcher.asyncio.sleep", new=AsyncMock()), \
         patch("src.core.dispatcher.get_db_connection", fake_conn):
        result = await dispatcher.execute_task(client, _task())

    assert result is True
    inserts = _delivery_inserts(mock_cur)
    assert len(inserts) == 1
    assert "telegram message_id=555" in inserts[0].args[1][0]
    assert "#11" in inserts[0].args[1][0]
    mock_conn.commit.assert_called_once()


@pytest.mark.asyncio
async def test_read_back_failure_keeps_task_successful():
    dispatcher = TaskDispatcher()
    sent = MagicMock(); sent.id = 556
    client = AsyncMock()
    client.send_message.return_value = sent
    client.get_messages.side_effect = Exception("ChannelPrivate: read restricted")
    mock_conn, mock_cur, fake_conn = _mock_db()

    with patch.object(dispatcher.limiter, "wait_jitter", new=AsyncMock()), \
         patch.object(dispatcher, "_resolve_entity", new=AsyncMock(return_value=object())), \
         patch("src.core.dispatcher.asyncio.sleep", new=AsyncMock()), \
         patch("src.core.dispatcher.get_db_connection", fake_conn):
        result = await dispatcher.execute_task(client, _task())

    assert result is True  # send succeeded; verification is best-effort
    assert _delivery_inserts(mock_cur) == []


@pytest.mark.asyncio
async def test_missing_message_in_recent_history_is_unverified():
    dispatcher = TaskDispatcher()
    sent = MagicMock(); sent.id = 557
    client = AsyncMock()
    client.send_message.return_value = sent
    client.get_messages.return_value = [MagicMock(id=999), MagicMock(id=998)]
    mock_conn, mock_cur, fake_conn = _mock_db()

    with patch.object(dispatcher.limiter, "wait_jitter", new=AsyncMock()), \
         patch.object(dispatcher, "_resolve_entity", new=AsyncMock(return_value=object())), \
         patch("src.core.dispatcher.asyncio.sleep", new=AsyncMock()), \
         patch("src.core.dispatcher.get_db_connection", fake_conn):
        result = await dispatcher.execute_task(client, _task())

    assert result is True
    assert _delivery_inserts(mock_cur) == []


@pytest.mark.asyncio
async def test_verify_delivery_returns_expected_id_when_present():
    dispatcher = TaskDispatcher()
    client = AsyncMock()
    client.get_messages.return_value = [MagicMock(id=42), MagicMock(id=41)]
    out = await dispatcher._verify_delivery(client, "entity", _task(3), 42)
    assert out == 42


@pytest.mark.asyncio
async def test_verify_delivery_never_raises_on_restricted_entity():
    dispatcher = TaskDispatcher()
    client = AsyncMock()
    client.get_messages.side_effect = RuntimeError("chat forbidden")
    out = await dispatcher._verify_delivery(client, "entity", _task(3), 42)
    assert out is None


@pytest.mark.asyncio
async def test_no_read_back_when_sent_message_has_no_id():
    dispatcher = TaskDispatcher()
    sent = MagicMock(); sent.id = None
    client = AsyncMock()
    client.send_message.return_value = sent
    mock_conn, mock_cur, fake_conn = _mock_db()

    with patch.object(dispatcher.limiter, "wait_jitter", new=AsyncMock()), \
         patch.object(dispatcher, "_resolve_entity", new=AsyncMock(return_value=object())), \
         patch("src.core.dispatcher.asyncio.sleep", new=AsyncMock()), \
         patch("src.core.dispatcher.get_db_connection", fake_conn):
        result = await dispatcher.execute_task(client, _task())

    assert result is True
    client.get_messages.assert_not_awaited()
    assert _delivery_inserts(mock_cur) == []


@pytest.mark.asyncio
async def test_process_next_task_completes_with_delivery_audit():
    dispatcher = TaskDispatcher()
    sent = MagicMock(); sent.id = 600
    client = AsyncMock()
    client.is_user_authorized.return_value = True
    client.send_message.return_value = sent
    client.get_messages.return_value = [MagicMock(id=600)]
    account = {"id": 2, "session_string": "s", "proxy": None}
    mock_conn, mock_cur, fake_conn = _mock_db()

    with patch.object(dispatcher.limiter, "is_quiet_hours", return_value=False), \
         patch.object(dispatcher, "claim_next_task", return_value=_task(12)), \
         patch.object(dispatcher.account_manager, "get_active_accounts", return_value=[account]), \
         patch.object(dispatcher.account_manager, "create_client", return_value=client), \
         patch.object(dispatcher, "_resolve_entity", new=AsyncMock(return_value=object())), \
         patch("src.core.dispatcher.asyncio.sleep", new=AsyncMock()), \
         patch("src.core.dispatcher.get_db_connection", fake_conn), \
         patch.object(dispatcher, "update_task_status") as mock_status:
        result = await dispatcher.process_next_task()

    assert result is True
    mock_status.assert_called_once_with(12, "COMPLETED")
    assert len(_delivery_inserts(mock_cur)) == 1
    client.disconnect.assert_awaited_once()
