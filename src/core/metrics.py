"""Lifecycle performance metrics for the growth engine (observability).

Aggregates task-lifecycle health from the PostgreSQL SSOT:
  - success rate (COMPLETED vs FAILED),
  - average time-to-delivery (created_at -> executed_at),
  - requeue / recovery frequency and backoff efficiency,
  - delivery read-back coverage.

Used by scripts/metrics_reporter.py (host or `docker compose exec worker`).
"""

import os
from typing import Any, Dict, Optional

from src.database.connection import get_db_connection

_LIFECYCLE_EVENTS = (
    "TASK_REQUEUED",
    "STALE_TASK_RECOVERED",
    "DELIVERY_VERIFIED",
    "CIRCUIT_BREAKER_TRIPPED",
)


def collect_lifecycle_metrics(max_retries: Optional[int] = None) -> Dict[str, Any]:
    """Return aggregated lifecycle metrics as a plain dict.

    A task that failed transiently and later succeeded counts once, as
    COMPLETED — so the success rate pairs completed deliveries against
    *currently* failed tasks, while backoff efficiency is reported via
    retry graduates vs terminal failures.
    """
    if max_retries is None:
        max_retries = int(os.getenv("GROWTH_MAX_RETRIES", "3"))

    m: Dict[str, Any] = {}
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status;")
            counts = dict(cur.fetchall())
            m["task_counts"] = counts
            completed = counts.get("COMPLETED", 0)
            failed = counts.get("FAILED", 0)
            finished = completed + failed
            m["success_rate"] = round(100 * completed / finished, 1) if finished else None

            cur.execute(
                """
                SELECT COUNT(*), COALESCE(AVG(retry_count), 0)
                FROM tasks
                WHERE status = 'COMPLETED' AND retry_count > 0;
                """
            )
            row = cur.fetchone()
            m["retry_graduates"] = int(row[0])
            m["avg_retries_of_graduates"] = round(float(row[1]), 2)

            cur.execute(
                """
                SELECT COUNT(*),
                       COALESCE(AVG(EXTRACT(EPOCH FROM (executed_at - created_at))), 0)
                FROM tasks
                WHERE status = 'COMPLETED' AND executed_at IS NOT NULL;
                """
            )
            row = cur.fetchone()
            m["deliveries_measured"] = int(row[0])
            m["avg_time_to_delivery_seconds"] = round(float(row[1]), 1)

            for event in _LIFECYCLE_EVENTS:
                cur.execute(
                    "SELECT COUNT(*) FROM system_logs WHERE event_type = %s;",
                    (event,),
                )
                m[event] = cur.fetchone()[0]

            cur.execute(
                """
                SELECT COUNT(*) FROM tasks
                WHERE status = 'FAILED' AND retry_count >= %s;
                """,
                (max_retries,),
            )
            m["terminal_failures"] = cur.fetchone()[0]
    return m


def format_lifecycle_summary(m: Dict[str, Any]) -> str:
    """Render the metrics dict as a concise, human-readable summary."""

    def pct(value) -> str:
        return f"{value}%" if value is not None else "n/a"

    def duration(seconds) -> str:
        if seconds is None or m.get("deliveries_measured", 0) == 0:
            return "n/a"
        seconds = float(seconds)
        return f"{seconds / 60:.1f} min" if seconds >= 90 else f"{seconds:.0f} s"

    counts = m.get("task_counts", {})
    completed = counts.get("COMPLETED", 0)
    lines = [
        "Lifecycle Performance (all-time):",
        f"  - Success rate: {pct(m.get('success_rate'))} "
        f"({completed} completed / {counts.get('FAILED', 0)} failed)",
        f"  - Avg time-to-delivery (PENDING -> COMPLETED): "
        f"{duration(m.get('avg_time_to_delivery_seconds'))} "
        f"over {m.get('deliveries_measured', 0)} measured send(s)",
        f"  - Requeue events: {m.get('TASK_REQUEUED', 0)} | "
        f"orphan recoveries: {m.get('STALE_TASK_RECOVERED', 0)}",
        f"  - Backoff efficiency: {m.get('retry_graduates', 0)} retried task(s) eventually "
        f"completed (avg {m.get('avg_retries_of_graduates', 0)} attempt(s) used); "
        f"{m.get('terminal_failures', 0)} terminally failed",
        f"  - Delivery verification coverage: {m.get('DELIVERY_VERIFIED', 0)}/{completed} "
        f"send(s) read-back confirmed",
        f"  - Circuit breaker trips: {m.get('CIRCUIT_BREAKER_TRIPPED', 0)}",
    ]
    return "\n".join(lines)
