"""Tests for transport fallbacks and bounded connection timeouts."""

import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest

from src.accounts.manager import (
    AccountManager,
    TRANSPORT_FALLBACKS,
)
from src.core.dispatcher import TaskDispatcher

ENV = {"TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": "a" * 32}


def _manager():
    with patch.dict(os.environ, ENV):
        return AccountManager()


def test_create_client_binds_abridged_transport_and_timeout_bounds():
    manager = _manager()
    client = manager.create_client("")  # empty StringSession is valid but credential-free
    assert client._connection is TRANSPORT_FALLBACKS[0]  # Abridged tried first
    assert client._timeout == manager.connect_timeout  # bounded handshake
    assert client._connection_retries == 1  # fallback logic owns retry policy


def test_create_client_explicit_transport_override():
    manager = _manager()
    client = manager.create_client("", connection_cls=TRANSPORT_FALLBACKS[1])
    assert client._connection is TRANSPORT_FALLBACKS[1]


def test_create_client_requires_credentials():
    with patch.dict(os.environ, {"TELEGRAM_API_ID": "", "TELEGRAM_API_HASH": ""}):
        manager = AccountManager()
        with pytest.raises(ValueError):
            manager.create_client("")


def test_transport_fallback_sequence_covers_three_modes():
    names = [t.__name__ for t in TRANSPORT_FALLBACKS]
    assert names == [
        "ConnectionTcpAbridged",
        "ConnectionTcpFull",
        "ConnectionTcpObfuscated",
    ]


@pytest.mark.asyncio
async def test_connect_with_fallback_succeeds_on_first_transport():
    manager = _manager()
    client = AsyncMock()
    result = await manager.connect_with_fallback(client)
    assert result is client
    assert client._connection is TRANSPORT_FALLBACKS[0]
    client.disconnect.assert_not_called()


@pytest.mark.asyncio
async def test_connect_with_fallback_falls_through_to_second_transport():
    manager = _manager()
    client = AsyncMock()
    # First transport severed; second attempt succeeds.
    client.connect = AsyncMock(side_effect=[ConnectionError("severed"), None])
    result = await manager.connect_with_fallback(client)
    assert result is client
    assert client._connection is TRANSPORT_FALLBACKS[1]
    client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_connect_with_fallback_raises_after_all_transports_fail():
    manager = _manager()
    client = AsyncMock()
    client.connect = AsyncMock(side_effect=ConnectionError("severed"))
    with pytest.raises(ConnectionError):
        await manager.connect_with_fallback(client)
    assert client.disconnect.await_count == len(TRANSPORT_FALLBACKS)


@pytest.mark.asyncio
async def test_connect_with_fallback_enforces_bounded_timeout():
    manager = _manager()
    client = AsyncMock()
    with patch(
        "src.accounts.manager.asyncio.wait_for",
        side_effect=asyncio.TimeoutError(),
    ):
        with pytest.raises(ConnectionError):
            await manager.connect_with_fallback(client)
    assert client.disconnect.await_count == len(TRANSPORT_FALLBACKS)


@pytest.mark.asyncio
async def test_dispatcher_requeues_task_when_all_transports_fail():
    dispatcher = TaskDispatcher()
    mock_task = {
        "id": 9,
        "target": "@t",
        "action_type": "SEND_MESSAGE",
        "payload": {},
        "retry_count": 0,
    }
    mock_account = {"id": 10, "session_string": "s", "proxy": None}
    mock_client = AsyncMock()
    with patch.object(dispatcher.limiter, "is_quiet_hours", return_value=False), \
         patch.object(dispatcher, "claim_next_task", return_value=mock_task), \
         patch.object(dispatcher.account_manager, "get_active_accounts", return_value=[mock_account]), \
         patch.object(dispatcher.account_manager, "create_client", return_value=mock_client), \
         patch.object(dispatcher.account_manager, "connect_with_fallback",
                      side_effect=ConnectionError("egress down")), \
         patch.object(dispatcher, "update_task_status") as mock_status:
        result = await dispatcher.process_next_task()

    assert result is False
    mock_status.assert_called_once_with(
        9, "PENDING", error_message="All transports failed: egress down"
    )
    mock_client.disconnect.assert_awaited_once()
