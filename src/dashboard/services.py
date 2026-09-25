"""DB-backed view/query services for the dashboard (read paths + mutations)."""

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

sys.path.insert(0, __import__("os").path.abspath(__import__("os").path.join(__import__("os").path.dirname(__file__), "..", "..")))

from src.database.connection import get_db_connection

logger = logging.getLogger("dashboard.services")

_SETTINGS_TABLE_READY = True


def _now_iso(dt) -> Optional[str]:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None


# ---------------------------------------------------------------- overview --

def dashboard_kpis() -> Dict[str, Any]:
    """KPI cards for the executive overview."""
    m: Dict[str, Any] = {}
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status;")
            counts = dict(cur.fetchall())
            m["counts"] = counts
            completed = counts.get("COMPLETED", 0)
            failed = counts.get("FAILED", 0)
            finished = completed + failed
            m["success_rate"] = round(100 * completed / finished, 1) if finished else None

            cur.execute(
                """
                SELECT COUNT(*), COALESCE(AVG(EXTRACT(EPOCH FROM (executed_at - created_at))), 0)
                FROM tasks WHERE status = 'COMPLETED' AND executed_at IS NOT NULL;
                """
            )
            row = cur.fetchone()
            m["deliveries_measured"] = int(row[0])
            m["avg_delivery_seconds"] = round(float(row[1]), 1)

            cur.execute(
                "SELECT COUNT(*) FROM system_logs WHERE event_type = 'DELIVERY_VERIFIED';"
            )
            m["verified_delivered"] = cur.fetchone()[0]

            cur.execute(
                "SELECT COUNT(*) FROM system_logs WHERE event_type = 'TASK_REQUEUED';"
            )
            m["requeue_events"] = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM accounts WHERE status = 'ACTIVE';")
            m["active_accounts"] = cur.fetchone()[0]
    return m


# ---------------------------------------------------------------- accounts --

def accounts_view() -> List[Dict[str, Any]]:
    """Telegram accounts with health fields for the session cards."""
    accounts: List[Dict[str, Any]] = []
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, phone_number, COALESCE(username, '-'), status,
                       failure_count, proxy_config, created_at, updated_at
                FROM accounts ORDER BY id;
                """
            )
            for row in cur.fetchall():
                accounts.append({
                    "id": row[0],
                    "phone": row[1],
                    "username": row[2],
                    "status": row[3],
                    "failure_count": row[4],
                    "proxy": row[5] if isinstance(row[5], dict) else (json.loads(row[5]) if row[5] else None),
                    "created": _now_iso(row[6]),
                    "updated": _now_iso(row[7]),
                })
    return accounts


def run_account_healthcheck(account_id: int) -> Dict[str, Any]:
    """Verify the account session via SpamBot probe + transport fallback connect."""
    import asyncio

    from src.accounts.manager import AccountManager
    from src.core.settings import get_setting_int

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT phone_number, session_string, proxy_config FROM accounts WHERE id = %s;",
                (account_id,),
            )
            row = cur.fetchone()
    if not row:
        return {"ok": False, "error": "account not found"}

    manager = AccountManager()
    proxy = row[2] if isinstance(row[2], dict) else (json.loads(row[2]) if row[2] else None)
    client = manager.create_client(row[1], proxy)
    transport_used, connected, authorized = None, False, False
    try:
        async def _connect():
            await manager.connect_with_fallback(client)

        asyncio.run(_connect())
        connected = client.is_connected()
        transport_used = getattr(client._connection, "__name__", None)
        if connected:
            authorized = asyncio.run(client.is_user_authorized())
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "transport": transport_used, "connected": connected}
    finally:
        try:
            asyncio.run(client.disconnect())
        except Exception:
            pass
    return {"ok": connected and authorized, "connected": connected,
            "authorized": authorized, "transport": transport_used}


# ---------------------------------------------------------------- channels --

def list_targets() -> List[Dict[str, Any]]:
    targets: List[Dict[str, Any]] = []
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, target, COALESCE(tag, '-'), enabled, created_at,
                       (SELECT COUNT(*) FROM tasks t WHERE t.target = c.target) AS total_tasks
                FROM target_channels c ORDER BY id;
                """
            )
            for row in cur.fetchall():
                targets.append({
                    "id": row[0], "target": row[1], "tag": row[2],
                    "enabled": row[3], "created": _now_iso(row[4]),
                    "total_tasks": row[5],
                })
    return targets


def insert_target(target: str, tag: Optional[str]) -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO target_channels (target, tag) VALUES (%s, %s);",
                (target, tag),
            )
        conn.commit()


def set_target_enabled(target_id: int) -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE target_channels SET enabled = NOT enabled WHERE id = %s;",
                (target_id,),
            )
        conn.commit()


def delete_target(target_id: int) -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM target_channels WHERE id = %s;", (target_id,))
        conn.commit()


def purge_targets() -> int:
    """Delete ALL target_channels rows (dashboard "Clear All Targets").

    Returns the number of rows removed; writes a TARGETS_PURGED audit row.
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM target_channels;")
            count = cur.fetchone()[0]
            cur.execute("DELETE FROM target_channels;")
            cur.execute(
                "INSERT INTO system_logs (level, event_type, message, created_at) "
                "VALUES ('WARNING', 'TARGETS_PURGED', %s, NOW());",
                (f"All {count} target channel(s) removed via dashboard",),
            )
        conn.commit()
    logger.warning("Purged %d target channel(s) via dashboard", count)
    return count


def bulk_import_targets(raw_text: str, tag: Optional[str] = None) -> Dict[str, Any]:
    """Bulk-import targets from pasted multi-line text ("Batch Add Targets").

    Accepts one target per line — @usernames, https://t.me/... links or
    numeric -100… ids — using the same parser as the task seeder for
    consistent normalization. Deduplicates within the pasted batch AND
    against target_channels.target (UNIQUE), inserts the rest with the
    optional common tag.

    Returns {"added": n, "skipped": n, "duplicates": [targets already
    present in the table]}. Invalid lines are simply not targets after
    parsing (parser drops empties); anything the seeder parser accepts is
    considered valid — Telegram-level validation happens at task runtime.
    """
    from src.core.seeder import parse_targets

    batch: List[str] = []
    seen = set()
    for candidate in parse_targets(raw_text or ""):
        if candidate not in seen:
            seen.add(candidate)
            batch.append(candidate)
    if not batch:
        return {"added": 0, "skipped": 0, "duplicates": []}

    added: List[str] = []
    duplicates: List[str] = []
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT target FROM target_channels WHERE target = ANY(%s);",
                (batch,),
            )
            existing = {row[0] for row in cur.fetchall()}
            for target in batch:
                if target in existing:
                    duplicates.append(target)
                    continue
                cur.execute(
                    "INSERT INTO target_channels (target, tag) VALUES (%s, %s);",
                    (target, tag),
                )
                added.append(target)
        conn.commit()
    return {"added": len(added), "skipped": len(duplicates), "duplicates": duplicates}


# ---------------------------------------------------------------- studio ----

def list_templates() -> List[Dict[str, Any]]:
    templates: List[Dict[str, Any]] = []
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, template, is_active, created_at, updated_at
                FROM message_templates ORDER BY is_active DESC, id;
                """
            )
            for row in cur.fetchall():
                templates.append({
                    "id": row[0], "name": row[1], "template": row[2],
                    "is_active": row[3], "created": _now_iso(row[4]),
                    "updated": _now_iso(row[5]),
                })
    return templates


def insert_template(name: str, template: str, is_active: bool = False) -> None:
    """Create a template; exclusive activation is applied on request.

    A UNIQUE(name) violation bubbles to the caller (409 conflict).
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            if is_active:
                cur.execute("UPDATE message_templates SET is_active = FALSE;")
            cur.execute(
                "INSERT INTO message_templates (name, template, is_active) "
                "VALUES (%s, %s, %s);",
                (name, template, is_active),
            )
        conn.commit()


def update_template(template_id: int, template: str = None,
                    name: str = None, is_active: bool = None) -> bool:
    """Partial update of an existing template. Returns False if missing.

    Exclusive activation applies when is_active=True is requested: every
    other template is deactivated first, so the seeder always has exactly
    one active source.
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM message_templates WHERE id = %s;", (template_id,)
            )
            if not cur.fetchone():
                return False
            if name is not None:
                cur.execute(
                    "UPDATE message_templates SET name = %s, updated_at = NOW() WHERE id = %s;",
                    (name, template_id),
                )
            if template is not None:
                cur.execute(
                    "UPDATE message_templates SET template = %s, updated_at = NOW() WHERE id = %s;",
                    (template, template_id),
                )
            if is_active is not None:
                if is_active:
                    cur.execute("UPDATE message_templates SET is_active = FALSE;")
                cur.execute(
                    "UPDATE message_templates SET is_active = %s, updated_at = NOW() WHERE id = %s;",
                    (is_active, template_id),
                )
        conn.commit()
    return True


def toggle_template(template_id: int) -> None:
    """Exclusive activation: the chosen template becomes the only active one."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT is_active FROM message_templates WHERE id = %s;", (template_id,))
            row = cur.fetchone()
            if not row:
                return
            activating = not row[0]
            if activating:
                cur.execute("UPDATE message_templates SET is_active = FALSE;")
            cur.execute(
                "UPDATE message_templates SET is_active = %s, updated_at = NOW() WHERE id = %s;",
                (activating, template_id),
            )
        conn.commit()


def delete_template(template_id: int) -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM message_templates WHERE id = %s;", (template_id,))
        conn.commit()


# ---------------------------------------------------------------- tasks -----

def purge_all_tasks() -> Dict[str, Any]:
    """Wipe the ENTIRE tasks table (dashboard "Clear All Tasks").

    TRUNCATE ... RESTART IDENTITY resets the serial sequence so new task
    ids start from 1 again, and clears any task-queue scratch keys in
    Redis (the queue itself is PostgreSQL-backed; Redis holds only
    transient coordination keys such as task:<id> state hints).

    Returns {"cleared": <row count>, "redis_keys": <keys removed>}.
    A TASKS_PURGED audit row is written for the operations trail.
    """
    cleared = 0
    redis_keys = 0
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM tasks;")
            cleared = cur.fetchone()[0]
            cur.execute("TRUNCATE tasks RESTART IDENTITY;")
            cur.execute(
                "INSERT INTO system_logs (level, event_type, message, created_at) "
                "VALUES ('WARNING', 'TASKS_PURGED', %s, NOW());",
                (f"All {cleared} task(s) purged via dashboard (identity reset)",),
            )
        conn.commit()

    try:
        from src.core.redis_client import get_redis_client

        client = get_redis_client()
        keys = list(client.scan_iter(match="task:*"))
        if keys:
            redis_keys = client.delete(*keys)
    except Exception as exc:  # Redis is auxiliary — a purge must not fail
        logger.warning("Redis task-key flush skipped: %s", exc)

    logger.warning("Purged %d task(s), %d redis key(s)", cleared, redis_keys)
    return {"cleared": cleared, "redis_keys": redis_keys}


def task_page(status: str = "", target: str = "", since: str = "",
              limit: int = 100) -> List[Dict[str, Any]]:
    query = """
        SELECT id, target, action_type, status, priority, retry_count,
               error_message, created_at, scheduled_at, executed_at, updated_at
        FROM tasks WHERE TRUE
    """
    params: list = []
    if status:
        query += " AND status = %s"
        params.append(status.upper())
    if target:
        query += " AND target ILIKE %s"
        params.append(f"%{target}%")
    if since:
        query += " AND created_at >= %s"
        params.append(since)
    query += " ORDER BY id DESC LIMIT %s"
    params.append(min(limit, 500))
    tasks: List[Dict[str, Any]] = []
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, tuple(params))
            for row in cur.fetchall():
                tasks.append({
                    "id": row[0], "target": row[1], "action_type": row[2],
                    "status": row[3], "priority": row[4], "retry_count": row[5],
                    "error_message": row[6], "created": _now_iso(row[7]),
                    "scheduled": _now_iso(row[8]), "executed": _now_iso(row[9]),
                    "updated": _now_iso(row[10]),
                })
    return tasks


def task_detail(task_id: int) -> Optional[Dict[str, Any]]:
    detail: Optional[Dict[str, Any]] = None
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, target, action_type, payload, status, priority, retry_count,
                       error_message, created_at, scheduled_at, executed_at, updated_at
                FROM tasks WHERE id = %s;
                """,
                (task_id,),
            )
            row = cur.fetchone()
            if row:
                payload = row[3]
                if isinstance(payload, str):
                    payload = json.loads(payload)
                detail = {
                    "id": row[0], "target": row[1], "action_type": row[2],
                    "payload": payload, "status": row[4], "priority": row[5],
                    "retry_count": row[6], "error_message": row[7],
                    "created": _now_iso(row[8]), "scheduled": _now_iso(row[9]),
                    "executed": _now_iso(row[10]), "updated": _now_iso(row[11]),
                    "logs": [],
                }
                cur.execute(
                    """
                    SELECT level, event_type, message, created_at FROM system_logs
                    WHERE message ILIKE %s OR event_type IN
                        ('TASK_REQUEUED', 'STALE_TASK_RECOVERED', 'DELIVERY_VERIFIED')
                    ORDER BY id DESC LIMIT 40;
                    """,
                    (f"%Task #{task_id}%",),
                )
                for log in cur.fetchall():
                    detail["logs"].append({
                        "level": log[0], "event_type": log[1],
                        "message": log[2], "created": _now_iso(log[3]),
                    })
    return detail


def cancel_task(task_id: int) -> bool:
    """Cancel a PENDING task (terminal CANCELLED status)."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET status = 'CANCELLED', updated_at = NOW() "
                "WHERE id = %s AND status = 'PENDING';",
                (task_id,),
            )
            updated = cur.rowcount
        conn.commit()
    return updated > 0


def purge_task(task_id: int) -> bool:
    """Delete a finished task from the queue."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM tasks WHERE id = %s AND status IN "
                "('COMPLETED', 'FAILED', 'CANCELLED');",
                (task_id,),
            )
            deleted = cur.rowcount
        conn.commit()
    return deleted > 0


# ---------------------------------------------------------------- logs ------

def latest_system_logs(limit: int = 200) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT level, event_type, message, created_at FROM system_logs "
                "ORDER BY id DESC LIMIT %s;",
                (min(limit, 500),),
            )
            for row in cur.fetchall():
                entries.append({
                    "level": row[0], "event_type": row[1],
                    "message": row[2], "created": _now_iso(row[3]),
                })
    return entries


# ------------------------------------------------------------- test send ----

def run_direct_test_send(target: str, template: str) -> Dict[str, Any]:
    """Send a one-off test message using the live account + proxy settings."""
    import asyncio

    from src.accounts.manager import AccountManager
    from telethon.tl.types import PeerChannel, PeerChat
    from src.core.templates import SpintaxEngine
    from src.core.dispatcher import TaskDispatcher

    destination = __import__("os").getenv("GROWTH_DESTINATION_CHANNEL", "").strip() or "@proxgram"
    text = SpintaxEngine.render_promo(template or "Test message from dashboard: {channel_link}", destination)

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, session_string, proxy_config FROM accounts "
                "WHERE status = 'ACTIVE' ORDER BY id LIMIT 1;"
            )
            row = cur.fetchone()
    if not row:
        return {"ok": False, "error": "no active account"}

    manager = AccountManager()
    proxy = row[2] if isinstance(row[2], dict) else (json.loads(row[2]) if row[2] else None)
    client = manager.create_client(row[1], proxy)

    async def _send():
        await manager.connect_with_fallback(client)
        if not await client.is_user_authorized():
            raise RuntimeError("account unauthorized")
        s = str(target).strip()
        if s.startswith("-100") and s[4:].isdigit():
            entity = PeerChannel(int(s[4:]))
        elif s.lstrip("-").isdigit():
            entity = PeerChat(int(s))
        else:
            entity = await client.get_input_entity(s)
        sent = await client.send_message(entity, text)
        confirmed = await TaskDispatcher._verify_delivery(
            _dispatcher, client, entity, {"id": 0, "target": s}, sent.id
        )
        return sent.id, confirmed

    _dispatcher = TaskDispatcher()
    try:
        message_id, verified = asyncio.run(_send())
        return {"ok": True, "message_id": message_id,
                "verified": verified is not None, "text": text}
    finally:
        try:
            asyncio.run(client.disconnect())
        except Exception:
            pass
