"""Run in the NetBox v4.1.1 environment, before django.setup()."""

import json
import os
import sys
from pathlib import Path

import psycopg

# This form path requires these tables even when they contain no rows.
required = (
    "public.dcim_manufacturer",
    "public.django_content_type",
    "public.extras_customfield",
    "public.extras_customfield_object_types",
)
url = os.environ["DATABASE_URL"]
with psycopg.connect(url, connect_timeout=10) as connection:
    connection.execute("SET TRANSACTION READ ONLY")
    missing = any(
        connection.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0] is None
        for table in required
    )
    if not missing:
        # Use the actual connection, including libpq defaults and environment resolution.
        os.environ.update(
            DB_NAME=connection.info.dbname,
            DB_HOST=connection.info.host,
            DB_PORT=str(connection.info.port),
            DB_USER=connection.info.user,
            DB_PASSWORD=connection.info.password,
        )

if missing:
    print(json.dumps({"reproduced": False, "outcome": "candidate_invalid"}))
    raise SystemExit(0)

# NetBox's Docker settings now point at the same database as the preflight.
os.execv(sys.executable, [sys.executable, str(Path(__file__).with_name("oracle.py"))])
