# DBReduce guide

DBReduce minimizes PostgreSQL datasets while a failing command still reproduces a bug.
An opt-in phase also removes whole application tables when the same failure survives.
It works only on a randomly named disposable database and exports a SQL reproducer.

## Install

Install the latest release from PyPI:

```bash
python -m pip install dbreduce
dbreduce --help
```

DBReduce v0.4.1 requires Python 3.10 or newer and is tested on Python 3.10–3.14.
The tested PostgreSQL range is 15–18, separately verified
for snapshot, clone, schema reduction and final logical restore.
See [the compatibility decision record](compatibility.md).

Prefer `uv tool install dbreduce` and check `dbreduce --help`.
`uvx dbreduce --help` provides ephemeral execution. `python -m pip install dbreduce`
remains supported. Install the CLI separately from the application; only the
oracle process needs the application's runtime and dependencies.

Install native PostgreSQL client tools (`pg_dump`, `pg_restore`, `psql`) matching
the server major version. A newer `pg_dump`
can read an older server, but its output is not guaranteed to restore back into
that older server. That combination is outside the tested baseline. Use a current,
patched release supporting `pg_dump --restrict-key` (introduced in clients 15.14,
16.10, 17.6 and 18.0). Preserve native client behavior, including `\restrict`.
The connection used for the workspace needs `CREATEDB` and permission to restore the
schema. A separate `--admin-database` DSN can supply those privileges. A read-only
source role is recommended. DBReduce only reads source metadata and uses `pg_dump`;
it never runs reduction SQL on the source.

## Reduce

```bash
dbreduce inspect --database postgresql://localhost/app_bug
dbreduce reduce \
  --database postgresql://localhost/app_bug \
  --oracle 'uv run pytest tests/test_checkout.py::test_negative_total' \
  --confirm 3
```

Use `--reduce-schema` to run whole-table schema reduction after the usual row
reduction. This mode uses a logical custom archive and fresh restore for schema
candidates even when row reduction uses `--candidate-backend clone`. It removes
table data, owned sequences, indexes, defaults, triggers, constraints, and foreign
keys that cannot survive removal of a referenced table. Each accepted candidate
must restore cleanly and reproduce the original oracle identity. Views and other
objects with dependencies not covered by this closure can make a candidate
invalid; those tables remain. Columns, standalone indexes, constraints, functions,
procedures, extensions, and namespaces are not independent reduction targets.

Prefer `--oracle-json` or `--oracle-framed-json` in this mode. A structured
`{"reproduced": false}` or a different signature rejects a candidate. A structured
`error` verdict, malformed output, nonzero structured-oracle exit, or timeout
rejects a schema candidate as an oracle infrastructure error. The report does
not claim local irreducibility when this happens. Baseline and final oracle
infrastructure failures still abort the run. Legacy any-nonzero mode prints an additional warning
because an application startup failure can look like a reproduction.

The oracle **must connect using `DATABASE_URL` or `DBREDUCE_DATABASE_URL`**. Both point
to the working copy and are set for every execution. Hardcoded connections and
external services cannot be redirected or sandboxed by DBReduce. Never point an
oracle at production. Only run trusted commands and restore trusted dumps: SQL
functions and triggers may have external effects.

## Failure identity

Use `--match-stderr 'negative total'` or `--match-stdout 'negative total'` to require
Python regular expressions in the selected stream. Both can be combined; all
conditions must match. `--expected-exit-code 1` optionally pins the exit status.
Without that option, matcher mode captures the initial nonzero exit status and
requires it on every subsequent run. Choose a narrow regex identifying the bug;
a broad regex can still match unrelated failures. Regex matching uses `search`,
without implicit multiline or DOTALL flags; inline flags are supported.

Prefer `--oracle-json` when the oracle can explicitly identify the bug. It must
exit **zero** and write exactly one JSON object to stdout:

```json
{ "reproduced": true, "signature": "checkout-negative-total" }
```

The first reproduced signature becomes the expected identity. Every later verdict
must match it exactly. `{"reproduced": false}` means the bug disappeared. A different
signature is a different failure and rejects the candidate. An `error` field with
a non-null value signals an infrastructure error. Malformed JSON, missing/invalid
fields, and nonzero process status in JSON mode abort reduction. Signature must be
a nonempty string of at most 256 characters. Extra fields are ignored. JSON mode
cannot be combined with exit/output matchers.

For applications that log to stdout, use `--oracle-framed-json`. The command must
exit zero and emit exactly one line beginning `DBREDUCE_VERDICT `, followed by the
same JSON verdict. Other stdout lines are ignored and never saved. Zero or multiple
framed lines, malformed JSON and nonzero exit status are infrastructure errors.
The 1 MiB captured-output limit still applies. Strict `--oracle-json` is unchanged.

Python script:

```python
import json
from app.checkout import NegativeTotalError, checkout

try:
    checkout()
except NegativeTotalError:
    verdict = {"reproduced": True, "signature": "checkout-negative-total"}
else:
    verdict = {"reproduced": False}
print("DBREDUCE_VERDICT " + json.dumps(verdict))
```

Pytest wrapper, where the selected test raises a specific exception for the bug:

```python
import json
import pytest
from app.checkout import NegativeTotalError


class Identity:
    signature = None
    setup_error = False

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        outcome = yield
        if call.excinfo:
            if outcome.get_result().when == "call":
                self.signature = (
                    "checkout-negative-total"
                    if call.excinfo.type is NegativeTotalError
                    else "other-test-failure"
                )
            else:
                self.setup_error = True


identity = Identity()
status = pytest.main(["-q", "tests/test_checkout.py::test_bug"], plugins=[identity])
verdict = (
    {"reproduced": False, "error": "pytest setup failed"}
    if identity.setup_error or status not in (0, 1)
    else {"reproduced": True, "signature": identity.signature}
    if identity.signature
    else {"reproduced": False}
)
print("DBREDUCE_VERDICT " + json.dumps(verdict))
```

Django application oracle, after normal startup logging:

```python
import json
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "project.settings")
import django

django.setup()
from django.db import IntegrityError
from wagtail.models import Collection

try:
    Collection.objects.get(name="Root").add_child(name="Mammals")
except IntegrityError as error:
    target = "wagtailcore_collection" in str(error) and "path" in str(error)
    verdict = {
        "reproduced": True,
        "signature": "collection-path-integrity" if target else "other-integrity-error",
    }
else:
    verdict = {"reproduced": False}
print("DBREDUCE_VERDICT " + json.dumps(verdict))
```

Dockerized application with a database host reachable from the container:

```bash
dbreduce reduce --database "$SOURCE_DATABASE_URL" \
  --oracle 'docker exec -e DATABASE_URL="$DATABASE_URL" app-container python /app/oracle.py' \
  --oracle-framed-json --confirm 3
```

If the host DSN uses `localhost`, translate that host to a container-reachable
address in the wrapper. No verdict file needs to be shared with the container.

`--confirm N` requires N/N matching verdicts, each starting from a fresh snapshot
restore or separate clone of the pristine candidate.
The first mismatch rejects the candidate immediately. The initial and final
confirmation also require N/N; a mismatch there aborts export. This does not prove
stability of a flaky oracle. Cached outcomes assume determinism within one run.

Timeout, process launch errors, and shell statuses 126 or greater abort the run;
they never preserve the bug. This conservatively reserves shell command-not-found
and signal conventions, including application exit codes in that range. A wrapper
that hides a signal behind another exit code cannot be detected reliably.
`--timeout` defaults to 60 seconds for each oracle execution, including failure
matcher evaluation. Remaining oracle process-group children are killed after each
execution, including timeout and Ctrl-C.

Without a matcher or structured mode, legacy mode warns and accepts any nonzero exit
below 126. DB connection failures and other application setup errors cannot be
reliably distinguished from bugs from an exit code alone. A regex can have the same
limitation if an application prints a matching message before failing elsewhere.
Use JSON verdicts and have the wrapper treat setup failures as infrastructure errors.

Only streams needed for identity matching are captured, through pipes with an enforced
1 MiB limit per stream. Overflow aborts and kills the oracle process group. Unused
streams are discarded. No output is written to temporary files.
No stdout/stderr or raw signature is saved in the report.
Structured signatures are reported as SHA-256 digests. Matcher mode reports `exit:N`;
legacy mode reports `any-nonzero`. Configured regexes are included for
reproducibility: do not embed secrets in them.

The source can also be a PostgreSQL **custom-format archive**:

```bash
pg_dump --format=custom --no-owner --no-privileges \
  --file app.dump postgresql://localhost/app_bug
dbreduce reduce --dump app.dump \
  --admin-database postgresql://localhost/postgres \
  --oracle 'uv run python examples/oracle.py' --oracle-json
```

With `--database`, the working copy inherits the source database's encoding and
locale. With `--dump`, DBReduce reads those settings from `pg_restore`'s generated
CREATE DATABASE statement. An archive that does not expose complete settings is
rejected before reduction.
Custom archives whose source database name contains a newline or carriage return
are rejected because `pg_restore` refuses to read their database settings.

Plain SQL is an output format, not an accepted input format. This avoids executing
`psql` reconnect/shell meta-commands while restoring user input.

Candidate isolation defaults to `--candidate-backend snapshot`. PostgreSQL 15–18
can explicitly use `--candidate-backend clone`; see [isolation](isolation.md) for
its ownership checks, replication preflight and final logical normalization.
`--restore-jobs 4` enables parallel custom-archive restores when the environment
benefits; the default remains 1. Clone strategies are `wal_log` (default) and
`file_copy`. PostgreSQL 18 also accepts `--clone-strategy file_copy
--file-copy-method clone` if its capability probe passes. The report distinguishes
configured CLONE from verified filesystem block sharing.

Outputs default to `dbreduce.min.sql` and `dbreduce-report.json`. Existing files are
never overwritten. Override with `--output` and `--report`. Both files are prepared
before publication; an error removes results created by the current run. The report includes
initial/final table and row counts, rows per table, oracle executions, cache hits,
constraint rejections, `RAISE EXCEPTION` candidate rejections, confirmation count,
elapsed seconds, and `restore_database`. Rejection counts identify the cause class;
raw database messages are not stored because they may contain application data. The
`restore_database` field gives the name of the database created by the SQL dump.
Without `--reduce-schema`, tables are kept even when emptied, so the schema
remains available to the oracle.

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
bug. The demo oracle returns the explicit identity `checkout-negative-total`.

Run these commands from a source repository with the development environment
installed as described in [Development](#development).

```bash
createdb dbreduce_demo
psql -X -v ON_ERROR_STOP=1 -d dbreduce_demo -f examples/fixture.sql
uv run dbreduce reduce \
  --database postgresql://localhost/dbreduce_demo \
  --oracle 'uv run python examples/oracle.py' --oracle-json --confirm 2
```

The report shows actual initial/final rows, expected/final signature digests,
identity preservation and elapsed time. See [benchmark](benchmark.md) for the
120,005-row demo and the v0.3 real-world backend comparison.

## Virtual relationships

`inspect` and `reduce` accept `--config relationships.json`:

```json
{
  "relationships": [
    {
      "from": { "table": "public.audit_log", "columns": ["tenant_id", "user_id"] },
      "to": { "table": "public.users", "columns": ["tenant_id", "id"] }
    }
  ]
}
```

Unqualified tables resolve to `public`. Use exact table/column names without SQL
quoting; names containing literal dots are not supported in this small config format.
Single-column and composite relations are supported. Unknown fields, missing tables,
invalid columns and incompatible equality operators are rejected before reduction.
Configuration files use standard JSON.

Deleting a parent includes matching child rows, transitively alongside database
FKs, including cycles. Comparisons use PostgreSQL `=` with its normal type and
collation resolution. NULL key components do not match, like MATCH SIMPLE FKs.
Targets need not be unique: matching any deleted parent marks a child for deletion.
This is an explicit deletion policy, not a new PostgreSQL constraint. It does not
repair preexisting orphans or prevent triggers from creating them. Child deletions
alone do not delete parents. Virtual relationships are not inferred.
Conditional and polymorphic relationships are not supported; `where` is rejected.

Inspect labels edges as `FK` or `virtual`, and includes both in dependency graphs
and strongly connected components (multi-table SCCs indicate cycles; self edges
are visible in the dependency graph). Reports count each kind separately and
record virtual endpoints for reproducibility.

## Reports

The report includes `oracle: "FAIL"` when the final state reproduces the bug.
Other fields include `dbreduce_version`, `failure_identity`, `oracle_stats`,
`candidate_backend`, `candidate_stats`, `relationships`, `performance`, `minimality`
and `transformations`. `oracle_stats.outcomes` counts actual executions by outcome;
`confirmations` is the configured N, not an additional execution count.
`candidate_stats.created` counts attempted deletion probes, including constraint
rejections and cache hits. Accepted/rejected counts partition those completed probes.
`performance.oracle_seconds` excludes preparation and restoration; elapsed time
includes all work. `performance.phases` gives count, total and average for each
operation. Some phases are nested, such as `restore` within oracle state preparation,
so their totals must not be added together. `database_stats` records exact created,
dropped, clone attempts/failures and cleanup failures. `cache_stats` records hit rate
and an estimated time saved from observed oracle cycle cost; it is an estimate, not
an observed counterfactual. Clone mode disables the exact fingerprint cache.
`database_clone_seconds` is zero for the snapshot backend. Aborted runs publish no
verified result or success report. Legacy identity preservation only means the
legacy exit-code policy held; it does not prove that the same bug survived.

With `--reduce-schema`, `schema_reduction` includes initial and final counts,
candidate outcomes, and the exact attempted transformation class. The `indexes`
count includes indexes backing constraints. `schema_objects` counts non-data
archive TOC entries, including defaults and other entries not broken out by kind.
`initial_sql_bytes` and `final_sql_bytes` measure plain logical SQL exports in
the same format; they do not compare compressed and uncompressed files. Schema
candidate caching is disabled. `performance.phases` includes schema planning,
introspection, reconstruction, restore, oracle execution, and final normalization.
`minimality_by_phase` states the row and table conclusions separately; the
existing top-level `minimality` string remains for older report consumers.
The minimality claim means locally irreducible under attempted transformations,
not a global minimum or reduction of every PostgreSQL object class.

## How snapshot mode works

1. Dump the source consistently and restore it into a random `dbreduce_<uuid>` database.
2. Discover tables, primary keys, validated foreign keys, row counts and dependencies.
3. Confirm the initial failure identity; abort if the required verdict does not reproduce.
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

Full restore per probe remains conservative and expensive. See the
[clone backend lifecycle](isolation.md) for the opt-in alternative. DBReduce is
unsuitable for very large databases: row identities,
FK closure and candidate dumps require memory/disk proportional to the dataset.

## Boundaries and limitations

- PostgreSQL only; POSIX systems (Linux/macOS) for oracle process-group cleanup.
- Validated database FKs and explicitly configured virtual relationships are analyzed.
  Semantic relationships are not inferred. Composite keys, duplicate rows and tables without
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
  a workspace. Clone cleanup verifies exact name, OID, owner and run marker before
  dropping each owned database.
- Ordinary failures and Ctrl-C attempt to clean up owned databases. An uncertain
  clone CREATE reply, killed process or server outage can leave a generated database.
  DBReduce reports exact names when it cannot confirm ownership or cleanup; inspect
  them manually. It does not force-disconnect other sessions from clone databases.
- Dumps/reports contain application data. Store them appropriately; no DSN is saved
  in the report. PostgreSQL errors are intentionally summarized without credentials.

## Development

```bash
uv sync --dev
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
