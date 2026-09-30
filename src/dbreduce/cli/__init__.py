import json
import os
import re
import shutil
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Annotated

import psycopg
import typer

from dbreduce import __version__
from dbreduce.cache.store import Cache
from dbreduce.graph.dependencies import components, dependencies
from dbreduce.oracle.runner import Oracle
from dbreduce.performance import Performance
from dbreduce.postgres.backend import CloneBackend, PostgresBackend
from dbreduce.postgres.clone import CloneStore, check_clone_source
from dbreduce.postgres.database import Workspace, read_archive_settings, read_settings
from dbreduce.postgres.dump import dump
from dbreduce.postgres.introspection import (
    check_extension_tables,
    inspect_database,
    inspect_schema_counts,
)
from dbreduce.postgres.relationships import load_relationships
from dbreduce.postgres.rows import read_rows
from dbreduce.postgres.schema_reduction import SchemaReducer, archive_objects, schema_counts
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
def inspect_command(
    database: Annotated[str, typer.Option(help="PostgreSQL source DSN")],
    config: Annotated[Path | None, typer.Option(help="JSON virtual relationships")] = None,
) -> None:
    """Show tables, exact row counts, keys and dependency graph (read-only)."""
    try:
        with psycopg.connect(database, connect_timeout=10) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            schema = load_relationships(conn, inspect_database(conn), config)
            schema_counts = inspect_schema_counts(conn)
        typer.echo(
            "Schema: "
            + ", ".join(
                f"{name.replace('_', ' ')}: {count}" for name, count in schema_counts.items()
            )
        )
        for table in schema.tables:
            typer.echo(
                f"{table.label}\n  rows: {table.rows}\n  pk: {', '.join(table.primary_key) or '-'}"
            )
            for fk in schema.foreign_keys:
                if fk.child == table.key:
                    typer.echo(
                        f"  {'virtual' if fk.virtual else 'FK'} {', '.join(fk.columns)}"
                        f" -> {'.'.join(fk.parent)}"
                        f"({', '.join(fk.target_columns)})"
                    )
        typer.echo("Dependencies (child -> parent):")
        for child, parents in dependencies(schema).items():
            typer.echo(
                f"  {'.'.join(child)} -> {', '.join('.'.join(p) for p in sorted(parents)) or '-'}"
            )
        typer.echo(f"Strongly connected components: {components(schema)}")
    except (OSError, ValueError, psycopg.Error) as error:
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
    oracle: Annotated[str, typer.Option(help="Shell oracle command using DATABASE_URL")],
    database: Annotated[str | None, typer.Option(help="Source PostgreSQL DSN")] = None,
    input_dump: Annotated[
        Path | None, typer.Option("--dump", help="Trusted pg_dump custom-format archive")
    ] = None,
    admin_database: Annotated[str | None, typer.Option(help="DSN with CREATEDB privilege")] = None,
    config: Annotated[Path | None, typer.Option(help="JSON virtual relationships")] = None,
    match_stdout: Annotated[str | None, typer.Option(help="Required stdout regex")] = None,
    match_stderr: Annotated[str | None, typer.Option(help="Required stderr regex")] = None,
    expected_exit_code: Annotated[int | None, typer.Option()] = None,
    oracle_json: Annotated[bool, typer.Option(help="Read structured verdict from stdout")] = False,
    oracle_framed_json: Annotated[
        bool, typer.Option(help="Read one DBREDUCE_VERDICT JSON line amid stdout logs")
    ] = False,
    restore_jobs: Annotated[int, typer.Option(min=1, help="Parallel pg_restore workers")] = 1,
    candidate_backend: Annotated[
        str, typer.Option(help="Candidate isolation: snapshot or clone")
    ] = "snapshot",
    clone_strategy: Annotated[
        str, typer.Option(help="Clone strategy: wal_log or file_copy")
    ] = "wal_log",
    file_copy_method: Annotated[
        str | None, typer.Option(help="For file_copy: copy or clone (PostgreSQL 18)")
    ] = None,
    confirm: Annotated[int, typer.Option(min=1)] = 1,
    timeout: Annotated[float, typer.Option(min=0.01)] = 60,
    reduce_schema: Annotated[
        bool, typer.Option(help="Also reduce whole application tables after row reduction")
    ] = False,
    output: Annotated[Path, typer.Option()] = Path("dbreduce.min.sql"),
    report: Annotated[Path, typer.Option()] = Path("dbreduce-report.json"),
) -> None:
    """Reduce a source database or dump; export SQL and a JSON report."""
    started = time.monotonic()
    performance = Performance()
    try:
        if (database is None) == (input_dump is None) or database == "":
            raise ValueError("Provide exactly one of --database or --dump")
        admin = admin_database or database
        if not admin:
            raise ValueError("--admin-database is required with --dump")
        if output.resolve() == report.resolve() or output.exists() or report.exists():
            raise ValueError("Output and report must be distinct paths that do not already exist")
        if candidate_backend not in ("snapshot", "clone"):
            raise ValueError("--candidate-backend must be snapshot or clone")
        if candidate_backend == "snapshot" and (
            clone_strategy != "wal_log" or file_copy_method is not None
        ):
            raise ValueError("Clone options require --candidate-backend clone")
        runner = Oracle(
            oracle,
            confirm=confirm,
            timeout=timeout,
            match_stdout=match_stdout,
            match_stderr=match_stderr,
            expected_exit_code=expected_exit_code,
            structured=oracle_json,
            framed=oracle_framed_json,
            performance=performance,
        )
        if runner.mode == "legacy":
            typer.echo(
                "Warning: No failure identity matcher configured. Any non-zero exit "
                "code except infrastructure/signal statuses counts as reproduction.",
                err=True,
            )
            if reduce_schema:
                typer.echo(
                    "Warning: Schema reduction with legacy any-nonzero oracle can accept "
                    "application startup failures. Use --oracle-json or --oracle-framed-json.",
                    err=True,
                )
        require_clients()
        with tempfile.TemporaryDirectory(prefix="dbreduce-") as temporary:
            snapshot = Path(temporary) / "accepted.dump"
            if database:
                with psycopg.connect(database, connect_timeout=10) as conn:
                    conn.execute("SET TRANSACTION READ ONLY")
                    check_extension_tables(conn)
                    if candidate_backend == "clone":
                        check_clone_source(conn)
                with performance.measure("source_dump"):
                    dump(database, snapshot)
            else:
                assert input_dump is not None
                with performance.measure("source_dump"):
                    shutil.copyfile(input_dump, snapshot)
            settings = read_settings(database) if database else read_archive_settings(snapshot)
            with Workspace(
                admin, settings=settings, performance=performance, restore_jobs=restore_jobs
            ) as workspace:
                workspace.reset(snapshot)
                with psycopg.connect(workspace.dsn, connect_timeout=10) as conn:
                    check_extension_tables(conn)
                    schema = load_relationships(conn, inspect_database(conn), config)
                # Keep one normalized snapshot for all probes.
                with performance.measure("initial_normalization_dump"):
                    dump(workspace.dsn, snapshot)
                initial_sql_bytes: int | None = None
                initial_schema_counts: dict[str, int] | None = None
                if reduce_schema:
                    initial_sql = Path(temporary) / "initial.sql"
                    with performance.measure("initial_logical_export"):
                        dump(workspace.dsn, initial_sql, archive=False, create_database=True)
                    initial_sql_bytes = initial_sql.stat().st_size
                    with psycopg.connect(workspace.dsn, connect_timeout=10) as conn:
                        initial_schema_counts = schema_counts(conn, archive_objects(snapshot)[1])
                initial_rows = sum(table.rows for table in schema.tables)
                typer.echo(f"Initial database: {len(schema.tables)} tables, {initial_rows} rows")
                cache = Cache()
                if not runner.fails(workspace.url, lambda: workspace.reset(snapshot)):
                    raise ValueError(
                        "Oracle did not reproduce the required failure on the initial copy"
                    )
                typer.echo("Oracle: FAIL\nReducing tables, row groups and individual rows...")
                workspace.reset(snapshot)
                store: CloneStore | None = None
                clone_backend: CloneBackend | None = None
                with ExitStack() as candidate_resources:
                    if candidate_backend == "clone":
                        store = candidate_resources.enter_context(
                            CloneStore(
                                admin,
                                settings,
                                performance,
                                strategy=clone_strategy,
                                file_copy_method=file_copy_method,
                                restore_jobs=restore_jobs,
                            )
                        )
                        store.probe_file_copy_clone()
                        accepted_db, initial_state = store.create_initial(snapshot, schema)
                        clone_backend = CloneBackend(
                            store,
                            schema,
                            accepted_db,
                            initial_state,
                            runner,
                            performance,
                            typer.echo,
                        )
                        if not clone_backend.confirm_initial_state():
                            raise ValueError(
                                "Oracle did not reproduce the required failure "
                                "on the accepted state"
                            )
                        backend: CloneBackend | PostgresBackend = clone_backend
                    else:
                        backend = PostgresBackend(
                            workspace, schema, snapshot, runner, cache, typer.echo, performance
                        )
                    final = reduce_state(backend, typer.echo)
                    final_archive = snapshot
                    if store is not None and clone_backend is not None:
                        final_archive = Path(temporary) / "final-logical.dump"
                        normalization_db = store.clone(clone_backend.accepted_db)
                        try:
                            with performance.measure("final_normalization_dump"):
                                dump(store.dsn(normalization_db), final_archive)
                        finally:
                            store.drop(normalization_db)
                        with performance.measure("final_normalization"):
                            workspace.reset(final_archive)
                        with psycopg.connect(workspace.dsn, connect_timeout=10) as conn:
                            with performance.measure("candidate_state_read"):
                                normalized, _ = read_rows(conn, schema)
                        if normalized != final:
                            raise ValueError(
                                "Final logical restore changed candidate state; "
                                "no verified result exported"
                            )
                    schema_reducer: SchemaReducer | None = None
                    if reduce_schema:
                        typer.echo("Reducing whole application tables...")
                        schema_archive = Path(temporary) / "schema-accepted.dump"
                        with performance.measure("schema_data_normalization"):
                            workspace.reset(final_archive)
                            dump(workspace.dsn, schema_archive)
                        schema_reducer = SchemaReducer(
                            workspace,
                            schema_archive,
                            runner,
                            performance,
                            typer.echo,
                            initial_schema_counts,
                        )
                        with performance.measure("schema_reduction"):
                            final_archive = schema_reducer.run()
                        normalized_archive = Path(temporary) / "schema-final.dump"
                        with performance.measure("final_logical_normalization"):
                            workspace.reset(final_archive)
                            dump(workspace.dsn, normalized_archive)
                            workspace.reset(normalized_archive)
                        final_archive = normalized_archive
                    # A fresh uncached final confirmation catches flaky-oracle failures.
                    if not runner.fails(workspace.url, lambda: workspace.reset(final_archive)):
                        raise ValueError(
                            "Final oracle identity confirmation failed; no verified result exported"
                        )
                    with performance.measure("final_normalization"):
                        workspace.reset(final_archive)
                exported = Path(temporary) / "result.sql"
                with performance.measure("final_export"):
                    dump(workspace.dsn, exported, archive=False, create_database=True)
                if reduce_schema:
                    with psycopg.connect(workspace.dsn, connect_timeout=10) as conn:
                        final_schema = inspect_database(conn)
                        final, _ = read_rows(conn, final_schema)
                with psycopg.connect(admin, connect_timeout=10) as conn:
                    version_row = conn.execute("SHOW server_version").fetchone()
                    assert version_row is not None
                    server_version = str(version_row[0])
                backend_info = {
                    "type": candidate_backend,
                    "strategy": clone_strategy if store is not None else None,
                    "server_version": server_version,
                    "file_copy_method": (
                        file_copy_method or store.server_file_copy_method
                        if store is not None and clone_strategy == "file_copy"
                        else None
                    ),
                    "file_copy_clone_probe": store.clone_probe if store is not None else None,
                    "filesystem_reflink_verified": False,
                    "restore_jobs": restore_jobs,
                }
                data_minimality: dict[str, object] = {
                    "locally_irreducible": not any(
                        backend.outcomes[key] for key in ("candidate_invalid", "different_failure")
                    ),
                }
                if backend.outcomes["candidate_invalid"]:
                    data_minimality["blocked_by_candidate_invalid"] = True
                final_rows = sum(map(len, final.values()))
                result = {
                    "dbreduce_version": __version__,
                    "failure_identity": runner.identity_report(),
                    "oracle_stats": {
                        "executions": runner.executions,
                        "confirmations": confirm,
                        "outcomes": dict(runner.outcomes),
                    },
                    "candidate_backend": backend_info,
                    "candidate_stats": {
                        "created": backend.probes,
                        "accepted": backend.accepted,
                        "rejected": backend.probes - backend.accepted,
                        "candidate_invalid": backend.outcomes["candidate_invalid"],
                    },
                    "cache_stats": {
                        "policy": "exact_fingerprint" if store is None else "disabled",
                        "lookups": cache.lookups,
                        "hits": cache.hits,
                        "hit_rate": cache.hits / cache.lookups if cache.lookups else 0,
                        "estimated_seconds_saved": (
                            (cache.accepted_hits * confirm + cache.rejected_hits)
                            * (
                                performance.phases["oracle_state_preparation"][1]
                                + performance.phases["oracle_process"][1]
                            )
                            / runner.executions
                            if runner.executions
                            else 0
                        ),
                    },
                    "relationships": {
                        "database_fk_count": sum(not fk.virtual for fk in schema.foreign_keys),
                        "virtual_relationship_count": sum(fk.virtual for fk in schema.foreign_keys),
                        "virtual": [
                            {
                                "from": {"table": list(fk.child), "columns": list(fk.columns)},
                                "to": {
                                    "table": list(fk.parent),
                                    "columns": list(fk.target_columns),
                                },
                            }
                            for fk in schema.foreign_keys
                            if fk.virtual
                        ],
                    },
                    "performance": {
                        "elapsed_seconds": time.monotonic() - started,
                        "oracle_seconds": runner.seconds,
                        "database_clone_seconds": (
                            performance.phases["database_clone"][1] if store is not None else 0
                        ),
                        "phases": performance.report(),
                    },
                    "minimality": "locally irreducible under attempted transformations",
                    "transformations": ["tables", "chunks", "single rows", "relationship closure"],
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
                schema_locally_irreducible = True
                if schema_reducer is not None:
                    schema_report = schema_reducer.report()
                    schema_minimality = schema_report["minimality"]
                    assert isinstance(schema_minimality, dict)
                    schema_locally_irreducible = schema_minimality["locally_irreducible"]
                    result["schema_reduction"] = schema_report
                    result["minimality_by_phase"] = {
                        "data": data_minimality,
                        "schema": schema_report["minimality"],
                    }
                    result["initial_sql_bytes"] = initial_sql_bytes
                    result["final_sql_bytes"] = exported.stat().st_size
                    result["final_tables"] = len(final)
                    transformations = result["transformations"]
                    assert isinstance(transformations, list)
                    result["transformations"] = [*transformations, "whole_table_removal"]
                elif not data_minimality["locally_irreducible"]:
                    result["minimality_by_phase"] = {"data": data_minimality}
                if not data_minimality["locally_irreducible"] or not schema_locally_irreducible:
                    result["minimality"] = "local irreducibility not established"
                result["database_stats"] = {
                    "clone_attempts": store.clone_attempts if store is not None else 0,
                    "clone_failures": store.clone_failures if store is not None else 0,
                    "fallbacks": 0,
                    "created": workspace.databases_created + (store.created if store else 0),
                    "dropped": workspace.databases_dropped + (store.dropped if store else 0),
                    "cleanup_failures": workspace.cleanup_failures
                    + (store.cleanup_failures if store else 0),
                }
            database_stats = result["database_stats"]
            assert isinstance(database_stats, dict)
            database_stats["dropped"] = workspace.databases_dropped + (
                store.dropped if store is not None else 0
            )
            database_stats["cleanup_failures"] = workspace.cleanup_failures + (
                store.cleanup_failures if store is not None else 0
            )
            performance_result = result["performance"]
            assert isinstance(performance_result, dict)
            performance_result["phases"] = performance.report()
            performance_result["elapsed_seconds"] = time.monotonic() - started
            result["duration_seconds"] = performance_result["elapsed_seconds"]
            publish_result(exported, output, report, result)
            typer.echo(
                f"Final: {final_rows} rows, oracle: FAIL, "
                f"executions: {runner.executions}, cache hits: {cache.hits}"
            )
    except (OSError, ValueError, re.error, RuntimeError, psycopg.Error) as error:
        fail(error)
