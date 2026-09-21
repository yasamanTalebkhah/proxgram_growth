import os
import json
import asyncio
import logging
from typing import Optional, Dict, Any
from telethon import TelegramClient
from telethon.errors import FloodWaitError, UserBannedInChannelError, ChatWriteForbiddenError
from src.database.connection import get_db_connection
from src.accounts.manager import AccountManager
from src.core.rate_limiter import AntiSpamLimiter
from src.core.templates import SpintaxEngine

logger = logging.getLogger(__name__)

class TaskDispatcher:
    def __init__(self, account_manager: Optional[AccountManager] = None, limiter: Optional[AntiSpamLimiter] = None):
        self.account_manager = account_manager or AccountManager()
        self.limiter = limiter or AntiSpamLimiter()

    def fetch_pending_task(self) -> Optional[Dict[str, Any]]:
        query = """
            SELECT id, target, action_type, payload, retry_count
            FROM tasks
            WHERE status = 'PENDING'
            ORDER BY priority DESC, id ASC
            LIMIT 1
            FOR UPDATE SKIP LOCKED;
        """
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(query)
                    row = cur.fetchone()
                    if row:
                        payload = row[3]
                        if isinstance(payload, str):
                            payload = json.loads(payload)
                        return {
                            "id": row[0],
                            "target": row[1],
                            "action_type": row[2],
                            "payload": payload,
                            "retry_count": row[4]
                        }
        except Exception as e:
            logger.error(f"Error fetching pending task: {e}")
        return None

    def update_task_status(self, task_id: int, status: str, error_message: Optional[str] = None):
        query = """
            UPDATE tasks
            SET status = %s,
                error_message = %s,
                retry_count = CASE WHEN %s = 'FAILED' THEN retry_count + 1 ELSE retry_count END,
                updated_at = NOW()
            WHERE id = %s;
        """
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(query, (status, error_message, status, task_id))
                conn.commit()
        except Exception as e:
            logger.error(f"Error updating task {task_id} status to {status}: {e}")

    async def execute_task(self, client: TelegramClient, task: Dict[str, Any]) -> bool:
        target = task["target"]
        action = task["action_type"]
        payload = task["payload"] or {}

        template = payload.get("template", "{سلام|درود} دوستان! جهت دریافت نرخ لحظه‌ای و پروکسی: {channel_link}")
        channel_link = payload.get("channel_link", "@proxgram")
        message_text = SpintaxEngine.render_promo(template, channel_link, payload.get("extra", {}))

        try:
            await self.limiter.wait_jitter()
            entity = await client.get_input_entity(target)

            if action == "SEND_MESSAGE":
                await client.send_message(entity, message_text)
            elif action == "COMMENT_REPLY":
                reply_to_id = payload.get("reply_to_msg_id")
                await client.send_message(entity, message_text, reply_to=reply_to_id)
            else:
                raise ValueError(f"Unsupported action type: {action}")

            logger.info(f"Task {task['id']} executed successfully on {target}")
            return True

        except FloodWaitError as fwe:
            logger.warning(f"FloodWait encountered on task {task['id']}: {fwe.seconds}s")
            await self.limiter.handle_flood_wait(fwe.seconds)
            raise
        except (UserBannedInChannelError, ChatWriteForbiddenError) as e:
            logger.error(f"Permission failure on task {task['id']} ({target}): {e}")
            raise
        except Exception as e:
            logger.error(f"Execution error on task {task['id']}: {e}")
            raise

    async def process_next_task(self) -> bool:
        if self.limiter.is_quiet_hours():
            logger.info("Quiet hours active. Task execution suspended.")
            return False

        task = self.fetch_pending_task()
        if not task:
            return False

        accounts = self.account_manager.get_active_accounts()
        if not accounts:
            logger.warning("No active accounts available for dispatching.")
            return False

        selected_account = accounts[0]
        acc_id = selected_account["id"]
        client = self.account_manager.create_client(selected_account["session_string"], selected_account["proxy"])

        self.update_task_status(task["id"], "RUNNING")

        try:
            await client.connect()
            if not await client.is_user_authorized():
                self.account_manager.update_account_status(acc_id, "RESTRICTED", failure_increment=True)
                self.update_task_status(task["id"], "PENDING", error_message="Selected account unauthorized")
                await client.disconnect()
                return False

            success = await self.execute_task(client, task)
            if success:
                self.update_task_status(task["id"], "COMPLETED")
            await client.disconnect()
            return success

        except FloodWaitError as fwe:
            self.update_task_status(task["id"], "PENDING", error_message=f"FloodWait: {fwe.seconds}s")
            await client.disconnect()
            return False
        except (UserBannedInChannelError, ChatWriteForbiddenError) as pe:
            self.update_task_status(task["id"], "FAILED", error_message=str(pe))
            await client.disconnect()
            return False
        except Exception as e:
            self.update_task_status(task["id"], "FAILED", error_message=str(e))
            self.account_manager.update_account_status(acc_id, "ACTIVE", failure_increment=True)
            await client.disconnect()
            return False
