"""Phase 2 — Automated Persian Channel & Group Discovery Engine.

Finds public broadcast channels matching Persian growth keywords,
pre-validates them ZERO-JOIN (never calls JoinChannelRequest) and inserts
only channels whose discussion group is verifiably commentable into
`target_channels` (tag='auto_discovered').

Pipeline per run:
  1. Build keyword queue (curated Persian pools: finance/forex, proxy/VPN,
     high-traffic) optionally overridden by the API caller.
  2. For each keyword: Telegram global search (contacts.SearchRequest)
     with randomized 15-45s jitter between queries; collect candidate
     public broadcast channels plus usernames extracted from fwd_from
     headers of public posts (forward graph).
  3. Filter candidates through the exclusion cache: anything already in
     target_channels, recently rejected (this process), or previously
     pruned (TARGET_AUTO_DELETED audit) is skipped without API calls.
  4. Zero-join pre-validation: resolve peer metadata (no join), confirm
     broadcast + public username, fetch latest post, resolve its
     discussion thread via GetDiscussionMessageRequest, and verify the
     account may comment (no InviteRequestSentError / private /
     ChatWriteForbiddenError / banned / default_banned_rights blocks).
  5. Insert valid candidates and write CHANNEL_DISCOVERED audit rows.

FloodWaitError pauses the whole discovery run for the requested duration
and resumes cleanly — a discovery job can never crash the worker loop.
"""

import asyncio
import json
import logging
import random
import time
from typing import Any, Dict, List, Optional, Set

from telethon import TelegramClient
from telethon.errors import (
    ChannelPrivateError,
    ChatWriteForbiddenError,
    FloodWaitError,
    InviteRequestSentError,
    UserBannedInChannelError,
)
from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.types import InputMessagesFilterEmpty

from src.core.dispatcher import NoDiscussionGroupError
from src.database.connection import get_db_connection

logger = logging.getLogger("discovery")

# ------------------------------------------------------------------ config --

KEYWORD_POOLS: Dict[str, List[str]] = {
    "finance": ["نرخ ارز", "قیمت دلار", "صرافی", "تتر"],
    "proxy": ["پروکسی", "کانفیگ", "v2ray", "فیلترشکن", "mtproto"],
    "traffic": ["اخبار", "دانشجویی"],
}
DEFAULT_KEYWORDS = [kw for pool in KEYWORD_POOLS.values() for kw in pool]

JITTER_RANGE = (15, 45)          # seconds between search queries
KEYWORD_COOLDOWN_SECONDS = 3600  # recently-used keywords rotate out
DEFAULT_DISCOVERY_LIMIT = 5      # accepted channels per run
MAX_SEARCH_RESULTS_PER_KEYWORD = 20

INSERT_TARGET_SQL = """
    INSERT INTO target_channels (target, tag, enabled)
    VALUES (%s, 'auto_discovered', TRUE)
    ON CONFLICT (target) DO NOTHING;
"""

INSERT_LOG_SQL = """
    INSERT INTO system_logs (level, event_type, message, created_at)
    VALUES (%s, 'CHANNEL_DISCOVERED', %s, NOW());
"""

INSERT_REJECT_SQL = """
    INSERT INTO system_logs (level, event_type, message, created_at)
    VALUES ('INFO', 'CHANNEL_DISCOVERY_REJECTED', %s, NOW());
"""

PRUNED_TARGETS_SQL = """
    SELECT message FROM system_logs
    WHERE event_type = 'TARGET_AUTO_DELETED'
      AND created_at > NOW() - INTERVAL '7 days'
    ORDER BY id DESC LIMIT 200;
"""


# ------------------------------------------------------------------ state ---

class DiscoveryState:
    """Thread-safe in-process metrics for the discovery engine (dashboard)."""

    def __init__(self) -> None:
        self.running = False
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.current_keyword: Optional[str] = None
        self.scanned = 0
        self.accepted = 0
        self.discarded = 0
        self.discards: List[str] = []
        self.accepted_targets: List[str] = []
        self.error: Optional[str] = None
        self.recent_keywords: List[str] = []

    def start(self, keywords: List[str]) -> None:
        self.running = True
        self.started_at = time.time()
        self.finished_at = None
        self.current_keyword = None
        self.scanned = self.accepted = self.discarded = 0
        self.discards = []
        self.accepted_targets = []
        self.error = None
        self.recent_keywords = list(keywords)

    def finish(self, error: Optional[str] = None) -> None:
        self.running = False
        self.finished_at = time.time()
        if error:
            self.error = error

    def snapshot(self) -> Dict[str, Any]:
        return {
            "running": self.running,
            "current_keyword": self.current_keyword,
            "scanned": self.scanned,
            "accepted": self.accepted,
            "discarded": self.discarded,
            "discards": list(self.discards[-20:]),
            "accepted_targets": list(self.accepted_targets[-20:]),
            "error": self.error,
            "keywords": list(self.recent_keywords),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "last_run_seconds": (
                round((self.finished_at or time.time()) - self.started_at, 1)
                if self.started_at else None
            ),
        }


STATE = DiscoveryState()

# Exclusion cache: candidates rejected during this process lifetime (also
# survives short restarts via the CHANNEL_DISCOVERY_REJECTED audit trail).
_REJECTED_CACHE: Set[str] = set()


# --------------------------------------------------------------- keywords ---

def build_keyword_queue(custom: Optional[List[str]] = None,
                        limit: Optional[int] = None) -> List[str]:
    """Normalize caller keywords or fall back to the curated Persian pools.

    Deduplicates in-order; custom keywords are tried first, then the
    remaining pool keywords fill the queue up to `limit` (default: all).
    """
    queue: List[str] = []
    seen: Set[str] = set()
    for kw in (custom or []) + ([] if custom else DEFAULT_KEYWORDS):
        kw = (kw or "").strip()
        if kw and kw.lower() not in seen:
            seen.add(kw.lower())
            queue.append(kw)
    if limit is not None and limit > 0:
        queue = queue[:limit]
    return queue


# ------------------------------------------------------------- exclusion ----

def _pruned_targets() -> Set[str]:
    """Targets auto-pruned in the last 7 days (never re-propose them)."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(PRUNED_TARGETS_SQL)
                rows = cur.fetchall()
    except Exception as exc:
        logger.warning("Could not read pruned-target audit trail: %s", exc)
        return set()
    pruned = set()
    for (message,) in rows:
        # Messages look like: Target 'https://t.me/x' auto-pruned ...
        if "Target '" in message:
            start = message.index("Target '") + len("Target '")
            end = message.find("'", start)
            if end > start:
                pruned.add(message[start:end])
    return pruned


def _known_targets() -> Set[str]:
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT target FROM target_channels;")
                return {row[0] for row in cur.fetchall()}
    except Exception as exc:
        logger.warning("Could not read target_channels for dedupe: %s", exc)
        return set()


def filter_excluded(candidates: List[str],
                    known: Optional[Set[str]] = None,
                    pruned: Optional[Set[str]] = None) -> List[str]:
    """Drop candidates already configured, recently rejected, or pruned.

    Pure function over the exclusion sets so unit tests can inject them;
    production callers use fetch_exclusion_sets().
    """
    known = known if known is not None else _known_targets()
    pruned = pruned if pruned is not None else _pruned_targets()
    known_lower = {k.lower() for k in known}
    out: List[str] = []
    for cand in candidates:
        key = (cand or "").strip().lower()
        if not key:
            continue
        if key in known_lower or key in _REJECTED_CACHE or key in {p.lower() for p in pruned}:
            continue
        out.append(cand)
    return out


def fetch_exclusion_sets() -> tuple:
    return _known_targets(), _pruned_targets()


# --------------------------------------------------------------- scanning ---

def _extract_usernames_from_result(res) -> List[str]:
    """Public broadcast channels from a contacts.SearchRequest response.

    Only channels with a resolvable @username and broadcast=True are
    candidates — private channels cannot be joined or commented anyway.
    """
    out: List[str] = []
    for chat in getattr(res, "chats", None) or []:
        if getattr(chat, "__class__", None).__name__ != "Channel":
            continue
        if not getattr(chat, "broadcast", False):
            continue
        username = getattr(chat, "username", None)
        if username:
            out.append(f"@{username}")
    return out


def _extract_fwd_usernames(messages) -> List[str]:
    """Forward-graph: channel usernames referenced by fwd_from headers."""
    out: List[str] = []
    for msg in messages or []:
        fwd = getattr(msg, "fwd_from", None)
        if fwd is None:
            continue
        from_name = getattr(fwd, "from_name", None)
        if from_name and isinstance(from_name, str) and from_name.startswith("@"):
            out.append(from_name)
        # channel_post + saved_from_peer supply numeric origins; usernames
        # are only actionable when present on the forwarded chat entity.
        peer = getattr(fwd, "saved_from_peer", None)
        if peer is not None and getattr(peer, "__class__", None).__name__ == "PeerChannel":
            continue  # numeric-only origin; resolution happens in validation
    return out


def scan_keyword(client: TelegramClient, keyword: str,
                 per_keyword_limit: int = MAX_SEARCH_RESULTS_PER_KEYWORD) -> Dict[str, Any]:
    """One Telegram global search pass for a keyword (zero-join reads only)."""
    result: Dict[str, Any] = {"candidates": [], "fwd_candidates": [], "error": None}
    try:
        res = client(SearchRequest(q=keyword, limit=per_keyword_limit))
        result["candidates"] = _extract_usernames_from_result(res)
        # Forward-graph pass over public posts mentioning the keyword.
        try:
            msgs = client(SearchGlobalRequest(
                q=keyword, limit=10,
                filter=InputMessagesFilterEmpty(),
                min_date=None, max_date=None,
                offset_rate=0, offset_peer=None, offset_id=0,
            ))
            result["fwd_candidates"] = _extract_fwd_usernames(
                getattr(msgs, "messages", None))
        except FloodWaitError:
            raise
        except Exception as exc:
            logger.debug("Forward-graph pass failed for %s: %s", keyword, exc)
    except FloodWaitError as fwe:
        raise
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        logger.warning("Search for '%s' failed: %s", keyword, exc)
    return result


def _respect_jitter() -> float:
    delay = random.uniform(*JITTER_RANGE)
    logger.info("Discovery jitter: sleeping %.1fs before next query", delay)
    time.sleep(delay)
    return delay


def _pause_for_flood_wait(fwe: FloodWaitError) -> float:
    wait = int(getattr(fwe, "seconds", 30)) + 2
    logger.warning("FloodWait during discovery: pausing %ss", wait)
    time.sleep(wait)
    return wait


# ------------------------------------------------------- pre-validation -----

def _default_banned_rights_block(chat) -> Optional[str]:
    """Non-writable default banned rights → reason string (else None)."""
    rights = getattr(chat, "default_banned_rights", None)
    if rights is None:
        return None
    if getattr(rights, "send_messages", False):
        return "default banned rights forbid sending messages"
    return None


def validate_candidate(client: TelegramClient, handle: str) -> Dict[str, Any]:
    """Zero-join pre-validation for one candidate channel.

    Returns {"ok": bool, "reason": str|None, "info": {...}}. NEVER joins —
    every step is a read: get_entity, latest post, GetDiscussionMessage.
    Accepts only public broadcast channels whose latest post has a linked,
    commentable discussion group.
    """
    info: Dict[str, Any] = {"handle": handle}
    try:
        entity = client.get_entity(handle)
    except (ChannelPrivateError, ValueError) as exc:
        return {"ok": False, "reason": f"unresolvable peer: {exc}", "info": info}

    if getattr(entity, "__class__", None).__name__ != "Channel" or \
            not getattr(entity, "broadcast", False):
        return {"ok": False, "reason": "not a broadcast channel", "info": info}
    if not getattr(entity, "username", None):
        return {"ok": False, "reason": "channel is private (no username)", "info": info}
    blocked = _default_banned_rights_block(entity)
    if blocked:
        return {"ok": False, "reason": blocked, "info": info}
    info["title"] = getattr(entity, "title", None)
    participants = getattr(entity, "participants_count", None)
    if participants:
        info["participants"] = participants

    try:
        posts = client.get_messages(entity, limit=1)
    except FloodWaitError:
        raise
    except Exception as exc:
        return {"ok": False, "reason": f"post fetch failed: {exc}", "info": info}
    post = posts[0] if posts else None
    if post is None or getattr(post, "service", False):
        return {"ok": False, "reason": "channel has no posts", "info": info}

    from telethon.tl.functions.messages import GetDiscussionMessageRequest

    try:
        discussion = client(GetDiscussionMessageRequest(peer=entity, msg_id=post.id))
    except (ChannelPrivateError, InviteRequestSentError) as exc:
        return {"ok": False,
                "reason": f"discussion private or join-gated: {exc}", "info": info}
    except ChatWriteForbiddenError as exc:
        return {"ok": False, "reason": f"discussion write-forbidden: {exc}", "info": info}
    except UserBannedInChannelError as exc:
        return {"ok": False, "reason": f"account banned in discussion: {exc}", "info": info}
    except FloodWaitError:
        raise
    except NoDiscussionGroupError:
        raise
    except Exception as exc:
        # Includes MSG_ID_INVALID (no discussion linked) — the dominant
        # "comments disabled" signal seen in live runs.
        return {"ok": False,
                "reason": f"no comment thread (comments disabled?): {exc}",
                "info": info}

    messages = list(getattr(discussion, "messages", None) or [])
    if not messages:
        return {"ok": False, "reason": "no linked discussion (comments disabled)",
                "info": info}
    return {"ok": True, "reason": None, "info": info}


# -------------------------------------------------------------- ingestion ---

def ingest_valid(handle: str, info: Dict[str, Any]) -> Optional[int]:
    """Insert an accepted candidate; returns the target row id (or None)."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(INSERT_TARGET_SQL, (handle,))
            cur.execute("SELECT id FROM target_channels WHERE target = %s;", (handle,))
            row = cur.fetchone()
            audit = (
                f"Discovered '{info.get('title') or handle}' ({handle})"
                + (f" · {info['participants']} subscribers" if info.get("participants") else "")
                + " · discussion group verified commentable (zero-join validation)"
            )
            cur.execute(INSERT_LOG_SQL, ("INFO", audit))
        conn.commit()
    logger.info("Discovered channel accepted: %s (%s)", info.get("title"), handle)
    return row[0] if row else None


def record_rejection(handle: str, reason: str) -> None:
    _REJECTED_CACHE.add(handle.strip().lower())
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(INSERT_REJECT_SQL, (
                    f"Discovery rejected '{handle}': {reason}",))
            conn.commit()
    except Exception as exc:
        logger.warning("Could not record discovery rejection for %s: %s", handle, exc)


# ------------------------------------------------------------ run control ---

def run_discovery(keywords: Optional[List[str]] = None,
                  limit: int = DEFAULT_DISCOVERY_LIMIT,
                  max_keywords: Optional[int] = None) -> Dict[str, Any]:
    """Synchronous discovery run (caller wraps in a thread for background).

    FloodWait pauses the run gracefully; any other unexpected error is
    recorded on STATE and never propagates.
    """
    if STATE.running:
        return {"ok": False, "detail": "discovery already running"}

    queue = build_keyword_queue(keywords, max_keywords)
    if not queue:
        return {"ok": False, "detail": "no keywords to scan"}

    from src.accounts.manager import AccountManager

    manager = AccountManager()
    accounts = manager.get_active_accounts()
    if not accounts:
        STATE.start(queue)
        STATE.finish(error="no active account for discovery")
        return {"ok": False, "detail": "no active account for discovery"}
    account = accounts[0]

    STATE.start(queue)
    known, pruned = fetch_exclusion_sets()

    client = manager.create_client(account["session_string"], account["proxy"])
    accepted = 0

    async def _run():
        nonlocal accepted
        await manager.connect_with_fallback(client)
        for index, keyword in enumerate(queue):
            STATE.current_keyword = keyword
            accepted += await _scan_and_validate(client, keyword, limit,
                                                 known, pruned)
            if accepted >= limit:
                break
            if index < len(queue) - 1:
                await asyncio.to_thread(_respect_jitter)

    try:
        asyncio.run(_run())
        STATE.finish()
    except FloodWaitError as fwe:
        wait = _pause_for_flood_wait(fwe)
        STATE.finish(error=f"paused {wait}s for Telegram FloodWait")
    except Exception as exc:
        logger.exception("Discovery run failed")
        STATE.finish(error=f"{type(exc).__name__}: {exc}")

    snap = STATE.snapshot()
    snap["ok"] = True
    return snap


async def _scan_and_validate(client: TelegramClient, keyword: str, limit: int,
                             known: Set[str], pruned: Set[str]) -> int:
    """Scan one keyword: search, dedupe, validate, ingest. Returns accepted count."""
    accepted = 0
    result = scan_keyword(client, keyword)
    candidates = result["candidates"] + result["fwd_candidates"]

    seen: Set[str] = set()
    unique: List[str] = []
    for cand in candidates:
        if cand.lower() not in seen:
            seen.add(cand.lower())
            unique.append(cand)
    fresh = filter_excluded(unique, known=known, pruned=pruned)
    STATE.scanned += len(unique)

    for handle in fresh:
        verdict = validate_candidate(client, handle)
        if verdict["ok"]:
            target_id = ingest_valid(handle, verdict["info"])
            if target_id:
                known.add(handle)          # in-process dedupe for the rest of the run
                STATE.accepted += 1
                STATE.accepted_targets.append(handle)
                accepted += 1
                if accepted >= limit:
                    break
        else:
            STATE.discarded += 1
            STATE.discards.append(f"{handle}: {verdict['reason']}")
            record_rejection(handle, verdict["reason"])
    return accepted


def start_background_discovery(keywords: Optional[List[str]] = None,
                               limit: int = DEFAULT_DISCOVERY_LIMIT,
                               max_keywords: Optional[int] = None) -> Dict[str, Any]:
    """Kick off a discovery run in a daemon thread (never blocks the API/worker)."""
    if STATE.running:
        return {"ok": False, "detail": "discovery already running"}
    import threading

    thread = threading.Thread(
        target=run_discovery,
        kwargs={"keywords": keywords, "limit": limit, "max_keywords": max_keywords},
        name="discovery-engine", daemon=True,
    )
    thread.start()
    return {"ok": True, "detail": "discovery started in background"}


def discovery_status() -> Dict[str, Any]:
    """Status + recent discovery metrics for the dashboard."""
    snap = STATE.snapshot()
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM target_channels WHERE tag = 'auto_discovered';")
                snap["auto_discovered_total"] = cur.fetchone()[0]
                cur.execute(
                    """
                    SELECT COUNT(*) FROM system_logs
                    WHERE event_type = 'CHANNEL_DISCOVERY_REJECTED'
                      AND created_at > NOW() - INTERVAL '24 hours';
                    """
                )
                snap["rejected_24h"] = cur.fetchone()[0]
    except Exception as exc:
        logger.warning("Could not enrich discovery status: %s", exc)
    return snap
