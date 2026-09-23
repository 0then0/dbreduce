import os
import subprocess
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict, make_conninfo


def client(command: list[str], dsn: str) -> None:
    params = conninfo_to_dict(dsn)
    if params.get("sslpassword"):
        raise ValueError(
            "Inline sslpassword would leak via process arguments; "
            "put it in a PostgreSQL service file instead"
        )
    params.pop("sslpassword", None)
    password = params.pop("password", None)
    env = os.environ.copy()
    if password is not None:
        env["PGPASSWORD"] = str(password)
    try:
        result = subprocess.run(
            [*command, "--dbname", make_conninfo("", **params)],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"{command[0]} timed out after 600 seconds") from error
    if result.returncode:
        # Raw stderr may contain credentials or application data.
        detail = result.stderr.lower()
        reason = next(
            (
                label
                for pattern, label in (
                    ("password authentication failed", "authentication failed"),
                    ("permission denied", "permission denied"),
                    ("connection refused", "connection refused"),
                    ("could not connect", "connection failed"),
                    ("unsupported version", "archive version unsupported"),
                    ("is not available", "required extension unavailable"),
                    ("not a valid archive", "invalid archive"),
                )
                if pattern in detail
            ),
            "check PostgreSQL client and server logs",
        )
        raise RuntimeError(f"{command[0]} failed (exit {result.returncode}; {reason})")


def dump(
    dsn: str,
    path: Path,
    *,
    archive: bool = True,
    restrict_key: str | None = None,
    create_database: bool = False,
) -> None:
    options = ["--restrict-key", restrict_key] if restrict_key is not None else []
    if create_database:
        options.append("--create")
    client(
        [
            "pg_dump",
            *options,
            "--no-owner",
            "--no-privileges",
            "--format",
            "custom" if archive else "plain",
            "--file",
            str(path),
        ],
        dsn,
    )


def restore(dsn: str, path: Path) -> None:
    with path.open("rb") as stream:
        archive = stream.read(5) == b"PGDMP"
    if not archive:
        raise ValueError("Input must be a pg_dump custom-format archive (--format=custom)")
    client(["pg_restore", "--exit-on-error", "--no-owner", "--no-privileges", str(path)], dsn)
