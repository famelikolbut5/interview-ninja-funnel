import logging
import os
import re
import sys
import traceback
from pathlib import Path
from typing import TYPE_CHECKING

import psycopg
from loguru import logger

if TYPE_CHECKING:
    from loguru import Record


class InterceptHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        details = error_details(record.exc_info[1]) if record.exc_info and record.exc_info[1] else {}
        logger.bind(source=record.name, **details).log(record.levelno, redact(record.getMessage()))


def redact(value: str) -> str:
    for name in ("BOT_TOKEN", "DB_PASSWORD", "APP_DB_PASSWORD", "POSTGRES_PASSWORD", "DATABASE_URL"):
        secret = os.getenv(name, "")
        if secret:
            value = value.replace(secret, "[secret]")
    value = re.sub(r"\b\d{5,}:[A-Za-z0-9_-]{10,}", "[secret]", value)
    return value


def clean_record(record: "Record") -> None:
    record["message"] = redact(record["message"])
    record["extra"] = {redact(key): redact_extra(value) for key, value in record["extra"].items()}
    record["exception"] = None


def redact_extra(value: object) -> object:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {redact(key) if isinstance(key, str) else key: redact_extra(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [redact_extra(item) for item in value]
    return value


def configure_logging(service: str = "app") -> None:
    logger.remove()
    logger.configure(patcher=clean_record, extra={"service": service})
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logger.add(sys.stderr, level=level, diagnose=False, backtrace=False,
               format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {message} | {extra}")
    directory = Path(os.getenv("LOG_DIRECTORY", "logs"))
    directory.mkdir(parents=True, exist_ok=True)
    logger.add(directory / f"{service}.jsonl", level=level, serialize=True, rotation="5 MB",
               retention=3, encoding="utf-8", diagnose=False, backtrace=False)
    logging.basicConfig(handlers=[InterceptHandler()], level=logging.WARNING, force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        target = logging.getLogger(name)
        target.handlers.clear()
        target.propagate = True


def error_details(error: BaseException) -> dict[str, str]:
    if isinstance(error, BaseExceptionGroup):
        return error_details(error.exceptions[0])
    frames = traceback.extract_tb(error.__traceback__)
    details = {
        "error_type": type(error).__name__,
        "trace": " > ".join(f"{frame.filename}:{frame.lineno} {frame.name}" for frame in frames[-5:]),
    }
    if isinstance(error, psycopg.Error):
        details["sqlstate"] = error.sqlstate or "connection_error"
    elif isinstance(error, OSError):
        details["reason"] = f"errno={error.errno}"
    if error.__cause__:
        details["cause_type"] = type(error.__cause__).__name__
    return details
