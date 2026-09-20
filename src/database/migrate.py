"""Database migration runner (Phase 1 — Foundation & Persistence).

Run from the repository root:  python -m src.database.migrate
"""

import os

from src.database.connection import get_db_connection


def run_migrations():
    schema_path = os.path.join(os.path.dirname(__file__), "init_schema.sql")
    if not os.path.exists(schema_path):
        print(f"[!] Schema file not found at: {schema_path}")
        return False

    with open(schema_path, "r", encoding="utf-8") as f:
        schema_sql = f.read()

    print("[*] Applying database schema migrations...")
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(schema_sql)
            conn.commit()
        print("[+] Migrations executed successfully!")
        return True
    except Exception as e:
        print(f"[-] Migration failed: {e}")
        return False


if __name__ == "__main__":
    run_migrations()
