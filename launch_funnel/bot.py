import asyncio
import logging
import re
from contextlib import suppress
from pathlib import Path
from time import monotonic, time

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.middlewares.base import (
    BaseRequestMiddleware,
    NextRequestMiddlewareType,
)
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramConflictError,
    TelegramUnauthorizedError,
)
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.methods import GetUpdates, Response, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import (
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LinkPreviewOptions,
    MenuButtonWebApp,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    WebAppInfo,
)
from loguru import logger

from .config import (
    STATIC,
    ConfigError,
    Funnel,
    load_environment,
    read_funnel,
    read_mini_app_url,
    read_settings,
)
from .delivery import Delivery, SendMessage
from .logging import configure_logging, error_details
from .media import Photo
from .storage import BotAlreadyRunning, Job, Registration, Storage


class PollingLog(BaseRequestMiddleware):
    last_error: str | None = None

    async def __call__(self, make_request: NextRequestMiddlewareType[TelegramType], bot: Bot,
                       method: TelegramMethod[TelegramType]) -> Response[TelegramType]:
        request = make_request(bot, method)
        if not isinstance(method, GetUpdates):
            return await request
        try:
            result = await request
        except TelegramAPIError as error:
            kind = type(error).__name__
            if kind != self.last_error:
                action = (
                    "stop_duplicate_poller"
                    if isinstance(error, TelegramConflictError)
                    else "check_telegram_connection"
                )
                logger.bind(event="polling_failed", error_type=kind, action=action).warning("Polling failed")
                self.last_error = kind
            raise
        if self.last_error is not None:
            logger.bind(event="polling_restored").info("Polling recovered")
            self.last_error = None
        return result


def build_router(funnel: Funnel, storage: Storage, admin_id: int, mini_app_url: str, cache_directory: Path) -> Router:
    router = Router()
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    cover = Photo(STATIC / "cover.jpg", cache_directory / "cover.json")
    contact_keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=funnel.share_button, request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    dashboard_keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=funnel.dashboard_button, web_app=WebAppInfo(url=mini_app_url)),
    ]])

    @router.message(CommandStart())
    async def start(message: Message, command: CommandObject) -> None:
        started_at = monotonic()
        if message.from_user is None:
            return
        source_key = command.args if command.args and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", command.args) else None
        attribution = funnel.sources.get(source_key) if source_key else None
        registered = await asyncio.to_thread(storage.start, message.from_user.id, time(), source_key, attribution)
        if registered:
            await message.answer(funnel.already_registered, reply_markup=ReplyKeyboardRemove())
        else:
            await cover.send(message, funnel.greeting, contact_keyboard)
        logger.bind(event="bot_start", message_id=message.message_id, already_registered=registered,
                    duration_ms=round((monotonic() - started_at) * 1000)).info(
            "Start command handled"
        )

    @router.message(Command("stats"), F.from_user.id == admin_id)
    async def open_dashboard(message: Message) -> None:
        await message.answer(funnel.dashboard_prompt, reply_markup=dashboard_keyboard)

    @router.message(Command("stats"))
    async def dashboard_for_participant(message: Message) -> None:
        await message.answer(funnel.dashboard_denied)

    @router.message(F.contact)
    async def register(message: Message) -> None:
        sender, contact = message.from_user, message.contact
        if sender is None or contact is None:
            return
        if sender.id != contact.user_id or message.forward_origin is not None:
            await message.answer(funnel.wrong_contact)
            return
        result = await asyncio.to_thread(
            storage.register, sender.id, message.chat.id, time(), funnel.messages, contact.phone_number,
        )
        if result is Registration.START_REQUIRED:
            await message.answer(funnel.start_first)
            return
        if result is Registration.NEW:
            logger.bind(event="registered", message_id=message.message_id, scheduled=2).info("Registration created")
        text = funnel.registered if result is Registration.NEW else funnel.already_registered
        await message.answer(text, reply_markup=ReplyKeyboardRemove())

    @router.message()
    async def other_input(message: Message) -> None:
        await message.answer(funnel.unexpected_input)

    @router.error()
    async def handle_error(event: ErrorEvent) -> bool:
        logger.bind(event="update_error", update_id=event.update.update_id, **error_details(event.exception)).error(
            "Update handler failed"
        )
        if event.update.message is not None:
            with suppress(TelegramAPIError):
                await event.update.message.answer(funnel.temporary_error)
        return True

    return router


def build_sender(bot: Bot, funnel: Funnel, cache_directory: Path) -> SendMessage:
    photos = {
        step: Photo(STATIC / follow_up.image, cache_directory / f"step-{step}.json")
        for step, follow_up in enumerate(funnel.messages, start=1) if follow_up.image
    }

    async def send(job: Job) -> None:
        if photo := photos.get(job.step):
            await photo.send_to(bot, job.chat_id, job.text)
        else:
            await bot.send_message(job.chat_id, job.text)

    return send


async def run() -> None:
    settings = read_settings()
    mini_app_url = read_mini_app_url()
    funnel = read_funnel(settings.funnel_path)
    storage = Storage(settings.database_url)
    await asyncio.to_thread(storage.check_ready)

    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(funnel, storage, settings.admin_user_id, mini_app_url, settings.cache_directory))
    async with Bot(settings.bot_token, default=DefaultBotProperties(
        parse_mode=ParseMode.HTML, link_preview=LinkPreviewOptions(is_disabled=True),
    )) as bot:
        bot.session.middleware.register(PollingLog())
        me = await bot.get_me()
        if (await bot.get_webhook_info()).url:
            raise ConfigError("У бота настроен webhook. Возьмите отдельного бота для демо с long polling")

        try:
            await bot.set_chat_menu_button(
                chat_id=settings.admin_user_id,
                menu_button=MenuButtonWebApp(text=funnel.dashboard_button, web_app=WebAppInfo(url=mini_app_url)),
            )
        except TelegramAPIError as error:
            logger.bind(event="dashboard_menu_failed", error_type=type(error).__name__).warning(
                "Dashboard menu update failed"
            )
        else:
            logger.bind(event="dashboard_menu_ready").debug("Dashboard menu configured")

        delivery = Delivery(storage, build_sender(bot, funnel, settings.cache_directory))
        logger.bind(event="bot_started", bot=me.username, delays=[step.delay_seconds for step in funnel.messages]).info(
            "Bot started"
        )
        async with asyncio.TaskGroup() as group:
            worker = group.create_task(delivery.run())
            try:
                await dispatcher.start_polling(bot, close_bot_session=False, allowed_updates=["message"],
                                               tasks_concurrency_limit=32)
            finally:
                worker.cancel()


def main() -> None:
    load_environment()
    configure_logging("bot")
    logging.getLogger("aiogram").setLevel(logging.CRITICAL)
    try:
        settings = read_settings()
        storage = Storage(settings.database_url)
        with storage.bot_lock(int(settings.bot_token.partition(":")[0])):
            asyncio.run(run())
    except KeyboardInterrupt:
        logger.bind(event="bot_stopped").info("Bot stopped")
    except ConfigError as error:
        logger.bind(event="invalid_config", reason=str(error)).error("Invalid configuration")
        raise SystemExit(2) from None
    except TelegramUnauthorizedError:
        logger.bind(event="invalid_bot_token", action="check_bot_token").error(
            "Bot authentication failed"
        )
        raise SystemExit(2) from None
    except BotAlreadyRunning:
        logger.bind(event="bot_already_running", action="stop_duplicate_poller").error(
            "Bot instance already running"
        )
        raise SystemExit(2) from None
    except Exception as error:
        logger.bind(event="bot_stopped", **error_details(error)).error(
            "Bot process failed"
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
