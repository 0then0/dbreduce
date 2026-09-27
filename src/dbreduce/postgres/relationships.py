"""Explicit virtual relationships, using PostgreSQL's normal equality resolution."""

import json
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

from dbreduce.models.schema import ForeignKey, Schema


def load_relationships(
    conn: psycopg.Connection[tuple[Any, ...]], schema: Schema, path: Path | None
) -> Schema:
    if path is None:
        return schema
    config = json.loads(path.read_text())
    if not isinstance(config, dict) or set(config) != {"relationships"}:
        raise ValueError("Config must contain only a relationships array")
    entries = config["relationships"]
    if not isinstance(entries, list):
        raise ValueError("relationships must be an array")
    known = {table.key for table in schema.tables}
    virtual = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) != {"from", "to"}:
            raise ValueError("Relationship requires only from and to endpoints")
        endpoints = []
        for side in ("from", "to"):
            endpoint = entry[side]
            if not isinstance(endpoint, dict) or set(endpoint) != {"table", "columns"}:
                raise ValueError("Endpoint requires table and columns")
            name, columns = endpoint["table"], endpoint["columns"]
            if not isinstance(name, str):
                raise ValueError("Relationship table must be a string")
            parts = name.split(".")
            key = ("public", parts[0]) if len(parts) == 1 else tuple(parts)
            if len(key) != 2 or key not in known:
                raise ValueError("Unknown relationship table")
            if (
                not isinstance(columns, list)
                or not columns
                or any(not isinstance(c, str) or not c for c in columns)
                or len(set(columns)) != len(columns)
            ):
                raise ValueError("Relationship columns must be distinct nonempty strings")
            endpoints.append(((key[0], key[1]), tuple(columns)))
        (child, columns), (parent, targets) = endpoints
        if len(columns) != len(targets):
            raise ValueError("Relationship endpoints must have the same number of columns")
        joins = sql.SQL(" AND ").join(
            sql.SQL("p.{} = c.{}").format(sql.Identifier(p), sql.Identifier(c))
            for c, p in zip(columns, targets, strict=True)
        )
        # Validate names, types and equality operators before any reduction.
        conn.execute(
            sql.SQL("SELECT 1 FROM {} c JOIN {} p ON {} LIMIT 0").format(
                sql.Identifier(*child), sql.Identifier(*parent), joins
            )
        )
        virtual.append(
            ForeignKey(f"virtual_{index}", child, parent, columns, targets, (), virtual=True)
        )
    return Schema(schema.tables, schema.foreign_keys + tuple(virtual))
