import asyncio
import json
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.methods import SendMessage
from httpx import ASGITransport, AsyncClient
from loguru import logger
from test_dashboard import signed_init_data

from launch_funnel import delivery
from launch_funnel.config import ConfigError, Settings, read_funnel, read_mini_app_url
from launch_funnel.logging import configure_logging
from launch_funnel.storage import BotAlreadyRunning, Job, Storage
from launch_funnel.web import create_app

CONFIG = Path(__file__).resolve().parents[1] / "funnel.yaml"
TOKEN = "12345:test-token"


@pytest.mark.parametrize("failure", ["network", "rate_limit", "bad_request"])
def test_retries_are_bounded_and_failures_visible(storage: Storage, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    db = storage
    db.start(7, 0)
    db.register(7, 7, 0, read_funnel(CONFIG).messages, "+15550000007")
    clock = [10.0]
    monkeypatch.setattr(delivery, "time", lambda: clock[0])
    monkeypatch.setattr(delivery, "uniform", lambda _a, _b: 0)
    calls = 0

    async def send(job: Job) -> None:
        nonlocal calls
        calls += 1
        method = SendMessage(chat_id=job.chat_id, text=job.text)
        if failure == "network":
            raise TelegramNetworkError(method, "network")
        if failure == "rate_limit":
            raise TelegramRetryAfter(method, "retry later", 1)
        raise TelegramBadRequest(method, "invalid request")

    async def run() -> None:
        worker = delivery.Delivery(db, send)
        for _ in range(5):
            await worker.deliver_due()
            clock[0] += 9

    asyncio.run(run())
    assert calls == (1 if failure == "bad_request" else 4)
    assert db.stats().failed == (1, 0)
    assert db.stats().pending == (0, 1)
    assert db.stats().sent == (0, 0)


def test_registration_rolls_back_with_schedule_failure(storage: Storage) -> None:
    db = storage
    db.start(7, 0)
    with db.connection() as connection:
        connection.execute("ALTER TABLE jobs ADD CONSTRAINT fail_registration CHECK (step < 1)")
    with pytest.raises(psycopg.errors.CheckViolation):
        db.register(7, 7, 1, read_funnel(CONFIG).messages, "+15550000007")
    assert db.stats().registered == 0
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchall()[0][0] == 0
        assert connection.execute("SELECT registered_at, phone_number FROM participants").fetchone() == (None, None)


def test_duplicate_worker_lock_is_released(storage: Storage) -> None:
    with storage.bot_lock(7):
        with pytest.raises(BotAlreadyRunning):
            with storage.bot_lock(7):
                pytest.fail("Second worker acquired the lock")
    with storage.bot_lock(7):
        pass


@pytest.mark.parametrize("url", ["http://localhost", "https://user:password@example.org", "javascript:alert(1)", "",
                                 "https://example.org:bad", "https://bad host.example", "https://example.org/\nbad"])
def test_mini_app_rejects_insecure_urls(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("MINI_APP_URL", url)
    with pytest.raises(ConfigError):
        read_mini_app_url()


def test_logs_keep_diagnostics_without_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", TOKEN)
    monkeypatch.setenv("DB_PASSWORD", "database-secret")
    monkeypatch.setenv("LOG_DIRECTORY", str(tmp_path))
    configure_logging("test")
    logger.bind(event="check", job_id=17, attempt=2, reason="network",
                transport={TOKEN: {"url": f"https://api.telegram.org/bot{TOKEN}/sendPhoto", "retry": [TOKEN]}}).error(
        f"Transport failed {TOKEN} database-secret"
    )
    logger.remove()
    content = (tmp_path / "test.jsonl").read_text(encoding="utf-8")
    assert TOKEN not in content
    assert "database-secret" not in content
    record = json.loads(content)["record"]
    assert record["extra"]["job_id"] == 17
    assert record["extra"]["attempt"] == 2
    assert record["file"]["name"] == "test_resilience.py"
    assert record["line"] > 0


def test_log_rotation_keeps_three_archives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_DIRECTORY", str(tmp_path))
    with open(os.devnull, "w") as sink:
        monkeypatch.setattr(sys, "stderr", sink)
        configure_logging("rotation")
        try:
            for sequence in range(14):
                logger.bind(sequence=sequence).info("x" * 800_000)
        finally:
            logger.remove()
    files = list(tmp_path.glob("rotation*.jsonl"))
    assert len(files) == 4
    assert all(path.stat().st_size <= 5_000_000 for path in files)
    records = [json.loads(line)["record"] for path in files for line in path.read_text(encoding="utf-8").splitlines()]
    assert max(record["extra"]["sequence"] for record in records) == 13


def test_api_rejects_malformed_sessions_and_surfaces_storage_failure(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app(Settings(TOKEN, 7, database_url, CONFIG))

    async def run() -> None:
        async with app.router.lifespan_context(app), AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            for value in ["x" * 4097, "broken", signed_init_data(7, age=timedelta(minutes=-5)),
                          signed_init_data(7) + "&user=invalid"]:
                response = await client.get("/api/stats", headers={"X-Telegram-Init-Data": value})
                assert response.status_code == 401
                assert response.headers["cache-control"] == "no-store"
            assert (await client.get("/healthz")).status_code == 200
            page = await client.get("/")
            assert page.headers["x-content-type-options"] == "nosniff"
            assert "frame-ancestors" in page.headers["content-security-policy"]
            assert (await client.get("/assets/../.env")).status_code == 404

            def broken_stats(_self: Storage) -> None:
                raise psycopg.OperationalError("test")

            monkeypatch.setattr(Storage, "stats", broken_stats)
            failed = await client.get("/api/stats", headers={"X-Telegram-Init-Data": signed_init_data(7)})
            assert failed.status_code == 503
            assert len(failed.headers["x-request-id"]) == 12
            assert "test" not in failed.text

    asyncio.run(run())


def test_health_has_a_deadline_when_database_resolution_stalls(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app(Settings(TOKEN, 7, database_url, CONFIG))

    async def run() -> None:
        async with app.router.lifespan_context(app), AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            monkeypatch.setattr(Storage, "check_ready", lambda _self: time.sleep(2.5))
            started = time.monotonic()
            assert (await client.get("/healthz")).status_code == 503
            assert time.monotonic() - started < 2.4

    asyncio.run(run())
