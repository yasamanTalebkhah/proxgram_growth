#!/usr/bin/env python3
"""Stage 1 of non-interactive Telegram login: request the login code.

Reads TELEGRAM_API_ID / TELEGRAM_API_HASH from .env, sends the Telegram
login code to the given phone number, and stores phone + phone_code_hash in
.telegram_login_state.json for the (separate) sign-in stage.

The phone_code_hash is an authorization token: the state file must never be
committed or shared. It is created with owner-only permissions.

Usage (from the repository root):
    python scripts/request_telegram_code.py +989123456789
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / ".telegram_login_state.json"


def masked(phone: str) -> str:
    return phone[:4] + "****" + phone[-2:] if len(phone) >= 6 else "***"


async def main() -> int:
    load_dotenv(ROOT / ".env")
    api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        print("[-] TELEGRAM_API_ID / TELEGRAM_API_HASH missing in .env", file=sys.stderr)
        return 2

    if len(sys.argv) != 2:
        print("usage: python scripts/request_telegram_code.py <phone>", file=sys.stderr)
        return 2
    phone = sys.argv[1].strip()

    client = TelegramClient(StringSession(), int(api_id), api_hash)
    await client.connect()
    try:
        if await client.is_user_authorized():
            print("[i] Session already authorized - no code needed.")
            return 0

        sent = await client.send_code_request(phone)
        STATE_FILE.write_text(json.dumps({"phone": phone, "phone_code_hash": sent.phone_code_hash}))
        try:
            STATE_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)  # owner read/write only
        except OSError:
            pass
        print(f"[+] Code sent to {masked(phone)} via {type(sent.type).__name__ if sent.type else 'unknown channel'}")
        print(f"[+] Login state stored in {STATE_FILE.name} (phone_code_hash kept out of output)")
        return 0
    except FloodWaitError as e:
        print(f"[-] FloodWait: Telegram requires waiting {e.seconds}s before the next attempt.", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"[-] Code request failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        await client.disconnect()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
