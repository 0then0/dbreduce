from collections import defaultdict
from typing import Any

import psycopg
from psycopg import sql

from dbreduce.models.schema import RowKey, Schema, State, TableKey


def read_rows(
    conn: psycopg.Connection[tuple[Any, ...]], schema: Schema
) -> tuple[
    State,
    dict[TableKey, dict[RowKey, str]],
]:
    state: State = {}
    conn.execute("SET LOCAL statement_timeout = '600s'")
    locations = {}
    for table in schema.tables:
        rows = conn.execute(
            sql.SQL(
                "SELECT row_to_json(t)::text, ctid::text FROM {} t "
                'ORDER BY row_to_json(t)::text COLLATE "C", ctid'
            ).format(sql.Identifier(*table.key))
        ).fetchall()
        counts: dict[str, int] = defaultdict(int)
        mapping = {}
        for value, ctid in rows:
            key = (value, counts[value])
            counts[value] += 1
            mapping[key] = ctid
        state[table.key] = list(mapping)
        locations[table.key] = mapping
    return state, locations


def delete_rows(
    conn: psycopg.Connection[tuple[Any, ...]], schema: Schema, table: TableKey, rows: list[RowKey]
) -> bool:
    """Compute incoming-FK closure to a fixed point, including cycles and self references."""
    conn.execute("SET LOCAL statement_timeout = '600s'")
    _, locations = read_rows(conn, schema)
    marked: dict[TableKey, set[str]] = {t.key: set() for t in schema.tables}
    marked[table] = {locations[table][key] for key in rows}
    changed = True
    while changed:
        changed = False
        for fk in schema.foreign_keys:
            if not marked[fk.parent]:
                continue
            joins = []
            for index, (child, parent, (namespace, operator)) in enumerate(
                zip(
                    fk.columns,
                    fk.target_columns,
                    fk.operators,
                    strict=True,
                )
            ):
                # Operator names come from pg_operator, whose grammar excludes SQL injection.
                parent_column = sql.SQL("p.{}").format(sql.Identifier(parent))
                if fk.collations:
                    collation = fk.collations[index]
                    if collation is not None:
                        parent_column = sql.SQL("{} COLLATE {}").format(
                            parent_column, sql.Identifier(*collation)
                        )
                joins.append(
                    sql.SQL("({}) OPERATOR({}.{}) c.{}").format(
                        parent_column,
                        sql.Identifier(namespace),
                        sql.SQL(operator),
                        sql.Identifier(child),
                    )
                )
            found = conn.execute(
                sql.SQL(
                    "SELECT c.ctid::text FROM {} c JOIN {} p ON {} WHERE p.ctid = ANY(%s::tid[])"
                ).format(
                    sql.Identifier(*fk.child),
                    sql.Identifier(*fk.parent),
                    sql.SQL(" AND ").join(joins),
                ),
                (sorted(marked[fk.parent]),),
            ).fetchall()
            before = len(marked[fk.child])
            marked[fk.child].update(row[0] for row in found)
            changed |= len(marked[fk.child]) != before
    deletes: list[sql.Composed] = []
    params = []
    for key, tids in sorted(marked.items()):
        if tids:
            deletes.append(
                sql.SQL("{} AS (DELETE FROM {} WHERE ctid = ANY(%s::tid[]))").format(
                    sql.Identifier(f"d{len(deletes)}"), sql.Identifier(*key)
                )
            )
            params.append(sorted(tids))
    if not deletes:
        return False
    # All sub-statements share one command boundary for immediate FK checks.
    conn.execute("SET CONSTRAINTS ALL DEFERRED")
    conn.execute(sql.SQL("WITH {} SELECT 1").format(sql.SQL(", ").join(deletes)), params)
    conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
    return True
