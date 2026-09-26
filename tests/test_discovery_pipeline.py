"""Target Auto-Discovery & Discussion Validation Pipeline (Phases 1-3):

schema contract for discovered_targets, crawler normalization/extraction
(recommendations + forward chains, dedupe), validator linked_chat_id rule
(promote vs disqualify, terminal errors, FloodWait backoff), and the
/api/discovery trigger + stats endpoints.
"""

import asyncio
import sys
import os
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient
from telethon.errors import (
    ChannelPrivateError,
    ChatWriteForbiddenError,
    FloodWaitError,
    InviteRequestSentError,
    MsgIdInvalidError,
    UserBannedInChannelError,
    UsernameNotOccupiedError,
)
from telethon.tl.functions.messages import GetDiscussionMessageRequest

from src.dashboard.app import app
from src.services import discovery_crawler, discussion_validator

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


def _row(id=1, username="@cand", attempts=0):
    return {"id": id, "username": username, "attempts": attempts}


# ------------------------------------------------------------- Phase 1: DDL --

def test_schema_defines_discovered_targets():
    ddl = open(os.path.join("src", "database", "init_schema.sql"),
               encoding="utf-8").read()
    assert "CREATE TABLE IF NOT EXISTS discovered_targets" in ddl
    for fragment in [
        "username_or_link VARCHAR(255) UNIQUE NOT NULL",
        "source_seed VARCHAR(255)",
        "discovery_method VARCHAR(50) NOT NULL DEFAULT 'SIMILAR_CHANNELS'",
        "status VARCHAR(50) NOT NULL DEFAULT 'PENDING_VALIDATION'",
        "linked_chat_id BIGINT",
        "attempt_count INTEGER NOT NULL DEFAULT 0",
        "last_checked_at TIMESTAMP WITH TIME ZONE",
        "created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP",
    ]:
        assert fragment in ddl, fragment


# -------------------------------------------------- crawler: normalization --

def test_normalize_username_strips_at_urls_and_lowercases():
    assert discovery_crawler.normalize_username("@FooBar") == "@foobar"
    assert discovery_crawler.normalize_username("t.me/Channel_X") == "@channel_x"
    assert discovery_crawler.normalize_username("https://t.me/Baz?si=abc") == "@baz"
    assert discovery_crawler.normalize_username("telegram.me/plain") == "@plain"
    assert discovery_crawler.normalize_username("plain_handle") == "@plain_handle"


def test_normalize_username_rejects_invites_junk_and_reserved():
    assert discovery_crawler.normalize_username("t.me/+AbCdEf") is None
    assert discovery_crawler.normalize_username("t.me/joinchat/AbCdEf") is None
    assert discovery_crawler.normalize_username("t.me/proxy?server=x") is None
    assert discovery_crawler.normalize_username("ab") is None
    assert discovery_crawler.normalize_username("") is None
    assert discovery_crawler.normalize_username(None) is None
    assert discovery_crawler.normalize_username("has space") is None


def test_extract_link_candidates_from_text_and_buttons():
    msg = MagicMock()
    msg.message = "Join https://t.me/Great_Channel and t.me/OtherOne"
    button = MagicMock()
    button.url = "https://t.me/ButtonChan?boost"
    button.text = "visit t.me/from_text"
    button_row = MagicMock()
    button_row.buttons = [button]
    msg.reply_markup.rows = [button_row]
    out = discovery_crawler._extract_link_candidates(msg)
    assert "@great_channel" in out and "@otherone" in out
    assert "@buttonchan" in out and "@from_text" in out


def test_extract_link_candidates_drops_invite_links():
    msg = MagicMock()
    msg.message = "private invite: https://t.me/+SecretHash and t.me/joinchat/AbCd"
    msg.reply_markup = None
    assert discovery_crawler._extract_link_candidates(msg) == []


def test_extract_forward_candidates_skips_numeric_origins():
    named = MagicMock()
    named.fwd_from.from_id = None
    named.fwd_from.from_name = "@OriginChan"
    numeric = MagicMock()
    numeric.fwd_from.from_id.__class__.__name__ = "PeerChannel"
    plain = MagicMock()
    plain.fwd_from = None
    assert discovery_crawler._extract_forward_candidates(named) == ["@originchan"]
    assert discovery_crawler._extract_forward_candidates(numeric) == []
    assert discovery_crawler._extract_forward_candidates(plain) == []


def test_recommendation_candidates_parses_public_channels_only():
    good = MagicMock()
    good.__class__.__name__ = "Channel"
    good.broadcast = True
    good.username = "Recommended"
    no_username = MagicMock()
    no_username.__class__.__name__ = "Channel"
    no_username.broadcast = True
    no_username.username = None
    user_chat = MagicMock()
    user_chat.__class__.__name__ = "User"
    user_chat.username = "somebody"
    res = MagicMock()
    res.chats = [good, no_username, user_chat]
    assert discovery_crawler._recommendation_candidates(res) == ["@recommended"]


# -------------------------------------------------------- crawler: dedupe ----

def test_upsert_pending_inserts_with_pending_validation_defaults():
    conn, cur, fake = _db(rowcount=1)
    with patch("src.services.discovery_crawler.get_db_connection", fake):
        assert discovery_crawler.upsert_pending("@new", "@seed", "SIMILAR_CHANNELS") is True
    sql = cur.execute.call_args_list[0].args[0]
    assert "discovered_targets" in sql and "ON CONFLICT (username_or_link) DO NOTHING" in sql
    assert cur.execute.call_args_list[0].args[1] == ("@new", "@seed", "SIMILAR_CHANNELS")


def test_upsert_pending_returns_false_on_conflict():
    conn, cur, fake = _db(rowcount=0)
    with patch("src.services.discovery_crawler.get_db_connection", fake):
        assert discovery_crawler.upsert_pending("@dup", "@seed", "FORWARD_CHAIN") is False


def test_sweep_seed_dedupes_across_sources_pool_and_targets():
    rec_chat = MagicMock()
    rec_chat.__class__.__name__ = "Channel"
    rec_chat.broadcast = True
    rec_chat.username = "FreshRec"
    rec_res = MagicMock()
    rec_res.chats = [rec_chat]
    dup_chat = MagicMock()
    dup_chat.__class__.__name__ = "Channel"
    dup_chat.broadcast = True
    dup_chat.username = "DupTarget"
    rec_res.chats = [rec_chat, dup_chat]  # recs include known dup
    msg = MagicMock()
    msg.message = "check t.me/LinkedChan"
    msg.reply_markup = None
    msg.fwd_from = None
    hist = MagicMock()
    hist.messages = [msg]

    c = AsyncMock()
    c.get_entity = AsyncMock(return_value="peer")
    c.side_effect = [rec_res, hist]  # 1st call: recommendations, 2nd: history

    upserted = []
    with patch("src.services.discovery_crawler.upsert_pending",
               side_effect=lambda handle, seed, method: upserted.append((handle, method)) or True):
        stats = asyncio.run(discovery_crawler.sweep_seed(c, "@seedchan", known={"@duptarget"}))

    assert ("@freshrec", "SIMILAR_CHANNELS") in upserted
    assert ("@linkedchan", "FORWARD_CHAIN") in upserted
    assert all(handle != "@duptarget" for handle, _ in upserted)  # excluded via known
    assert len(upserted) == 2  # cross-source/in-sweep dedupe
    assert stats["added"] == 2 and stats["skipped"] == 1


def test_active_seeds_reads_enabled_targets_only():
    conn, cur, fake = _db(fetchall=[("@seed1",), ("@seed2",)])
    with patch("src.services.discovery_crawler.get_db_connection", fake):
        assert discovery_crawler.active_seeds() == ["@seed1", "@seed2"]
    assert "enabled = TRUE" in cur.execute.call_args_list[0].args[0]


def test_excluded_merges_target_channels_and_pool():
    conn, cur, fake = _db()
    cur.fetchall.side_effect = [[("@Known",)], [("https://t.me/pooled",)]]
    with patch("src.services.discovery_crawler.get_db_connection", fake):
        excluded = discovery_crawler._excluded()
    assert excluded == {"@known", "https://t.me/pooled"}


# -------------------------------------------------- crawler: run control -----

def _crawler_manager(client_mock):
    manager = MagicMock()
    manager.get_active_accounts.return_value = [
        {"session_string": "sess", "proxy": None}]
    manager.create_client.return_value = client_mock
    manager.connect_with_fallback = AsyncMock(return_value=client_mock)
    return manager


def test_run_crawl_floodwait_pauses_gracefully():
    with patch("src.accounts.manager.AccountManager", return_value=_crawler_manager(MagicMock())), \
         patch("src.services.discovery_crawler.active_seeds", return_value=["@seed"]), \
         patch("src.services.discovery_crawler._excluded", return_value=set()), \
         patch("src.services.discovery_crawler.sweep_seed",
               side_effect=FloodWaitError(request=None)), \
         patch("src.services.discovery_crawler.time.sleep") as slept:
        snap = discovery_crawler.run_crawl()
    assert snap["ok"] is True
    assert "FloodWait" in snap["error"]
    assert snap["running"] is False
    slept.assert_called_once()


def test_crawler_pause_for_flood_wait_sleeps_requested_duration():
    with patch("src.services.discovery_crawler.time.sleep") as slept:
        waited = discovery_crawler._pause_for_flood_wait(FloodWaitError(request=None))
    assert waited >= 2
    slept.assert_called_once()


def test_start_background_crawl_conflicts_when_running():
    discovery_crawler.CRAWLER_STATE.running = True
    try:
        result = discovery_crawler.start_background_crawl()
        assert result["ok"] is False and "already running" in result["detail"]
    finally:
        discovery_crawler.CRAWLER_STATE.running = False


# ------------------------------------------------------- validator: probing --

def _post(msg_id=90):
    post = MagicMock()
    post.id = msg_id
    post.service = False
    return post


def _validator_client(linked_chat_id=None, get_entity_error=None,
                      discussion_messages=None, discussion_error=None,
                      posts=None, get_msgs_error=None):
    """AsyncMock client for the two-step probe (Telethon calls are awaited).

    Call order: 1) GetFullChannelRequest -> full_chat.linked_chat_id,
    2) GetDiscussionMessageRequest -> discussion wrapper (or raises).
    get_messages returns the latest-post list for the probe.
    """
    c = AsyncMock()
    if get_entity_error:
        c.get_entity = AsyncMock(side_effect=get_entity_error)
        return c
    c.get_entity = AsyncMock(return_value="peer")
    c.get_messages = AsyncMock(
        side_effect=get_msgs_error) if get_msgs_error else AsyncMock(
        return_value=[_post()] if posts is None else posts)
    full = MagicMock()
    full.full_chat.linked_chat_id = linked_chat_id
    if discussion_error is not None:
        c.side_effect = [full, discussion_error]
    else:
        wrapper = MagicMock()
        wrapper.messages = [MagicMock(id=91)] if discussion_messages is None \
            else discussion_messages
        c.side_effect = [full, wrapper]
    return c


def test_validate_target_accepts_linked_chat_id_with_open_thread():
    c = _validator_client(12345)
    verdict = asyncio.run(
        discussion_validator.validate_target(c, "@good"))
    assert verdict["ok"] is True
    assert verdict["linked_chat_id"] == 12345
    # Step 2 actually probed the latest post's discussion thread
    assert any(isinstance(call.args[0], GetDiscussionMessageRequest)
               for call in c.await_args_list)


def test_validate_target_no_linked_chat_disqualifies_before_probe():
    c = _validator_client(None)
    verdict = asyncio.run(
        discussion_validator.validate_target(c, "@nochat"))
    assert verdict["ok"] is False
    assert "no discussion" in verdict["reason"]
    assert verdict["linked_chat_id"] is None
    # short-circuited: no post fetch, no GetDiscussionMessageRequest call
    assert not any(isinstance(call.args[0], GetDiscussionMessageRequest)
                   for call in c.await_args_list)


def test_validate_target_terminal_error_maps_to_disqualify():
    for err in (ChannelPrivateError(request=None), UsernameNotOccupiedError(request=None)):
        verdict = asyncio.run(discussion_validator.validate_target(
            _validator_client(get_entity_error=err), "@dead"))
        assert verdict["ok"] is False
        assert "terminal" in verdict["reason"]


def test_validate_target_unresolvable_handle_disqualifies():
    verdict = asyncio.run(discussion_validator.validate_target(
        _validator_client(get_entity_error=ValueError("no session")), "@ghost"))
    assert verdict["ok"] is False and "unresolvable" in verdict["reason"]


# ------------------------------------------- validator: zero-join probe ------

def test_validate_target_msgid_invalid_disqualifies():
    """Linked group exists but the latest post has no open thread (live
    false-positive class: @net_3rf)."""
    c = _validator_client(555, discussion_error=MsgIdInvalidError(request=None))
    verdict = asyncio.run(discussion_validator.validate_target(c, "@net_3rf"))
    assert verdict["ok"] is False
    assert "no open comment thread" in verdict["reason"]


def test_validate_target_join_approval_gated_disqualifies():
    """Discussion requires admin join approval (live false-positive class:
    @ln2ray) — rejected cleanly instead of promoting a blocked target."""
    c = _validator_client(556, discussion_error=InviteRequestSentError(request=None))
    verdict = asyncio.run(discussion_validator.validate_target(c, "@ln2ray"))
    assert verdict["ok"] is False
    assert "join-approval" in verdict["reason"]


def test_validate_target_private_or_forbidden_discussion_disqualifies():
    for err, fragment in ((ChannelPrivateError(request=None), "private"),
                          (ChatWriteForbiddenError(request=None), "write-forbidden"),
                          (UserBannedInChannelError(request=None), "banned")):
        c = _validator_client(557, discussion_error=err)
        verdict = asyncio.run(discussion_validator.validate_target(c, "@x"))
        assert verdict["ok"] is False and fragment in verdict["reason"]


def test_validate_target_unresolvable_thread_disqualifies():
    c = _validator_client(558, discussion_error=RuntimeError("weird rpc"))
    verdict = asyncio.run(discussion_validator.validate_target(c, "@x"))
    assert verdict["ok"] is False and "unresolvable" in verdict["reason"]


def test_validate_target_empty_discussion_disqualifies():
    c = _validator_client(559, discussion_messages=[])
    verdict = asyncio.run(discussion_validator.validate_target(c, "@x"))
    assert verdict["ok"] is False and "no linked discussion" in verdict["reason"]


def test_validate_target_channel_without_posts_disqualifies():
    c = _validator_client(560, posts=[])
    verdict = asyncio.run(discussion_validator.validate_target(c, "@empty"))
    assert verdict["ok"] is False and "no posts" in verdict["reason"]


def test_validate_target_post_fetch_failure_disqualifies_without_crash():
    c = _validator_client(561, get_msgs_error=RuntimeError("rpc timeout"))
    verdict = asyncio.run(discussion_validator.validate_target(c, "@x"))
    assert verdict["ok"] is False and "post fetch failed" in verdict["reason"]


def test_validate_target_discussion_floodwait_propagates():
    c = _validator_client(562, discussion_error=FloodWaitError(request=None))
    with pytest.raises(FloodWaitError):
        asyncio.run(discussion_validator.validate_target(c, "@x"))


# ----------------------------------------------------- validator: batching ---

def test_validate_batch_promotes_valid_and_disqualifies_rest():
    batch = [_row(1, "@good"), _row(2, "@bad")]
    with patch("src.services.discussion_validator.record_attempt"), \
         patch("src.services.discussion_validator._jitter_sleep", new_callable=AsyncMock), \
         patch("src.services.discussion_validator.validate_target",
               new_callable=AsyncMock,
               side_effect=[{"ok": True, "reason": None, "linked_chat_id": 777},
                            {"ok": False, "reason": "no discussion group linked",
                             "linked_chat_id": None}]), \
         patch("src.services.discussion_validator.mark_validated") as mark_ok, \
         patch("src.services.discussion_validator.mark_disqualified") as mark_bad, \
         patch("src.services.discussion_validator.promote_target", return_value=True) as promote, \
         patch("src.services.discussion_validator.audit"):
        counts = asyncio.run(discussion_validator.validate_batch(MagicMock(), batch))
    assert counts == {"checked": 2, "validated": 1, "disqualified": 1}
    mark_ok.assert_called_once_with(1, 777)
    mark_bad.assert_called_once_with(2)
    promote.assert_called_once_with("@good")


def test_validate_batch_terminal_error_disqualified_never_promoted():
    batch = [_row(9, "@dead")]
    with patch("src.services.discussion_validator.record_attempt"), \
         patch("src.services.discussion_validator._jitter_sleep", new_callable=AsyncMock), \
         patch("src.services.discussion_validator.validate_target",
               new_callable=AsyncMock,
               side_effect=ChannelPrivateError(request=None)), \
         patch("src.services.discussion_validator.mark_disqualified") as mark_bad, \
         patch("src.services.discussion_validator.promote_target") as promote, \
         patch("src.services.discussion_validator.audit"):
        counts = asyncio.run(discussion_validator.validate_batch(MagicMock(), batch))
    assert counts == {"checked": 1, "validated": 0, "disqualified": 1}
    mark_bad.assert_called_once_with(9)
    promote.assert_not_called()


def test_validate_batch_floodwait_leaves_remaining_targets_pending():
    batch = [_row(1, "@first"), _row(2, "@second"), _row(3, "@third")]
    probe = MagicMock(return_value={"ok": True, "reason": None, "linked_chat_id": 5})
    with patch("src.services.discussion_validator.record_attempt"), \
         patch("src.services.discussion_validator.validate_target", new_callable=AsyncMock, side_effect=[
             {"ok": True, "reason": None, "linked_chat_id": 5},
             FloodWaitError(request=None)]) as vt, \
         patch("src.services.discussion_validator.mark_validated"), \
         patch("src.services.discussion_validator.mark_disqualified") as mark_bad, \
         patch("src.services.discussion_validator.promote_target", return_value=True), \
         patch("src.services.discussion_validator.audit"):
        with pytest.raises(FloodWaitError):
            asyncio.run(discussion_validator.validate_batch(MagicMock(), batch))
    assert vt.call_count == 2          # @third never probed — stays PENDING_VALIDATION
    assert mark_bad.call_count == 0    # nothing dropped on FloodWait


def test_promote_target_inserts_enabled_with_auto_discovery_tag():
    conn, cur, fake = _db(rowcount=1)
    with patch("src.services.discussion_validator.get_db_connection", fake):
        assert discussion_validator.promote_target("@fresh") is True
    sql, params = cur.execute.call_args_list[0].args
    assert "'auto_discovery', TRUE" in sql
    assert "ON CONFLICT (target) DO NOTHING" in sql
    assert params == ("@fresh",)


def test_fetch_pending_reads_pending_validation_ordered():
    conn, cur, fake = _db(fetchall=[(4, "@old", 2), (7, "@newer", 0)])
    with patch("src.services.discussion_validator.get_db_connection", fake):
        rows = discussion_validator.fetch_pending(limit=10)
    sql = cur.execute.call_args_list[0].args[0]
    assert "status = 'PENDING_VALIDATION'" in sql
    assert "ORDER BY attempt_count ASC, id ASC" in sql
    assert cur.execute.call_args_list[0].args[1] == (10,)
    assert rows[0]["username"] == "@old" and rows[1]["attempts"] == 0


def test_run_validation_floodwait_pauses_without_dropping_pool():
    batch = [_row(1, "@one")]
    with patch("src.accounts.manager.AccountManager", return_value=_validator_manager(MagicMock())), \
         patch("src.services.discussion_validator.fetch_pending", return_value=batch), \
         patch("src.services.discussion_validator.validate_batch",
               side_effect=FloodWaitError(request=None)), \
         patch("src.services.discussion_validator.time.sleep") as slept:
        snap = discussion_validator.run_validation()
    assert snap["ok"] is True
    assert "FloodWait" in snap["error"]
    assert snap["counts"] == {"checked": 0, "validated": 0, "disqualified": 0}
    slept.assert_called_once()


def _validator_manager(client_mock):
    manager = MagicMock()
    manager.get_active_accounts.return_value = [
        {"session_string": "sess", "proxy": None}]
    manager.create_client.return_value = client_mock
    manager.connect_with_fallback = AsyncMock(return_value=client_mock)
    return manager


# --------------------------------------------------------------- pipeline API --

def test_stats_endpoint_returns_pool_counts_and_engine_state():
    with patch("src.api.discovery_routes.validator_status",
               return_value={"running": False, "pool": {"total": 5, "pending": 3,
                                                        "validated": 1, "disqualified": 1,
                                                        "failed": 0}}):
        data = client.get("/api/discovery/stats").json()
    assert data["pool"]["total"] == 5 and data["pool"]["pending"] == 3
    assert "crawler" in data
    assert data["crawler"]["running"] is False


def test_trigger_crawler_starts_background_run():
    with patch("src.api.discovery_routes.start_background_crawl",
               return_value={"ok": True, "detail": "crawler started in background"}) as bg:
        resp = client.post("/api/discovery/trigger-crawler")
    assert resp.status_code == 200
    bg.assert_called_once()


def test_trigger_crawler_conflicts_when_running():
    with patch("src.api.discovery_routes.start_background_crawl",
               return_value={"ok": False, "detail": "crawler already running"}):
        assert client.post("/api/discovery/trigger-crawler").status_code == 409


def test_trigger_validator_starts_background_run():
    with patch("src.api.discovery_routes.start_background_validation",
               return_value={"ok": True, "detail": "validator started in background"}) as bg:
        resp = client.post("/api/discovery/trigger-validator")
    assert resp.status_code == 200
    bg.assert_called_once()


def test_trigger_validator_conflicts_when_running():
    with patch("src.api.discovery_routes.start_background_validation",
               return_value={"ok": False, "detail": "validator already running"}):
        assert client.post("/api/discovery/trigger-validator").status_code == 409
