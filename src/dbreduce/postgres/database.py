import hashlib
import os
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from urllib.parse import quote, urlencode

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from dbreduce.performance import Performance
from dbreduce.postgres.dump import restore


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
            result = subprocess.run(
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
                capture_output=True,
                timeout=600,
                env={**os.environ, "LC_ALL": "C"},
                check=False,
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
        self.cleanup_failures = 0
        self.performance = performance or Performance()
        self.restore_jobs = restore_jobs

    def __enter__(self) -> "Workspace":
        return self

    def create(self) -> None:
        self.close()
        statement = initial_database_statement(self.name, self.settings)
        try:
            with psycopg.connect(self.admin_dsn, autocommit=True, connect_timeout=10) as conn:
                conn.execute("SET statement_timeout = '600s'")
                conn.execute("SELECT pg_advisory_lock(%s)", (self.lock_key,))
                # A lost CREATE reply is ambiguous. A name alone is never ownership proof.
                self.unproven = True
                with self.performance.measure("database_create"):
                    conn.execute(statement)
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
        if self.created:
            try:
                with psycopg.connect(self.admin_dsn, autocommit=True, connect_timeout=10) as conn:
                    conn.execute("SET statement_timeout = '600s'")
                    conn.execute("SELECT pg_advisory_lock(%s)", (self.lock_key,))
                    row = conn.execute(
                        "SELECT oid, datdba, shobj_description(oid, 'pg_database') "
                        "FROM pg_database WHERE datname = %s",
                        (self.name,),
                    ).fetchone()
                    if row is None or (row[0], row[1], row[2]) != (
                        self.oid,
                        self.owner,
                        self.marker,
                    ):
                        raise RuntimeError(
                            f"Ownership of workspace database {self.name} cannot be verified; "
                            "inspect it manually before removal"
                        )
                    with self.performance.measure("database_cleanup"):
                        conn.execute(
                            sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                                sql.Identifier(self.name)
                            )
                        )
            except RuntimeError:
                self.cleanup_failures += 1
                raise
            except psycopg.Error as error:
                self.cleanup_failures += 1
                raise RuntimeError(
                    f"Could not confirm cleanup of workspace database {self.name}; "
                    "inspect and remove it if present"
                ) from error
            self.created = False
            self.oid = self.owner = None
            self.databases_dropped += 1
        if self.unproven:
            try:
                with psycopg.connect(self.admin_dsn, connect_timeout=10) as conn:
                    row = conn.execute(
                        "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = %s)",
                        (self.name,),
                    ).fetchone()
            except psycopg.Error as error:
                self.cleanup_failures += 1
                raise RuntimeError(
                    f"Could not resolve creation outcome for workspace database {self.name}; "
                    "inspect it manually before removal"
                ) from error
            if row is None or row[0]:
                self.cleanup_failures += 1
                raise RuntimeError(
                    f"Creation outcome for workspace database {self.name} is unverified; "
                    "inspect it manually before removal"
                )
            self.unproven = False

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
