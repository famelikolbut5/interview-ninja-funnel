import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest

from launch_funnel.config import ConfigError, read_funnel
from launch_funnel.import_sqlite import import_sqlite
from launch_funnel.migrate import MIGRATIONS, apply_migrations
from launch_funnel.storage import Storage


def test_migrations_are_serialized_and_repeatable(empty_database_url: str) -> None:
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = [pool.submit(apply_migrations, empty_database_url) for _ in range(2)]
        for task in tasks:
            task.result()
    apply_migrations(empty_database_url)
    with Storage(empty_database_url).connection() as db:
        assert db.execute("SELECT version FROM schema_migrations").fetchall() == [("001_initial.sql",)]


def test_changed_migration_is_rejected(storage: Storage, tmp_path: Path) -> None:
    source = MIGRATIONS / "001_initial.sql"
    (tmp_path / source.name).write_text(source.read_text() + "\nSELECT 1;\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="Изменена"):
        apply_migrations(storage.database_url, tmp_path)


def test_failed_migration_rolls_back_schema_and_version(storage: Storage, tmp_path: Path) -> None:
    shutil.copy2(MIGRATIONS / "001_initial.sql", tmp_path)
    (tmp_path / "002_failure.sql").write_text("CREATE TABLE partial (id INT); SELECT 1 / 0;", encoding="utf-8")
    storage.start(7, 100)
    with pytest.raises(psycopg.errors.DivisionByZero):
        apply_migrations(storage.database_url, tmp_path)
    with storage.connection() as db:
        assert db.execute("SELECT to_regclass('partial')").fetchone() == (None,)
        assert db.execute("SELECT version FROM schema_migrations").fetchall() == [("001_initial.sql",)]
        assert db.execute("SELECT user_id FROM participants").fetchall() == [(7,)]


def test_sqlite_import_preserves_data_and_cannot_overwrite_it(storage: Storage, tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as old:
        old.executescript("""
            CREATE TABLE participants (user_id INTEGER PRIMARY KEY, started_at REAL, registered_at REAL,
                phone_number TEXT, source_key TEXT, utm_source TEXT, utm_medium TEXT, utm_campaign TEXT,
                utm_content TEXT, utm_term TEXT);
            CREATE TABLE jobs (id INTEGER PRIMARY KEY, user_id INTEGER, chat_id INTEGER, step INTEGER,
                text TEXT, due_at REAL, available_at REAL, attempts INTEGER, sent_at REAL, failed_at REAL);
            INSERT INTO participants VALUES (7, 100, 101, '+15550000007', 'ninja_channel',
                'telegram', 'organic', 'webinar', 'post', 'python');
            INSERT INTO jobs VALUES (41, 7, 7, 1, 'first', 111, 111, 1, 112, NULL);
            INSERT INTO jobs VALUES (42, 7, 7, 2, 'second', 161, 161, 0, NULL, NULL);
        """)
    assert import_sqlite(path, storage) == (1, 2)
    with pytest.raises(ConfigError, match="пустую"):
        import_sqlite(path, storage)
    with storage.connection() as db:
        assert db.execute("SELECT phone_number, source_key, utm_source, utm_medium, utm_campaign, utm_content, utm_term FROM participants").fetchone() == (
            "+15550000007", "ninja_channel", "telegram", "organic", "webinar", "post", "python",
        )
        assert db.execute("SELECT id, due_at, attempts, sent_at FROM jobs ORDER BY id").fetchall() == [(41, 111, 1, 112), (42, 161, 0, None)]
    storage.start(8, 200)
    storage.register(8, 8, 201, read_funnel(Path("funnel.yaml")).messages, "+15550000008")
    with storage.connection() as db:
        assert db.execute("SELECT id FROM jobs WHERE user_id = 8 ORDER BY id").fetchall() == [(43,), (44,)]


def test_application_role_has_no_superuser_or_public_schema_creation(storage: Storage) -> None:
    with storage.connection() as db:
        assert db.execute("SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = current_user").fetchone() == (False, False, False)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute("CREATE TABLE public.forbidden_table (id INT)")
