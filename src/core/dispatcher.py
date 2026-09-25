import os
import json
import asyncio
import logging
from typing import Optional, Dict, Any
from telethon import TelegramClient, utils
from telethon.errors import FloodWaitError, UserBannedInChannelError, ChatWriteForbiddenError
from telethon.tl.functions.messages import GetDiscussionMessageRequest
from telethon.tl.types import PeerChannel, PeerChat
from src.database.connection import get_db_connection
from src.accounts.manager import AccountManager
from src.core.rate_limiter import AntiSpamLimiter
from src.core.templates import SpintaxEngine

logger = logging.getLogger(__name__)


class NoDiscussionGroupError(Exception):
    """Broadcast channel has no linked discussion group or comments are locked.

    Terminal condition: no amount of retrying will create a discussion
    thread, so these tasks are SKIPPED rather than requeued.
    """


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

    # Terminal statuses for the sweeper/requeue eligibility checks.
    TERMINAL_STATUSES = ("COMPLETED", "FAILED", "CANCELLED", "SKIPPED")

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

    def skip_task(self, task_id: int, reason: str):
        """Mark a task SKIPPED — terminal, no retry_count burn, no requeue.

        Used when a task can never succeed on retry (e.g. a broadcast
        channel without comments), unlike FAILED which the backoff
        requeue engine periodically returns to PENDING.
        """
        query = """
            UPDATE tasks
            SET status = 'SKIPPED',
                error_message = %s,
                executed_at = NOW(),
                updated_at = NOW()
            WHERE id = %s;
        """
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(query, (reason, task_id))
                    cur.execute(
                        "INSERT INTO system_logs (level, event_type, message, created_at) "
                        "VALUES ('INFO', 'TASK_SKIPPED', %s, NOW());",
                        (f"Task #{task_id} skipped: {reason}",),
                    )
                conn.commit()
        except Exception as e:
            logger.error(f"Error skipping task {task_id}: {e}")

    def retry_task(self, task_id: int, max_retries: Optional[int] = None) -> bool:
        """Manually requeue a FAILED task as PENDING (dashboard retry button).

        Unlike requeue_failed_tasks() there is no backoff wait — an operator
        who inspects a failure and presses retry wants it to run now. The
        attempt still respects GROWTH_MAX_RETRIES so a terminally-dead task
        is not resurrected forever. Returns True when the task was requeued.
        """
        if max_retries is None:
            max_retries = int(os.getenv("GROWTH_MAX_RETRIES", "3"))
        query = """
            UPDATE tasks
            SET status = 'PENDING',
                updated_at = NOW()
            WHERE id = %s
              AND status = 'FAILED'
              AND retry_count < %s;
        """
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(query, (task_id, max_retries))
                    requeued = cur.rowcount > 0
                    if requeued:
                        cur.execute(
                            "INSERT INTO system_logs (level, event_type, message, created_at) "
                            "VALUES ('INFO', 'TASK_REQUEUED', %s, NOW());",
                            (f"Task #{task_id} manually retried from dashboard "
                             f"(retry_count preserved)",),
                        )
                    conn.commit()
                    return requeued
        except Exception as e:
            logger.error(f"Error retrying task {task_id}: {e}")
            return False

    def sweep_stale_tasks(self, timeout_minutes: Optional[int] = None, max_retries: Optional[int] = None) -> Dict[str, int]:
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
        if max_retries is None:
            max_retries = int(os.getenv("GROWTH_MAX_RETRIES", "3"))
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

    async def _verify_delivery(self, client: TelegramClient, entity, task: Dict[str, Any], expected_id: int) -> Optional[int]:
        """Confirm a sent message is physically present in the target history.

        Fetches the most recent messages once and checks the expected
        telegram message id among them (it may not be the very latest if
        another post raced us). Strictly fail-safe: restricted entities,
        cold-session lookup failures or transient API errors degrade to
        'unverified' (None) with a WARNING — never an exception — so a
        read-restricted target can never crash the worker loop.
        """
        try:
            await asyncio.sleep(1)  # brief propagation grace before read-back
            recent = await client.get_messages(entity, limit=5)
            if any(getattr(m, "id", None) == expected_id for m in (recent or [])):
                return expected_id
            logger.warning(
                f"Delivery read-back: message {expected_id} not found in recent "
                f"history of {task['target']} (task {task['id']}) — unverified."
            )
            return None
        except Exception as exc:
            logger.warning(
                f"Delivery read-back unavailable for task {task['id']} "
                f"({task['target']}): {type(exc).__name__}: {exc}"
            )
            return None

    def _record_delivery(self, task_id: int, target: str, message_id: int):
        """Write a DELIVERY_VERIFIED audit row with the telegram message id."""
        query = """
            INSERT INTO system_logs (level, event_type, message, created_at)
            VALUES ('INFO', 'DELIVERY_VERIFIED', %s, NOW());
        """
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(query, (
                        f"Task #{task_id} delivered to {target}: "
                        f"telegram message_id={message_id} (read-back confirmed)",
                    ))
                conn.commit()
        except Exception as e:
            logger.error(f"Delivery audit log failure for task {task_id}: {e}")

    def requeue_failed_tasks(self, max_retries: Optional[int] = None, base_delay_seconds: Optional[int] = None) -> int:
        """Requeue transiently FAILED tasks once their backoff window elapses.

        Exponential backoff: base_delay * (2 ** retry_count), evaluated in
        SQL so host CLI and worker agree on timing to the second. Tasks that
        have already burned GROWTH_MAX_RETRIES attempts stay terminally
        FAILED; everything else returns to PENDING with its last error
        message preserved for context. One TASK_REQUEUED audit row per
        requeued task. Returns the number of requeued tasks.
        """
        if max_retries is None:
            max_retries = int(os.getenv("GROWTH_MAX_RETRIES", "3"))
        if base_delay_seconds is None:
            base_delay_seconds = int(os.getenv("GROWTH_REQUEUE_BASE_DELAY_SECONDS", "60"))
        query = """
            WITH eligible AS (
                SELECT id
                FROM tasks
                WHERE status = 'FAILED'
                  AND retry_count < %s
                  AND updated_at < NOW() - (%s * POWER(2, retry_count) * INTERVAL '1 second')
                FOR UPDATE SKIP LOCKED
            )
            UPDATE tasks t
            SET status = 'PENDING',
                updated_at = NOW()
            FROM eligible e
            WHERE t.id = e.id
            RETURNING t.id, t.retry_count;
        """
        requeued = 0
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(query, (max_retries, base_delay_seconds))
                    rows = cur.fetchall()
                    for task_id, retry_count in rows:
                        requeued += 1
                        delay = base_delay_seconds * (2 ** retry_count)
                        cur.execute(
                            "INSERT INTO system_logs (level, event_type, message, created_at) "
                            "VALUES (%s, %s, %s, NOW());",
                            ("INFO", "TASK_REQUEUED",
                             f"Task #{task_id} requeued after failure "
                             f"(retry_count={retry_count}, backoff={delay}s elapsed)"),
                        )
                conn.commit()
                if requeued:
                    logger.info(f"Requeued {requeued} failed task(s) after backoff.")
        except Exception as e:
            logger.error(f"Error requeuing failed tasks: {e}")
        return requeued

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
            read_entity = entity
            if self._is_broadcast_channel(entity):
                # The comment lands in the discussion group — read back
                # there, not on the broadcast channel itself.
                sent_id, read_entity = await self._comment_in_discussion(
                    client, entity, target, message_text, task
                )
            else:
                sent = await client.send_message(entity, message_text)
                sent_id = getattr(sent, "id", None)
        elif action == "COMMENT_REPLY":
            reply_to_id = payload.get("reply_to_msg_id")
            sent = await client.send_message(entity, message_text, reply_to=reply_to_id)
            sent_id = getattr(sent, "id", None)
        else:
            raise ValueError(f"Unsupported action type: {action}")

        if sent_id is not None:
            # For discussion comments the reply lands in the discussion
            # group, so read back there — not on the channel entity.
            confirmed = await self._verify_delivery(client, read_entity, task, sent_id)
            if confirmed is not None:
                self._record_delivery(task["id"], target, confirmed)

        logger.info(f"Task {task['id']} executed successfully on {target}")
        return True

    @staticmethod
    def _is_broadcast_channel(entity) -> bool:
        """True for broadcast Channel entities (posts-only, no typing/comments inline).

        Small chats/groups (Chat), users and megagroups are not broadcast
        channels; commenting on them goes through the normal send path.
        """
        return (
            getattr(entity, "__class__", None).__name__ == "Channel"
            and getattr(entity, "broadcast", False)
        )

    async def _comment_in_discussion(self, client: TelegramClient, channel_entity, target: str,
                                     message_text: str, task: Dict[str, Any]) -> Optional[int]:
        """Comment on the latest post of a broadcast channel via its discussion group.

        Broadcast channels reject direct user sends (CHAT_WRITE_FORBIDDEN /
        CHAT_ADMIN_REQUIRED), so growth comments must be posted as replies
        to the channel's most recent post inside the linked discussion
        group. GetDiscussionMessageRequest maps a channel post to its
        discussion-group message; the comment is sent TO THE DISCUSSION
        GROUP (never to the channel) with reply_to set to the thread
        origin message id — Telegram's comment-thread convention.

        Before sending, the account auto-joins the discussion group
        (JoinChannelRequest); UserAlreadyParticipantError is swallowed.
        A private/join-approval group, a write-forbidden group or a locked
        comment thread raises NoDiscussionGroupError so the caller marks
        the task SKIPPED — terminal, no retry loops on tasks that can
        never succeed.

        Returns (telegram message id of the posted comment — for read-back
        verification — and the discussion-group entity), or (None, group)
        when the id cannot be determined.
        """
        from telethon.errors import (
            ChannelPrivateError,
            ChatWriteForbiddenError,
            InviteRequestSentError,
            MsgIdInvalidError,
            UserAlreadyParticipantError,
            UserBannedInChannelError,
        )
        from telethon.tl.functions.channels import JoinChannelRequest

        posts = await client.get_messages(channel_entity, limit=1)
        post = posts[0] if posts else None
        if post is None or getattr(post, "service", False):
            raise NoDiscussionGroupError(f"Channel {target} has no posts to comment on")

        try:
            discussion = await client(GetDiscussionMessageRequest(
                peer=channel_entity,
                msg_id=post.id,
            ))
        except ChannelPrivateError as cpe:
            raise NoDiscussionGroupError(
                f"Channel {target} discussion is private or join-approval gated: {cpe}"
            )
        except MsgIdInvalidError as mii:
            # Telegram raises MSG_ID_INVALID for GetDiscussionMessageRequest
            # when the channel has NO linked discussion group (comments
            # disabled) — there is no thread to resolve. Terminal skip.
            raise NoDiscussionGroupError(
                f"Channel {target} post {post.id} has no comment thread "
                f"(comments disabled or no discussion linked): {mii}"
            )
        # Telegram answers with messages.DiscussionMessage. Resolve the
        # thread EXACTLY like Telethon's own comment support
        # (client._get_comment_data): every message in the response lives
        # in the DISCUSSION GROUP's id sequence, so the thread origin is
        # the lowest-id message, and the discussion chat is the entry in
        # discussion.chats matching that message's channel id. Taking
        # messages[0] blindly can select the channel-side post and send
        # the comment to the broadcast channel — which fails with
        # CHAT_ADMIN_REQUIRED.
        messages = list(getattr(discussion, "messages", None) or [])
        if not messages:
            raise NoDiscussionGroupError(
                f"Channel {target} post {post.id} has no linked discussion (comments disabled?)"
            )
        origin = min(messages, key=lambda m: getattr(m, "id", 0))
        origin_channel_id = getattr(getattr(origin, "peer_id", None), "channel_id", None)
        discussion_chat = next(
            (c for c in (getattr(discussion, "chats", None) or [])
             if getattr(c, "id", None) == origin_channel_id),
            None,
        )
        if discussion_chat is None:
            raise NoDiscussionGroupError(
                f"Channel {target} post {post.id} discussion group could not be resolved"
            )
        discussion_message_id = origin.id
        discussion_entity = utils.get_input_peer(discussion_chat)

        # Auto-join the discussion group so the account may comment. A
        # private/approval-gated group is a terminal skip, not a retry.
        try:
            await client(JoinChannelRequest(discussion_entity))
        except UserAlreadyParticipantError:
            pass
        except InviteRequestSentError as ire:
            # Approval-gated group: the join REQUEST was sent but an admin
            # must approve it — the account cannot comment now, and no
            # retry will change that until approval happens.
            raise NoDiscussionGroupError(
                f"Discussion group for {target} requires admin join approval "
                f"(request sent): {ire}"
            )
        except ChannelPrivateError as cpe:
            raise NoDiscussionGroupError(
                f"Discussion group for {target} is private or requires join approval: {cpe}"
            )
        # Account-level send restrictions surface the same way regardless
        # of which lock is hit — either the group forbids the write or the
        # account may not post there. Both are terminal for the task.
        try:
            from telethon.errors import ChatAdminRequiredError

            forbidden = (ChatWriteForbiddenError, UserBannedInChannelError,
                         ChatAdminRequiredError)
        except ImportError:  # very old Telethon
            forbidden = (ChatWriteForbiddenError, UserBannedInChannelError)
        try:
            sent = await client.send_message(
                discussion_entity,
                message_text,
                reply_to=discussion_message_id,
            )
        except forbidden as wpe:
            raise NoDiscussionGroupError(
                f"Comments locked or send forbidden in discussion group for {target}: {wpe}"
            )

        logger.info(
            f"Task {task['id']}: commented on post {post.id} of {target} in discussion "
            f"group (thread origin {discussion_message_id}, comment id {getattr(sent, 'id', '?')})"
        )
        return getattr(sent, "id", None), discussion_entity

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

        Username/t.me targets come back from get_input_entity as a BARE
        InputPeerChannel that carries no broadcast/megagroup flags — the
        dispatcher cannot tell a broadcast channel from a discussion group
        with it. Upgrade it to a full Channel entity via GetChannelsRequest
        so _is_broadcast_channel() sees the real flags.
        """
        from telethon.tl.functions.channels import GetChannelsRequest
        from telethon.tl.types import InputChannel, InputPeerChannel

        parsed = self._parse_numeric_target(target)
        if parsed:
            kind, ident = parsed
            peer = PeerChannel(ident) if kind == "channel" else PeerChat(ident)
            try:
                return await client.get_entity(peer)
            except ValueError as exc:
                logger.warning(f"Peer-based resolution failed for {target}: {exc}")
        entity = await client.get_input_entity(target)
        if isinstance(entity, InputPeerChannel):
            try:
                result = await client(GetChannelsRequest(id=[
                    InputChannel(entity.channel_id, entity.access_hash)
                ]))
                full = (getattr(result, "chats", None) or [None])[0]
                if full is not None:
                    return full
            except Exception as exc:
                logger.warning(
                    f"Could not upgrade InputPeerChannel to full Channel for "
                    f"{target}: {exc} — proceeding with bare peer"
                )
        return entity

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
        except NoDiscussionGroupError as nde:
            logger.warning(f"Task {task['id']} cannot be commented ({task['target']}): {nde}")
            self.skip_task(task["id"], str(nde))
            try:
                await client.disconnect()
            except Exception:
                pass
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
