import hashlib
import json
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, Message, ReplyKeyboardMarkup
from loguru import logger


class Photo:
    def __init__(self, image: Path, cache: Path) -> None:
        self.image = image
        self.cache = cache
        self.digest = hashlib.sha256(image.read_bytes()).hexdigest()
        self.file_id: str | None = None
        try:
            record = json.loads(cache.read_text(encoding="utf-8"))
            if record.get("sha256") == self.digest and isinstance(record.get("file_id"), str):
                self.file_id = record["file_id"]
        except (OSError, ValueError, AttributeError):
            pass

    async def send(self, message: Message, caption: str, keyboard: ReplyKeyboardMarkup) -> None:
        bot = message.bot
        if bot is None:
            raise RuntimeError("У сообщения нет контекста бота")
        await self.send_to(bot, message.chat.id, caption, keyboard)

    async def send_to(self, bot: Bot, chat_id: int, caption: str, keyboard: ReplyKeyboardMarkup | None = None) -> None:
        if self.file_id:
            try:
                await bot.send_photo(chat_id, self.file_id, caption=caption, reply_markup=keyboard)
                return
            except TelegramBadRequest as error:
                description = error.message.casefold()
                if not any(reason in description for reason in ("file_id", "file identifier", "file reference", "file_reference")):
                    raise
                self.file_id = None
        sent = await bot.send_photo(chat_id, FSInputFile(self.image), caption=caption, reply_markup=keyboard)
        if sent.photo:
            self.file_id = sent.photo[-1].file_id
            try:
                self.cache.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.cache.with_suffix(".tmp")
                temporary.write_text(json.dumps({"sha256": self.digest, "file_id": self.file_id}), encoding="utf-8")
                temporary.replace(self.cache)
            except OSError:
                logger.bind(event="media_cache_failed", action="Проверьте права записи в каталог data").warning(
                    "Изображение отправлено. Сохранить кэш файла не удалось"
                )
