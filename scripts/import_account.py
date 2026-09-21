"""Import and validate a Telethon userbot session into the accounts table (Phase 2).

Usage (from the repository root):
    python -m scripts.import_account --phone +989123456789 --session "<StringSession>"
    python -m scripts.import_account --phone ... --session ... \
        --proxy '{"enabled": true, "type": "socks5", "host": "1.2.3.4", "port": 1080}'
"""

import argparse
import asyncio
import json
import os
import sys

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.database.connection import get_db_connection

load_dotenv()


async def validate_and_save(phone: str, session_str: str, proxy_json: str | None) -> bool:
    api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        print("[-] TELEGRAM_API_ID / TELEGRAM_API_HASH must be configured in the environment.")
        return False

    proxy = None
    if proxy_json:
        try:
            proxy = json.loads(proxy_json)
        except Exception as e:
            print(f"[-] Invalid proxy JSON format: {e}")
            return False

    print(f"[*] Validating session for {phone}...")
    client = TelegramClient(StringSession(session_str), int(api_id), api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            print("[-] Session string is invalid or expired (not authorized).")
            await client.disconnect()
            return False

        me = await client.get_me()
        username = f"@{me.username}" if me.username else "(no username)"
        print(f"[+] Successfully authenticated as: {me.first_name} (ID: {me.id}, Username: {username})")
        await client.disconnect()
    except Exception as e:
        print(f"[-] Telethon connection error: {e}")
        return False

    print("[*] Storing account in database...")
    query = """
        INSERT INTO accounts (phone_number, session_string, proxy_config, status, failure_count)
        VALUES (%s, %s, %s, 'ACTIVE', 0)
        ON CONFLICT (phone_number)
        DO UPDATE SET session_string = EXCLUDED.session_string,
                      proxy_config = EXCLUDED.proxy_config,
                      status = 'ACTIVE',
                      failure_count = 0,
                      updated_at = NOW()
        RETURNING id;
    """
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (phone, session_str, json.dumps(proxy) if proxy else None))
                account_id = cur.fetchone()[0]
            conn.commit()
        print(f"[+] Account registered successfully with ID: {account_id}")
        return True
    except Exception as e:
        print(f"[-] Database storage error: {e}")
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Import and validate Telegram account session")
    parser.add_argument("--phone", required=True, help="Account phone number (e.g. +1234567890)")
    parser.add_argument("--session", required=True, help="Telethon StringSession string")
    parser.add_argument(
        "--proxy",
        required=False,
        default=None,
        help='Proxy configuration in JSON format, e.g. {"enabled": true, "type": "socks5", "host": "1.2.3.4", "port": 1080}',
    )

    args = parser.parse_args()
    asyncio.run(validate_and_save(args.phone, args.session, args.proxy))


if __name__ == "__main__":
    main()
