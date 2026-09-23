import uuid
from pathlib import Path

import psycopg

from dbreduce.cache.store import Cache, fingerprint
from dbreduce.models.schema import RowKey, Schema, State, TableKey
from dbreduce.oracle.runner import Oracle
from dbreduce.postgres.database import Workspace
from dbreduce.postgres.dump import dump
from dbreduce.postgres.rows import delete_rows, read_rows


class PostgresBackend:
    def __init__(
        self, workspace: Workspace, schema: Schema, snapshot: Path, oracle: Oracle, cache: Cache
    ) -> None:
        self.workspace = workspace
        self.schema = schema
        self.snapshot = snapshot
        self.oracle = oracle
        self.cache = cache
        self.constraint_rejections = 0
        self.restrict_key = uuid.uuid4().hex
        with psycopg.connect(workspace.dsn, connect_timeout=10) as conn:
            self.current, _ = read_rows(conn, schema)

    def state(self) -> State:
        return self.current

    def attempt(self, table: TableKey, rows: list[RowKey]) -> bool:
        self.workspace.reset(self.snapshot)
        try:
            with psycopg.connect(self.workspace.dsn, connect_timeout=10) as conn:
                conn.execute("SET statement_timeout = '600s'")
                delete_rows(conn, self.schema, table, rows)
        except psycopg.errors.IntegrityConstraintViolation:
            self.constraint_rejections += 1
            return False
        candidate_dump = self.snapshot.with_name("candidate.dump")
        dump(self.workspace.dsn, candidate_dump)
        # Compare and fingerprint the state the oracle will actually see after restore.
        self.workspace.reset(candidate_dump)
        with psycopg.connect(self.workspace.dsn, connect_timeout=10) as conn:
            candidate, _ = read_rows(conn, self.schema)
        if sum(map(len, candidate.values())) >= sum(map(len, self.current.values())):
            return False
        fingerprint_dump = self.snapshot.with_name("fingerprint.sql")
        dump(self.workspace.dsn, fingerprint_dump, archive=False, restrict_key=self.restrict_key)
        key = fingerprint(fingerprint_dump)
        accepted = self.cache.get(key)
        if accepted is None:
            accepted = self.oracle.fails(
                self.workspace.url, lambda: self.workspace.reset(candidate_dump)
            )
            self.cache.results[key] = accepted
        if accepted:
            candidate_dump.replace(self.snapshot)
            self.current = candidate
        return accepted
