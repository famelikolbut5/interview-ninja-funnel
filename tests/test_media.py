import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import cast

import pytest
import yaml
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendPhoto
from aiogram.methods.base import TelegramMethod, TelegramType
from aiogram.types import FSInputFile, Message, ReplyKeyboardMarkup

from launch_funnel import delivery
from launch_funnel.bot import build_sender
from launch_funnel.config import ConfigError, read_funnel
from launch_funnel.media import Photo
from launch_funnel.storage import Storage


class PhotoSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.photos: list[FSInputFile | str] = []
        self.calls: list[SendPhoto] = []
        self.failure: str | None = None

    async def make_request(self, bot: Bot, method: TelegramMethod[TelegramType], timeout: int | None = None) -> TelegramType:
        assert isinstance(method, SendPhoto)
        assert isinstance(method.photo, (FSInputFile, str))
        self.photos.append(method.photo)
        self.calls.append(method)
        if self.failure and isinstance(method.photo, str):
            raise TelegramBadRequest(method, self.failure)
        return cast(TelegramType, Message.model_validate({
            "message_id": 1, "date": 1, "chat": {"id": 7, "type": "private"},
            "photo": [{"file_id": "cached-photo", "file_unique_id": "unique", "width": 100, "height": 100}],
        }))

    async def close(self) -> None:
        pass

    async def stream_content(self, url: str, headers: dict[str, str] | None = None, timeout: int = 30,
                             chunk_size: int = 65536, raise_for_status: bool = True) -> AsyncGenerator[bytes, None]:
        yield b""


def test_cover_reuses_file_after_restart_and_invalidates_changed_art(tmp_path: Path) -> None:
    image = tmp_path / "cover.jpg"
    image.write_bytes(b"image")
    cache = tmp_path / "cache.json"

    async def run() -> None:
        session = PhotoSession()
        async with Bot("12345:test-token", session=session) as bot:
            message = Message.model_validate({
                "message_id": 1, "date": 1, "chat": {"id": 7, "type": "private"},
            }).as_(bot)
            keyboard = ReplyKeyboardMarkup(keyboard=[])
            await Photo(image, cache).send(message, "hello", keyboard)
            await Photo(image, cache).send(message, "hello", keyboard)
            assert isinstance(session.photos[0], FSInputFile)
            assert session.photos[1] == "cached-photo"
            image.write_bytes(b"new image")
            await Photo(image, cache).send(message, "hello", keyboard)
            assert isinstance(session.photos[2], FSInputFile)

    asyncio.run(run())


def test_each_photo_has_its_caption_at_10_and_60_seconds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, storage: Storage) -> None:
    funnel = read_funnel(Path(__file__).resolve().parents[1] / "funnel.yaml")
    registered_at = 100.125
    storage.start(7, registered_at)
    storage.register(7, 7, registered_at, funnel.messages, "+15550000007")
    with storage.connection() as db:
        assert db.execute("SELECT step, due_at - %s FROM jobs ORDER BY step", (registered_at,)).fetchall() == [(1, 10.0), (2, 60.0)]
    clock = [registered_at]
    monkeypatch.setattr(delivery, "time", lambda: clock[0])

    async def run() -> None:
        session = PhotoSession()
        async with Bot("12345:test-token", session=session) as bot:
            worker = delivery.Delivery(storage, build_sender(bot, funnel, tmp_path))
            for offset, expected in [(9.999, 0), (10.0, 1), (59.999, 1), (60.0, 2)]:
                clock[0] = registered_at + offset
                await worker.deliver_due()
                assert len(session.calls) == expected
            for call, step in zip(session.calls, funnel.messages, strict=True):
                assert isinstance(call.photo, FSInputFile)
                assert Path(call.photo.path).name == step.image
                assert call.caption == step.text
                assert call.chat_id == 7
            assert storage.stats().sent == (1, 1)

    asyncio.run(run())


@pytest.mark.parametrize("replacement", ["../question.jpg", "missing-file.jpg"])
def test_funnel_rejects_invalid_photo_file(tmp_path: Path, replacement: str) -> None:
    original = Path(__file__).resolve().parents[1] / "funnel.yaml"
    config = tmp_path / "funnel.yaml"
    config.write_text(original.read_text(encoding="utf-8").replace("image: question.jpg", f"image: {replacement}"), encoding="utf-8")
    with pytest.raises(ConfigError):
        read_funnel(config)


def test_funnel_rejects_caption_over_telegram_limit(tmp_path: Path) -> None:
    original = Path(__file__).resolve().parents[1] / "funnel.yaml"
    data = read_funnel(original).model_dump()
    data["messages"][0]["text"] = "а" * 1025
    config = tmp_path / "long-caption.yaml"
    config.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ConfigError, match="1024"):
        read_funnel(config)


@pytest.mark.parametrize("failure,uploads", [("wrong file identifier/HTTP URL specified", 1), ("can't parse entities", 0)])
def test_only_invalid_file_id_triggers_reupload(tmp_path: Path, failure: str, uploads: int) -> None:
    image = tmp_path / "cover.jpg"
    image.write_bytes(b"image")
    photo = Photo(image, tmp_path / "cache.json")
    photo.file_id = "cached-photo"

    async def run() -> None:
        session = PhotoSession()
        session.failure = failure
        async with Bot("12345:test-token", session=session) as bot:
            if uploads:
                await photo.send_to(bot, 7, "caption")
            else:
                with pytest.raises(TelegramBadRequest):
                    await photo.send_to(bot, 7, "caption")
        assert sum(isinstance(item, FSInputFile) for item in session.photos) == uploads
        assert len(session.calls) == 1 + uploads

    asyncio.run(run())
