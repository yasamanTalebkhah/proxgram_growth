"""Database connection pool (Phase 1 — Foundation & Persistence)."""

import os

import psycopg2
from psycopg2 import pool
from contextlib import contextmanager
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres_secure_password@localhost:5432/proxgram_db",
)

_db_pool = None


def get_pool():
    global _db_pool
    if _db_pool is None:
        _db_pool = psycopg2.pool.SimpleConnectionPool(
            minconn=1,
            maxconn=10,
            dsn=DATABASE_URL
        )
    return _db_pool


@contextmanager
def get_db_connection():
    pool_instance = get_pool()
    conn = pool_instance.getconn()
    try:
        yield conn
    finally:
        pool_instance.putconn(conn)
