import hashlib
from pathlib import Path

import psycopg
from loguru import logger

from .config import ConfigError, load_environment, read_database_url
from .logging import configure_logging, error_details

MIGRATIONS = Path(__file__).with_name("migrations")


def apply_migrations(database_url: str, directory: Path = MIGRATIONS) -> None:
    files = sorted(directory.glob("[0-9][0-9][0-9]_*.sql"))
    if not files:
        raise ConfigError("Не найдены SQL-миграции")
    with psycopg.connect(database_url, connect_timeout=5) as db:
        db.execute("SELECT pg_advisory_xact_lock(724016, 1)")
        db.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY, checksum TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )""")
        applied: dict[str, str] = dict(db.execute("SELECT version, checksum FROM schema_migrations").fetchall())
        if set(applied) - {path.name for path in files}:
            raise ConfigError("В базе есть миграции из другой версии проекта")
        for path in files:
            content = path.read_bytes()
            checksum = hashlib.sha256(content).hexdigest()
            if path.name in applied:
                if applied[path.name] != checksum:
                    raise ConfigError(f"Изменена уже применённая миграция {path.name}")
                continue
            db.execute(content.decode("utf-8"))
            db.execute("INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)", (path.name, checksum))


def main() -> None:
    load_environment()
    configure_logging("migrate")
    try:
        apply_migrations(read_database_url())
    except (ConfigError, psycopg.Error) as error:
        logger.bind(event="migration_failed", **error_details(error)).error(
            "Миграция не применена. Проверьте PostgreSQL и файлы миграций, затем повторите запуск"
        )
        raise SystemExit(1) from None
    logger.bind(event="migration_complete").info("Схема PostgreSQL обновлена")


if __name__ == "__main__":
    main()
