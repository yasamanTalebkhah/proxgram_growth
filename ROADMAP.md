# Proxgram Growth Engine — Architectural Roadmap & Master Plan

## 1. System Overview & Architecture
The proxgram_growth subsystem is an isolated, automated growth engine built to funnel audience and organic traffic into the primary channel (بازار ارز و پروکسی آزاد). It operates as a set of autonomous Telegram Userbots running under strict anti-spam guidelines to guarantee account safety and eliminate service disruption on the main channel.

Architecture Flow:
+--------------------------------------------------------------+
|                    proxgram_growth Engine                    |
+--------------------------------------------------------------+
|                                                              |
|   +-------------------+              +-------------------+   |
|   |  Account Manager  |              |  Task Dispatcher  |   |
|   |  (Telethon/Auth)  |              |  (Worker / Queue) |   |
|   +---------+---------+              +---------+---------+   |
|             |                                  |             |
|             +----------------+-----------------+             |
|                              |                               |
|               +--------------v---------------+               |
|               |  Anti-Spam & Rate Limiter    |               |
|               |  - FloodWait Handling        |               |
|               |  - Template Rotating         |               |
|               |  - Quiet Hours Window        |               |
|               +--------------+---------------+               |
|                              |                               |
|             +----------------+----------------+              |
|             |                                 |              |
|   +---------v---------+             +---------v---------+    |
|   |    PostgreSQL     |             |       Redis       |    |
|   | (SSOT: Metadata,  |             | (Distributed Lock,|    |
|   | Accounts, Logs)   |             | Queues & Caching) |    |
|   +-------------------+             +-------------------+    |
+--------------------------------------------------------------+

---

## 2. Phased Roadmap

### Phase 1: Infrastructure, Storage & Connection Pool [CURRENT]
- Deliverables:
  - Standardized Docker Compose stack hosting PostgreSQL 16 and Redis 7.
  - Initial relational schema (src/database/init_schema.sql) covering:
    - accounts: Session strings, proxy assignments, phone numbers, state (ACTIVE, RESTRICTED, BANNED).
    - tasks: Target channels/groups, execution state, schedules, retry counters.
    - system_logs: Level, event types, error tracebacks, execution audits.
  - Robust database connection pooling (src/database/connection.py) via psycopg2.
  - Redis client abstraction (src/core/redis_client.py) for queue management and locks.
  - Automated migration runner (src/database/migrate.py) and healthcheck integration script (scripts/verify_phase1.py).
- Success Criteria:
  - Automated integration tests cleanly passing database connectivity, Redis read/write, and table verification.

---

### Phase 2: Telethon Session Management & Account Orchestration
- Deliverables:
  - Multi-session abstraction layer via Telethon (src/accounts/manager.py).
  - Secure credential storage (string sessions) with automatic state updates.
  - Proxy binding per account (SOCKS5/HTTP/MTProto support).
  - Healthcheck routine to verify account statuses (is_user_authorized(), detection of SpamBot bans).
  - CLI script to import, validate, and activate sessions (scripts/import_account.py).
- Success Criteria:
  - Zero-touch session restoration from DB, successful authorization validation without credential leakage.

---

### Phase 3: Task Dispatcher, Templating & Anti-Spam Logic
- Deliverables:
  - Priority task queue worker driven by Redis (src/core/dispatcher.py).
  - Spintax/Template engine for rotating promotional copy (src/core/templates.py).
  - Resilient rate limiter enforcing:
    - Per-account cooldowns.
    - Global quiet hours enforcement (e.g., night hours suspension).
    - Jitter delays (randomized interval execution).
    - Intelligent FloodWait backoff and task reassignment.
  - Target scanner: Safe resolution of discussion groups and target post IDs.
- Success Criteria:
  - Graceful handling of Telegram rate limits without account bans or unhandled exceptions.

---

### Phase 4: Production Deployment, Monitoring & Observability
- Deliverables:
  - Long-running worker daemon orchestration via systemd / Docker service definitions.
  - Structured logging pipeline streaming events into PostgreSQL system_logs and standard out.
  - Operational metrics tracking:
    - Conversion and target completion rates.
    - FloodWait occurrences per session.
    - Proxy latency and failure metrics.
  - Emergency circuit breaker: Automatic fleet shutdown upon elevated Telegram error rates.
- Success Criteria:
  - Stable 24/7 background execution with automated alerting and minimal manual intervention.

---

## 3. Directory Layout
proxgram_growth/
├── docker-compose.yml
├── requirements.txt
├── .env.example
├── ROADMAP.md
├── scripts/
│   └── verify_phase1.py
└── src/
    ├── __init__.py
    ├── core/
    │   ├── __init__.py
    │   └── redis_client.py
    └── database/
        ├── __init__.py
        ├── connection.py
        ├── init_schema.sql
        └── migrate.py
