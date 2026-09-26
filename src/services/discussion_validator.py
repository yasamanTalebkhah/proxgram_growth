"""Discussion Validator & Production Promoter (pipeline Phase 3).

Drains the PENDING_VALIDATION pool in `discovered_targets`:
  - resolves each candidate's peer and calls GetFullChannelRequest;
  - full_chat.linked_chat_id present  -> VALIDATED_HAS_DISCUSSION,
    stores linked_chat_id and atomically upserts the channel into
    `target_channels` (tag='auto_discovery', enabled=TRUE) so the seeder
    can start commenting into its discussion group;
  - linked_chat_id absent             -> DISQUALIFIED_NO_DISCUSSION
    (never inserted into target_channels);
  - terminal Telegram errors (ChannelPrivateError, UsernameNotOccupiedError,
    UsernameInvalidError)             -> DISQUALIFIED_NO_DISCUSSION;
  - FloodWaitError                    -> the whole batch pauses gracefully
    for the requested duration; no target is dropped and the run can never
    crash the worker loop.

Read-only toward Telegram: never joins anything.
"""

import asyncio
import logging
import random
import threading
import time
from typing import Any, Dict, List, Optional

from telethon import TelegramClient
from telethon.errors import (
    ChannelPrivateError,
    ChatWriteForbiddenError,
    FloodWaitError,
    InviteRequestSentError,
    MsgIdInvalidError,
    UserBannedInChannelError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetDiscussionMessageRequest

from src.database.connection import get_db_connection

logger = logging.getLogger("discovery.validator")

# ------------------------------------------------------------------ config --

BATCH_SIZE = 25                    # pending targets per validation run
VALIDATOR_JITTER_RANGE = (3, 7)    # seconds between per-target checks

FETCH_PENDING_SQL = """
    SELECT id, username_or_link, attempt_count
    FROM discovered_targets
    WHERE status = 'PENDING_VALIDATION'
    ORDER BY attempt_count ASC, id ASC
    LIMIT %s;
"""

MARK_VALIDATED_SQL = """
    UPDATE discovered_targets
    SET status = 'VALIDATED_HAS_DISCUSSION',
        linked_chat_id = %s,
        last_checked_at = NOW()
    WHERE id = %s;
"""

MARK_DISQUALIFIED_SQL = """
    UPDATE discovered_targets
    SET status = 'DISQUALIFIED_NO_DISCUSSION',
        linked_chat_id = NULL,
        last_checked_at = NOW()
    WHERE id = %s;
"""

TOUCH_ATTEMPT_SQL = """
    UPDATE discovered_targets
    SET attempt_count = attempt_count + 1, last_checked_at = NOW()
    WHERE id = %s;
"""

PROMOTE_TARGET_SQL = """
    INSERT INTO target_channels (target, tag, enabled)
    VALUES (%s, 'auto_discovery', TRUE)
    ON CONFLICT (target) DO NOTHING;
"""

INSERT_LOG_SQL = """
    INSERT INTO system_logs (level, event_type, message, created_at)
    VALUES (%s, %s, %s, NOW());
"""

TERMINAL_ERRORS = (ChannelPrivateError, UsernameNotOccupiedError, UsernameInvalidError)


# ------------------------------------------------------------------- state ---

class ValidatorState:
    """Thread-safe in-process metrics for one validation run (dashboard)."""

    def __init__(self) -> None:
        self.running = False
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.current_target: Optional[str] = None
        self.validated = 0
        self.disqualified = 0
        self.promoted: List[str] = []
        self.error: Optional[str] = None

    def start(self) -> None:
        self.running = True
        self.started_at = time.time()
        self.finished_at = None
        self.current_target = None
        self.validated = 0
        self.disqualified = 0
        self.promoted = []
        self.error = None

    def finish(self, error: Optional[str] = None) -> None:
        self.running = False
        self.finished_at = time.time()
        if error:
            self.error = error

    def snapshot(self) -> Dict[str, Any]:
        return {
            "running": self.running,
            "current_target": self.current_target,
            "validated": self.validated,
            "disqualified": self.disqualified,
            "promoted": list(self.promoted[-20:]),
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "last_run_seconds": (
                round((self.finished_at or time.time()) - self.started_at, 1)
                if self.started_at else None
            ),
        }


VALIDATOR_STATE = ValidatorState()


# -------------------------------------------------------------- pool access --

def fetch_pending(limit: int = BATCH_SIZE) -> List[Dict[str, Any]]:
    """Next PENDING_VALIDATION batch (oldest, fewest attempts first)."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(FETCH_PENDING_SQL, (limit,))
                rows = cur.fetchall()
    except Exception as exc:
        logger.warning("Could not fetch pending pool: %s", exc)
        return []
    return [{"id": r[0], "username": r[1], "attempts": r[2]} for r in rows]


def mark_validated(target_id: int, linked_chat_id: int) -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(MARK_VALIDATED_SQL, (linked_chat_id, target_id))
        conn.commit()


def mark_disqualified(target_id: int) -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(MARK_DISQUALIFIED_SQL, (target_id,))
        conn.commit()


def record_attempt(target_id: int) -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(TOUCH_ATTEMPT_SQL, (target_id,))
        conn.commit()


def audit(level: str, event_type: str, message: str) -> None:
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(INSERT_LOG_SQL, (level, event_type, message))
            conn.commit()
    except Exception as exc:
        logger.warning("Could not write validator audit row: %s", exc)


def promote_target(handle: str) -> bool:
    """Atomic upsert into target_channels (tag='auto_discovery', enabled).

    Returns True when a new row was inserted (rowcount), False when the
    target was already configured.
    """
    inserted = False
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(PROMOTE_TARGET_SQL, (handle,))
            inserted = cur.rowcount > 0
        conn.commit()
    return inserted


# -------------------------------------------------------------- validation ---

async def validate_target(client: TelegramClient, handle: str) -> Dict[str, Any]:
    """Two-step zero-join validation for one candidate.

    Step 1 — linked_chat_id: resolve the peer and call
    GetFullChannelRequest; a missing linked_chat_id disqualifies.
    Step 2 — discussion probe: fetch the latest post and resolve its
    comment thread via GetDiscussionMessageRequest. Live runs showed a
    linked group alone is not sufficient (~28% false positives):
    join-approval-gated groups (InviteRequestSentError) and posts with no
    open thread (MsgIdInvalidError) must be rejected BEFORE promotion.

    Every path here is a read — never joins anything. Returns
    {"ok", "reason", "linked_chat_id"}; FloodWaitError propagates to the
    run-level pause handler; terminal Telegram errors are mapped to a
    disqualification verdict here.
    """
    try:
        peer = await client.get_entity(handle)
    except TERMINAL_ERRORS as exc:
        return {"ok": False, "reason": f"terminal peer error: {type(exc).__name__}",
                "linked_chat_id": None}
    except ValueError as exc:  # Telethon raises ValueError for dead handles
        return {"ok": False, "reason": f"unresolvable peer: {exc}",
                "linked_chat_id": None}

    try:
        full = await client(GetFullChannelRequest(channel=peer))
    except TERMINAL_ERRORS:
        raise  # terminal mapping happens at the caller level
    except FloodWaitError:
        raise

    full_chat = getattr(full, "full_chat", None) or full
    linked = getattr(full_chat, "linked_chat_id", None)
    if not linked:
        return {"ok": False, "reason": "no discussion group linked",
                "linked_chat_id": None}

    # ---- Step 2: zero-join discussion probe on the latest post ----
    try:
        posts = await client.get_messages(peer, limit=1)
    except FloodWaitError:
        raise
    except Exception as exc:
        return {"ok": False, "reason": f"post fetch failed: {exc}",
                "linked_chat_id": linked}
    post = posts[0] if posts else None
    if post is None or getattr(post, "service", False):
        return {"ok": False, "reason": "channel has no posts to probe",
                "linked_chat_id": linked}

    try:
        discussion = await client(GetDiscussionMessageRequest(
            peer=peer, msg_id=post.id))
    except MsgIdInvalidError:
        return {"ok": False,
                "reason": "latest post has no open comment thread (comments disabled?)",
                "linked_chat_id": linked}
    except (ChannelPrivateError, InviteRequestSentError) as exc:
        return {"ok": False,
                "reason": f"discussion private or join-approval gated: {type(exc).__name__}",
                "linked_chat_id": linked}
    except ChatWriteForbiddenError:
        return {"ok": False, "reason": "discussion write-forbidden",
                "linked_chat_id": linked}
    except UserBannedInChannelError:
        return {"ok": False, "reason": "account banned in discussion",
                "linked_chat_id": linked}
    except FloodWaitError:
        raise
    except Exception as exc:
        return {"ok": False,
                "reason": f"discussion thread unresolvable: {type(exc).__name__}: {exc}",
                "linked_chat_id": linked}

    if not list(getattr(discussion, "messages", None) or []):
        return {"ok": False, "reason": "no linked discussion resolved",
                "linked_chat_id": linked}

    return {"ok": True, "reason": None, "linked_chat_id": linked}


async def validate_batch(client: TelegramClient,
                         batch: Optional[List[Dict[str, Any]]] = None,
                         single: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
    """Validate one batch of pending targets. FloodWait pauses the batch.

    On FloodWaitError the remaining targets stay PENDING_VALIDATION —
    nothing is dropped; the next run picks them up. `single` validates one
    explicit pool row instead of pulling the pending queue (re-probes).
    """
    counts = {"checked": 0, "validated": 0, "disqualified": 0}
    if batch is None:
        batch = [single] if single else fetch_pending(BATCH_SIZE)

    for row in batch:
        target_id = row["id"]
        handle = row["username"]
        VALIDATOR_STATE.current_target = handle
        record_attempt(target_id)
        try:
            verdict = await validate_target(client, handle)
        except FloodWaitError:
            raise  # remaining targets untouched (still PENDING_VALIDATION)
        except TERMINAL_ERRORS:
            mark_disqualified(target_id)
            audit("INFO", "CHANNEL_DISCOVERY_REJECTED",
                  f"Discovery validation disqualified '{handle}': terminal Telegram error")
            counts["checked"] += 1
            counts["disqualified"] += 1
            VALIDATOR_STATE.disqualified += 1
            continue

        if verdict["ok"]:
            mark_validated(target_id, verdict["linked_chat_id"])
            if promote_target(handle):
                audit("INFO", "CHANNEL_DISCOVERED",
                      f"Auto-discovery validated '{handle}' · discussion linked_chat_id="
                      f"{verdict['linked_chat_id']} · promoted to target_channels")
            else:
                audit("INFO", "CHANNEL_DISCOVERED",
                      f"Auto-discovery validated '{handle}' (already in target_channels)")
            counts["checked"] += 1
            counts["validated"] += 1
            VALIDATOR_STATE.validated += 1
            VALIDATOR_STATE.promoted.append(handle)
        else:
            mark_disqualified(target_id)
            audit("INFO", "CHANNEL_DISCOVERY_REJECTED",
                  f"Discovery validation disqualified '{handle}': {verdict['reason']}")
            counts["checked"] += 1
            counts["disqualified"] += 1
            VALIDATOR_STATE.disqualified += 1

        await _jitter_sleep()
    return counts


async def _jitter_sleep() -> None:
    """Async anti-flood pause between per-target checks (never blocks the loop)."""
    await asyncio.sleep(random.uniform(*VALIDATOR_JITTER_RANGE))


def _pause_for_flood_wait(fwe: FloodWaitError) -> float:
    wait = int(getattr(fwe, "seconds", 30)) + 2
    logger.warning("FloodWait during validation: pausing %ss", wait)
    time.sleep(wait)
    return wait


# ------------------------------------------------------------- run control ---

def run_validation(limit: int = BATCH_SIZE,
                   single_id: Optional[int] = None) -> Dict[str, Any]:
    """Synchronous validation run (caller wraps in a thread for background).

    With `single_id`, validates exactly that discovered_targets row
    (operator re-probe); it must already be PENDING_VALIDATION.
    """
    if VALIDATOR_STATE.running:
        return {"ok": False, "detail": "validator already running"}

    from src.accounts.manager import AccountManager

    manager = AccountManager()
    accounts = manager.get_active_accounts()
    if not accounts:
        VALIDATOR_STATE.start()
        VALIDATOR_STATE.finish(error="no active account for validator")
        return {"ok": False, "detail": "no active account for validator"}

    zero_counts = {"checked": 0, "validated": 0, "disqualified": 0}
    if single_id:
        from src.services.discovery_studio import get_target_record

        record = get_target_record(single_id)
        if record is None:
            return {"ok": False, "detail": "target not found", "counts": zero_counts}
        if record["status"] != "PENDING_VALIDATION":
            return {"ok": False,
                    "detail": f"target status is {record['status']}, not PENDING_VALIDATION",
                    "counts": zero_counts}
        batch = [{"id": record["id"], "username": record["username"], "attempts": 0}]
    else:
        batch = fetch_pending(limit)
    if not batch:
        VALIDATOR_STATE.start()
        VALIDATOR_STATE.finish()
        return {"ok": True, "detail": "no pending targets to validate", "counts": zero_counts}

    VALIDATOR_STATE.start()
    client = manager.create_client(accounts[0]["session_string"],
                                   accounts[0]["proxy"])

    async def _run():
        await manager.connect_with_fallback(client)
        return await validate_batch(client, batch)

    try:
        counts = asyncio.run(_run())
        VALIDATOR_STATE.finish()
    except FloodWaitError as fwe:
        wait = _pause_for_flood_wait(fwe)
        VALIDATOR_STATE.finish(error=f"paused {wait}s for Telegram FloodWait")
        counts = dict(zero_counts)
    except Exception as exc:
        logger.exception("Validation run failed")
        VALIDATOR_STATE.finish(error=f"{type(exc).__name__}: {exc}")
        counts = dict(zero_counts)

    snap = VALIDATOR_STATE.snapshot()
    snap["ok"] = True
    snap["counts"] = counts
    return snap


def start_background_validation(limit: Optional[int] = None,
                                single_id: Optional[int] = None) -> Dict[str, Any]:
    """Kick off validation in a daemon thread (never blocks the API/worker)."""
    if VALIDATOR_STATE.running:
        return {"ok": False, "detail": "validator already running"}
    kwargs: Dict[str, Any] = {}
    if limit:
        kwargs["limit"] = limit
    if single_id:
        kwargs["single_id"] = single_id
    thread = threading.Thread(
        target=run_validation, kwargs=kwargs,
        name="discussion-validator", daemon=True,
    )
    thread.start()
    return {"ok": True, "detail": "validator started in background"}


def validator_status() -> Dict[str, Any]:
    """Validator state + pool counts for the dashboard."""
    from src.services.discovery_crawler import pool_stats

    snap = VALIDATOR_STATE.snapshot()
    snap["pool"] = pool_stats()
    return snap
