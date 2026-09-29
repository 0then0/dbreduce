"""Oracle-checked table removal from a logical PostgreSQL archive."""

import os
import re
import subprocess
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

from dbreduce.models.schema import TableKey
from dbreduce.oracle.runner import Oracle, OracleError
from dbreduce.performance import Performance
from dbreduce.postgres.database import Workspace
from dbreduce.postgres.dump import ClientError, dump
from dbreduce.postgres.introspection import inspect_schema_counts
from dbreduce.reducer.ddmin import chunks, sizes

_KINDS = tuple(
    kind.encode("ascii")
    for kind in (
        "MATERIALIZED VIEW DATA",
        "SEQUENCE OWNED BY",
        "FK CONSTRAINT",
        "TABLE DATA",
        "SEQUENCE SET",
        "MATERIALIZED VIEW",
        "PROCEDURE",
        "FUNCTION",
        "CONSTRAINT",
        "EXTENSION",
        "TRIGGER",
        "DEFAULT",
        "COMMENT",
        "SEQUENCE",
        "SCHEMA",
        "INDEX",
        "VIEW",
        "TYPE",
        "TABLE",
    )
)
_TOC = re.compile(rb"^(\d+); (\d+) (\d+) (.+)$")


def _toc_fields(value: bytes) -> list[bytes]:
    """Split pg_restore identifiers without decoding database-encoding bytes."""
    fields = []
    index = 0
    while index < len(value):
        while index < len(value) and value[index] == 32:
            index += 1
        if index == len(value):
            break
        field = bytearray()
        quoted = False
        while index < len(value):
            if value[index] == 34:
                if quoted and index + 1 < len(value) and value[index + 1] == 34:
                    field.append(34)
                    index += 2
                    continue
                quoted = not quoted
            elif value[index] == 32 and not quoted:
                break
            else:
                field.append(value[index])
            index += 1
        fields.append(bytes(field))
    return fields


@dataclass(frozen=True)
class SchemaObject:
    dump_id: int
    catalog_oid: int
    object_oid: int
    kind: str
    identity: bytes
    line: bytes


def archive_objects(path: Path) -> tuple[bytes, list[SchemaObject]]:
    try:
        result = subprocess.run(
            ["pg_restore", "--list", str(path)],
            capture_output=True,
            timeout=600,
            env={**os.environ, "LC_ALL": "C"},
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Timed out reading archive object list") from error
    if result.returncode:
        raise ValueError("Cannot read custom archive object list")
    objects = []
    for line in result.stdout.splitlines():
        match = _TOC.match(line)
        if match is None:
            continue
        detail = match[4]
        raw_kind = next((kind for kind in _KINDS if detail.startswith(kind + b" ")), b"")
        kind = raw_kind.decode("ascii") if raw_kind else "OTHER"
        objects.append(
            SchemaObject(
                int(match[1]),
                int(match[2]),
                int(match[3]),
                kind,
                detail[len(raw_kind) + 1 :] if raw_kind else detail,
                line,
            )
        )
    return result.stdout, objects


def object_counts(objects: list[SchemaObject]) -> dict[str, int]:
    names = {
        "TABLE": "tables",
        "INDEX": "indexes",
        "CONSTRAINT": "constraints",
        "FK CONSTRAINT": "constraints",
        "VIEW": "views",
        "MATERIALIZED VIEW": "materialized_views",
        "FUNCTION": "functions",
        "PROCEDURE": "procedures",
        "TRIGGER": "triggers",
        "SEQUENCE": "sequences",
        "TYPE": "types",
        "EXTENSION": "extensions",
        "SCHEMA": "schemas",
    }
    counts = dict.fromkeys(names.values(), 0)
    for obj in objects:
        name = names.get(obj.kind)
        if name is not None:
            counts[name] += 1
    counts["schema_objects"] = sum(
        obj.kind not in ("TABLE DATA", "SEQUENCE SET", "MATERIALIZED VIEW DATA") for obj in objects
    )
    return counts


def schema_counts(conn: psycopg.Connection[Any], objects: list[SchemaObject]) -> dict[str, int]:
    counts = object_counts(objects)
    counts.update(inspect_schema_counts(conn))
    return counts


def table_oids(conn: psycopg.Connection[Any]) -> dict[TableKey, tuple[int, int]]:
    rows = conn.execute("""
        SELECT n.nspname, c.relname, c.oid
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'r' AND n.nspname <> 'information_schema'
          AND n.nspname !~ '^pg_'
        ORDER BY n.nspname, c.relname
    """).fetchall()

    def count(schema: str, name: str) -> int:
        row = conn.execute(
            sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(schema, name))
        ).fetchone()
        assert row is not None
        return int(row[0])

    return {
        (schema, name): (
            oid,
            count(schema, name),
        )
        for schema, name, oid in rows
    }


def removal_ids(
    conn: psycopg.Connection[Any],
    objects: list[SchemaObject],
    removed_tables: set[TableKey],
    tables: dict[TableKey, tuple[int, int]],
) -> set[int]:
    """Close automatic/internal dependencies and FKs pointing at removed tables."""
    class_row = conn.execute("SELECT 'pg_class'::regclass::oid").fetchone()
    constraint_row = conn.execute("SELECT 'pg_constraint'::regclass::oid").fetchone()
    assert class_row is not None and constraint_row is not None
    class_oid, constraint_oid = class_row[0], constraint_row[0]
    table_ids = [tables[key][0] for key in removed_tables]
    removed = {(class_oid, oid) for oid in table_ids}
    fk_rows = conn.execute(
        "SELECT oid FROM pg_constraint WHERE contype = 'f' AND confrelid = ANY(%s)",
        (table_ids,),
    ).fetchall()
    removed.update((constraint_oid, row[0]) for row in fk_rows)
    dependents: dict[tuple[int, int], list[tuple[int, int]]] = {}
    rows = conn.execute("""
        SELECT classid, objid, refclassid, refobjid FROM pg_depend
        WHERE deptype IN ('a', 'i', 'P', 'S')
    """).fetchall()
    for classid, objid, refclassid, refobjid in rows:
        dependents.setdefault((refclassid, refobjid), []).append((classid, objid))
    pending = deque(removed)
    while pending:
        for dependent in dependents.get(pending.popleft(), ()):
            if dependent not in removed:
                removed.add(dependent)
                pending.append(dependent)
    excluded = {
        obj.dump_id
        for obj in objects
        if (obj.catalog_oid, obj.object_oid) in removed
        or (obj.catalog_oid == 0 and (class_oid, obj.object_oid) in removed)
    }
    removed_names = {
        (fields[0], fields[1])
        for obj in objects
        if obj.kind == "TABLE" and obj.object_oid in table_ids
        for fields in [_toc_fields(obj.identity)]
        if len(fields) >= 2
    }
    excluded.update(
        obj.dump_id
        for obj in objects
        if obj.kind == "COMMENT"
        and (fields := _toc_fields(obj.identity))
        and len(fields) >= 3
        and fields[1] in (b"TABLE", b"COLUMN")
        and any(
            fields[0] == schema_name
            and (fields[2] == table_name or fields[2].startswith(table_name + b"."))
            for schema_name, table_name in removed_names
        )
    )
    removed_sequences = {
        obj.identity for obj in objects if obj.kind == "SEQUENCE" and obj.dump_id in excluded
    }
    excluded.update(
        obj.dump_id
        for obj in objects
        if obj.kind in ("SEQUENCE SET", "SEQUENCE OWNED BY") and obj.identity in removed_sequences
    )
    return excluded


def selected_list(toc: bytes, excluded: set[int], path: Path) -> None:
    lines = []
    for line in toc.splitlines(keepends=True):
        match = _TOC.match(line.rstrip(b"\n"))
        lines.append(b";" + line if match and int(match[1]) in excluded else line)
    path.write_bytes(b"".join(lines))


class SchemaReducer:
    def __init__(
        self,
        workspace: Workspace,
        archive: Path,
        oracle: Oracle,
        performance: Performance,
        progress: Callable[[str], None],
        initial: dict[str, int] | None = None,
    ) -> None:
        self.workspace = workspace
        self.archive = archive
        self.oracle = oracle
        self.performance = performance
        self.progress = progress
        self.outcomes: Counter[str] = Counter()
        self.attempted = 0
        self.accepted = 0
        self.initial: dict[str, int] = initial or {}

    def run(self) -> Path:
        directory = self.archive.parent
        while True:
            with self.performance.measure("schema_candidate_planning"):
                self.workspace.reset(self.archive)
                plan_archive = directory / "schema-plan.dump"
                dump(self.workspace.dsn, plan_archive)
                toc, objects = archive_objects(plan_archive)
                with psycopg.connect(self.workspace.dsn, connect_timeout=10) as conn:
                    tables = table_oids(conn)
                    if not self.initial:
                        self.initial = schema_counts(conn, objects)
                    with self.performance.measure("schema_introspection"):
                        ids_by_table = {
                            key: removal_ids(conn, objects, {key}, tables) for key in tables
                        }
                ordered = sorted(tables, key=lambda key: (tables[key][1] > 0, key))
            changed = False
            for size in sizes(len(ordered)):
                for group in chunks(ordered, size):
                    if self._attempt(plan_archive, toc, ids_by_table, set(group)):
                        changed = True
                        break
                if changed:
                    break
            if not changed:
                return self.archive

    def _attempt(
        self,
        plan_archive: Path,
        toc: bytes,
        ids_by_table: dict[TableKey, set[int]],
        removed_tables: set[TableKey],
    ) -> bool:
        self.attempted += 1
        with self.performance.measure("schema_candidate_planning"):
            # Closure was computed before any candidate replaced the planning database.
            excluded = set().union(*(ids_by_table[key] for key in removed_tables))
            selected = plan_archive.with_name("schema-selected.list")
            selected_list(toc, excluded, selected)
        if not excluded:
            self.outcomes["invalid_schema"] += 1
            return False
        try:
            with self.performance.measure("schema_candidate_restore"):
                self.workspace.reset(plan_archive, use_list=selected)
        except ClientError as error:
            if error.command != "pg_restore" or error.reason not in (
                "constraint violation",
                "missing dependency",
                "invalid schema",
            ):
                self.outcomes["oracle_infrastructure_error"] += 1
                raise
            self.outcomes[
                "constraint_rejected"
                if error.reason == "constraint violation"
                else "invalid_schema"
            ] += 1
            return False
        with psycopg.connect(self.workspace.dsn, connect_timeout=10) as conn:
            actual_tables = {
                (schema, name)
                for schema, name in conn.execute("""
                    SELECT n.nspname, c.relname FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE c.relkind = 'r' AND n.nspname <> 'information_schema'
                      AND n.nspname !~ '^pg_'
                """).fetchall()
            }
        if actual_tables != ids_by_table.keys() - removed_tables:
            self.outcomes["invalid_schema"] += 1
            return False
        candidate = plan_archive.with_name("schema-candidate.dump")
        with self.performance.measure("schema_candidate_reconstruction"):
            dump(self.workspace.dsn, candidate)
        try:
            with self.performance.measure("schema_candidate_restore"):
                self.workspace.reset(candidate)
        except ClientError as error:
            if error.command != "pg_restore" or error.reason not in (
                "constraint violation",
                "missing dependency",
                "invalid schema",
            ):
                self.outcomes["oracle_infrastructure_error"] += 1
                raise
            self.outcomes[
                "constraint_rejected"
                if error.reason == "constraint violation"
                else "invalid_schema"
            ] += 1
            return False
        try:

            def prepare_oracle() -> None:
                with self.performance.measure("schema_oracle_preparation"):
                    self.workspace.reset(candidate)

            with self.performance.measure("schema_oracle_execution"):
                accepted = self.oracle.fails(self.workspace.url, prepare_oracle)
        except OracleError:
            self.outcomes["oracle_infrastructure_error"] += 1
            return False
        if not accepted:
            outcome = self.oracle.last_outcome
            self.outcomes["bug_disappeared" if outcome == "passed" else outcome] += 1
            return False
        self.accepted += 1
        self.outcomes["accepted"] += 1
        candidate.replace(self.archive)
        self.progress(f"schema: removed {len(removed_tables)} table(s)")
        return True

    def report(self) -> dict[str, object]:
        objects = archive_objects(self.archive)[1]
        with psycopg.connect(self.workspace.dsn, connect_timeout=10) as conn:
            final = schema_counts(conn, objects)
        return {
            "enabled": True,
            "initial": self.initial,
            "final": final,
            "candidates": {
                "attempted": self.attempted,
                "accepted": self.accepted,
                "rejected": self.attempted - self.accepted,
                **{
                    key: self.outcomes[key]
                    for key in (
                        "bug_disappeared",
                        "different_failure",
                        "invalid_schema",
                        "constraint_rejected",
                        "oracle_infrastructure_error",
                    )
                },
            },
            "minimality": {
                "locally_irreducible": not any(
                    self.outcomes[key]
                    for key in (
                        "invalid_schema",
                        "constraint_rejected",
                        "oracle_infrastructure_error",
                    )
                ),
                "transformations": ["whole_table_removal"],
            },
            "cache": "disabled",
        }
