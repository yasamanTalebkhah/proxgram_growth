import os
import sys
import signal
import asyncio
import logging

# Ensure project root is in sys.path (needed when launched as a script,
# e.g. by the Dockerfile CMD: python src/core/worker.py)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from src.core.dispatcher import TaskDispatcher
from src.database.connection import get_db_connection

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("worker")

class GrowthWorker:
    def __init__(self, failure_threshold: int = 5):
        self.dispatcher = TaskDispatcher()
        self.is_running = True
        self.failure_threshold = failure_threshold
        self.consecutive_failures = 0

    def record_log(self, level: str, event_type: str, message: str):
        query = """
            INSERT INTO system_logs (level, event_type, message, created_at)
            VALUES (%s, %s, %s, NOW());
        """
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(query, (level, event_type, message))
                conn.commit()
        except Exception as e:
            logger.error(f"Database logging failure: {e}")

    def stop(self, signum=None, frame=None):
        logger.info("Shutdown signal received. Stopping worker gracefully...")
        self.is_running = False

    async def run(self):
        self.record_log("INFO", "WORKER_STARTED", "Growth daemon worker started successfully.")
        logger.info("Growth worker daemon initialized.")

        while self.is_running:
            try:
                processed = await self.dispatcher.process_next_task()
                if processed:
                    self.consecutive_failures = 0
                    self.record_log("INFO", "TASK_PROCESSED", "Task completed cleanly.")
                else:
                    await asyncio.sleep(5)
            except Exception as e:
                self.consecutive_failures += 1
                logger.error(f"Unhandled worker loop error: {e}")
                self.record_log("ERROR", "WORKER_ERROR", str(e))

                if self.consecutive_failures >= self.failure_threshold:
                    msg = f"Circuit breaker tripped: {self.consecutive_failures} consecutive failures. Halting worker."
                    logger.critical(msg)
                    self.record_log("CRITICAL", "CIRCUIT_BREAKER_TRIPPED", msg)
                    self.stop()
                    break

                await asyncio.sleep(5)

        self.record_log("INFO", "WORKER_STOPPED", "Growth daemon worker shutdown complete.")
        logger.info("Worker process terminated.")

if __name__ == "__main__":
    worker = GrowthWorker()
    loop = asyncio.get_event_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, worker.stop)
        except NotImplementedError:
            signal.signal(sig, worker.stop)

    loop.run_until_complete(worker.run())
