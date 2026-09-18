"""ProxGram Growth — Telethon userbot entry point.

Monitors target channels for new posts and posts a randomized, non-spam
comment on the linked discussion thread pointing to the destination
channel — guarded by per-target cooldowns, a global comment cap, human-like
delays, FloodWait-aware exponential backoff and persistent processed-post
tracking.

This process is fully isolated from the main posting bot: it authenticates
with its own user session (SESSION_STRING) and never touches bot tokens.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import signal
import time
from dataclasses import dataclass, field
from typing import Any

from telethon import TelegramClient, errors, events
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon import utils as tl_utils

from backoff import BackoffPolicy
from config import Config, TargetConfig, load_config
from logging_setup import configure_logging
from rate_limit import GlobalPacer, HumanDelay, PerTargetPacer
from state_manager import StateManager
from templates import SPEED_NOTES, render_random_with_template

logger = logging.getLogger("proxgram.growth")

MAX_TRACKED_ALBUMS = 256
MAX_ATTEMPTS = 5


@dataclass
class WorkerStats:
    posts_seen: int = 0
    comments_posted: int = 0
    comments_skipped_cooldown: int = 0
    comments_skipped_processed: int = 0
    comments_skipped_global: int = 0
    comments_failed: int = 0
    flood_waits: int = 0


@dataclass
class PendingComment:
    """A comment scheduled for a channel post's discussion thread."""

    target: TargetConfig
    channel: str
    discussion_entity: Any
    discussion_id: int
    reply_to_msg_id: int
    post_id: int
    context: dict[str, Any] = field(default_factory=dict)


def entity_id(entity: Any) -> int | None:
    """Best-effort extraction of a raw id from a Telethon entity."""
    for attr in ("id", "channel_id", "chat_id"):
        value = getattr(entity, attr, None)
        if isinstance(value, int):
            return value
    return None


class GrowthWorker:
    """Telethon-based automated commenter for public channel discussions."""

    def __init__(
        self,
        config: Config,
        *,
        client: Any = None,
        rng: random.Random | None = None,
        state: StateManager | None = None,
        sleep: Any = None,
        clock: Any = None,
        comment_poster: Any = None,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        self.config = config
        self.rng = rng or random.Random()
        self.sleep = sleep or asyncio.sleep
        self.clock = clock or time.time
        self.state = state or StateManager(config.state_file)
        self.pacer = PerTargetPacer(rng=self.rng)
        self.global_pacer = GlobalPacer(
            max_comments=config.global_max_comments,
            window_seconds=config.global_window,
            rng=self.rng,
        )
        self.backoff = BackoffPolicy(
            base=config.backoff_base,
            factor=config.backoff_factor,
            max_seconds=config.backoff_max,
            rng=self.rng,
        )
        self.max_attempts = max_attempts
        self._comment_poster = comment_poster
        self.client = client
        # marked channel id -> marked discussion group id
        self.discussions: dict[int, int] = {}
        # marked discussion group id -> chat entity (for sending comments)
        self.discussion_entities: dict[int, Any] = {}
        # target channel string -> marked channel id
        self._channel_ids: dict[str, int] = {}
        self.stats = WorkerStats()
        self._stop_event = asyncio.Event()
        self._pending: dict[int, asyncio.Task] = {}
        self._pending_by_target: dict[str, int] = {}
        self._last_comment: dict[str, str] = {}
        self._seen_albums: list[Any] = []

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def run(self) -> None:
        """Connect, register handlers, and run until stop is requested."""
        if self.client is None:
            self.client = TelegramClient(
                StringSession(self.config.session_string),
                self.config.api_id,
                self.config.api_hash,
            )
        await self.client.connect()
        if not await self.client.is_user_authorized():
            logger.error(
                "Session is not authorized; refusing to start. "
                "Regenerate SESSION_STRING via scripts/generate_session.py."
            )
            return
        me = await self.client.get_me()
        logger.info("Growth worker online (user id=%s)", getattr(me, "id", "?"))

        await self._resolve_discussions()
        if not self.discussions:
            logger.error("No resolvable target channels; nothing to monitor.")
            await self._shutdown()
            return

        chats = sorted({*self.discussions.keys(), *self.discussions.values()})
        self.client.add_event_handler(self._on_new_message, events.NewMessage(chats=chats))

        self._install_signal_handlers()
        logger.info(
            "Monitoring %d target(s); destination=%s; dry_run=%s",
            len(self.config.targets),
            self.config.destination_channel,
            self.config.dry_run,
        )
        try:
            await self._stop_event.wait()
        finally:
            await self._shutdown()

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        if os.name == "nt":  # pragma: no cover - platform-specific branch
            return  # KeyboardInterrupt propagates naturally on Windows.
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request_stop)
            except NotImplementedError:  # pragma: no cover
                pass

    def request_stop(self) -> None:
        """Request graceful shutdown (idempotent)."""
        self._stop_event.set()

    async def _shutdown(self) -> None:
        for task in list(self._pending.values()):
            task.cancel()
        if self._pending:
            await asyncio.gather(*self._pending.values(), return_exceptions=True)
        self._pending.clear()
        self.state.save()
        if self.client is not None and getattr(self.client, "is_connected", lambda: False)():
            try:
                await self.client.disconnect()
            except Exception:  # noqa: BLE001 - shutdown must never raise
                logger.warning("Error while disconnecting client", exc_info=True)
        logger.info(
            "Growth worker stopped. seen=%d posted=%d cooldown_skips=%d "
            "processed_skips=%d global_skips=%d failed=%d flood_waits=%d",
            self.stats.posts_seen,
            self.stats.comments_posted,
            self.stats.comments_skipped_cooldown,
            self.stats.comments_skipped_processed,
            self.stats.comments_skipped_global,
            self.stats.comments_failed,
            self.stats.flood_waits,
        )

    # ------------------------------------------------------------------ #
    # Telegram plumbing
    # ------------------------------------------------------------------ #

    async def _resolve_discussions(self) -> None:
        """Resolve each target channel and its linked discussion group."""
        for target in self.config.targets:
            try:
                entity = await self.client.get_entity(target.channel)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Cannot resolve target %s: %s", target.channel, type(exc).__name__)
                continue
            marked = tl_utils.get_peer_id(entity)
            self._channel_ids[target.channel] = marked
            linked = await self._linked_chat(entity)
            if linked is None:
                logger.warning(
                    "Target %s has no resolvable linked discussion group; "
                    "comments unavailable for it",
                    target.channel,
                )
                continue
            linked_marked = tl_utils.get_peer_id(linked)
            self.discussions[marked] = linked_marked
            self.discussion_entities[linked_marked] = linked
            logger.info(
                "Target %s (id=%s) -> discussion %s", target.channel, marked, linked_marked
            )

    async def _linked_chat(self, entity: Any) -> Any | None:
        """Return the linked discussion chat entity for a broadcast channel."""
        try:
            full = await self.client(GetFullChannelRequest(channel=entity))
        except errors.RPCError:
            return None
        full_chat = getattr(full, "full_chat", full)
        linked_id = getattr(full_chat, "linked_chat_id", None)
        if not linked_id:
            return None
        for chat in getattr(full, "chats", ()) or ():
            if getattr(chat, "id", None) == linked_id:
                return chat
        return None

    # ------------------------------------------------------------------ #
    # Event handling
    # ------------------------------------------------------------------ #

    async def _on_new_message(self, event: Any) -> None:
        """Telethon entry point; never lets exceptions escape."""
        try:
            await self._handle_event(event)
        except Exception:  # noqa: BLE001
            logger.exception("Unhandled error in growth event handler")

    async def _handle_event(self, event: Any) -> None:
        if getattr(event, "out", False):
            return
        message = getattr(event, "message", None)
        if message is None or getattr(message, "action", None) is not None:
            return

        chat_id = _event_chat_id(event)
        if chat_id is None:
            return

        target, discussion_entity, discussion_id, reply_to = self._classify(chat_id, message)
        if target is None:
            return

        grouped = getattr(message, "grouped_id", None)
        if grouped is not None and self._recently_seen_album(grouped):
            return  # Only the first item of an album triggers a comment.

        self.stats.posts_seen += 1

        # Idempotency: never handle the same channel/post id twice
        # (protects against Telegram redelivering updates after reconnects).
        if self.state.is_post_processed(target.channel, message.id):
            self.stats.comments_skipped_processed += 1
            logger.debug("Post %s in %s already processed; skipping", message.id, target.channel)
            return

        if self._pending_by_target.get(target.channel, 0) > 0:
            self.stats.comments_skipped_cooldown += 1
            logger.info("Comment already pending for %s; skipping", target.channel)
            return

        self.state.mark_post_processed(target.channel, message.id)
        self._spawn_comment_task(
            PendingComment(
                target=target,
                channel=target.channel,
                discussion_entity=discussion_entity,
                discussion_id=discussion_id,
                reply_to_msg_id=reply_to,
                post_id=message.id,
                context=self._build_context(),
            )
        )

    def _classify(
        self, chat_id: int, message: Any
    ) -> tuple[TargetConfig | None, Any, int, int]:
        """Map an incoming message to (target, discussion entity, id, reply_to).

        Returns (None, None, 0, 0) when the message is not a triggerable post.
        """
        # Case 1: a freshly published post in the broadcast channel itself.
        if chat_id in self.discussions:
            if not getattr(message, "post", False):
                return None, None, 0, 0
            target = self._target_for_channel_id(chat_id)
            discussion_id = self.discussions.get(chat_id, 0)
            return target, self.discussion_entities.get(discussion_id), discussion_id, message.id

        # Case 2: the auto-forward of a channel post inside the linked
        # discussion group (this is the thread root).
        if chat_id in set(self.discussions.values()):
            fwd = getattr(message, "fwd_from", None)
            if fwd is None and not getattr(message, "post", False):
                return None, None, 0, 0  # ordinary group chatter — ignore
            target = self._target_for_discussion(chat_id)
            reply_header = getattr(message, "reply_to", None)
            reply_to = getattr(reply_header, "reply_to_msg_id", None) or message.id
            return target, self.discussion_entities.get(chat_id), chat_id, int(reply_to)

        return None, None, 0, 0

    def _recently_seen_album(self, grouped_id: Any) -> bool:
        if grouped_id in self._seen_albums:
            return True
        self._seen_albums.append(grouped_id)
        if len(self._seen_albums) > MAX_TRACKED_ALBUMS:
            del self._seen_albums[: len(self._seen_albums) // 2]
        return False

    def _target_for_channel_id(self, channel_id: int) -> TargetConfig | None:
        for channel, cid in self._channel_ids.items():
            if cid == channel_id:
                return self.config.target_for(channel)
        return None

    def _target_for_discussion(self, discussion_id: int) -> TargetConfig | None:
        for cid, did in self.discussions.items():
            if did == discussion_id:
                return self._target_for_channel_id(cid)
        return None

    def _build_context(self) -> dict[str, Any]:
        return {
            "channel": self.config.destination_channel,
            "proxy_count": self.rng.randint(18, 64),
            "speed_note": self.rng.choice(SPEED_NOTES),
        }

    # ------------------------------------------------------------------ #
    # Comment pipeline
    # ------------------------------------------------------------------ #

    def _spawn_comment_task(self, pending: PendingComment) -> None:
        task = asyncio.get_running_loop().create_task(
            self._comment_flow(pending),
            name=f"growth-comment:{pending.channel}:{pending.reply_to_msg_id}",
        )
        self._pending[id(task)] = task
        self._pending_by_target[pending.channel] = self._pending_by_target.get(pending.channel, 0) + 1

        def _done(t: asyncio.Task, *, _key: str = pending.channel) -> None:
            self._pending.pop(id(t), None)
            remaining = self._pending_by_target.get(_key, 1) - 1
            if remaining > 0:
                self._pending_by_target[_key] = remaining
            else:
                self._pending_by_target.pop(_key, None)

        task.add_done_callback(_done)

    async def _comment_flow(self, pending: PendingComment) -> None:
        """Human delay -> per-target cooldown -> global cap -> send with retry."""
        target = pending.target
        key = pending.channel

        # 1. Human-like delay before engaging.
        delay = HumanDelay(
            min_seconds=target.delay_min, max_seconds=target.delay_max, rng=self.rng
        ).sample()
        logger.info("Engaging %s thread in %.1fs", key, delay)
        await self.sleep(delay)

        # 2. Per-target cooldown (max 1 comment per target per window),
        # honoring persisted state across restarts. A stale trigger is
        # dropped rather than commented late: commenting 10 minutes after
        # a post reads as spam and doubles the moderation risk.
        wait = self.pacer.acquire(target, now=time.monotonic())
        persisted_at = self.state.last_post_at(key)
        if persisted_at is not None:
            remaining = target.cooldown - (self.clock() - persisted_at)
            wait = max(wait, remaining, 0.0)
        if wait > 0:
            self.stats.comments_skipped_cooldown += 1
            logger.info("Cooldown active for %s (%.0fs left); skipping this post", key, wait)
            return

        # 3. Global account-level cap.
        if not self.global_pacer.try_acquire():
            self.stats.comments_skipped_global += 1
            logger.warning(
                "Global cap reached; skipping comment for %s (next slot in ~%.0fs)",
                key,
                self.global_pacer.seconds_until_slot(),
            )
            return

        # 4. Render a template, avoiding an immediate repeat of the last one.
        last_template = self.state.last_template(key) or self._last_comment.get(key)
        text, template = render_random_with_template(
            self.config.templates,
            pending.context,
            rng=self.rng,
            avoid=last_template,
        )
        self._last_comment[key] = template

        await self._send_with_retry(pending, text, template)

    async def _send_with_retry(self, pending: PendingComment, text: str, template: str) -> None:
        target = pending.target
        for attempt in range(self.max_attempts):
            try:
                if self._comment_poster is not None:
                    await self._comment_poster(
                        pending.discussion_entity, pending.reply_to_msg_id, text
                    )
                else:
                    await self._post_comment_telethon(
                        pending.discussion_entity, pending.reply_to_msg_id, text
                    )
            except errors.FloodWaitError as exc:
                self.stats.flood_waits += 1
                retry_delay = self.backoff.flood_wait_delay(exc.seconds, attempt)
                logger.warning(
                    "FloodWait (%ss) on %s; backing off %.0fs (attempt %d/%d)",
                    exc.seconds,
                    pending.channel,
                    retry_delay,
                    attempt + 1,
                    self.max_attempts,
                )
                await self.sleep(retry_delay)
                continue
            except (errors.ChatWriteForbiddenError, errors.ChannelPrivateError) as exc:
                self.stats.comments_failed += 1
                logger.error(
                    "No permission to comment in %s (%s); giving up",
                    pending.channel,
                    type(exc).__name__,
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - transient network/RPC errors
                retry_delay = self.backoff.delay_for(attempt)
                logger.warning(
                    "Transient error (%s) on %s; retry %d/%d in %.0fs",
                    type(exc).__name__,
                    pending.channel,
                    attempt + 1,
                    self.max_attempts,
                    retry_delay,
                )
                await self.sleep(retry_delay)
                continue

            # Success -------------------------------------------------------
            self.stats.comments_posted += 1
            self.pacer.mark_posted(target, now=time.monotonic())
            self.state.remember_post(pending.channel, at=self.clock())
            self.state.remember_template(pending.channel, template)
            logger.info(
                "Comment posted in %s (thread %s): %r",
                pending.channel,
                pending.reply_to_msg_id,
                text[:80],
            )
            return

        self.stats.comments_failed += 1
        logger.error(
            "Giving up on comment for %s after %d attempt(s)", pending.channel, self.max_attempts
        )

    async def _post_comment_telethon(self, discussion_entity: Any, reply_to: int, text: str) -> None:
        """Send a comment into the discussion thread of a channel post."""
        if self.config.dry_run:
            logger.info("[dry-run] would comment %r in thread %s", text[:80], reply_to)
            return
        await self.client.send_message(discussion_entity, text, reply_to=reply_to, parse_mode=None)


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #


def _event_chat_id(event: Any) -> int | None:
    chat = getattr(event, "chat_id", None)
    if isinstance(chat, int):
        return chat
    return entity_id(getattr(event, "chat", None))


# ---------------------------------------------------------------------- #
# CLI entry point
# ---------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="ProxGram growth worker (Telethon userbot)")
    parser.add_argument("--env-file", default=".env", help="Path to .env file (default: .env)")
    parser.add_argument("--config", default=None, help="Optional JSON file with non-secret settings")
    parser.add_argument("--templates", default=None, help="Optional JSON file with comment templates")
    parser.add_argument("--dry-run", action="store_true", help="Log actions without posting")
    parser.add_argument(
        "--once", action="store_true", help="Exit after startup checks (smoke test)"
    )
    args = parser.parse_args(argv)

    env_overrides = dict(os.environ)
    if args.dry_run:
        env_overrides["GROWTH_DRY_RUN"] = "1"
    try:
        config = load_config(
            args.config,
            env_file=args.env_file,
            templates_path=args.templates,
            environ=env_overrides,
        )
    except Exception as exc:  # noqa: BLE001 - config errors must print cleanly
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    configure_logging(
        level=config.log_level,
        secrets=(config.session_string, config.api_hash, str(config.api_id)),
    )

    worker = GrowthWorker(config)
    try:
        asyncio.run(worker.run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
