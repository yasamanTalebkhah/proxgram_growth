# proxgram_growth

Standalone, fully modular Telegram growth engine powered by a Telethon
**client userbot**. It discovers target channels, dispatches comment /
message tasks through a PostgreSQL-backed queue, and enforces strict
anti-spam pacing so the driving accounts stay safe. All state lives in
PostgreSQL (SSOT); Redis is available for queues and distributed locks.

> **Security isolation:** this repository is deliberately decoupled from the
> main posting bot. It holds **no bot credentials** — only client-specific
> environment variables (`TELEGRAM_API_ID`, `TELEGRAM_API_HASH`,
> `SESSION_STRING`). A restriction on this userbot account can never touch
> the main bot.

## Architecture

```
src/
├── accounts/
│   └── manager.py          # Telethon AccountManager: DB-backed sessions, proxy binding
├── core/
│   ├── dispatcher.py       # TaskDispatcher: atomic task claiming, execution, requeue/fail
│   ├── worker.py           # GrowthWorker daemon: signal handling, circuit breaker, DB logs
│   ├── rate_limiter.py     # AntiSpamLimiter: jitter, quiet hours, FloodWait handling
│   ├── templates.py        # SpintaxEngine: rotating copy with variable interpolation
│   └── backoff.py          # ExponentialBackoff (full-jitter option)
├── database/
│   ├── connection.py       # psycopg2 connection pool
│   ├── migrate.py          # Schema migration runner
│   └── init_schema.sql     # accounts / tasks / system_logs tables
scripts/
├── generate_session.py     # One-time StringSession generator
├── import_account.py       # Validate + register a session into the accounts table
├── healthcheck_accounts.py # Authorization + SpamBot + restriction checks
├── verify_phase1.py        # End-to-end infra verification
├── metrics_reporter.py     # Task/account distributions, FloodWait & breaker counters
└── deploy.sh               # Zero-downtime deployment
```

## Safety model

| Layer | Mechanism |
|---|---|
| Isolation | Own repo, own process, userbot sessions only; no bot tokens anywhere |
| Atomic claiming | Tasks claimed via `FOR UPDATE SKIP LOCKED`, committed inside the transaction |
| Anti-spam pacing | Randomized jitter delays + configurable quiet-hours suspension |
| FloodWait | Wait honored + margin, task requeued (never lost), no worker blocking |
| Permission errors | `UserBannedInChannelError` / `ChatWriteForbiddenError` fail the task immediately |
| Circuit breaker | N consecutive loop failures halt the daemon and log `CIRCUIT_BREAKER_TRIPPED` |
| Account health | Authorization + SpamBot + restriction-flag checks; unhealthy accounts marked `RESTRICTED` |
| Credential hygiene | Secrets only via env/`.env`; `.dockerignore` keeps them out of images; state stays in the DB |

## Quick start

### 1. Install

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# fill in TELEGRAM_API_ID, TELEGRAM_API_HASH, DB/Redis URLs
```

### 3. Run the infrastructure

```bash
docker compose up -d postgres redis
docker compose exec -T postgres psql -U proxgram -d proxgram_growth -f - < src/database/init_schema.sql
python -m src.database.migrate            # or rely on the psql apply above
```

### 4. Register an account

```bash
python scripts/generate_session.py        # prints a StringSession once
python -m scripts.import_account --phone +98912... --session "<StringSession>"
python scripts/healthcheck_accounts.py    # verify authorization + SpamBot status
```

### 5. Run the worker

```bash
python src/core/worker.py                 # local
docker compose up -d --build worker       # production
```

## Testing & operations

```bash
pytest tests/ -v                          # 9 unit tests, faked boundaries, no network
python scripts/verify_phase1.py           # live infra integration check
python scripts/metrics_reporter.py        # operational metrics report
./scripts/deploy.sh                       # zero-downtime deploy on the VPS
```

CI runs the suite against real Postgres 16 + Redis 7 services on every push
(see `.github/workflows/ci.yml`). Operational procedures live in `RUNBOOK.md`.
