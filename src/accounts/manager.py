"""Telethon account orchestration (Phase 2).

Loads ACTIVE accounts from the PostgreSQL SSOT, builds authenticated
TelegramClient instances (with optional per-account proxy binding) and
tracks account health state back into the database.
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

from src.database.connection import get_db_connection

load_dotenv()

logger = logging.getLogger(__name__)


class AccountManager:
    """Multi-session Telethon orchestration on top of the accounts table."""

    def __init__(self) -> None:
        self.api_id = os.getenv("TELEGRAM_API_ID")
        self.api_hash = os.getenv("TELEGRAM_API_HASH")
        if self.api_id:
            self.api_id = int(self.api_id)

    def get_active_accounts(self) -> List[Dict[str, Any]]:
        query = """
            SELECT id, phone_number, session_string, proxy_config, status, failure_count
            FROM accounts
            WHERE status = 'ACTIVE'
            ORDER BY id ASC;
        """
        accounts: List[Dict[str, Any]] = []
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query)
                for row in cur.fetchall():
                    proxy = row[3]
                    if isinstance(proxy, str):
                        proxy = json.loads(proxy)
                    accounts.append(
                        {
                            "id": row[0],
                            "phone_number": row[1],
                            "session_string": row[2],
                            "proxy": proxy,
                            "status": row[4],
                            "failure_count": row[5],
                        }
                    )
        return accounts

    def create_client(
        self, session_string: str, proxy: Optional[Dict[str, Any]] = None
    ) -> TelegramClient:
        if not self.api_id or not self.api_hash:
            raise ValueError(
                "TELEGRAM_API_ID and TELEGRAM_API_HASH environment variables must be configured."
            )

        session = StringSession(session_string)
        proxy_param = None
        if proxy and proxy.get("enabled", False):
            import socks  # provided by the PySocks package

            proxy_type_map = {
                "socks5": socks.SOCKS5,
                "socks4": socks.SOCKS4,
                "http": socks.HTTP,
            }
            ptype = proxy_type_map.get(
                str(proxy.get("type", "socks5")).lower(), socks.SOCKS5
            )
            proxy_param = (
                ptype,
                proxy.get("host"),
                int(proxy.get("port")),
                True,
                proxy.get("username"),
                proxy.get("password"),
            )

        return TelegramClient(session, self.api_id, self.api_hash, proxy=proxy_param)

    def update_account_status(
        self, account_id: int, status: str, failure_increment: bool = False
    ) -> None:
        query = """
            UPDATE accounts
            SET status = %s,
                failure_count = CASE WHEN %s THEN failure_count + 1 ELSE failure_count END,
                updated_at = NOW()
            WHERE id = %s;
        """
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (status, failure_increment, account_id))
            conn.commit()

    def record_success(self, account_id: int) -> None:
        """Reset the failure counter after a healthy interaction."""
        query = """
            UPDATE accounts
            SET failure_count = 0, updated_at = NOW()
            WHERE id = %s;
        """
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (account_id,))
            conn.commit()
