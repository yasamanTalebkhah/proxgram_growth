"""Phase 2 discovery engine + API: keyword queue, exclusion dedupe, zero-join
validation (accept open comments, discard closed), FloodWait handling,
forward-graph extraction, and the /api/channels/discover endpoints."""

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
    UserBannedInChannelError,
)

from src.services import discovery
from src.dashboard.app import app

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


def _broadcast(username="persian_channel", title="کانال نمونه", participants=12000,
               banned_rights=None):
    chat = MagicMock()
    chat.__class__.__name__ = "Channel"
    chat.broadcast = True
    chat.username = username
    chat.title = title
    chat.participants_count = participants
    chat.default_banned_rights = banned_rights
    return chat


def _post(msg_id=90):
    post = MagicMock()
    post.id = msg_id
    post.service = False
    return post


def _validation_client(channel=None, discussion_messages=None, get_msgs=None,
                       get_entity_result="__default__"):
    """Mock client for validate_candidate: sync (non-awaited) methods."""
    c = MagicMock()
    if get_entity_result == "__default__":
        c.get_entity = MagicMock(return_value=channel or _broadcast())
    else:
        c.get_entity = MagicMock(return_value=get_entity_result)
    c.get_messages = MagicMock(return_value=[_post()] if get_msgs is None else get_msgs)
    wrapper = MagicMock()
    wrapper.messages = discussion_messages if discussion_messages is not None else [MagicMock(id=91)]
    c.return_value = wrapper
    return c


# ------------------------------------------------------------- keywords ----

def test_build_keyword_queue_defaults_cover_pools():
    queue = discovery.build_keyword_queue(None)
    for expected in ["نرخ ارز", "قیمت دلار", "صرافی", "تتر", "پروکسی", "کانفیگ",
                     "v2ray", "فیلترشکن", "mtproto", "اخبار", "دانشجویی"]:
        assert expected in queue


def test_build_keyword_queue_dedupes_and_prioritizes_custom():
    queue = discovery.build_keyword_queue(["پروکسی", "پروکسی", " طلای آبشده "])
    assert queue[0] == "پروکسی"           # custom first
    assert queue[1] == "طلای آبشده"
    # remaining pool keywords appended without duplicating
    assert len(queue) == len(set(queue))


def test_build_keyword_queue_respects_limit():
    assert len(discovery.build_keyword_queue(None, limit=3)) == 3


# ---------------------------------------------------------- exclusions -----

def test_filter_excluded_drops_known_rejected_and_pruned():
    discovery._REJECTED_CACHE.clear()
    discovery._REJECTED_CACHE.update(["@rejected1"])
    out = discovery.filter_excluded(
        ["@fresh", "@known", "@rejected1", "@pruned1", ""],
        known={"@known"}, pruned={"@pruned1"},
    )
    assert out == ["@fresh"]
    discovery._REJECTED_CACHE.clear()


def test_filter_excluded_is_case_insensitive():
    out = discovery.filter_excluded(["@Known"], known={"@known"}, pruned=set())
    assert out == []


def test_pruned_targets_parses_audit_messages():
    rows = [("Target 'https://t.me/dead' auto-pruned ... reason",)]
    conn, cur, fake = _db(fetchall=rows)
    with patch("src.services.discovery.get_db_connection", fake):
        assert discovery._pruned_targets() == {"https://t.me/dead"}


# ---------------------------------------------------------- fwd graph ------

def test_extract_fwd_usernames_pulls_named_forwards():
    msg = MagicMock()
    msg.fwd_from.from_name = "@forwarded_channel"
    other = MagicMock()
    other.fwd_from = None
    assert discovery._extract_fwd_usernames([msg, other]) == ["@forwarded_channel"]


def test_search_result_extraction_only_public_broadcasts():
    res = MagicMock()
    res.chats = [_broadcast("good"), _broadcast(username=None), MagicMock()]  # non-Channel
    assert discovery._extract_usernames_from_result(res) == ["@good"]


# ------------------------------------------------- zero-join validation ----

def test_validate_accepts_channel_with_open_comments():
    c = _validation_client(discussion_messages=[MagicMock(id=91)])
    verdict = discovery.validate_candidate(c, "@good_channel")
    assert verdict["ok"] is True and verdict["reason"] is None
    assert verdict["info"]["title"]


def test_validate_discards_channel_without_discussion():
    c = _validation_client(discussion_messages=[])
    verdict = discovery.validate_candidate(c, "@nothread")
    assert verdict["ok"] is False
    assert "no linked discussion" in verdict["reason"] or "comments disabled" in verdict["reason"]


@pytest.mark.parametrize("exc, expected_fragment", [
    (ChannelPrivateError(request=None), "private or join-gated"),
    (InviteRequestSentError(request=None), "private or join-gated"),
    (ChatWriteForbiddenError(request=None), "write-forbidden"),
    (UserBannedInChannelError(request=None), "banned in discussion"),
])
def test_validate_discards_permission_locked_discussions(exc, expected_fragment):
    c = _validation_client()
    c.side_effect = exc  # GetDiscussionMessageRequest raises
    verdict = discovery.validate_candidate(c, "@locked")
    assert verdict["ok"] is False
    assert expected_fragment in verdict["reason"]


def test_validate_discards_msgidinvalid_no_discussion():
    """MSG_ID_INVALID (the live 'comments disabled' signal) must be a clean discard."""
    from telethon.errors import MsgIdInvalidError

    c = _validation_client()
    c.side_effect = MsgIdInvalidError(request=None)
    verdict = discovery.validate_candidate(c, "@nocomments")
    assert verdict["ok"] is False
    assert "no comment thread" in verdict["reason"]


def test_validate_discards_private_and_non_broadcast():
    c = _validation_client(get_entity_result=ChannelPrivateError(request=None))
    assert discovery.validate_candidate(c, "@x")["ok"] is False

    megagroup = _broadcast(username="mg")
    megagroup.broadcast = False
    megagroup.megagroup = True
    assert discovery.validate_candidate(_validation_client(get_entity_result=megagroup),
                                        "@mg")["ok"] is False

    no_user = _broadcast(username=None)
    assert discovery.validate_candidate(_validation_client(get_entity_result=no_user),
                                        "@nouser")["ok"] is False


def test_validate_discards_default_banned_rights():
    rights = MagicMock()
    rights.send_messages = True
    muted = _broadcast(banned_rights=rights)
    assert "default banned rights" in discovery.validate_candidate(
        _validation_client(get_entity_result=muted), "@muted")["reason"]


def test_validate_never_calls_join():
    c = _validation_client(discussion_messages=[MagicMock(id=91)])
    discovery.validate_candidate(c, "@good")
    # The only awaited/request calls are reads; JoinChannelRequest is never imported
    # into the call path, and no call should be a join.
    from telethon.tl.functions.channels import JoinChannelRequest
    for call in c.mock_calls:
        assert "JoinChannelRequest" not in repr(call)


# --------------------------------------------------- FloodWait handling ----

def test_scan_keyword_raises_floodwait_for_pause():
    c = MagicMock()
    c.side_effect = FloodWaitError(request=None)
    with pytest.raises(FloodWaitError):
        discovery.scan_keyword(c, "پروکسی")


def test_pause_for_flood_wait_sleeps_requested_duration():
    with patch("src.services.discovery.time.sleep") as slept:
        waited = discovery._pause_for_flood_wait(FloodWaitError(request=None))
    assert waited >= 2
    slept.assert_called_once()


def test_jitter_within_required_range():
    with patch("src.services.discovery.time.sleep") as slept, \
         patch("src.services.discovery.random.uniform", return_value=30.0) as uni:
        discovery._respect_jitter()
    uni.assert_called_once_with(15, 45)
    slept.assert_called_once_with(30.0)


# ----------------------------------------------------------- ingestion -----

def test_ingest_valid_inserts_with_auto_discovered_tag_and_audits():
    conn, cur, fake = _db(fetchone=(33,))
    with patch("src.services.discovery.get_db_connection", fake):
        target_id = discovery.ingest_valid("@good_channel", {"title": "کانال", "participants": 5000})
    assert target_id == 33
    sqls = [c.args[0] for c in cur.execute.call_args_list]
    assert any("tag, enabled" in s and "auto_discovered" in s for s in sqls)
    assert any("CHANNEL_DISCOVERED" in s for s in sqls)
    audit_call = [c for c in cur.execute.call_args_list if "CHANNEL_DISCOVERED" in c.args[0]][0]
    audit = audit_call.args[1][1]  # (level, message) params tuple
    assert "کانال" in audit and "5000" in audit


def test_record_rejection_caches_and_logs():
    discovery._REJECTED_CACHE.clear()
    conn, cur, fake = _db()
    with patch("src.services.discovery.get_db_connection", fake):
        discovery.record_rejection("@bad", "comments disabled")
    assert "@bad" in discovery._REJECTED_CACHE
    assert any("CHANNEL_DISCOVERY_REJECTED" in c.args[0] for c in cur.execute.call_args_list)
    discovery._REJECTED_CACHE.clear()


# ---------------------------------------------------------- status/api -----

def test_status_endpoint_reports_metrics_and_pools():
    conn, cur, fake = _db(fetchone=(7,))
    with patch("src.services.discovery.get_db_connection", fake):
        data = client.get("/api/channels/discover/status").json()
    assert data["running"] is False
    assert data["auto_discovered_total"] == 7
    assert "پروکسی" in data["default_keywords"]
    assert set(data["keyword_pools"].keys()) == {"finance", "proxy", "traffic"}


def test_discover_endpoint_starts_background_run():
    with patch("src.api.routes.start_background_discovery",
               return_value={"ok": True, "detail": "discovery started in background"}) as bg:
        resp = client.post("/api/channels/discover",
                           json={"keywords": ["تتر"], "limit": 3})
    assert resp.status_code == 200
    bg.assert_called_once_with(keywords=["تتر"], limit=3, max_keywords=None)


def test_discover_endpoint_conflicts_when_running():
    with patch("src.api.routes.start_background_discovery",
               return_value={"ok": False, "detail": "discovery already running"}):
        resp = client.post("/api/channels/discover", json={})
    assert resp.status_code == 409


def test_discover_endpoint_validates_limit_range():
    assert client.post("/api/channels/discover", json={"limit": 0}).status_code == 422
    assert client.post("/api/channels/discover", json={"limit": 99}).status_code == 422
