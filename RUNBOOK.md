# Proxgram Growth Engine — Operations Runbook

## Core Workflows

### 1. Service Management
- Start services: `docker compose up -d`
- Stop services: `docker compose down`
- View worker logs: `docker compose logs -f worker`
- Restart only the worker: `docker compose up -d --no-deps --force-recreate worker`

### 2. Metrics & Observability
- Run live metrics check: `python scripts/metrics_reporter.py`
- Inspect system logs via database:
```sql
SELECT created_at, level, event_type, message
FROM system_logs
ORDER BY created_at DESC
LIMIT 50;
```
- Task pipeline health:
```sql
SELECT status, COUNT(*) FROM tasks GROUP BY status ORDER BY 2 DESC;
```
- Account fleet health:
```sql
SELECT status, COUNT(*) FROM accounts GROUP BY status;
```

### 3. Deployment
- Standard release (on the VPS): `./scripts/deploy.sh`
  - Pulls `main`, ensures Postgres/Redis are healthy, re-applies the idempotent
    schema, rebuilds and recreates **only** the worker (Postgres/Redis keep running).
- Rollback: check out the previous tag/commit, then rebuild:
```bash
git checkout <previous-commit>
docker compose build worker && docker compose up -d --no-deps worker
```

### 4. First-Time Server Setup
1. Clone the repository and `cp .env.example .env`.
2. Fill `.env` — `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, DB/Redis URLs.
   **Never commit `.env`; it is gitignored and excluded from images via `.dockerignore`.**
3. Bring the stack up: `docker compose up -d postgres redis`.
4. Apply the schema: `docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f - < src/database/init_schema.sql`
5. Import accounts: `python -m scripts.import_account --phone +98... --session "<StringSession>"`
6. Start the worker: `docker compose up -d --build worker`.

### 5. Smoke test after deploy
With the worker **stopped**: `docker compose stop worker && ./scripts/smoke_seed.sh`
— seeds one mock account + one PENDING task, prints the queue and the
metrics report, then removes the test rows. Re-start the worker afterwards.

### Circuit breaker tripped (`CIRCUIT_BREAKER_TRIPPED` in logs)
1. Check `system_logs` for the last `WORKER_ERROR` messages to find the root cause.
2. Most common causes: database unreachable, Telethon auth failures across all accounts.
3. After fixing the cause: `docker compose up -d --no-deps --force-recreate worker`
   (the breaker state is in-process, so a restart resets it).

### FloodWait storm (`flood_wait_tasks` climbing)
1. Pause intake: stop the worker (`docker compose stop worker`) — tasks stay queued.
2. Check which targets generate the waits; raise per-task spacing in payloads.
3. Restart with `docker compose up -d --no-deps worker`. Requeued tasks resume automatically.

### Account marked RESTRICTED
1. Run the healthcheck: `python scripts/healthcheck_accounts.py`.
2. If the session is dead, re-import: `python -m scripts.import_account ...`.
3. If SpamBot reports limitations, retire the account — do not force-reactivate.

### Postgres/Redis down
- `docker compose ps` → container state; `docker compose logs postgres|redis`.
- Data lives in named volumes `postgres_data` / `redis_data`; verify with
  `docker volume ls` before any `down -v` (never run `down -v` in production).

## Backups
- Nightly logical dump (cron on the VPS):
```bash
docker compose exec -T postgres pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip > backups/pg_$(date +%F).sql.gz
```
- Session strings are credentials: the `accounts` table is sensitive — restrict DB
  access, and include the backup directory in off-machine secret storage policy.
