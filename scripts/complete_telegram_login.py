#!/usr/bin/env python3
"""Stage 2 of non-interactive Telegram login: complete the sign-in.

Reads TELEGRAM_API_ID / TELEGRAM_API_HASH from .env and {phone, phone_code_hash}
from .telegram_login_state.json (written by scripts/request_telegram_code.py),
completes the login with the delivered code (and the 2FA password if the
account has one), exports the Telethon StringSession, and writes SESSION_STRING
into .env — never printing the session to stdout. On success the state file is
removed.

Usage (from the repository root):
    python scripts/complete_telegram_login.py <5-digit-code> [2fa-password]

Note: the session string is written to .env in-process (not via a shell
substitution) so it never appears in a command line or process list.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import sys
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import (
    PhoneCodeEmptyError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / ".telegram_login_state.json"
ENV_FILE = ROOT / ".env"


def set_env_value(env_path: Path, key: str, value: str) -> None:
    """Set KEY=value in the env file, in place, without echoing the value."""
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    replacement_line = f"{key}={value}"
    if pattern.search(text):
        text = pattern.sub(lambda _m: replacement_line, text, count=1)
    else:
        text = text.rstrip("\n") + "\n" + replacement_line + "\n"
    env_path.write_text(text, encoding="utf-8")
    try:
        env_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def masked(phone: str) -> str:
    return phone[:4] + "****" + phone[-2:] if len(phone) >= 6 else "***"


async def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python scripts/complete_telegram_login.py <code> [2fa-password]", file=sys.stderr)
        return 2
    code = sys.argv[1].strip()
    password = sys.argv[2] if len(sys.argv) > 2 else None

    load_dotenv(ENV_FILE)
    api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        print("[-] TELEGRAM_API_ID / TELEGRAM_API_HASH missing in .env", file=sys.stderr)
        return 2
    if not STATE_FILE.exists():
        print("[-] Login state missing: run scripts/request_telegram_code.py first.", file=sys.stderr)
        return 2

    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    phone, phone_code_hash = state["phone"], state["phone_code_hash"]

    client = TelegramClient(StringSession(), int(api_id), api_hash)
    await client.connect()
    try:
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        except SessionPasswordNeededError:
            if not password:
                print("[-] Two-factor authentication is enabled on this account.", file=sys.stderr)
                print("[-] Re-run with the password as the second argument (never commit it).", file=sys.stderr)
                return 3
            await client.sign_in(password=password)
        except (PhoneCodeInvalidError, PhoneCodeExpiredError, PhoneCodeEmptyError) as e:
            print(f"[-] Code rejected ({type(e).__name__}). Request a fresh code and retry once.", file=sys.stderr)
            return 4

        me = await client.get_me()
        session_string = client.export_session_string()
        set_env_value(ENV_FILE, "SESSION_STRING", session_string)
        STATE_FILE.unlink(missing_ok=True)

        uname = f"@{me.username}" if me.username else "(no username)"
        print(f"[+] Signed in as {me.first_name} (id={me.id}, {uname})")
        print(f"[+] SESSION_STRING written to .env ({len(session_string)} chars, value not displayed)")
        print(f"[+] {STATE_FILE.name} removed")
        return 0
    except Exception as e:
        print(f"[-] Sign-in failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        await client.disconnect()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
