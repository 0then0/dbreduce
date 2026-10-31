import os
from functools import lru_cache

import psycopg
import pytest


@lru_cache(maxsize=1)
def server_major(admin: str) -> int:
    with psycopg.connect(admin, connect_timeout=10) as conn:
        row = conn.execute("SELECT current_setting('server_version_num')::int / 10000").fetchone()
    assert row is not None
    return int(row[0])


@pytest.fixture(autouse=True)
def _clone_backend_needs_postgres_15(request):
    """The clone backend relies on CREATE DATABASE ... STRATEGY, new in PostgreSQL 15."""
    admin = os.environ.get("DBREDUCE_TEST_ADMIN")
    if (
        admin
        and request.node.get_closest_marker("postgres")
        and "clone" in request.node.name
        and server_major(admin) < 15
    ):
        pytest.skip("The clone backend requires PostgreSQL 15 or later")
