#!/usr/bin/env python3
"""One-time helper: generate a Telethon StringSession for the growth worker.

Run this ONCE on a trusted machine, interactively, then store the printed
session string in the GROWTH worker's secret store / environment. The
string grants full access to the account — never commit it, never log it.

Usage:
    python scripts/growth_session.py
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from telethon import TelegramClient  # noqa: E402
from telethon.sessions import StringSession  # noqa: E402


def main() -> int:
    print("ProxGram growth worker — session generator")
    print("------------------------------------------")
    api_id = input("API_ID: ").strip()
    api_hash = getpass.getpass("API_HASH: ").strip()
    if not api_id.isdigit() or not api_hash:
        print("Invalid input.", file=sys.stderr)
        return 2

    with TelegramClient(StringSession(), int(api_id), api_hash) as client:
        session_string = client.session.save()
        me = client.get_me()

    print()
    print("SESSION_STRING (store securely, e.g. in your .env / secret manager):")
    print(session_string)
    print()
    print(f"Logged in as: {getattr(me, 'first_name', '?')} (id={getattr(me, 'id', '?')})")
    print("Reminder: use a dedicated account for growth automation, never your personal one.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
