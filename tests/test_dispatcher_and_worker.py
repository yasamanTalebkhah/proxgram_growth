import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from src.core.dispatcher import TaskDispatcher
from src.core.worker import GrowthWorker

@pytest.mark.asyncio
async def test_dispatcher_process_next_task_no_tasks():
    dispatcher = TaskDispatcher()
    with patch.object(dispatcher, 'claim_next_task', return_value=None):
        result = await dispatcher.process_next_task()
        assert result is False

@pytest.mark.asyncio
async def test_dispatcher_process_next_task_success():
    dispatcher = TaskDispatcher()
    mock_task = {
        "id": 1,
        "target": "@test_channel",
        "action_type": "SEND_MESSAGE",
        "payload": {"template": "test", "channel_link": "@link"},
        "retry_count": 0
    }
    mock_account = {
        "id": 10,
        "session_string": "mock_session",
        "proxy": None
    }

    mock_client = AsyncMock()
    mock_client.connect = AsyncMock()
    mock_client.is_user_authorized = AsyncMock(return_value=True)
    mock_client.disconnect = AsyncMock()

    with patch.object(dispatcher.limiter, 'is_quiet_hours', return_value=False), \
         patch.object(dispatcher, 'claim_next_task', return_value=mock_task), \
         patch.object(dispatcher.account_manager, 'get_active_accounts', return_value=[mock_account]), \
         patch.object(dispatcher.account_manager, 'create_client', return_value=mock_client), \
         patch.object(dispatcher, 'execute_task', return_value=True) as mock_exec, \
         patch.object(dispatcher, 'update_task_status') as mock_status:

        result = await dispatcher.process_next_task()
        assert result is True
        mock_exec.assert_called_once_with(mock_client, mock_task)
        mock_status.assert_called_once_with(1, "COMPLETED")
        mock_client.disconnect.assert_called_once()

@pytest.mark.asyncio
async def test_worker_circuit_breaker_triggers():
    worker = GrowthWorker(failure_threshold=2)
    with patch.object(worker.dispatcher, 'process_next_task', side_effect=Exception("DB Failure")), \
         patch.object(worker, 'record_log') as mock_log, \
         patch("asyncio.sleep", new_callable=AsyncMock):

        await worker.run()
        assert worker.is_running is False
        assert worker.consecutive_failures == 2
        mock_log.assert_any_call("CRITICAL", "CIRCUIT_BREAKER_TRIPPED", "Circuit breaker tripped: 2 consecutive failures. Halting worker.")
