"""Task seeding engine: turn configured targets into PENDING SEND_MESSAGE tasks.

Target sources (merged, deduplicated):
  - `target_channels` table rows where enabled = TRUE (dashboard-managed)
  - GROWTH_TARGET_CHANNELS env var (legacy fallback)

Message content comes from the active row in `message_templates`
(dashboard Spintax Studio), falling back to the dispatcher's default.

Dedupe: targets with a PENDING or COMPLETED SEND_MESSAGE task created within
the last GROWTH_SEED_DEDUPE_HOURS (default 24) are skipped, so boot-time
seeding, periodic seeding and manual runs never flood the queue.
"""

import json
import logging
import os
import re
from datetime import timedelta

from src.core.settings import get_setting_int
from src.database.connection import get_db_connection

logger = logging.getLogger("seeder")

# Same default the dispatcher falls back to when a payload omits "template".
DEFAULT_TEMPLATE = "{سلام|درود} دوستان! جهت دریافت نرخ لحظه‌ای و پروکسی: {channel_link}"
DEFAULT_DESTINATION = "@proxgram"

DEDUPE_SQL = """
    SELECT 1 FROM tasks
    WHERE target = %s
      AND action_type = 'SEND_MESSAGE'
      AND status IN ('PENDING', 'COMPLETED')
      AND created_at > NOW() - %s
    LIMIT 1;
"""

INSERT_TASK_SQL = """
    INSERT INTO tasks (target, action_type, payload, status, priority)
    VALUES (%s, 'SEND_MESSAGE', %s::jsonb, 'PENDING', 0)
    RETURNING id;
"""

INSERT_LOG_SQL = """
    INSERT INTO system_logs (level, event_type, message, created_at)
    VALUES ('INFO', 'TASKS_SEEDED', %s, NOW());
"""


def _db_targets() -> list:
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT target FROM target_channels WHERE enabled = TRUE ORDER BY id;"
                )
                return [row[0] for row in cur.fetchall()]
    except Exception as exc:
        logger.warning(f"Could not read target_channels (falling back to env): {exc}")
        return []


def _env_targets() -> list:
    raw = os.getenv("GROWTH_TARGET_CHANNELS", "")
    return [t for t in (x.strip() for x in re.split(r"[,;\s]+", raw)) if t]


def _active_template() -> str:
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT template FROM message_templates WHERE is_active = TRUE "
                    "ORDER BY updated_at DESC LIMIT 1;"
                )
                row = cur.fetchone()
                return row[0] if row else DEFAULT_TEMPLATE
    except Exception:
        return DEFAULT_TEMPLATE


def parse_targets(raw: str) -> list:
    """Split GROWTH_TARGET_CHANNELS on commas / semicolons / whitespace."""
    return [t for t in (x.strip() for x in re.split(r"[,;\s]+", raw or "")) if t]


def seed_tasks(dedupe_hours: int | None = None, force: bool = False) -> dict:
    """Insert one PENDING SEND_MESSAGE task per configured target.

    Safe to call repeatedly — the dedupe window prevents duplicates.
    Returns {"seeded": [(target, task_id)], "skipped": {target: reason}}.
    """
    if dedupe_hours is None:
        dedupe_hours = get_setting_int("GROWTH_SEED_DEDUPE_HOURS")
    window = timedelta(hours=dedupe_hours)

    seen: set = set()
    targets: list = []
    for target in _db_targets() + _env_targets():
        if target not in seen:
            seen.add(target)
            targets.append(target)

    destination = os.getenv("GROWTH_DESTINATION_CHANNEL", "").strip() or DEFAULT_DESTINATION
    summary: dict = {"seeded": [], "skipped": {}}

    if not targets:
        logger.warning("No targets configured (target_channels table and env both empty).")
        return summary

    payload = json.dumps({"template": _active_template(), "channel_link": destination})

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            for target in targets:
                if not force:
                    cur.execute(DEDUPE_SQL, (target, window))
                    if cur.fetchone():
                        summary["skipped"][target] = f"task exists within last {dedupe_hours}h"
                        continue
                cur.execute(INSERT_TASK_SQL, (target, payload))
                task_id = cur.fetchone()[0]
                summary["seeded"].append((target, task_id))
                logger.info(f"Seeded task {task_id} -> {target}")

            if summary["seeded"]:
                message = (
                    f"Seeded {len(summary['seeded'])} task(s): "
                    + ", ".join(f"#{tid} {target}" for target, tid in summary["seeded"])
                    + (f"; skipped {len(summary['skipped'])} (dedupe)" if summary["skipped"] else "")
                )
                cur.execute(INSERT_LOG_SQL, (message,))
        # Single commit covers task inserts + the audit log row.
        conn.commit()

    return summary


def _print_pending_queue() -> None:
    query = """
        SELECT id, target, priority, created_at
        FROM tasks
        WHERE status = 'PENDING'
        ORDER BY priority DESC, id ASC;
    """
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query)
                rows = cur.fetchall()
    except Exception as exc:
        logger.error(f"Could not read pending queue: {exc}")
        return

    print("\n=== PENDING queue ===")
    if not rows:
        print("  (empty)")
        return
    print(f"  {'ID':>4}  {'PRIO':>4}  {'CREATED (UTC)':<19}  TARGET")
    for task_id, target, priority, created in rows:
        created_str = created.strftime("%Y-%m-%d %H:%M:%S") if created else "-"
        print(f"  {task_id:>4}  {priority:>4}  {created_str:<19}  {target}")


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Seed SEND_MESSAGE tasks from configured targets."
    )
    parser.add_argument(
        "--dedupe-hours", type=int, default=None,
        help="Skip targets with a task newer than this many hours "
             "(default: GROWTH_SEED_DEDUPE_HOURS or 24)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Bypass the dedupe check and seed every target",
    )
    args = parser.parse_args()

    try:
        summary = seed_tasks(dedupe_hours=args.dedupe_hours, force=args.force)
    except Exception as exc:
        logger.error(f"Seeding failed: {exc}")
        return 1

    print("\n=== Seed summary ===")
    for target, task_id in summary["seeded"]:
        print(f"  [new]      task #{task_id}  target={target}")
    for target, reason in summary["skipped"].items():
        print(f"  [skipped]  target={target}  ({reason})")

    if not summary["seeded"] and not summary["skipped"]:
        print("  (no targets configured — check target_channels / GROWTH_TARGET_CHANNELS)")
        return 1

    _print_pending_queue()
    return 0
