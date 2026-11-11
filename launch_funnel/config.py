import os
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Self
from urllib.parse import urlsplit

import yaml
from dotenv import load_dotenv
from psycopg import ProgrammingError
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

STATIC = Path(__file__).resolve().parent / "static"
MessageText = Annotated[str, Field(min_length=1, max_length=4096)]
ButtonText = Annotated[str, Field(min_length=1, max_length=64)]
SourceKey = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
UTMValue = Annotated[str, Field(min_length=1, max_length=200)]


class ConfigError(ValueError):
    pass


class Attribution(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    utm_source: UTMValue
    utm_medium: UTMValue
    utm_campaign: UTMValue
    utm_content: UTMValue | None = None
    utm_term: UTMValue | None = None


class FollowUp(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    title: Annotated[str, Field(min_length=1, max_length=70)]
    delay_seconds: Annotated[int, Field(gt=0)]
    text: MessageText
    image: Annotated[str | None, Field(pattern=r"^[a-z0-9][a-z0-9_-]*\.(jpg|jpeg|png)$")] = None

    @model_validator(mode="after")
    def check_photo_caption(self) -> Self:
        if self.image and len(self.text.encode("utf-16-le")) // 2 > 1024:
            raise ValueError("Подпись к изображению должна быть не длиннее 1024 символов с HTML-разметкой")
        return self


class Funnel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    greeting: Annotated[str, Field(min_length=1, max_length=1024)]
    share_button: ButtonText
    registered: MessageText
    already_registered: MessageText
    wrong_contact: MessageText
    start_first: MessageText
    unexpected_input: MessageText
    temporary_error: MessageText
    dashboard_button: ButtonText
    dashboard_prompt: MessageText
    dashboard_denied: MessageText = "Статистика открывается с аккаунта владельца бота."
    messages: Annotated[list[FollowUp], Field(min_length=2, max_length=2)]
    sources: dict[SourceKey, Attribution] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_delays(self) -> Self:
        first, second = self.messages
        if first.delay_seconds >= second.delay_seconds:
            raise ValueError("Задержка второго сообщения должна быть больше первой")
        return self


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    admin_user_id: int
    database_url: str
    funnel_path: Path
    cache_directory: Path = Path("data")


def read_database_url() -> str:
    if value := os.getenv("DATABASE_URL", "").strip():
        try:
            conninfo_to_dict(value)
        except ProgrammingError:
            raise ConfigError("Проверьте DATABASE_URL") from None
        return value
    password = os.getenv("DB_PASSWORD", os.getenv("APP_DB_PASSWORD", ""))
    if not password:
        raise ConfigError("Задайте APP_DB_PASSWORD в .env")
    return make_conninfo(
        host=os.getenv("DB_HOST", "127.0.0.1"), port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "funnel"), user=os.getenv("DB_USER", "funnel"), password=password,
        options="-c search_path=funnel,public -c statement_timeout=10000 -c lock_timeout=5000",
    )


def read_settings() -> Settings:
    token = os.getenv("BOT_TOKEN", "").strip()
    bot_id, separator, secret = token.partition(":")
    if not (bot_id.isascii() and bot_id.isdecimal() and separator and secret and not any(c.isspace() for c in secret)):
        raise ConfigError("Задайте корректный BOT_TOKEN в .env")

    admin_id = os.getenv("ADMIN_USER_ID", "").strip()
    if not (admin_id.isascii() and admin_id.isdecimal() and 0 < int(admin_id) < 2**52):
        raise ConfigError("Задайте числовой ADMIN_USER_ID в .env")
    return Settings(
        token, int(admin_id),
        read_database_url(),
        Path(os.getenv("FUNNEL_CONFIG", "funnel.yaml")).resolve(),
        Path(os.getenv("CACHE_DIRECTORY", "data")).resolve(),
    )


def read_mini_app_url() -> str:
    value = os.getenv("MINI_APP_URL", "").strip()
    try:
        url = urlsplit(value)
        url.port
    except ValueError as error:
        raise ConfigError("Проверьте адрес MINI_APP_URL в .env") from error
    if url.scheme != "https" or not url.hostname or url.username or url.password or any(c.isspace() for c in value):
        raise ConfigError("Укажите HTTPS-адрес туннеля в MINI_APP_URL")
    return value


def read_funnel(path: Path) -> Funnel:
    try:
        funnel = Funnel.model_validate(yaml.safe_load(path.read_text(encoding="utf-8-sig")))
        for step in funnel.messages:
            if step.image and not (STATIC / step.image).is_file():
                raise ConfigError(f"Нет изображения {step.image} в каталоге launch_funnel/static")
        return funnel
    except OSError as error:
        raise ConfigError(f"Не удалось прочитать конфиг {path}") from error
    except UnicodeError as error:
        raise ConfigError(f"Сохраните {path} в кодировке UTF-8") from error
    except yaml.YAMLError as error:
        raise ConfigError(f"Исправьте синтаксис YAML в {path}") from error
    except ValidationError as error:
        issues = "; ".join(
            f"{'.'.join(map(str, issue['loc'])) or 'messages'}: {issue['msg']}"
            for issue in error.errors(include_input=False)
        )
        raise ConfigError(f"Проверьте {path}: {issues}") from error


def load_environment() -> None:
    load_dotenv(Path.cwd() / ".env")
