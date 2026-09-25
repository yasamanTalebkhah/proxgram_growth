"""Broadcast-channel commenting: GetDiscussionMessageRequest flow, SKIPPED
terminal status, and dashboard-driven manual retry (all DB mocked)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.dispatcher import NoDiscussionGroupError, TaskDispatcher


def _channel(broadcast=True, megagroup=False):
    chat = MagicMock()
    chat.__class__.__name__ = "Channel"
    chat.broadcast = broadcast
    chat.megagroup = megagroup
    return chat


def _task(**overrides):
    task = {
        "id": 7,
        "target": "-1003481813519",
        "action_type": "SEND_MESSAGE",
        "payload": {"template": "t {channel_link}", "channel_link": "@proxgram"},
        "retry_count": 0,
    }
    task.update(overrides)
    return task


def _client_with(post, discussion_result="__default__", sent_id=555):
    client = AsyncMock()
    client.get_messages = AsyncMock(return_value=[post])
    # Awaited result of the top-level client(request) call — Telethon usage.
    if discussion_result == "__default__":
        from telethon.tl.types import PeerChannel

        dm = MagicMock()
        dm.id = 91
        dm.peer_id = PeerChannel(456)
        wrapper = MagicMock()
        wrapper.messages = [dm]
        client.return_value = wrapper
    else:
        client.return_value = discussion_result
    sent = MagicMock()
    sent.id = sent_id
    client.send_message = AsyncMock(return_value=sent)
    client.get_entity = AsyncMock(return_value=MagicMock(name="discussion_group"))
    return client


def _post(msg_id=90):
    post = MagicMock()
    post.id = msg_id
    post.service = False
    return post


def _discussion_result(msg_id=91):
    """messages.DiscussionMessage-shaped result with one thread-origin message."""
    from telethon.tl.types import PeerChannel

    dm = MagicMock()
    dm.id = msg_id
    dm.peer_id = PeerChannel(456)  # real TL type: utils.get_peer_id must cast it
    wrapper = MagicMock()
    wrapper.messages = [dm]
    return wrapper


# ------------------------------------------------------- broadcast flow ----

def test_broadcast_channel_detected_only_for_broadcast_entities():
    assert TaskDispatcher._is_broadcast_channel(_channel(broadcast=True)) is True
    assert TaskDispatcher._is_broadcast_channel(_channel(broadcast=False, megagroup=True)) is False
    assert TaskDispatcher._is_broadcast_channel(MagicMock(name="Chat")) is False
    assert TaskDispatcher._is_broadcast_channel(MagicMock(name="User")) is False


def test_comment_in_discussion_posts_reply_in_group_and_returns_id():
    d = TaskDispatcher()
    client = _client_with(_post(90), sent_id=555)

    sent_id, group = asyncio.run(
        d._comment_in_discussion(client, _channel(), "-100123", "hello", _task())
    )

    assert sent_id == 555
    assert group is client.get_entity.return_value
    # Latest post fetched from the channel itself
    client.get_messages.assert_awaited_once()
    # Discussion resolved through the TL request
    from telethon.tl.functions.messages import GetDiscussionMessageRequest

    request = client.await_args.args[0]
    assert isinstance(request, GetDiscussionMessageRequest)
    assert request.msg_id == 90
    # Comment sent as a reply to the thread origin in the discussion group
    send_args, send_kwargs = client.send_message.call_args
    assert send_kwargs.get("reply_to") == 91
    assert send_args[0] is client.get_entity.return_value


def test_comment_without_discussion_raises_no_discussion_error():
    d = TaskDispatcher()
    # Telegram answers with an empty message list when comments are locked.
    empty = MagicMock()
    empty.messages = []
    client = _client_with(_post(90), discussion_result=empty)

    with pytest.raises(NoDiscussionGroupError):
        asyncio.run(
            d._comment_in_discussion(client, _channel(), "-100123", "hello", _task())
        )


def test_comment_on_channel_without_posts_raises_no_discussion_error():
    d = TaskDispatcher()
    client = _client_with(_post())
    client.get_messages = AsyncMock(return_value=[])  # channel history is empty

    with pytest.raises(NoDiscussionGroupError):
        asyncio.run(
            d._comment_in_discussion(client, _channel(), "-100123", "hello", _task())
        )


@pytest.mark.asyncio
async def test_execute_task_send_message_uses_discussion_flow_for_broadcast():
    d = TaskDispatcher()
    channel = _channel()
    client = _client_with(_post(90), sent_id=555)

    with patch.object(d.limiter, "wait_jitter", new_callable=AsyncMock), \
         patch.object(d, "_resolve_entity", new_callable=AsyncMock, return_value=channel), \
         patch.object(d, "_verify_delivery", new_callable=AsyncMock, return_value=555) as vd, \
         patch.object(d, "_record_delivery") as rec:
        assert await d.execute_task(client, _task()) is True

    # Read-back must target the discussion group, not the channel
    vd.assert_awaited_once()
    assert vd.call_args.args[1] is client.get_entity.return_value
    rec.assert_called_once()


@pytest.mark.asyncio
async def test_execute_task_regular_channel_send_message_unchanged():
    d = TaskDispatcher()
    client = AsyncMock()
    sent = MagicMock()
    sent.id = 42
    client.send_message = AsyncMock(return_value=sent)

    with patch.object(d.limiter, "wait_jitter", new_callable=AsyncMock), \
         patch.object(d, "_resolve_entity", new_callable=AsyncMock, return_value=MagicMock()), \
         patch.object(d, "_verify_delivery", new_callable=AsyncMock, return_value=None):
        assert await d.execute_task(client, _task()) is True

    client.send_message.assert_awaited_once()
    assert client.send_message.call_args.args[1] == "t @proxgram"


# ------------------------------------------------- SKIPPED status paths ----

def test_skip_task_marks_skipped_without_retry_burn():
    d = TaskDispatcher()
    conn, cur = MagicMock(), MagicMock()
    cur.rowcount = 1
    conn.cursor.return_value.__enter__.return_value = cur
    ctx = MagicMock()
    ctx.__enter__.return_value = conn
    with patch("src.core.dispatcher.get_db_connection", return_value=ctx):
        d.skip_task(7, "no discussion group")
    sqls = [c.args[0] for c in cur.execute.call_args_list]
    assert any("SET status = 'SKIPPED'" in s for s in sqls)
    assert any("TASK_SKIPPED" in s for s in sqls)
    # No retry_count increment anywhere in the UPDATE
    assert not any("retry_count + 1" in s for s in sqls)


@pytest.mark.asyncio
async def test_process_next_task_marks_skipped_on_no_discussion_error():
    d = TaskDispatcher()
    account = {"id": 2, "session_string": "s", "proxy": None}
    client = AsyncMock()
    client.disconnect = AsyncMock()

    with patch.object(d.limiter, "is_quiet_hours", return_value=False), \
         patch.object(d, "claim_next_task", return_value=_task()), \
         patch.object(d.account_manager, "get_active_accounts", return_value=[account]), \
         patch.object(d.account_manager, "create_client", return_value=client), \
         patch.object(d.account_manager, "connect_with_fallback", new_callable=AsyncMock), \
         patch.object(d.account_manager, "update_account_status") as upd, \
         patch.object(client, "is_user_authorized", new_callable=AsyncMock, return_value=True), \
         patch.object(d, "execute_task",
                      side_effect=NoDiscussionGroupError("comments locked")), \
         patch.object(d, "skip_task") as skip, \
         patch.object(d, "update_task_status") as status:
        result = await d.process_next_task()

    assert result is False
    skip.assert_called_once_with(7, "comments locked")
    status.assert_not_called()          # never marked FAILED
    upd.assert_not_called()             # account not penalized
    client.disconnect.assert_awaited_once()


# ------------------------------------------------------ manual retry -------

def test_retry_task_requeues_failed_with_retries_left():
    d = TaskDispatcher()
    conn, cur = MagicMock(), MagicMock()
    cur.rowcount = 1
    conn.cursor.return_value.__enter__.return_value = cur
    ctx = MagicMock()
    ctx.__enter__.return_value = conn
    with patch("src.core.dispatcher.get_db_connection", return_value=ctx), \
         patch.dict("os.environ", {"GROWTH_MAX_RETRIES": "3"}):
        assert d.retry_task(7) is True
    sql = cur.execute.call_args_list[0].args[0]
    assert "status = 'PENDING'" in sql
    assert "status = 'FAILED'" in sql
    assert "retry_count < %s" in sql
    assert cur.execute.call_args_list[0].args[1] == (7, 3)


def test_retry_task_refuses_maxed_out_task():
    d = TaskDispatcher()
    conn, cur = MagicMock(), MagicMock()
    cur.rowcount = 0
    conn.cursor.return_value.__enter__.return_value = cur
    ctx = MagicMock()
    ctx.__enter__.return_value = conn
    with patch("src.core.dispatcher.get_db_connection", return_value=ctx):
        assert d.retry_task(7) is False
