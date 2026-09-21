import asyncio
import random
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

class AntiSpamLimiter:
    def __init__(self, min_delay: int = 30, max_delay: int = 90, quiet_start: int = 1, quiet_end: int = 6):
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.quiet_start = quiet_start
        self.quiet_end = quiet_end

    def is_quiet_hours(self) -> bool:
        current_hour = datetime.now().hour
        if self.quiet_start <= self.quiet_end:
            return self.quiet_start <= current_hour < self.quiet_end
        return current_hour >= self.quiet_start or current_hour < self.quiet_end

    async def wait_jitter(self):
        delay = random.uniform(self.min_delay, self.max_delay)
        logger.info(f"Applying anti-spam jitter delay: {delay:.2f}s")
        await asyncio.sleep(delay)

    async def handle_flood_wait(self, seconds: int):
        logger.warning(f"Telegram FloodWait triggered: waiting {seconds}s")
        await asyncio.sleep(seconds + random.uniform(5, 15))
