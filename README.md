# DBReduce

DBReduce minimizes PostgreSQL datasets while a failing command still reproduces a bug.
It works only on a randomly named disposable database and exports a SQL reproducer.

## Install

```bash
uv sync --dev
uv run dbreduce --help
```

Install PostgreSQL client tools (`pg_dump`, `pg_restore`) matching or newer than the
server. Use a current, patched release supporting `pg_dump --restrict-key`.
The connection used for the workspace needs `CREATEDB` and permission to restore the
schema. A separate `--admin-database` DSN can supply those privileges. A read-only
source role is recommended. DBReduce only reads source metadata and uses `pg_dump`;
it never runs reduction SQL on the source.

## Reduce

```bash
uv run dbreduce inspect --database postgresql://localhost/app_bug
uv run dbreduce reduce \
  --database postgresql://localhost/app_bug \
  --oracle 'uv run pytest tests/test_checkout.py::test_negative_total' \
  --confirm 3
```

The oracle **must connect using `DATABASE_URL` or `DBREDUCE_DATABASE_URL`**. Both point
to the working copy and are set for every execution. Hardcoded connections and
external services cannot be redirected or sandboxed by DBReduce. Never point an
oracle at production. Only run trusted commands and restore trusted dumps: SQL
functions and triggers may have external effects.

Exit zero means the failure disappeared; every nonzero status, including a crash or
shell exit 127, means it remains. Timeouts and failure to launch the shell abort
reduction. `--timeout` defaults to 60 seconds per execution. Oracle output is
suppressed; first verify the command independently.
Any nonzero test failure counts, including an unrelated application/setup error.
Specific failure signatures are not supported yet.

The source can also be a PostgreSQL **custom-format archive**:

```bash
pg_dump --format=custom --no-owner --no-privileges \
  --file app.dump postgresql://localhost/app_bug
uv run dbreduce reduce --dump app.dump \
  --admin-database postgresql://localhost/postgres \
  --oracle 'uv run python examples/oracle.py'
```

With `--database`, the working copy inherits the source database's encoding and
locale. With `--dump`, DBReduce reads those settings from `pg_restore`'s generated
CREATE DATABASE statement. An archive that does not expose complete settings is
rejected before reduction.

Plain SQL is an output format, not an accepted input format. This avoids executing
`psql` reconnect/shell meta-commands while restoring user input.

Outputs default to `dbreduce.min.sql` and `dbreduce-report.json`. Existing files are
never overwritten. Override with `--output` and `--report`. Both files are prepared
before publication; an error removes results created by the current run. The report includes
initial/final table and row counts, rows per table, oracle executions, cache hits,
constraint rejections, confirmation count, elapsed seconds, and `restore_database`,
the name of the database created by the SQL dump. Tables are kept,
even when emptied, so the schema remains available to the oracle.

Restore the SQL through a maintenance database using a role with `CREATEDB`:

```bash
psql -X -v ON_ERROR_STOP=1 -d postgres -f dbreduce.min.sql
```

The dump creates a new `dbreduce_<uuid>` database with the verified encoding and
locale, then connects to it. Read its name from `restore_database` in the JSON report.
The restore fails rather than overwriting a database with that name.

## Demo

The fixture contains 12,005 rows across `users`, `orders`, `order_items`, `coupons`,
and `payments`. A paid order with a coupon greater than its item total triggers the
bug. A verified local run on PostgreSQL 17 reduced the fixture to five linked rows
while the oracle continued to fail.

```bash
createdb dbreduce_demo
psql -X -v ON_ERROR_STOP=1 -d dbreduce_demo -f examples/fixture.sql
uv run dbreduce reduce \
  --database postgresql://localhost/dbreduce_demo \
  --oracle 'uv run python examples/oracle.py'
```

```text
12,005 rows
    |
 DBReduce
    |
5 rows
    |
oracle still fails
```

That run used 53 oracle executions and 28 cache hits. The source database was
unchanged, and a fresh database restored from the exported SQL still failed the
oracle. The report contains the actual result of each run.

## How it works

1. Dump the source consistently and restore it into a random `dbreduce_<uuid>` database.
2. Discover tables, primary keys, validated foreign keys, row counts and dependencies.
3. Confirm the initial failure; abort if the command succeeds.
4. Try whole-table row sets, then successively smaller chunks, then individual rows.
5. Expand each candidate along incoming FK edges until a fixed point. This handles
   self-references and cycles without disabling constraints. Execute all deletes in
   one SQL statement, defer deferrable constraints, and validate before commit.
   PostgreSQL constraint violations reject a candidate without running the oracle.
6. Dump and restore a candidate before reading its row state. Reject it when the
   restored copy did not shrink. Restore that dump before **every** oracle
   confirmation, so oracle writes never become part of the accepted state.
7. Cache the complete candidate SQL snapshot fingerprint, including schema and
   sequence values. COPY rows are sorted within each table for a stable key. Cache
   lifetime is one run. Restore the accepted snapshot before each new attempt;
   repeat passes until none reduce the row count.
8. Reconfirm the final state without the cache, restore the pristine snapshot, export,
   and drop the working database.

Full restore per probe is intentionally conservative and expensive. The MVP favors
correctness over speed. It is unsuitable for very large databases: row identities,
FK closure and candidate dumps require memory/disk proportional to the dataset.

## Boundaries and limitations

- PostgreSQL only; POSIX systems (Linux/macOS) for oracle process-group cleanup.
- Only real, validated FK constraints are analyzed. Semantic relationships without
  constraints are not inferred. Composite keys, duplicate rows and tables without
  primary keys are supported using snapshot-local CTIDs and complete row values.
- Partitioned/inherited/foreign tables and row-level security are explicitly rejected.
- Extension-owned tables are rejected because `pg_dump` may omit their rows.
- Triggers run normally and can change the result. Restrictive constraints can prevent
  otherwise useful reductions. No constraints or security controls are disabled.
- Results are locally irreducible under the attempted FK-closed deletions, not
  mathematically minimal. FK closure deliberately deletes dependent rows even for
  `SET NULL`/`SET DEFAULT` actions, which can miss smaller alternatives.
- Nondeterministic tests can produce incorrect minimization. `--confirm` mitigates,
  but does not solve, flakiness. Cached outcomes assume deterministic behavior.
- The copy preserves schema/data and sequence values, not original database names,
  ownership, grants, role settings, or external infrastructure. Required roles and
  extensions must exist on the destination server. Initial confirmation detects
  some incompatibilities, not unrelated failures.
- Inline `sslpassword` in a DSN is rejected to keep the TLS-key passphrase out of
  PostgreSQL client process arguments. Put it in a libpq service file instead.
- PostgreSQL client operations have a 600-second timeout; connections default to a
  10-second timeout. Oracle executions use `--timeout` independently.
- Do not allow other clients to write into the workspace. Do not use production as
  a workspace. Only generated database names are ever passed to DROP DATABASE.
- Ordinary failures and Ctrl-C clean up the workspace. A killed process, server
  outage, or interruption while CREATE DATABASE is still in flight can leave a
  `dbreduce_<uuid>` database for manual cleanup.
- Dumps/reports contain application data. Store them appropriately; no DSN is saved
  in the report. PostgreSQL errors are intentionally summarized without credentials.

## Development

```bash
uv run pytest -m 'not postgres'
uv run ruff check .
uv run mypy src
```

Opt-in integration tests create/drop only random disposable databases:

```bash
DBREDUCE_TEST_ADMIN=postgresql://localhost/postgres uv run pytest -m postgres
```

They require a PostgreSQL server with `CREATEDB`, `pg_dump`, `pg_restore`, and `psql`.
They cover FK closure, nondeferrable cycles, oracle-write isolation, reduction,
duplicate rows, extension-owned tables, locale, and SQL round-trip. The 12,005-row
example is intended for a manual end-to-end run;
full reduction is deliberately not part of the default test suite.

## Module interfaces

- `reducer.engine.Backend`: `state()` and `attempt(table, rows)`; no PostgreSQL imports.
- `postgres`: introspection, FK deletion, snapshot I/O and workspace lifecycle.
- `oracle.Oracle`: command execution with fresh-state callback for every confirmation.
- `cache`: fingerprints and per-run outcomes.
- `graph`: dependencies and strongly connected components for inspection.
- `cli`: input validation, orchestration, progress and result files.
