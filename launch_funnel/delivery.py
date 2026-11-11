import asyncio
from random import uniform
from time import time
from typing import Protocol

import psycopg
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from loguru import logger

from .logging import error_details
from .storage import Job, Storage

MAX_ATTEMPTS = 4


class SendMessage(Protocol):
    async def __call__(self, job: Job) -> None: ...


class Delivery:
    def __init__(self, storage: Storage, send: SendMessage) -> None:
        self.storage = storage
        self.send = send

    async def run(self) -> None:
        while True:
            await self.deliver_due()
            await asyncio.sleep(0.5)

    async def deliver_due(self) -> None:
        jobs = await asyncio.to_thread(self.storage.due_jobs, time())
        for job in jobs:
            await self.deliver(job)
            await asyncio.sleep(0.05)

    async def deliver(self, job: Job) -> None:
        attempt = job.attempts + 1
        log = logger.bind(event="follow_up", job_id=job.id, step=job.step, attempt=attempt)
        try:
            await self.send(job)
        except TelegramAPIError as error:
            retry_in: int | None
            if isinstance(error, TelegramRetryAfter):
                retry_in = max(1, error.retry_after) if attempt < MAX_ATTEMPTS else None
                reason = "rate_limit"
            elif isinstance(error, (TelegramNetworkError, TelegramServerError)):
                retry_in = round(2**attempt + uniform(0, 1)) if attempt < MAX_ATTEMPTS else None
                reason = "telegram_unavailable"
            elif isinstance(error, TelegramForbiddenError):
                retry_in = None
                reason = "chat_forbidden"
            else:
                retry_in = None
                reason = "telegram_rejected_request"

            log = log.bind(reason=reason, error_type=type(error).__name__)
            if retry_in is not None:
                await asyncio.to_thread(self.storage.retry, job.id, time() + retry_in)
                log.bind(retry_in_seconds=retry_in).warning("Telegram временно недоступен для отправки. Повтор запланирован")
            else:
                await asyncio.to_thread(self.storage.mark_failed, job.id, time())
                hint = (
                    "Проверьте доступ к Telegram API и лимиты, затем воспроизведите на тестовом аккаунте"
                    if reason == "telegram_unavailable"
                    else "Проверьте, открыт ли чат с ботом, и текст шага в funnel.yaml"
                )
                log.bind(action=hint).error("Отправка завершилась ошибкой")
        else:
            sent_at = time()
            try:
                await asyncio.to_thread(self.storage.mark_sent, job.id, sent_at)
            except psycopg.Error as error:
                log.bind(**error_details(error)).error(
                    "Telegram принял сообщение, PostgreSQL не записал результат. Проверьте состояние задания до повторной отправки"
                )
                raise
            log.bind(late_by_seconds=round(max(0, sent_at - job.due_at), 2)).info("Telegram принял сообщение")
