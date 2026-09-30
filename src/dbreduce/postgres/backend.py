import uuid
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import psycopg

from dbreduce.cache.store import Cache, fingerprint
from dbreduce.models.schema import RowKey, Schema, State, TableKey
from dbreduce.oracle.runner import Oracle
from dbreduce.performance import Performance
from dbreduce.postgres.clone import CloneStore, OwnedDatabase
from dbreduce.postgres.database import Workspace
from dbreduce.postgres.dump import dump
from dbreduce.postgres.rows import CandidateRejected, delete_rows, read_rows


class PostgresBackend:
    def __init__(
        self,
        workspace: Workspace,
        schema: Schema,
        snapshot: Path,
        oracle: Oracle,
        cache: Cache,
        progress: Callable[[str], None] = lambda _: None,
        performance: Performance | None = None,
    ) -> None:
        self.progress = progress
        self.workspace = workspace
        self.schema = schema
        self.snapshot = snapshot
        self.oracle = oracle
        self.cache = cache
        self.performance = performance or Performance()
        self.outcomes: Counter[str] = Counter()
        self.probes = 0
        self.accepted = 0
        self.constraint_rejections = 0
        self.raise_rejections = 0
        self.restrict_key = uuid.uuid4().hex
        with psycopg.connect(workspace.dsn, connect_timeout=10) as conn:
            with self.performance.measure("candidate_state_read"):
                self.current, _ = read_rows(conn, schema)

    def state(self) -> State:
        return self.current

    def attempt(self, table: TableKey, rows: list[RowKey]) -> bool:
        self.probes += 1
        with self.performance.measure("accepted_restore"):
            self.workspace.reset(self.snapshot)
        try:
            with self.performance.measure("candidate_deletion"):
                with psycopg.connect(self.workspace.dsn, connect_timeout=10) as conn:
                    conn.execute("SET statement_timeout = '600s'")
                    delete_rows(conn, self.schema, table, rows)
        except psycopg.errors.IntegrityConstraintViolation:
            self.progress("candidate rejected: constraint")
            self.constraint_rejections += 1
            return False
        except CandidateRejected:
            self.progress("candidate rejected: trigger")
            self.raise_rejections += 1
            return False
        # A fresh name also avoids stale bind-mount entries after an accepted dump is moved.
        candidate_dump = self.snapshot.with_name(f"candidate-{self.probes}.dump")
        with self.performance.measure("candidate_dump"):
            dump(self.workspace.dsn, candidate_dump)
        # Compare and fingerprint the state the oracle will actually see after restore.
        with self.performance.measure("candidate_restore"):
            self.workspace.reset(candidate_dump)
        with psycopg.connect(self.workspace.dsn, connect_timeout=10) as conn:
            with self.performance.measure("candidate_state_read"):
                candidate, _ = read_rows(conn, self.schema)
        if sum(map(len, candidate.values())) >= sum(map(len, self.current.values())):
            candidate_dump.unlink(missing_ok=True)
            return False
        fingerprint_dump = self.snapshot.with_name("fingerprint.sql")
        with self.performance.measure("fingerprint_dump"):
            dump(
                self.workspace.dsn,
                fingerprint_dump,
                archive=False,
                restrict_key=self.restrict_key,
            )
        with self.performance.measure("fingerprint_hash"):
            key = fingerprint(fingerprint_dump)
        accepted = self.cache.get(key)
        if accepted is None:
            accepted = self.oracle.fails(
                self.workspace.url, lambda: self.workspace.reset(candidate_dump)
            )
            assert self.oracle.last_outcome is not None
            self.outcomes[self.oracle.last_outcome] += 1
            self.cache.results[key] = accepted
            self.progress(f"candidate: {self.oracle.last_outcome}")
        else:
            self.progress(f"candidate: cached ({'accepted' if accepted else 'rejected'})")
        if accepted:
            self.accepted += 1
            candidate_dump.replace(self.snapshot)
            self.current = candidate
        else:
            candidate_dump.unlink(missing_ok=True)
        return accepted


class CloneBackend:
    def __init__(
        self,
        store: CloneStore,
        schema: Schema,
        accepted_db: OwnedDatabase,
        initial_state: State,
        oracle: Oracle,
        performance: Performance,
        progress: Callable[[str], None] = lambda _: None,
    ) -> None:
        self.store = store
        self.schema = schema
        self.accepted_db = accepted_db
        self.current = initial_state
        self.oracle = oracle
        self.performance = performance
        self.progress = progress
        self.outcomes: Counter[str] = Counter()
        self.probes = 0
        self.accepted = 0
        self.constraint_rejections = 0
        self.raise_rejections = 0

    def state(self) -> State:
        return self.current

    def confirm_initial_state(self) -> bool:
        """Check the accepted snapshot through independent, disposable oracle clones."""
        oracle_db: OwnedDatabase | None = None

        def prepare() -> str:
            nonlocal oracle_db
            oracle_db = self.store.clone(self.accepted_db)
            return self.store.url(oracle_db)

        def cleanup() -> None:
            nonlocal oracle_db
            if oracle_db is not None:
                self.store.drop(oracle_db)
                oracle_db = None

        return self.oracle.fails("", prepare, cleanup)

    def attempt(self, table: TableKey, rows: list[RowKey]) -> bool:
        self.probes += 1
        candidate_db = self.store.clone(self.accepted_db)
        promoted = False
        try:
            try:
                with self.performance.measure("candidate_deletion"):
                    with psycopg.connect(self.store.dsn(candidate_db), connect_timeout=10) as conn:
                        conn.execute("SET statement_timeout = '600s'")
                        delete_rows(conn, self.schema, table, rows)
            except psycopg.errors.IntegrityConstraintViolation:
                self.constraint_rejections += 1
                self.progress("candidate rejected: constraint")
                return False
            except CandidateRejected:
                self.raise_rejections += 1
                self.progress("candidate rejected: trigger")
                return False
            with psycopg.connect(self.store.dsn(candidate_db), connect_timeout=10) as conn:
                with self.performance.measure("candidate_state_read"):
                    candidate, _ = read_rows(conn, self.schema)
            if sum(map(len, candidate.values())) >= sum(map(len, self.current.values())):
                return False
            self.store.freeze(candidate_db)
            oracle_db: OwnedDatabase | None = None

            def prepare() -> str:
                nonlocal oracle_db
                oracle_db = self.store.clone(candidate_db)
                return self.store.url(oracle_db)

            def cleanup() -> None:
                nonlocal oracle_db
                if oracle_db is not None:
                    self.store.drop(oracle_db)
                    oracle_db = None

            accepted = self.oracle.fails("", prepare, cleanup)
            assert self.oracle.last_outcome is not None
            self.outcomes[self.oracle.last_outcome] += 1
            self.progress(f"candidate: {self.oracle.last_outcome}")
            if accepted:
                old = self.accepted_db
                self.accepted_db = candidate_db
                self.current = candidate
                self.accepted += 1
                promoted = True
                self.store.drop(old)
            return accepted
        finally:
            if not promoted:
                self.store.drop(candidate_db)
