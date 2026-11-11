import asyncio
from collections.abc import AsyncGenerator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.methods import SendMessage, SendPhoto
from aiogram.methods.base import TelegramMethod, TelegramType
from aiogram.types import (
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from loguru import logger

from launch_funnel import bot as bot_module
from launch_funnel import delivery
from launch_funnel.bot import build_router
from launch_funnel.config import ConfigError, Settings, read_funnel
from launch_funnel.media import Photo
from launch_funnel.storage import Job, Registration, Stats, Storage

FUNNEL_PATH = Path(__file__).resolve().parents[1] / "funnel.yaml"
FUNNEL = read_funnel(FUNNEL_PATH)
TOKEN = "12345:test-token"


def test_invalid_config_points_to_delay(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text(FUNNEL_PATH.read_text(encoding="utf-8").replace("delay_seconds: 10", "delay_seconds: -1"), encoding="utf-8")
    with pytest.raises(ConfigError, match=r"messages\.0\.delay_seconds"):
        read_funnel(path)


def test_simultaneous_contacts_create_one_schedule(storage: Storage) -> None:
    assert storage.register(7, 7, 100, FUNNEL.messages, "+15550000007") is Registration.START_REQUIRED
    storage.start(7, 100)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(storage.register, 7, 7, 101, FUNNEL.messages, "+15550000007")
        second = pool.submit(storage.register, 7, 7, 101, FUNNEL.messages, "+15550000007")
        assert {first.result(), second.result()} == {Registration.NEW, Registration.ALREADY_REGISTERED}
    assert storage.start(7, 105)
    assert storage.stats() == Stats(1, 1, (0, 0), (1, 1))
    assert storage.due_jobs(110.9) == []
    first_job, = storage.due_jobs(111)
    assert first_job.due_at == 111
    storage.mark_sent(first_job.id, 111)
    assert storage.due_jobs(160.9) == []
    second_job, = storage.due_jobs(161)
    assert second_job.due_at == 161


def test_delivery_recovers_after_restart_and_transient_error(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> None:
    storage.start(7, 100)
    storage.register(7, 7, 100, FUNNEL.messages, "+15550000007")
    clock = [109.9]
    monkeypatch.setattr(delivery, "uniform", lambda _a, _b: 0)
    sent: list[str] = []
    failures = iter([
        TelegramRetryAfter(SendMessage(chat_id=7, text="test"), "limit", 55),
        TelegramNetworkError(SendMessage(chat_id=7, text="test"), "network"),
    ])

    async def send(job: Job) -> None:
        assert job.chat_id == 7
        if error := next(failures, None):
            raise error
        sent.append(job.text)

    monkeypatch.setattr(delivery, "time", lambda: clock[0])
    asyncio.run(delivery.Delivery(storage, send).deliver_due())
    assert sent == []
    clock[0] = 110
    asyncio.run(delivery.Delivery(storage, send).deliver_due())
    reopened = Storage(storage.database_url)
    restarted = delivery.Delivery(reopened, send)
    clock[0] = 160
    asyncio.run(restarted.deliver_due())
    assert reopened.stats().sent == (0, 0)
    clock[0] = 165
    asyncio.run(restarted.deliver_due())
    clock[0] = 169
    asyncio.run(restarted.deliver_due())
    assert sent == [FUNNEL.messages[0].text]
    asyncio.run(restarted.deliver_due())
    asyncio.run(restarted.deliver_due())
    assert sent == [item.text for item in FUNNEL.messages]
    assert reopened.stats().sent == (1, 1)


def test_forbidden_delivery_is_not_counted_as_sent(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> None:
    storage.start(7, 100)
    storage.register(7, 7, 100, FUNNEL.messages, "+15550000007")

    async def send(job: Job) -> None:
        raise TelegramForbiddenError(SendMessage(chat_id=job.chat_id, text=job.text), "blocked")

    monkeypatch.setattr(delivery, "time", lambda: 110)
    asyncio.run(delivery.Delivery(storage, send).deliver_due())
    assert storage.stats().sent == (0, 0)
    assert storage.due_jobs(110) == []


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[SendMessage | SendPhoto] = []

    async def make_request(self, bot: Bot, method: TelegramMethod[TelegramType], timeout: int | None = None) -> TelegramType:
        assert isinstance(method, (SendMessage, SendPhoto))
        self.calls.append(method)
        response = Message.model_validate({"message_id": 1, "date": 1, "chat": {"id": 7, "type": "private"}})
        return cast(TelegramType, response)

    async def close(self) -> None:
        pass

    async def stream_content(self, url: str, headers: dict[str, str] | None = None, timeout: int = 30,
                             chunk_size: int = 65536, raise_for_status: bool = True) -> AsyncGenerator[bytes, None]:
        yield b""


def test_dispatcher_wires_real_commands_and_contact(
    storage: Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    starts: list[dict[str, object]] = []
    sink = logger.add(lambda message: starts.append(dict(message.record["extra"])),
                      filter=lambda record: record["extra"].get("event") == "bot_start")

    async def run() -> None:
        session = RecordingSession()
        dispatcher = Dispatcher()
        dispatcher.include_router(build_router(FUNNEL, storage, 7, "https://demo.example/?demo=1", tmp_path))
        async with Bot(TOKEN, session=session) as bot:
            async def feed(update_id: int, user_id: int = 7, **payload: object) -> None:
                update = Update.model_validate({
                    "update_id": update_id,
                    "message": {
                        "message_id": update_id, "date": 100, "chat": {"id": user_id, "type": "private"},
                        "from": {"id": user_id, "first_name": "Demo", "is_bot": False}, **payload,
                    },
                })
                await dispatcher.feed_update(bot, update)

            await feed(1, text="/start ninja_channel")
            greeting = session.calls[-1]
            assert isinstance(greeting, SendPhoto)
            assert isinstance(greeting.reply_markup, ReplyKeyboardMarkup)
            contact_button = greeting.reply_markup.keyboard[0][0]
            assert contact_button.text == FUNNEL.share_button
            assert contact_button.request_contact is True
            assert contact_button.web_app is None
            assert greeting.reply_markup.one_time_keyboard is True

            await feed(2, contact={"user_id": 8, "first_name": "Other", "phone_number": "000"})
            assert storage.stats().registered == 0
            await feed(20, contact={"user_id": 7, "first_name": "Demo", "phone_number": "000"},
                       forward_origin={"type": "user", "date": 100,
                                       "sender_user": {"id": 7, "first_name": "Demo", "is_bot": False}})
            assert storage.stats().registered == 0
            for update_id in (3, 4):
                await feed(update_id, contact={"user_id": 7, "first_name": "Demo", "phone_number": "+15550000007"})
            assert storage.stats() == Stats(1, 1, (0, 0), (1, 1))
            assert isinstance(session.calls[-1], SendMessage) and session.calls[-1].text == FUNNEL.already_registered
            assert isinstance(session.calls[-1].reply_markup, ReplyKeyboardRemove)

            await feed(5, user_id=8, text="/stats")
            assert isinstance(session.calls[-1], SendMessage) and session.calls[-1].text == FUNNEL.dashboard_denied
            await feed(6, text="/stats")
            markup = session.calls[-1].reply_markup
            assert isinstance(markup, InlineKeyboardMarkup)
            button = markup.inline_keyboard[0][0]
            assert button.web_app is not None and button.web_app.url == "https://demo.example/?demo=1"

            await feed(7, text="/start tg_ads")
            with storage.connection() as db:
                row = db.execute(
                    "SELECT phone_number, source_key, utm_source, utm_medium, utm_campaign, utm_content FROM participants WHERE user_id = 7",
                ).fetchone()
                assert row == ("+15550000007", "ninja_channel", "telegram", "organic", "interview_ninja_webinar", "channel_post")
                assert db.execute("SELECT COUNT(*) FROM jobs WHERE user_id = 7").fetchall()[0][0] == 2
            for user_id, payload, expected in ((9, "unknown_campaign", "unknown_campaign"), (10, "bad payload", None),
                                              (11, "a" * 65, None)):
                await feed(user_id, user_id=user_id, text=f"/start {payload}")
                with storage.connection() as db:
                    assert db.execute("SELECT source_key, utm_source FROM participants WHERE user_id = %s", (user_id,)).fetchone() == (expected, None)

            async def unavailable_photo(_photo: Photo, message: Message, caption: str,
                                        keyboard: ReplyKeyboardMarkup) -> None:
                raise TelegramNetworkError(SendPhoto(chat_id=message.chat.id, photo="cached-photo"), "network")

            monkeypatch.setattr(Photo, "send", unavailable_photo)
            await feed(12, user_id=12, text="/start")
            assert isinstance(session.calls[-1], SendMessage)
            assert session.calls[-1].text == FUNNEL.temporary_error

    try:
        asyncio.run(run())
    finally:
        logger.remove(sink)
    assert [record["already_registered"] for record in starts] == [False, True, False, False, False]
    assert all("duration_ms" in record and "message_id" in record for record in starts)


def test_attribution_and_phone_survive_restart_and_repeated_contact(storage: Storage) -> None:
    attribution = FUNNEL.sources["ninja_channel"].model_copy(update={"utm_term": "python"})
    storage.start(7, 100, "ninja_channel", attribution)
    storage.register(7, 7, 101, FUNNEL.messages, "+15550000007")
    reopened = Storage(storage.database_url)
    reopened.initialize()
    assert reopened.start(7, 110, "tg_ads", FUNNEL.sources["tg_ads"])
    assert reopened.register(7, 7, 111, FUNNEL.messages, "+15550000008") is Registration.ALREADY_REGISTERED
    with reopened.connection() as db:
        assert db.execute("SELECT started_at, registered_at, phone_number, source_key, utm_term FROM participants").fetchone() == (
            100, 101, "+15550000008", "ninja_channel", "python",
        )
        assert db.execute("SELECT due_at FROM jobs ORDER BY step").fetchall() == [(111,), (161,)]
    reopened.start(8, 120)
    reopened.start(8, 121, "tg_ads", FUNNEL.sources["tg_ads"])
    with reopened.connection() as db:
        assert db.execute("SELECT source_key, utm_source FROM participants WHERE user_id = 8").fetchone() == (None, None)


@pytest.mark.parametrize("key", ["bad key", "a" * 65])
def test_invalid_source_key_is_rejected(tmp_path: Path, key: str) -> None:
    path = tmp_path / "source.yaml"
    path.write_text(FUNNEL_PATH.read_text(encoding="utf-8").replace("  ninja_channel:", f"  {key}:"), encoding="utf-8")
    with pytest.raises(ConfigError, match="sources"):
        read_funnel(path)


def test_invalid_token_stops_without_restart_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bot_module, "load_environment", lambda: None)
    monkeypatch.setattr(bot_module, "configure_logging", lambda _name: None)
    monkeypatch.setattr(bot_module, "read_settings", lambda: Settings(TOKEN, 7, "postgresql://localhost/unused", FUNNEL_PATH))

    async def rejected() -> None:
        raise TelegramUnauthorizedError(SendMessage(chat_id=7, text="test"), "Unauthorized")

    from contextlib import nullcontext
    monkeypatch.setattr(Storage, "bot_lock", lambda _self, _bot_id: nullcontext())
    monkeypatch.setattr(bot_module, "run", rejected)
    with pytest.raises(SystemExit) as result:
        bot_module.main()
    assert result.value.code == 2
