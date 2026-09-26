"""Discovery Auto-Pilot — quota-driven crawl/validate loop.

Runs the discovery pipeline autonomously until `target_quota` channels
have been validated and promoted: keeps the PENDING_VALIDATION pool fed
(crawl when < POOL_FLOOR), validates in batches, and spaces every phase
with interruptible safety cooldowns. An operator stop signal breaks all
sleeps within 0.5s and finishes the run cleanly.

Integration notes (matches the real snapshot shapes):
  - pool_stats() -> {"total","pending","validated","disqualified","failed"}
  - run_crawl()  -> snapshot with "added" (newly inserted pending targets)
  - run_validation() -> {"counts": {"checked","validated","disqualified"},
                         "promoted": [handles validated this run], ...}
Both run_* helpers return {"ok": False, "detail": ...} when their engine
is already running from another entry point; the loop treats that as an
empty sweep and keeps pacing.
"""

import logging
import random
import threading
import time
from typing import Any, Dict, Optional

from src.services.discovery_crawler import run_crawl, pool_stats
from src.services.discussion_validator import run_validation

logger = logging.getLogger("discovery.autopilot")

INTER_BATCH_COOLDOWN_RANGE = (20, 35)
POST_CRAWL_COOLDOWN = 15.0
POOL_FLOOR = 10          # crawl when pending pool drops below this
EMPTY_SWEEP_LIMIT = 3    # give up after N crawls with zero new targets (pool empty)
IDLE_POLL_SECONDS = 2.0


class AutoPilotState:
    def __init__(self):
        self._lock = threading.Lock()
        self.running = False
        self.target_quota = 100
        self.promoted_count = 0
        self.total_checked = 0
        self.current_phase = "idle"  # idle, starting, crawling, validating, cooling_down, stopping, completed, stopped, error
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        self.last_error: Optional[str] = None
        self.stop_requested = False

    def start(self, quota: int):
        with self._lock:
            self.running = True
            self.target_quota = max(1, quota)
            self.promoted_count = 0
            self.total_checked = 0
            self.current_phase = "starting"
            self.start_time = time.time()
            self.end_time = None
            self.last_error = None
            self.stop_requested = False

    def request_stop(self):
        with self._lock:
            self.stop_requested = True
            self.current_phase = "stopping"

    def record_progress(self, validated_delta: int, promoted_delta: int, phase: str):
        with self._lock:
            self.total_checked += validated_delta
            self.promoted_count += promoted_delta
            self.current_phase = phase

    def set_phase(self, phase: str):
        with self._lock:
            self.current_phase = phase

    def finish(self, phase: str = "completed", error: Optional[str] = None):
        with self._lock:
            self.running = False
            self.current_phase = phase
            self.end_time = time.time()
            self.last_error = error

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            elapsed = 0.0
            if self.start_time:
                end = self.end_time or time.time()
                elapsed = round(end - self.start_time, 1)
            progress_pct = 0.0
            if self.target_quota > 0:
                progress_pct = min(100.0, round((self.promoted_count / self.target_quota) * 100, 1))

            return {
                "running": self.running,
                "target_quota": self.target_quota,
                "promoted_count": self.promoted_count,
                "total_checked": self.total_checked,
                "progress_percent": progress_pct,
                "current_phase": self.current_phase,
                "elapsed_seconds": elapsed,
                "stop_requested": self.stop_requested,
                "last_error": self.last_error,
            }


AUTOPILOT_STATE = AutoPilotState()
_autopilot_thread: Optional[threading.Thread] = None


def _interruptible_sleep(seconds: float) -> bool:
    """Sleep in 0.5s slices; False when a stop was requested mid-sleep."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if AUTOPILOT_STATE.stop_requested:
            return False
        time.sleep(0.5)
    return not AUTOPILOT_STATE.stop_requested


def run_autopilot_loop(target_quota: int = 100) -> Dict[str, Any]:
    AUTOPILOT_STATE.start(quota=target_quota)
    logger.info("Discovery Auto-Pilot started with target quota: %d", target_quota)

    consecutive_empty_sweeps = 0

    try:
        while AUTOPILOT_STATE.running:
            if AUTOPILOT_STATE.stop_requested:
                AUTOPILOT_STATE.finish(phase="stopped")
                logger.info("Discovery Auto-Pilot gracefully stopped by operator.")
                break

            if AUTOPILOT_STATE.promoted_count >= AUTOPILOT_STATE.target_quota:
                AUTOPILOT_STATE.finish(phase="completed")
                logger.info("Discovery Auto-Pilot achieved quota: %d promoted",
                            AUTOPILOT_STATE.promoted_count)
                break

            stats = pool_stats()
            pending_count = stats.get("pending", 0)  # real pool_stats() key

            if pending_count < POOL_FLOOR:
                AUTOPILOT_STATE.set_phase("crawling")
                logger.info("Pending pool low (%d targets). Triggering crawler sweep...", pending_count)
                crawl_res = run_crawl()

                new_targets = crawl_res.get("added", 0)  # real crawler snapshot key
                if new_targets == 0:
                    consecutive_empty_sweeps += 1
                else:
                    consecutive_empty_sweeps = 0

                if consecutive_empty_sweeps >= EMPTY_SWEEP_LIMIT and pending_count == 0:
                    AUTOPILOT_STATE.finish(
                        phase="completed",
                        error="No more new targets discovered after multiple sweeps.")
                    logger.warning("Auto-Pilot exhausted target discovery.")
                    break

                AUTOPILOT_STATE.set_phase("cooling_down")
                if not _interruptible_sleep(POST_CRAWL_COOLDOWN):
                    AUTOPILOT_STATE.finish(phase="stopped")
                    break

            if AUTOPILOT_STATE.stop_requested:
                AUTOPILOT_STATE.finish(phase="stopped")
                break

            AUTOPILOT_STATE.set_phase("validating")
            batch_limit = 25
            val_res = run_validation(limit=batch_limit)
            counts = val_res.get("counts", {}) or {}
            checked = counts.get("checked", 0)
            promoted = len(val_res.get("promoted", []) or [])
            AUTOPILOT_STATE.record_progress(validated_delta=checked,
                                            promoted_delta=promoted,
                                            phase="cooling_down")

            if AUTOPILOT_STATE.promoted_count >= AUTOPILOT_STATE.target_quota:
                AUTOPILOT_STATE.finish(phase="completed")
                break

            if checked > 0:
                cooldown = random.uniform(*INTER_BATCH_COOLDOWN_RANGE)
                logger.info("Inter-batch safety cooldown: %.1fs", cooldown)
                if not _interruptible_sleep(cooldown):
                    AUTOPILOT_STATE.finish(phase="stopped")
                    break
            else:
                # No progress this pass (validator busy elsewhere, no active
                # account, or empty pool) — brief interruptible idle poll so
                # the loop can never busy-spin on a failure mode.
                if not _interruptible_sleep(IDLE_POLL_SECONDS):
                    AUTOPILOT_STATE.finish(phase="stopped")
                    break

    except Exception as e:
        logger.exception("Unexpected error in Discovery Auto-Pilot loop: %s", e)
        AUTOPILOT_STATE.finish(phase="error", error=str(e))

    return AUTOPILOT_STATE.snapshot()


def start_background_autopilot(target_quota: int = 100) -> Dict[str, Any]:
    global _autopilot_thread
    if AUTOPILOT_STATE.running:
        return {"ok": False, "detail": "Auto-Pilot loop is already running"}

    _autopilot_thread = threading.Thread(
        target=run_autopilot_loop,
        kwargs={"target_quota": target_quota},
        daemon=True,
        name="DiscoveryAutoPilotThread"
    )
    _autopilot_thread.start()
    return {"ok": True, "detail": f"Auto-Pilot loop initiated with target of {target_quota} channels"}


def stop_background_autopilot() -> Dict[str, Any]:
    if not AUTOPILOT_STATE.running:
        return {"ok": False, "detail": "Auto-Pilot is not currently running"}
    AUTOPILOT_STATE.request_stop()
    return {"ok": True, "detail": "Stop signal transmitted to Auto-Pilot"}


def autopilot_status() -> Dict[str, Any]:
    return AUTOPILOT_STATE.snapshot()
