# proxgram_growth

Standalone, fully modular Telegram growth engine powered by a Telethon
**client userbot**. It monitors designated public channels, waits for new
posts, and posts timely, non-spam comments on the linked discussion threads
to route readers to the main proxy channel.

> **Security isolation:** this repository is deliberately decoupled from the
> main posting bot. It holds **no bot credentials** — only client-specific
> environment variables (`TELEGRAM_API_ID`, `TELEGRAM_API_HASH`,
> `SESSION_STRING`). A restriction on this userbot account can never touch
> the main bot.

## Project structure

```
main.py             # Core entry point: Telethon init, event listening, comment dispatch
config.py           # Centralized env loader + validator (supports .env)
templates.py        # Dynamic, rotatable anti-spam comment template repository
state_manager.py    # Persistent JSON state: processed channel/post IDs, cooldowns
rate_limit.py       # Per-target cooldown pacer + global rolling-window cap
backoff.py          # Exponential backoff (FloodWait-aware, full jitter)
logging_setup.py    # Logging with automatic credential redaction
scripts/
  generate_session.py   # One-time StringSession generator
config/
  growth_worker.sample.json     # Non-secret settings sample
  growth_templates.sample.json  # Comment template sample
tests/              # Unit tests (no network, no real accounts)
requirements.txt    # Production dependencies: telethon, python-dotenv
```

## Safety model

| Layer | Mechanism |
|---|---|
| Isolation | Own repo, own process, own userbot session; no bot tokens anywhere |
| Human-like pacing | 5–20 s randomized delay before engaging a fresh post |
| Per-target cooldown | Max 1 comment per target channel per 10 min + jitter |
| Global cap | Max 12 comments per rolling hour across all targets |
| Idempotency | Processed channel/post IDs persisted; redelivered updates never double-comment |
| Template rotation | Randomized wording; never the same template twice in a row per channel |
| FloodWait | Honors Telegram's wait + margin, exponential backoff with full jitter |
| Permission errors | `ChatWriteForbidden` / `ChannelPrivate` give up immediately |
| Credential hygiene | Secrets scrubbed from every log line; state file stores timestamps and IDs only |
| Persistence | Cooldowns and processed posts survive restarts via `data/state.json` |

## Setup

### 1. Install

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Generate a session (once, on a trusted machine)

Use a **dedicated account** for automation — never your personal one.

```bash
python scripts/generate_session.py
```

### 3. Configure environment variables

```bash
cp .env.example .env
# edit .env and fill in:
#   TELEGRAM_API_ID / TELEGRAM_API_HASH / SESSION_STRING
#   GROWTH_TARGET_CHANNELS (comma-separated @usernames or ids)
#   GROWTH_DESTINATION_CHANNEL (your main proxy channel mention)
```

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_API_ID` | yes | — | API id from my.telegram.org |
| `TELEGRAM_API_HASH` | yes | — | API hash from my.telegram.org |
| `SESSION_STRING` | yes | — | Telethon StringSession (see step 2) |
| `GROWTH_TARGET_CHANNELS` | yes | — | Comma-separated targets |
| `GROWTH_DESTINATION_CHANNEL` | yes | — | Channel mention inserted into comments |
| `GROWTH_PER_CHANNEL_COOLDOWN` | no | `600` | Seconds between comments per target |
| `GROWTH_PER_CHANNEL_JITTER` | no | `120` | Random extra cooldown seconds |
| `GROWTH_DELAY_MIN` / `GROWTH_DELAY_MAX` | no | `5` / `20` | Human-like delay window (s) |
| `GROWTH_GLOBAL_WINDOW` | no | `3600` | Global rolling window (s) |
| `GROWTH_GLOBAL_MAX` | no | `12` | Max comments per global window |
| `GROWTH_STATE_FILE` | no | `data/state.json` | State persistence path |
| `GROWTH_LOG_LEVEL` | no | `INFO` | Logging verbosity |
| `GROWTH_DRY_RUN` | no | `0` | `1` = log instead of posting |

### 4. Run

```bash
# Smoke test (connects, resolves targets, exits; or use GROWTH_DRY_RUN=1 to watch)
python main.py --dry-run --once

# Production
python main.py
```

The account must **join** each target channel's discussion group — comments
are posted as a group member replying to the auto-forwarded channel post.

## Deployment

Any process supervisor works. Example systemd unit:

```ini
[Unit]
Description=ProxGram growth worker
After=network-online.target

[Service]
WorkingDirectory=/opt/proxgram_growth
EnvironmentFile=/opt/proxgram_growth/.env
ExecStart=/opt/proxgram_growth/.venv/bin/python main.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

State (`data/state.json`) makes restarts safe: the worker resumes cooldowns
and never re-comments posts it already processed.

## Compliance note

Automated commenting violates the spirit of Telegram's ToS when it becomes
spam. Keep comments on-topic, infrequent and valuable; respect channel admins;
stop immediately if they object. Run `GROWTH_DRY_RUN=1` for the first days.
