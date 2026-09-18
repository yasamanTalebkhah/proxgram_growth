# ProxGram Growth Worker

Independent, Userbot-based subsystem (Python + Telethon) that monitors
designated public channels and posts timely, non-spam comments on their
discussion threads to route readers to the main proxy channel.

**Runs as a separate process from the main posting bot.** It authenticates
with its own user account session (`SESSION_STRING`), never the main bot
token — so a restriction on this account cannot touch the main bot.

## Safety model

| Layer | Mechanism |
|---|---|
| Isolation | Own process, own userbot session, own env vars |
| Human-like pacing | 5–20 s randomized delay before engaging a fresh post |
| Per-target cooldown | Max 1 comment per target channel per 10 min (600 s) + jitter |
| Global cap | Max 12 comments per rolling hour across all targets |
| Template rotation | Randomized wording; never the same template twice in a row per channel |
| FloodWait | Honors Telegram's wait + margin, with exponential backoff & full jitter |
| Permission errors | `ChatWriteForbidden` / `ChannelPrivate` give up immediately (no retry hammering) |
| Crash safety | Event handler never raises; pending tasks cancelled on shutdown |
| Credential hygiene | `SESSION_STRING` / `API_HASH` scrubbed from every log line; state file stores only timestamps |
| Persistence | Cooldowns survive restarts via `var/growth_state.json` |

## Layout

```
proxgram_growth/
  config.py          # schema, env/JSON loading, validation
  worker.py          # Telethon engine: monitor -> classify -> comment
  rate_limit.py      # per-target pacer, global rolling cap, human delay
  backoff.py         # exponential backoff, FloodWait handling
  templates.py       # template rendering + rotation
  state.py           # restart-safe cooldown persistence
  logging_setup.py   # logging with credential redaction
scripts/
  growth_worker.py   # standalone runner (--dry-run, --once)
  growth_session.py  # one-time StringSession generator
config/
  growth_worker.sample.json
  growth_templates.sample.json
tests/               # 66 unit tests (no network required)
```

## Setup

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt   # (Linux: .venv/bin/pip)

# 1. Generate a session ONCE, on a trusted machine, for a DEDICATED account:
.venv/Scripts/python scripts/growth_session.py

# 2. Configure (secrets via env, never CLI args):
cp .env.growth.example .env.growth   # fill in API_ID / API_HASH / SESSION_STRING

# 3. Smoke-test without posting:
.venv/Scripts/python scripts/growth_worker.py --dry-run --once

# 4. Run for real:
.venv/Scripts/python scripts/growth_worker.py
```

The account must **join** each target channel's discussion group (comments
are posted as a group member replying to the auto-forwarded channel post).

## Tuning cadence

- `GROWTH_PER_CHANNEL_COOLDOWN=600` — raise to 1800+ for cautious operation.
- `GROWTH_GLOBAL_MAX=12` per hour is already conservative; do not raise it
  above ~20 without long-term testing.
- `GROWTH_DRY_RUN=1` for the first days: the worker logs everything it
  *would* post without sending anything.

## Compliance note

Automated commenting is against the spirit of Telegram's ToS if it becomes
spam. Keep comments on-topic, infrequent and valuable; prefer replying where
relevant over advertising; stop immediately if channel admins object.
