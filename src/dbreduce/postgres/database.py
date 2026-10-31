import hashlib
import math
import os
import re
import subprocess
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import TracebackType
from urllib.parse import quote, urlencode

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from dbreduce.cancellation import Leftover, current
from dbreduce.performance import Performance
from dbreduce.postgres.dump import restore
from dbreduce.processes import run_owned


@dataclass(frozen=True)
class DatabaseSettings:
    encoding: str
    lc_collate: str
    lc_ctype: str
    provider: str
    locale: str | None
    icu_rules: str | None


def read_settings(dsn: str) -> DatabaseSettings:
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        row = conn.execute("""
            SELECT pg_encoding_to_char(d.encoding), d.datcollate, d.datctype,
                   coalesce(to_jsonb(d)->>'datlocprovider', 'c'),
                   coalesce(to_jsonb(d)->>'datlocale',
                            to_jsonb(d)->>'daticulocale'),
                   to_jsonb(d)->>'daticurules'
            FROM pg_database d WHERE d.datname = current_database()
        """).fetchone()
    if row is None:
        raise ValueError("Cannot read source database settings")
    return DatabaseSettings(*row)


def read_archive_settings(path: Path) -> DatabaseSettings:
    """Use pg_restore's own CREATE DATABASE statement as archive metadata."""
    with path.open("rb") as archive:
        try:
            result = run_owned(
                [
                    "pg_restore",
                    "--create",
                    "--schema-only",
                    "--use-list",
                    os.devnull,
                    "--file",
                    "-",
                ],
                stdin=archive,
                timeout=600,
                env={**os.environ, "LC_ALL": "C"},
            )
        except subprocess.TimeoutExpired as error:
            raise ValueError("Timed out reading archive database settings") from error
    if result.returncode:
        if b"database name contains a newline or carriage return" in result.stderr:
            raise ValueError(
                "Custom archive has a database name containing a newline or carriage return; "
                "pg_restore cannot safely read its settings"
            )
        raise ValueError("Cannot read database settings from custom archive")
    statement = next(
        (
            line
            for line in result.stdout.splitlines()
            if line.startswith(b"CREATE DATABASE ") and b" WITH TEMPLATE = " in line
        ),
        None,
    )
    if statement is None:
        raise ValueError("Archive does not expose source database settings")

    encoding_match = re.search(rb"\bENCODING\s*=\s*'([^']+)'", statement)
    if encoding_match is None:
        raise ValueError("Archive database encoding is missing")
    encoding = encoding_match.group(1).decode("ascii")
    codec = (
        "cp" + encoding[3:]
        if encoding.startswith("WIN")
        else "ascii"
        if encoding == "SQL_ASCII"
        else encoding
    )

    def setting(name: str) -> str | None:
        match = re.search(rb"\b" + name.encode() + rb"\s*=\s*'((?:''|[^'])*)'", statement)
        if not match:
            return None
        try:
            return match.group(1).replace(b"''", b"'").decode(codec)
        except (UnicodeError, LookupError) as error:
            raise ValueError("Archive locale encoding cannot be decoded") from error

    collate = setting("LC_COLLATE") or setting("LOCALE")
    ctype = setting("LC_CTYPE") or setting("LOCALE")
    provider_match = re.search(rb"\bLOCALE_PROVIDER\s*=\s*(\w+)", statement)
    provider = {"libc": "c", "icu": "i", "builtin": "b"}.get(
        provider_match.group(1).decode("ascii") if provider_match else "libc"
    )
    if not encoding or not collate or not ctype or provider is None:
        raise ValueError("Archive database encoding/locale is incomplete")
    locale = setting("ICU_LOCALE") or setting("BUILTIN_LOCALE") or setting("LOCALE")
    return DatabaseSettings(encoding, collate, ctype, provider, locale, setting("ICU_RULES"))


def database_url(dsn: str, name: str) -> str:
    params = conninfo_to_dict(dsn)
    params.pop("dbname", None)
    user = str(params.pop("user", ""))
    password = params.pop("password", None)
    authority = quote(user, safe="")
    if password is not None:
        authority += ":" + quote(str(password), safe="")
    if authority:
        authority += "@"
    host = str(params.get("host", ""))
    # Keep socket paths and multi-host libpq settings in the query string.
    if host and "/" not in host and "," not in host:
        params.pop("host")
        authority += f"[{host}]" if ":" in host else host
        port = params.pop("port", None)
        if port is not None:
            authority += ":" + str(port)
    query = "?" + urlencode(params) if params else ""
    return f"postgresql://{authority}/{quote(name, safe='')}{query}"


def initial_database_statement(name: str, settings: DatabaseSettings | None) -> sql.Composed:
    statement = sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name))
    if settings is not None:
        statement += sql.SQL(" ENCODING {} LC_COLLATE {} LC_CTYPE {}").format(
            sql.Literal(settings.encoding),
            sql.Literal(settings.lc_collate),
            sql.Literal(settings.lc_ctype),
        )
        if settings.provider == "i":
            if settings.locale is None:
                raise ValueError("Source ICU locale is missing")
            statement += sql.SQL(" LOCALE_PROVIDER icu ICU_LOCALE {}").format(
                sql.Literal(settings.locale)
            )
            if settings.icu_rules:
                statement += sql.SQL(" ICU_RULES {}").format(sql.Literal(settings.icu_rules))
        elif settings.provider == "b":
            if settings.locale is None:
                raise ValueError("Source builtin locale is missing")
            statement += sql.SQL(" LOCALE_PROVIDER builtin BUILTIN_LOCALE {}").format(
                sql.Literal(settings.locale)
            )
        elif settings.provider != "c":
            raise ValueError(f"Unsupported locale provider: {settings.provider}")
    return statement


class CleanupStatus(str, Enum):
    """Outcome of removing one database owned by this run."""

    REMOVED = "removed"  # DROP completed; the catalog no longer lists it
    ABSENT = "already absent"  # nothing to remove
    NOT_REMOVED = "not removed"  # PostgreSQL refused or timed out; still present
    UNVERIFIED = "ownership unverified"  # a database with this name differs from the one created
    UNKNOWN = "result unknown"  # the server could not be asked, or the answer was lost


@dataclass(frozen=True)
class CleanupResult:
    status: CleanupStatus
    detail: str = ""
    error: BaseException | None = None

    @property
    def done(self) -> bool:
        return self.status in (CleanupStatus.REMOVED, CleanupStatus.ABSENT)


def statement_timeout(default_seconds: int) -> str:
    """SET statement for a cleanup statement, shortened to the remaining shutdown budget."""
    return f"SET statement_timeout = '{math.ceil(current().bounded(default_seconds))}s'"


def server_label(dsn: str) -> str:
    """host:port of a DSN for reports; never the user, password or database."""
    try:
        params = conninfo_to_dict(dsn)
    except psycopg.ProgrammingError:
        return ""
    host = str(params.get("host") or params.get("hostaddr") or "")
    port = params.get("port")
    return f"{host}:{port}" if host and port else host


def inspection_hint(dsn: str, name: str, marker: str | None) -> str:
    """How an operator can check one specific database before removing it by hand."""
    where = server_label(dsn)
    query = (
        "SELECT oid, shobj_description(oid, 'pg_database') FROM pg_database "
        f"WHERE datname = '{name}'"
    )
    if marker is None:
        proof = (
            "It has no run marker. The name is random and was generated by this run, but "
            "DBReduce does not treat a name as proof of ownership."
        )
    else:
        proof = f"It belongs to this run only if its comment is '{marker}'."
    return (f"On {where}: " if where else "") + f"{query}. {proof} Drop it only after checking."


def drop_owned_database(
    admin_dsn: str,
    name: str,
    *,
    oid: int,
    owner: int,
    marker: str,
    force: bool,
    lock_key: int | None,
    performance: Performance,
) -> CleanupResult:
    """Drop one database after matching its OID, owner and marker; say what is known.

    This is the single removal contract for workspace and clone databases. The result
    separates a confirmed drop, an already absent database, a refused drop, a name that no
    longer matches what was created, and an outcome that could not be established.
    """
    control = current()
    if control.expired():
        return CleanupResult(
            CleanupStatus.UNKNOWN, "the shutdown budget ended before the drop was attempted"
        )
    statement = sql.SQL("DROP DATABASE {}" + (" WITH (FORCE)" if force else "")).format(
        sql.Identifier(name)
    )
    dropping = False
    try:
        with psycopg.connect(
            admin_dsn, autocommit=True, connect_timeout=int(control.bounded(10))
        ) as conn:
            conn.execute(statement_timeout(600))
            if lock_key is not None:
                conn.execute("SELECT pg_advisory_lock(%s)", (lock_key,))
            row = conn.execute(
                "SELECT oid, datdba, shobj_description(oid, 'pg_database') "
                "FROM pg_database WHERE datname = %s",
                (name,),
            ).fetchone()
            if row is None:
                return CleanupResult(CleanupStatus.ABSENT)
            if (row[0], row[1], row[2]) != (oid, owner, marker):
                return CleanupResult(
                    CleanupStatus.UNVERIFIED,
                    "a database with this name exists but its OID, owner or marker differ",
                )
            dropping = True
            with performance.measure("database_cleanup"):
                conn.execute(statement)
            return CleanupResult(CleanupStatus.REMOVED)
    except psycopg.Error as error:
        state = getattr(error, "sqlstate", None)
        if not dropping:
            if state is None:
                return CleanupResult(
                    CleanupStatus.UNKNOWN, "the server could not be reached", error
                )
            return CleanupResult(
                CleanupStatus.NOT_REMOVED, f"cleanup statement failed (SQLSTATE {state})", error
            )
        # The server may have finished the DROP even though the reply was lost.
        try:
            with psycopg.connect(
                admin_dsn, autocommit=True, connect_timeout=int(control.bounded(10))
            ) as conn:
                present = conn.execute(
                    "SELECT oid FROM pg_database WHERE datname = %s", (name,)
                ).fetchone()
        except psycopg.Error:
            return CleanupResult(
                CleanupStatus.UNKNOWN,
                "the DROP reply was lost and the server could not be checked",
                error,
            )
        if present is None or present[0] != oid:
            return CleanupResult(CleanupStatus.REMOVED, "confirmed by a catalog check", error)
        if state is not None:
            return CleanupResult(
                CleanupStatus.NOT_REMOVED, f"PostgreSQL rejected the DROP (SQLSTATE {state})", error
            )
        return CleanupResult(
            CleanupStatus.UNKNOWN,
            "the DROP reply was lost and the database was still listed when checked",
            error,
        )


def settle_unproven_create(admin_dsn: str, name: str) -> bool | None:
    """Resolve an ambiguous CREATE DATABASE: True if it exists, False if absent, else None.

    The creating session names itself after the database. A statement this run left running
    on the server is cancelled first, so the database cannot appear after the answer.
    """
    control = current()
    try:
        with psycopg.connect(
            admin_dsn, autocommit=True, connect_timeout=int(control.bounded(10))
        ) as conn:
            conn.execute(statement_timeout(30))
            give_up = time.monotonic() + control.bounded(10)
            while True:
                pids = list(
                    conn.execute(
                        "SELECT pid FROM pg_stat_activity "
                        "WHERE application_name = %s AND pid <> pg_backend_pid()",
                        (name,),
                    ).fetchall()
                )
                if not pids:
                    break
                for (pid,) in pids:
                    conn.execute("SELECT pg_cancel_backend(%s)", (pid,))
                if time.monotonic() >= give_up:
                    return None
                time.sleep(0.1)
            row = conn.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = %s)", (name,)
            ).fetchone()
    except psycopg.Error:
        return None
    return None if row is None else bool(row[0])


class Workspace:
    """Own exactly one random database; no destructive method accepts a user database name."""

    def __init__(
        self,
        admin_dsn: str,
        settings: DatabaseSettings | None = None,
        performance: Performance | None = None,
        restore_jobs: int = 1,
    ) -> None:
        self.admin_dsn = admin_dsn
        self.name = f"dbreduce_{uuid.uuid4().hex}"
        self.lock_key = int.from_bytes(
            hashlib.sha256(self.name.encode()).digest()[:8], "big", signed=True
        )
        self.dsn = make_conninfo(admin_dsn, dbname=self.name)
        self.url = database_url(admin_dsn, self.name)
        self.settings = settings
        self.created = False
        self.owner: int | None = None
        self.oid: int | None = None
        self.marker = "dbreduce-workspace:" + uuid.uuid4().hex
        self.unproven = False
        self.databases_created = 0
        self.databases_dropped = 0
        self.databases_absent = 0
        self.cleanup_failures = 0
        self.last_cleanup: CleanupResult | None = None
        self.performance = performance or Performance()
        self.restore_jobs = restore_jobs
        current().register(self.leftovers)

    def __enter__(self) -> "Workspace":
        return self

    def leftovers(self) -> list[Leftover]:
        if self.created:
            result = self.last_cleanup
            state = result.status.value if result is not None else "removal not attempted"
            detail = result.detail if result is not None else ""
            hint = inspection_hint(self.admin_dsn, self.name, self.marker)
            return [Leftover("database", self.name, state, ". ".join(filter(None, (detail, hint))))]
        if self.unproven:
            return [
                Leftover(
                    "database",
                    self.name,
                    "creation outcome unverified",
                    inspection_hint(self.admin_dsn, self.name, None),
                )
            ]
        return []

    def create(self) -> None:
        self.close()
        statement = initial_database_statement(self.name, self.settings)
        try:
            # The session is named after the database so an unfinished CREATE can be found.
            with psycopg.connect(
                self.admin_dsn,
                autocommit=True,
                connect_timeout=10,
                application_name=self.name,
            ) as conn:
                conn.execute("SET statement_timeout = '600s'")
                conn.execute("SELECT pg_advisory_lock(%s)", (self.lock_key,))
                # A lost CREATE reply is ambiguous. A name alone is never ownership proof.
                self.unproven = True
                with self.performance.measure("database_create"):
                    conn.execute(statement)
                # A signal must not split recording the database from marking it as ours.
                with current().protected():
                    row = conn.execute(
                        "SELECT oid, datdba FROM pg_database WHERE datname = %s", (self.name,)
                    ).fetchone()
                    if row is None:
                        raise RuntimeError(f"Cannot verify created workspace database {self.name}")
                    conn.execute(
                        sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                            sql.Identifier(self.name), sql.Literal(self.marker)
                        )
                    )
                    self.oid, self.owner = int(row[0]), int(row[1])
                    self.created = True
                    self.unproven = False
                    self.last_cleanup = None  # a result about an earlier database
                    self.databases_created += 1
        except psycopg.errors.DuplicateDatabase:
            self.unproven = False
            raise
        except BaseException as error:
            # SQLSTATE proves PostgreSQL rejected the statement. A transport failure does not.
            if getattr(error, "sqlstate", None) is not None:
                try:
                    with psycopg.connect(self.admin_dsn, connect_timeout=10) as conn:
                        row = conn.execute(
                            "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = %s)",
                            (self.name,),
                        ).fetchone()
                    if row is not None and not row[0]:
                        self.unproven = False
                except psycopg.Error:
                    pass
            if self.unproven:
                raise RuntimeError(
                    f"Creation outcome for workspace database {self.name} is unverified; "
                    "inspect it manually before removal"
                ) from error
            raise

    def reset(self, snapshot: Path, *, use_list: Path | None = None) -> None:
        self.create()
        with self.performance.measure("restore"):
            restore(self.dsn, snapshot, jobs=self.restore_jobs, use_list=use_list)

    def close(self) -> None:
        # Cancellation is delivered after cleanup, never in the middle of it.
        with current().protected():
            self._close()

    def _close(self) -> None:
        if self.created:
            assert self.oid is not None and self.owner is not None
            result = drop_owned_database(
                self.admin_dsn,
                self.name,
                oid=self.oid,
                owner=self.owner,
                marker=self.marker,
                force=True,
                lock_key=self.lock_key,
                performance=self.performance,
            )
            self.last_cleanup = result
            if not result.done:
                self.cleanup_failures += 1
                if result.status is CleanupStatus.UNVERIFIED:
                    raise RuntimeError(
                        f"Ownership of workspace database {self.name} cannot be verified; "
                        "inspect it manually before removal"
                    )
                raise RuntimeError(
                    f"Could not confirm cleanup of workspace database {self.name} "
                    f"({result.detail}); inspect it before any manual removal"
                ) from result.error
            self.created = False
            self.oid = self.owner = None
            current().record_cleanup(result.status.value)
            if result.status is CleanupStatus.REMOVED:
                self.databases_dropped += 1
            else:
                self.databases_absent += 1
        if self.unproven:
            exists = settle_unproven_create(self.admin_dsn, self.name)
            if exists is False:
                self.unproven = False
                return
            self.cleanup_failures += 1
            if exists is None:
                raise RuntimeError(
                    f"Could not resolve creation outcome for workspace database {self.name}; "
                    "inspect it manually before removal"
                )
            raise RuntimeError(
                f"Creation outcome for workspace database {self.name} is unverified; "
                "inspect it manually before removal"
            )

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            self.close()
        except RuntimeError as cleanup_error:
            if isinstance(exc, Exception):
                original = (
                    "PostgreSQL operation failed; check connection and permissions"
                    if isinstance(exc, psycopg.Error)
                    else str(exc) or type(exc).__name__
                )
                raise RuntimeError(
                    f"Original failure: {original}; {cleanup_error}"
                ) from cleanup_error
            raise
