import re
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from urllib.parse import quote, urlencode

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

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
    with path.open("rb") as archive, tempfile.TemporaryFile() as generated:
        try:
            result = subprocess.run(
                ["pg_restore", "--create", "--schema-only", "--file", "-"],
                stdin=archive,
                stdout=generated,
                stderr=subprocess.DEVNULL,
                timeout=600,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise ValueError("Timed out reading archive database settings") from error
        if result.returncode:
            raise ValueError("Cannot read database settings from custom archive")
        generated.seek(0)
        statement = next(
            (
                line
                for line in generated
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


class Workspace:
    """Own exactly one random database; no destructive method accepts a user database name."""

    def __init__(self, admin_dsn: str, settings: DatabaseSettings | None = None) -> None:
        self.admin_dsn = admin_dsn
        self.name = f"dbreduce_{uuid.uuid4().hex}"
        self.dsn = make_conninfo(admin_dsn, dbname=self.name)
        self.url = database_url(admin_dsn, self.name)
        self.settings = settings
        self.created = False

    def __enter__(self) -> "Workspace":
        return self

    def create(self) -> None:
        self.close()
        with psycopg.connect(self.admin_dsn, autocommit=True, connect_timeout=10) as conn:
            conn.execute("SET statement_timeout = '600s'")
            statement = sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(self.name)
            )
            if self.settings is not None:
                settings = self.settings
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
                        statement += sql.SQL(" ICU_RULES {}").format(
                            sql.Literal(settings.icu_rules)
                        )
                elif settings.provider == "b":
                    if settings.locale is None:
                        raise ValueError("Source builtin locale is missing")
                    statement += sql.SQL(" LOCALE_PROVIDER builtin BUILTIN_LOCALE {}").format(
                        sql.Literal(settings.locale)
                    )
                elif settings.provider != "c":
                    raise ValueError(f"Unsupported locale provider: {settings.provider}")
            conn.execute(statement)
            self.created = True

    def reset(self, snapshot: Path) -> None:
        self.create()
        restore(self.dsn, snapshot)

    def close(self) -> None:
        if self.created:
            with psycopg.connect(self.admin_dsn, autocommit=True, connect_timeout=10) as conn:
                conn.execute("SET statement_timeout = '600s'")
                conn.execute(
                    sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(self.name))
                )
            self.created = False

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
