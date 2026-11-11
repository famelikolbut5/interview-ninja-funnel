import os
from collections.abc import Iterator
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from launch_funnel.storage import Storage


@pytest.fixture
def empty_database_url() -> Iterator[str]:
    app_url = os.environ["TEST_DATABASE_URL"]
    admin_url = os.environ["TEST_ADMIN_URL"]
    schema = "test_" + uuid4().hex
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {} AUTHORIZATION funnel").format(sql.Identifier(schema)))
        try:
            yield make_conninfo(app_url, options=f"-c search_path={schema},public -c statement_timeout=10000 -c lock_timeout=5000")
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def database_url(empty_database_url: str) -> str:
    Storage(empty_database_url).initialize()
    return empty_database_url


@pytest.fixture
def storage(database_url: str) -> Storage:
    return Storage(database_url)
