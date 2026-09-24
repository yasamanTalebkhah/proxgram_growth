"""Telethon account orchestration (Phase 2).

Loads ACTIVE accounts from the PostgreSQL SSOT, builds authenticated
TelegramClient instances (with optional per-account proxy binding) and
tracks account health state back into the database.
"""

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.network import (
    ConnectionTcpAbridged,
    ConnectionTcpFull,
    ConnectionTcpObfuscated,
)

from src.database.connection import get_db_connection

load_dotenv()

logger = logging.getLogger(__name__)

# MTProto transports tried in order by connect_with_fallback(). Abridged
# often survives DPI that severs Full handshakes; Obfuscated wraps the
# stream to look like random traffic. Authorization is transport-agnostic,
# so switching costs nothing but a reconnect.
TRANSPORT_FALLBACKS = (
    ConnectionTcpAbridged,
    ConnectionTcpFull,
    ConnectionTcpObfuscated,
)

DEFAULT_CONNECT_TIMEOUT_SECONDS = 20


class AccountManager:
    """Multi-session Telethon orchestration on top of the accounts table."""

    def __init__(self) -> None:
        self.api_id = os.getenv("TELEGRAM_API_ID")
        self.api_hash = os.getenv("TELEGRAM_API_HASH")
        if self.api_id:
            self.api_id = int(self.api_id)
        # Hard cap for every connection attempt (see connect_with_fallback).
        self.connect_timeout = int(
            os.getenv("GROWTH_CONNECT_TIMEOUT_SECONDS", str(DEFAULT_CONNECT_TIMEOUT_SECONDS))
        )

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
        self,
        session_string: str,
        proxy: Optional[Dict[str, Any]] = None,
        connection_cls: Any = None,
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

        if connection_cls is None:
            connection_cls = TRANSPORT_FALLBACKS[0]

        # connection_retries=1: internal transport retries are disabled on
        # purpose — connect_with_fallback() owns retry/fallback policy and
        # every attempt is bounded by self.connect_timeout.
        return TelegramClient(
            session,
            self.api_id,
            self.api_hash,
            proxy=proxy_param,
            connection=connection_cls,
            timeout=self.connect_timeout,
            connection_retries=1,
            retry_delay=1,
        )

    async def connect_with_fallback(self, client: TelegramClient) -> TelegramClient:
        """Connect the client, falling through TRANSPORT_FALLBACKS on failure.

        Each attempt is hard-bounded by GROWTH_CONNECT_TIMEOUT_SECONDS via
        asyncio.wait_for, so a silently dropped MTProto handshake can never
        hang the dispatcher loop. Raises ConnectionError when every
        transport in the sequence fails.
        """
        last_exc: Optional[BaseException] = None
        for transport in TRANSPORT_FALLBACKS:
            client._connection = transport
            try:
                await asyncio.wait_for(client.connect(), timeout=self.connect_timeout)
                return client
            except (asyncio.TimeoutError, ConnectionError, OSError, RuntimeError) as exc:
                last_exc = exc
                logger.warning(
                    f"Transport {transport.__name__} failed within "
                    f"{self.connect_timeout}s: {exc}"
                )
                try:
                    await client.disconnect()
                except Exception:
                    pass
        raise ConnectionError(
            f"All {len(TRANSPORT_FALLBACKS)} transports failed "
            f"(tried: {', '.join(t.__name__ for t in TRANSPORT_FALLBACKS)}); "
            f"last error: {last_exc}"
        )

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
