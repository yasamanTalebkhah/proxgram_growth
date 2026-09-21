import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.database.connection import get_db_connection

def generate_report():
    report = {}
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT status, COUNT(*)
                FROM tasks
                GROUP BY status;
            """)
            report["task_status"] = dict(cur.fetchall())

            cur.execute("""
                SELECT status, COUNT(*)
                FROM accounts
                GROUP BY status;
            """)
            report["account_status"] = dict(cur.fetchall())

            cur.execute("""
                SELECT COUNT(*)
                FROM system_logs
                WHERE event_type = 'CIRCUIT_BREAKER_TRIPPED';
            """)
            report["circuit_breaker_events"] = cur.fetchone()[0]

            cur.execute("""
                SELECT COUNT(*)
                FROM tasks
                WHERE error_message LIKE '%FloodWait%';
            """)
            report["flood_wait_tasks"] = cur.fetchone()[0]

    print("========================================")
    print("       PROXGRAM GROWTH METRICS          ")
    print("========================================")
    print("Tasks Distribution:")
    for status, count in report.get("task_status", {}).items():
        print(f"  - {status}: {count}")

    print("\nAccounts Distribution:")
    for status, count in report.get("account_status", {}).items():
        print(f"  - {status}: {count}")

    print("\nOperational Flags:")
    print(f"  - FloodWait Count: {report.get('flood_wait_tasks', 0)}")
    print(f"  - Circuit Breaker Trips: {report.get('circuit_breaker_events', 0)}")
    print("========================================")

if __name__ == "__main__":
    generate_report()
