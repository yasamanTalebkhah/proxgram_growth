"""Target Studio service layer — pool inspection & manual operator actions.

Backs the dashboard "Auto-Discovery Radar & Pipeline Control" panel:
  - filtered/paginated listing of discovered_targets with the latest
    disqualification reason resolved from the audit trail;
  - summary metrics (pool counts, promotions, rejection rate);
  - per-target re-probe (reset to PENDING_VALIDATION for revalidation),
    force-promote (manual, bypasses checks), delete, and
    disqualified-pool purge.

Every mutation writes an audit row so operator actions stay traceable.
"""

import logging
import re
from typing import Any, Dict, List, Optional

from src.database.connection import get_db_connection

logger = logging.getLogger("discovery.studio")

# ------------------------------------------------------------------- SQL ----

LIST_TARGETS_SQL = """
    SELECT id, username_or_link, source_seed, discovery_method, status,
           linked_chat_id, last_checked_at, created_at
    FROM discovered_targets
    WHERE (%(status)s::VARCHAR IS NULL OR status = %(status)s)
      AND (%(search)s::VARCHAR IS NULL OR username_or_link ILIKE %(like)s)
    ORDER BY created_at DESC, id DESC
    LIMIT %(limit)s OFFSET %(offset)s;
"""

COUNT_TARGETS_SQL = """
    SELECT COUNT(*) FROM discovered_targets
    WHERE (%(status)s::VARCHAR IS NULL OR status = %(status)s)
      AND (%(search)s::VARCHAR IS NULL OR username_or_link ILIKE %(like)s);
"""

SUMMARY_SQL = """
    SELECT COUNT(*),
           COUNT(*) FILTER (WHERE status = 'PENDING_VALIDATION'),
           COUNT(*) FILTER (WHERE status = 'VALIDATED_HAS_DISCUSSION'),
           COUNT(*) FILTER (WHERE status = 'DISQUALIFIED_NO_DISCUSSION')
    FROM discovered_targets;
"""

PROMOTED_COUNT_SQL = """
    SELECT COUNT(*) FROM target_channels WHERE tag = 'auto_discovery';
"""

LATEST_REJECTIONS_SQL = """
    SELECT message FROM system_logs
    WHERE event_type = 'CHANNEL_DISCOVERY_REJECTED'
    ORDER BY id DESC LIMIT 400;
"""

GET_TARGET_SQL = """
    SELECT id, username_or_link, status FROM discovered_targets WHERE id = %s;
"""

REPROBE_SQL = """
    UPDATE discovered_targets
    SET status = 'PENDING_VALIDATION', last_checked_at = NOW()
    WHERE id = %s;
"""

FORCE_PROMOTE_SQL = """
    INSERT INTO target_channels (target, tag, enabled)
    VALUES (%s, %s, TRUE)
    ON CONFLICT (target) DO NOTHING;
"""

DELETE_TARGET_SQL = "DELETE FROM discovered_targets WHERE id = %s;"

PURGE_DISQUALIFIED_SQL = """
    DELETE FROM discovered_targets
    WHERE status = 'DISQUALIFIED_NO_DISCUSSION'
"""

PURGE_DISQUALIFIED_DAYS_SQL = PURGE_DISQUALIFIED_SQL + \
    " AND last_checked_at < NOW() - (%s || ' days')::interval"

INSERT_LOG_SQL = """
    INSERT INTO system_logs (level, event_type, message, created_at)
    VALUES (%s, %s, %s, NOW());
"""

REASON_RE = re.compile(r"disqualified '([^']+)':\s*(.+)$")


def _audit(event_type: str, message: str) -> None:
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(INSERT_LOG_SQL, ("INFO", event_type, message))
            conn.commit()
    except Exception as exc:
        logger.warning("Could not write studio audit row: %s", exc)


def _latest_rejection_reasons() -> Dict[str, str]:
    """Handle -> most recent disqualification reason from the audit trail."""
    reasons: Dict[str, str] = {}
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(LATEST_REJECTIONS_SQL)
                rows = cur.fetchall()
    except Exception as exc:
        logger.warning("Could not read rejection audit trail: %s", exc)
        return reasons
    for (message,) in rows:
        match = REASON_RE.search(message or "")
        if match:
            handle, reason = match.group(1), match.group(2).strip()
            reasons.setdefault(handle, reason)
    return reasons


def list_targets(status: Optional[str] = None, search: Optional[str] = None,
                 limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    """Filtered + paginated discovered_targets rows (newest first)."""
    params = {
        "status": (status or None),
        "search": (search or None),
        "like": f"%{(search or '').strip()}%",
        "limit": max(1, min(int(limit), 200)),
        "offset": max(0, int(offset)),
    }
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(LIST_TARGETS_SQL, params)
                rows = cur.fetchall()
                cur.execute(COUNT_TARGETS_SQL, params)
                total = cur.fetchone()[0]
    except Exception as exc:
        logger.warning("Could not list discovered targets: %s", exc)
        return {"items": [], "total_count": 0}

    reasons = _latest_rejection_reasons()
    items = [{
        "id": r[0],
        "username_or_link": r[1],
        "source_seed": r[2],
        "discovery_method": r[3],
        "status": r[4],
        "linked_chat_id": r[5],
        "disqualification_reason": reasons.get(r[1]),
        "last_checked_at": r[6].isoformat() if r[6] else None,
        "created_at": r[7].isoformat() if r[7] else None,
    } for r in rows]
    return {"items": items, "total_count": total}


def studio_stats() -> Dict[str, Any]:
    """Flat summary metrics for the studio KPI cards."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(SUMMARY_SQL)
                total, pending, validated, disqualified = cur.fetchone()
                cur.execute(PROMOTED_COUNT_SQL)
                promoted = cur.fetchone()[0]
    except Exception as exc:
        logger.warning("Could not compute studio stats: %s", exc)
        total = pending = validated = disqualified = promoted = 0
    decided = validated + disqualified
    return {
        "total_discovered": total,
        "pending_count": pending,
        "validated_count": validated,
        "disqualified_count": disqualified,
        "total_promoted": promoted,
        "rejection_rate_percentage": round(disqualified / decided * 100, 1) if decided else 0.0,
    }


def get_target_record(target_id: int) -> Optional[Dict[str, Any]]:
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(GET_TARGET_SQL, (target_id,))
                row = cur.fetchone()
    except Exception as exc:
        logger.warning("Could not read discovered target %s: %s", target_id, exc)
        return None
    if not row:
        return None
    return {"id": row[0], "username": row[1], "status": row[2]}


def reprobe_target(target_id: int) -> bool:
    """Reset a target to PENDING_VALIDATION so the validator re-checks it."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(REPROBE_SQL, (target_id,))
            changed = cur.rowcount > 0
        conn.commit()
    if changed:
        _audit("TARGET_REPROBE_QUEUED",
               f"discovered_targets #{target_id} reset to PENDING_VALIDATION for re-probe")
    return changed


def force_promote_target(target_id: int,
                         tag: str = "manual_promoted") -> Optional[bool]:
    """Insert the target into target_channels bypassing validation checks.

    Returns True when newly inserted, False when it was already configured,
    and None when the pool row does not exist.
    """
    record = get_target_record(target_id)
    if record is None:
        return None
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(FORCE_PROMOTE_SQL, (record["username"], tag))
            inserted = cur.rowcount > 0
        conn.commit()
    _audit("CHANNEL_DISCOVERED",
           f"Force-promoted '{record['username']}' from discovered_targets "
           f"#{target_id} (tag={tag}, checks bypassed by operator)")
    return inserted


def delete_target_record(target_id: int) -> bool:
    """Remove a discovered_targets row entirely."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(DELETE_TARGET_SQL, (target_id,))
            deleted = cur.rowcount > 0
        conn.commit()
    if deleted:
        _audit("TARGETS_PURGED",
               f"discovered_targets #{target_id} deleted by operator")
    return deleted


def purge_disqualified(days: Optional[int] = None) -> int:
    """Delete disqualified records (optionally only older than `days`)."""
    deleted = 0
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            if days:
                cur.execute(PURGE_DISQUALIFIED_DAYS_SQL, (int(days),))
            else:
                cur.execute(PURGE_DISQUALIFIED_SQL)
            deleted = cur.rowcount
        conn.commit()
    if deleted:
        _audit("TARGETS_PURGED",
               f"Purged {deleted} DISQUALIFIED_NO_DISCUSSION record(s)"
               + (f" older than {days} days" if days else ""))
    return deleted
