#!/usr/bin/env bash
# Smoke-seed the stack after a fresh deploy: one mock account + one PENDING
# task, verify the queue, report metrics, then clean the test rows up.
#
# Run ONLY with the worker stopped (docker compose stop worker) so the mock
# session is never used by the real dispatcher.
set -euo pipefail

command -v docker >/dev/null 2>&1 || { echo "[-] docker not found — run this on the VPS"; exit 1; }
docker info >/dev/null 2>&1 || { echo "[-] docker daemon not running"; exit 1; }

PG_USER="${POSTGRES_USER:-proxgram}"
PG_DB="${POSTGRES_DB:-proxgram_growth}"

echo "[*] Seeding mock account + smoke-test task into ${PG_DB}..."
docker compose exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1 <<'SQL'
INSERT INTO accounts (phone_number, session_string, status, failure_count)
VALUES ('+10000000000', 'MOCK_SESSION_FOR_SMOKE_TEST', 'ACTIVE', 0)
ON CONFLICT (phone_number) DO NOTHING;

INSERT INTO tasks (target, action_type, payload, status)
VALUES ('@test_channel', 'SEND_MESSAGE',
        '{"template": "Smoke test message", "channel_link": "https://t.me/test"}',
        'PENDING');
SQL

echo "[*] Pending tasks in queue:"
docker compose exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -c \
  "SELECT id, target, action_type, status FROM tasks WHERE status = 'PENDING';"

echo "[*] Metrics report:"
python scripts/metrics_reporter.py

echo "[*] Cleaning up smoke-test rows..."
docker compose exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1 <<'SQL'
DELETE FROM tasks WHERE target = '@test_channel' AND payload->>'template' = 'Smoke test message';
DELETE FROM accounts WHERE phone_number = '+10000000000';
SQL

echo "[+] Smoke test complete."
