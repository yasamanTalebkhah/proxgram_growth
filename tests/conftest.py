"""Shared fixtures and fakes for growth worker tests."""

from __future__ import annotations

import asyncio
import random
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from telethon.tl.types import Channel, Chat  # noqa: E402

from proxgram_growth.config import Config, TargetConfig  # noqa: E402
from proxgram_growth.templates import DEFAULT_TEMPLATES  # noqa: E402

# Marked ids matching Telethon conventions: PeerChannel(123) -> -1000000000123,
# PeerChat(456) -> -456.
MARKED_CHANNEL = -1000000000123
MARKED_DISCUSSION = -456


class FakeChannel(Channel):
    """Real Telethon Channel TLObject, constructible offline."""

    def __init__(self, raw_id: int) -> None:
        super().__init__(
            id=raw_id,
            title="News",
            broadcast=True,
            megagroup=False,
            photo=None,
            date=None,
        )


class FakeChat(Chat):
    """Real Telethon Chat TLObject (small group), constructible offline."""

    def __init__(self, raw_id: int) -> None:
        super().__init__(
            id=raw_id,
            title="Discussion",
            creator=True,
            photo=None,
            participants_count=100,
            date=None,
            version=1,
        )


def fake_get_peer_id(entity, add_mark=True):
    """Deterministic marked-id helper mirroring Telethon conventions."""
    if getattr(entity, "broadcast", False):
        return -(1_000_000_000_000 + entity.id)
    return -abs(entity.id)


class FakeClient:
    """Minimal Telethon client double for unit tests."""

    def __init__(self) -> None:
        self.handlers: list = []
        self.entities = {123: FakeChannel(123)}
        self.full_response = SimpleNamespace(
            full_chat=SimpleNamespace(linked_chat_id=456),
            chats=[FakeChat(456)],
        )
        self.disconnected = False

    async def connect(self):
        return self

    async def is_user_authorized(self):
        return True

    async def get_me(self):
        return SimpleNamespace(id=42)

    def add_event_handler(self, callback, event=None):
        self.handlers.append(callback)

    async def get_entity(self, channel):
        return self.entities[123]

    async def __call__(self, request):
        return self.full_response

    def is_connected(self):
        return True

    async def disconnect(self):
        self.disconnected = True


class FakeSleep:
    """Records sleep durations; can block the first call for orchestration."""

    def __init__(self, gate_first: bool = False) -> None:
        self.calls: list[float] = []
        self.gate = asyncio.Event() if gate_first else None

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self.gate is not None and len(self.calls) == 1:
            await self.gate.wait()


class Poster:
    """Records send_message-style calls; can raise scripted outcomes."""

    def __init__(self, outcomes: list | None = None) -> None:
        self.calls: list[tuple] = []
        self.outcomes = list(outcomes or [])

    @property
    def count(self) -> int:
        return len(self.calls)

    async def __call__(self, entity, reply_to, text) -> None:
        self.calls.append((entity, reply_to, text))
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome


def make_config(**overrides) -> Config:
    defaults = dict(
        api_id=1,
        api_hash="a" * 32,
        session_string="session-string-for-tests-0000",
        destination_channel="@proxgram",
        targets=(
            TargetConfig(
                channel="@news", cooldown=600, jitter=0, delay_min=0, delay_max=0
            ),
        ),
        templates=DEFAULT_TEMPLATES,
        state_file=None,
        global_window=3600,
        global_max_comments=10,
        backoff_base=30,
        backoff_factor=2.0,
        backoff_max=3600,
        dry_run=False,
    )
    defaults.update(overrides)
    config = Config(**defaults)
    config.validate()
    return config


def make_worker(config=None, *, client=None, sleep=None, poster=None, state=None):
    from proxgram_growth.state import StateStore
    from proxgram_growth.worker import GrowthWorker

    return GrowthWorker(
        config or make_config(),
        client=client,
        rng=random.Random(1234),
        state=state or StateStore(None),
        sleep=sleep or FakeSleep(),
        comment_poster=poster,
        max_attempts=3,
    )


def make_message(**kwargs):
    defaults = dict(
        id=7,
        post=True,
        fwd_from=None,
        reply_to=None,
        grouped_id=None,
        action=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def make_event(chat_id, message, *, out=False):
    return SimpleNamespace(chat_id=chat_id, message=message, out=out)


async def drain(worker) -> None:
    tasks = list(worker._pending.values())
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def seed_channel_ids(worker) -> None:
    """Pre-populate resolution maps as if _resolve_discussions had run."""
    worker._channel_ids = {"@news": MARKED_CHANNEL}
    worker.discussions = {MARKED_CHANNEL: MARKED_DISCUSSION}
    worker.discussion_entities = {MARKED_DISCUSSION: FakeChat(456)}


@pytest.fixture
def seeded_worker():
    worker = make_worker()
    seed_channel_ids(worker)
    return worker
