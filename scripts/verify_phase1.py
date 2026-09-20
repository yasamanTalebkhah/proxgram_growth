import sys
import os

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.database.connection import get_db_connection
from src.core.redis_client import get_redis_client
from src.database.migrate import run_migrations

def test_database():
    print("[*] Testing PostgreSQL connection...")
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version();")
                version = cur.fetchone()[0]
                print(f"[+] PostgreSQL connected! Version: {version}")
        return True
    except Exception as e:
        print(f"[-] PostgreSQL connection failed: {e}")
        return False

def test_redis():
    print("[*] Testing Redis connection...")
    try:
        r = get_redis_client()
        r.set("phase1_test_key", "active", ex=30)
        val = r.get("phase1_test_key")
        if val == "active":
            print("[+] Redis connected and key write/read verified!")
            return True
        else:
            print("[-] Redis test key mismatch!")
            return False
    except Exception as e:
        print(f"[-] Redis connection failed: {e}")
        return False

def verify_tables():
    print("[*] Verifying created tables in DB...")
    expected_tables = {"accounts", "tasks", "system_logs"}
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT table_name 
                    FROM information_schema.tables 
                    WHERE table_schema = 'public';
                """)
                tables = {row[0] for row in cur.fetchall()}
                missing = expected_tables - tables
                if not missing:
                    print(f"[+] All expected tables verified: {expected_tables}")
                    return True
                else:
                    print(f"[-] Missing tables: {missing}")
                    return False
    except Exception as e:
        print(f"[-] Table verification failed: {e}")
        return False

def main():
    print("=== PROXGRAM GROWTH: PHASE 1 INTEGRATION TEST ===")
    
    # Run migration first
    if not run_migrations():
        print("[-] Migration step failed, aborting.")
        sys.exit(1)
        
    db_ok = test_database()
    redis_ok = test_redis()
    tables_ok = verify_tables()

    if db_ok and redis_ok and tables_ok:
        print("=== [SUCCESS] PHASE 1 INFRASTRUCTURE IS OPERATIONAL ===")
        sys.exit(0)
    else:
        print("=== [FAILURE] PHASE 1 CHECKS DID NOT PASS ===")
        sys.exit(1)

if __name__ == "__main__":
    main()
