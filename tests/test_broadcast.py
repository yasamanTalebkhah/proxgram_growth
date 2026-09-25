"""Broadcast-channel commenting: GetDiscussionMessageRequest flow resolved
exactly like Telethon's own comment support (thread origin = lowest-id
message, discussion chat matched by channel_id), explicit discussion-group
dispatch, auto-join, SKIPPED terminal status, and manual retry."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telethon.errors import (
    ChannelPrivateError,
    ChatAdminRequiredError,
    ChatWriteForbiddenError,
    InviteRequestSentError,
    MsgIdInvalidError,
    UserAlreadyParticipantError,
    UserBannedInChannelError,
)
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import GetDiscussionMessageRequest
from telethon.tl.types import Channel, PeerChannel

from src.core.dispatcher import NoDiscussionGroupError, TaskDispatcher


def _channel(broadcast=True, megagroup=False):
    chat = MagicMock()
    chat.__class__.__name__ = "Channel"
    chat.broadcast = broadcast
    chat.megagroup = megagroup
    return chat


def _task(*, id=7, target="-1003481813519", action_type="SEND_MESSAGE", payload=None,
          retry_count=0):
    return {
        "id": id,
        "target": target,
        "action_type": action_type,
        "payload": payload or {"template": "t {channel_link}", "channel_id": "@proxgram"},
        "retry_count": retry_count,
    }


def _post(msg_id=90):
    post = MagicMock()
    post.id = msg_id
    post.service = False
    return post


def _group_channel(cid=456, access_hash=12345):
    """A real megagroup Channel as embedded in messages.DiscussionMessage.chats."""
    return Channel(id=cid, title="discussion", megagroup=True, photo=None,
                   date=None, access_hash=access_hash)


def _discussion_result(messages, chats):
    """messages.DiscussionMessage-shaped result."""
    wrapper = MagicMock()
    wrapper.messages = messages
    wrapper.chats = chats
    return wrapper


def _origin_message(msg_id, channel_id):
    m = MagicMock()
    m.id = msg_id
    peer = MagicMock()
    peer.channel_id = channel_id
    m.peer_id = PeerChannel(channel_id)  # real TL type for peer matching
    m.peer_id.channel_id = channel_id
    return m


def _client(post=None, discussion_result=None, sent_id=555):
    client = AsyncMock()
    client.get_messages = AsyncMock(return_value=[post if post is not None else _post()])
    # Awaited result of the top-level client(request) call — used for both
    # GetDiscussionMessageRequest and JoinChannelRequest.
    client.return_value = discussion_result
    sent = MagicMock()
    sent.id = sent_id
    client.send_message = AsyncMock(return_value=sent)
    return client


def _run_comment(d, client):
    return asyncio.run(
        d._comment_in_discussion(client, _channel(), "-100123", "hello", _task())
    )


# ------------------------------------------------------- broadcast flow ----

def test_resolve_upgrades_bare_input_peer_to_full_channel():
    """t.me/username targets resolve to a bare InputPeerChannel with no
    broadcast flag; the dispatcher must upgrade it via GetChannelsRequest
    so broadcast detection works (root cause of admin-required failures)."""
    from telethon.tl.functions.channels import GetChannelsRequest
    from telethon.tl.types import InputChannel, InputPeerChannel

    d = TaskDispatcher()
    client = AsyncMock()
    bare = InputPeerChannel(channel_id=777, access_hash=42)
    client.get_input_entity = AsyncMock(return_value=bare)
    full = _group_channel(777, 42)
    full.broadcast = True
    client.return_value = MagicMock(chats=[full])

    entity = asyncio.run(d._resolve_entity(client, "https://t.me/somechannel"))

    assert entity is full
    req = client.await_args.args[0]
    assert isinstance(req, GetChannelsRequest)
    assert isinstance(req.id[0], InputChannel)
    assert req.id[0].channel_id == 777


def test_resolve_keeps_bare_peer_when_upgrade_fails():
    d = TaskDispatcher()
    client = AsyncMock()
    from telethon.tl.types import InputPeerChannel

    client.get_input_entity = AsyncMock(
        return_value=InputPeerChannel(channel_id=777, access_hash=42))
    client.side_effect = RuntimeError("network down")

    entity = asyncio.run(d._resolve_entity(client, "https://t.me/somechannel"))
    assert type(entity).__name__ == "InputPeerChannel"  # degraded, not crashed


def test_broadcast_channel_detected_only_for_broadcast_entities():
    assert TaskDispatcher._is_broadcast_channel(_channel(broadcast=True)) is True
    assert TaskDispatcher._is_broadcast_channel(_channel(broadcast=False, megagroup=True)) is False
    assert TaskDispatcher._is_broadcast_channel(MagicMock(name="Chat")) is False
    assert TaskDispatcher._is_broadcast_channel(MagicMock(name="Chat", megagroup=True)) is False


def test_comment_sends_to_discussion_group_not_channel():
    d = TaskDispatcher()
    group = _group_channel(456, 12345)
    # messages[0] is the group-side origin; a second higher-id entry
    # mimics the channel-side post also present in real responses.
    origin = _origin_message(91, 456)
    channel_side = _origin_message(999, 789)
    client = _client(discussion_result=_discussion_result(
        [channel_side, origin], [_group_channel(789, 555), group]))

    sent_id, read_entity = _run_comment(d, client)

    assert sent_id == 555
    # read_entity is the InputPeerChannel built from the embedded group chat
    assert getattr(read_entity, "channel_id", None) == 456
    assert type(read_entity).__name__ == "InputPeerChannel"
    client.get_messages.assert_awaited_once()
    # Discussion resolved through the TL request on the channel peer
    request = client.await_args_list[0].args[0]
    assert isinstance(request, GetDiscussionMessageRequest)
    assert request.msg_id == 90
    # Comment sent TO THE DISCUSSION GROUP as a reply to the thread origin
    send_args, send_kwargs = client.send_message.call_args
    assert type(send_args[0]).__name__ == "InputPeerChannel"
    assert send_args[0].channel_id == 456
    assert send_kwargs.get("reply_to") == 91


def test_comment_auto_joins_discussion_group_before_sending():
    d = TaskDispatcher()
    group = _group_channel(456, 12345)
    client = _client(discussion_result=_discussion_result(
        [_origin_message(91, 456)], [group]))

    _run_comment(d, client)

    # Join request issued against the discussion entity...
    join_request = client.await_args_list[1].args[0]
    assert isinstance(join_request, JoinChannelRequest)
    # ...and the send followed it.
    client.send_message.assert_awaited_once()


def test_comment_already_participant_is_ignored_and_send_proceeds():
    d = TaskDispatcher()
    group = _group_channel(456, 12345)
    client = _client(discussion_result=_discussion_result(
        [_origin_message(91, 456)], [group]))

    def _call_side_effect(request, *a, **kw):
        if isinstance(request, JoinChannelRequest):
            raise UserAlreadyParticipantError(request=request)
        return client.return_value

    client.side_effect = _call_side_effect

    sent_id, _ = _run_comment(d, client)
    assert sent_id == 555
    client.send_message.assert_awaited_once()


def test_comment_private_discussion_on_get_raises_skip():
    d = TaskDispatcher()
    group = _group_channel(456, 12345)
    client = _client(discussion_result=_discussion_result(
        [_origin_message(91, 456)], [group]))

    def _call_side_effect(request, *a, **kw):
        raise ChannelPrivateError(request=request)

    client.side_effect = _call_side_effect

    with pytest.raises(NoDiscussionGroupError):
        _run_comment(d, client)
    client.send_message.assert_not_awaited()


def test_comment_private_group_on_join_raises_skip():
    d = TaskDispatcher()
    group = _group_channel(456, 12345)
    client = _client(discussion_result=_discussion_result(
        [_origin_message(91, 456)], [group]))

    def _call_side_effect(request, *a, **kw):
        if isinstance(request, JoinChannelRequest):
            raise ChannelPrivateError(request=request)
        return client.return_value

    client.side_effect = _call_side_effect

    with pytest.raises(NoDiscussionGroupError, match="join approval"):
        _run_comment(d, client)
    client.send_message.assert_not_awaited()


def test_comment_locked_thread_maps_write_forbidden_to_skip():
    d = TaskDispatcher()
    group = _group_channel(456, 12345)
    client = _client(discussion_result=_discussion_result(
        [_origin_message(91, 456)], [group]))
    client.send_message = AsyncMock(side_effect=ChatWriteForbiddenError(request=None))

    with pytest.raises(NoDiscussionGroupError, match="Comments locked"):
        _run_comment(d, client)


def test_comment_admin_required_maps_to_skip():
    """The live-fire failure: ChatAdminRequiredError must SKIPPED, not retry-loop."""
    d = TaskDispatcher()
    group = _group_channel(456, 12345)
    client = _client(discussion_result=_discussion_result(
        [_origin_message(91, 456)], [group]))
    client.send_message = AsyncMock(side_effect=ChatAdminRequiredError(request=None))

    with pytest.raises(NoDiscussionGroupError):
        _run_comment(d, client)


def test_comment_banned_in_group_maps_to_skip():
    d = TaskDispatcher()
    group = _group_channel(456, 12345)
    client = _client(discussion_result=_discussion_result(
        [_origin_message(91, 456)], [group]))
    client.send_message = AsyncMock(side_effect=UserBannedInChannelError(request=None))

    with pytest.raises(NoDiscussionGroupError):
        _run_comment(d, client)


def test_comment_join_request_sent_maps_to_skip():
    """Approval-gated groups raise InviteRequestSentError on join — terminal."""
    d = TaskDispatcher()
    group = _group_channel(456, 12345)
    client = _client(discussion_result=_discussion_result(
        [_origin_message(91, 456)], [group]))

    def _call_side_effect(request, *a, **kw):
        if isinstance(request, JoinChannelRequest):
            raise InviteRequestSentError(request=request)
        return client.return_value

    client.side_effect = _call_side_effect

    with pytest.raises(NoDiscussionGroupError, match="join approval"):
        _run_comment(d, client)
    client.send_message.assert_not_awaited()


def test_comment_msg_id_invalid_on_get_maps_to_skip():
    """MSG_ID_INVALID from GetDiscussionMessageRequest = no discussion linked."""
    d = TaskDispatcher()
    group = _group_channel(456, 12345)
    client = _client(discussion_result=_discussion_result(
        [_origin_message(91, 456)], [group]))

    def _call_side_effect(request, *a, **kw):
        if isinstance(request, GetDiscussionMessageRequest):
            raise MsgIdInvalidError(request=request)
        return client.return_value

    client.side_effect = _call_side_effect

    with pytest.raises(NoDiscussionGroupError, match="no comment thread"):
        _run_comment(d, client)
    client.send_message.assert_not_awaited()


def test_comment_unresolvable_group_raises_skip():
    d = TaskDispatcher()
    # Origin's channel_id has no counterpart in discussion.chats
    client = _client(discussion_result=_discussion_result(
        [_origin_message(91, 999)], [_group_channel(456, 12345)]))

    with pytest.raises(NoDiscussionGroupError, match="could not be resolved"):
        _run_comment(d, client)


def test_comment_without_discussion_raises_no_discussion_error():
    d = TaskDispatcher()
    empty = MagicMock()
    empty.messages = []
    client = _client(discussion_result=empty)

    with pytest.raises(NoDiscussionGroupError):
        _run_comment(d, client)


def test_comment_on_channel_without_posts_raises_no_discussion_error():
    d = TaskDispatcher()
    client = _client()
    client.get_messages = AsyncMock(return_value=[])  # channel history is empty

    with pytest.raises(NoDiscussionGroupError):
        _run_comment(d, client)


@pytest.mark.asyncio
async def test_execute_task_send_message_uses_discussion_flow_for_broadcast():
    d = TaskDispatcher()
    channel = _channel()
    group = _group_channel(456, 12345)
    client = _client(discussion_result=_discussion_result(
        [_origin_message(91, 456)], [group]), sent_id=555)

    with patch.object(d.limiter, "wait_jitter", new_callable=AsyncMock), \
         patch.object(d, "_resolve_entity", new_callable=AsyncMock, return_value=channel), \
         patch.object(d, "_verify_delivery", new_callable=AsyncMock, return_value=555) as vd, \
         patch.object(d, "_record_delivery") as rec:
        assert await d.execute_task(client, _task()) is True

    # Read-back must target the discussion group's input peer, not the channel
    vd.assert_awaited_once()
    assert type(vd.call_args.args[1]).__name__ == "InputPeerChannel"
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
