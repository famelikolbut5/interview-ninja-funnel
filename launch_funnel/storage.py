from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum

import psycopg
from psycopg.rows import TupleRow

from .config import Attribution, FollowUp
from .migrate import apply_migrations


class Registration(Enum):
    NEW = "new"
    ALREADY_REGISTERED = "already_registered"
    START_REQUIRED = "start_required"


@dataclass(frozen=True, slots=True)
class Job:
    id: int
    chat_id: int
    step: int
    text: str
    attempts: int
    due_at: float


@dataclass(frozen=True, slots=True)
class Stats:
    started: int
    registered: int
    sent: tuple[int, int]
    pending: tuple[int, int] = (0, 0)
    failed: tuple[int, int] = (0, 0)


class BotAlreadyRunning(RuntimeError):
    pass


class Storage:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection[TupleRow]]:
        with psycopg.connect(self.database_url, connect_timeout=5) as connection:
            yield connection

    def initialize(self) -> None:
        apply_migrations(self.database_url)

    def check_ready(self) -> None:
        with self.connection() as db:
            db.execute("SELECT user_id, phone_number, utm_source FROM participants LIMIT 1")
            db.execute("SELECT id, due_at FROM jobs LIMIT 1")

    @contextmanager
    def bot_lock(self, bot_id: int) -> Iterator[None]:
        with psycopg.connect(self.database_url, autocommit=True, connect_timeout=5) as db:
            row = db.execute("SELECT pg_try_advisory_lock(%s)", (bot_id,)).fetchone()
            if row is None or not row[0]:
                raise BotAlreadyRunning
            yield

    def start(self, user_id: int, now: float, source_key: str | None = None,
              attribution: Attribution | None = None) -> bool:
        with self.connection() as db:
            db.execute(
                """INSERT INTO participants
                    (user_id, started_at, source_key, utm_source, utm_medium, utm_campaign, utm_content, utm_term)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                (user_id, now, source_key,
                 attribution.utm_source if attribution else None,
                 attribution.utm_medium if attribution else None,
                 attribution.utm_campaign if attribution else None,
                 attribution.utm_content if attribution else None,
                 attribution.utm_term if attribution else None),
            )
            row = db.execute("SELECT registered_at, phone_number FROM participants WHERE user_id = %s", (user_id,)).fetchone()
            return row is not None and row[0] is not None and row[1] is not None

    def register(self, user_id: int, chat_id: int, now: float, follow_ups: Sequence[FollowUp],
                 phone_number: str) -> Registration:
        with self.connection() as db:
            result = db.execute(
                "UPDATE participants SET registered_at = %s, phone_number = %s WHERE user_id = %s AND registered_at IS NULL",
                (now, phone_number, user_id),
            )
            if not result.rowcount:
                updated = db.execute("UPDATE participants SET phone_number = %s WHERE user_id = %s", (phone_number, user_id))
                return Registration.ALREADY_REGISTERED if updated.rowcount else Registration.START_REQUIRED
            db.cursor().executemany(
                "INSERT INTO jobs (user_id, chat_id, step, text, due_at, available_at) VALUES (%s, %s, %s, %s, %s, %s)",
                [
                    (user_id, chat_id, step, follow_up.text, now + follow_up.delay_seconds, now + follow_up.delay_seconds)
                    for step, follow_up in enumerate(follow_ups, start=1)
                ],
            )
            return Registration.NEW

    def due_jobs(self, now: float) -> list[Job]:
        with self.connection() as db:
            rows = db.execute("""
                SELECT job.id, job.chat_id, job.step, job.text, job.attempts, job.due_at
                FROM jobs AS job
                WHERE job.available_at <= %s AND job.sent_at IS NULL AND job.failed_at IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM jobs AS previous
                      WHERE previous.user_id = job.user_id AND previous.step < job.step
                        AND previous.sent_at IS NULL AND previous.failed_at IS NULL
                  )
                ORDER BY job.available_at, job.id LIMIT 25
            """, (now,))
            return [Job(int(row[0]), int(row[1]), int(row[2]), str(row[3]), int(row[4]), float(row[5])) for row in rows]

    def mark_sent(self, job_id: int, now: float) -> None:
        with self.connection() as db:
            db.execute("UPDATE jobs SET sent_at = %s WHERE id = %s", (now, job_id))

    def mark_failed(self, job_id: int, now: float) -> None:
        with self.connection() as db:
            db.execute("UPDATE jobs SET failed_at = %s, attempts = attempts + 1 WHERE id = %s", (now, job_id))

    def retry(self, job_id: int, available_at: float) -> None:
        with self.connection() as db:
            db.execute("UPDATE jobs SET available_at = %s, attempts = attempts + 1 WHERE id = %s", (available_at, job_id))

    def stats(self) -> Stats:
        with self.connection() as db:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            totals = db.execute("""
                SELECT (SELECT COUNT(*) FROM participants),
                       (SELECT COUNT(registered_at) FROM participants)
            """).fetchone()
            assert totals is not None
            started, registered = totals
            rows = db.execute("""
                SELECT step, COUNT(*) FILTER (WHERE sent_at IS NOT NULL),
                       COUNT(*) FILTER (WHERE sent_at IS NULL AND failed_at IS NULL), COUNT(*) FILTER (WHERE failed_at IS NOT NULL)
                FROM jobs GROUP BY step
            """).fetchall()
            counts = {int(row[0]): tuple(int(value) for value in row[1:]) for row in rows}
            a, b = counts.get(1, (0, 0, 0)), counts.get(2, (0, 0, 0))
            return Stats(int(started), int(registered), (a[0], b[0]), (a[1], b[1]), (a[2], b[2]))
