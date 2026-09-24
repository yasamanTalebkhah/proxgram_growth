#!/usr/bin/env python3
"""Non-interactive QR-code login for the growth worker session.

Flow:
  1. Run this script (it must stay alive while the QR is being scanned).
  2. It renders the Telegram "Link Desktop Device" QR code:
       - URL + ASCII art to qr_login.txt (stdout too)
       - PNG to qr_login.png (when Pillow is available)
  3. Scan it with an already-authorized Telegram app:
       Settings > Devices > Link Desktop Device
  4. On success the exported StringSession is written to .env under
     SESSION_STRING — the value itself is never printed.

Telethon's QR login uses the ExportLoginToken device-linking flow (the same
path official desktop apps use), which is not the captcha-gated SendCodeRequest
path, so it typically works when SMS code delivery is blocked.

Optional 2FA: set the TELEGRAM_2FA_PASSWORD environment variable before
running if the account has a cloud password.

Usage (from the repository root):
    python scripts/generate_session_qr.py
"""

from __future__ import annotations

import asyncio
import os
import re
import stat
import sys
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
QR_TXT = ROOT / "qr_login.txt"
QR_PNG = ROOT / "qr_login.png"

MAX_QR_REGENERATIONS = 20  # each token lives ~30 s; re-render on expiry
WAIT_TIMEOUT = 45


def log(msg: str) -> None:
    print(msg, flush=True)


def set_env_value(env_path: Path, key: str, value: str) -> None:
    """Set KEY=value in the env file in place, without echoing the value."""
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    replacement = f"{key}={value}"
    if pattern.search(text):
        text = pattern.sub(lambda _m: replacement, text, count=1)
    else:
        text = text.rstrip("\n") + "\n" + replacement + "\n"
    env_path.write_text(text, encoding="utf-8")
    try:
        env_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def render_qr_artifacts(url: str) -> None:
    """Write the QR as text (URL + ASCII art) and, when possible, as PNG."""
    lines = [url, ""]
    try:
        import qrcode

        qr = qrcode.QRCode(border=2)
        qr.add_data(url)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        # Two visual variants: dark modules on light, and inverted, so at
        # least one scans well regardless of the reader's background.
        lines.append("dark-on-light:")
        lines += ["".join("██" if cell else "  " for cell in row) for row in matrix]
        lines.append("")
        lines.append("light-on-dark (inverted):")
        lines += ["".join("  " if cell else "██" for cell in row) for row in matrix]
        lines.append("")
        try:
            qr.make_image(fill_color="black", back_color="white").save(QR_PNG)
            log(f"[+] PNG QR written to {QR_PNG.name}")
        except Exception as e:  # Pillow missing or save failed
            log(f"[i] PNG rendering unavailable ({type(e).__name__}) - text only")
    except ImportError:
        lines += ["(qrcode package not installed - printing URL only)"]
    QR_TXT.write_text("\n".join(lines), encoding="utf-8")


async def main() -> int:
    load_dotenv(ENV_FILE)
    api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        log("[-] TELEGRAM_API_ID / TELEGRAM_API_HASH missing in .env")
        return 2
    password = os.getenv("TELEGRAM_2FA_PASSWORD")  # optional, not required

    client = TelegramClient(StringSession(), int(api_id), api_hash)
    await client.connect()
    try:
        if await client.is_user_authorized():
            log("[i] Session already authorized - nothing to do.")
            return 0

        log("[*] Requesting QR login token (scan with: Settings > Devices > Link Desktop Device)")
        user = None
        for attempt in range(1, MAX_QR_REGENERATIONS + 1):
            qr = await client.qr_login()
            log(f"[*] QR token {attempt}/{MAX_QR_REGENERATIONS} ready - waiting for scan ({WAIT_TIMEOUT}s window)")
            render_qr_artifacts(qr.url)

            try:
                # Telethon prints its own ASCII QR via qr.print_ascii(); our
                # file artifacts above carry the same token for display.
                user = await qr.wait(timeout=WAIT_TIMEOUT)
                break
            except asyncio.TimeoutError:
                log("[i] No scan within the window - token expired, regenerating...")
                continue
            except SessionPasswordNeededError:
                if not password:
                    log("[-] 2FA is enabled: set TELEGRAM_2FA_PASSWORD in the environment and re-run.")
                    return 3
                user = await client.sign_in(password=password)
                break

        if user is None:
            log("[-] QR login did not complete within the attempt budget.")
            return 1

        session_string = client.export_session_string()
        set_env_value(ENV_FILE, "SESSION_STRING", session_string)
        QR_TXT.unlink(missing_ok=True)
        QR_PNG.unlink(missing_ok=True)

        uname = f"@{user.username}" if user.username else "(no username)"
        log(f"[+] QR login succeeded: {user.first_name} (id={user.id}, {uname})")
        log(f"[+] SESSION_STRING written to .env ({len(session_string)} chars, value not displayed)")
        return 0
    except Exception as e:
        log(f"[-] QR login failed: {type(e).__name__}: {e}")
        return 1
    finally:
        await client.disconnect()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
