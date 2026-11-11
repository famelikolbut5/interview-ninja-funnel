import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from httpx import ASGITransport, AsyncClient

from launch_funnel.config import Settings, read_funnel
from launch_funnel.storage import Storage
from launch_funnel.web import create_app

TOKEN = "12345:test-token"
CONFIG = Path(__file__).resolve().parents[1] / "funnel.yaml"


def signed_init_data(user_id: int, *, age: timedelta = timedelta()) -> str:
    fields = {
        "auth_date": str(int((datetime.now(UTC) - age).timestamp())),
        "query_id": "local-check",
        "user": json.dumps({"id": user_id, "first_name": "Demo"}),
    }
    canonical = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.digest(b"WebAppData", TOKEN.encode(), hashlib.sha256)
    fields["hash"] = hmac.digest(secret, canonical.encode(), hashlib.sha256).hex()
    return urlencode(fields)


def test_dashboard_requires_a_fresh_signed_owner_session_and_reads_actual_stats(database_url: str) -> None:
    app = create_app(Settings(TOKEN, 7, database_url, CONFIG))

    async def run() -> None:
        async with app.router.lifespan_context(app), AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            assert (await client.get("/")).status_code == 200
            assert (await client.get("/register")).status_code == 404
            for private_path in ("/.env", "/assets/%2e%2e/.env", "/assets/%2e%2e%2f.env", "/assets/..%5c.env",
                                 "/data/interview-ninja.sqlite3", "/logs/bot.jsonl", "/docs", "/openapi.json"):
                assert (await client.get(private_path)).status_code == 404
            assert Storage(database_url).stats().started == 0
            assert (await client.get("/api/stats")).status_code == 401
            header = "X-Telegram-Init-Data"
            assert (await client.get("/api/stats", headers={header: signed_init_data(8)})).status_code == 403
            expired = signed_init_data(7, age=timedelta(hours=2))
            assert (await client.get("/api/stats", headers={header: expired})).status_code == 401
            signed = signed_init_data(7)
            tampered = signed.replace("Demo", "Altered")
            assert (await client.get("/api/stats", headers={header: tampered})).status_code == 401

            db = Storage(database_url)
            db.start(7, 100)
            db.start(8, 100)
            db.register(7, 7, 101, read_funnel(CONFIG).messages, "+15550000007")
            job, = db.due_jobs(111)
            db.mark_sent(job.id, 111)
            response = await client.get("/api/stats", headers={header: signed})
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            data = response.json()
            assert (data["started"], data["registered"]) == (2, 1)
            assert [(step["delay_seconds"], step["sent"]) for step in data["steps"]] == [(10, 1), (60, 0)]
            with db.connection() as connection:
                connection.execute("DROP TABLE jobs")
            assert (await client.get("/healthz")).status_code == 503

    asyncio.run(run())
