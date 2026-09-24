#!/usr/bin/env python3
"""QR-code login → writes SESSION_STRING into .env (secret is never printed).

Tokens auto-refresh every ~45 s; scan the QR from an already-authorized
Telegram app:  Settings → Devices → Link Desktop Device
Artifacts while waiting: qr_login.png / qr_preview.html (both gitignored).
"""
import asyncio
import base64
import os
import sys

# Windows redirected stdout defaults to cp1252; the ASCII QR needs UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001 - non-critical
    pass

from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID = 6
API_HASH = "eb06d4abfb49dc3eeb1aeb98ae0f581e"
TOKEN_TIMEOUT = 45
MAX_TOKENS = 20

PREVIEW_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="20">
<title>Telegram QR Login</title>
<style>
  body {{ background:#17212b; color:#fff; font-family:sans-serif;
         display:flex; flex-direction:column; align-items:center;
         justify-content:center; height:100vh; margin:0; }}
  img {{ width:420px; height:420px; image-rendering:pixelated;
        background:#fff; padding:16px; border-radius:12px; }}
  p {{ margin-top:18px; opacity:.8; text-align:center; }}
</style></head>
<body>
  <img src="data:image/png;base64,{b64}" alt="Telegram login QR">
  <p>Scan with Telegram → Settings → Devices → Link Desktop Device<br>
     (auto-refreshes every 20 s with the current token)</p>
</body></html>
"""


def save_session_string(value: str) -> None:
    """Replace/append SESSION_STRING in .env without printing it."""
    path = ".env"
    lines = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    key = "SESSION_STRING="
    out, replaced = [], False
    for line in lines:
        if line.startswith(key):
            out.append(key + value)
            replaced = True
        elif line.strip() or out:
            out.append(line)
    if not replaced:
        out.append(key + value)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out).rstrip("\n") + "\n")


def cleanup_artifacts() -> None:
    for name in ("qr_login.png", "qr_login.txt", "qr_preview.html"):
        try:
            os.remove(name)
        except OSError:
            pass


def render_qr(url: str) -> None:
    print("\n---QR_START---", flush=True)
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.print_ascii(invert=True)
    except ImportError:
        print(url)
    print("---QR_END---", flush=True)

    try:
        import qrcode
        img = qrcode.make(url)
        img.save("qr_login.png")
        with open("qr_login.png", "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        with open("qr_preview.html", "w", encoding="utf-8") as f:
            f.write(PREVIEW_HTML.format(b64=b64))
        print("[i] qr_login.png + qr_preview.html refreshed", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[!] artifact rendering failed: {exc!r}", flush=True)


async def main() -> None:
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()

    if await client.is_user_authorized():
        save_session_string(StringSession.save(client.session))
        print("SUCCESS: session already authorized; SESSION_STRING saved to .env", flush=True)
        await client.disconnect()
        return

    qr_login = await client.qr_login()
    render_qr(qr_login.url)

    for attempt in range(1, MAX_TOKENS + 1):
        try:
            await qr_login.wait(timeout=TOKEN_TIMEOUT)
            save_session_string(StringSession.save(client.session))
            print("SUCCESS: authorization complete; SESSION_STRING saved to .env", flush=True)
            cleanup_artifacts()
            await client.disconnect()
            return
        except asyncio.TimeoutError:
            print(f"[i] token {attempt}/{MAX_TOKENS} expired - refreshing", flush=True)
            try:
                await qr_login.recreate()
            except Exception:
                qr_login = await client.qr_login()
            render_qr(qr_login.url)
        except Exception as exc:  # noqa: BLE001
            # Tunnel flap / connection loss: reconnect and re-issue a token
            # instead of giving up (network may recover within seconds).
            print(f"[!] {type(exc).__name__} - reconnecting", flush=True)
            await asyncio.sleep(3)
            try:
                await client.connect()
                if await client.is_user_authorized():
                    save_session_string(StringSession.save(client.session))
                    print("SUCCESS: session authorized after reconnect", flush=True)
                    cleanup_artifacts()
                    await client.disconnect()
                    return
                qr_login = await client.qr_login()
                render_qr(qr_login.url)
            except Exception as exc2:  # noqa: BLE001
                print(f"[!] reconnect failed: {type(exc2).__name__}", flush=True)

    await client.disconnect()
    print("FAILED: no scan before token limit - rerun to start a fresh QR", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
