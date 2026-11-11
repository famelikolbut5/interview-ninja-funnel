import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, AsyncIterator
from urllib.parse import parse_qsl
from uuid import uuid4

import psycopg
import uvicorn
from aiogram.utils.web_app import safe_parse_webapp_init_data
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel
from starlette.middleware.base import RequestResponseEndpoint

from .config import (
    STATIC,
    ConfigError,
    Settings,
    load_environment,
    read_funnel,
    read_settings,
)
from .logging import configure_logging, error_details
from .storage import Storage


class StepStats(BaseModel):
    title: str
    delay_seconds: int
    sent: int
    pending: int
    failed: int


class DashboardStats(BaseModel):
    started: int
    registered: int
    steps: list[StepStats]
    checked_at: datetime


def authorize(settings: Settings, init_data: str, now: datetime, request_id: str) -> None:
    log = logger.bind(event="dashboard_access_denied", request_id=request_id)
    try:
        if len(init_data) > 4096:
            raise ValueError("Oversized initData")
        pairs = parse_qsl(init_data, strict_parsing=True, max_num_fields=16)
        if len(dict(pairs)) != len(pairs):
            raise ValueError("Duplicate initData fields")
        data = safe_parse_webapp_init_data(settings.bot_token, init_data)
    except (TypeError, ValueError):
        log.bind(reason="invalid_signature_or_payload").warning("Dashboard authorization rejected")
        raise HTTPException(401, "Откройте статистику из чата бота") from None
    if not now - timedelta(hours=1) <= data.auth_date <= now + timedelta(seconds=30):
        log.bind(reason="expired_or_future_auth_date").warning("Dashboard authorization rejected")
        raise HTTPException(401, "Переоткройте статистику из чата бота")
    if data.user is None or data.user.id != settings.admin_user_id:
        log.bind(reason="not_owner").warning("Dashboard authorization rejected")
        raise HTTPException(403, "Статистика доступна владельцу бота")


def create_app(settings: Settings) -> FastAPI:
    funnel = read_funnel(settings.funnel_path)
    storage = Storage(settings.database_url)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            storage.check_ready()
        except (OSError, psycopg.Error) as error:
            logger.bind(event="dashboard_startup_failed", **error_details(error)).error(
                "Database readiness check failed"
            )
            raise
        logger.bind(event="dashboard_started", url="http://127.0.0.1:8000").info("Web server started")
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/assets", StaticFiles(directory=STATIC), name="assets")

    @app.middleware("http")
    async def security_headers(request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' https://telegram.org; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/healthz", include_in_schema=False)
    async def health() -> dict[str, str]:
        try:
            await asyncio.wait_for(asyncio.to_thread(storage.check_ready), timeout=2)
        except (TimeoutError, psycopg.Error):
            raise HTTPException(503, "Storage unavailable") from None
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/stats")
    def stats(response: Response, init_data: Annotated[str, Header(alias="X-Telegram-Init-Data")] = "") -> DashboardStats:
        request_id = uuid4().hex[:12]
        now = datetime.now(UTC)
        authorize(settings, init_data, now, request_id)
        try:
            totals = storage.stats()
        except psycopg.Error as error:
            logger.bind(event="dashboard_stats_failed", request_id=request_id, **error_details(error)).error(
                "Dashboard stats query failed"
            )
            raise HTTPException(503, "Статистика временно недоступна", headers={"X-Request-ID": request_id}) from None
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Request-ID"] = request_id
        logger.bind(event="dashboard_stats_served", request_id=request_id).info("Dashboard stats served")
        return DashboardStats(
            started=totals.started,
            registered=totals.registered,
            steps=[
                StepStats(title=step.title, delay_seconds=step.delay_seconds, sent=sent, pending=pending, failed=failed)
                for step, sent, pending, failed in zip(
                    funnel.messages, totals.sent, totals.pending, totals.failed, strict=True,
                )
            ],
            checked_at=now,
        )

    return app


def main() -> None:
    load_environment()
    configure_logging("web")
    try:
        app = create_app(read_settings())
    except ConfigError as error:
        logger.bind(event="invalid_config", reason=str(error)).error("Invalid configuration")
        raise SystemExit(2) from None
    uvicorn.run(app, host=os.getenv("WEB_HOST", "127.0.0.1"), port=8000, log_config=None, access_log=False,
                proxy_headers=False, limit_concurrency=32, timeout_keep_alive=5,
                h11_max_incomplete_event_size=16384)


if __name__ == "__main__":
    main()
