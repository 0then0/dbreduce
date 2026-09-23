import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Annotated

import psycopg
import typer

from dbreduce.cache.store import Cache
from dbreduce.graph.dependencies import components, dependencies
from dbreduce.oracle.runner import Oracle
from dbreduce.postgres.backend import PostgresBackend
from dbreduce.postgres.database import Workspace, read_archive_settings, read_settings
from dbreduce.postgres.dump import dump
from dbreduce.postgres.introspection import check_extension_tables, inspect_database
from dbreduce.reducer.engine import reduce as reduce_state

app = typer.Typer(
    no_args_is_help=True, help="Minimize PostgreSQL bug reproducers in a disposable copy."
)


def publish_result(exported: Path, output: Path, report: Path, result: dict[str, object]) -> None:
    """Prepare both files, then publish without overwriting existing paths."""
    pending: list[Path] = []
    created: list[Path] = []
    try:
        with tempfile.NamedTemporaryFile(
            dir=output.parent, prefix=".dbreduce-", delete=False
        ) as file:
            pending.append(Path(file.name))
            with exported.open("rb") as source:
                shutil.copyfileobj(source, file)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=report.parent,
            prefix=".dbreduce-",
            delete=False,
        ) as file:
            pending.append(Path(file.name))
            json.dump(result, file, indent=2)
            file.write("\n")
        for prepared_path, destination in zip(pending, (output, report), strict=True):
            os.link(prepared_path, destination)
            created.append(destination)
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    finally:
        for path in pending:
            path.unlink(missing_ok=True)


def require_clients() -> None:
    for name in ("pg_dump", "pg_restore"):
        if not shutil.which(name):
            raise ValueError(f"Required PostgreSQL client tool is missing: {name}")


@app.command("inspect")
def inspect_command(database: Annotated[str, typer.Option(help="PostgreSQL source DSN")]) -> None:
    """Show tables, exact row counts, keys and dependency graph (read-only)."""
    try:
        with psycopg.connect(database, connect_timeout=10) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            schema = inspect_database(conn)
        for table in schema.tables:
            typer.echo(
                f"{table.label}\n  rows: {table.rows}\n  pk: {', '.join(table.primary_key) or '-'}"
            )
            for fk in schema.foreign_keys:
                if fk.child == table.key:
                    typer.echo(
                        f"  FK {', '.join(fk.columns)} -> {'.'.join(fk.parent)}"
                        f"({', '.join(fk.target_columns)})"
                    )
        typer.echo("Dependencies (child -> parent):")
        for child, parents in dependencies(schema).items():
            typer.echo(
                f"  {'.'.join(child)} -> {', '.join('.'.join(p) for p in sorted(parents)) or '-'}"
            )
        typer.echo(f"Strongly connected components: {components(schema)}")
    except (ValueError, psycopg.Error) as error:
        fail(error)


def fail(error: Exception) -> None:
    message = (
        "PostgreSQL operation failed; check connection and permissions"
        if isinstance(error, psycopg.Error)
        else str(error)
    )
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(1)


@app.command("reduce")
def reduce_command(
    oracle: Annotated[str, typer.Option(help="Shell command; nonzero exit reproduces the bug")],
    database: Annotated[str | None, typer.Option(help="Source PostgreSQL DSN")] = None,
    input_dump: Annotated[
        Path | None, typer.Option("--dump", help="Trusted pg_dump custom-format archive")
    ] = None,
    admin_database: Annotated[str | None, typer.Option(help="DSN with CREATEDB privilege")] = None,
    confirm: Annotated[int, typer.Option(min=1)] = 1,
    timeout: Annotated[float, typer.Option(min=0.01)] = 60,
    output: Annotated[Path, typer.Option()] = Path("dbreduce.min.sql"),
    report: Annotated[Path, typer.Option()] = Path("dbreduce-report.json"),
) -> None:
    """Reduce a source database or dump; export SQL and a JSON report."""
    started = time.monotonic()
    try:
        if (database is None) == (input_dump is None) or database == "":
            raise ValueError("Provide exactly one of --database or --dump")
        admin = admin_database or database
        if not admin:
            raise ValueError("--admin-database is required with --dump")
        if output.resolve() == report.resolve() or output.exists() or report.exists():
            raise ValueError("Output and report must be distinct paths that do not already exist")
        require_clients()
        with tempfile.TemporaryDirectory(prefix="dbreduce-") as temporary:
            snapshot = Path(temporary) / "accepted.dump"
            if database:
                with psycopg.connect(database, connect_timeout=10) as conn:
                    conn.execute("SET TRANSACTION READ ONLY")
                    check_extension_tables(conn)
                dump(database, snapshot)
            else:
                assert input_dump is not None
                shutil.copyfile(input_dump, snapshot)
            settings = read_settings(database) if database else read_archive_settings(snapshot)
            with Workspace(admin, settings=settings) as workspace:
                workspace.reset(snapshot)
                with psycopg.connect(workspace.dsn, connect_timeout=10) as conn:
                    check_extension_tables(conn)
                    schema = inspect_database(conn)
                # Keep one normalized snapshot for all probes.
                dump(workspace.dsn, snapshot)
                initial_rows = sum(table.rows for table in schema.tables)
                typer.echo(f"Initial database: {len(schema.tables)} tables, {initial_rows} rows")
                runner = Oracle(oracle, confirm=confirm, timeout=timeout)
                cache = Cache()
                if not runner.fails(workspace.url, lambda: workspace.reset(snapshot)):
                    raise ValueError(
                        "Oracle passed on the initial copy; failure does not reproduce"
                    )
                typer.echo("Oracle: FAIL\nReducing tables, row groups and individual rows...")
                workspace.reset(snapshot)
                backend = PostgresBackend(workspace, schema, snapshot, runner, cache)
                final = reduce_state(backend, typer.echo)
                # A fresh uncached final confirmation catches some flaky-oracle failures.
                if not runner.fails(workspace.url, lambda: workspace.reset(snapshot)):
                    raise ValueError(
                        "Final oracle confirmation passed; no verified result exported"
                    )
                workspace.reset(snapshot)
                exported = Path(temporary) / "result.sql"
                dump(workspace.dsn, exported, archive=False, create_database=True)
                final_rows = sum(map(len, final.values()))
                result = {
                    "initial_tables": len(schema.tables),
                    "initial_rows": initial_rows,
                    "final_tables": len(final),
                    "final_rows": final_rows,
                    "oracle_executions": runner.executions,
                    "cache_hits": cache.hits,
                    "duration_seconds": time.monotonic() - started,
                    "rows_by_table": {
                        schema_name: {
                            name: len(final[(schema_name, name)])
                            for schema, name in final
                            if schema == schema_name
                        }
                        for schema_name, _ in final
                    },
                    "constraint_rejections": backend.constraint_rejections,
                    "raise_exception_rejections": backend.raise_rejections,
                    "confirm": confirm,
                    "oracle": "FAIL",
                    "restore_database": workspace.name,
                }
                publish_result(exported, output, report, result)
                typer.echo(
                    f"Final: {final_rows} rows, oracle: FAIL, "
                    f"executions: {runner.executions}, cache hits: {cache.hits}"
                )
    except (OSError, ValueError, RuntimeError, psycopg.Error) as error:
        fail(error)
