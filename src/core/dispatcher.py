import os
import json
import asyncio
import logging
from typing import Optional, Dict, Any
from telethon import TelegramClient
from telethon.errors import FloodWaitError, UserBannedInChannelError, ChatWriteForbiddenError
from telethon.tl.types import PeerChannel, PeerChat
from src.database.connection import get_db_connection
from src.accounts.manager import AccountManager
from src.core.rate_limiter import AntiSpamLimiter
from src.core.templates import SpintaxEngine

logger = logging.getLogger(__name__)

class TaskDispatcher:
    def __init__(self, account_manager: Optional[AccountManager] = None, limiter: Optional[AntiSpamLimiter] = None):
        self.account_manager = account_manager or AccountManager()
        self.limiter = limiter or AntiSpamLimiter()

    def claim_next_task(self) -> Optional[Dict[str, Any]]:
        query = """
            WITH next_task AS (
                SELECT id
                FROM tasks
                WHERE status = 'PENDING'
                ORDER BY priority DESC, id ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE tasks
            SET status = 'RUNNING',
                updated_at = NOW()
            FROM next_task
            WHERE tasks.id = next_task.id
            RETURNING tasks.id, tasks.target, tasks.action_type, tasks.payload, tasks.retry_count;
        """
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(query)
                    row = cur.fetchone()
                # The claim UPDATE must be committed before the pooled
                # connection is returned, otherwise putconn() rolls the
                # RUNNING status back and the task can be claimed twice.
                conn.commit()
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
            logger.error(f"Error atomically claiming next task: {e}")
        return None

    def update_task_status(self, task_id: int, status: str, error_message: Optional[str] = None):
        query = """
            UPDATE tasks
            SET status = %s,
                error_message = %s,
                retry_count = CASE WHEN %s = 'FAILED' THEN retry_count + 1 ELSE retry_count END,
                executed_at = CASE WHEN %s = 'COMPLETED' THEN NOW() ELSE executed_at END,
                updated_at = NOW()
            WHERE id = %s;
        """
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(query, (status, error_message, status, status, task_id))
                conn.commit()
        except Exception as e:
            logger.error(f"Error updating task {task_id} status to {status}: {e}")

    def sweep_stale_tasks(self, timeout_minutes: Optional[int] = None, max_retries: int = 3) -> Dict[str, int]:
        """Recover tasks orphaned in RUNNING by a dead/interrupted worker.

        A RUNNING task whose updated_at is older than the claim timeout
        (GROWTH_CLAIM_TIMEOUT_MINUTES, default 10) can no longer be in
        flight: claims flip to a terminal status within seconds, and a
        worker that dies mid-send never reports back. Each stale claim is
        atomically requeued as PENDING — or marked FAILED once it has
        exhausted max_retries attempts — with a system_logs audit row.

        Returns {"recovered": <requeued>, "failed": <terminal>}.
        """
        if timeout_minutes is None:
            timeout_minutes = int(os.getenv("GROWTH_CLAIM_TIMEOUT_MINUTES", "10"))
        counts = {"recovered": 0, "failed": 0}
        query = """
            WITH stale AS (
                SELECT id
                FROM tasks
                WHERE status = 'RUNNING'
                  AND updated_at < NOW() - (%s * INTERVAL '1 minute')
                FOR UPDATE SKIP LOCKED
            )
            UPDATE tasks t
            SET status = CASE WHEN t.retry_count >= %s THEN 'FAILED' ELSE 'PENDING' END,
                error_message = CASE WHEN t.retry_count >= %s
                    THEN 'Claim timeout: orphaned RUNNING task exceeded max retries'
                    ELSE 'Claim timeout: orphaned RUNNING task recovered to PENDING' END,
                retry_count = t.retry_count + 1,
                updated_at = NOW()
            FROM stale s
            WHERE t.id = s.id
            RETURNING t.id, t.status, t.retry_count;
        """
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(query, (timeout_minutes, max_retries, max_retries))
                    rows = cur.fetchall()
                    for task_id, new_status, new_retry_count in rows:
                        counts["recovered" if new_status == "PENDING" else "failed"] += 1
                        outcome = ("marked FAILED (max retries exhausted)"
                                   if new_status == "FAILED" else "requeued as PENDING")
                        cur.execute(
                            "INSERT INTO system_logs (level, event_type, message, created_at) "
                            "VALUES (%s, %s, %s, NOW());",
                            ("WARNING", "STALE_TASK_RECOVERED",
                             f"Task #{task_id} orphaned in RUNNING beyond {timeout_minutes}m "
                             f"claim timeout -> {outcome} (retry_count={new_retry_count})"),
                        )
                conn.commit()
                if counts["recovered"] or counts["failed"]:
                    logger.warning(
                        f"Claim-timeout sweeper recovered {counts['recovered']} orphaned task(s), "
                        f"marked {counts['failed']} FAILED."
                    )
        except Exception as e:
            logger.error(f"Error sweeping stale tasks: {e}")
        return counts

    async def execute_task(self, client: TelegramClient, task: Dict[str, Any]) -> bool:
        target = task["target"]
        action = task["action_type"]
        payload = task["payload"] or {}

        template = payload.get("template", "{سلام|درود} دوستان! جهت دریافت نرخ لحظه‌ای و پروکسی: {channel_link}")
        channel_link = payload.get("channel_link", "@proxgram")
        message_text = SpintaxEngine.render_promo(template, channel_link, payload.get("extra", {}))

        await self.limiter.wait_jitter()
        entity = await self._resolve_entity(client, target)

        if action == "SEND_MESSAGE":
            await client.send_message(entity, message_text)
        elif action == "COMMENT_REPLY":
            reply_to_id = payload.get("reply_to_msg_id")
            await client.send_message(entity, message_text, reply_to=reply_to_id)
        else:
            raise ValueError(f"Unsupported action type: {action}")

        logger.info(f"Task {task['id']} executed successfully on {target}")
        return True

    @staticmethod
    def _parse_numeric_target(target: str):
        """Return (peer_type, id) for numeric/-100-marked targets, else None."""
        s = str(target).strip()
        try:
            if s.startswith("-100") and s[4:].isdigit():
                return "channel", int(s[4:])
            if s.lstrip("-").isdigit():
                return "chat", int(s)
            return None
        except ValueError:
            return None

    async def _resolve_entity(self, client: TelegramClient, target: str):
        """Resolve a target without depending on a warm session entity cache.

        Cold sessions cannot resolve marked channel ids via get_input_entity,
        but PeerChannel/PeerChat lookups work from member access alone.
        Falls back to the default path for @usernames and other forms.
        """
        parsed = self._parse_numeric_target(target)
        if parsed:
            kind, ident = parsed
            peer = PeerChannel(ident) if kind == "channel" else PeerChat(ident)
            try:
                return await client.get_entity(peer)
            except ValueError as exc:
                logger.warning(f"Peer-based resolution failed for {target}: {exc}")
        return await client.get_input_entity(target)

    async def process_next_task(self) -> bool:
        if self.limiter.is_quiet_hours():
            logger.info("Quiet hours active. Task execution suspended.")
            return False

        task = self.claim_next_task()
        if not task:
            return False

        accounts = self.account_manager.get_active_accounts()
        if not accounts:
            logger.warning("No active accounts available for dispatching.")
            self.update_task_status(task["id"], "PENDING", error_message="No active accounts available")
            return False

        selected_account = accounts[0]
        acc_id = selected_account["id"]
        client = self.account_manager.create_client(selected_account["session_string"], selected_account["proxy"])

        try:
            await self.account_manager.connect_with_fallback(client)
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

        except ConnectionError as ce:
            # All transports failed — an egress problem, not a task problem.
            # Requeue without burning retry_count so the task runs as soon as
            # the network path recovers.
            logger.warning(f"All transports failed on task {task['id']}: {ce}. Requeuing.")
            self.update_task_status(task["id"], "PENDING", error_message=f"All transports failed: {ce}")
            try:
                await client.disconnect()
            except Exception:
                pass
            return False
        except FloodWaitError as fwe:
            logger.warning(f"FloodWait encountered on task {task['id']}: {fwe.seconds}s. Requeuing task.")
            self.update_task_status(task["id"], "PENDING", error_message=f"FloodWait: {fwe.seconds}s")
            await client.disconnect()
            return False
        except (UserBannedInChannelError, ChatWriteForbiddenError) as pe:
            logger.error(f"Permission failure on task {task['id']} ({task['target']}): {pe}")
            self.update_task_status(task["id"], "FAILED", error_message=str(pe))
            await client.disconnect()
            return False
        except Exception as e:
            logger.error(f"Execution error on task {task['id']}: {e}")
            self.update_task_status(task["id"], "FAILED", error_message=str(e))
            self.account_manager.update_account_status(acc_id, "ACTIVE", failure_increment=True)
            await client.disconnect()
            return False
