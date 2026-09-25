"""Auto-pruning: terminal comment-ineligibility exceptions must SKIPPED the
task, hard-DELETE the target row from target_channels, and write a
TARGET_AUTO_DELETED audit row — while transient errors (FloodWait,
connection loss) must NEVER prune. DB mocked."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telethon.errors import (
    ChannelPrivateError,
    ChatAdminRequiredError,
    ChatWriteForbiddenError,
    FloodWaitError,
    InviteRequestSentError,
    MsgIdInvalidError,
    UserBannedInChannelError,
)

from src.core.dispatcher import NoDiscussionGroupError, TaskDispatcher


def _task(*, id=7, target="-1003481813519", action_type="SEND_MESSAGE",
          payload=None, retry_count=0):
    return {
        "id": id,
        "target": target,
        "action_type": action_type,
        "payload": payload or {"template": "t {channel_link}", "channel_link": "@p"},
        "retry_count": retry_count,
    }


def _dispatch_with_exception(exc, target="-1003481813519"):
    """Run process_next_task with execute_task raising the given exception."""
    d = TaskDispatcher()
    account = {"id": 2, "session_string": "s", "proxy": None}
    client = AsyncMock()
    client.disconnect = AsyncMock()

    with patch.object(d.limiter, "is_quiet_hours", return_value=False), \
         patch.object(d, "claim_next_task", return_value=_task(id=7, target=target)), \
         patch.object(d.account_manager, "get_active_accounts", return_value=[account]), \
         patch.object(d.account_manager, "create_client", return_value=client), \
         patch.object(d.account_manager, "connect_with_fallback", new_callable=AsyncMock), \
         patch.object(client, "is_user_authorized", new_callable=AsyncMock, return_value=True), \
         patch.object(d.account_manager, "update_account_status") as upd, \
         patch.object(d, "execute_task", side_effect=exc), \
         patch.object(d, "skip_task") as skip, \
         patch.object(d, "update_task_status") as status:
        result = asyncio.run(d.process_next_task())

    return d, skip, status, upd, client


# ---------------------------------------------- terminal → skip + prune ----

TERMINAL_EXC_TYPES = [
    MsgIdInvalidError,        # no linked discussion group
    ChannelPrivateError,      # discussion private / unreachable
    InviteRequestSentError,   # join-request gated
    ChatWriteForbiddenError,  # write forbidden in group
    UserBannedInChannelError, # account banned in group
    ChatAdminRequiredError,   # admin rights required
]


@pytest.mark.parametrize("exc_type", TERMINAL_EXC_TYPES)
def test_terminal_exceptions_wrapped_by_discussion_flow_skip_and_prune(exc_type):
    """Inside the discussion flow every terminal exception is converted to
    NoDiscussionGroupError (the wrapping itself is covered in
    test_broadcast.py); process_next_task must then skip AND prune."""
    raw = exc_type(request=None)
    wrapped = NoDiscussionGroupError(f"terminal: {raw}")
    _, skip, status, _, _ = _dispatch_with_exception(wrapped)

    skip.assert_called_once()
    args, kwargs = skip.call_args
    assert args[0] == 7                       # task id
    assert "terminal" in args[1]
    assert kwargs.get("prune_target") == "-1003481813519"
    status.assert_not_called()                # never plain-FAILED


@pytest.mark.parametrize("exc_type", [
    UserBannedInChannelError,  # raw (e.g. COMMENT_REPLY action sends)
    ChatWriteForbiddenError,
    ChatAdminRequiredError,
])
def test_raw_permission_failures_skip_and_prune(exc_type):
    """Permission errors escaping outside the discussion flow are caught
    directly in process_next_task and equally prune the target."""
    _, skip, status, _, _ = _dispatch_with_exception(exc_type(request=None))

    skip.assert_called_once()
    args, kwargs = skip.call_args
    assert args[0] == 7
    assert "Permission failure" in args[1]
    assert kwargs.get("prune_target") == "-1003481813519"
    status.assert_not_called()


def test_no_discussion_group_error_prunes_target():
    _, skip, status, _, _ = _dispatch_with_exception(
        NoDiscussionGroupError("no comment thread (comments disabled)"))

    skip.assert_called_once_with(7,
        "no comment thread (comments disabled)",
        prune_target="-1003481813519")
    status.assert_not_called()


@pytest.mark.asyncio
async def test_execute_task_chat_admin_required_wraps_for_pruning():
    """CHAT_ADMIN_REQUIRED during the discussion flow must surface as a
    NoDiscussionGroupError so process_next_task prunes the target."""
    d = TaskDispatcher()
    from unittest.mock import MagicMock as M

    group = M()
    group.id = 91
    peer = M()
    peer.channel_id = 456
    group.peer_id = M()
    discussion = M()
    discussion.messages = [group]

    client = AsyncMock()
    client.get_messages = AsyncMock(return_value=[M(id=90, service=False)])
    client.return_value = discussion
    client.send_message = AsyncMock(side_effect=ChatAdminRequiredError(request=None))

    with patch.object(d.limiter, "wait_jitter", new_callable=AsyncMock):
        with pytest.raises(NoDiscussionGroupError):
            await d._comment_in_discussion(
                client, M(broadcast=True), "-100x", "hi", _task()
            )


# ------------------------------------------- skip_task DB-side behavior ----

def _skip_ctx():
    conn, cur = MagicMock(), MagicMock()
    cur.rowcount = 1
    conn.cursor.return_value.__enter__.return_value = cur
    ctx = MagicMock()
    ctx.__enter__.return_value = conn
    return conn, cur, ctx


def test_skip_task_with_prune_deletes_target_and_audits():
    d = TaskDispatcher()
    conn, cur, ctx = _skip_ctx()
    with patch("src.core.dispatcher.get_db_connection", return_value=ctx):
        d.skip_task(7, "no discussion", prune_target="@dead_channel")

    sqls = [c.args[0] for c in cur.execute.call_args_list]
    assert any("SET status = 'SKIPPED'" in s for s in sqls)
    assert any("TASK_SKIPPED" in s for s in sqls)
    assert any(s.strip() == "DELETE FROM target_channels WHERE target = %s;" for s in sqls)
    assert any("TARGET_AUTO_DELETED" in s for s in sqls)

    # DELETE parameterized with the exact target string
    delete_call = [c for c in cur.execute.call_args_list
                   if "DELETE FROM target_channels" in c.args[0]][0]
    assert delete_call.args[1] == ("@dead_channel",)

    # Audit message includes target + reason + task id
    audit_call = [c for c in cur.execute.call_args_list
                  if "TARGET_AUTO_DELETED" in c.args[0]][0]
    assert "@dead_channel" in audit_call.args[1][0]
    assert "no discussion" in audit_call.args[1][0]
    assert "#7" in audit_call.args[1][0]

    # One commit for the whole transaction
    conn.commit.assert_called_once()


def test_skip_task_without_prune_leaves_target_alone():
    d = TaskDispatcher()
    conn, cur, ctx = _skip_ctx()
    with patch("src.core.dispatcher.get_db_connection", return_value=ctx):
        d.skip_task(7, "no discussion")
    sqls = [c.args[0] for c in cur.execute.call_args_list]
    assert not any("DELETE FROM target_channels" in s for s in sqls)
    assert not any("TARGET_AUTO_DELETED" in s for s in sqls)


def test_skip_task_prune_still_audits_when_row_missing():
    """Target already gone (rowcount 0) — audit row still written, no crash."""
    d = TaskDispatcher()
    conn, cur, ctx = _skip_ctx()
    cur.rowcount = 0
    with patch("src.core.dispatcher.get_db_connection", return_value=ctx):
        d.skip_task(7, "gone", prune_target="@vanished")
    sqls = [c.args[0] for c in cur.execute.call_args_list]
    assert any("TARGET_AUTO_DELETED" in s for s in sqls)
    conn.commit.assert_called_once()


# ------------------------------------- transient errors must NOT prune -----

def test_flood_wait_requeues_and_never_prunes():
    fwe = FloodWaitError(request=None)
    fwe.seconds = 42
    _, skip, status, _, _ = _dispatch_with_exception(fwe)

    skip.assert_not_called()
    status.assert_called_once()
    args, kwargs = status.call_args.args, status.call_args.kwargs
    assert args[0] == 7 and args[1] == "PENDING"
    assert "FloodWait" in kwargs.get("error_message", "")


def test_connection_error_requeues_and_never_prunes():
    _, skip, status, _, _ = _dispatch_with_exception(
        ConnectionError("all transports failed"))

    skip.assert_not_called()
    status.assert_called_once()
    args = status.call_args.args
    assert args[1] == "PENDING"


def test_generic_execution_error_fails_but_never_prunes():
    _, skip, status, upd, _ = _dispatch_with_exception(
        RuntimeError("unexpected condition"))

    skip.assert_not_called()
    status.assert_called_once()
    args = status.call_args.args
    assert args[1] == "FAILED"
    # Account health bookkeeping still applies on generic errors
    upd.assert_called_once()
