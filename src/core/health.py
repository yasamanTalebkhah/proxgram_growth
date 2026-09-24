"""Health probes for the dashboard (PostgreSQL, Redis, worker heartbeat, SOCKS)."""

import os
import socket
from datetime import datetime, timezone
from typing import Any, Dict

from src.database.connection import get_db_connection


def postgres_ok() -> bool:
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
                return cur.fetchone()[0] == 1
    except Exception:
        return False


def redis_ok() -> bool:
    try:
        from src.core.redis_client import get_redis_client

        return bool(get_redis_client().ping())
    except Exception:
        return False


def socks_proxy_ok() -> bool:
    """TCP-probe the user's SOCKS inbound (read-only; never binds it).

    Defaults to the v2rayN-style local proxy reachable from containers.
    Refuses to probe the dashboard's own port as a safety assertion.
    """
    host = os.getenv("DASHBOARD_SOCKS_PROBE_HOST", "host.docker.internal")
    port = int(os.getenv("DASHBOARD_SOCKS_PROBE_PORT", "10808"))
    dashboard_port = int(os.getenv("DASHBOARD_PORT", "8080"))
    if port == dashboard_port:
        return False
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except Exception:
        return False


def get_worker_heartbeat() -> Dict[str, Any]:
    """Derive worker liveness from the newest worker lifecycle audit event."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT event_type, created_at FROM system_logs
                    WHERE event_type IN
                        ('WORKER_STARTED', 'WORKER_STOPPED', 'CIRCUIT_BREAKER_TRIPPED')
                    ORDER BY id DESC LIMIT 1;
                    """
                )
                row = cur.fetchone()
        if not row:
            return {"running": False, "last_event": None, "seconds_ago": None}
        event_type, created_at = row
        seconds_ago = int((datetime.now(timezone.utc) - created_at).total_seconds())
        return {
            "running": event_type == "WORKER_STARTED",
            "last_event": event_type,
            "seconds_ago": seconds_ago,
        }
    except Exception:
        return {"running": False, "last_event": None, "seconds_ago": None}
