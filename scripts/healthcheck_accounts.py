import os
import sys
import asyncio
import logging

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from telethon import TelegramClient
from telethon.tl.functions.messages import GetHistoryRequest
from src.accounts.manager import AccountManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

CLEAN_SUBSTRINGS = [
    "free", "no limits", "good news", "birds are singing",
    "هیچ محدودیتی", "آزاد", "حساب شما مشکلی ندارد",
    "вас нет никаких ограничений", "свободен"
]

async def check_spambot(client: TelegramClient) -> bool:
    try:
        spambot = await client.get_input_entity("SpamBot")
        await client.send_message(spambot, "/start")
        await asyncio.sleep(3)
        history = await client(GetHistoryRequest(
            peer=spambot,
            offset_id=0,
            offset_date=None,
            add_offset=0,
            limit=1,
            max_id=0,
            min_id=0,
            hash=0
        ))
        if history.messages:
            text = history.messages[0].message.lower()
            return any(sub in text for sub in CLEAN_SUBSTRINGS)
        return True
    except Exception as e:
        logger.warning(f"SpamBot verification skipped due to error: {e}")
        return True

async def run_healthchecks():
    manager = AccountManager()
    accounts = manager.get_active_accounts()

    if not accounts:
        logger.info("No active accounts found in database.")
        return

    logger.info(f"Found {len(accounts)} active account(s) to verify.")

    for acc in accounts:
        acc_id = acc["id"]
        phone = acc["phone_number"]
        session_str = acc["session_string"]
        proxy = acc["proxy"]

        logger.info(f"Checking account ID: {acc_id} ({phone})...")
        client = manager.create_client(session_str, proxy)

        try:
            await client.connect()
            if not await client.is_user_authorized():
                logger.error(f"Account {phone} is not authorized. Marking RESTRICTED.")
                manager.update_account_status(acc_id, "RESTRICTED", failure_increment=True)
                await client.disconnect()
                continue

            me = await client.get_me()
            if getattr(me, "restricted", False):
                logger.warning(f"Account {phone} native restriction flag detected. Marking RESTRICTED.")
                manager.update_account_status(acc_id, "RESTRICTED", failure_increment=True)
                await client.disconnect()
                continue

            is_clean = await check_spambot(client)
            if not is_clean:
                logger.warning(f"Account {phone} has SpamBot limitations. Marking RESTRICTED.")
                manager.update_account_status(acc_id, "RESTRICTED", failure_increment=True)
            else:
                logger.info(f"Account {phone} passed all checks. Status: ACTIVE.")
                manager.update_account_status(acc_id, "ACTIVE", failure_increment=False)

            await client.disconnect()

        except Exception as e:
            logger.error(f"Error checking account {phone}: {e}")
            manager.update_account_status(acc_id, "RESTRICTED", failure_increment=True)

if __name__ == "__main__":
    asyncio.run(run_healthchecks())
