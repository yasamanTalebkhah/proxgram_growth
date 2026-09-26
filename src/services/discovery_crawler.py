"""Target Auto-Discovery Crawler — network-graph miner (pipeline Phase 2).

Complements the keyword-search engine (src/services/discovery.py): instead
of searching Telegram globally, it walks the channel graph outward from
seeds. For every active seed channel in `target_channels`:

  1. GetChannelRecommendationsRequest — Telegram's own algorithmic peer
     recommendations for that seed (method SIMILAR_CHANNELS).
  2. The seed's last MESSAGE_WINDOW messages — forward origins
     (fwd_from.from_id headers) and t.me/ usernames embedded in message
     text and reply-markup buttons (method FORWARD_CHAIN), inspected in
     the same sweep as the recommendations pass.

Candidates are normalized (lowercase, strip '@' and t.me URLs), deduped
against BOTH `discovered_targets` and `target_channels`, and upserted into
`discovered_targets` with status PENDING_VALIDATION. Validation happens
separately in src/services/discussion_validator.py.

Strict anti-flood: randomized 3-7s jitter between seed sweeps; FloodWait
pauses the run gracefully — a crawl can never crash the worker loop.
MANUAL_SEED is the discovery_method reserved for operator-inserted rows.
"""

import asyncio
import logging
import random
import re
import threading
import time
from typing import Any, Dict, List, Optional, Set

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.functions.channels import GetChannelRecommendationsRequest
from telethon.tl.functions.messages import GetHistoryRequest

from src.database.connection import get_db_connection

logger = logging.getLogger("discovery.crawler")

# ------------------------------------------------------------------ config --

CRAWLER_JITTER_RANGE = (3, 7)     # seconds between seed sweeps (spec: 3-7)
MESSAGE_WINDOW = 30               # last N seed messages mined per sweep
MAX_SEEDS_PER_RUN = 8             # soft cap so one run stays gentle
MAX_CANDIDATES_PER_SEED = 40      # hard cap per seed sweep

INSERT_DISCOVERED_SQL = """
    INSERT INTO discovered_targets (username_or_link, source_seed, discovery_method)
    VALUES (%s, %s, %s)
    ON CONFLICT (username_or_link) DO NOTHING;
"""

SEEDS_SQL = """
    SELECT target FROM target_channels WHERE enabled = TRUE ORDER BY id ASC LIMIT %s;
"""

# t.me links in text/buttons; invite-style links (+hash, joinchat) never match
# the [A-Za-z0-9_] charset or are dropped by the reserved-name guard below.
T_ME_RE = re.compile(r"(?:t\.me|telegram\.me)/([A-Za-z0-9_]{3,64})", re.IGNORECASE)
HANDLE_RE = re.compile(r"^[a-z0-9_]{3,64}$")
_RESERVED_PATHS = {"joinchat", "addlist", "share", "proxy", "socks", "addstickers"}


# ------------------------------------------------------------ normalization --

def normalize_username(raw: Optional[str]) -> Optional[str]:
    """Normalize any channel reference to '@handle' (or None).

    Accepts @handles, bare handles and t.me/telegram.me URLs (with query
    strings). Lowercases, strips the prefix, drops invite links and junk.
    """
    if not raw:
        return None
    low = str(raw).strip().lower()
    match = T_ME_RE.search(low)
    if match:
        low = match.group(1)
    low = low.lstrip("@").strip()
    if not HANDLE_RE.match(low) or low in _RESERVED_PATHS:
        return None
    return "@" + low


def _extract_forward_candidates(msg) -> List[str]:
    """Usernames exposed by a message's forward header (fwd_from).

    Numeric-only origins (fwd_from.from_id = PeerChannel/PeerUser) carry no
    username and cannot be promoted by the username-keyed validator, so
    they are inspected and skipped here.
    """
    out: List[str] = []
    fwd = getattr(msg, "fwd_from", None)
    if fwd is None:
        return out
    from_id = getattr(fwd, "from_id", None)
    if from_id is not None and getattr(from_id, "__class__", None).__name__ == "PeerChannel":
        return out  # numeric origin without a resolvable username
    from_name = getattr(fwd, "from_name", None)
    if isinstance(from_name, str) and from_name.startswith("@"):
        norm = normalize_username(from_name)
        if norm:
            out.append(norm)
    return out


def _extract_link_candidates(msg) -> List[str]:
    """t.me usernames embedded in message text and reply-markup buttons."""
    out: List[str] = []
    texts = [getattr(msg, "message", None) or ""]
    markup = getattr(msg, "reply_markup", None)
    for row in getattr(markup, "rows", None) or []:
        for button in getattr(row, "buttons", None) or []:
            texts.append(getattr(button, "url", None) or "")
            texts.append(getattr(button, "text", None) or "")
    for text in texts:
        for handle in T_ME_RE.findall(text or ""):
            norm = normalize_username(handle)
            if norm:
                out.append(norm)
    return out


def _recommendation_candidates(res) -> List[str]:
    """Public channel usernames from a GetChannelRecommendationsRequest."""
    out: List[str] = []
    for chat in getattr(res, "chats", None) or []:
        if getattr(chat, "__class__", None).__name__ != "Channel":
            continue
        username = getattr(chat, "username", None)
        if not username:
            continue  # private recommendation — nothing to promote
        norm = normalize_username("@" + username)
        if norm:
            out.append(norm)
    return out


# ------------------------------------------------------------------- state ---

class CrawlerState:
    """Thread-safe in-process metrics for one crawler run (dashboard)."""

    def __init__(self) -> None:
        self.running = False
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.current_seed: Optional[str] = None
        self.seeds: List[str] = []
        self.added = 0
        self.skipped = 0
        self.last_sweeps: List[Dict[str, Any]] = []
        self.error: Optional[str] = None

    def start(self, seeds: List[str]) -> None:
        self.running = True
        self.started_at = time.time()
        self.finished_at = None
        self.current_seed = None
        self.seeds = list(seeds)
        self.added = 0
        self.skipped = 0
        self.last_sweeps = []
        self.error = None

    def finish(self, error: Optional[str] = None) -> None:
        self.running = False
        self.finished_at = time.time()
        if error:
            self.error = error

    def snapshot(self) -> Dict[str, Any]:
        return {
            "running": self.running,
            "current_seed": self.current_seed,
            "seeds": list(self.seeds),
            "added": self.added,
            "skipped": self.skipped,
            "last_sweeps": list(self.last_sweeps[-10:]),
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "last_run_seconds": (
                round((self.finished_at or time.time()) - self.started_at, 1)
                if self.started_at else None
            ),
        }


CRAWLER_STATE = CrawlerState()


# ------------------------------------------------------------------ db -------

def active_seeds(limit: int = MAX_SEEDS_PER_RUN) -> List[str]:
    """Enabled seed channels (oldest first, capped) from target_channels."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(SEEDS_SQL, (limit,))
                return [row[0] for row in cur.fetchall()]
    except Exception as exc:
        logger.warning("Could not read seed channels: %s", exc)
        return []


def _excluded() -> Set[str]:
    """Lowercased union of target_channels + discovered_targets (dedupe key)."""
    known: Set[str] = set()
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT target FROM target_channels;")
                known |= {(row[0] or "").strip().lower() for row in cur.fetchall()}
                cur.execute("SELECT username_or_link FROM discovered_targets;")
                known |= {(row[0] or "").strip().lower() for row in cur.fetchall()}
    except Exception as exc:
        logger.warning("Could not read exclusion sets for crawl: %s", exc)
    return known


def upsert_pending(candidate: str, source_seed: Optional[str],
                   discovery_method: str) -> bool:
    """Insert a PENDING_VALIDATION pool row; False when it already existed."""
    inserted = False
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(INSERT_DISCOVERED_SQL, (candidate, source_seed, discovery_method))
            inserted = cur.rowcount > 0
        conn.commit()
    return inserted


def pool_stats() -> Dict[str, int]:
    """discovered_targets lifecycle counts (dashboard /stats endpoint)."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*),
                           COUNT(*) FILTER (WHERE status = 'PENDING_VALIDATION'),
                           COUNT(*) FILTER (WHERE status = 'VALIDATED_HAS_DISCUSSION'),
                           COUNT(*) FILTER (WHERE status = 'DISQUALIFIED_NO_DISCUSSION'),
                           COUNT(*) FILTER (WHERE status = 'FAILED')
                    FROM discovered_targets;
                    """
                )
                total, pending, validated, disqualified, failed = cur.fetchone()
                return {
                    "total": total,
                    "pending": pending,
                    "validated": validated,
                    "disqualified": disqualified,
                    "failed": failed,
                }
    except Exception as exc:
        logger.warning("Could not read discovered_targets stats: %s", exc)
        return {"total": 0, "pending": 0, "validated": 0, "disqualified": 0, "failed": 0}


# ---------------------------------------------------------------- sweeping ---

async def sweep_seed(client: TelegramClient, seed: str,
                     known: Set[str]) -> Dict[str, Any]:
    """One seed sweep: recommendations + last-30-message mining, deduped.

    Returns {"seed", "recommendations", "mined", "added", "skipped"}.
    FloodWaitError propagates to the run-level pause handler.
    """
    stats: Dict[str, Any] = {"seed": seed, "recommendations": 0, "mined": 0,
                             "added": 0, "skipped": 0}
    entity = await client.get_entity(seed)

    similar: List[str] = []
    forwarded: List[str] = []
    try:
        res = await client(GetChannelRecommendationsRequest(channel=entity))
        similar = _recommendation_candidates(res)[:MAX_CANDIDATES_PER_SEED]
        stats["recommendations"] = len(similar)
    except FloodWaitError:
        raise
    except Exception as exc:
        logger.warning("Recommendations failed for %s: %s", seed, exc)
    try:
        history = await client(GetHistoryRequest(
            peer=entity, offset_id=0, offset_date=None, add_offset=0,
            limit=MESSAGE_WINDOW, max_id=0, min_id=0, hash=0,
        ))
        for msg in getattr(history, "messages", None) or []:
            forwarded.extend(_extract_forward_candidates(msg))
            forwarded.extend(_extract_link_candidates(msg))
        forwarded = forwarded[:MAX_CANDIDATES_PER_SEED]
        stats["mined"] = len(forwarded)
    except FloodWaitError:
        raise
    except Exception as exc:
        logger.warning("History mining failed for %s: %s", seed, exc)

    # Upsert recommendations (SIMILAR_CHANNELS) and mined links/forwards
    # (FORWARD_CHAIN); dedupe across sources, the pool and target_channels.
    seen: Set[str] = set()
    for candidate, method in ([(c, "SIMILAR_CHANNELS") for c in similar]
                              + [(c, "FORWARD_CHAIN") for c in forwarded]):
        if candidate in seen or candidate.lower() in known:
            stats["skipped"] += 1
            continue
        seen.add(candidate)
        try:
            if upsert_pending(candidate, seed, method):
                known.add(candidate.lower())
                stats["added"] += 1
            else:
                stats["skipped"] += 1
        except Exception as exc:
            logger.warning("Could not upsert candidate %s: %s", candidate, exc)
            stats["skipped"] += 1
    return stats


def _respect_jitter() -> float:
    delay = random.uniform(*CRAWLER_JITTER_RANGE)
    logger.info("Crawler jitter: sleeping %.1fs before next seed sweep", delay)
    time.sleep(delay)
    return delay


def _pause_for_flood_wait(fwe: FloodWaitError) -> float:
    wait = int(getattr(fwe, "seconds", 30)) + 2
    logger.warning("FloodWait during crawl: pausing %ss", wait)
    time.sleep(wait)
    return wait


# ------------------------------------------------------------- run control ---

def run_crawl(seeds: Optional[List[str]] = None,
              max_seeds: int = MAX_SEEDS_PER_RUN) -> Dict[str, Any]:
    """Synchronous crawl run (caller wraps in a thread for background)."""
    if CRAWLER_STATE.running:
        return {"ok": False, "detail": "crawler already running"}

    from src.accounts.manager import AccountManager

    manager = AccountManager()
    accounts = manager.get_active_accounts()
    if not accounts:
        CRAWLER_STATE.start([])
        CRAWLER_STATE.finish(error="no active account for crawler")
        return {"ok": False, "detail": "no active account for crawler"}

    seed_list = [s for s in (seeds or []) if s] or active_seeds(max_seeds)
    if not seed_list:
        CRAWLER_STATE.start([])
        CRAWLER_STATE.finish(error="no active seed channels")
        return {"ok": False, "detail": "no active seed channels"}
    seed_list = seed_list[:max_seeds]

    CRAWLER_STATE.start(seed_list)
    known = _excluded()
    client = manager.create_client(accounts[0]["session_string"],
                                   accounts[0]["proxy"])

    async def _run():
        await manager.connect_with_fallback(client)
        for index, seed in enumerate(seed_list):
            CRAWLER_STATE.current_seed = seed
            sweep = await sweep_seed(client, seed, known)
            CRAWLER_STATE.last_sweeps.append(sweep)
            CRAWLER_STATE.added += sweep["added"]
            CRAWLER_STATE.skipped += sweep["skipped"]
            logger.info("Seed %s: +%d pending, %d skipped", seed,
                        sweep["added"], sweep["skipped"])
            if index < len(seed_list) - 1:
                await asyncio.to_thread(_respect_jitter)

    try:
        asyncio.run(_run())
        CRAWLER_STATE.finish()
    except FloodWaitError as fwe:
        wait = _pause_for_flood_wait(fwe)
        CRAWLER_STATE.finish(error=f"paused {wait}s for Telegram FloodWait")
    except Exception as exc:
        logger.exception("Discovery crawl failed")
        CRAWLER_STATE.finish(error=f"{type(exc).__name__}: {exc}")

    snap = CRAWLER_STATE.snapshot()
    snap["ok"] = True
    return snap


def start_background_crawl(seeds: Optional[List[str]] = None,
                           max_seeds: int = MAX_SEEDS_PER_RUN) -> Dict[str, Any]:
    """Kick off a crawl in a daemon thread (never blocks the API/worker)."""
    if CRAWLER_STATE.running:
        return {"ok": False, "detail": "crawler already running"}
    thread = threading.Thread(
        target=run_crawl, kwargs={"seeds": seeds, "max_seeds": max_seeds},
        name="discovery-crawler", daemon=True,
    )
    thread.start()
    return {"ok": True, "detail": "crawler started in background"}


def crawler_status() -> Dict[str, Any]:
    """Crawler state + discovered_targets pool counts for the dashboard."""
    snap = CRAWLER_STATE.snapshot()
    snap["pool"] = pool_stats()
    return snap
