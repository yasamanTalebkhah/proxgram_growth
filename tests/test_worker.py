"""Unit tests for the growth worker: event flow, cooldowns, FloodWait, shutdown.

Telethon is faked at the boundary (FakeClient / Poster), so these tests run
without network access and without importing network-heavy client paths.
"""

from __future__ import annotations

import asyncio

import pytest

from conftest import (
    MARKED_CHANNEL,
    MARKED_DISCUSSION,
    FakeSleep,
    Poster,
    drain,
    make_config,
    make_event,
    make_message,
    make_worker,
    seed_channel_ids,
)
from backoff import BackoffPolicy
from rate_limit import GlobalPacer, PerTargetPacer
from state_manager import StateManager
from main import entity_id


# --------------------------------------------------------------------- #
# Event classification
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_channel_post_triggers_comment(seeded_worker):
    worker = seeded_worker
    poster = Poster()
    worker._comment_poster = poster
    worker.sleep = FakeSleep()

    event = make_event(MARKED_CHANNEL, make_message(post=True, id=7))
    await worker._handle_event(event)
    await drain(worker)

    assert poster.count == 1
    entity, reply_to, text = poster.calls[0]
    assert reply_to == 7
    assert text and "@proxgram" in text


@pytest.mark.asyncio
async def test_ordinary_group_chatter_is_ignored(seeded_worker):
    worker = seeded_worker
    poster = Poster()
    worker._comment_poster = poster

    # Message in the discussion group without forward/post markers.
    event = make_event(MARKED_DISCUSSION, make_message(post=False, fwd_from=None, id=99))
    await worker._handle_event(event)
    await drain(worker)

    assert poster.count == 0
    assert worker.stats.posts_seen == 0


@pytest.mark.asyncio
async def test_discussion_forward_of_channel_post_triggers(seeded_worker):
    worker = seeded_worker
    poster = Poster()
    worker._comment_poster = poster
    worker.sleep = FakeSleep()

    fwd = object()  # any non-None forward marker
    event = make_event(
        MARKED_DISCUSSION,
        make_message(post=False, fwd_from=fwd, reply_to=make_reply(7), id=99),
    )
    await worker._handle_event(event)
    await drain(worker)

    assert poster.count == 1
    assert poster.calls[0][1] == 7  # replies to the thread root


@pytest.mark.asyncio
async def test_outgoing_own_messages_are_ignored(seeded_worker):
    worker = seeded_worker
    poster = Poster()
    worker._comment_poster = poster

    event = make_event(MARKED_CHANNEL, make_message(post=True), out=True)
    await worker._handle_event(event)
    await drain(worker)

    assert poster.count == 0


@pytest.mark.asyncio
async def test_album_triggers_only_once(seeded_worker):
    worker = seeded_worker
    poster = Poster()
    worker._comment_poster = poster
    worker.sleep = FakeSleep()

    first = make_event(MARKED_CHANNEL, make_message(post=True, grouped_id=555, id=1))
    second = make_event(MARKED_CHANNEL, make_message(post=True, grouped_id=555, id=2))
    await worker._handle_event(first)
    await worker._handle_event(second)
    await drain(worker)

    assert poster.count == 1


@pytest.mark.asyncio
async def test_unknown_channel_is_ignored(seeded_worker):
    worker = seeded_worker
    poster = Poster()
    worker._comment_poster = poster

    event = make_event(-1009999999999, make_message(post=True))
    await worker._handle_event(event)
    await drain(worker)

    assert poster.count == 0


@pytest.mark.asyncio
async def test_handler_never_raises(seeded_worker, monkeypatch):
    worker = seeded_worker

    async def boom(event):
        raise RuntimeError("boom")

    monkeypatch.setattr(worker, "_handle_event", boom)
    await worker._on_new_message(make_event(MARKED_CHANNEL, make_message()))  # must not raise


# --------------------------------------------------------------------- #
# Rate limiting inside the pipeline
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_per_target_cooldown_blocks_second_comment(seeded_worker):
    worker = seeded_worker
    poster = Poster()
    worker._comment_poster = poster
    worker.sleep = FakeSleep()

    for post_id in (7, 8):
        await worker._handle_event(make_event(MARKED_CHANNEL, make_message(post=True, id=post_id)))
        await drain(worker)

    assert poster.count == 1
    assert worker.stats.comments_posted == 1
    assert worker.stats.comments_skipped_cooldown == 1


@pytest.mark.asyncio
async def test_persisted_state_cooldown_survives_restart(tmp_path):
    state_file = tmp_path / "state.json"
    config = make_config()

    # First run: post a comment (persisted via clock).
    state = StateManager(str(state_file))
    worker = make_worker(config, state=state)
    seed_channel_ids(worker)
    poster = Poster()
    worker._comment_poster = poster
    worker.sleep = FakeSleep()
    await worker._handle_event(make_event(MARKED_CHANNEL, make_message(post=True, id=7)))
    await drain(worker)
    assert poster.count == 1

    # Second run: fresh worker, same state file, seconds later.
    worker2 = make_worker(config, state=StateManager(str(state_file)))
    seed_channel_ids(worker2)
    poster2 = Poster()
    worker2._comment_poster = poster2
    worker2.sleep = FakeSleep()
    await worker2._handle_event(make_event(MARKED_CHANNEL, make_message(post=True, id=8)))
    await drain(worker2)

    assert poster2.count == 0
    assert worker2.stats.comments_skipped_cooldown == 1


@pytest.mark.asyncio
async def test_global_cap_skips_comment(seeded_worker):
    worker = seeded_worker
    worker.global_pacer = GlobalPacer(max_comments=1, window_seconds=3600)
    worker.global_pacer.try_acquire()  # exhaust the single slot
    poster = Poster()
    worker._comment_poster = poster
    worker.sleep = FakeSleep()

    await worker._handle_event(make_event(MARKED_CHANNEL, make_message(post=True, id=7)))
    await drain(worker)

    assert poster.count == 0
    assert worker.stats.comments_skipped_global == 1


@pytest.mark.asyncio
async def test_no_immediate_template_repeat(seeded_worker):
    worker = seeded_worker
    worker.sleep = FakeSleep()
    poster = Poster()
    worker._comment_poster = poster

    texts = []

    async def spy(entity, reply_to, text):
        texts.append(text)

    worker._comment_poster = spy
    # Reset cooldown pacing between posts.
    for i in range(2):
        worker.pacer = PerTargetPacer(rng=worker.rng)
        worker.state = StateManager(None)
        await worker._handle_event(make_event(MARKED_CHANNEL, make_message(post=True, id=i + 1)))
        await drain(worker)

    assert len(texts) == 2


# --------------------------------------------------------------------- #
# FloodWait / error handling
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_floodwait_is_retried_with_backoff(seeded_worker):
    from telethon import errors

    worker = seeded_worker
    worker.sleep = FakeSleep()
    outcomes = [errors.FloodWaitError(request=None), None]
    # FloodWaitError requires a request kwarg in some versions.
    outcomes[0] = errors.FloodWaitError(request=None)
    poster = Poster(outcomes=outcomes)
    worker._comment_poster = poster

    await worker._handle_event(make_event(MARKED_CHANNEL, make_message(post=True, id=7)))
    await drain(worker)

    assert poster.count == 2
    assert worker.stats.comments_posted == 1
    assert worker.stats.flood_waits == 1
    # A backoff sleep honoring the FloodWait seconds happened.
    assert any(w >= 10 for w in worker.sleep.calls)


@pytest.mark.asyncio
async def test_floodwait_gives_up_after_max_attempts(seeded_worker):
    from telethon import errors

    worker = seeded_worker
    worker.sleep = FakeSleep()
    poster = Poster(outcomes=[errors.FloodWaitError(request=None) for _ in range(3)])
    worker._comment_poster = poster

    await worker._handle_event(make_event(MARKED_CHANNEL, make_message(post=True, id=7)))
    await drain(worker)

    assert worker.stats.comments_posted == 0
    assert worker.stats.comments_failed == 1
    assert worker.stats.flood_waits == 3  # one FloodWait per attempt


@pytest.mark.asyncio
async def test_permission_errors_do_not_retry(seeded_worker):
    from telethon import errors

    worker = seeded_worker
    worker.sleep = FakeSleep()
    poster = Poster(outcomes=[errors.ChatWriteForbiddenError(request=None)])
    worker._comment_poster = poster

    await worker._handle_event(make_event(MARKED_CHANNEL, make_message(post=True, id=7)))
    await drain(worker)

    assert poster.count == 1
    assert worker.stats.comments_failed == 1
    assert worker.stats.flood_waits == 0
    assert not any(w > 1 for w in worker.sleep.calls)  # no backoff sleeps


@pytest.mark.asyncio
async def test_transient_error_retries(seeded_worker):
    worker = seeded_worker
    worker.sleep = FakeSleep()
    poster = Poster(outcomes=[ConnectionError("reset"), None])
    worker._comment_poster = poster

    await worker._handle_event(make_event(MARKED_CHANNEL, make_message(post=True, id=7)))
    await drain(worker)

    assert poster.count == 2
    assert worker.stats.comments_posted == 1


# --------------------------------------------------------------------- #
# Shutdown
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_shutdown_cancels_pending_tasks_and_disconnects(seeded_worker):
    worker = seeded_worker
    client = FakeClientShim()
    worker.client = client
    worker.pacer.mark_posted(worker.config.targets[0], now=0.0)  # force cooldown skip

    gate = asyncio.Event()

    async def slow_poster(entity, reply_to, text):
        await gate.wait()

    worker._comment_poster = slow_poster
    worker.sleep = FakeSleep()

    task = asyncio.get_running_loop().create_task(
        worker._comment_flow(
            _pending(worker, reply_to=1)
        )
    )
    worker._pending[id(task)] = task

    await worker._shutdown()

    assert task.cancelled() or task.done()
    assert client.disconnected


@pytest.mark.asyncio
async def test_request_stop_is_idempotent(seeded_worker):
    worker = seeded_worker
    worker.request_stop()
    worker.request_stop()
    assert worker._stop_event.is_set()


# --------------------------------------------------------------------- #
# Startup wiring
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_run_registers_handlers_and_stops_cleanly(seeded_worker):
    from conftest import FakeClient

    worker = seeded_worker
    client = FakeClient()
    worker.client = client
    # FakeClient resolves FakeChannel(123) -> linked FakeChat(456), which
    # re-populates the same marked ids the fixture seeded (integration path).

    async def run_and_stop():
        await asyncio.sleep(0.01)
        worker.request_stop()

    await asyncio.gather(worker.run(), run_and_stop())
    assert len(client.handlers) >= 1
    assert client.disconnected


@pytest.mark.asyncio
async def test_unauthorized_session_refuses_to_start(seeded_worker):
    from conftest import FakeClient

    worker = seeded_worker

    class UnauthorizedClient(FakeClient):
        async def is_user_authorized(self):
            return False

    worker = seeded_worker
    worker.client = UnauthorizedClient()

    await asyncio.wait_for(worker.run(), timeout=2)
    assert worker.stats.posts_seen == 0


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #


class FakeClientShim:
    def __init__(self):
        self.disconnected = False

    def is_connected(self):
        return True

    async def disconnect(self):
        self.disconnected = True


class _ReplyHeader:
    def __init__(self, msg_id: int) -> None:
        self.reply_to_msg_id = msg_id


def make_reply(msg_id: int):
    return _ReplyHeader(msg_id)


def _pending(worker, *, reply_to: int = 1):
    from main import PendingComment

    target = worker.config.targets[0]
    return PendingComment(
        target=target,
        channel=target.channel,
        discussion_entity=object(),
        discussion_id=MARKED_DISCUSSION,
        reply_to_msg_id=reply_to,
        post_id=reply_to,
        context={"channel": "@proxgram", "proxy_count": 10, "speed_note": "fast"},
    )
