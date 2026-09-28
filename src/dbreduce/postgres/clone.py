import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, cast

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from dbreduce.models.schema import Schema, State
from dbreduce.performance import Performance
from dbreduce.postgres.database import (
    DatabaseSettings,
    database_url,
    initial_database_statement,
)
from dbreduce.postgres.dump import restore
from dbreduce.postgres.rows import read_rows


@dataclass
class OwnedDatabase:
    name: str
    oid: int
    owner: int
    frozen: bool = False


def check_clone_source(conn: psycopg.Connection[tuple[Any, ...]]) -> None:
    checks = {
        "publications": "SELECT EXISTS (SELECT 1 FROM pg_publication)",
        "subscriptions": "SELECT EXISTS (SELECT 1 FROM pg_subscription "
        "WHERE subdbid = (SELECT oid FROM pg_database WHERE datname = current_database()))",
        "logical replication slots": "SELECT EXISTS (SELECT 1 FROM pg_replication_slots "
        "WHERE database = current_database() AND slot_type = 'logical')",
        "prepared transactions": "SELECT EXISTS (SELECT 1 FROM pg_prepared_xacts "
        "WHERE database = current_database())",
    }
    try:
        for label, query in checks.items():
            row = conn.execute(query).fetchone()
            assert row is not None
            if row[0]:
                raise ValueError(
                    f"Clone backend does not support {label}; use snapshot backend"
                )
    except psycopg.Error as error:
        raise ValueError(
            "Cannot verify replication and prepared-transaction preflight; "
            "use snapshot backend"
        ) from error


class CloneStore:
    """Track each database by a creation response, OID, owner and run marker."""

    def __init__(
        self,
        admin_dsn: str,
        settings: DatabaseSettings,
        performance: Performance,
        *,
        strategy: str = "wal_log",
        file_copy_method: str | None = None,
        restore_jobs: int = 1,
    ) -> None:
        if strategy not in ("wal_log", "file_copy"):
            raise ValueError("Clone strategy must be wal_log or file_copy")
        if file_copy_method not in (None, "copy", "clone"):
            raise ValueError("File copy method must be copy or clone")
        if file_copy_method is not None and strategy != "file_copy":
            raise ValueError("File copy method requires file_copy strategy")
        self.admin_dsn = admin_dsn
        self.settings = settings
        self.performance = performance
        self.strategy = strategy
        self.requested_file_copy_method = file_copy_method
        self.restore_jobs = restore_jobs
        self.marker = "dbreduce-run:" + uuid.uuid4().hex
        self.owned: dict[str, OwnedDatabase] = {}
        self.unproven: set[str] = set()
        self.created = 0
        self.dropped = 0
        self.clone_attempts = 0
        self.clone_failures = 0
        self.cleanup_failures = 0
        with self._admin() as conn:
            version_row = conn.execute("SHOW server_version_num").fetchone()
            assert version_row is not None
            self.server_version = int(cast(str, version_row[0]))
            row = conn.execute(
                "SELECT setting FROM pg_settings WHERE name = 'file_copy_method'"
            ).fetchone()
            self.server_file_copy_method = cast(str, row[0]).lower() if row else None
        if self.server_version < 170000:
            raise ValueError("Clone backend requires PostgreSQL 17 or later")
        if file_copy_method is not None and self.server_file_copy_method is None:
            raise ValueError("file_copy_method requires PostgreSQL 18")
        self.clone_probe: str | None = None

    def probe_file_copy_clone(self) -> None:
        if self.strategy != "file_copy" or (
            self.requested_file_copy_method != "clone"
            and self.server_file_copy_method != "clone"
        ):
            return
        probe = None
        try:
            probe = self._create(
                lambda name: sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                    sql.Identifier(name)
                )
            )
            self.freeze(probe)
            copy = self.clone(probe)
            self.drop(copy)
            self.clone_probe = "passed"
        except psycopg.Error as error:
            self.clone_probe = "unsupported"
            raise ValueError(
                "PostgreSQL could not use file_copy_method=CLONE on this server/filesystem"
            ) from error
        finally:
            if probe is not None:
                self.drop(probe)

    def _admin(self) -> psycopg.Connection[tuple[object, ...]]:
        return psycopg.connect(self.admin_dsn, autocommit=True, connect_timeout=10)

    def dsn(self, database: OwnedDatabase) -> str:
        return make_conninfo(self.admin_dsn, dbname=database.name)

    def url(self, database: OwnedDatabase) -> str:
        return database_url(self.admin_dsn, database.name)

    def _create(
        self, statement: Callable[[str], sql.SQL | sql.Composed], *, clone: bool = False
    ) -> OwnedDatabase:
        name = f"dbreduce_{uuid.uuid4().hex}"
        query = statement(name)
        if clone:
            self.clone_attempts += 1
        try:
            with self._admin() as conn:
                conn.execute("SET statement_timeout = '600s'")
                if self.requested_file_copy_method is not None:
                    conn.execute(
                        sql.SQL("SET file_copy_method TO {}").format(
                            sql.SQL(self.requested_file_copy_method.upper())
                        )
                    )
                # A missing reply can mean CREATE committed. Keep the exact name for inspection.
                self.unproven.add(name)
                with self.performance.measure("database_clone" if clone else "database_create"):
                    conn.execute(query)
                row = conn.execute(
                    "SELECT d.oid, d.datdba FROM pg_database d WHERE d.datname = %s",
                    (name,),
                ).fetchone()
                if row is None:
                    raise RuntimeError(f"Cannot verify created database {name}")
                conn.execute(
                    sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                        sql.Identifier(name), sql.Literal(self.marker)
                    )
                )
                database = OwnedDatabase(name, cast(int, row[0]), cast(int, row[1]))
                self.owned[name] = database
                self.unproven.remove(name)
                self.created += 1
                return database
        except psycopg.errors.DuplicateDatabase:
            self.unproven.discard(name)
            if clone:
                self.clone_failures += 1
            raise
        except BaseException as error:
            if clone:
                self.clone_failures += 1
            if name in self.unproven:
                # A PostgreSQL ErrorResponse with SQLSTATE means CREATE was rejected. Check
                # by exact generated name before deciding the outcome is still ambiguous.
                if isinstance(error, psycopg.Error) and error.sqlstate is not None:
                    try:
                        with self._admin() as conn:
                            row = conn.execute(
                                "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = %s)",
                                (name,),
                            ).fetchone()
                        if row is not None and not row[0]:
                            self.unproven.discard(name)
                    except psycopg.Error:
                        pass
                if name not in self.unproven:
                    raise error
                raise RuntimeError(
                    f"Creation outcome for database {name} is unverified; "
                    "inspect it manually before removal"
                ) from error
            raise

    def create_initial(self, snapshot: Path, schema: Schema) -> tuple[OwnedDatabase, State]:
        database = self._create(
            lambda name: initial_database_statement(name, self.settings)
        )
        with self.performance.measure("restore"):
            restore(self.dsn(database), snapshot, jobs=self.restore_jobs)
        with psycopg.connect(self.dsn(database), connect_timeout=10) as conn:
            check_clone_source(conn)
            with self.performance.measure("accepted_state_read"):
                initial_state, _ = read_rows(conn, schema)
        self.freeze(database)
        return database, initial_state

    def _verify(self, database: OwnedDatabase) -> None:
        if self.owned.get(database.name) is not database:
            raise RuntimeError(f"Database {database.name} is not tracked by this run")
        try:
            with self._admin() as conn:
                row = conn.execute(
                    "SELECT oid, datdba, shobj_description(oid, 'pg_database'), datallowconn "
                    "FROM pg_database WHERE datname = %s",
                    (database.name,),
                ).fetchone()
        except psycopg.Error as error:
            raise RuntimeError(
                f"Could not verify ownership of database {database.name}; "
                "inspect it manually before removal"
            ) from error
        if row is None or (row[0], row[1], row[2]) != (
            database.oid, database.owner, self.marker
        ):
            raise RuntimeError(
                f"Ownership of database {database.name} cannot be verified; "
                "inspect it manually before removal"
            )

    def freeze(self, database: OwnedDatabase) -> None:
        self._verify(database)
        with self._admin() as conn:
            conn.execute(
                sql.SQL("ALTER DATABASE {} WITH ALLOW_CONNECTIONS false").format(
                    sql.Identifier(database.name)
                )
            )
        self._wait_quiescent(database)
        database.frozen = True

    def _wait_quiescent(self, database: OwnedDatabase) -> None:
        deadline = time.monotonic() + 10
        with self.performance.measure("clone_source_quiescence_wait"):
            while True:
                with self._admin() as conn:
                    sessions = conn.execute(
                        "SELECT backend_type FROM pg_stat_activity WHERE datid = %s",
                        (database.oid,),
                    ).fetchall()
                if not sessions:
                    return
                if any(row[0] != "autovacuum worker" for row in sessions):
                    raise RuntimeError(
                        f"Database {database.name} has unexpected sessions; "
                        "DBReduce will not terminate them"
                    )
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"Autovacuum on database {database.name} did not become quiescent"
                    )
                time.sleep(0.1)

    def clone(self, source: OwnedDatabase) -> OwnedDatabase:
        self._verify(source)
        if not source.frozen:
            raise RuntimeError(f"Clone source {source.name} is not frozen")
        with self._admin() as conn:
            row = conn.execute(
                "SELECT datallowconn FROM pg_database WHERE oid = %s", (source.oid,)
            ).fetchone()
        if row is None or row[0]:
            raise RuntimeError(
                f"Clone source {source.name} is not quiescent; "
                "DBReduce will not terminate other sessions"
            )
        self._wait_quiescent(source)
        return self._create(
            lambda name: sql.SQL("CREATE DATABASE {} WITH TEMPLATE {} STRATEGY {}").format(
                sql.Identifier(name),
                sql.Identifier(source.name),
                sql.SQL(self.strategy.upper()),
            ),
            clone=True,
        )

    def drop(self, database: OwnedDatabase) -> None:
        try:
            self._verify(database)
            with self._admin() as conn:
                conn.execute("SET statement_timeout = '600s'")
                with self.performance.measure("database_cleanup"):
                    conn.execute(
                        sql.SQL("DROP DATABASE {}").format(sql.Identifier(database.name))
                    )
        except RuntimeError:
            self.cleanup_failures += 1
            raise
        except psycopg.Error as error:
            self.cleanup_failures += 1
            raise RuntimeError(
                f"Could not clean up database {database.name}; inspect sessions, "
                "prepared transactions, logical slots and subscriptions, then remove it manually"
            ) from error
        self.owned.pop(database.name)
        self.dropped += 1

    def close(self) -> None:
        failures = []
        for database in list(self.owned.values())[::-1]:
            try:
                self.drop(database)
            except (RuntimeError, KeyboardInterrupt) as error:
                failures.append(
                    f"Could not confirm cleanup of database {database.name}: "
                    f"{str(error) or type(error).__name__}; inspect it before manual removal"
                )
        if self.unproven:
            failures.append(
                "Unverified creation outcome for: " + ", ".join(sorted(self.unproven))
            )
        if failures:
            raise RuntimeError("; ".join(failures))

    def __enter__(self) -> "CloneStore":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            self.close()
        except RuntimeError as error:
            if exc is not None:
                raise RuntimeError(f"Reduction failed; {error}") from error
            raise
