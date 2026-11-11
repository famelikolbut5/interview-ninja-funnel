import argparse
import sqlite3
from pathlib import Path

import psycopg
from loguru import logger
from psycopg import sql

from .config import ConfigError, load_environment, read_database_url
from .logging import configure_logging, error_details
from .storage import Storage

PARTICIPANT_COLUMNS = (
    "user_id", "started_at", "registered_at", "phone_number", "source_key",
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
)
JOB_COLUMNS = (
    "id", "user_id", "chat_id", "step", "text", "due_at", "available_at", "attempts", "sent_at", "failed_at",
)


def import_sqlite(source: Path, storage: Storage) -> tuple[int, int]:
    with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as old:
        old.row_factory = sqlite3.Row
        participants = [dict(row) for row in old.execute("SELECT * FROM participants ORDER BY user_id")]
        jobs = [dict(row) for row in old.execute("SELECT * FROM jobs ORDER BY id")]
    with storage.connection() as db:
        db.execute("LOCK TABLE participants, jobs IN ACCESS EXCLUSIVE MODE")
        if db.execute("SELECT 1 FROM participants UNION ALL SELECT 1 FROM jobs LIMIT 1").fetchone():
            raise ConfigError("Импорт разрешён только в пустую базу")
        for table, columns, rows in (("participants", PARTICIPANT_COLUMNS, participants), ("jobs", JOB_COLUMNS, jobs)):
            query = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, columns)),
                sql.SQL(", ").join(sql.Placeholder() for _ in columns),
            )
            db.cursor().executemany(query, [tuple(row.get(column) for column in columns) for row in rows])
        db.execute("""SELECT setval(pg_get_serial_sequence('jobs', 'id'),
            COALESCE((SELECT max(id) FROM jobs), 1), EXISTS(SELECT 1 FROM jobs))""")
    return len(participants), len(jobs)


def main() -> None:
    parser = argparse.ArgumentParser(description="Перенос SQLite в пустую PostgreSQL")
    parser.add_argument("source", type=Path)
    source = parser.parse_args().source
    load_environment()
    configure_logging("import")
    try:
        participants, jobs = import_sqlite(source, Storage(read_database_url()))
    except (ConfigError, sqlite3.Error, psycopg.Error) as error:
        logger.bind(event="import_failed", **error_details(error)).error(
            "SQLite import failed"
        )
        raise SystemExit(1) from None
    logger.bind(event="import_complete", participants=participants, jobs=jobs).info("SQLite import completed")


if __name__ == "__main__":
    main()
