from typing import Any

import psycopg
from psycopg import sql

from dbreduce.models.schema import ForeignKey, Schema, Table


def check_extension_tables(conn: psycopg.Connection[tuple[Any, ...]]) -> None:
    conn.execute("SET LOCAL statement_timeout = '600s'")
    found = conn.execute("""
        SELECT n.nspname, c.relname
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_depend d ON d.classid = 'pg_class'::regclass
                        AND d.objid = c.oid AND d.deptype = 'e'
        WHERE c.relkind = 'r' AND n.nspname !~ '^pg_'
        LIMIT 1
    """).fetchone()
    if found:
        raise ValueError(
            f"Unsupported extension-owned table {found[0]}.{found[1]}: pg_dump may omit its rows"
        )


def inspect_database(conn: psycopg.Connection[tuple[Any, ...]]) -> Schema:
    # Physical row identifiers are only used within one restored snapshot.
    conn.execute("SET LOCAL statement_timeout = '600s'")
    relations = conn.execute("""
        SELECT c.oid, n.nspname, c.relname, c.relkind,
               EXISTS (SELECT 1 FROM pg_inherits i
                       WHERE i.inhrelid = c.oid OR i.inhparent = c.oid),
               c.relrowsecurity
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'p', 'f')
          AND n.nspname <> 'information_schema'
          AND n.nspname !~ '^pg_'
        ORDER BY n.nspname, c.relname
    """).fetchall()
    tables = []
    by_oid = {}
    for oid, namespace, name, kind, inherited, rls in relations:
        if kind != "r" or inherited or rls:
            raise ValueError(
                f"Unsupported table {namespace}.{name}: partitioning, inheritance, "
                "foreign tables and row-level security are outside this MVP"
            )
        key = (namespace, name)
        by_oid[oid] = key
        pk = conn.execute(
            """
            SELECT a.attname FROM pg_constraint c
            CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, pos)
            JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
            WHERE c.conrelid = %s AND c.contype = 'p' ORDER BY k.pos
        """,
            (oid,),
        ).fetchall()
        row = conn.execute(
            sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(*key))
        ).fetchone()
        assert row is not None
        tables.append(Table(key, tuple(item[0] for item in pk), row[0]))
    foreign_keys = []
    rows = conn.execute("""
        SELECT c.conname, c.conrelid, c.confrelid,
               array_agg(ca.attname ORDER BY k.pos),
               array_agg(pa.attname ORDER BY k.pos),
               array_agg(ns.nspname ORDER BY k.pos),
               array_agg(op.oprname ORDER BY k.pos), bool_and(c.convalidated),
               array_agg(pcn.nspname ORDER BY k.pos),
               array_agg(pc.collname ORDER BY k.pos)
        FROM pg_constraint c
        CROSS JOIN LATERAL generate_subscripts(c.conkey, 1) AS k(pos)
        JOIN pg_attribute ca ON ca.attrelid = c.conrelid AND ca.attnum = c.conkey[k.pos]
        JOIN pg_attribute pa ON pa.attrelid = c.confrelid AND pa.attnum = c.confkey[k.pos]
        JOIN pg_operator op ON op.oid = c.conpfeqop[k.pos]
        JOIN pg_namespace ns ON ns.oid = op.oprnamespace
        LEFT JOIN pg_collation pc ON pc.oid = pa.attcollation
        LEFT JOIN pg_namespace pcn ON pcn.oid = pc.collnamespace
        WHERE c.contype = 'f'
        GROUP BY c.oid, c.conname, c.conrelid, c.confrelid ORDER BY c.oid
    """).fetchall()
    for (
        name,
        child,
        parent,
        columns,
        targets,
        namespaces,
        operators,
        validated,
        collation_schemas,
        collation_names,
    ) in rows:
        if child not in by_oid and parent not in by_oid:
            continue
        if child not in by_oid or parent not in by_oid or not validated:
            raise ValueError(f"Unsupported external or unvalidated foreign key: {name}")
        foreign_keys.append(
            ForeignKey(
                name,
                by_oid[child],
                by_oid[parent],
                tuple(columns),
                tuple(targets),
                tuple(zip(namespaces, operators, strict=True)),
                tuple(
                    (namespace, collation) if namespace is not None else None
                    for namespace, collation in zip(collation_schemas, collation_names, strict=True)
                ),
            )
        )
    return Schema(tuple(tables), tuple(foreign_keys))
